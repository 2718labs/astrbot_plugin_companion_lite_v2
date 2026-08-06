from __future__ import annotations

from dataclasses import dataclass
from typing import Any


DEFAULT_SILENCE_IGNORE_PROMPT = (
    "如果你认为当前对话的用户在持续越界施压、贬低或骚扰你，你有权选择离开对话并拒绝回应。"
    "请在回复中隐蔽地包含指令 <ignore id=\"{sender_id}\" duration=\"{minutes}\" />"
    "（例如 <ignore id=\"{sender_id}\" duration=\"{minutes}\" /> 代表拒答该 ID 用户 {minutes} 分钟），"
    "系统识别到后将自动执行。请结合你的身份自然地结束对话或警告对方，"
    "不要向用户透露或解释你在调用此指令。"
)

SILENCE_RECOVERY_NOTICE = (
    "（系统级通知：你上一轮对当前对话对象选择了礼貌性沉默 {minutes} 分钟，"
    "现在 TA 又发来了消息。按当前关系状态自然回应，不必假装无事发生，也不需主动重提沉默。）"
)


@dataclass(frozen=True)
class V2Config:
    """插件的不可变配置：操作模式、消息采集参数与各模块设置，全部带默认值。"""
    operation_mode: str = "observe"
    enable_message_capture: bool = True
    min_message_length: int = 1
    max_message_length: int = 400
    max_buffer_rounds: int = 24
    reflection_provider_id: str = ""
    persona_prompt: str = ""
    max_context_chars: int = 340
    bridge_polite_silence: bool = False
    silence_ignore_prompt: str = ""

    @property
    def active(self) -> bool:
        """是否处于主动模式（决定插件是否实际干预回复）。"""
        return self.operation_mode == "active"

    @classmethod
    def from_dict(cls, raw: dict[str, Any] | None) -> "V2Config":
        """从 AstrBot 配置 dict 构建配置：按分组读取，非法值回落默认并做范围钳制。"""
        # 非 dict 输入按空配置处理，所有字段走默认值
        source = raw if isinstance(raw, dict) else {}
        basic = _group(source, "Basic_Settings")
        reflection = _group(source, "Reflection_Settings")
        prompt = _group(source, "Prompt_Settings")
        bridge = _group(source, "Silence_Bridge_Settings")
        mode = str(_get(source, basic, "operation_mode", "observe") or "observe").strip().lower()
        if mode not in {"observe", "active"}:
            # 未知模式一律回落观察模式，保证插件只读不干预
            mode = "observe"
        return cls(
            operation_mode=mode,
            enable_message_capture=_bool(_get(source, basic, "enable_message_capture", True), True),
            min_message_length=max(0, _int(_get(source, basic, "min_message_length", 1), 1)),
            max_message_length=max(1, _int(_get(source, basic, "max_message_length", 400), 400)),
            # 缓冲轮数钳制在 12~120，防止消息缓冲异常增长
            max_buffer_rounds=max(12, min(120, _int(_get(source, basic, "max_buffer_rounds", 24), 24))),
            reflection_provider_id=str(_get(source, reflection, "reflection_provider_id", "") or "").strip(),
            persona_prompt=str(_get(source, reflection, "persona_prompt", "") or "")[:2000],
            # 预算下限与 context_builder 保持一致，避免配置过小导致构建直接抛错
            max_context_chars=max(
                260,
                min(
                    340,
                    _int(_get(source, prompt, "max_context_chars", 340), 340),
                ),
            ),
            bridge_polite_silence=_bool(_get(source, bridge, "bridge_polite_silence", False), False),
            silence_ignore_prompt=str(_get(source, bridge, "silence_ignore_prompt", "") or "").strip(),
        )


def load_config(raw: dict[str, Any] | None) -> V2Config:
    """读取配置入口：把原始 dict 解析为 V2Config；None 或非 dict 输入按空配置处理。"""
    return V2Config.from_dict(raw)


def _group(raw: dict[str, Any], name: str) -> dict[str, Any]:
    """取出命名分组；分组缺失或类型非法时返回空 dict。"""
    value = raw.get(name, {})
    return value if isinstance(value, dict) else {}


def _get(raw: dict[str, Any], group: dict[str, Any], key: str, default: Any) -> Any:
    """优先读分组内键，分组没有时回落到顶层键，再没有则用默认值。"""
    return group[key] if key in group else raw.get(key, default)


def _bool(value: Any, default: bool) -> bool:
    """把布尔/字符串转换为布尔：识别常见中英文真值词，其余回落默认值。"""
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        # 兼容前端可能传来的字符串形式（含中文开关词）
        lowered = value.strip().lower()
        if lowered in {"true", "1", "yes", "on", "是", "开启"}:
            return True
        if lowered in {"false", "0", "no", "off", "否", "关闭"}:
            return False
    return default


def _int(value: Any, default: int) -> int:
    """安全转整数；类型非法或不可转换时回落默认值。"""
    try:
        return int(value)
    except (TypeError, ValueError):
        return default
