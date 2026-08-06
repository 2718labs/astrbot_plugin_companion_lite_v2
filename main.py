from __future__ import annotations

import asyncio
import hashlib
import re
import time
import uuid
from pathlib import Path
from typing import Any

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.event.filter import PermissionType
from astrbot.api.platform import MessageType
from astrbot.api.star import Context, Star

try:
    from astrbot.core.agent.message import TextPart
except ImportError:
    TextPart = None

try:
    from astrbot.core.utils.astrbot_path import get_astrbot_data_path
except ImportError:

    def get_astrbot_data_path() -> str:
        return str(Path(".").resolve())


from .config import load_config
from .core import (
    CommandsController,
    PersonaService,
    ReflectionService,
    RelationshipState,
    SilenceBridgeController,
    Storage,
    WebUIController,
    analysis_kind_for_round,
    apply_severe_evidence,
)
from .llm import (
    COMPANION_STATIC_PROTOCOL,
    ContextBuilder,
    LLMCallResult,
    RelationshipReflection,
    detect_severe_candidate,
)


SYSTEM_COMMAND_RE = re.compile(r"(?:^|\s)/[A-Za-z0-9_\-\u4e00-\u9fff]+(?:\s|$)")
PROCESSED_EXTRA = "_companion_lite_v2_processed"
INTERACTION_KEY_EXTRA = "_companion_lite_v2_interaction_key"
INJECTED_EXTRA = "_companion_lite_v2_injected"


class CompanionLiteV2Plugin(Star):
    """AstrBot 入口：装配核心服务，只保留框架事件入口与共享运行时设施。"""

    def __init__(self, context: Context, config: dict[str, Any] | None = None) -> None:
        """初始化存储、上下文构建与各核心服务，并注册调试页 API。"""
        super().__init__(context)
        self.context = context
        self.plugin_config = load_config(config)
        self.raw_config = config
        data_dir = Path(get_astrbot_data_path()) / "plugin_data" / "astrbot_plugin_companion_lite_v2"
        data_dir.mkdir(parents=True, exist_ok=True)
        self.storage = Storage(str(data_dir / "companion_lite_v2.db"))
        self.context_builder = ContextBuilder()
        self.reflection = RelationshipReflection(
            self._llm_generate,
            provider_id=self.plugin_config.reflection_provider_id,
            persona_prompt=self.plugin_config.persona_prompt,
        )
        self._initialized = False
        self._response_locks: dict[str, asyncio.Lock] = {}
        self._analysis_locks: dict[str, asyncio.Lock] = {}
        self._background_tasks: set[asyncio.Task] = set()
        self._persona_by_user: dict[str, str] = {}
        self._bond_lock = asyncio.Lock()
        self.persona = PersonaService(self)
        self.silence_bridge = SilenceBridgeController(self)
        self.reflection_service = ReflectionService(self)
        self.webui = WebUIController(self)
        self.commands = CommandsController(self)
        self.webui.register()

    async def initialize(self) -> None:
        """标记就绪并补调度插件重载期间错过的深度/轻量分析。"""
        self._initialized = True
        recovered = 0
        for item in self.storage.list_states():
            if not bool(item.get("enabled", True)):
                continue
            state = RelationshipState.from_dict(
                item.get("state"),
                user_id=str(item.get("user_id") or ""),
            )
            if state.last_analysis_status == "running":
                state.last_analysis_status = "interrupted"
                state.last_analysis_note = "插件重载时分析尚未完成"
                state.last_analysis_at = time.time()
                self._save_state(state)

            deep_target = (state.round_sequence // 6) * 6
            if deep_target > state.last_deep_round:
                recovered += int(self.reflection_service.enqueue(state.user_id, deep_target, "deep"))

            latest_even = state.round_sequence - (state.round_sequence % 2)
            if (
                analysis_kind_for_round(latest_even) == "light"
                and latest_even > state.last_deep_round
                and (latest_even > state.last_analysis_round or state.last_analysis_status == "interrupted")
                and state.round_sequence <= latest_even + 2
            ):
                recovered += int(self.reflection_service.enqueue(state.user_id, latest_even, "light"))
        logger.info(
            "[CLV2] 初始化完成: mode=%s, 使用独立V2数据库, 补调度=%s",
            self.plugin_config.operation_mode,
            recovered,
        )

    async def terminate(self) -> None:
        """停止后台任务、还原桥接并关闭 V2 独立数据库连接。"""
        self._initialized = False
        if self.silence_bridge.managed:
            await self.silence_bridge.sync(force_restore=True)
        for task in list(self._background_tasks):
            task.cancel()
        self.storage.close()

    async def _llm_generate(
        self,
        prompt: str,
        system_prompt: str = "",
        provider_id: str = "",
        timeout_seconds: int = 45,
    ) -> LLMCallResult:
        """按 provider 偏好调用分析模型，统一收敛为 LLMCallResult。"""
        try:
            provider = self.context.get_provider_by_id(provider_id) if provider_id else None
            provider = provider or self.context.get_using_provider(None)
            if provider is None:
                return LLMCallResult(error="provider_unavailable")
            async with asyncio.timeout(max(1, int(timeout_seconds))):
                response = await provider.text_chat(prompt=prompt, system_prompt=system_prompt, contexts=[])
            usage = getattr(response, "usage", None)
            return LLMCallResult(
                text=str(response.completion_text or ""),
                input_other=getattr(usage, "input_other", None),
                input_cached=getattr(usage, "input_cached", None),
                output=getattr(usage, "output", None),
            )
        except TimeoutError:
            logger.warning("[CLV2] 分析LLM调用超时（%s秒）", timeout_seconds)
            return LLMCallResult(error="timeout")
        except Exception as exc:
            logger.warning("[CLV2] 反思LLM调用失败: %s", exc)
            return LLMCallResult(error=type(exc).__name__)

    def _response_lock(self, user_id: str) -> asyncio.Lock:
        return self._response_locks.setdefault(user_id, asyncio.Lock())

    def _analysis_lock(self, user_id: str) -> asyncio.Lock:
        return self._analysis_locks.setdefault(user_id, asyncio.Lock())

    @staticmethod
    def _is_private(event: AstrMessageEvent) -> bool:
        actual = event.get_message_type()
        accepted = {
            value
            for value in (
                getattr(MessageType, "PRIVATE_MESSAGE", None),
                getattr(MessageType, "FRIEND_MESSAGE", None),
            )
            if value is not None
        }
        return actual in accepted

    @staticmethod
    def _is_system_command_text(text: str) -> bool:
        stripped = str(text or "").strip()
        return not stripped or stripped.startswith(("!", "#")) or bool(SYSTEM_COMMAND_RE.search(stripped))

    def _should_capture_text(self, text: str) -> bool:
        return (
            self.plugin_config.enable_message_capture
            and not self._is_system_command_text(text)
            and len(text) >= self.plugin_config.min_message_length
        )

    def _truncate_captured_text(self, text: str) -> str:
        value = str(text or "")
        limit = self.plugin_config.max_message_length
        if len(value) <= limit:
            return value
        marker = f"\n…[中间内容已截断，原文共 {len(value)} 字]…\n"
        available = limit - len(marker)
        if available <= 1:
            return value[:limit]
        head_length = max(1, available * 2 // 3)
        tail_length = available - head_length
        if tail_length <= 0:
            return value[:limit]
        return value[:head_length] + marker + value[-tail_length:]

    @staticmethod
    def _event_value(event: AstrMessageEvent, name: str) -> str:
        value = getattr(event, name, "")
        if callable(value):
            try:
                value = value()
            except TypeError:
                value = ""
        return str(value or "")

    def _user_identity(self, event: AstrMessageEvent) -> str:
        message_obj = getattr(event, "message_obj", None)
        sender = self._event_value(event, "get_sender_id")
        unified_msg_origin = (
            self._event_value(event, "unified_msg_origin") or str(getattr(message_obj, "unified_msg_origin", "") or "").strip()
        )
        if unified_msg_origin:
            return unified_msg_origin
        platform = (
            self._event_value(event, "get_platform_name")
            or self._event_value(event, "platform_name")
            or str(getattr(message_obj, "platform_name", "") or "")
        )
        session = (
            self._event_value(event, "get_session_id")
            or self._event_value(event, "session_id")
            or str(getattr(message_obj, "session_id", "") or "")
        )
        if not sender:
            return ""
        platform_key = platform or "unknown-platform"
        if session.count(":") >= 2:
            return session
        session_target = session or sender
        return f"{platform_key}:FriendMessage:{session_target}"

    def _interaction_key(self, event: AstrMessageEvent, user_id: str) -> str:
        message_obj = getattr(event, "message_obj", None)
        message_id = str(getattr(message_obj, "message_id", "") or "").strip()
        if message_id:
            digest = hashlib.sha256(f"{user_id}\0{message_id}".encode("utf-8")).hexdigest()
            return f"message:{digest}"
        if hasattr(event, "get_extra"):
            existing = str(event.get_extra(INTERACTION_KEY_EXTRA, "") or "")
            if existing:
                return existing
        generated = f"event:{uuid.uuid4()}"
        if hasattr(event, "set_extra"):
            event.set_extra(INTERACTION_KEY_EXTRA, generated)
        return generated

    async def _load_state(self, user_id: str) -> RelationshipState:
        raw = self.storage.get_state(user_id)
        state = RelationshipState.from_dict(raw, user_id=user_id)
        if raw is None:
            self.storage.save_state(user_id, state.to_dict())
        return state

    def _save_state(self, state: RelationshipState) -> None:
        self.storage.save_state(state.user_id, state.to_dict())

    async def _capture_user_message(self, event: AstrMessageEvent, user_id: str, text: str) -> bool:
        if not self._should_capture_text(text):
            return False
        key = self._interaction_key(event, user_id)
        async with self._response_lock(user_id):
            if not self.storage.is_user_enabled(user_id):
                return False
            claimed = self.storage.claim_interaction(
                key,
                user_id,
                self._truncate_captured_text(text),
            )
            if claimed:
                await self._load_state(user_id)
            return claimed

    async def _run_severe_precheck(self, user_id: str, text: str) -> None:
        async with self._response_lock(user_id):
            if not self.storage.is_user_enabled(user_id):
                return
            state = await self._load_state(user_id)
            candidate = detect_severe_candidate(text, state)
            next_round = state.round_sequence + 1
            window = state.round_sequence // 6
            if state.severe_window != window:
                state.severe_window = window
                state.severe_confirmation_count = 0
                state.severe_last_message_hash = ""

            trace: dict[str, Any] = {
                "gate_hit": candidate.hit,
                "gate_category": candidate.category,
                "gate_reason": candidate.reason,
                "confirmation_called": False,
                "skip_reason": "",
                "target_round": next_round,
            }
            if not candidate.hit:
                state.last_precheck_trace = trace
                self._save_state(state)
                return
            if (
                state.active_issue
                and state.active_issue.kind in {"boundary_violation", "degradation", "coercion"}
                and state.posture in {"guarded", "disengaged"}
            ):
                trace["skip_reason"] = "existing_severe_issue"
                state.last_precheck_trace = trace
                self._save_state(state)
                return
            if candidate.message_hash and candidate.message_hash == state.severe_last_message_hash:
                trace["skip_reason"] = "duplicate_message"
                state.last_precheck_trace = trace
                self._save_state(state)
                return
            if state.severe_confirmation_count >= 2:
                trace["skip_reason"] = "window_limit"
                state.last_precheck_trace = trace
                self._save_state(state)
                return

            state.severe_confirmation_count += 1
            state.severe_last_message_hash = candidate.message_hash
            trace["confirmation_called"] = True
            self._save_state(state)
            outcome = await self.reflection.analyze_severe(state, text)
            trace["analysis"] = outcome.trace
            if outcome.value is None:
                trace["skip_reason"] = outcome.trace.get("error") or "invalid_result"
            else:
                decision = apply_severe_evidence(state, outcome.value, next_round)
                trace["code_decision"] = decision
            state.last_precheck_trace = trace
            self._save_state(state)

    @filter.on_llm_request(priority=-10)
    async def inject_companion_context(self, event: AstrMessageEvent, req=None) -> None:
        if not self._initialized or req is None or not self._is_private(event):
            return
        await self.silence_bridge.sync()
        text = str(event.get_message_str() or "").strip()
        if self._is_system_command_text(text):
            return
        user_id = self._user_identity(event)
        if not user_id or not self.storage.is_user_enabled(user_id):
            return
        processed = bool(event.get_extra(PROCESSED_EXTRA, False)) if hasattr(event, "get_extra") else False
        if not processed:
            captured = await self._capture_user_message(event, user_id, text)
            if captured and hasattr(event, "set_extra"):
                event.set_extra(PROCESSED_EXTRA, True)

        if not self.storage.is_user_enabled(user_id):
            return
        persona_resolution = await self.persona.resolve_persona_id(user_id)

        await self._run_severe_precheck(user_id, text)

        async with self._response_lock(user_id):
            if not self.storage.is_user_enabled(user_id):
                return
            self.persona.remember(user_id, req)
            state = await self._load_state(user_id)
            relationship_role = self.persona.relationship_role(state, persona_resolution)
            compiled = self.context_builder.build(
                state,
                max_chars=self.plugin_config.max_context_chars,
                next_round=state.round_sequence + 1,
                relationship_role=relationship_role,
            )
            already_injected = bool(event.get_extra(INJECTED_EXTRA, False)) if hasattr(event, "get_extra") else False
            should_inject = self.plugin_config.active and bool(compiled) and not already_injected
            state.last_compiled_context = compiled
            state.last_context_injected = bool(self.plugin_config.active and compiled and (should_inject or already_injected))
            state.last_context_at = time.time()
            self._save_state(state)

            if should_inject:
                if hasattr(event, "set_extra"):
                    event.set_extra(INJECTED_EXTRA, True)
                current_system_prompt = str(getattr(req, "system_prompt", "") or "")
                if COMPANION_STATIC_PROTOCOL not in current_system_prompt:
                    req.system_prompt = (
                        f"{COMPANION_STATIC_PROTOCOL}\n\n{current_system_prompt}"
                        if current_system_prompt
                        else COMPANION_STATIC_PROTOCOL
                    )
                if not self._append_extra_user_content(req, compiled):
                    req.prompt = (f"{getattr(req, 'prompt', '')}\n\n{compiled}").strip()
            if self.plugin_config.active:
                if self.silence_bridge.should_inject_silence(state):
                    self.silence_bridge.append_prompt(event, req, state)
                if self.silence_bridge.consume_recovery(state, event, req):
                    self._save_state(state)

    @filter.on_llm_response(priority=-10)
    async def capture_llm_response(self, event: AstrMessageEvent, resp=None) -> None:
        if not self._initialized or resp is None or not self._is_private(event) or not self.plugin_config.enable_message_capture:
            return
        if (
            getattr(resp, "role", "assistant") != "assistant"
            or getattr(resp, "tools_call_name", None)
            or getattr(resp, "tools_call_extra_content", None)
        ):
            return
        raw_assistant_text = str(getattr(resp, "completion_text", "") or "").strip()
        if not raw_assistant_text:
            return
        assistant_text = self._truncate_captured_text(raw_assistant_text)
        user_id = self._user_identity(event)
        if not user_id or not self.storage.is_user_enabled(user_id):
            return
        key = self._interaction_key(event, user_id)
        async with self._response_lock(user_id):
            if not self.storage.is_user_enabled(user_id):
                return
            state = await self._load_state(user_id)
            next_round = state.round_sequence + 1
            if not self.storage.complete_interaction(
                key,
                user_id,
                assistant_text,
                completed_round=next_round,
            ):
                return
            state.round_sequence = next_round
            if self.plugin_config.bridge_polite_silence:
                event_info = self.silence_bridge.extract_ignore_event(raw_assistant_text)
                if event_info is not None and self.silence_bridge.resolve_polite_silence() is not None:
                    event_info["duration_minutes"] = self.silence_bridge.clamp_duration(
                        event_info["duration_minutes"]
                    )
                    event_info["source_round"] = next_round
                    event_info["created_at"] = time.time()
                    state.last_silence_event = event_info
                    state.silence_count += 1
                    logger.info(
                        "[CLV2] 已记录拒答事件 target=%s minutes=%s",
                        event_info["target_id"],
                        event_info["duration_minutes"],
                    )
            self._save_state(state)
            self.storage.trim_completed_rounds(user_id, self.plugin_config.max_buffer_rounds)

        deep_target = (next_round // 6) * 6
        if deep_target > state.last_deep_round:
            self.reflection_service.enqueue(user_id, deep_target, "deep")
        elif analysis_kind_for_round(next_round) == "light":
            self.reflection_service.enqueue(user_id, next_round, "light")

    @filter.command("clv2_status")
    @filter.permission_type(PermissionType.ADMIN)
    @filter.event_message_type(filter.EventMessageType.PRIVATE_MESSAGE)
    async def cmd_status(self, event: AstrMessageEvent):
        async for item in self.commands.status(event):
            yield item

    @filter.command("companion_bond")
    @filter.permission_type(PermissionType.ADMIN)
    @filter.event_message_type(filter.EventMessageType.PRIVATE_MESSAGE)
    async def cmd_bond(self, event: AstrMessageEvent):
        async for item in self.commands.bond(event):
            yield item

    @filter.command("companion_unbond")
    @filter.permission_type(PermissionType.ADMIN)
    @filter.event_message_type(filter.EventMessageType.PRIVATE_MESSAGE)
    async def cmd_unbond(self, event: AstrMessageEvent):
        async for item in self.commands.unbond(event):
            yield item

    @filter.command("clv2_reset")
    @filter.permission_type(PermissionType.ADMIN)
    @filter.event_message_type(filter.EventMessageType.PRIVATE_MESSAGE)
    async def cmd_reset(self, event: AstrMessageEvent):
        async for item in self.commands.reset(event):
            yield item

    @filter.command("clv2_reflect")
    @filter.permission_type(PermissionType.ADMIN)
    @filter.event_message_type(filter.EventMessageType.PRIVATE_MESSAGE)
    async def cmd_reflect(self, event: AstrMessageEvent):
        async for item in self.commands.reflect(event):
            yield item

    @staticmethod
    def _append_extra_user_content(req: Any, text: str) -> bool:
        parts = getattr(req, "extra_user_content_parts", None)
        if TextPart is None or parts is None or not hasattr(parts, "append"):
            return False
        try:
            part = TextPart(text=text)
            if hasattr(part, "mark_as_temp"):
                marked = part.mark_as_temp()
                parts.append(marked or part)
            else:
                parts.append(part)
            return True
        except Exception as exc:
            logger.debug("[CLV2] 追加额外用户内容失败 error=%s", exc)
            return False
