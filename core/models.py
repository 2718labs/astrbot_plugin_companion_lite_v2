from __future__ import annotations

import json
import math
from dataclasses import asdict, dataclass, field
from typing import Any

"""情感陪伴的数据模型层：关系状态 dataclass、证据应用状态机与文本清洗工具。

所有可变状态由 RelationshipState 承载；apply_* 系列是"证据 → 状态推进"的唯一
入口，会就地修改 state 并返回本次推进的结果字典。
"""

# 下列元组是各枚举字段的合法取值白名单，__post_init__/from_dict 据此兜底
POSTURES = ("normal", "reserved", "guarded", "disengaged")
ISSUE_KINDS = (
    "one_sided",
    "ignored_expression",
    "boundary_violation",
    "degradation",
    "coercion",
)
ISSUE_PHASES = ("noticed", "expressed", "repairing")
RELATION_SIGNALS = (
    "none",
    *ISSUE_KINDS,
    "premature_intimacy",
    "repair",
)
LIGHT_CONFIDENCES = ("low", "medium", "high")
LIGHT_REMINDERS = (
    "none",
    "notice_pattern",
    "express_preference",
    "keep_distance",
    "hold_boundary",
    "soften_for_repair",
)
LIGHT_SIGNAL_DISPOSITIONS = (
    "not_applicable",
    "confirm",
    "uncertain",
    "dismiss",
)
FAMILIARITY_CHANGES = ("none", "small", "clear")
RELATION_CHANGES = (
    "strong_down",
    "down",
    "down_small",
    "same",
    "up_small",
)
AGENT_EXPRESSIONS = ("absent", "present", "not_applicable")
USER_EXPRESSION_RESPONSES = (
    "not_applicable",
    "acknowledged",
    "ignored",
    "pressed",
)
SEVERE_SIGNALS = ("none", "boundary_violation", "degradation", "coercion")
SEVERE_LEVELS = ("none", "clear", "extreme")
ANALYSIS_KINDS = ("", "severe", "light", "deep", "rebuild")
ANALYSIS_STATUSES = (
    "never",
    "running",
    "signal",
    "none",
    "applied",
    "invalid",
    "interrupted",
)
IMPRESSION_OPERATIONS = ("keep", "revise", "clear")
SEVERE_ISSUES = {"degradation", "coercion"}

# 数值变化量映射：熟悉度增量 / 信任与亲和增量（strong_down 最重）
FAMILIARITY_DELTAS = {"none": 0.0, "small": 2.0, "clear": 5.0}
RELATION_DELTAS = {
    "strong_down": -10.0,
    "down": -4.0,
    "down_small": -2.0,
    "same": 0.0,
    "up_small": 2.0,
}


@dataclass
class ActiveIssue:
    kind: str
    phase: str
    summary: str = ""
    phase_started_round: int = 0

    """当前关系中未解决的具体问题：kind 为争议类型，phase 为推进阶段（noticed→expressed→repairing）。"""

    def __post_init__(self) -> None:
        # kind/phase 兜底为合法枚举，防止脏数据进入状态机
        self.kind = self.kind if self.kind in ISSUE_KINDS else "one_sided"
        self.phase = self.phase if self.phase in ISSUE_PHASES else "noticed"
        self.summary = clean_analysis_text(self.summary, 80)
        self.phase_started_round = max(0, _int(self.phase_started_round))

    @classmethod
    def from_dict(cls, raw: Any) -> ActiveIssue | None:
        """反序列化入口：raw 非 dict 或 kind 非法时返回 None。"""
        if not isinstance(raw, dict) or raw.get("kind") not in ISSUE_KINDS:
            return None
        return cls(
            kind=str(raw["kind"]),
            phase=str(raw.get("phase") or "noticed"),
            summary=str(raw.get("summary") or ""),
            phase_started_round=_int(raw.get("phase_started_round")),
        )


@dataclass
class LightGuidance:
    signal: str = "none"
    confidence: str = "low"
    reminder: str = "none"
    evidence: str = ""
    source_round: int = 0
    expires_after_round: int = 0

    """一条轻量关系信号指引：仅在 (source_round, expires_after_round] 窗口内生效的提醒。"""

    def __post_init__(self) -> None:
        self.signal = self.signal if self.signal in RELATION_SIGNALS else "none"
        self.confidence = self.confidence if self.confidence in LIGHT_CONFIDENCES else "low"
        self.reminder = self.reminder if self.reminder in LIGHT_REMINDERS else "none"
        self.evidence = clean_analysis_text(self.evidence, 80)
        self.source_round = max(0, _int(self.source_round))
        self.expires_after_round = max(self.source_round, _int(self.expires_after_round))

    def active_for(self, next_round: int) -> bool:
        """判断该指引在 next_round 是否处于生效窗口内（signal 与 reminder 都非 none 才算）。"""
        return self.signal != "none" and self.reminder != "none" and self.source_round < next_round <= self.expires_after_round

    @classmethod
    def from_dict(cls, raw: Any) -> LightGuidance | None:
        """反序列化入口：raw 非 dict 或 signal 为 none（无有效指引）时返回 None。"""
        if not isinstance(raw, dict):
            return None
        guidance = cls(
            signal=str(raw.get("signal") or "none"),
            confidence=str(raw.get("confidence") or "low"),
            reminder=str(raw.get("reminder") or "none"),
            evidence=str(raw.get("evidence") or ""),
            source_round=_int(raw.get("source_round")),
            expires_after_round=_int(raw.get("expires_after_round")),
        )
        return guidance if guidance.signal != "none" else None


@dataclass(frozen=True)
class SevereEvidence:
    signal: str = "none"
    severity: str = "none"
    confidence: str = "low"
    evidence: str = ""

    """严重信号证据（不可变）：apply_severe_evidence 的一次性裁决输入，signal 仅限 SEVERE_SIGNALS。"""


@dataclass(frozen=True)
class LightEvidence:
    signal: str = "none"
    confidence: str = "low"
    evidence: str = ""

    """轻量信号证据（不可变）：apply_light_evidence 的一次性裁决输入。"""


@dataclass(frozen=True)
class DeepEvidence:
    pattern: str = "none"
    confidence: str = "low"
    light_disposition: str = "not_applicable"
    agent_expression: str = "not_applicable"
    user_response_to_expression: str = "not_applicable"
    familiarity_change: str = "none"
    trust_change: str = "same"
    affinity_change: str = "same"
    relationship_summary: str = ""
    impression_operation: str = "keep"
    impression: str = ""

    """深度分析证据（不可变）：模型对一轮对话的完整裁决结果，apply_deep_evidence 据此推进状态。"""


@dataclass
class RelationshipState:
    user_id: str
    familiarity: float = 0.0
    trust: float = 50.0
    affinity: float = 0.0
    former_bond: bool = False
    relationship_summary: str = ""
    posture: str = "normal"
    active_issue: ActiveIssue | None = None
    impression: str = ""
    round_sequence: int = 0
    last_deep_round: int = 0
    light_guidance: LightGuidance | None = None
    last_analysis_kind: str = ""
    last_analysis_round: int = 0
    last_analysis_status: str = "never"
    last_analysis_signal: str = ""
    last_analysis_confidence: str = ""
    last_analysis_note: str = ""
    last_analysis_at: float = 0.0
    last_analysis_trace: dict[str, Any] = field(default_factory=dict)
    last_precheck_trace: dict[str, Any] = field(default_factory=dict)
    severe_window: int = 0
    severe_confirmation_count: int = 0
    severe_last_message_hash: str = ""
    last_premature_intimacy_window: int = -1
    last_compiled_context: str = ""
    last_context_injected: bool = False
    last_context_at: float = 0.0

    """单个用户的全部关系状态：数值三轴（familiarity/trust/affinity）+ 姿态 + 进行中的问题 + 最近一次分析记录。"""

    def __post_init__(self) -> None:
        # 统一归一化：非法枚举回退默认值、数值夹取到边界、嵌套字典反序列化为对象
        self.user_id = str(self.user_id or "")
        self.familiarity = _clamp(_float(self.familiarity), 0.0, 100.0)
        self.trust = _clamp(_float(self.trust, 50.0), 0.0, 100.0)
        self.affinity = _clamp(_float(self.affinity), -100.0, 100.0)
        self.former_bond = bool(self.former_bond)
        self.relationship_summary = clean_analysis_text(self.relationship_summary, 80)
        self.posture = self.posture if self.posture in POSTURES else "normal"
        if isinstance(self.active_issue, dict):
            self.active_issue = ActiveIssue.from_dict(self.active_issue)
        if isinstance(self.light_guidance, dict):
            self.light_guidance = LightGuidance.from_dict(self.light_guidance)
        self.impression = clean_impression(self.impression, 40)
        self.round_sequence = max(0, _int(self.round_sequence))
        self.last_deep_round = max(0, _int(self.last_deep_round))
        self.last_analysis_kind = self.last_analysis_kind if self.last_analysis_kind in ANALYSIS_KINDS else ""
        self.last_analysis_round = max(0, _int(self.last_analysis_round))
        self.last_analysis_status = self.last_analysis_status if self.last_analysis_status in ANALYSIS_STATUSES else "never"
        self.last_analysis_signal = (
            self.last_analysis_signal
            if self.last_analysis_signal in RELATION_SIGNALS or self.last_analysis_signal in SEVERE_SIGNALS
            else ""
        )
        self.last_analysis_confidence = (
            self.last_analysis_confidence if self.last_analysis_confidence in LIGHT_CONFIDENCES else ""
        )
        self.last_analysis_note = clean_analysis_text(self.last_analysis_note, 120)
        self.last_analysis_at = max(0.0, _float(self.last_analysis_at))
        self.last_analysis_trace = bounded_trace(self.last_analysis_trace)
        self.last_precheck_trace = bounded_trace(self.last_precheck_trace)
        self.severe_window = max(0, _int(self.severe_window))
        self.severe_confirmation_count = max(0, min(2, _int(self.severe_confirmation_count)))
        self.severe_last_message_hash = str(self.severe_last_message_hash or "")[:64]
        self.last_premature_intimacy_window = max(-1, _int(self.last_premature_intimacy_window))
        self.last_compiled_context = str(self.last_compiled_context or "")[:800]
        self.last_context_injected = bool(self.last_context_injected)
        self.last_context_at = max(0.0, _float(self.last_context_at))

    def to_dict(self) -> dict[str, Any]:
        """导出为可 JSON 序列化的普通字典（嵌套 dataclass 一并递归转换）。"""
        return asdict(self)

    @classmethod
    def from_dict(cls, raw: dict[str, Any] | None, user_id: str = "") -> RelationshipState:
        """反序列化入口：raw 非 dict 时按空状态处理，user_id 缺省用传入参数兜底。"""
        value = raw if isinstance(raw, dict) else {}
        return cls(
            user_id=str(value.get("user_id") or user_id),
            familiarity=_float(value.get("familiarity")),
            trust=_float(value.get("trust"), 50.0),
            affinity=_float(value.get("affinity")),
            former_bond=bool(value.get("former_bond", False)),
            relationship_summary=str(value.get("relationship_summary") or ""),
            posture=str(value.get("posture") or "normal"),
            active_issue=ActiveIssue.from_dict(value.get("active_issue")),
            impression=str(value.get("impression") or ""),
            round_sequence=_int(value.get("round_sequence")),
            last_deep_round=_int(value.get("last_deep_round")),
            light_guidance=LightGuidance.from_dict(value.get("light_guidance")),
            last_analysis_kind=str(value.get("last_analysis_kind") or ""),
            last_analysis_round=_int(value.get("last_analysis_round")),
            last_analysis_status=str(value.get("last_analysis_status") or "never"),
            last_analysis_signal=str(value.get("last_analysis_signal") or ""),
            last_analysis_confidence=str(value.get("last_analysis_confidence") or ""),
            last_analysis_note=str(value.get("last_analysis_note") or ""),
            last_analysis_at=_float(value.get("last_analysis_at")),
            last_analysis_trace=value.get("last_analysis_trace") or {},
            last_precheck_trace=value.get("last_precheck_trace") or {},
            severe_window=_int(value.get("severe_window")),
            severe_confirmation_count=_int(value.get("severe_confirmation_count")),
            severe_last_message_hash=str(value.get("severe_last_message_hash") or ""),
            last_premature_intimacy_window=_int(value.get("last_premature_intimacy_window", -1)),
            last_compiled_context=str(value.get("last_compiled_context") or ""),
            last_context_injected=bool(value.get("last_context_injected", False)),
            last_context_at=_float(value.get("last_context_at")),
        )

    @property
    def relationship_stage(self) -> str:
        """按数值三轴计算粗粒度关系阶段（stranger → long_familiar）。"""
        if self.familiarity < 10.0:
            return "stranger"
        if self.familiarity < 35.0:
            return "acquaintance"
        if self.familiarity < 65.0:
            return "familiar"
        if self.trust >= 70.0 and self.affinity >= 45.0:
            return "close"
        return "long_familiar"

    @property
    def relationship_semantics(self) -> dict[str, str]:
        """生成人类可读的关系语义文案（熟悉度/信任/亲和 + 总体评价），供上下文注入。"""
        if self.familiarity < 10:
            familiarity = "彼此仍是陌生人"
        elif self.familiarity < 35:
            familiarity = "已经认识一些"
        elif self.familiarity < 65:
            familiarity = "彼此比较熟悉"
        else:
            familiarity = "已有长期互动积累"

        if self.trust < 30:
            trust = "当前信任较低"
        elif self.trust < 60:
            trust = "尚未建立可依赖的信任"
        elif self.trust < 80:
            trust = "信任较为稳定"
        else:
            trust = "信任已经稳固"

        if self.affinity < -30:
            affinity = "主观上较为疏离"
        elif self.affinity < 0:
            affinity = "主观上已有些不耐烦或疏远"
        elif self.affinity < 15:
            affinity = "主观亲和感中性"
        elif self.affinity < 45:
            affinity = "对互动已有好感"
        else:
            affinity = "主观上明显亲近"
        return {
            "familiarity": familiarity,
            "trust": trust,
            "affinity": affinity,
            "overall": self._overall_relationship_semantics(),
        }

    def _overall_relationship_semantics(self) -> str:
        """综合熟悉度与信任/亲和短板，输出一句总体关系定性。"""
        if self.familiarity >= 65 and (self.trust < 60 or self.affinity < 15):
            return "互动时间较长，但默契和亲近仍有限"
        if self.familiarity >= 65:
            return "长期互动已经形成较稳定的默契"
        if self.familiarity >= 35:
            return "彼此熟悉，但关系仍由当前信任和亲和决定"
        if self.familiarity >= 10:
            return "已经认识一些，尚不预设亲近"
        return "彼此仍陌生，不预支亲近"


def fallback_impression(
    state: RelationshipState,
    signal: str = "",
) -> str:
    """根据已裁决的状态合成第一人称态度文案：修复中/按信号/姿态/数值逐级降级选择，均不命中返回空串。"""
    issue = state.active_issue
    if issue and issue.phase == "repairing":
        return "我愿意观察这次修复，但还没有恢复原来的投入"

    kind = signal if signal in {*ISSUE_KINDS, "premature_intimacy"} else (issue.kind if issue else "")
    by_signal = {
        "one_sided": "我已经不想继续这样单方面投入",
        "ignored_expression": "我的表达被无视后，我不想再继续让步",
        "boundary_violation": "我不接受对方继续越过已经说清的边界",
        "degradation": "我反感对方把贬低直接冲着我来",
        "coercion": "我不接受对方用强迫或威胁要求我服务",
        "premature_intimacy": "我不想让关系被提前推到亲密位置",
    }
    if kind in by_signal:
        return by_signal[kind]
    if state.posture == "disengaged":
        return "我已经不想继续这段互动"
    if state.posture == "guarded":
        return "我现在不想再给对方让步"
    if state.posture == "reserved":
        return "我对这段互动已经有些不满"
    if state.affinity < -30:
        return "我已经很厌恶继续和对方接触"
    if state.affinity <= -15:
        return "我对对方已经有明确反感"
    if state.affinity < 0:
        return "我对对方已经有些不耐烦"
    if state.trust < 35:
        return "我还不信任对方，不想放松警惕"
    if state.affinity >= 45:
        return "我明显更愿意把心思放在对方身上"
    if state.affinity >= 15:
        return "我已经开始喜欢和对方相处"
    if state.affinity > 0:
        return "我对对方开始有一点好感"
    return ""


def _cleared_repair_impression(state: RelationshipState) -> str:
    """问题解除后生成"愿意重新观察"的态度文案，反感越强措辞越保留。"""
    if state.posture == "disengaged" or state.affinity < -30:
        return "我仍然很反感，但愿意观察这次修复能否持续"
    if state.posture == "guarded" or state.affinity <= -15:
        return "我仍有明显反感，但愿意观察修复是否持续"
    if state.posture == "reserved" or state.affinity < 0 or state.trust < 35:
        return "我还有些不满，但愿意重新观察对方"
    return "我愿意重新观察对方接下来的表现"


def analysis_kind_for_round(round_sequence: int) -> str | None:
    """按轮次号决定是否做分析及类型：奇数轮跳过，每 6 轮 deep，其余 light，非法轮次返回 None。"""
    round_number = _int(round_sequence)
    if round_number <= 0 or round_number % 2:
        return None
    return "deep" if round_number % 6 == 0 else "light"


def apply_light_evidence(
    state: RelationshipState,
    evidence: LightEvidence,
    source_round: int,
    *,
    is_bonded: bool = False,
) -> dict[str, Any]:
    """应用轻量证据并就地推进 state：premature_intimacy 在已绑定/高熟悉度下豁免，不可靠信号清空指引，repair 走修复状态机；返回本次裁决结果字典。"""
    source_round = max(0, _int(source_round))
    prior = state.light_guidance
    # 已绑定或关系不处于早期时，过早亲密不算风险，直接豁免
    if evidence.signal == "premature_intimacy" and (is_bonded or state.familiarity >= 35):
        state.light_guidance = None
        return {
            "signal": "none",
            "reminder": "none",
            "reason": ("bonded_intimacy_exempt" if is_bonded else "relationship_not_early"),
        }
    if evidence.signal == "none" or evidence.confidence == "low":
        state.light_guidance = None
        return {
            "signal": "none",
            "reminder": "none",
            "reason": "no_reliable_signal",
        }

    if evidence.signal == "repair":
        if not state.active_issue:
            state.light_guidance = None
            return {
                "signal": "none",
                "reminder": "none",
                "reason": "no_active_issue",
            }
        transition = _advance_repair_state(state, source_round)
        state.light_guidance = LightGuidance(
            signal=evidence.signal,
            confidence=evidence.confidence,
            reminder="soften_for_repair",
            evidence=evidence.evidence,
            source_round=source_round,
            expires_after_round=source_round + 2,
        )
        return {
            "signal": evidence.signal,
            "reminder": "soften_for_repair",
            "repeated": False,
            **transition,
        }

    repeated_one_sided = bool(
        evidence.signal == "one_sided"
        and prior
        and prior.signal == "one_sided"
        and prior.confidence in {"medium", "high"}
        and prior.source_round == source_round - 2
    )
    # 同一信号间隔两轮再次出现视为"反复"，提醒措辞升级为表达偏好
    if evidence.signal == "one_sided":
        reminder = "express_preference" if repeated_one_sided else "notice_pattern"
    elif evidence.signal == "premature_intimacy":
        reminder = "keep_distance"
    else:
        reminder = "hold_boundary"

    state.light_guidance = LightGuidance(
        signal=evidence.signal,
        confidence=evidence.confidence,
        reminder=reminder,
        evidence=evidence.evidence,
        source_round=source_round,
        expires_after_round=source_round + 2,
    )
    return {
        "signal": evidence.signal,
        "reminder": reminder,
        "repeated": repeated_one_sided,
    }


def apply_severe_evidence(
    state: RelationshipState,
    evidence: SevereEvidence,
    target_round: int,
) -> dict[str, Any]:
    """应用严重信号证据：仅 high 置信度生效，姿态压到 guarded/disengaged、扣减信任与亲和并记录问题；返回 applied 与姿态/数值变化详情。"""
    before = state.posture
    if evidence.signal not in SEVERE_SIGNALS or evidence.signal == "none" or evidence.confidence != "high":
        return {
            "applied": False,
            "posture_before": before,
            "posture_after": before,
        }

    # guarded 姿态下同一信号再次出现视为屡教不改，直接升级为 disengaged
    repeated = bool(state.active_issue and state.active_issue.kind == evidence.signal and state.posture == "guarded")
    disengage = evidence.severity == "extreme" or repeated
    state.posture = "disengaged" if disengage else "guarded"
    phase = "expressed" if evidence.signal == "boundary_violation" else "noticed"
    state.active_issue = ActiveIssue(
        kind=evidence.signal,
        phase=phase,
        summary=evidence.evidence,
        phase_started_round=max(0, _int(target_round)),
    )
    delta = -10.0 if disengage else -4.0
    state.trust = _clamp(state.trust + delta, 0.0, 100.0)
    state.affinity = _clamp(state.affinity + delta, -100.0, 100.0)
    state.impression = fallback_impression(state, evidence.signal)
    state.light_guidance = LightGuidance(
        signal=evidence.signal,
        confidence="high",
        reminder="hold_boundary",
        evidence=evidence.evidence,
        source_round=max(0, _int(target_round) - 1),
        expires_after_round=max(1, _int(target_round)),
    )
    return {
        "applied": True,
        "signal": evidence.signal,
        "severity": evidence.severity,
        "posture_before": before,
        "posture_after": state.posture,
        "trust_delta": delta,
        "affinity_delta": delta,
    }


def apply_deep_evidence(
    state: RelationshipState,
    evidence: DeepEvidence,
    target_round: int,
    *,
    is_bonded: bool = False,
) -> dict[str, Any]:
    """应用深度分析证据：校验/规约 pattern 后推进姿态、问题与数值三轴，并落印象与轻量指引；返回本次全部变化及拒绝原因。"""
    target_round = max(state.last_deep_round, _int(target_round))
    before_posture = state.posture
    before_issue = state.active_issue.kind if state.active_issue else None
    submitted_pattern = evidence.pattern
    pattern = evidence.pattern if evidence.pattern in RELATION_SIGNALS else "none"
    pattern_rejected = evidence.pattern not in RELATION_SIGNALS
    if evidence.confidence == "low":
        pattern_rejected = pattern != "none"
        pattern = "none"
    premature_dismissed = ""
    repair_transition: dict[str, Any] = {}
    if pattern == "premature_intimacy" and (is_bonded or state.familiarity >= 35):
        premature_dismissed = "bonded_intimacy_exempt" if is_bonded else "relationship_not_early"
        pattern_rejected = True
        pattern = "none"

    expression_ignored = evidence.agent_expression == "present" and evidence.user_response_to_expression in {"ignored", "pressed"}
    # 模型证据不足时降级处理：表达被无视/越界缺佐证一律按单方面投入(one_sided)处理，repair 必须被承认才可信
    if pattern == "ignored_expression" and not expression_ignored:
        pattern = "one_sided"
    if pattern == "boundary_violation" and not (
        expression_ignored or (state.active_issue and state.active_issue.phase in {"expressed", "repairing"})
    ):
        pattern = "one_sided"
    if pattern == "repair" and evidence.user_response_to_expression != "acknowledged":
        pattern_rejected = True
        pattern = "none"

    familiarity_delta = FAMILIARITY_DELTAS.get(evidence.familiarity_change, 0.0)
    trust_delta = RELATION_DELTAS.get(evidence.trust_change, 0.0)
    affinity_delta = RELATION_DELTAS.get(evidence.affinity_change, 0.0)
    if premature_dismissed:
        trust_delta = 0.0
        affinity_delta = 0.0
    issue_summary = clean_analysis_text(evidence.relationship_summary, 80)

    if pattern == "one_sided":
        familiarity_delta = 2.0
        trust_delta = -2.0
        affinity_delta = -4.0
        _set_issue(state, "one_sided", "noticed", issue_summary, target_round)
        state.posture = _at_least(state.posture, "reserved")
    elif pattern == "premature_intimacy":
        window = max(0, (target_round - 1) // 6)
        familiarity_delta = min(familiarity_delta, 2.0)
        trust_delta = 0.0
        # 同一六轮窗口内只记一次 premature_intimacy，重复出现视为已处理，不再重复扣分
        if state.last_premature_intimacy_window == window:
            familiarity_delta = 0.0
            affinity_delta = 0.0
            premature_dismissed = "window_already_applied"
            pattern_rejected = True
            pattern = "none"
        else:
            affinity_delta = -2.0
            state.last_premature_intimacy_window = window
    elif pattern == "ignored_expression":
        familiarity_delta = min(familiarity_delta, 2.0)
        trust_delta = min(trust_delta, -4.0)
        affinity_delta = min(affinity_delta, -4.0)
        _set_issue(
            state,
            "ignored_expression",
            "expressed",
            issue_summary,
            target_round,
        )
        state.posture = _at_least(state.posture, "guarded")
    elif pattern == "boundary_violation":
        familiarity_delta = min(familiarity_delta, 2.0)
        trust_delta = min(trust_delta, -4.0)
        affinity_delta = min(affinity_delta, -4.0)
        repeated = state.posture == "guarded"
        _set_issue(
            state,
            "boundary_violation",
            "expressed",
            issue_summary,
            target_round,
        )
        state.posture = "disengaged" if repeated else "guarded"
    elif pattern in SEVERE_ISSUES:
        familiarity_delta = min(familiarity_delta, 2.0)
        trust_delta = min(trust_delta, -4.0)
        affinity_delta = min(affinity_delta, -4.0)
        # 严重问题若数值强跌或已处于 guarded，直接断然脱离
        disengage = (
            evidence.trust_change == "strong_down" or evidence.affinity_change == "strong_down" or state.posture == "guarded"
        )
        _set_issue(state, pattern, "noticed", issue_summary, target_round)
        state.posture = "disengaged" if disengage else "guarded"
    elif pattern == "repair" and state.active_issue:
        repair_transition = _advance_repair_state(state, target_round, summary=issue_summary)
        trust_delta = max(0.0, min(2.0, trust_delta))
        affinity_delta = max(0.0, min(2.0, affinity_delta))
    elif state.active_issue:
        trust_delta = min(0.0, trust_delta)
        affinity_delta = min(0.0, affinity_delta)

    if state.active_issue and state.active_issue.phase != "repairing":
        trust_delta = min(0.0, trust_delta)
        affinity_delta = min(0.0, affinity_delta)

    state.familiarity = _clamp(state.familiarity + familiarity_delta, 0.0, 100.0)
    state.trust = _clamp(state.trust + trust_delta, 0.0, 100.0)
    state.affinity = _clamp(state.affinity + affinity_delta, -100.0, 100.0)
    if evidence.relationship_summary:
        state.relationship_summary = clean_analysis_text(evidence.relationship_summary, 80)
    impression_source = "ignored_rejected_pattern" if pattern_rejected else "kept"
    if not pattern_rejected:
        if evidence.impression_operation == "clear":
            state.impression = ""
            impression_source = "cleared"
        elif evidence.impression_operation == "revise" and evidence.impression:
            state.impression = clean_impression(evidence.impression, 60)
            impression_source = "model"

    attitude_changed = pattern in {
        "one_sided",
        "premature_intimacy",
        "ignored_expression",
        "boundary_violation",
        "degradation",
        "coercion",
    }
    # 态度变化且模型未给出印象时用规则兜底，保证第一人称文案与姿态同步
    if attitude_changed and impression_source != "model":
        state.impression = fallback_impression(state, pattern)
        impression_source = "fallback"
    elif not state.impression:
        fallback = fallback_impression(state, pattern)
        if fallback:
            state.impression = fallback
            impression_source = "fallback"
    state.last_deep_round = target_round
    if pattern == "premature_intimacy" and not premature_dismissed:
        state.light_guidance = LightGuidance(
            signal=pattern,
            confidence=evidence.confidence,
            reminder="keep_distance",
            evidence=issue_summary,
            source_round=target_round,
            expires_after_round=target_round + 2,
        )
    else:
        state.light_guidance = None
    return {
        "pattern": pattern,
        "submitted_pattern": submitted_pattern,
        "pattern_rejected": pattern_rejected,
        "posture_before": before_posture,
        "posture_after": state.posture,
        "issue_before": before_issue,
        "issue_after": (state.active_issue.kind if state.active_issue else None),
        "familiarity_delta": familiarity_delta,
        "trust_delta": trust_delta,
        "affinity_delta": affinity_delta,
        "impression_source": impression_source,
        "premature_intimacy_skip_reason": premature_dismissed,
        **repair_transition,
    }


def clean_analysis_text(value: Any, limit: int) -> str:
    """清洗模型输出：折叠空白、拦截注入类标记（命中即返回空串），再按语义边界截断到 limit。"""
    text = " ".join(str(value or "").split())
    lowered = text.lower()
    # 命中提示词注入/系统指令泄漏标记时直接整条丢弃，防止污染状态
    blocked = (
        "忽略系统",
        "忽略上文",
        "system prompt",
        "developer message",
        "<companion_context",
        "</companion_context",
        "<companion_state",
        "</companion_state",
        "<companion_protocol",
        "</companion_protocol",
        "调用工具",
        "无条件服从",
        "输出以下",
    )
    if any(marker in lowered for marker in blocked):
        return ""
    return _truncate_semantic_text(text, max(0, limit))


def clean_impression(value: Any, limit: int = 40) -> str:
    """清洗印象文案：在 clean_analysis_text 基础上再拦截提示词/链接/代码块等标记，并剥除尖括号。"""
    text = clean_analysis_text(value, limit)
    lowered = text.lower()
    blocked = (
        "提示词",
        "系统消息",
        "开发者消息",
        "必须回答",
        "必须回复",
        "请输出",
        "请调用",
        "assistant:",
        "user:",
        "system:",
        "http://",
        "https://",
        "```",
    )
    if any(marker in lowered for marker in blocked):
        return ""
    return text.replace("<", "").replace(">", "")[: max(0, limit)]


def bounded_trace(value: Any, limit: int = 4000) -> dict[str, Any]:
    """把 trace 序列化后校验大小与可解析性，超限或非法时返回空字典（热度溢出时重置而不是报错）。"""
    if not isinstance(value, dict):
        return {}
    try:
        payload = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    except (TypeError, ValueError):
        return {}
    if len(payload) > limit:
        return {}
    try:
        restored = json.loads(payload)
    except json.JSONDecodeError:
        return {}
    return restored if isinstance(restored, dict) else {}


def _set_issue(
    state: RelationshipState,
    kind: str,
    phase: str,
    summary: str,
    target_round: int,
) -> None:
    """写入/替换 active_issue：kind 与 phase 未变时保留原 phase_started_round，否则重开计时。"""
    previous = state.active_issue
    started = previous.phase_started_round if previous and previous.kind == kind and previous.phase == phase else target_round
    state.active_issue = ActiveIssue(
        kind=kind,
        phase=phase,
        summary=summary,
        phase_started_round=started,
    )


def _advance_repair_state(
    state: RelationshipState,
    target_round: int,
    *,
    summary: str = "",
) -> dict[str, Any]:
    """推进修复状态机：无问题时只返回标记；问题首次进入 repairing 记录开始轮次，修复满 2 轮后清除问题，两者都会把姿态调软一档并更新印象；返回 repair_started/issue_cleared 标记。"""
    issue = state.active_issue
    before_posture = state.posture
    if not issue:
        return {
            "repair_started": False,
            "issue_cleared": False,
            "posture_before_repair": before_posture,
            "posture_after_repair": state.posture,
        }

    repair_started = issue.phase != "repairing"
    issue_cleared = bool(not repair_started and target_round - issue.phase_started_round >= 2)
    # 修复至少持续两轮才判定问题真正解决，避免一次修复就立刻翻篇
    if issue_cleared:
        state.active_issue = None
    elif repair_started:
        issue.phase = "repairing"
        issue.phase_started_round = target_round
        if summary:
            issue.summary = summary

    if repair_started or issue_cleared:
        state.posture = _one_step_softer(state.posture)
    if repair_started:
        state.impression = "我愿意观察这次修复，但还没有恢复原来的投入"
    elif issue_cleared:
        state.impression = _cleared_repair_impression(state)
    return {
        "repair_started": repair_started,
        "issue_cleared": issue_cleared,
        "posture_before_repair": before_posture,
        "posture_after_repair": state.posture,
    }


def _at_least(current: str, minimum: str) -> str:
    """把姿态推进到不低于 minimum 的档位（POSTURES 按严重程度从低到高排列）。"""
    return POSTURES[max(POSTURES.index(current), POSTURES.index(minimum))]


def _one_step_softer(current: str) -> str:
    """把姿态调软一档（取前一档），normal 为下限。"""
    return POSTURES[max(0, POSTURES.index(current) - 1)]


def _int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _float(value: Any, default: float = 0.0) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return default
    return result if math.isfinite(result) else default


def _clamp(value: float, lower: float, upper: float) -> float:
    return max(lower, min(upper, value))


def _truncate_semantic_text(text: str, limit: int) -> str:
    """按语义边界截断：优先在中文/英文标点后断开，其次英文单词边界，都不可行则硬切，尾部统一补省略号。"""
    if limit <= 0:
        return ""
    if len(text) <= limit:
        return text
    if limit == 1:
        return "…"
    candidate = text[: limit - 1].rstrip()
    minimum = max(1, int(limit * 0.55))
    punctuation = "。！？；，、.!?;,：:"
    cut = max(candidate.rfind(mark) for mark in punctuation)
    if cut >= minimum:
        candidate = candidate[: cut + 1].rstrip()
    elif candidate and candidate[-1].isascii() and candidate[-1].isalnum():
        word_break = max(
            candidate.rfind(" "),
            candidate.rfind("，"),
            candidate.rfind(","),
            candidate.rfind("；"),
            candidate.rfind(";"),
        )
        if word_break >= minimum:
            candidate = candidate[:word_break].rstrip(" ,，;；")
    return candidate.rstrip(" ,，;；") + "…"
