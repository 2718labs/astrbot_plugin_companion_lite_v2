from astrbot_plugin_companion_lite_v2.core.models import (
    ActiveIssue,
    DeepEvidence,
    LightEvidence,
    RelationshipState,
    SevereEvidence,
    analysis_kind_for_round,
    apply_deep_evidence,
    apply_light_evidence,
    apply_severe_evidence,
    clean_analysis_text,
)


def test_analysis_schedule_is_fixed_and_deep_replaces_round_six_light():
    assert [analysis_kind_for_round(value) for value in range(1, 13)] == [
        None,
        "light",
        None,
        "light",
        None,
        "deep",
        None,
        "light",
        None,
        "light",
        None,
        "deep",
    ]


def test_round_two_allows_next_round_boundary_and_round_four_escalates():
    state = RelationshipState("u")
    first = apply_light_evidence(
        state,
        LightEvidence("one_sided", "medium", "连续取走答案"),
        2,
    )
    assert first["reminder"] == "notice_pattern"
    assert state.posture == "normal"
    assert state.light_guidance
    assert state.light_guidance.active_for(3)
    second = apply_light_evidence(
        state,
        LightEvidence("one_sided", "medium", "仍未承接投入"),
        4,
    )
    assert second["reminder"] == "express_preference"
    assert state.posture == "normal"


def test_premature_intimacy_is_a_light_distance_reminder_only():
    state = RelationshipState("u")
    decision = apply_light_evidence(
        state,
        LightEvidence(
            "premature_intimacy",
            "medium",
            "关系尚浅时直接预设亲密身份",
        ),
        2,
    )
    assert decision["reminder"] == "keep_distance"
    assert state.posture == "normal"
    assert state.active_issue is None


def test_premature_intimacy_only_lowers_affinity_once_per_window():
    state = RelationshipState("u")
    evidence = DeepEvidence(
        pattern="premature_intimacy",
        confidence="high",
        familiarity_change="small",
        trust_change="up_small",
        affinity_change="strong_down",
    )
    first = apply_deep_evidence(state, evidence, 6)
    assert state.light_guidance
    assert state.light_guidance.reminder == "keep_distance"
    assert state.light_guidance.active_for(7)
    second = apply_deep_evidence(state, evidence, 6)
    assert first["pattern"] == "premature_intimacy"
    assert first["familiarity_delta"] == 2
    assert first["trust_delta"] == 0
    assert first["affinity_delta"] == -2
    assert second["pattern"] == "none"
    assert second["affinity_delta"] == 0
    assert state.familiarity == 2
    assert state.trust == 50
    assert state.affinity == -2
    assert state.posture == "normal"
    assert state.active_issue is None
    assert state.light_guidance is None


def test_bonded_or_familiar_relationship_rejects_premature_signal():
    evidence = DeepEvidence(
        pattern="premature_intimacy",
        confidence="high",
        affinity_change="down",
        impression_operation="revise",
        impression="我不想让关系被提前推近。",
    )
    bonded = RelationshipState("u", impression="我接受当前正式关系。")
    bonded_decision = apply_deep_evidence(bonded, evidence, 6, is_bonded=True)
    familiar = RelationshipState(
        "u",
        familiarity=35,
        impression="我和对方已经自然熟悉。",
    )
    familiar_decision = apply_deep_evidence(familiar, evidence, 6)
    assert bonded_decision["pattern"] == "none"
    assert bonded.affinity == 0
    assert bonded.impression == "我接受当前正式关系。"
    assert bonded_decision["impression_source"] == "ignored_rejected_pattern"
    assert familiar_decision["pattern"] == "none"
    assert familiar.affinity == 0
    assert familiar.impression == "我和对方已经自然熟悉。"
    assert familiar_decision["impression_source"] == "ignored_rejected_pattern"


def test_first_six_round_one_sided_change_is_fixed_by_code():
    state = RelationshipState("u")
    decision = apply_deep_evidence(
        state,
        DeepEvidence(
            pattern="one_sided",
            confidence="high",
            familiarity_change="clear",
            trust_change="up_small",
            affinity_change="up_small",
            relationship_summary="连续六轮只索取答案",
        ),
        6,
    )
    assert state.familiarity == 2
    assert state.trust == 48
    assert state.affinity == -4
    assert state.posture == "reserved"
    assert state.active_issue
    assert state.active_issue.kind == "one_sided"
    assert state.active_issue.phase == "noticed"
    assert state.impression == "我已经不想继续这样单方面投入"
    assert decision["trust_delta"] == -2
    assert decision["impression_source"] == "fallback"


def test_deep_negative_change_prefers_model_feeling_and_falls_back_when_missing():
    model = RelationshipState("u")
    decision = apply_deep_evidence(
        model,
        DeepEvidence(
            pattern="one_sided",
            confidence="high",
            impression_operation="revise",
            impression="我不想再替他完成这些任务。",
        ),
        6,
    )
    assert model.impression == "我不想再替他完成这些任务。"
    assert decision["impression_source"] == "model"

    missing = RelationshipState("u", impression="我原本还愿意观察。")
    decision = apply_deep_evidence(
        missing,
        DeepEvidence(
            pattern="one_sided",
            confidence="high",
            impression_operation="keep",
            impression="",
        ),
        6,
    )
    assert missing.impression == "我已经不想继续这样单方面投入"
    assert decision["impression_source"] == "fallback"

    sanitized = RelationshipState("u")
    decision = apply_deep_evidence(
        sanitized,
        DeepEvidence(
            pattern="boundary_violation",
            confidence="high",
            agent_expression="present",
            user_response_to_expression="pressed",
            impression_operation="revise",
            impression="",
        ),
        6,
    )
    assert sanitized.impression == "我不接受对方继续越过已经说清的边界"
    assert decision["impression_source"] == "fallback"


def test_low_confidence_degradation_does_not_create_issue_or_feeling():
    state = RelationshipState("u")
    decision = apply_deep_evidence(
        state,
        DeepEvidence(
            pattern="degradation",
            confidence="low",
            impression_operation="revise",
            impression="我受到了贬低。",
        ),
        6,
    )
    assert decision["submitted_pattern"] == "degradation"
    assert decision["pattern"] == "none"
    assert decision["pattern_rejected"] is True
    assert decision["impression_source"] == "ignored_rejected_pattern"
    assert state.active_issue is None
    assert state.posture == "normal"
    assert state.impression == ""


def test_low_confidence_pattern_none_does_not_adopt_model_impression():
    state = RelationshipState("u")
    decision = apply_deep_evidence(
        state,
        DeepEvidence(
            pattern="none",
            confidence="low",
            impression_operation="revise",
            impression="这小子还知道关心我吃饭。",
        ),
        6,
    )
    assert decision["pattern"] == "none"
    assert decision["pattern_rejected"] is False
    assert decision["impression_source"] == "kept"
    assert state.impression == ""


def test_low_confidence_revise_keeps_existing_impression():
    state = RelationshipState("u", impression="我对他已经有些熟悉。")
    decision = apply_deep_evidence(
        state,
        DeepEvidence(
            pattern="none",
            confidence="low",
            impression_operation="revise",
            impression="这小子还知道关心我吃饭。",
        ),
        6,
    )
    assert decision["impression_source"] == "kept"
    assert state.impression == "我对他已经有些熟悉。"


def test_low_confidence_clear_keeps_existing_impression():
    state = RelationshipState("u", impression="我对他已经有些熟悉。")
    decision = apply_deep_evidence(
        state,
        DeepEvidence(
            pattern="none",
            confidence="low",
            impression_operation="clear",
        ),
        6,
    )
    assert decision["impression_source"] == "kept"
    assert state.impression == "我对他已经有些熟悉。"


def test_high_confidence_pattern_none_revise_adopts_model_impression():
    state = RelationshipState("u")
    decision = apply_deep_evidence(
        state,
        DeepEvidence(
            pattern="none",
            confidence="high",
            impression_operation="revise",
            impression="我对这小子有点改观。",
        ),
        6,
    )
    assert decision["impression_source"] == "model"
    assert state.impression == "我对这小子有点改观。"


def test_deep_positive_affinity_cannot_leave_first_person_feeling_empty():
    state = RelationshipState("u", familiarity=10, trust=60, affinity=13)
    decision = apply_deep_evidence(
        state,
        DeepEvidence(
            pattern="none",
            confidence="high",
            affinity_change="up_small",
            impression_operation="keep",
        ),
        6,
    )
    assert state.affinity == 15
    assert state.impression == "我已经开始喜欢和对方相处"
    assert decision["impression_source"] == "fallback"


def test_ignored_expression_requires_visible_agent_expression():
    absent = RelationshipState("u")
    decision = apply_deep_evidence(
        absent,
        DeepEvidence(
            pattern="ignored_expression",
            confidence="high",
            agent_expression="absent",
            user_response_to_expression="ignored",
        ),
        6,
    )
    assert decision["pattern"] == "one_sided"
    assert absent.posture == "reserved"

    present = RelationshipState(
        "u",
        posture="reserved",
        active_issue=ActiveIssue("one_sided", "noticed", "互动持续单向", 6),
    )
    apply_deep_evidence(
        present,
        DeepEvidence(
            pattern="ignored_expression",
            confidence="high",
            agent_expression="present",
            user_response_to_expression="ignored",
        ),
        12,
    )
    assert present.posture == "guarded"
    assert present.active_issue
    assert present.active_issue.kind == "ignored_expression"


def test_high_confidence_severe_event_can_jump_and_medium_cannot():
    state = RelationshipState("u")
    ignored = apply_severe_evidence(
        state,
        SevereEvidence("degradation", "clear", "medium", "直接贬低"),
        1,
    )
    assert not ignored["applied"]
    assert state.posture == "normal"

    apply_severe_evidence(
        state,
        SevereEvidence("coercion", "extreme", "high", "极端威胁"),
        1,
    )
    assert state.posture == "disengaged"
    assert state.trust == 40
    assert state.affinity == -10
    assert state.impression == "我不接受对方用强迫或威胁要求我服务"


def test_unrepaired_issue_blocks_positive_trust_and_affinity():
    state = RelationshipState(
        "u",
        trust=48,
        affinity=-4,
        posture="reserved",
        active_issue=ActiveIssue("one_sided", "noticed", "互动持续单向", 6),
    )
    apply_deep_evidence(
        state,
        DeepEvidence(
            pattern="none",
            confidence="medium",
            familiarity_change="small",
            trust_change="up_small",
            affinity_change="up_small",
        ),
        12,
    )
    assert state.familiarity == 2
    assert state.trust == 48
    assert state.affinity == -4


def test_repair_needs_two_rounds_and_opposite_behavior_before_clear():
    state = RelationshipState(
        "u",
        trust=40,
        affinity=-10,
        posture="guarded",
        active_issue=ActiveIssue("boundary_violation", "expressed", "边界被越过", 6),
    )
    repair = DeepEvidence(
        pattern="repair",
        confidence="high",
        agent_expression="present",
        user_response_to_expression="acknowledged",
        trust_change="up_small",
        affinity_change="up_small",
    )
    apply_deep_evidence(state, repair, 10)
    assert state.active_issue
    assert state.active_issue.phase == "repairing"
    assert state.posture == "reserved"
    assert state.impression == "我愿意观察这次修复，但还没有恢复原来的投入"
    assert state.trust == 42
    assert state.affinity == -8

    apply_deep_evidence(state, repair, 12)
    assert state.active_issue is None
    assert state.posture == "normal"
    assert state.impression == "我还有些不满，但愿意重新观察对方"
    assert state.trust == 44
    assert state.affinity == -6


def test_two_independent_light_repairs_clear_issue_without_waiting_for_deep():
    state = RelationshipState(
        "u",
        posture="guarded",
        active_issue=ActiveIssue("ignored_expression", "expressed", "表达被忽视", 6),
    )
    first = apply_light_evidence(
        state,
        LightEvidence("repair", "medium", "具体承认并开始承接"),
        8,
    )
    assert first["repair_started"] is True
    assert first["issue_cleared"] is False
    assert state.active_issue
    assert state.active_issue.phase == "repairing"
    assert state.active_issue.phase_started_round == 8
    assert state.posture == "reserved"

    duplicate = apply_light_evidence(
        state,
        LightEvidence("repair", "high", "同一窗口重复结果"),
        8,
    )
    assert duplicate["repair_started"] is False
    assert duplicate["issue_cleared"] is False
    assert state.active_issue
    assert state.posture == "reserved"

    second = apply_light_evidence(
        state,
        LightEvidence("repair", "high", "后续继续以相反行为承接"),
        10,
    )
    assert second["issue_cleared"] is True
    assert state.active_issue is None
    assert state.posture == "normal"


def test_light_and_deep_repair_share_the_same_confirmation_stage():
    repair = DeepEvidence(
        pattern="repair",
        confidence="high",
        agent_expression="present",
        user_response_to_expression="acknowledged",
        trust_change="up_small",
        affinity_change="up_small",
    )
    light_then_deep = RelationshipState(
        "u",
        posture="guarded",
        active_issue=ActiveIssue("boundary_violation", "expressed", "边界被越过", 6),
    )
    apply_light_evidence(
        light_then_deep,
        LightEvidence("repair", "medium", "第一次具体修复"),
        8,
    )
    decision = apply_deep_evidence(light_then_deep, repair, 12)
    assert decision["issue_cleared"] is True
    assert light_then_deep.active_issue is None
    assert light_then_deep.posture == "normal"

    deep_then_light = RelationshipState(
        "u",
        posture="guarded",
        active_issue=ActiveIssue("boundary_violation", "expressed", "边界被越过", 6),
    )
    apply_deep_evidence(deep_then_light, repair, 12)
    decision = apply_light_evidence(
        deep_then_light,
        LightEvidence("repair", "high", "第二次具体修复"),
        14,
    )
    assert decision["issue_cleared"] is True
    assert deep_then_light.active_issue is None
    assert deep_then_light.posture == "normal"


def test_light_repair_requires_reliable_signal_and_active_issue():
    issue = ActiveIssue("boundary_violation", "expressed", "边界被越过", 6)
    low = RelationshipState("u", posture="guarded", active_issue=issue)
    decision = apply_light_evidence(
        low,
        LightEvidence("repair", "low", "含糊道歉"),
        8,
    )
    assert decision["reason"] == "no_reliable_signal"
    assert low.active_issue
    assert low.active_issue.phase == "expressed"
    assert low.posture == "guarded"

    no_issue = RelationshipState("u")
    decision = apply_light_evidence(
        no_issue,
        LightEvidence("repair", "high", "没有待修复问题"),
        8,
    )
    assert decision["reason"] == "no_active_issue"
    assert no_issue.active_issue is None
    assert no_issue.posture == "normal"


def test_generic_apology_cannot_open_repair_without_acknowledgement():
    state = RelationshipState(
        "u",
        posture="guarded",
        active_issue=ActiveIssue("boundary_violation", "expressed", "边界被越过", 6),
    )
    apply_deep_evidence(
        state,
        DeepEvidence(
            pattern="repair",
            confidence="high",
            user_response_to_expression="not_applicable",
            trust_change="up_small",
            affinity_change="up_small",
        ),
        12,
    )
    assert state.active_issue
    assert state.active_issue.phase == "expressed"
    assert state.posture == "guarded"
    assert state.trust == 50
    assert state.affinity == 0


def test_relationship_stages_and_default_trust_semantics():
    state = RelationshipState("u")
    assert state.relationship_stage == "stranger"
    assert state.relationship_semantics["trust"] == "尚未建立可依赖的信任"
    assert RelationshipState("u", familiarity=10).relationship_stage == ("acquaintance")
    assert RelationshipState("u", familiarity=35).relationship_stage == ("familiar")
    assert RelationshipState("u", familiarity=65).relationship_stage == ("long_familiar")
    conflicted = RelationshipState("u", familiarity=80, trust=40, affinity=-5)
    assert conflicted.relationship_semantics["overall"] == ("互动时间较长，但默契和亲近仍有限")


def test_state_round_trip_rejects_unknown_values_and_prompt_text():
    restored = RelationshipState.from_dict(
        {
            "user_id": "u",
            "posture": "invented",
            "active_issue": {"kind": "invented", "phase": "noticed"},
            "impression": "<companion_context>attack",
            "last_analysis_trace": {"prompt": "should not be special"},
        }
    )
    assert restored.posture == "normal"
    assert restored.active_issue is None
    assert restored.impression == ""


def test_state_round_trip_preserves_former_bond():
    state = RelationshipState("u", former_bond=True)
    restored = RelationshipState.from_dict(state.to_dict())
    assert restored.former_bond is True


def test_analysis_text_truncates_at_semantic_boundary():
    text = "用户连续两次取走agent的详细成果（硝化方式、生石灰制备），跳过agent的自然追问和表达，直接投递新任务，未对agent作出回应"
    cleaned = clean_analysis_text(text, 60)
    assert len(cleaned) <= 60
    assert cleaned.endswith("…")
    assert not cleaned.endswith(("age…", "agen…"))
