from __future__ import annotations

import asyncio
import hashlib
import re
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import unquote

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.event.filter import PermissionType
from astrbot.api.platform import MessageType
from astrbot.api.star import Context, Star

try:
    from astrbot.core import sp
except ImportError:
    sp = None

try:
    from astrbot.api.web import json_response, request
except ImportError:

    def json_response(payload: dict[str, Any]) -> dict[str, Any]:
        return payload

    request = None

try:
    from astrbot.core.agent.message import TextPart
except ImportError:
    TextPart = None

try:
    from astrbot.core.utils.astrbot_path import get_astrbot_data_path
except ImportError:

    def get_astrbot_data_path() -> str:
        return str(Path(".").resolve())


from . import __version__
from .config import V2Config, load_config
from .core import (
    RelationshipState,
    Storage,
    analysis_kind_for_round,
    apply_deep_evidence,
    apply_light_evidence,
    apply_severe_evidence,
    fallback_impression,
)
from .llm import (
    COMPANION_PROTOCOL_VERSION,
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


@dataclass(frozen=True)
class PersonaResolution:
    persona_id: str = ""
    source: str = "none"
    error: str = ""


class CompanionLiteV2Plugin(Star):
    def __init__(self, context: Context, config: dict[str, Any] | None = None) -> None:
        super().__init__(context)
        self.context = context
        self.plugin_config: V2Config = load_config(config)
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
        self._reflection_tasks: dict[str, asyncio.Task] = {}
        self._reflection_queues: dict[str, list[tuple[int, str]]] = {}
        self._background_tasks: set[asyncio.Task] = set()
        self._persona_by_user: dict[str, str] = {}
        self._bond_lock = asyncio.Lock()
        self._register_page_api()

    def _register_page_api(self) -> None:
        if not hasattr(self.context, "register_web_api"):
            return
        prefix = "/astrbot_plugin_companion_lite_v2/page"
        self.context.register_web_api(
            f"{prefix}/state",
            self._api_state,
            ["GET"],
            "Get V2 relationship state",
        )
        self.context.register_web_api(
            f"{prefix}/sessions",
            self._api_sessions,
            ["GET"],
            "List V2 private relationship sessions",
        )
        self.context.register_web_api(
            f"{prefix}/messages",
            self._api_messages,
            ["GET"],
            "Get V2 message buffer",
        )
        self.context.register_web_api(
            f"{prefix}/health",
            self._api_health,
            ["GET"],
            "Get V2 plugin health",
        )
        self.context.register_web_api(
            f"{prefix}/reset",
            self._api_reset,
            ["POST"],
            "Reset V2 relationship state",
        )
        self.context.register_web_api(
            f"{prefix}/reset/<path:user_id>",
            self._api_reset_path,
            ["POST"],
            "Reset V2 relationship state by UMO",
        )
        self.context.register_web_api(
            f"{prefix}/enabled",
            self._api_enabled,
            ["POST"],
            "Enable or disable V2 processing for an existing UMO",
        )
        self.context.register_web_api(
            f"{prefix}/reflect",
            self._api_reflect,
            ["POST"],
            "Run V2 deep reflection",
        )
        self.context.register_web_api(
            f"{prefix}/rebuild",
            self._api_rebuild,
            ["POST"],
            "Rebuild V2 relationship state while preserving messages",
        )

    async def initialize(self) -> None:
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
                recovered += int(self._enqueue_reflection(state.user_id, deep_target, "deep"))
                continue

            latest_even = state.round_sequence - (state.round_sequence % 2)
            if (
                analysis_kind_for_round(latest_even) == "light"
                and latest_even > state.last_deep_round
                and (latest_even > state.last_analysis_round or state.last_analysis_status == "interrupted")
                and state.round_sequence <= latest_even + 2
            ):
                recovered += int(self._enqueue_reflection(state.user_id, latest_even, "light"))
        logger.info(
            "[CLV2] 初始化完成: mode=%s, 使用独立V2数据库, 补调度=%s",
            self.plugin_config.operation_mode,
            recovered,
        )

    async def terminate(self) -> None:
        """停止后台任务并关闭 V2 独立数据库连接。"""
        self._initialized = False
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

    async def _resolve_persona_id(self, user_id: str) -> PersonaResolution:
        umo = str(user_id or "").strip()
        if not umo:
            return PersonaResolution(error="umo_missing")
        if sp is None:
            return PersonaResolution(error="service_provider_unavailable")
        try:
            session_config = (
                await sp.get_async(
                    scope="umo",
                    scope_id=umo,
                    key="session_service_config",
                    default={},
                )
                or {}
            )
            if not isinstance(session_config, dict):
                return PersonaResolution(
                    source="session_override",
                    error="session_config_invalid",
                )
            session_persona = str(session_config.get("persona_id") or "").strip()
            if session_persona:
                return await self._validated_persona(session_persona, "session_override")

            conversation_manager = getattr(self.context, "conversation_manager", None)
            if conversation_manager is not None:
                conversation_id = await conversation_manager.get_curr_conversation_id(umo)
                if conversation_id is not None:
                    conversation = await conversation_manager.get_conversation(umo, conversation_id)
                    conversation_persona = str(getattr(conversation, "persona_id", "") or "").strip()
                    if conversation_persona:
                        return await self._validated_persona(conversation_persona, "conversation")

            persona_manager = getattr(self.context, "persona_manager", None)
            if persona_manager is None:
                return PersonaResolution(error="persona_manager_unavailable")
            default_persona = await persona_manager.get_default_persona_v3(umo=umo)
            if not default_persona:
                return PersonaResolution(source="default", error="persona_missing")
            if isinstance(default_persona, dict):
                persona_id = str(default_persona.get("name") or default_persona.get("persona_id") or "").strip()
            else:
                persona_id = str(getattr(default_persona, "name", "") or getattr(default_persona, "persona_id", "") or "").strip()
            if not persona_id or persona_id == "[%None]":
                return PersonaResolution(source="default", error="persona_missing")
            return PersonaResolution(persona_id, "default")
        except Exception as exc:
            logger.debug("[CLV2] 人格解析失败 user=%s error=%s", umo, exc)
            return PersonaResolution(error="resolution_failed")

    async def _validated_persona(self, persona_id: str, source: str) -> PersonaResolution:
        candidate = str(persona_id or "").strip()
        if not candidate or candidate == "[%None]":
            return PersonaResolution(source=source, error="persona_missing")
        persona_manager = getattr(self.context, "persona_manager", None)
        if persona_manager is None:
            return PersonaResolution(source=source, error="persona_manager_unavailable")
        resolver = getattr(persona_manager, "get_persona_v3_by_id", None)
        if callable(resolver):
            try:
                if resolver(candidate):
                    return PersonaResolution(candidate, source)
                return PersonaResolution(source=source, error="persona_not_found")
            except Exception:
                return PersonaResolution(source=source, error="persona_lookup_failed")
        getter = getattr(persona_manager, "get_persona", None)
        if callable(getter):
            try:
                if await getter(candidate):
                    return PersonaResolution(candidate, source)
            except Exception:
                pass
        return PersonaResolution(source=source, error="persona_not_found")

    def _relationship_role(
        self,
        state: RelationshipState,
        resolution: PersonaResolution,
    ) -> str:
        if not resolution.persona_id:
            return "unbound"
        bond = self.storage.get_bond(resolution.persona_id)
        if bond and str(bond.get("user_id") or "") == state.user_id:
            return "bonded"
        if state.former_bond:
            return "former"
        if bond:
            return "other"
        return "unbound"

    def _bond_debug_payload(
        self,
        state: RelationshipState,
        resolution: PersonaResolution,
    ) -> dict[str, Any]:
        bond = self.storage.get_bond(resolution.persona_id) if resolution.persona_id else None
        role = self._relationship_role(state, resolution)
        status = {
            "bonded": "bound_current",
            "other": "occupied_elsewhere",
            "former": "former",
            "unbound": ("unresolved" if not resolution.persona_id else "unbound"),
        }[role]
        return {
            "persona_id": resolution.persona_id,
            "persona_source": resolution.source,
            "persona_error": resolution.error,
            "bond_status": status,
            "relationship_role": role,
            "bond_user_id": str(bond.get("user_id") or "") if bond else "",
            "bond_bound_at": float(bond.get("bound_at") or 0) if bond else 0,
        }

    def _remember_persona(self, user_id: str, req: Any) -> None:
        if self.plugin_config.persona_prompt:
            self._persona_by_user[user_id] = self.plugin_config.persona_prompt
            return
        prompt = " ".join(str(getattr(req, "system_prompt", "") or "").split())
        if prompt:
            self._persona_by_user[user_id] = prompt[:2000]

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
        persona_resolution = await self._resolve_persona_id(user_id)

        await self._run_severe_precheck(user_id, text)

        async with self._response_lock(user_id):
            if not self.storage.is_user_enabled(user_id):
                return
            self._remember_persona(user_id, req)
            state = await self._load_state(user_id)
            relationship_role = self._relationship_role(state, persona_resolution)
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

    @filter.on_llm_response()
    async def capture_llm_response(self, event: AstrMessageEvent, resp=None) -> None:
        if not self._initialized or resp is None or not self._is_private(event) or not self.plugin_config.enable_message_capture:
            return
        if (
            getattr(resp, "role", "assistant") != "assistant"
            or getattr(resp, "tools_call_name", None)
            or getattr(resp, "tools_call_extra_content", None)
        ):
            return
        assistant_text = str(getattr(resp, "completion_text", "") or "").strip()
        if not assistant_text:
            return
        assistant_text = self._truncate_captured_text(assistant_text)
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
            self._save_state(state)
            self.storage.trim_completed_rounds(user_id, self.plugin_config.max_buffer_rounds)

        deep_target = (next_round // 6) * 6
        if deep_target > state.last_deep_round:
            self._enqueue_reflection(user_id, deep_target, "deep")
        elif analysis_kind_for_round(next_round) == "light":
            self._enqueue_reflection(user_id, next_round, "light")

    def _enqueue_reflection(self, user_id: str, target_round: int, kind: str) -> bool:
        if target_round <= 0 or kind not in {"light", "deep"} or not self.storage.is_user_enabled(user_id):
            return False
        queue = self._reflection_queues.setdefault(user_id, [])
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
        task = self._reflection_tasks.get(user_id)
        if task and not task.done():
            return True
        task = asyncio.create_task(self._reflection_worker(user_id))
        self._reflection_tasks[user_id] = task
        self._background_tasks.add(task)
        task.add_done_callback(lambda done: self._reflection_done(user_id, done))
        return True

    def _reflection_done(self, user_id: str, task: asyncio.Task) -> None:
        self._background_tasks.discard(task)
        if self._reflection_tasks.get(user_id) is task:
            self._reflection_tasks.pop(user_id, None)
        if not task.cancelled() and task.exception():
            logger.warning(
                "[CLV2] 反思任务异常 user=%s: %s",
                user_id,
                task.exception(),
            )

    async def _reflection_worker(self, user_id: str) -> None:
        queue = self._reflection_queues.setdefault(user_id, [])
        while queue and self._initialized:
            if not self.storage.is_user_enabled(user_id):
                queue.clear()
                break
            target_round, kind = queue.pop(0)
            await self._perform_reflection(user_id, target_round, kind)

    async def _perform_reflection(self, user_id: str, target_round: int, kind: str) -> bool:
        async with self._analysis_lock(user_id):
            return await self._perform_reflection_locked(user_id, target_round, kind)

    async def _perform_reflection_locked(self, user_id: str, target_round: int, kind: str) -> bool:
        if not self.storage.is_user_enabled(user_id):
            return False
        if kind == "deep":
            messages = self.storage.get_recent_messages(
                user_id,
                limit=20,
                up_to_round=target_round,
                completed_only=True,
            )
        else:
            messages = self.storage.get_completed_rounds(user_id, 2, up_to_round=target_round)
        if len(messages) < 2:
            return False
        state = await self._load_state(user_id)
        if kind == "deep" and state.last_deep_round >= target_round:
            return True
        if kind == "light" and target_round <= state.last_deep_round:
            return True
        if (
            kind == "light"
            and state.last_analysis_kind == "light"
            and state.last_analysis_round >= target_round
            and state.last_analysis_status in {"signal", "none"}
        ):
            return True
        persona_resolution = await self._resolve_persona_id(user_id)
        relationship_role = self._relationship_role(state, persona_resolution)

        async with self._response_lock(user_id):
            latest = await self._load_state(user_id)
            latest.last_analysis_kind = kind
            latest.last_analysis_round = target_round
            latest.last_analysis_status = "running"
            latest.last_analysis_signal = ""
            latest.last_analysis_confidence = ""
            latest.last_analysis_note = "正在检查最近两个完整来回" if kind == "light" else "正在综合近期关系状态"
            latest.last_analysis_at = time.time()
            self._save_state(latest)
            state = latest

        if kind == "light":
            outcome = await self.reflection.analyze_light(
                state,
                messages,
                target_round,
                relationship_role=relationship_role,
            )
        else:
            outcome = await self.reflection.analyze_deep(
                state,
                messages,
                persona_prompt=self._persona_by_user.get(user_id, ""),
                relationship_role=relationship_role,
            )
        if outcome.value is None:
            async with self._response_lock(user_id):
                latest = await self._load_state(user_id)
                if latest.last_analysis_kind == kind and latest.last_analysis_round == target_round:
                    latest.last_analysis_status = "invalid"
                    latest.last_analysis_note = "模型返回为空、调用失败或输出格式不符合约定"
                    latest.last_analysis_trace = outcome.trace
                    latest.last_analysis_at = time.time()
                    self._save_state(latest)
            logger.warning(
                "[CLV2] %s分析无有效结果 user=%s round=%s",
                "轻" if kind == "light" else "深",
                user_id,
                target_round,
            )
            return False

        async with self._response_lock(user_id):
            latest = await self._load_state(user_id)
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
            self._save_state(latest)
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

    @filter.command("clv2_status")
    @filter.permission_type(PermissionType.ADMIN)
    @filter.event_message_type(filter.EventMessageType.PRIVATE_MESSAGE)
    async def cmd_status(self, event: AstrMessageEvent):
        """查看当前私聊 UMO 的关系状态与最近语义投影。"""
        user_id = self._user_identity(event)
        state = await self._load_state(user_id)
        persona_resolution = await self._resolve_persona_id(user_id)
        bond_debug = self._bond_debug_payload(state, persona_resolution)
        issue = state.active_issue
        issue_text = f"{issue.kind}/{issue.phase}: {issue.summary or '-'}" if issue else "-"
        light = state.light_guidance
        light_text = f"{light.signal}/{light.reminder}，有效至第{light.expires_after_round}轮" if light else "-"
        yield event.plain_result(
            "CompanionLiteV2 状态\n"
            f"模式: {self.plugin_config.operation_mode}\n"
            f"用户: {user_id}\n"
            f"人格: {bond_debug['persona_id'] or '-'} "
            f"({bond_debug['persona_source'] or bond_debug['persona_error'] or '-'})\n"
            f"正式关系: {bond_debug['bond_status']}\n"
            f"轮次: {state.round_sequence}，最近深分析: {state.last_deep_round}\n"
            f"关系阶段: {state.relationship_stage}\n"
            f"三维: 熟悉度 {state.familiarity:.1f} / "
            f"信任 {state.trust:.1f} / 亲和 {state.affinity:.1f}\n"
            f"关系总结: {state.relationship_summary or '-'}\n"
            f"关系姿态: {state.posture}\n"
            f"当前问题: {issue_text}\n"
            f"轻提醒: {light_text}\n"
            f"主观印象: {state.impression or '-'}\n"
            f"最近上下文实际注入: {'是' if state.last_context_injected else '否'}\n"
            f"最近编译文本:\n{state.last_compiled_context or '-'}"
        )

    @filter.command("companion_bond")
    @filter.permission_type(PermissionType.ADMIN)
    @filter.event_message_type(filter.EventMessageType.PRIVATE_MESSAGE)
    async def cmd_bond(self, event: AstrMessageEvent):
        """将当前私聊窗口设为当前人格唯一的正式关系。"""
        user_id = self._user_identity(event)
        resolution = await self._resolve_persona_id(user_id)
        if not resolution.persona_id:
            yield event.plain_result("这轮没认出我正在使用哪套人格，先别乱绑。")
            return
        async with self._bond_lock:
            result = self.storage.bind_persona(resolution.persona_id, user_id)
            status = str(result.get("status") or "")
            if status == "occupied":
                yield event.plain_result("这个位置已经有人了。要换，先在原来的窗口解除。")
                return
            if status == "already_bound":
                yield event.plain_result("这个位置本来就是你的，还确认什么。")
                return
            if status != "bound":
                yield event.plain_result("这次绑定没有落稳，先别把关系说死。")
                return
            async with self._response_lock(user_id):
                state = await self._load_state(user_id)
                was_former = state.former_bond
                state.former_bond = False
                if not was_former:
                    state.familiarity = max(state.familiarity, 35.0)
                    state.trust = max(state.trust, 60.0)
                    state.affinity = max(state.affinity, 15.0)
                if not state.impression:
                    state.impression = fallback_impression(state)
                self._save_state(state)
        yield event.plain_result("行，这个位置给你了。只有一个——以后怎么待我，自己掂量。")

    @filter.command("companion_unbond")
    @filter.permission_type(PermissionType.ADMIN)
    @filter.event_message_type(filter.EventMessageType.PRIVATE_MESSAGE)
    async def cmd_unbond(self, event: AstrMessageEvent):
        """解除当前窗口的正式关系，但保留既有相处状态。"""
        user_id = self._user_identity(event)
        resolution = await self._resolve_persona_id(user_id)
        if not resolution.persona_id:
            yield event.plain_result("这个窗口没有可解除的关系。")
            return
        async with self._bond_lock:
            if not self.storage.unbind_persona(resolution.persona_id, user_id):
                yield event.plain_result("这个窗口没有可解除的关系。")
                return
            async with self._response_lock(user_id):
                state = await self._load_state(user_id)
                state.former_bond = True
                self._save_state(state)
        yield event.plain_result("关系名我收回了，发生过的事不清零。以后还是看你怎么待我。")

    @filter.command("clv2_reset")
    @filter.permission_type(PermissionType.ADMIN)
    @filter.event_message_type(filter.EventMessageType.PRIVATE_MESSAGE)
    async def cmd_reset(self, event: AstrMessageEvent):
        """重置当前私聊 UMO 的全部 CompanionLiteV2 数据。"""
        user_id = self._user_identity(event)
        await self._reset_user(user_id)
        yield event.plain_result(f"已重置 CompanionLiteV2 独立状态: {user_id}")

    @filter.command("clv2_reflect")
    @filter.permission_type(PermissionType.ADMIN)
    @filter.event_message_type(filter.EventMessageType.PRIVATE_MESSAGE)
    async def cmd_reflect(self, event: AstrMessageEvent):
        """立即尝试运行当前私聊 UMO 的深度关系分析。"""
        user_id = self._user_identity(event)
        if not self.storage.is_user_enabled(user_id):
            yield event.plain_result("CompanionLiteV2 此 UMO 已关闭，未调用分析模型")
            return
        state = await self._load_state(user_id)
        ok = await self._perform_reflection(user_id, state.round_sequence, "deep")
        yield event.plain_result(
            "CompanionLiteV2 深分析已完成" if ok else "CompanionLiteV2 深分析未执行：没有完整来回或模型未返回有效结果"
        )

    async def _reset_user(self, user_id: str) -> dict[str, int]:
        async with self._analysis_lock(user_id):
            queue = self._reflection_queues.get(user_id)
            if queue is not None:
                queue.clear()
            self._reflection_queues.pop(user_id, None)
            self._persona_by_user.pop(user_id, None)
            async with self._response_lock(user_id):
                before_messages = len(self.storage.get_recent_messages(user_id, limit=10000))
                before_rounds = self.storage.get_max_completed_round(user_id)
                self.storage.reset_user(user_id)
                self._save_state(RelationshipState(user_id=user_id))
                return {
                    "messages_deleted": before_messages,
                    "rounds_deleted": before_rounds,
                }

    async def _resolve_user_id(self) -> str:
        if request is None:
            return ""
        user_id = str(request.query.get("user_id", "") or "").strip()
        if user_id:
            return user_id
        try:
            body = await request.json({})
        except Exception:
            body = None
        if not isinstance(body, dict):
            return ""
        return str(body.get("user_id", "") or "").strip()

    async def _set_user_enabled(self, user_id: str, enabled: bool) -> dict[str, Any]:
        if not self.storage.has_state(user_id):
            return {"ok": False, "error": "unknown_umo", "user_id": user_id}
        async with self._analysis_lock(user_id), self._response_lock(user_id):
            if not enabled:
                queue = self._reflection_queues.get(user_id)
                if queue is not None:
                    queue.clear()
                self._reflection_queues.pop(user_id, None)
                self._persona_by_user.pop(user_id, None)
            self.storage.set_user_enabled(user_id, enabled)
        return {
            "ok": True,
            "user_id": user_id,
            "enabled": enabled,
            "active_injection": bool(self.plugin_config.active and enabled),
        }

    async def _api_state(self):
        user_id = await self._resolve_user_id()
        if not user_id:
            return json_response({"error": "user_id_required"})
        state = await self._load_state(user_id)
        resolution = await self._resolve_persona_id(user_id)
        return json_response(self._state_payload(state, resolution))

    async def _api_sessions(self):
        sessions: list[dict[str, Any]] = []
        for item in self.storage.list_states(limit=1000):
            state = RelationshipState.from_dict(
                item.get("state"),
                user_id=str(item.get("user_id") or ""),
            )
            sessions.append(
                {
                    "user_id": state.user_id,
                    **self._identity_parts(state.user_id),
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
        return json_response({"sessions": sessions, "count": len(sessions)})

    async def _api_messages(self):
        user_id = await self._resolve_user_id()
        if not user_id:
            return json_response({"error": "user_id_required"})
        limit = request.query.get("limit", 40, type=int) if request is not None else 40
        messages = self.storage.get_recent_messages(user_id, limit=limit)
        return json_response({"messages": messages, "count": len(messages)})

    async def _api_health(self):
        return json_response(
            {
                "initialized": self._initialized,
                "plugin_id": "astrbot_plugin_companion_lite_v2",
                "version": __version__,
                "operation_mode": self.plugin_config.operation_mode,
                "active_injection": self.plugin_config.active,
                "background_tasks": len(self._background_tasks),
            }
        )

    async def _api_reset(self):
        user_id = await self._resolve_user_id()
        if not user_id:
            return json_response({"error": "user_id_required"})
        if not self.storage.has_state(user_id):
            return json_response({"error": "unknown_umo"})
        return await self._reset_response(user_id)

    async def _api_reset_path(self, user_id: str):
        user_id = str(user_id or "").strip()
        if not user_id:
            return json_response({"error": "user_id_required"})
        for _ in range(3):
            if self.storage.has_state(user_id):
                return await self._reset_response(user_id)
            decoded = unquote(user_id)
            if decoded == user_id:
                break
            user_id = decoded
        return json_response({"error": "unknown_umo"})

    async def _api_enabled(self):
        if request is None:
            return json_response({"error": "request_unavailable"})
        try:
            body = await request.json({})
        except Exception:
            body = None
        if not isinstance(body, dict):
            return json_response({"error": "invalid_request_body"})
        user_id = str(body.get("user_id", "") or "").strip()
        enabled_value = body.get("enabled")
        if not user_id:
            return json_response({"error": "user_id_required"})
        if not isinstance(enabled_value, bool):
            return json_response({"error": "invalid_enabled_value"})
        result = await self._set_user_enabled(user_id, enabled_value)
        return json_response(result)

    async def _reset_response(self, user_id: str):
        deleted = await self._reset_user(user_id)
        state = await self._load_state(user_id)
        remaining_messages = len(self.storage.get_recent_messages(user_id, limit=10000))
        return json_response(
            {
                "ok": True,
                "user_id": user_id,
                **deleted,
                "remaining_messages": remaining_messages,
                "posture": state.posture,
                "round_sequence": state.round_sequence,
            }
        )

    async def _api_reflect(self):
        user_id = await self._resolve_user_id()
        if not user_id:
            return json_response({"error": "user_id_required"})
        if not self.storage.is_user_enabled(user_id):
            return json_response({"ok": False, "user_id": user_id, "error": "umo_disabled"})
        state = await self._load_state(user_id)
        ok = await self._perform_reflection(user_id, state.round_sequence, "deep")
        return json_response({"ok": ok, "user_id": user_id})

    async def _rebuild_profile(self, user_id: str) -> tuple[bool, str, RelationshipState | None]:
        if not self.storage.is_user_enabled(user_id):
            return False, "umo_disabled", None
        if self.plugin_config.operation_mode != "observe":
            return False, "observe_mode_required", None
        async with self._analysis_lock(user_id):
            current_state = await self._load_state(user_id)
            persona_resolution = await self._resolve_persona_id(user_id)
            relationship_role = self._relationship_role(current_state, persona_resolution)
            if relationship_role == "bonded":
                return False, "bonded_rebuild_forbidden", None
            _, revision = self.storage.get_state_record(user_id)
            if revision is None:
                return False, "state_revision_missing", None
            message_revision = self.storage.get_message_revision(user_id)
            max_round = self.storage.get_max_completed_round(user_id)
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
                    messages = self.storage.get_recent_messages(
                        user_id,
                        limit=20,
                        up_to_round=target_round,
                        completed_only=True,
                    )
                    rebuilt.round_sequence = target_round
                    outcome = await self.reflection.analyze_deep(
                        rebuilt,
                        messages,
                        persona_prompt=self._persona_by_user.get(user_id, ""),
                        relationship_role=relationship_role,
                    )
                else:
                    messages = self.storage.get_completed_rounds(user_id, 2, up_to_round=target_round)
                    rebuilt.round_sequence = target_round
                    outcome = await self.reflection.analyze_light(
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

            async with self._response_lock(user_id):
                if self.storage.get_message_revision(user_id) != message_revision:
                    return False, "new_messages_arrived", None
                _, current_revision = self.storage.get_state_record(user_id)
                if current_revision != revision:
                    return False, "state_changed_during_rebuild", None
                if not self.storage.replace_state_if_revision(user_id, revision, rebuilt.to_dict()):
                    return False, "atomic_replace_conflict", None
            return True, "", rebuilt

    async def _api_rebuild(self):
        user_id = await self._resolve_user_id()
        if not user_id:
            return json_response({"error": "user_id_required"})
        ok, error, state = await self._rebuild_profile(user_id)
        payload: dict[str, Any] = {
            "ok": ok,
            "user_id": user_id,
            "error": error,
        }
        if state is not None:
            resolution = await self._resolve_persona_id(user_id)
            payload["state"] = self._state_payload(state, resolution)
        return json_response(payload)

    def _state_payload(
        self,
        state: RelationshipState,
        resolution: PersonaResolution | None = None,
    ) -> dict[str, Any]:
        persona = resolution or PersonaResolution(error="persona_not_resolved")
        bond_debug = self._bond_debug_payload(state, persona)
        payload = state.to_dict()
        payload.update(CompanionLiteV2Plugin._identity_parts(state.user_id))
        payload.update(bond_debug)
        enabled = self.storage.is_user_enabled(state.user_id)
        payload["enabled"] = enabled
        payload["active_injection"] = bool(self.plugin_config.active and enabled)
        payload["relationship_stage"] = state.relationship_stage
        payload["effective_relationship_stage"] = (
            "familiar"
            if bond_debug["relationship_role"] == "former" and state.relationship_stage in {"long_familiar", "close"}
            else state.relationship_stage
        )
        payload["relationship_semantics"] = state.relationship_semantics
        payload["companion_protocol_version"] = COMPANION_PROTOCOL_VERSION
        payload["next_compiled_preview"] = (
            self.context_builder.build(
                state,
                max_chars=self.plugin_config.max_context_chars,
                next_round=state.round_sequence + 1,
                relationship_role=str(bond_debug["relationship_role"] or "unbound"),
            )
            if enabled
            else ""
        )
        return payload

    @staticmethod
    def _identity_parts(user_id: str) -> dict[str, str]:
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
        except Exception:
            return False
