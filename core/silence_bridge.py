from __future__ import annotations

import re
from typing import Any

from astrbot.api import logger

from ..config import DEFAULT_SILENCE_IGNORE_PROMPT, SILENCE_RECOVERY_NOTICE

POLITE_SILENCE_NAME = "astrbot_plugin_polite_silence"


def _clean_sender_id(value: Any) -> str:
    """清洗 sender_id 中的引号，避免破坏 <ignore> 标签属性；比较时保持同一口径。"""
    return str(value or "").strip().replace('"', "'")


class SilenceBridgeController:
    """polite_silence 桥接：概率接管、状态机触发、拒答事件解析与恢复告知。"""

    IGNORE_TAG_RE = re.compile(r"<ignore\b([^>]*)>(.*?)</ignore>|<ignore\b([^>]*?)/>", re.DOTALL)

    def __init__(self, plugin) -> None:
        """持有插件引用与接管状态；未安装 polite_silence 时整条链 no-op。"""
        self.plugin = plugin
        self.instance = None
        self.original_trigger: int | None = None
        self.managed = False

    def resolve_polite_silence(self) -> Any:
        """探测 polite_silence 实例：先查 star_context，再查 get_registered_star。"""
        star_context = getattr(self.plugin.context, "star_context", None)
        if isinstance(star_context, dict):
            instance = star_context.get(POLITE_SILENCE_NAME)
            if instance is not None:
                return instance
        getter = getattr(self.plugin.context, "get_registered_star", None)
        if not callable(getter):
            return None
        metadata = getter(POLITE_SILENCE_NAME)
        if not metadata or not getattr(metadata, "activated", True):
            return None
        instance = getattr(metadata, "star_cls", None)
        if instance is None:
            instance = getattr(metadata, "instance", None)
        return instance

    async def sync(self, force_restore: bool = False) -> None:
        """按配置接管/还原 polite_silence 的概率注入；未装或缺字段时静默降级。"""
        config = self.plugin.plugin_config
        want_managed = bool(config.bridge_polite_silence and config.active)
        if force_restore:
            want_managed = False
        if self.managed == want_managed:
            return
        instance = self.resolve_polite_silence()
        config_obj = getattr(instance, "config", None)
        if instance is None or config_obj is None or not hasattr(config_obj, "get"):
            return
        trigger_key = "trigger_percent"
        if trigger_key not in config_obj:
            return
        try:
            if want_managed:
                self.original_trigger = config_obj.get(trigger_key, 0)
                config_obj[trigger_key] = 0
                self.instance = instance
                self.managed = True
                logger.info("[CLV2] 已接管 polite_silence 拒答注入: trigger_percent=0")
            else:
                original = self.original_trigger
                if original is not None:
                    config_obj[trigger_key] = original
                self.instance = None
                self.original_trigger = None
                self.managed = False
                logger.info("[CLV2] 已还原 polite_silence trigger_percent=%s", original)
        except Exception:
            logger.debug("[CLV2] polite_silence 配置接管失败", exc_info=True)

    @staticmethod
    def should_inject_silence(state) -> bool:
        """状态机判定是否向主模型提示礼貌性沉默；修复期一律不提示。"""
        issue = state.active_issue
        if issue is not None and issue.phase == "repairing":
            return False
        if state.posture == "disengaged":
            return True
        if (
            issue is not None
            and issue.kind in {"boundary_violation", "degradation", "coercion"}
            and state.posture in {"guarded", "disengaged"}
        ):
            return True
        guidance = state.light_guidance
        return bool(guidance is not None and guidance.reminder == "hold_boundary")

    @staticmethod
    def silence_minutes(state) -> int:
        """按姿态给出建议沉默时长：disengaged 90 分钟，其余 30 分钟。"""
        return 90 if state.posture == "disengaged" else 30

    @staticmethod
    def silence_status_line(state) -> str:
        """拒答状态摘要行：累计次数 + 最近一次时长与轮次。"""
        text = f"拒答: {state.silence_count} 次"
        if state.last_silence_event:
            text += (
                f" · 最近一次沉默 {state.last_silence_event['duration_minutes']} 分钟"
                f"（第 {state.last_silence_event['source_round']} 轮）"
            )
        return text

    def append_prompt(self, event, req, state) -> None:
        """向 system_prompt 尾部追加拒答指令模板，前缀保持稳定，携带真实 sender_id 与建议时长。"""
        sender_id = _clean_sender_id(event.get_sender_id())
        if not sender_id:
            return
        minutes = self.silence_minutes(state)
        template = self.plugin.plugin_config.silence_ignore_prompt or DEFAULT_SILENCE_IGNORE_PROMPT
        notice = template.format(sender_id=sender_id, minutes=minutes)
        current = str(getattr(req, "system_prompt", "") or "")
        req.system_prompt = f"{current}\n\n{notice}" if current else notice

    def consume_recovery(self, state, event, req) -> bool:
        """拒答结束、对方回来时在 system_prompt 尾部一次性告知主模型沉默时长，并清除事件。"""
        event_info = state.last_silence_event
        if not event_info:
            return False
        sender_id = _clean_sender_id(event.get_sender_id())
        if not sender_id or _clean_sender_id(event_info.get("target_id")) != sender_id:
            return False
        minutes = int(event_info.get("duration_minutes") or 0)
        notice = SILENCE_RECOVERY_NOTICE.format(minutes=minutes)
        current = str(getattr(req, "system_prompt", "") or "")
        req.system_prompt = f"{current}\n\n{notice}" if current else notice
        state.last_silence_event = None
        return True

    @staticmethod
    def extract_ignore_event(text: str) -> dict[str, Any] | None:
        """从 LLM 响应中提取拒答标签的 target_id 与 duration；无有效标签返回 None。"""
        match = SilenceBridgeController.IGNORE_TAG_RE.search(str(text or ""))
        if not match:
            return None
        attr_str = (match.group(1) or match.group(3) or "").strip()
        inner = (match.group(2) or "").strip()
        attrs = {}
        for attr_match in re.finditer(r'([\w]+)=["\']([^"\']*)["\']', attr_str):
            attrs[attr_match.group(1).lower()] = attr_match.group(2).strip()
        target_id = (
            attrs.get("id") or attrs.get("user") or attrs.get("user_id") or attrs.get("target") or ""
        ).strip()
        duration = (attrs.get("duration") or attrs.get("time") or attrs.get("minutes") or "").strip()
        if not target_id and inner:
            target_id = inner
        if not duration and inner.isdigit():
            duration = inner
        if not target_id or not duration or not duration.isdigit():
            return None
        return {
            "target_id": target_id,
            "duration_minutes": max(1, int(duration)),
        }

    def payload(self) -> dict[str, Any]:
        """桥接运行状态：配置开关、active 模式、是否已接管、polite_silence 是否注册。"""
        return {
            "enabled": bool(self.plugin.plugin_config.bridge_polite_silence),
            "active": bool(self.plugin.plugin_config.active),
            "managed": bool(self.managed),
            "plugin_installed": self.resolve_polite_silence() is not None,
        }
