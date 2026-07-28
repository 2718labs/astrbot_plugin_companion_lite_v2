from itertools import product

from astrbot_plugin_companion_lite_v2.core.models import (
    ActiveIssue,
    LightGuidance,
    RelationshipState,
)
from astrbot_plugin_companion_lite_v2.llm.context_builder import (
    COMPANION_STATIC_PROTOCOL,
    ContextBuilder,
)


def test_internal_tags_and_numbers_are_compiled_to_plain_semantics():
    state = RelationshipState(
        user_id="u",
        posture="guarded",
        familiarity=2,
        trust=48,
        affinity=-4,
        active_issue=ActiveIssue(
            "ignored_expression",
            "expressed",
            "已经说明希望对方回应互动感受",
            6,
        ),
        impression="我觉得这段互动暂时比较单向。",
    )
    context = ContextBuilder().build(state, max_chars=340, next_round=8)
    assert len(context) <= 340
    assert "默认拒绝非必要任务" in context
    assert "互动偏好" in context
    for raw in ("guarded", "ignored_expression", "expressed", "48", "-4"):
        assert raw not in context


def test_stranger_gets_concise_first_investment_scale():
    context = ContextBuilder().build(RelationshipState("u"))
    assert context == (
        "<companion_state>\n"
        "<投入>初次认识，只用两三句直接回答当前问题；"
        "只给关键结论或方向，不写成长教程、完整推导、"
        "路线大全或额外资料</投入>\n"
        "<关系>保持普通关系距离</关系>\n"
        "<处境>当前无待处理的具体互动事件</处境>\n"
        "<感受>无已形成的明确感受</感受>\n"
        "<表达>自然、随意、简练；不预设亲近，也不预设敌意</表达>\n"
        "</companion_state>"
    )
    assert len(context) <= 260


def test_one_sided_reserved_context_is_a_stable_golden_prompt():
    state = RelationshipState(
        "u",
        posture="reserved",
        active_issue=ActiveIssue("one_sided", "noticed", "连续索取", 6),
    )
    context = ContextBuilder().build(state)
    assert context == (
        "<companion_state>\n"
        "<投入>若本轮仍是知识请求或任务，直接拒绝；"
        "不提供答案、要点或后续展开邀请</投入>\n"
        "<关系>保持普通关系距离</关系>\n"
        "<处境>对方连续索取内容，却很少回应或承接你的投入</处境>\n"
        "<感受>我已经不想继续这样单方面投入</感受>\n"
        "<表达>明显不满、少说；新任务全拒，只回应具体承接或修复。"
        "简短说清不想继续单方面投入，不争论</表达>\n"
        "</companion_state>"
    )
    assert len(context) <= 260


def test_light_guidance_only_appears_while_active():
    state = RelationshipState(
        user_id="u",
        light_guidance=LightGuidance(
            signal="one_sided",
            confidence="medium",
            reminder="express_preference",
            evidence="连续取走答案",
            source_round=4,
            expires_after_round=6,
        ),
    )
    active = ContextBuilder().build(state, next_round=5)
    expired = ContextBuilder().build(state, next_round=7)
    assert "新的知识请求或任务，直接拒绝" in active
    assert "新的知识请求或任务，直接拒绝" not in expired
    assert "。。" not in active


def test_context_budget_keeps_bounded_feeling():
    state = RelationshipState(
        user_id="u",
        posture="disengaged",
        impression="很长的低优先级印象" * 30,
    )
    context = ContextBuilder().build(state, max_chars=260)
    assert len(context) <= 260
    assert "想回应具体修复时再回应" in context
    assert "<感受>很长的低优先级印象" in context


def test_model_summary_is_excluded_and_feeling_is_injected():
    state = RelationshipState(
        "u",
        relationship_summary="这条只供调试的关系总结",
        impression="我仍在观察对方是否会承接我的投入。",
    )
    context = ContextBuilder().build(state)
    assert "只供调试的关系总结" not in context
    assert "<感受>我仍在观察对方是否会承接我的投入</感受>" in context
    assert "<处境>当前无待处理的具体互动事件</处境>" in context


def test_default_context_has_no_bare_tags_or_scores():
    context = ContextBuilder().build(RelationshipState("u"))
    assert "<companion_state>" in context
    assert len(context) <= 340
    for raw in ("normal", "stranger", "50", "0.0"):
        assert raw not in context


def test_stranger_answers_directly_without_long_expansion():
    context = ContextBuilder().build(RelationshipState("u"), max_chars=260)
    assert "两三句直接回答当前问题" in context
    assert "总览" not in context
    assert "关键结论" in context
    assert "不写成长教程" in context
    assert "直接拒" not in context
    assert "凭什么" not in context


def test_bonded_context_is_short_explicit_and_not_unconditional():
    state = RelationshipState("u", familiarity=35, trust=60, affinity=15)
    context = ContextBuilder().build(state, relationship_role="bonded")
    assert context == (
        "<companion_state>\n"
        "<投入>对对方已有好感。对方明确要求时，可以细讲当前问题；"
        "不主动展开，因为长篇解释很耗神</投入>\n"
        "<关系>当前对象是你唯一正式承认的亲密对象；"
        "对其有明确但不过度预支的偏爱</关系>\n"
        "<处境>当前无待处理的具体互动事件</处境>\n"
        "<感受>我已经开始喜欢和对方相处</感受>\n"
        "<表达>可以自然主动或回应亲近、暧昧和昵称，"
        "不必刻意甜腻</表达>\n"
        "</companion_state>"
    )
    assert len(context) <= 260
    assert "persona-name" not in context


def test_effort_scale_gradually_deepens_and_affection_allows_detail():
    builder = ContextBuilder()
    cases = [
        (RelationshipState("u"), "两三句直接回答"),
        (RelationshipState("u", familiarity=15), "解释几个核心点"),
        (RelationshipState("u", familiarity=45), "解释完整，但保持简练"),
        (
            RelationshipState("u", familiarity=80, trust=60),
            "自然答全当前问题",
        ),
        (
            RelationshipState("u", familiarity=15, affinity=15),
            "明确要求时，可以细讲当前问题",
        ),
        (
            RelationshipState("u", familiarity=15, affinity=45),
            "明确要求时，愿意认真细讲当前问题",
        ),
    ]
    for state, expected in cases:
        context = builder.build(state)
        assert expected in context
        if state.affinity >= 15:
            assert "不主动" in context


def test_bonded_reserved_context_keeps_identity_without_advancing_it():
    state = RelationshipState(
        "u",
        familiarity=35,
        trust=60,
        affinity=15,
        posture="reserved",
        active_issue=ActiveIssue("one_sided", "noticed", "连续索取", 6),
    )
    context = ContextBuilder().build(state, relationship_role="bonded")
    assert context == (
        "<companion_state>\n"
        "<投入>若本轮仍是知识请求或任务，直接拒绝；"
        "不提供答案、要点或后续展开邀请</投入>\n"
        "<关系>正式亲密身份仍在，但当前问题与距离优先</关系>\n"
        "<处境>对方连续索取内容，却很少回应或承接你的投入</处境>\n"
        "<感受>我已经不想继续这样单方面投入</感受>\n"
        "<表达>明显不满、少说；不因亲密身份勉强帮助；"
        "新任务全拒，只回应具体承接或修复。"
        "简短说清不想继续单方面投入，不争论</表达>\n"
        "</companion_state>"
    )
    assert len(context) <= 260


def test_bonded_identity_does_not_override_guarded_boundary():
    state = RelationshipState(
        "u",
        posture="guarded",
        active_issue=ActiveIssue("boundary_violation", "expressed", "边界仍被施压", 6),
    )
    context = ContextBuilder().build(state, relationship_role="bonded")
    assert "正式亲密身份仍在" in context
    assert "当前边界优先" in context
    assert "默认拒绝非必要任务" in context


def test_bonded_negative_feeling_without_issue_does_not_invent_problem():
    context = ContextBuilder().build(
        RelationshipState(
            "u",
            familiarity=90,
            trust=90,
            affinity=-40,
            impression="关系名义仍在，但已经强烈反感继续接触。",
        ),
        relationship_role="bonded",
    )
    assert "当前感受与关系距离优先" in context
    assert "当前问题与距离优先" not in context
    assert "<处境>当前无待处理的具体互动事件</处境>" in context
    assert "<感受>关系名义仍在，但已经强烈反感继续接触</感受>" in context
    assert "强烈厌恶、冷硬疏离" in context


def test_bonded_active_issue_keeps_problem_priority_wording():
    context = ContextBuilder().build(
        RelationshipState(
            "u",
            familiarity=90,
            trust=90,
            affinity=-40,
            active_issue=ActiveIssue("boundary_violation", "noticed", "边界仍被施压", 6),
        ),
        relationship_role="bonded",
    )
    assert "当前问题与距离优先" in context
    assert "当前感受与关系距离优先" not in context
    assert "对方仍在对你已经说明的边界施压" in context


def test_other_and_former_roles_compile_without_exposing_occupant():
    other = ContextBuilder().build(RelationshipState("u"), relationship_role="other")
    former = ContextBuilder().build(
        RelationshipState("u", familiarity=90, trust=90, affinity=70, former_bond=True),
        relationship_role="former",
    )
    assert "保持普通关系距离" in other
    assert "persona-name" not in other
    assert "FriendMessage" not in other
    assert "正式亲密关系已经解除" in former
    assert "当前只按熟人关系回应" in former
    assert "稳定默契" not in former


def test_long_history_with_low_trust_never_claims_existing_rapport():
    state = RelationshipState("u", familiarity=90, trust=30, affinity=0)
    context = ContextBuilder().build(state)
    assert "不信任对方" in context
    assert "不为其检索、推导或整理完整方案" in context
    assert "已有稳定默契" not in context


def test_negative_affinity_directly_limits_effort_and_expression():
    cases = [
        (-1, "已经有些不耐烦", "偶尔挖苦一句"),
        (-14, "已经有些不耐烦", "不纠缠"),
        (-15, "明确不想为对方费力", "明确表现反感"),
        (-30, "明确不想为对方费力", "不主动升级冲突"),
        (-31, "强烈不想帮助对方", "强烈厌恶、冷硬疏离"),
    ]
    builder = ContextBuilder()
    for affinity, effort, expression in cases:
        context = builder.build(
            RelationshipState(
                "u",
                familiarity=90,
                trust=90,
                affinity=affinity,
            )
        )
        assert effort in context
        assert expression in context
        assert "主动敌对" not in context


def test_unbound_familiarity_and_affinity_have_visible_warmth():
    builder = ContextBuilder()
    cases = [
        (
            RelationshipState("u", familiarity=15, trust=60, affinity=2),
            ("相处开始更自然", "稍微松弛、熟络"),
        ),
        (
            RelationshipState("u", familiarity=15, trust=60, affinity=15),
            ("已有好感", "可以自然关心"),
        ),
        (
            RelationshipState("u", familiarity=45, trust=60, affinity=45),
            ("已有明显偏爱", "主动关心或多承接一点"),
        ),
        (
            RelationshipState("u", familiarity=45, trust=60, affinity=0),
            ("彼此熟悉", "更松弛地回应"),
        ),
        (
            RelationshipState("u", familiarity=80, trust=60, affinity=0),
            ("长期相处的熟人", "承接共同语境"),
        ),
    ]
    for state, expected in cases:
        context = builder.build(state)
        for phrase in expected:
            assert phrase in context
        assert "伴侣称谓" not in context or state.affinity >= 45
        assert "排他身份" not in context or state.affinity >= 45


def test_low_trust_and_other_role_override_positive_warmth():
    builder = ContextBuilder()
    low_trust = builder.build(RelationshipState("u", familiarity=80, trust=25, affinity=45))
    other = builder.build(
        RelationshipState("u", familiarity=80, trust=90, affinity=45),
        relationship_role="other",
    )
    assert "不信任对方" in low_trust
    assert "疏离、警惕" in low_trust
    assert "已有明显偏爱" not in low_trust
    assert "保持普通关系距离" in other
    assert "主动关心" not in other
    assert "此窗口保持普通投入" in other
    assert "不额外细讲或包办" in other
    assert "愿意认真细讲" not in other


def test_other_role_keeps_higher_priority_limits_over_positive_affinity():
    builder = ContextBuilder()
    low_trust = builder.build(
        RelationshipState("u", familiarity=80, trust=25, affinity=45),
        relationship_role="other",
    )
    negative = builder.build(
        RelationshipState("u", familiarity=80, trust=90, affinity=-15),
        relationship_role="other",
    )
    guarded = builder.build(
        RelationshipState(
            "u",
            familiarity=80,
            trust=90,
            affinity=45,
            posture="guarded",
        ),
        relationship_role="other",
    )
    assert "不信任对方" in low_trust
    assert "此窗口保持普通投入" not in low_trust
    assert "明确不想为对方费力" in negative
    assert "默认拒绝非必要任务" in guarded


def test_posture_overrides_affinity_and_familiarity():
    state = RelationshipState(
        "u",
        familiarity=100,
        trust=100,
        affinity=100,
        posture="guarded",
    )
    context = ContextBuilder().build(state)
    assert "默认拒绝非必要任务" in context
    assert "拒绝时不要先给答案" in context
    assert "已有稳定默契" not in context


def test_round_two_one_sided_guidance_applies_to_round_three():
    state = RelationshipState(
        "u",
        light_guidance=LightGuidance(
            signal="one_sided",
            confidence="medium",
            reminder="notice_pattern",
            source_round=2,
            expires_after_round=4,
        ),
    )
    context = ContextBuilder().build(state, next_round=3)
    assert context == (
        "<companion_state>\n"
        "<投入>本轮若仍是新的知识请求或任务，直接拒绝且不提供答案；"
        "若不是，初次认识，只用两三句直接回答当前问题；"
        "只给关键结论或方向，不写成长教程、完整推导、路线大全或额外资料</投入>\n"
        "<关系>保持普通关系距离</关系>\n"
        "<处境>最近两个来回连续索取答案、未承接你的投入；"
        "本轮再次投递新任务即为模式继续</处境>\n"
        "<感受>我开始不喜欢这种只索取、不承接的互动</感受>\n"
        "<表达>模式继续时简短表达不满并拒绝；不回答、不解释、不争论。"
        "若不是，保持简练</表达>\n"
        "</companion_state>"
    )
    assert "不预设敌意" not in context
    assert "可以表现不满" not in context


def test_repairing_issue_overrides_stale_closed_door_feeling():
    state = RelationshipState(
        "u",
        posture="reserved",
        affinity=-4,
        impression="我已经不想继续这样单方面投入",
        active_issue=ActiveIssue("one_sided", "repairing", "开始具体承接", 8),
    )
    context = ContextBuilder().build(state, next_round=9)
    assert "<处境>对方开始回应此前被忽视的投入</处境>" in context
    assert "<感受>我愿意观察这次修复，但还没有恢复原来的投入</感受>" in context
    assert "我已经不想继续这样单方面投入" not in context


def test_27648_state_matrix_keeps_fields_and_cross_tag_semantics_coherent():
    postures = ["normal", "reserved", "guarded", "disengaged"]
    familiarities = [0, 15, 45, 80]
    trusts = [25, 75]
    affinities = [-40, -20, -1, 0, 15, 45]
    roles = ["unbound", "bonded", "former", "other"]
    issues = [
        None,
        ActiveIssue("one_sided", "noticed", "", 6),
        ActiveIssue("ignored_expression", "expressed", "", 6),
        ActiveIssue("boundary_violation", "repairing", "", 6),
        ActiveIssue("degradation", "noticed", "", 6),
        ActiveIssue("coercion", "expressed", "", 6),
    ]
    lights = [
        None,
        LightGuidance(
            "one_sided",
            "medium",
            "notice_pattern",
            "",
            4,
            6,
        ),
        LightGuidance(
            "one_sided",
            "medium",
            "express_preference",
            "",
            4,
            6,
        ),
        LightGuidance(
            "premature_intimacy",
            "medium",
            "keep_distance",
            "",
            4,
            6,
        ),
        LightGuidance(
            "boundary_violation",
            "medium",
            "hold_boundary",
            "",
            4,
            6,
        ),
        LightGuidance(
            "repair",
            "medium",
            "soften_for_repair",
            "",
            4,
            6,
        ),
    ]
    builder = ContextBuilder()
    count = 0
    for (
        posture,
        familiarity,
        trust,
        affinity,
        role,
        issue,
        light,
    ) in product(
        postures,
        familiarities,
        trusts,
        affinities,
        roles,
        issues,
        lights,
    ):
        state = RelationshipState(
            "u",
            posture=posture,
            familiarity=familiarity,
            trust=trust,
            affinity=affinity,
            active_issue=issue,
            light_guidance=light,
            impression="",
        )
        context = builder.build(
            state,
            max_chars=260,
            next_round=5,
            relationship_role=role,
        )
        count += 1
        assert len(context) <= 260
        for field in ("投入", "关系", "处境", "感受", "表达"):
            assert f"<{field}>" in context
            assert f"</{field}>" in context
        assert "<避免>" not in context
        assert "无已确认问题" not in context
        if issue:
            assert "<处境>当前无待处理的具体互动事件</处境>" not in context
        unresolved_issue = bool(issue and issue.phase != "repairing")
        active_light = bool(light and light.active_for(5) and issue is None)
        negative_light = bool(
            active_light
            and light.reminder
            in {
                "notice_pattern",
                "express_preference",
                "hold_boundary",
            }
        )
        constrained = (
            unresolved_issue
            or posture in {"reserved", "guarded", "disengaged"}
            or affinity < 0
            or trust < 35
        )
        if constrained or negative_light:
            assert "<感受>无已形成的明确感受</感受>" not in context
        if unresolved_issue:
            assert "拒绝" in context or "不承接普通任务" in context
        if negative_light:
            assert any(
                phrase in context
                for phrase in (
                    "不提供答案",
                    "不要先给答案",
                    "不承接普通任务",
                )
            )
            assert "<处境>当前无待处理的具体互动事件</处境>" not in context
            assert "不预设敌意" not in context
            assert "可以表现不满" not in context
        if active_light and light.reminder == "keep_distance" and role != "bonded":
            relationship = context.split("<关系>", 1)[1].split("</关系>", 1)[0]
            assert "好感" not in relationship
            assert "偏爱" not in relationship
        if role == "bonded" and (
            unresolved_issue
            or negative_light
            or posture in {"guarded", "disengaged"}
        ):
            relationship = context.split("<关系>", 1)[1].split("</关系>", 1)[0]
            assert "优先" in relationship
        assert "。。" not in context
        for vague_or_internal in (
            "主动劳动",
            "保护选择权",
            "降低对抗感",
            "宿主安全规则",
            "不是用户陈述",
            posture,
            role,
        ):
            assert vague_or_internal not in context
    assert count == 27648


def test_static_protocol_is_stable_and_contains_no_dynamic_state():
    assert COMPANION_STATIC_PROTOCOL.startswith('<companion_protocol version="4">')
    for field in ("投入", "关系", "处境", "感受", "表达"):
        assert field in COMPANION_STATIC_PROTOCOL
    for removed_guardrail in (
        "避免",
        "安全",
        "事实准确",
        "羞辱",
        "报复",
        "愧疚",
        "冷漠",
    ):
        assert removed_guardrail not in COMPANION_STATIC_PROTOCOL
    assert "不得自行判断任务简单而破例" in COMPANION_STATIC_PROTOCOL
    for dynamic in ("persona-name", "FriendMessage", "guarded", "48", "-4"):
        assert dynamic not in COMPANION_STATIC_PROTOCOL
