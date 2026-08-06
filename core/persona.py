from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from astrbot.api import logger

try:
    from astrbot.core import sp
except ImportError:
    sp = None


@dataclass(frozen=True)
class PersonaResolution:
    """人格解析结果：命中的人格 ID、来源与失败原因。"""

    persona_id: str = ""
    source: str = "none"
    error: str = ""


class PersonaService:
    """人格解析与正式关系判定：跟随 AstrBot 会话/默认人格，维护绑定状态。"""

    def __init__(self, plugin) -> None:
        """持有插件引用，按需访问 context、storage 与运行时配置。"""
        self.plugin = plugin

    async def resolve_persona_id(self, user_id: str) -> PersonaResolution:
        """按会话覆盖、会话人格、默认人格的优先级解析当前人格。"""
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
                return await self.validated_persona(session_persona, "session_override")

            conversation_manager = getattr(self.plugin.context, "conversation_manager", None)
            if conversation_manager is not None:
                conversation_id = await conversation_manager.get_curr_conversation_id(umo)
                if conversation_id is not None:
                    conversation = await conversation_manager.get_conversation(umo, conversation_id)
                    conversation_persona = str(getattr(conversation, "persona_id", "") or "").strip()
                    if conversation_persona:
                        return await self.validated_persona(conversation_persona, "conversation")

            persona_manager = getattr(self.plugin.context, "persona_manager", None)
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

    async def validated_persona(self, persona_id: str, source: str) -> PersonaResolution:
        """校验候选人格 ID 是否真实存在，并记录其来源。"""
        candidate = str(persona_id or "").strip()
        if not candidate or candidate == "[%None]":
            return PersonaResolution(source=source, error="persona_missing")
        persona_manager = getattr(self.plugin.context, "persona_manager", None)
        if persona_manager is None:
            return PersonaResolution(source=source, error="persona_manager_unavailable")
        resolver = getattr(persona_manager, "get_persona_v3_by_id", None)
        if callable(resolver):
            try:
                if resolver(candidate):
                    return PersonaResolution(candidate, source)
                return PersonaResolution(source=source, error="persona_not_found")
            except Exception as exc:
                logger.debug("[CLV2] 人格主查询失败 user=%s error=%s", candidate, exc)
                return PersonaResolution(source=source, error="persona_lookup_failed")
        getter = getattr(persona_manager, "get_persona", None)
        if callable(getter):
            try:
                if await getter(candidate):
                    return PersonaResolution(candidate, source)
            except Exception as exc:
                logger.debug("[CLV2] 人格回退查询失败 user=%s error=%s", candidate, exc)
        return PersonaResolution(source=source, error="persona_not_found")

    def relationship_role(
        self,
        state,
        resolution: PersonaResolution,
    ) -> str:
        """按正式绑定与历史关系判定当前窗口的角色。"""
        if not resolution.persona_id:
            return "unbound"
        bond = self.plugin.storage.get_bond(resolution.persona_id)
        if bond and str(bond.get("user_id") or "") == state.user_id:
            return "bonded"
        if state.former_bond:
            return "former"
        if bond:
            return "other"
        return "unbound"

    def bond_debug_payload(
        self,
        state,
        resolution: PersonaResolution,
    ) -> dict[str, Any]:
        """生成关系档案调试所需的人格与绑定摘要。"""
        bond = self.plugin.storage.get_bond(resolution.persona_id) if resolution.persona_id else None
        role = self.relationship_role(state, resolution)
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

    def remember(self, user_id: str, req: Any) -> None:
        """记录本次请求使用的运行时人格，供深度分析复用。"""
        if self.plugin.plugin_config.persona_prompt:
            self.plugin._persona_by_user[user_id] = self.plugin.plugin_config.persona_prompt
            return
        prompt = " ".join(str(getattr(req, "system_prompt", "") or "").split())
        if prompt:
            self.plugin._persona_by_user[user_id] = prompt[:2000]
