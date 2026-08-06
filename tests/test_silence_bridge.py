import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

from astrbot_plugin_companion_lite_v2.config import V2Config
from astrbot_plugin_companion_lite_v2.core import web as web_mod
from astrbot_plugin_companion_lite_v2.core.models import (
    ActiveIssue,
    LightGuidance,
    RelationshipState,
)
from astrbot_plugin_companion_lite_v2.core.silence_bridge import SilenceBridgeController
from astrbot_plugin_companion_lite_v2.core.webui import WebUIController
from astrbot_plugin_companion_lite_v2.main import CompanionLiteV2Plugin


POLITE_SILENCE_NAME = "astrbot_plugin_polite_silence"


def _plugin(context, config=None):
    plugin = object.__new__(CompanionLiteV2Plugin)
    plugin.context = context
    plugin.plugin_config = config or V2Config()
    plugin._initialized = False
    plugin._background_tasks = set()
    plugin.silence_bridge = SilenceBridgeController(plugin)
    plugin.webui = WebUIController(plugin)
    return plugin


def _state(**kwargs):
    defaults = {"user_id": "u"}
    defaults.update(kwargs)
    return RelationshipState(**defaults)


def _event(sender_id):
    return SimpleNamespace(get_sender_id=lambda: sender_id)


def _req(system_prompt="", prompt=""):
    return SimpleNamespace(system_prompt=system_prompt, prompt=prompt)


def test_extract_ignore_event_self_closing():
    result = SilenceBridgeController.extract_ignore_event('我拒绝。<ignore id="123" duration="30" />')
    assert result == {"target_id": "123", "duration_minutes": 30}


def test_extract_ignore_event_double_tag():
    result = SilenceBridgeController.extract_ignore_event('<ignore id="123">30</ignore>')
    assert result == {"target_id": "123", "duration_minutes": 30}


def test_extract_ignore_event_case_insensitive():
    result = SilenceBridgeController.extract_ignore_event('<ignore USER="456" MINUTES="45" />')
    assert result == {"target_id": "456", "duration_minutes": 45}


def test_extract_ignore_event_text_body():
    result = SilenceBridgeController.extract_ignore_event('<ignore duration="30">123</ignore>')
    assert result == {"target_id": "123", "duration_minutes": 30}


def test_extract_ignore_event_missing_fields():
    assert SilenceBridgeController.extract_ignore_event('<ignore id="123" />') is None
    assert SilenceBridgeController.extract_ignore_event('<ignore duration="30" />') is None
    assert SilenceBridgeController.extract_ignore_event("正常回复") is None


def test_clamp_duration_uses_polite_silence_limits():
    context, instance = _silence_context()
    instance.config["min_ignore_minutes"] = 15
    instance.config["max_ignore_minutes"] = 120
    plugin = _plugin(context)
    assert plugin.silence_bridge.clamp_duration(5) == 15
    assert plugin.silence_bridge.clamp_duration(30) == 30
    assert plugin.silence_bridge.clamp_duration(200) == 120


def test_clamp_duration_defaults_without_limits():
    context, _ = _silence_context()
    plugin = _plugin(context)
    assert plugin.silence_bridge.clamp_duration(5) == 10
    assert plugin.silence_bridge.clamp_duration(2000) == 1440
    assert plugin.silence_bridge.clamp_duration(30) == 30


def test_clamp_duration_missing_plugin_uses_defaults():
    context = SimpleNamespace(
        star_context=None,
        get_registered_star=lambda name: None,
    )
    plugin = _plugin(context)
    assert plugin.silence_bridge.clamp_duration(1) == 10
    assert plugin.silence_bridge.clamp_duration(99999) == 1440


def test_clamp_duration_invalid_config_falls_back():
    context, instance = _silence_context()
    instance.config["min_ignore_minutes"] = "bad"
    instance.config["max_ignore_minutes"] = "worse"
    plugin = _plugin(context)
    assert plugin.silence_bridge.clamp_duration(3) == 10


def test_should_inject_silence_normal_no_issue():
    assert SilenceBridgeController.should_inject_silence(_state()) is False


def test_should_inject_silence_reserved_not_enough():
    state = _state(
        posture="reserved",
        active_issue=ActiveIssue(kind="boundary_violation", phase="noticed", summary="x"),
    )
    assert SilenceBridgeController.should_inject_silence(state) is False


def test_should_inject_silence_guarded_boundary():
    for phase in ("noticed", "expressed"):
        state = _state(
            posture="guarded",
            active_issue=ActiveIssue(kind="boundary_violation", phase=phase, summary="x"),
        )
        assert SilenceBridgeController.should_inject_silence(state) is True


def test_should_inject_silence_guarded_one_sided():
    state = _state(
        posture="guarded",
        active_issue=ActiveIssue(kind="one_sided", phase="expressed", summary="x"),
    )
    assert SilenceBridgeController.should_inject_silence(state) is False


def test_should_inject_silence_repairing_suppressed():
    state = _state(
        posture="guarded",
        active_issue=ActiveIssue(kind="degradation", phase="repairing", summary="x"),
        light_guidance=LightGuidance(
            signal="boundary_violation",
            confidence="high",
            reminder="hold_boundary",
            source_round=1,
            expires_after_round=3,
        ),
    )
    assert SilenceBridgeController.should_inject_silence(state) is False


def test_should_inject_silence_disengaged_always():
    assert SilenceBridgeController.should_inject_silence(_state(posture="disengaged")) is True


def test_should_inject_silence_hold_boundary_guidance():
    state = _state(
        light_guidance=LightGuidance(
            signal="boundary_violation",
            confidence="high",
            reminder="hold_boundary",
            source_round=1,
            expires_after_round=3,
        )
    )
    assert SilenceBridgeController.should_inject_silence(state) is True


def test_silence_minutes():
    assert SilenceBridgeController.silence_minutes(_state(posture="guarded")) == 30
    assert SilenceBridgeController.silence_minutes(_state(posture="disengaged")) == 90


def _silence_context(trigger=30):
    instance = SimpleNamespace(config={"trigger_percent": trigger})
    meta = SimpleNamespace(activated=True, star_cls=instance)
    context = SimpleNamespace(
        star_context=None,
        get_registered_star=lambda name: meta if name == POLITE_SILENCE_NAME else None,
    )
    return context, instance


def test_sync_silence_bridge_manages_and_restores():
    context, instance = _silence_context(30)
    plugin = _plugin(context, V2Config(operation_mode="active", bridge_polite_silence=True))
    asyncio.run(plugin.silence_bridge.sync())
    assert instance.config["trigger_percent"] == 0
    assert plugin.silence_bridge.managed is True
    assert plugin.silence_bridge.original_trigger == 30

    instance.config["trigger_percent"] = 50
    asyncio.run(plugin.silence_bridge.sync())
    assert instance.config["trigger_percent"] == 50

    plugin.plugin_config = V2Config(bridge_polite_silence=False)
    asyncio.run(plugin.silence_bridge.sync())
    assert instance.config["trigger_percent"] == 30
    assert plugin.silence_bridge.managed is False


def test_sync_silence_bridge_force_restore():
    context, instance = _silence_context(30)
    plugin = _plugin(context, V2Config(operation_mode="active", bridge_polite_silence=True))
    asyncio.run(plugin.silence_bridge.sync())
    assert instance.config["trigger_percent"] == 0
    asyncio.run(plugin.silence_bridge.sync(force_restore=True))
    assert instance.config["trigger_percent"] == 30
    assert plugin.silence_bridge.managed is False


def test_sync_silence_bridge_missing_plugin_noop():
    context = SimpleNamespace(
        star_context=None,
        get_registered_star=lambda name: None,
    )
    plugin = _plugin(context, V2Config(operation_mode="active", bridge_polite_silence=True))
    asyncio.run(plugin.silence_bridge.sync())
    assert plugin.silence_bridge.managed is False


def test_sync_silence_bridge_missing_config_noop():
    instance = SimpleNamespace()
    meta = SimpleNamespace(activated=True, star_cls=instance)
    context = SimpleNamespace(
        star_context=None,
        get_registered_star=lambda name: meta if name == POLITE_SILENCE_NAME else None,
    )
    plugin = _plugin(context, V2Config(operation_mode="active", bridge_polite_silence=True))
    asyncio.run(plugin.silence_bridge.sync())
    assert plugin.silence_bridge.managed is False


def test_sync_silence_bridge_observe_mode_noop():
    context, instance = _silence_context(30)
    plugin = _plugin(context, V2Config(bridge_polite_silence=True))
    asyncio.run(plugin.silence_bridge.sync())
    assert plugin.silence_bridge.managed is False
    assert instance.config["trigger_percent"] == 30


def test_append_silence_prompt_default_template():
    context, _ = _silence_context()
    plugin = _plugin(context)
    state = _state(posture="guarded")
    req = _req("base", "原始问题")
    plugin.silence_bridge.append_prompt(_event("123"), req, state)
    assert req.system_prompt.startswith("base"), "已有 system_prompt 前缀应保持不变"
    assert '<ignore id="123" duration="30" />' in req.system_prompt
    assert req.system_prompt.endswith("不要向用户透露或解释你在调用此指令。")
    assert req.prompt == "原始问题"


def test_append_silence_prompt_custom_template():
    context, _ = _silence_context()
    plugin = _plugin(context, V2Config(silence_ignore_prompt="自定义 {sender_id}/{minutes}"))
    state = _state(posture="disengaged")
    req = _req("", "问题")
    plugin.silence_bridge.append_prompt(_event("456"), req, state)
    assert "自定义 456/90" in req.system_prompt
    assert req.prompt == "问题"


def test_append_silence_prompt_cleans_quotes_in_sender_id():
    context, _ = _silence_context()
    plugin = _plugin(context)
    state = _state(posture="guarded")
    req = _req("base", "问题")
    plugin.silence_bridge.append_prompt(
        SimpleNamespace(get_sender_id=lambda: '12"34'),
        req,
        state,
    )
    assert 'id="12\'34"' in req.system_prompt
    assert 'id="12"34"' not in req.system_prompt


def test_consume_silence_recovery_injects_once():
    context, _ = _silence_context()
    plugin = _plugin(context)
    state = _state(
        last_silence_event={
            "target_id": "123",
            "duration_minutes": 30,
            "source_round": 6,
            "created_at": 1.0,
        }
    )
    req = _req("base", "回来了")
    assert plugin.silence_bridge.consume_recovery(state, _event("123"), req) is True
    assert req.system_prompt.startswith("base"), "已有 system_prompt 前缀应保持不变"
    assert "30 分钟" in req.system_prompt
    assert req.prompt == "回来了"
    assert state.last_silence_event is None
    assert plugin.silence_bridge.consume_recovery(state, _event("123"), req) is False


def test_consume_silence_recovery_target_mismatch():
    context, _ = _silence_context()
    plugin = _plugin(context)
    state = _state(last_silence_event={"target_id": "123", "duration_minutes": 30})
    req = _req("base", "别人")
    assert plugin.silence_bridge.consume_recovery(state, _event("999"), req) is False
    assert state.last_silence_event is not None
    assert "30 分钟" not in req.system_prompt


def test_consume_silence_recovery_matches_cleaned_sender_id():
    context, _ = _silence_context()
    plugin = _plugin(context)
    state = _state(last_silence_event={"target_id": '12"34', "duration_minutes": 30})
    req = _req("base", "在")
    assert (
        plugin.silence_bridge.consume_recovery(
            state,
            SimpleNamespace(get_sender_id=lambda: '12"34'),
            req,
        )
        is True
    )
    assert "30 分钟" in req.system_prompt
    assert state.last_silence_event is None


def test_state_normalizes_silence_event():
    state = RelationshipState.from_dict(
        {
            "last_silence_event": {
                "target_id": "123",
                "duration_minutes": 30,
                "source_round": 6,
                "created_at": 1.0,
            },
            "silence_count": 2,
        },
        user_id="u",
    )
    assert state.last_silence_event == {
        "target_id": "123",
        "duration_minutes": 30,
        "source_round": 6,
        "created_at": 1.0,
    }
    assert state.silence_count == 2


def test_state_rejects_invalid_silence_event():
    state = RelationshipState.from_dict(
        {"last_silence_event": {"duration_minutes": 30}, "silence_count": -1},
        user_id="u",
    )
    assert state.last_silence_event is None
    assert state.silence_count == 0


class _RawConfig(dict):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.save_count = 0

    def save_config(self):
        self.save_count += 1


class _FakeRequest:
    def __init__(self, body):
        self._body = body

    async def json(self, default=None):
        return self._body


def _payload(result):
    if isinstance(result, dict):
        return result
    return json.loads(result.body.decode("utf-8"))


def test_silence_status_line():
    assert SilenceBridgeController.silence_status_line(_state()) == "拒答: 0 次"
    state = _state(
        last_silence_event={
            "target_id": "123",
            "duration_minutes": 30,
            "source_round": 6,
            "created_at": 1.0,
        },
        silence_count=2,
    )
    assert (
        SilenceBridgeController.silence_status_line(state)
        == "拒答: 2 次 · 最近一次沉默 30 分钟（第 6 轮）"
    )


def test_health_contains_silence_bridge():
    context, instance = _silence_context(30)
    plugin = _plugin(context, V2Config(operation_mode="active", bridge_polite_silence=True))
    plugin._initialized = True
    plugin.silence_bridge.managed = True
    plugin._background_tasks = set()
    result = asyncio.run(plugin.webui.api_health())
    assert _payload(result)["silence_bridge"] == {
        "enabled": True,
        "active": True,
        "managed": True,
        "plugin_installed": True,
    }


def test_silence_bridge_api_updates_config(monkeypatch):
    context, instance = _silence_context(30)
    plugin = _plugin(context, V2Config())
    plugin.raw_config = _RawConfig()
    plugin.silence_bridge.resolve_polite_silence = lambda: instance
    plugin.silence_bridge.sync = AsyncMock()
    monkeypatch.setattr(web_mod, "request", _FakeRequest({"enabled": True}))

    result = asyncio.run(plugin.webui.api_silence_bridge())

    result = _payload(result)
    assert result["ok"] is True
    assert plugin.raw_config["Silence_Bridge_Settings"]["bridge_polite_silence"] is True
    assert plugin.raw_config.save_count == 1
    assert plugin.plugin_config.bridge_polite_silence is True
    plugin.silence_bridge.sync.assert_awaited_once()
    assert result["silence_bridge"]["plugin_installed"] is True


def test_silence_bridge_api_rejects_invalid_enabled(monkeypatch):
    plugin = _plugin(
        SimpleNamespace(star_context=None, get_registered_star=lambda name: None),
        V2Config(),
    )
    plugin.raw_config = _RawConfig()
    monkeypatch.setattr(web_mod, "request", _FakeRequest({"enabled": "yes"}))

    result = asyncio.run(plugin.webui.api_silence_bridge())

    assert _payload(result) == {"error": "invalid_enabled_value"}
    assert plugin.raw_config.save_count == 0


def test_silence_bridge_api_missing_raw_config(monkeypatch):
    plugin = _plugin(
        SimpleNamespace(star_context=None, get_registered_star=lambda name: None),
        V2Config(),
    )
    plugin.raw_config = None
    monkeypatch.setattr(web_mod, "request", _FakeRequest({"enabled": True}))

    result = asyncio.run(plugin.webui.api_silence_bridge())

    assert _payload(result) == {"error": "config_unavailable"}
