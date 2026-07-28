import sqlite3
from concurrent.futures import ThreadPoolExecutor

from astrbot_plugin_companion_lite_v2.core.storage import Storage


def test_storage_creates_isolated_domain_tables(tmp_path):
    path = tmp_path / "companion_lite_v2.db"
    storage = Storage(str(path))
    storage.close()
    connection = sqlite3.connect(path)
    try:
        tables = {
            row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")
        }
    finally:
        connection.close()
    assert tables == {
        "companion_state",
        "companion_bond",
        "message_buffer",
        "pending_interaction",
        "companion_umo_settings",
    }


def test_interaction_pair_is_exactly_once(tmp_path):
    storage = Storage(str(tmp_path / "pairs.db"))
    try:
        assert storage.claim_interaction("k", "u", "hello")
        assert not storage.claim_interaction("k", "u", "duplicate")
        assert not storage.complete_interaction("k", "other", "wrong", 1)
        assert storage.complete_interaction("k", "u", "reply", 1)
        assert not storage.complete_interaction("k", "u", "again", 1)
        messages = storage.get_completed_rounds("u", 2)
        assert [(item["role"], item["content"]) for item in messages] == [
            ("user", "hello"),
            ("assistant", "reply"),
        ]
    finally:
        storage.close()


def test_concurrent_claim_has_one_winner(tmp_path):
    storage = Storage(str(tmp_path / "concurrent.db"))
    try:
        with ThreadPoolExecutor(max_workers=8) as executor:
            results = list(
                executor.map(
                    lambda _: storage.claim_interaction("same", "u", "hello"),
                    range(20),
                )
            )
        assert results.count(True) == 1
    finally:
        storage.close()


def test_persona_bond_is_unique_idempotent_and_private(tmp_path):
    storage = Storage(str(tmp_path / "bond.db"))
    try:
        first = storage.bind_persona("persona-name", "bot:FriendMessage:1")
        same = storage.bind_persona("persona-name", "bot:FriendMessage:1")
        occupied = storage.bind_persona("persona-name", "bot:FriendMessage:2")
        assert first["status"] == "bound"
        assert same["status"] == "already_bound"
        assert occupied["status"] == "occupied"
        assert storage.get_bond("persona-name")["user_id"] == ("bot:FriendMessage:1")
        assert not storage.unbind_persona("persona-name", "bot:FriendMessage:2")
        assert storage.unbind_persona("persona-name", "bot:FriendMessage:1")
        assert storage.get_bond("persona-name") is None
    finally:
        storage.close()


def test_concurrent_persona_bind_has_one_new_winner(tmp_path):
    storage = Storage(str(tmp_path / "bond-concurrent.db"))
    try:
        users = [f"bot:FriendMessage:{index}" for index in range(20)]
        with ThreadPoolExecutor(max_workers=8) as executor:
            results = list(
                executor.map(
                    lambda user: storage.bind_persona("persona-name", user),
                    users,
                )
            )
        assert [item["status"] for item in results].count("bound") == 1
        assert [item["status"] for item in results].count("occupied") == 19
    finally:
        storage.close()


def test_trim_keeps_pending_and_latest_completed_rounds(tmp_path):
    storage = Storage(str(tmp_path / "trim.db"))
    try:
        for round_number in range(1, 5):
            key = f"k{round_number}"
            assert storage.claim_interaction(key, "u", f"u{round_number}")
            assert storage.complete_interaction(key, "u", f"a{round_number}", round_number)
        assert storage.claim_interaction("pending", "u", "half")
        assert storage.trim_completed_rounds("u", 2) == 2
        contents = [item["content"] for item in storage.get_recent_messages("u", 20)]
        assert contents == ["u3", "a3", "u4", "a4", "half"]
    finally:
        storage.close()


def test_reset_is_scoped_to_v2_user(tmp_path):
    storage = Storage(str(tmp_path / "reset.db"))
    try:
        storage.save_state("u1", {"user_id": "u1"})
        storage.save_state("u2", {"user_id": "u2"})
        storage.set_user_enabled("u1", False)
        storage.bind_persona("persona-name", "u1")
        storage.claim_interaction("u1-k", "u1", "hello")
        storage.reset_user("u1")
        assert storage.get_state("u1") is None
        assert storage.get_state("u2") == {"user_id": "u2"}
        assert storage.get_pending_interaction("u1-k") is None
        assert storage.get_recent_messages("u1") == []
        assert storage.get_bond("persona-name") is None
        assert storage.is_user_enabled("u1") is False
    finally:
        storage.close()


def test_per_umo_enabled_setting_persists_and_clears_pending(tmp_path):
    path = tmp_path / "enabled.db"
    storage = Storage(str(path))
    storage.save_state("u", {"user_id": "u"})
    assert storage.is_user_enabled("u") is True
    assert storage.claim_interaction("pending", "u", "hello")
    storage.set_user_enabled("u", False)
    assert storage.is_user_enabled("u") is False
    assert storage.get_pending_interaction("pending") is None
    assert storage.get_recent_messages("u") == []
    storage.close()

    reopened = Storage(str(path))
    try:
        assert reopened.is_user_enabled("u") is False
        reopened.set_user_enabled("u", True)
        assert reopened.is_user_enabled("u") is True
    finally:
        reopened.close()


def test_deep_window_uses_latest_twenty_completed_messages_at_target(tmp_path):
    storage = Storage(str(tmp_path / "window.db"))
    try:
        for round_number in range(1, 13):
            key = f"k{round_number}"
            storage.claim_interaction(key, "u", f"u{round_number}")
            storage.complete_interaction(key, "u", f"a{round_number}", round_number)
        storage.claim_interaction("pending", "u", "not completed")
        messages = storage.get_recent_messages("u", 20, up_to_round=12, completed_only=True)
        assert len(messages) == 20
        assert messages[0]["content"] == "u3"
        assert messages[-1]["content"] == "a12"
        assert all(item["completed_round"] > 0 for item in messages)
    finally:
        storage.close()


def test_list_states_keeps_private_relationships_separate(tmp_path):
    storage = Storage(str(tmp_path / "sessions.db"))
    try:
        storage.save_state("p:s1:u", {"user_id": "p:s1:u", "round_sequence": 2})
        storage.save_state("p:s2:u", {"user_id": "p:s2:u", "round_sequence": 8})
        listed = storage.list_states()
        assert {item["user_id"] for item in listed} == {"p:s1:u", "p:s2:u"}
    finally:
        storage.close()


def test_state_revision_and_message_revision_support_atomic_rebuild(tmp_path):
    storage = Storage(str(tmp_path / "revision.db"))
    try:
        storage.save_state("u", {"user_id": "u", "value": 1})
        _, revision = storage.get_state_record("u")
        assert revision is not None
        assert storage.get_message_revision("u") == (0, 0)
        storage.claim_interaction("k", "u", "hello")
        count, max_id = storage.get_message_revision("u")
        assert count == 1
        assert max_id > 0
        assert storage.replace_state_if_revision("u", revision, {"user_id": "u", "value": 2})
        assert not storage.replace_state_if_revision("u", revision, {"user_id": "u", "value": 3})
        assert storage.get_state("u")["value"] == 2
    finally:
        storage.close()
