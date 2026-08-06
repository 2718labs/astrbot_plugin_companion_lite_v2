from __future__ import annotations

import functools
import time
from typing import Any
from urllib.parse import unquote

from astrbot.api import logger

from .. import __version__
from ..config import load_config
from .models import (
    RelationshipState,
    analysis_kind_for_round,
    apply_deep_evidence,
    apply_light_evidence,
)
from .persona import PersonaResolution
from .protocol import COMPANION_PROTOCOL_VERSION
from . import web as web_mod

PAGE_PREFIX = "/astrbot_plugin_companion_lite_v2/page"


class WebUIController:
    """调试页 Web API 与档案管理操作：注册路由、查询/重置/重建关系档案。"""

    def __init__(self, plugin) -> None:
        """持有插件引用；路由在插件初始化时注册。"""
        self.plugin = plugin

    @staticmethod
    def _guarded(handler):
        """包装 API handler：异常统一收敛为 JSON 错误响应，避免裸 500。"""

        @functools.wraps(handler)
        async def wrapped(*args, **kwargs):
            try:
                return await handler(*args, **kwargs)
            except Exception as exc:
                logger.exception("[CLV2] API 处理失败 error=%s", exc)
                return web_mod.json_response({"error": "internal_error"})

        return wrapped

    def register(self) -> None:
        """向 AstrBot 注册 /page 前缀下的全部调试 API。"""
        if not hasattr(self.plugin.context, "register_web_api"):
            return
        context = self.plugin.context
        context.register_web_api(
            f"{PAGE_PREFIX}/state",
            self._guarded(self.api_state),
            ["GET"],
            "Get V2 relationship state",
        )
        context.register_web_api(
            f"{PAGE_PREFIX}/sessions",
            self._guarded(self.api_sessions),
            ["GET"],
            "List V2 private relationship sessions",
        )
        context.register_web_api(
            f"{PAGE_PREFIX}/messages",
            self._guarded(self.api_messages),
            ["GET"],
            "Get V2 message buffer",
        )
        context.register_web_api(
            f"{PAGE_PREFIX}/health",
            self._guarded(self.api_health),
            ["GET"],
            "Get V2 plugin health",
        )
        context.register_web_api(
            f"{PAGE_PREFIX}/reset",
            self._guarded(self.api_reset),
            ["POST"],
            "Reset V2 relationship state",
        )
        context.register_web_api(
            f"{PAGE_PREFIX}/reset/<path:user_id>",
            self._guarded(self.api_reset_path),
            ["POST"],
            "Reset V2 relationship state by UMO",
        )
        context.register_web_api(
            f"{PAGE_PREFIX}/enabled",
            self._guarded(self.api_enabled),
            ["POST"],
            "Enable or disable V2 processing for an existing UMO",
        )
        context.register_web_api(
            f"{PAGE_PREFIX}/reflect",
            self._guarded(self.api_reflect),
            ["POST"],
            "Run V2 deep reflection",
        )
        context.register_web_api(
            f"{PAGE_PREFIX}/rebuild",
            self._guarded(self.api_rebuild),
            ["POST"],
            "Rebuild V2 relationship state while preserving messages",
        )
        context.register_web_api(
            f"{PAGE_PREFIX}/silence_bridge",
            self._guarded(self.api_silence_bridge),
            ["POST"],
            "Set polite_silence bridge enabled state",
        )

    async def api_state(self):
        """返回指定 UMO 的完整关系状态载荷。"""
        user_id = await self.resolve_user_id()
        if not user_id:
            return web_mod.json_response({"error": "user_id_required"})
        state = await self.plugin._load_state(user_id)
        resolution = await self.plugin.persona.resolve_persona_id(user_id)
        return web_mod.json_response(self.state_payload(state, resolution))

    async def api_sessions(self):
        """列出全部私聊关系会话，限制在管理页安全上限内。"""
        sessions: list[dict[str, Any]] = []
        for item in self.plugin.storage.list_states(limit=1000):
            state = RelationshipState.from_dict(
                item.get("state"),
                user_id=str(item.get("user_id") or ""),
            )
            sessions.append(
                {
                    "user_id": state.user_id,
                    **self.identity_parts(state.user_id),
                    "round_sequence": state.round_sequence,
                    "relationship_stage": state.relationship_stage,
                    "familiarity": state.familiarity,
                    "trust": state.trust,
                    "affinity": state.affinity,
                    "posture": state.posture,
                    "former_bond": state.former_bond,
                    "enabled": bool(item.get("enabled", True)),
                    "updated_at": item.get("updated_at", 0),
                }
            )
        return web_mod.json_response({"sessions": sessions, "count": len(sessions)})

    async def api_messages(self):
        """返回指定 UMO 的最近消息缓冲。"""
        user_id = await self.resolve_user_id()
        if not user_id:
            return web_mod.json_response({"error": "user_id_required"})
        limit = web_mod.request.query.get("limit", 40, type=int) if web_mod.request is not None else 40
        messages = self.plugin.storage.get_recent_messages(user_id, limit=limit)
        return web_mod.json_response({"messages": messages, "count": len(messages)})

    async def api_health(self):
        """返回插件健康状态与桥接运行摘要。"""
        return web_mod.json_response(
            {
                "initialized": self.plugin._initialized,
                "plugin_id": "astrbot_plugin_companion_lite_v2",
                "version": __version__,
                "operation_mode": self.plugin.plugin_config.operation_mode,
                "active_injection": self.plugin.plugin_config.active,
                "background_tasks": len(self.plugin._background_tasks),
                "silence_bridge": self.plugin.silence_bridge.payload(),
            }
        )

    async def api_reset(self):
        """按请求体/查询参数中的 UMO 重置档案。"""
        user_id = await self.resolve_user_id()
        if not user_id:
            return web_mod.json_response({"error": "user_id_required"})
        if not self.plugin.storage.has_state(user_id):
            return web_mod.json_response({"error": "unknown_umo"})
        return await self.reset_response(user_id)

    async def api_reset_path(self, user_id: str):
        """兼容 URL 编码路径的 UMO 重置入口。"""
        user_id = str(user_id or "").strip()
        if not user_id:
            return web_mod.json_response({"error": "user_id_required"})
        for _ in range(3):
            if self.plugin.storage.has_state(user_id):
                return await self.reset_response(user_id)
            decoded = unquote(user_id)
            if decoded == user_id:
                break
            user_id = decoded
        return web_mod.json_response({"error": "unknown_umo"})

    async def api_enabled(self):
        """启停指定 UMO 的采集、注入与分析。"""
        if web_mod.request is None:
            return web_mod.json_response({"error": "request_unavailable"})
        try:
            body = await web_mod.request.json({})
        except Exception as exc:
            logger.debug("[CLV2] 请求体解析失败 error=%s", exc)
            body = None
        if not isinstance(body, dict):
            return web_mod.json_response({"error": "invalid_request_body"})
        user_id = str(body.get("user_id", "") or "").strip()
        enabled_value = body.get("enabled")
        if not user_id:
            return web_mod.json_response({"error": "user_id_required"})
        if not isinstance(enabled_value, bool):
            return web_mod.json_response({"error": "invalid_enabled_value"})
        result = await self.set_user_enabled(user_id, enabled_value)
        return web_mod.json_response(result)

    async def api_reflect(self):
        """手动触发一次深度反思。"""
        user_id = await self.resolve_user_id()
        if not user_id:
            return web_mod.json_response({"error": "user_id_required"})
        if not self.plugin.storage.is_user_enabled(user_id):
            return web_mod.json_response({"ok": False, "user_id": user_id, "error": "umo_disabled"})
        state = await self.plugin._load_state(user_id)
        ok = await self.plugin.reflection_service.perform(user_id, state.round_sequence, "deep")
        return web_mod.json_response({"ok": ok, "user_id": user_id})

    async def api_rebuild(self):
        """observe 模式下按既有消息重建关系档案。"""
        user_id = await self.resolve_user_id()
        if not user_id:
            return web_mod.json_response({"error": "user_id_required"})
        ok, error, state = await self.rebuild_profile(user_id)
        payload: dict[str, Any] = {
            "ok": ok,
            "user_id": user_id,
            "error": error,
        }
        if state is not None:
            resolution = await self.plugin.persona.resolve_persona_id(user_id)
            payload["state"] = self.state_payload(state, resolution)
        return web_mod.json_response(payload)

    async def api_silence_bridge(self):
        """WebUI 开关：写回 AstrBotConfig 并持久化，立即接管/还原桥接。"""
        if web_mod.request is None:
            return web_mod.json_response({"error": "request_unavailable"})
        try:
            body = await web_mod.request.json({})
        except Exception as exc:
            logger.debug("[CLV2] 请求体解析失败 error=%s", exc)
            body = None
        if not isinstance(body, dict) or not isinstance(body.get("enabled"), bool):
            return web_mod.json_response({"error": "invalid_enabled_value"})
        if self.plugin.raw_config is None:
            return web_mod.json_response({"error": "config_unavailable"})
        enabled = body["enabled"]
        try:
            raw_config = self.plugin.raw_config
            silence_group = raw_config.get("Silence_Bridge_Settings")
            if not isinstance(silence_group, dict):
                silence_group = {}
                raw_config["Silence_Bridge_Settings"] = silence_group
            silence_group["bridge_polite_silence"] = enabled
            saver = getattr(raw_config, "save_config", None)
            if callable(saver):
                saver()
        except Exception as exc:
            logger.warning("[CLV2] 保存桥接配置失败 error=%s", exc)
            return web_mod.json_response({"error": "save_failed"})
        self.plugin.plugin_config = load_config(self.plugin.raw_config)
        await self.plugin.silence_bridge.sync()
        return web_mod.json_response(
            {
                "ok": True,
                "enabled": enabled,
                "silence_bridge": self.plugin.silence_bridge.payload(),
            }
        )

    async def resolve_user_id(self) -> str:
        """从查询参数或 JSON 请求体解析目标 UMO。"""
        if web_mod.request is None:
            return ""
        user_id = str(web_mod.request.query.get("user_id", "") or "").strip()
        if user_id:
            return user_id
        try:
            body = await web_mod.request.json({})
        except Exception as exc:
            logger.debug("[CLV2] 请求体解析失败 error=%s", exc)
            body = None
        if not isinstance(body, dict):
            return ""
        return str(body.get("user_id", "") or "").strip()

    async def set_user_enabled(self, user_id: str, enabled: bool) -> dict[str, Any]:
        """按 UMO 启停处理，关闭时清空其反思队列与运行时人格。"""
        if not self.plugin.storage.has_state(user_id):
            return {"ok": False, "error": "unknown_umo", "user_id": user_id}
        async with self.plugin._analysis_lock(user_id), self.plugin._response_lock(user_id):
            if not enabled:
                queue = self.plugin.reflection_service.queues.get(user_id)
                if queue is not None:
                    queue.clear()
                self.plugin.reflection_service.queues.pop(user_id, None)
                self.plugin._persona_by_user.pop(user_id, None)
            self.plugin.storage.set_user_enabled(user_id, enabled)
        return {
            "ok": True,
            "user_id": user_id,
            "enabled": enabled,
            "active_injection": bool(self.plugin.plugin_config.active and enabled),
        }

    async def reset_user(self, user_id: str) -> dict[str, int]:
        """重置指定 UMO 的消息、档案、队列与绑定，返回删除统计。"""
        async with self.plugin._analysis_lock(user_id):
            queue = self.plugin.reflection_service.queues.get(user_id)
            if queue is not None:
                queue.clear()
            self.plugin.reflection_service.queues.pop(user_id, None)
            self.plugin._persona_by_user.pop(user_id, None)
            async with self.plugin._response_lock(user_id):
                before_messages = len(self.plugin.storage.get_recent_messages(user_id, limit=10000))
                before_rounds = self.plugin.storage.get_max_completed_round(user_id)
                self.plugin.storage.reset_user(user_id)
                self.plugin._save_state(RelationshipState(user_id=user_id))
                return {
                    "messages_deleted": before_messages,
                    "rounds_deleted": before_rounds,
                }

    async def reset_response(self, user_id: str):
        """执行重置并返回重置后的状态摘要。"""
        deleted = await self.reset_user(user_id)
        state = await self.plugin._load_state(user_id)
        remaining_messages = len(self.plugin.storage.get_recent_messages(user_id, limit=10000))
        return web_mod.json_response(
            {
                "ok": True,
                "user_id": user_id,
                **deleted,
                "remaining_messages": remaining_messages,
                "posture": state.posture,
                "round_sequence": state.round_sequence,
            }
        )

    async def rebuild_profile(self, user_id: str) -> tuple[bool, str, RelationshipState | None]:
        """observe 模式下逐轮重放分析并原子替换档案；失败保留原档案。"""
        if not self.plugin.storage.is_user_enabled(user_id):
            return False, "umo_disabled", None
        if self.plugin.plugin_config.operation_mode != "observe":
            return False, "observe_mode_required", None
        async with self.plugin._analysis_lock(user_id):
            current_state = await self.plugin._load_state(user_id)
            persona_resolution = await self.plugin.persona.resolve_persona_id(user_id)
            relationship_role = self.plugin.persona.relationship_role(current_state, persona_resolution)
            if relationship_role == "bonded":
                return False, "bonded_rebuild_forbidden", None
            _, revision = self.plugin.storage.get_state_record(user_id)
            if revision is None:
                return False, "state_revision_missing", None
            message_revision = self.plugin.storage.get_message_revision(user_id)
            max_round = self.plugin.storage.get_max_completed_round(user_id)
            if max_round < 2:
                return False, "not_enough_completed_rounds", None

            rebuilt = RelationshipState(
                user_id=user_id,
                round_sequence=0,
                former_bond=current_state.former_bond,
            )
            steps: list[dict[str, Any]] = []
            last_model_trace: dict[str, Any] = {}
            for target_round in range(2, max_round + 1, 2):
                kind = analysis_kind_for_round(target_round)
                if kind == "deep":
                    messages = self.plugin.storage.get_recent_messages(
                        user_id,
                        limit=20,
                        up_to_round=target_round,
                        completed_only=True,
                    )
                    rebuilt.round_sequence = target_round
                    outcome = await self.plugin.reflection.analyze_deep(
                        rebuilt,
                        messages,
                        persona_prompt=self.plugin._persona_by_user.get(user_id, ""),
                        relationship_role=relationship_role,
                    )
                else:
                    messages = self.plugin.storage.get_completed_rounds(user_id, 2, up_to_round=target_round)
                    rebuilt.round_sequence = target_round
                    outcome = await self.plugin.reflection.analyze_light(
                        rebuilt,
                        messages,
                        target_round,
                        relationship_role=relationship_role,
                    )
                if outcome.value is None:
                    return (
                        False,
                        f"{kind}_analysis_invalid_at_round_{target_round}",
                        None,
                    )
                if kind == "deep":
                    decision = apply_deep_evidence(
                        rebuilt,
                        outcome.value,
                        target_round,
                        is_bonded=relationship_role == "bonded",
                    )
                    signal = outcome.value.pattern
                    confidence = outcome.value.confidence
                else:
                    decision = apply_light_evidence(
                        rebuilt,
                        outcome.value,
                        target_round,
                        is_bonded=relationship_role == "bonded",
                    )
                    signal = outcome.value.signal
                    confidence = outcome.value.confidence
                last_model_trace = dict(outcome.trace)
                steps.append(
                    {
                        "round": target_round,
                        "kind": kind,
                        "prompt_version": outcome.trace.get("prompt_version", ""),
                        "prompt_chars": outcome.trace.get("prompt_chars"),
                        "usage": outcome.trace.get("usage", {}),
                        "model_tags": outcome.trace.get("model_tags", {}),
                        "code_decision": decision,
                    }
                )

            rebuilt.round_sequence = max_round
            rebuilt.last_analysis_kind = "rebuild"
            rebuilt.last_analysis_round = max_round
            rebuilt.last_analysis_status = "applied"
            rebuilt.last_analysis_signal = signal
            rebuilt.last_analysis_confidence = confidence
            rebuilt.last_analysis_note = "保留消息的档案重建已由模型观察与代码裁决完成"
            rebuilt.last_analysis_at = time.time()
            rebuilt.last_analysis_trace = {
                **last_model_trace,
                "kind": "rebuild",
                "operation_version": "rebuild-v1",
                "steps": steps[-6:],
                "code_decision": {
                    "message_count": message_revision[0],
                    "round_sequence": max_round,
                    "atomic_replace": True,
                },
            }

            async with self.plugin._response_lock(user_id):
                if self.plugin.storage.get_message_revision(user_id) != message_revision:
                    return False, "new_messages_arrived", None
                _, current_revision = self.plugin.storage.get_state_record(user_id)
                if current_revision != revision:
                    return False, "state_changed_during_rebuild", None
                if not self.plugin.storage.replace_state_if_revision(user_id, revision, rebuilt.to_dict()):
                    return False, "atomic_replace_conflict", None
            return True, "", rebuilt

    def state_payload(
        self,
        state: RelationshipState,
        resolution: PersonaResolution | None = None,
    ) -> dict[str, Any]:
        """组装前端档案载荷：状态快照 + 身份/绑定/阶段/投影预览。"""
        persona = resolution or PersonaResolution(error="persona_not_resolved")
        bond_debug = self.plugin.persona.bond_debug_payload(state, persona)
        payload = state.to_dict()
        payload.update(self.identity_parts(state.user_id))
        payload.update(bond_debug)
        enabled = self.plugin.storage.is_user_enabled(state.user_id)
        payload["enabled"] = enabled
        payload["active_injection"] = bool(self.plugin.plugin_config.active and enabled)
        payload["relationship_stage"] = state.relationship_stage
        payload["effective_relationship_stage"] = (
            "familiar"
            if bond_debug["relationship_role"] == "former" and state.relationship_stage in {"long_familiar", "close"}
            else state.relationship_stage
        )
        payload["relationship_semantics"] = state.relationship_semantics
        payload["companion_protocol_version"] = COMPANION_PROTOCOL_VERSION
        payload["next_compiled_preview"] = (
            self.plugin.context_builder.build(
                state,
                max_chars=self.plugin.plugin_config.max_context_chars,
                next_round=state.round_sequence + 1,
                relationship_role=str(bond_debug["relationship_role"] or "unbound"),
            )
            if enabled
            else ""
        )
        return payload

    @staticmethod
    def identity_parts(user_id: str) -> dict[str, str]:
        """把 UMO 拆分为平台、会话类型与目标 ID 等展示字段。"""
        parts = user_id.split(":", 2)
        if len(parts) < 3:
            return {
                "umo": user_id,
                "platform": "",
                "session_type": "",
                "session_target": user_id,
                "session_id": user_id,
                "sender_id": user_id,
            }
        platform, session_type, session_target = parts
        return {
            "umo": user_id,
            "platform": platform,
            "session_type": session_type,
            "session_target": session_target,
            "session_id": user_id,
            "sender_id": session_target,
        }
