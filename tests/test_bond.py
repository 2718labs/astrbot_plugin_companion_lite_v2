import asyncio
from types import SimpleNamespace

import astrbot_plugin_companion_lite_v2.main as main_impl
from astrbot_plugin_companion_lite_v2.core.models import (
    ActiveIssue,
    RelationshipState,
)


class FakeServiceProvider:
    def __init__(self, session_configs=None):
        self.session_configs = session_configs or {}

    async def get_async(self, *, scope, scope_id, key, default=None):
        assert scope == "umo"
        assert key == "session_service_config"
        return self.session_configs.get(scope_id, default)


class FakePersonaManager:
    def __init__(self, default_id="persona-name"):
        self.default_id = default_id
        self.personas = {
            "persona-name": {"name": "persona-name"},
            "Other": {"name": "Other"},
        }

    def get_persona_v3_by_id(self, persona_id):
        return self.personas.get(persona_id)

    async def get_default_persona_v3(self, umo=None):
        del umo
        return self.personas.get(self.default_id)


class FakeConversationManager:
    def __init__(self, persona_by_umo=None):
        self.persona_by_umo = persona_by_umo or {}

    async def get_curr_conversation_id(self, umo):
        return "conversation" if umo in self.persona_by_umo else None

    async def get_conversation(self, umo, conversation_id):
        assert conversation_id == "conversation"
        return SimpleNamespace(persona_id=self.persona_by_umo[umo])


class FakeContext:
    def __init__(self, persona_by_umo=None):
        self.routes = []
        self.persona_manager = FakePersonaManager()
        self.conversation_manager = FakeConversationManager(persona_by_umo)

    def register_web_api(self, *args):
        self.routes.append(args)

    def get_provider_by_id(self, _):
        return None

    def get_using_provider(self, _):
        return None


class FakeEvent:
    def __init__(self, umo):
        self.unified_msg_origin = umo
        self.message_obj = SimpleNamespace(
            message_id="command",
            platform_name="botname",
            session_id=umo,
        )

    def get_sender_id(self):
        return self.unified_msg_origin.rsplit(":", 1)[-1]

    def get_platform_name(self):
        return "botname"

    def get_session_id(self):
        return self.unified_msg_origin

    def plain_result(self, text):
        return text


async def collect(command):
    return [item async for item in command]


def make_plugin(tmp_path, monkeypatch, context=None, session_configs=None):
    monkeypatch.setattr(main_impl, "get_astrbot_data_path", lambda: str(tmp_path))
    monkeypatch.setattr(main_impl, "sp", FakeServiceProvider(session_configs))
    return main_impl.CompanionLiteV2Plugin(context or FakeContext(), {})


def test_persona_resolution_matches_livingmemory_priority(tmp_path, monkeypatch):
    override_umo = "botname:FriendMessage:1"
    conversation_umo = "botname:FriendMessage:2"
    default_umo = "botname:FriendMessage:3"
    context = FakeContext({override_umo: "persona-name", conversation_umo: "Other"})
    plugin = make_plugin(
        tmp_path,
        monkeypatch,
        context,
        {override_umo: {"persona_id": "Other"}},
    )

    async def run():
        override = await plugin._resolve_persona_id(override_umo)
        conversation = await plugin._resolve_persona_id(conversation_umo)
        default = await plugin._resolve_persona_id(default_umo)
        await plugin.terminate()
        return override, conversation, default

    override, conversation, default = asyncio.run(run())
    assert (override.persona_id, override.source) == (
        "Other",
        "session_override",
    )
    assert (conversation.persona_id, conversation.source) == (
        "Other",
        "conversation",
    )
    assert (default.persona_id, default.source) == (
        "persona-name",
        "default",
    )


def test_persona_resolution_fails_closed_for_none_and_unknown(tmp_path, monkeypatch):
    none_umo = "botname:FriendMessage:none"
    unknown_umo = "botname:FriendMessage:unknown"
    context = FakeContext({unknown_umo: "Missing"})
    plugin = make_plugin(
        tmp_path,
        monkeypatch,
        context,
        {none_umo: {"persona_id": "[%None]"}},
    )

    async def run():
        explicit_none = await plugin._resolve_persona_id(none_umo)
        unknown = await plugin._resolve_persona_id(unknown_umo)
        await plugin.terminate()
        return explicit_none, unknown

    explicit_none, unknown = asyncio.run(run())
    assert explicit_none.persona_id == ""
    assert explicit_none.error == "persona_missing"
    assert unknown.persona_id == ""
    assert unknown.error == "persona_not_found"


def test_bond_sets_small_preference_floor_without_clearing_issue(tmp_path, monkeypatch):
    umo = "botname:FriendMessage:100000001"
    plugin = make_plugin(tmp_path, monkeypatch)
    state = RelationshipState(
        umo,
        familiarity=2,
        trust=48,
        affinity=-4,
        posture="reserved",
        active_issue=ActiveIssue("one_sided", "noticed", "互动持续单向", 6),
    )
    plugin._save_state(state)

    async def run():
        await plugin.initialize()
        reply = await collect(plugin.cmd_bond(FakeEvent(umo)))
        persisted = await plugin._load_state(umo)
        bond = plugin.storage.get_bond("persona-name")
        await plugin.terminate()
        return reply, persisted, bond

    reply, persisted, bond = asyncio.run(run())
    assert reply == ["行，这个位置给你了。只有一个——以后怎么待我，自己掂量。"]
    assert (
        persisted.familiarity,
        persisted.trust,
        persisted.affinity,
    ) == (35, 60, 15)
    assert persisted.posture == "reserved"
    assert persisted.active_issue
    assert persisted.active_issue.kind == "one_sided"
    assert persisted.impression == "我已经不想继续这样单方面投入"
    assert bond["user_id"] == umo


def test_bond_is_exclusive_idempotent_and_does_not_repair_decline(tmp_path, monkeypatch):
    first_umo = "botname:FriendMessage:1"
    second_umo = "botname:FriendMessage:2"
    plugin = make_plugin(tmp_path, monkeypatch)

    async def run():
        await plugin.initialize()
        first = await collect(plugin.cmd_bond(FakeEvent(first_umo)))
        state = await plugin._load_state(first_umo)
        first_impression = state.impression
        state.trust = 35
        state.affinity = -20
        plugin._save_state(state)
        same = await collect(plugin.cmd_bond(FakeEvent(first_umo)))
        occupied = await collect(plugin.cmd_bond(FakeEvent(second_umo)))
        persisted = await plugin._load_state(first_umo)
        await plugin.terminate()
        return first, same, occupied, persisted, first_impression

    first, same, occupied, persisted, first_impression = asyncio.run(run())
    assert first
    assert first_impression == "我已经开始喜欢和对方相处"
    assert same == ["这个位置本来就是你的，还确认什么。"]
    assert occupied == ["这个位置已经有人了。要换，先在原来的窗口解除。"]
    assert first_umo not in occupied[0]
    assert persisted.trust == 35
    assert persisted.affinity == -20


def test_unbond_preserves_profile_and_rebind_clears_former_without_relift(tmp_path, monkeypatch):
    umo = "botname:FriendMessage:1"
    plugin = make_plugin(tmp_path, monkeypatch)

    async def run():
        await plugin.initialize()
        await collect(plugin.cmd_bond(FakeEvent(umo)))
        state = await plugin._load_state(umo)
        state.familiarity = 72
        state.trust = 41
        state.affinity = -12
        plugin._save_state(state)
        unbond_reply = await collect(plugin.cmd_unbond(FakeEvent(umo)))
        former = await plugin._load_state(umo)
        rebind_reply = await collect(plugin.cmd_bond(FakeEvent(umo)))
        rebound = await plugin._load_state(umo)
        await plugin.terminate()
        return unbond_reply, former, rebind_reply, rebound

    unbond_reply, former, rebind_reply, rebound = asyncio.run(run())
    assert unbond_reply == ["关系名我收回了，发生过的事不清零。以后还是看你怎么待我。"]
    assert former.former_bond is True
    assert (former.familiarity, former.trust, former.affinity) == (
        72,
        41,
        -12,
    )
    assert rebind_reply
    assert rebound.former_bond is False
    assert (rebound.familiarity, rebound.trust, rebound.affinity) == (
        72,
        41,
        -12,
    )
