from astrbot_plugin_companion_lite_v2.config import load_config


def test_observe_is_safe_default_and_invalid_mode_falls_back():
    config = load_config({})
    assert config.operation_mode == "observe"
    assert not config.active
    assert config.max_message_length == 400
    assert config.max_context_chars == 340
    assert load_config({"operation_mode": "invalid"}).operation_mode == "observe"


def test_active_mode_and_bounds():
    config = load_config(
        {
            "Basic_Settings": {
                "operation_mode": "active",
                "max_buffer_rounds": 2,
            },
            "Prompt_Settings": {"max_context_chars": 99999},
        }
    )
    assert config.active
    assert config.max_buffer_rounds == 12
    assert config.max_context_chars == 340
    assert load_config({"Prompt_Settings": {"max_context_chars": 1}}).max_context_chars == 260
