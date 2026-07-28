from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class V2Config:
    operation_mode: str = "observe"
    enable_message_capture: bool = True
    min_message_length: int = 1
    max_message_length: int = 400
    max_buffer_rounds: int = 24
    reflection_provider_id: str = ""
    persona_prompt: str = ""
    max_context_chars: int = 340

    @property
    def active(self) -> bool:
        return self.operation_mode == "active"

    @classmethod
    def from_dict(cls, raw: dict[str, Any] | None) -> "V2Config":
        source = raw if isinstance(raw, dict) else {}
        basic = _group(source, "Basic_Settings")
        reflection = _group(source, "Reflection_Settings")
        prompt = _group(source, "Prompt_Settings")
        mode = str(_get(source, basic, "operation_mode", "observe") or "observe").strip().lower()
        if mode not in {"observe", "active"}:
            mode = "observe"
        return cls(
            operation_mode=mode,
            enable_message_capture=_bool(_get(source, basic, "enable_message_capture", True), True),
            min_message_length=max(0, _int(_get(source, basic, "min_message_length", 1), 1)),
            max_message_length=max(1, _int(_get(source, basic, "max_message_length", 400), 400)),
            max_buffer_rounds=max(12, min(120, _int(_get(source, basic, "max_buffer_rounds", 24), 24))),
            reflection_provider_id=str(_get(source, reflection, "reflection_provider_id", "") or "").strip(),
            persona_prompt=str(_get(source, reflection, "persona_prompt", "") or "")[:2000],
            max_context_chars=max(
                260,
                min(
                    340,
                    _int(_get(source, prompt, "max_context_chars", 340), 340),
                ),
            ),
        )


def load_config(raw: dict[str, Any] | None) -> V2Config:
    return V2Config.from_dict(raw)


def _group(raw: dict[str, Any], name: str) -> dict[str, Any]:
    value = raw.get(name, {})
    return value if isinstance(value, dict) else {}


def _get(raw: dict[str, Any], group: dict[str, Any], key: str, default: Any) -> Any:
    return group[key] if key in group else raw.get(key, default)


def _bool(value: Any, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"true", "1", "yes", "on", "是", "开启"}:
            return True
        if lowered in {"false", "0", "no", "off", "否", "关闭"}:
            return False
    return default


def _int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default
