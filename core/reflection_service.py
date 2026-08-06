from __future__ import annotations

import asyncio
import time

from astrbot.api import logger

from .models import (
    apply_deep_evidence,
    apply_light_evidence,
)


class ReflectionService:
    """深度/轻量反思的调度与执行：队列、后台任务、锁内应用证据。"""

    def __init__(self, plugin) -> None:
        """持有插件引用，队列与任务按 user_id 隔离。"""
        self.plugin = plugin
        self.queues: dict[str, list[tuple[int, str]]] = {}
        self.tasks: dict[str, asyncio.Task] = {}

    def enqueue(self, user_id: str, target_round: int, kind: str) -> bool:
        """按轮次排序入队；无运行中任务时启动 worker。"""
        if target_round <= 0 or kind not in {"light", "deep"} or not self.plugin.storage.is_user_enabled(user_id):
            return False
        queue = self.queues.setdefault(user_id, [])
        item = (target_round, kind)
        added = item not in queue
        if added:
            queue.append(item)
            queue.sort(key=lambda value: (value[0], value[1] != "deep"))
            logger.info(
                "[CLV2] 已调度%s分析 user=%s round=%s",
                "轻" if kind == "light" else "深",
                user_id,
                target_round,
            )
        task = self.tasks.get(user_id)
        if task and not task.done():
            return True
        task = asyncio.create_task(self.worker(user_id))
        self.tasks[user_id] = task
        self.plugin._background_tasks.add(task)
        task.add_done_callback(lambda done: self.done(user_id, done))
        return True

    def done(self, user_id: str, task: asyncio.Task) -> None:
        """任务结束回调：清理句柄并记录异常。"""
        self.plugin._background_tasks.discard(task)
        if self.tasks.get(user_id) is task:
            self.tasks.pop(user_id, None)
        if not task.cancelled() and task.exception():
            logger.warning(
                "[CLV2] 反思任务异常 user=%s: %s",
                user_id,
                task.exception(),
            )

    async def worker(self, user_id: str) -> None:
        """串行消费该用户的反思队列，直至队列清空或插件停止。"""
        queue = self.queues.setdefault(user_id, [])
        while queue and self.plugin._initialized:
            if not self.plugin.storage.is_user_enabled(user_id):
                queue.clear()
                break
            target_round, kind = queue.pop(0)
            await self.perform(user_id, target_round, kind)

    async def perform(self, user_id: str, target_round: int, kind: str) -> bool:
        """在分析锁内执行一轮反思。"""
        async with self.plugin._analysis_lock(user_id):
            return await self.perform_locked(user_id, target_round, kind)

    async def _prepare(self, user_id: str, target_round: int, kind: str):
        """取消息并做前置检查；返回 (messages, relationship_role, already_done)。"""
        if not self.plugin.storage.is_user_enabled(user_id):
            return [], "", False
        if kind == "deep":
            messages = self.plugin.storage.get_recent_messages(
                user_id,
                limit=20,
                up_to_round=target_round,
                completed_only=True,
            )
        else:
            messages = self.plugin.storage.get_completed_rounds(user_id, 2, up_to_round=target_round)
        if len(messages) < 2:
            return [], "", False
        state = await self.plugin._load_state(user_id)
        if kind == "deep" and state.last_deep_round >= target_round:
            return [], "", True
        if kind == "light" and target_round <= state.last_deep_round:
            return [], "", True
        if (
            kind == "light"
            and state.last_analysis_kind == "light"
            and state.last_analysis_round >= target_round
            and state.last_analysis_status in {"signal", "none"}
        ):
            return [], "", True
        persona_resolution = await self.plugin.persona.resolve_persona_id(user_id)
        relationship_role = self.plugin.persona.relationship_role(state, persona_resolution)
        return messages, relationship_role, False

    async def _mark_running(self, user_id: str, target_round: int, kind: str):
        """锁内标记分析开始，返回最新状态供模型调用使用。"""
        async with self.plugin._response_lock(user_id):
            latest = await self.plugin._load_state(user_id)
            latest.last_analysis_kind = kind
            latest.last_analysis_round = target_round
            latest.last_analysis_status = "running"
            latest.last_analysis_signal = ""
            latest.last_analysis_confidence = ""
            latest.last_analysis_note = "正在检查最近两个完整来回" if kind == "light" else "正在综合近期关系状态"
            latest.last_analysis_at = time.time()
            self.plugin._save_state(latest)
            return latest

    async def _apply_outcome(
        self,
        user_id: str,
        target_round: int,
        kind: str,
        outcome,
        relationship_role: str,
    ) -> bool:
        """锁内应用模型证据或记录无效结果，返回整体是否成功。"""
        if outcome.value is None:
            async with self.plugin._response_lock(user_id):
                latest = await self.plugin._load_state(user_id)
                if latest.last_analysis_kind == kind and latest.last_analysis_round == target_round:
                    latest.last_analysis_status = "invalid"
                    latest.last_analysis_note = "模型返回为空、调用失败或输出格式不符合约定"
                    latest.last_analysis_trace = outcome.trace
                    latest.last_analysis_at = time.time()
                    self.plugin._save_state(latest)
            logger.warning(
                "[CLV2] %s分析无有效结果 user=%s round=%s",
                "轻" if kind == "light" else "深",
                user_id,
                target_round,
            )
            return False

        async with self.plugin._response_lock(user_id):
            latest = await self.plugin._load_state(user_id)
            if latest.round_sequence < target_round:
                return False
            if kind == "light":
                if target_round <= latest.last_deep_round:
                    return False
                evidence = outcome.value
                decision = apply_light_evidence(
                    latest,
                    evidence,
                    target_round,
                    is_bonded=relationship_role == "bonded",
                )
                latest.last_analysis_status = "none" if evidence.signal == "none" else "signal"
                latest.last_analysis_signal = evidence.signal
                latest.last_analysis_confidence = evidence.confidence
                latest.last_analysis_note = (
                    "未发现需要提醒主人格的关系信号" if evidence.signal == "none" else "模型只提交关系证据，提醒由代码生成"
                )
            else:
                if target_round <= latest.last_deep_round:
                    logger.info(
                        "[CLV2] 深分析结果已过期 user=%s round=%s last_deep_round=%s",
                        user_id,
                        target_round,
                        latest.last_deep_round,
                    )
                    return True
                evidence = outcome.value
                decision = apply_deep_evidence(
                    latest,
                    evidence,
                    target_round,
                    is_bonded=relationship_role == "bonded",
                )
                latest.last_analysis_status = "applied"
                latest.last_analysis_signal = evidence.pattern
                latest.last_analysis_confidence = evidence.confidence
                latest.last_analysis_note = "模型观察已由代码规则裁决并更新关系状态"
            trace = dict(outcome.trace)
            trace["code_decision"] = decision
            latest.last_analysis_trace = trace
            latest.last_analysis_kind = kind
            latest.last_analysis_round = target_round
            latest.last_analysis_at = time.time()
            self.plugin._save_state(latest)
        logger.info(
            "[CLV2] %s分析完成 user=%s round=%s status=%s signal=%s confidence=%s",
            "轻" if kind == "light" else "深",
            user_id,
            target_round,
            latest.last_analysis_status,
            latest.last_analysis_signal or "-",
            latest.last_analysis_confidence or "-",
        )
        return True

    async def perform_locked(self, user_id: str, target_round: int, kind: str) -> bool:
        """执行单轮反思：准备输入、标记运行、调模型并应用结果。"""
        messages, relationship_role, already_done = await self._prepare(user_id, target_round, kind)
        if already_done:
            return True
        if not messages:
            return False
        state = await self._mark_running(user_id, target_round, kind)
        if kind == "light":
            outcome = await self.plugin.reflection.analyze_light(
                state,
                messages,
                target_round,
                relationship_role=relationship_role,
            )
        else:
            outcome = await self.plugin.reflection.analyze_deep(
                state,
                messages,
                persona_prompt=self.plugin._persona_by_user.get(user_id, ""),
                relationship_role=relationship_role,
            )
        return await self._apply_outcome(user_id, target_round, kind, outcome, relationship_role)
