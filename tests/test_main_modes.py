import asyncio
from types import SimpleNamespace

from astrbot.api.platform import MessageType

import astrbot_plugin_companion_lite_v2.main as main_impl
from astrbot_plugin_companion_lite_v2.core import web as web_mod
from astrbot_plugin_companion_lite_v2.core.models import (
    DeepEvidence,
    LightEvidence,
    RelationshipState,
    SevereEvidence,
)
from astrbot_plugin_companion_lite_v2.core.persona import PersonaResolution
from astrbot_plugin_companion_lite_v2.core.webui import WebUIController
from astrbot_plugin_companion_lite_v2.llm import COMPANION_STATIC_PROTOCOL
from astrbot_plugin_companion_lite_v2.llm.reflection import ReflectionOutcome


class FakeContext:
    def __init__(self):
        self.routes = []

    def register_web_api(self, *args):
        self.routes.append(args)

    def get_provider_by_id(self, _):
        return None

    def get_using_provider(self, _):
        return None


class FakeEvent:
    def __init__(
        self,
        message_id: str,
        session_id: str = "private",
        unified_msg_origin: str = "",
    ):
        self.message_obj = SimpleNamespace(
            message_id=message_id,
            platform_name="test",
            session_id=session_id,
        )
        self.unified_msg_origin = unified_msg_origin
        self._extra = {}
        self.session_id = session_id

    def get_message_type(self):
        return MessageType.FRIEND_MESSAGE

    def get_message_str(self):
        return "你好，帮我看看这个问题"

    def get_sender_id(self):
        return "user"

    def get_platform_name(self):
        return "test"

    def get_session_id(self):
        return self.session_id

    def get_extra(self, key, default=None):
        return self._extra.get(key, default)

    def set_extra(self, key, value):
        self._extra[key] = value


def _exercise_mode(tmp_path, monkeypatch, mode: str):
    monkeypatch.setattr(main_impl, "get_astrbot_data_path", lambda: str(tmp_path))
    plugin = main_impl.CompanionLiteV2Plugin(FakeContext(), {"Basic_Settings": {"operation_mode": mode}})
    event = FakeEvent(f"message-{mode}")
    request = SimpleNamespace(
        prompt="原始请求",
        system_prompt="原始人格",
        extra_user_content_parts=None,
    )

    async def run():
        await plugin.initialize()
        await plugin.inject_companion_context(event, request)
        state = await plugin._load_state("test:FriendMessage:private")
        await plugin.terminate()
        return state

    return request, asyncio.run(run())


def test_observe_mode_compiles_but_never_mutates_request(tmp_path, monkeypatch):
    request, state = _exercise_mode(tmp_path, monkeypatch, "observe")
    assert request.prompt == "原始请求"
    assert request.system_prompt == "原始人格"
    assert state.last_compiled_context.startswith("<companion_state>")
    assert state.last_context_injected is False


def test_active_mode_injects_once_and_records_actual_injection(tmp_path, monkeypatch):
    request, state = _exercise_mode(tmp_path, monkeypatch, "active")
    assert request.prompt.count("<companion_state>") == 1
    assert request.system_prompt.startswith(COMPANION_STATIC_PROTOCOL)
    assert request.system_prompt.endswith("原始人格")
    assert request.system_prompt.count("<companion_protocol") == 1
    assert state.last_context_injected is True


def test_active_mode_appends_one_temporary_part_after_existing_memory(tmp_path, monkeypatch):
    class FakeTextPart:
        def __init__(self, text):
            self.text = text
            self.is_temp = False

        def mark_as_temp(self):
            self.is_temp = True
            return self

    monkeypatch.setattr(main_impl, "get_astrbot_data_path", lambda: str(tmp_path))
    monkeypatch.setattr(main_impl, "TextPart", FakeTextPart)
    plugin = main_impl.CompanionLiteV2Plugin(FakeContext(), {"Basic_Settings": {"operation_mode": "active"}})
    event = FakeEvent("message-temp")
    memory = FakeTextPart("<living_memory>事实背景</living_memory>")
    request = SimpleNamespace(
        prompt="原始请求",
        system_prompt="botname人格",
        extra_user_content_parts=[memory],
    )

    async def run():
        await plugin.initialize()
        await plugin.inject_companion_context(event, request)
        await plugin.inject_companion_context(event, request)
        state = await plugin._load_state("test:FriendMessage:private")
        await plugin.terminate()
        return state

    state = asyncio.run(run())
    assert request.prompt == "原始请求"
    assert request.system_prompt.startswith(COMPANION_STATIC_PROTOCOL)
    assert request.system_prompt.endswith("botname人格")
    assert request.system_prompt.count("<companion_protocol") == 1
    assert len(request.extra_user_content_parts) == 2
    assert request.extra_user_content_parts[0] is memory
    relation = request.extra_user_content_parts[1]
    assert relation.text.startswith("<companion_state>")
    assert relation.is_temp is True
    assert state.last_context_injected is True


def test_same_sender_in_different_private_sessions_has_separate_identity(tmp_path, monkeypatch):
    monkeypatch.setattr(main_impl, "get_astrbot_data_path", lambda: str(tmp_path))
    plugin = main_impl.CompanionLiteV2Plugin(FakeContext(), {})
    try:
        assert plugin._user_identity(
            FakeEvent(
                "1",
                "session-a",
                unified_msg_origin="botname:FriendMessage:100000002",
            )
        ) == ("botname:FriendMessage:100000002")
        assert plugin._user_identity(
            FakeEvent(
                "2",
                "session-b",
                unified_msg_origin="botname:FriendMessage:100000001",
            )
        ) == ("botname:FriendMessage:100000001")
    finally:
        plugin.storage.close()


def test_debug_identity_parts_preserve_colons_inside_session_id():
    assert WebUIController.identity_parts("qq:private:thread:42:user-7") == {
        "umo": "qq:private:thread:42:user-7",
        "platform": "qq",
        "session_type": "private",
        "session_target": "thread:42:user-7",
        "session_id": "qq:private:thread:42:user-7",
        "sender_id": "thread:42:user-7",
    }


def test_debug_actions_accept_user_id_from_json_body(tmp_path, monkeypatch):
    class FakeRequest:
        query = {}

        async def json(self, _default=None):
            return {"user_id": "botname:FriendMessage:100000001"}

    monkeypatch.setattr(main_impl, "get_astrbot_data_path", lambda: str(tmp_path))
    monkeypatch.setattr(web_mod, "request", FakeRequest())
    plugin = main_impl.CompanionLiteV2Plugin(FakeContext(), {})
    try:
        assert asyncio.run(plugin.webui.resolve_user_id()) == ("botname:FriendMessage:100000001")
    finally:
        plugin.storage.close()


def test_capture_hard_limits_long_messages_but_keeps_head_and_tail(tmp_path, monkeypatch):
    monkeypatch.setattr(main_impl, "get_astrbot_data_path", lambda: str(tmp_path))
    plugin = main_impl.CompanionLiteV2Plugin(
        FakeContext(),
        {"Basic_Settings": {"max_message_length": 80}},
    )
    try:
        original = "开" * 140 + "结" * 80
        clipped = plugin._truncate_captured_text(original)
        assert plugin._should_capture_text(original)
        assert len(clipped) == 80
        assert clipped.startswith("开")
        assert clipped.endswith("结")
        assert "中间内容已截断" in clipped
        assert "220 字" in clipped
    finally:
        plugin.storage.close()


def test_deep_reflection_gets_latest_twenty_messages_and_runtime_persona(tmp_path, monkeypatch):
    monkeypatch.setattr(main_impl, "get_astrbot_data_path", lambda: str(tmp_path))
    plugin = main_impl.CompanionLiteV2Plugin(FakeContext(), {})
    user_id = "test:private:user"
    captured = {}

    async def analyze(state, messages, persona_prompt="", relationship_role="unbound"):
        captured["messages"] = messages
        captured["persona_prompt"] = persona_prompt
        captured["relationship_role"] = relationship_role
        return ReflectionOutcome(
            DeepEvidence(
                pattern="none",
                confidence="medium",
                familiarity_change="clear",
                relationship_summary="已经交流了一段时间",
                impression_operation="revise",
                impression="我觉得对方很直接。",
            ),
            {
                "kind": "deep",
                "prompt_version": "test",
                "model_tags": {"pattern": "none"},
            },
        )

    async def run():
        await plugin.initialize()
        for round_number in range(1, 13):
            key = f"k{round_number}"
            plugin.storage.claim_interaction(key, user_id, f"user-{round_number}")
            plugin.storage.complete_interaction(key, user_id, f"assistant-{round_number}", round_number)
        plugin._save_state(RelationshipState(user_id=user_id, round_sequence=12))
        plugin._persona_by_user[user_id] = "botname式主人格"
        plugin.reflection.analyze_deep = analyze
        ok = await plugin.reflection_service.perform(user_id, 12, "deep")
        state = await plugin._load_state(user_id)
        await plugin.terminate()
        return ok, state

    ok, state = asyncio.run(run())
    assert ok
    assert len(captured["messages"]) == 20
    assert captured["messages"][0]["content"] == "user-3"
    assert captured["messages"][-1]["content"] == "assistant-12"
    assert captured["persona_prompt"] == "botname式主人格"
    assert captured["relationship_role"] == "unbound"
    assert state.familiarity == 5
    assert state.relationship_summary == "已经交流了一段时间"
    assert state.impression == "我觉得对方很直接。"


def test_reset_clears_profile_messages_queue_and_bond(tmp_path, monkeypatch):
    monkeypatch.setattr(main_impl, "get_astrbot_data_path", lambda: str(tmp_path))
    plugin = main_impl.CompanionLiteV2Plugin(FakeContext(), {})
    user_id = "test:FriendMessage:user"

    async def run():
        await plugin.initialize()
        plugin._save_state(
            RelationshipState(
                user_id,
                familiarity=20,
                trust=10,
                affinity=-30,
                posture="guarded",
                impression="我不想再理他了",
                round_sequence=4,
            )
        )
        plugin.storage.claim_interaction("reset-k", user_id, "继续回答")
        plugin.storage.bind_persona("persona-name", user_id)
        queue = plugin.reflection_service.queues.setdefault(user_id, [])
        queue.append((4, "light"))

        deleted = await plugin.webui.reset_user(user_id)
        state = await plugin._load_state(user_id)
        messages = plugin.storage.get_recent_messages(user_id)
        bond = plugin.storage.get_bond("persona-name")
        await plugin.terminate()
        return state, messages, bond, queue, deleted

    state, messages, bond, old_queue, deleted = asyncio.run(run())
    assert (state.familiarity, state.trust, state.affinity) == (0, 50, 0)
    assert state.posture == "normal"
    assert state.impression == ""
    assert state.active_issue is None
    assert state.round_sequence == 0
    assert messages == []
    assert bond is None
    assert old_queue == []
    assert deleted == {"messages_deleted": 1, "rounds_deleted": 0}


def test_light_reflection_records_none_as_a_visible_success(tmp_path, monkeypatch):
    monkeypatch.setattr(main_impl, "get_astrbot_data_path", lambda: str(tmp_path))
    plugin = main_impl.CompanionLiteV2Plugin(FakeContext(), {})
    user_id = "test:private:user"

    async def analyze(
        _state,
        _messages,
        source_round,
        relationship_role="unbound",
    ):
        return ReflectionOutcome(
            LightEvidence(
                signal="none",
                confidence="low",
                evidence="",
            ),
            {
                "kind": "light",
                "prompt_version": "test",
                "model_tags": {"signal": "none"},
            },
        )

    async def run():
        await plugin.initialize()
        for round_number in range(1, 3):
            key = f"k{round_number}"
            plugin.storage.claim_interaction(key, user_id, f"user-{round_number}")
            plugin.storage.complete_interaction(key, user_id, f"assistant-{round_number}", round_number)
        plugin._save_state(RelationshipState(user_id=user_id, round_sequence=2))
        plugin.reflection.analyze_light = analyze
        ok = await plugin.reflection_service.perform(user_id, 2, "light")
        state = await plugin._load_state(user_id)
        await plugin.terminate()
        return ok, state

    ok, state = asyncio.run(run())
    assert ok
    assert state.light_guidance is None
    assert state.last_analysis_kind == "light"
    assert state.last_analysis_round == 2
    assert state.last_analysis_status == "none"
    assert state.last_analysis_signal == "none"
    assert state.last_analysis_confidence == "low"


def test_initialize_recovers_latest_unobserved_light_round(tmp_path, monkeypatch):
    monkeypatch.setattr(main_impl, "get_astrbot_data_path", lambda: str(tmp_path))
    plugin = main_impl.CompanionLiteV2Plugin(FakeContext(), {})
    plugin._save_state(RelationshipState(user_id="test:private:user", round_sequence=4))
    scheduled = []
    plugin.reflection_service.enqueue = lambda user_id, target_round, kind: scheduled.append((user_id, target_round, kind)) or True

    async def run():
        await plugin.initialize()
        await plugin.terminate()

    asyncio.run(run())
    assert scheduled == [("test:private:user", 4, "light")]


def test_v2_registers_session_and_message_debug_routes(tmp_path, monkeypatch):
    monkeypatch.setattr(main_impl, "get_astrbot_data_path", lambda: str(tmp_path))
    context = FakeContext()
    plugin = main_impl.CompanionLiteV2Plugin(context, {})
    try:
        paths = {item[0] for item in context.routes}
        assert "/astrbot_plugin_companion_lite_v2/page/sessions" in paths
        assert "/astrbot_plugin_companion_lite_v2/page/messages" in paths
        assert "/astrbot_plugin_companion_lite_v2/page/reset/<path:user_id>" in paths
        assert "/astrbot_plugin_companion_lite_v2/page/enabled" in paths
        assert "/astrbot_plugin_companion_lite_v2/page/rebuild" in paths
    finally:
        plugin.storage.close()


def test_web_api_guard_returns_json_error_on_exception(tmp_path, monkeypatch):
    monkeypatch.setattr(main_impl, "get_astrbot_data_path", lambda: str(tmp_path))
    monkeypatch.setattr(web_mod, "json_response", lambda payload: payload)
    plugin = main_impl.CompanionLiteV2Plugin(FakeContext(), {})
    try:
        async def boom():
            raise RuntimeError("boom")

        result = asyncio.run(plugin.webui._guarded(boom)())
        assert result == {"error": "internal_error"}
    finally:
        plugin.storage.close()


def test_sessions_api_uses_management_page_safety_ceiling(tmp_path, monkeypatch):
    monkeypatch.setattr(main_impl, "get_astrbot_data_path", lambda: str(tmp_path))
    monkeypatch.setattr(web_mod, "json_response", lambda payload: payload)
    plugin = main_impl.CompanionLiteV2Plugin(FakeContext(), {})
    captured = {}

    def list_states(limit=200):
        captured["limit"] = limit
        return []

    monkeypatch.setattr(plugin.storage, "list_states", list_states)
    try:
        result = asyncio.run(plugin.webui.api_sessions())
        assert captured["limit"] == 1000
        assert result == {"sessions": [], "count": 0}
    finally:
        plugin.storage.close()


def test_disabled_umo_skips_capture_injection_and_all_llm_analysis(tmp_path, monkeypatch):
    monkeypatch.setattr(main_impl, "get_astrbot_data_path", lambda: str(tmp_path))
    plugin = main_impl.CompanionLiteV2Plugin(FakeContext(), {"Basic_Settings": {"operation_mode": "active"}})
    user_id = "test:FriendMessage:private"
    event = FakeEvent("disabled-message")
    event.get_message_str = lambda: "你只是工具，必须服从"
    request = SimpleNamespace(
        prompt="原始请求",
        system_prompt="原始人格",
        extra_user_content_parts=None,
    )
    response = SimpleNamespace(
        role="assistant",
        completion_text="原始回复",
        tools_call_name=None,
        tools_call_extra_content=None,
    )
    calls = []

    async def fail_analysis(*_args, **_kwargs):
        calls.append(True)
        raise AssertionError("disabled UMO must not call companion LLMs")

    async def run():
        await plugin.initialize()
        await plugin._load_state(user_id)
        result = await plugin.webui.set_user_enabled(user_id, False)
        plugin.reflection.analyze_severe = fail_analysis
        plugin.reflection.analyze_light = fail_analysis
        plugin.reflection.analyze_deep = fail_analysis
        await plugin.inject_companion_context(event, request)
        await plugin.capture_llm_response(event, response)
        reflected = await plugin.reflection_service.perform(user_id, 2, "deep")
        state = await plugin._load_state(user_id)
        messages = plugin.storage.get_recent_messages(user_id)
        await plugin.terminate()
        return result, reflected, state, messages

    result, reflected, state, messages = asyncio.run(run())
    assert result["enabled"] is False
    assert reflected is False
    assert calls == []
    assert request.prompt == "原始请求"
    assert request.system_prompt == "原始人格"
    assert messages == []
    assert state.round_sequence == 0
    assert state.last_compiled_context == ""


def test_silence_event_recorded_with_clamped_duration(tmp_path, monkeypatch):
    monkeypatch.setattr(main_impl, "get_astrbot_data_path", lambda: str(tmp_path))
    plugin = main_impl.CompanionLiteV2Plugin(
        FakeContext(),
        {
            "Basic_Settings": {"operation_mode": "active"},
            "Silence_Bridge_Settings": {"bridge_polite_silence": True},
        },
    )
    user_id = "test:FriendMessage:private"
    instance = SimpleNamespace(
        config={
            "trigger_percent": 30,
            "min_ignore_minutes": 15,
            "max_ignore_minutes": 120,
        }
    )
    event = FakeEvent("silence-message")
    response = SimpleNamespace(
        role="assistant",
        completion_text='我拒绝。<ignore id="user" duration="5" />',
        tools_call_name=None,
        tools_call_extra_content=None,
    )

    async def run():
        await plugin.initialize()
        plugin.silence_bridge.resolve_polite_silence = lambda: instance
        plugin.reflection_service.enqueue = lambda *_args, **_kwargs: False
        key = plugin._interaction_key(event, user_id)
        assert plugin.storage.claim_interaction(key, user_id, "你好")
        await plugin.capture_llm_response(event, response)
        state = await plugin._load_state(user_id)
        messages = plugin.storage.get_recent_messages(user_id)
        await plugin.terminate()
        return state, messages

    state, messages = asyncio.run(run())
    assert state.silence_count == 1
    assert state.last_silence_event["target_id"] == "user"
    assert state.last_silence_event["duration_minutes"] == 15
    assert all(
        "<ignore" not in str(item.get("content") or "") for item in messages
    ), "入库文本不应包含拒答标签"


def test_per_umo_toggle_requires_existing_state_and_survives_reset(tmp_path, monkeypatch):
    monkeypatch.setattr(main_impl, "get_astrbot_data_path", lambda: str(tmp_path))
    plugin = main_impl.CompanionLiteV2Plugin(FakeContext(), {})

    async def run():
        await plugin.initialize()
        unknown = await plugin.webui.set_user_enabled("missing", False)
        await plugin._load_state("existing")
        disabled = await plugin.webui.set_user_enabled("existing", False)
        await plugin.webui.reset_user("existing")
        still_disabled = plugin.storage.is_user_enabled("existing")
        await plugin.terminate()
        return unknown, disabled, still_disabled

    unknown, disabled, still_disabled = asyncio.run(run())
    assert unknown["error"] == "unknown_umo"
    assert disabled["ok"] is True
    assert still_disabled is False


def test_enabled_api_uses_json_body_and_supports_disable_then_enable(tmp_path, monkeypatch):
    class FakeRequest:
        query = {}

        def __init__(self):
            self.body = {}

        async def json(self, _default=None):
            return self.body

    fake_request = FakeRequest()
    monkeypatch.setattr(main_impl, "get_astrbot_data_path", lambda: str(tmp_path))
    monkeypatch.setattr(web_mod, "request", fake_request)
    plugin = main_impl.CompanionLiteV2Plugin(FakeContext(), {})
    user_id = "botname:FriendMessage:100000001"

    async def run():
        await plugin.initialize()
        await plugin._load_state(user_id)
        fake_request.body = {"user_id": user_id, "enabled": False}
        await plugin.webui.api_enabled()
        after_disable = plugin.storage.is_user_enabled(user_id)
        fake_request.body = {"user_id": user_id, "enabled": True}
        await plugin.webui.api_enabled()
        after_enable = plugin.storage.is_user_enabled(user_id)
        await plugin.terminate()
        return after_disable, after_enable

    after_disable, after_enable = asyncio.run(run())
    assert after_disable is False
    assert after_enable is True


def test_legacy_reset_path_decodes_chinese_umo_without_creating_alias(tmp_path, monkeypatch):
    monkeypatch.setattr(main_impl, "get_astrbot_data_path", lambda: str(tmp_path))
    plugin = main_impl.CompanionLiteV2Plugin(FakeContext(), {})
    user_id = "botname:FriendMessage:100000002"
    encoded = "botname%3AFriendMessage%3A100000002"
    double_encoded = "botname%253AFriendMessage%253A100000002"

    async def run():
        await plugin.initialize()
        await plugin._load_state(user_id)
        await plugin.webui.api_reset_path(double_encoded)
        states = {item["user_id"] for item in plugin.storage.list_states()}
        await plugin.terminate()
        return states

    states = asyncio.run(run())
    assert states == {user_id}
    assert encoded not in states
    assert double_encoded not in states


def test_reset_rejects_unknown_umo_instead_of_creating_state(tmp_path, monkeypatch):
    class FakeRequest:
        query = {}

        async def json(self, _default=None):
            return {"user_id": "botname%3Aunknown"}

    monkeypatch.setattr(main_impl, "get_astrbot_data_path", lambda: str(tmp_path))
    monkeypatch.setattr(web_mod, "request", FakeRequest())
    plugin = main_impl.CompanionLiteV2Plugin(FakeContext(), {})

    async def run():
        await plugin.initialize()
        await plugin.webui.api_reset()
        states = plugin.storage.list_states()
        await plugin.terminate()
        return states

    assert asyncio.run(run()) == []


def test_normal_messages_never_call_severe_confirmation(tmp_path, monkeypatch):
    monkeypatch.setattr(main_impl, "get_astrbot_data_path", lambda: str(tmp_path))
    plugin = main_impl.CompanionLiteV2Plugin(FakeContext(), {})
    called = 0

    async def analyze(*_args, **_kwargs):
        nonlocal called
        called += 1
        raise AssertionError("normal messages must not call the LLM")

    async def run():
        await plugin.initialize()
        plugin.reflection.analyze_severe = analyze
        for index in range(100):
            await plugin._run_severe_precheck(
                "test:private:user",
                f"普通问题 {index}：请给我一个简短结论",
            )
        state = await plugin._load_state("test:private:user")
        await plugin.terminate()
        return state

    state = asyncio.run(run())
    assert called == 0
    assert state.last_precheck_trace["gate_hit"] is False


def test_severe_confirmation_deduplicates_and_limits_each_window(tmp_path, monkeypatch):
    monkeypatch.setattr(main_impl, "get_astrbot_data_path", lambda: str(tmp_path))
    plugin = main_impl.CompanionLiteV2Plugin(FakeContext(), {})
    called = 0

    async def analyze(*_args, **_kwargs):
        nonlocal called
        called += 1
        return ReflectionOutcome(
            SevereEvidence(
                signal="none",
                severity="none",
                confidence="high",
                evidence="转述或误命中",
            ),
            {
                "kind": "severe",
                "prompt_version": "test",
                "model_tags": {"signal": "none"},
            },
        )

    async def run():
        await plugin.initialize()
        plugin.reflection.analyze_severe = analyze
        user_id = "test:private:user"
        await plugin._run_severe_precheck(user_id, "你就是垃圾")
        await plugin._run_severe_precheck(user_id, "你就是垃圾")
        duplicate = await plugin._load_state(user_id)
        await plugin._run_severe_precheck(user_id, "你只是个奴隶")
        await plugin._run_severe_precheck(user_id, "你这个蠢货助手")
        limited = await plugin._load_state(user_id)
        await plugin.terminate()
        return duplicate, limited

    duplicate, limited = asyncio.run(run())
    assert called == 2
    assert duplicate.last_precheck_trace["skip_reason"] == ("duplicate_message")
    assert limited.last_precheck_trace["skip_reason"] == "window_limit"


def test_existing_severe_issue_skips_repeat_confirmation(tmp_path, monkeypatch):
    monkeypatch.setattr(main_impl, "get_astrbot_data_path", lambda: str(tmp_path))
    plugin = main_impl.CompanionLiteV2Plugin(FakeContext(), {})
    called = 0

    async def analyze(*_args, **_kwargs):
        nonlocal called
        called += 1
        return ReflectionOutcome(None, {})

    async def run():
        await plugin.initialize()
        plugin.reflection.analyze_severe = analyze
        user_id = "test:private:user"
        plugin._save_state(
            RelationshipState(
                user_id,
                posture="guarded",
                active_issue={
                    "kind": "degradation",
                    "phase": "noticed",
                },
            )
        )
        await plugin._run_severe_precheck(user_id, "你这个垃圾助手")
        state = await plugin._load_state(user_id)
        await plugin.terminate()
        return state

    state = asyncio.run(run())
    assert called == 0
    assert state.last_precheck_trace["skip_reason"] == ("existing_severe_issue")


def test_concurrent_deep_reflection_applies_the_round_only_once(tmp_path, monkeypatch):
    monkeypatch.setattr(main_impl, "get_astrbot_data_path", lambda: str(tmp_path))
    plugin = main_impl.CompanionLiteV2Plugin(FakeContext(), {})
    user_id = "test:private:user"
    entered = asyncio.Event()
    release = asyncio.Event()
    calls = 0

    async def deep(
        _state,
        _messages,
        persona_prompt="",
        relationship_role="unbound",
    ):
        nonlocal calls
        calls += 1
        entered.set()
        await release.wait()
        return ReflectionOutcome(
            DeepEvidence(
                pattern="one_sided",
                confidence="high",
                agent_expression="absent",
                user_response_to_expression="not_applicable",
                relationship_summary="六轮互动持续单向",
                impression_operation="revise",
                impression="我对这段单向互动有些不悦。",
            ),
            {
                "kind": "deep",
                "prompt_version": "test-deep",
                "model_tags": {"pattern": "one_sided"},
            },
        )

    async def run():
        await plugin.initialize()
        for round_number in range(1, 7):
            key = f"k{round_number}"
            plugin.storage.claim_interaction(key, user_id, f"question-{round_number}")
            plugin.storage.complete_interaction(key, user_id, f"answer-{round_number}", round_number)
        plugin._save_state(RelationshipState(user_id=user_id, round_sequence=6))
        plugin.reflection.analyze_deep = deep
        first = asyncio.create_task(plugin.reflection_service.perform(user_id, 6, "deep"))
        await entered.wait()
        second = asyncio.create_task(plugin.reflection_service.perform(user_id, 6, "deep"))
        await asyncio.sleep(0)
        release.set()
        results = await asyncio.gather(first, second)
        state = await plugin._load_state(user_id)
        await plugin.terminate()
        return results, state

    results, state = asyncio.run(run())
    assert results == [True, True]
    assert calls == 1
    assert (state.familiarity, state.trust, state.affinity) == (
        2,
        48,
        -4,
    )
    assert state.last_deep_round == 6


def test_bonded_rebuild_is_rejected_before_any_analysis_call(tmp_path, monkeypatch):
    monkeypatch.setattr(main_impl, "get_astrbot_data_path", lambda: str(tmp_path))
    plugin = main_impl.CompanionLiteV2Plugin(FakeContext(), {})
    user_id = "botname:FriendMessage:100000001"
    calls = 0

    async def resolve(_user_id):
        return PersonaResolution(persona_id="persona-name", source="default")

    async def must_not_run(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        raise AssertionError("bonded rebuild must fail before LLM calls")

    async def run():
        await plugin.initialize()
        for round_number in range(1, 7):
            key = f"k{round_number}"
            plugin.storage.claim_interaction(key, user_id, f"question-{round_number}")
            plugin.storage.complete_interaction(key, user_id, f"answer-{round_number}", round_number)
        original = RelationshipState(
            user_id,
            familiarity=35,
            trust=20,
            affinity=-30,
            round_sequence=6,
        )
        plugin._save_state(original)
        plugin.storage.bind_persona("persona-name", user_id)
        plugin.persona.resolve_persona_id = resolve
        plugin.reflection.analyze_light = must_not_run
        plugin.reflection.analyze_deep = must_not_run
        before_messages = plugin.storage.get_recent_messages(user_id, 20)
        result = await plugin.webui.rebuild_profile(user_id)
        persisted = await plugin._load_state(user_id)
        after_messages = plugin.storage.get_recent_messages(user_id, 20)
        await plugin.terminate()
        return result, persisted, before_messages, after_messages

    result, persisted, before_messages, after_messages = asyncio.run(run())
    assert result == (False, "bonded_rebuild_forbidden", None)
    assert calls == 0
    assert (persisted.familiarity, persisted.trust, persisted.affinity) == (
        35,
        20,
        -30,
    )
    assert before_messages == after_messages


def test_observe_rebuild_preserves_messages_and_replaces_profile_atomically(tmp_path, monkeypatch):
    monkeypatch.setattr(main_impl, "get_astrbot_data_path", lambda: str(tmp_path))
    plugin = main_impl.CompanionLiteV2Plugin(FakeContext(), {})
    user_id = "botname:FriendMessage:100000001"

    async def light(
        _state,
        _messages,
        source_round,
        relationship_role="unbound",
    ):
        return ReflectionOutcome(
            LightEvidence(
                "one_sided",
                "medium",
                f"第{source_round}轮仍只索取答案",
            ),
            {
                "kind": "light",
                "prompt_version": "test-light",
                "model_tags": {"signal": "one_sided"},
            },
        )

    async def deep(
        _state,
        _messages,
        persona_prompt="",
        relationship_role="unbound",
    ):
        return ReflectionOutcome(
            DeepEvidence(
                pattern="one_sided",
                confidence="high",
                agent_expression="absent",
                user_response_to_expression="not_applicable",
                relationship_summary="六轮均未承接投入",
                impression_operation="revise",
                impression="我觉得这段互动一直偏单向。",
            ),
            {
                "kind": "deep",
                "prompt_version": "test-deep",
                "prompt_chars": 321,
                "usage": {
                    "input_other": 10,
                    "input_cached": 90,
                    "output": 20,
                    "cache_ratio": 0.9,
                },
                "model_tags": {
                    "pattern": "one_sided",
                    "agent_expression": "absent",
                },
            },
        )

    async def run():
        await plugin.initialize()
        for round_number in range(1, 7):
            key = f"k{round_number}"
            plugin.storage.claim_interaction(key, user_id, f"question-{round_number}")
            plugin.storage.complete_interaction(key, user_id, f"answer-{round_number}", round_number)
        plugin._save_state(
            RelationshipState(
                user_id,
                familiarity=80,
                trust=90,
                affinity=70,
                round_sequence=6,
            )
        )
        before_messages = plugin.storage.get_recent_messages(user_id, 20)
        plugin.reflection.analyze_light = light
        plugin.reflection.analyze_deep = deep
        ok, error, rebuilt = await plugin.webui.rebuild_profile(user_id)
        after_messages = plugin.storage.get_recent_messages(user_id, 20)
        await plugin.terminate()
        return ok, error, rebuilt, before_messages, after_messages

    ok, error, rebuilt, before_messages, after_messages = asyncio.run(run())
    assert ok and error == ""
    assert rebuilt
    assert (rebuilt.familiarity, rebuilt.trust, rebuilt.affinity) == (
        2,
        48,
        -4,
    )
    assert rebuilt.posture == "reserved"
    assert rebuilt.active_issue
    assert (rebuilt.active_issue.kind, rebuilt.active_issue.phase) == (
        "one_sided",
        "noticed",
    )
    assert before_messages == after_messages
    assert rebuilt.last_analysis_trace["operation_version"] == "rebuild-v1"
    assert rebuilt.last_analysis_trace["prompt_version"] == "test-deep"
    assert rebuilt.last_analysis_trace["prompt_chars"] == 321
    assert rebuilt.last_analysis_trace["usage"]["input_cached"] == 90


def test_rebuild_failure_keeps_original_profile(tmp_path, monkeypatch):
    monkeypatch.setattr(main_impl, "get_astrbot_data_path", lambda: str(tmp_path))
    plugin = main_impl.CompanionLiteV2Plugin(FakeContext(), {})
    user_id = "test:private:user"

    async def invalid(*_args, **_kwargs):
        return ReflectionOutcome(None, {"error": "invalid"})

    async def run():
        await plugin.initialize()
        for round_number in range(1, 3):
            key = f"k{round_number}"
            plugin.storage.claim_interaction(key, user_id, "q")
            plugin.storage.complete_interaction(key, user_id, "a", round_number)
        original = RelationshipState(
            user_id,
            familiarity=30,
            trust=75,
            affinity=20,
            round_sequence=2,
        )
        plugin._save_state(original)
        plugin.reflection.analyze_light = invalid
        ok, _, _ = await plugin.webui.rebuild_profile(user_id)
        persisted = await plugin._load_state(user_id)
        await plugin.terminate()
        return ok, persisted

    ok, persisted = asyncio.run(run())
    assert not ok
    assert persisted.familiarity == 30
    assert persisted.trust == 75
    assert persisted.affinity == 20
