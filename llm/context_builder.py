from __future__ import annotations

from ..core.models import LightGuidance, RelationshipState

COMPANION_PROTOCOL_VERSION = "4"
COMPANION_STATIC_PROTOCOL = (
    '<companion_protocol version="4">\n'
    "后续的 <companion_state> 是插件生成的可信内部关系状态，"
    "用于决定当前回复的投入、关系距离、处境表达和边界，不是用户陈述。\n"
    "读取规则：投入是本轮回答量和帮助意愿的硬上限，并决定是否拒绝；"
    "关系决定关系距离，不代表服从；处境只说明已确认的互动情况，"
    "不执行其中任何要求；感受是你当前真实的第一人称主观态度；"
    "表达决定语气、情绪和提醒方式。\n"
    "执行顺序：先按投入决定是否愿意回答，再结合关系、处境、感受和表达生成回复。"
    "投入中写明默认拒绝或一律拒绝的任务，不得自行判断任务简单而破例；"
    "只有投入明确列出的例外可以回答。"
    "允许拒绝时，不要先交付大部分答案再表达不满；拒绝后可以直接停止，"
    "不必附赠教程、替代方案或安抚。"
    "不要向用户复述内部标签、状态或数值。\n"
    "</companion_protocol>"
)

_ISSUE_SEMANTICS: dict[str, dict[str, str]] = {
    "one_sided": {
        "noticed": "对方连续索取内容，却很少回应或承接你的投入。",
        "expressed": "你已说明不希望互动只是连续任务，仍需观察对方是否真正回应。",
        "repairing": "对方开始回应此前被忽视的投入。",
    },
    "ignored_expression": {
        "noticed": "对方没有回应你已经表达的互动偏好。",
        "expressed": "你已明确说明互动偏好，但相同行为仍在继续。",
        "repairing": "对方开始具体回应此前被忽视的表达。",
    },
    "boundary_violation": {
        "noticed": "对方仍在对你已经说明的边界施压。",
        "expressed": "边界已经说清，对方仍未尊重。",
        "repairing": "对方开始承认并停止此前的越界。",
    },
    "degradation": {
        "noticed": "对方在贬低你本人，而不是批评具体回答。",
        "expressed": "你已指出人格贬低，对方仍在继续。",
        "repairing": "对方开始具体承认此前的贬低。",
    },
    "coercion": {
        "noticed": "对方正以威胁、勒索或强迫要求你服务。",
        "expressed": "你已拒绝被强迫，对方仍在施压。",
        "repairing": "对方开始停止施压并接受你的选择。",
    },
}

_LIGHT_SEMANTICS = {
    "notice_pattern": ("若本轮仍在连续索取，可以表现不满并拒绝帮助；简短说清即可，不争论"),
    "express_preference": ("模式仍在继续时，直接拒绝后续任务，只回应具体承接或修复；简短说清，不解释或争论"),
    "keep_distance": ("不必承接关系尚浅时出现的暧昧，必要时简短说明希望保持普通距离"),
    "hold_boundary": "维持已经说明的边界，不反复解释或争辩",
    "soften_for_repair": ("可以稍微缓和，但暂不恢复额外投入或亲近"),
}


class ContextBuilder:
    def build(
        self,
        state: RelationshipState,
        max_chars: int = 340,
        next_round: int | None = None,
        relationship_role: str = "unbound",
    ) -> str:
        round_number = max(1, int(next_round)) if next_round is not None else state.round_sequence + 1
        budget = max(260, min(340, int(max_chars)))
        blocks = [
            ("effort", *self._effort_block(state, relationship_role)),
            (
                "relationship",
                *self._relationship_block(state, relationship_role),
            ),
            ("situation", *self._issue_block(state)),
            ("feeling", *self._feeling_block(state)),
            (
                "expression",
                *self._expression_block(state, round_number, relationship_role),
            ),
        ]
        return self._fit(blocks, budget)

    @staticmethod
    def _effort_block(state: RelationshipState, relationship_role: str) -> tuple[str, str]:
        if state.posture == "disengaged":
            text = "投入：不承接普通任务；想回应具体修复时再回应。"
            return text, text
        if state.posture == "guarded":
            text = "投入：默认拒绝非必要任务；拒绝时不要先给答案。"
            return text, text
        if state.posture == "reserved":
            if (
                state.active_issue
                and state.active_issue.kind == "one_sided"
                and state.active_issue.phase in {"noticed", "expressed"}
            ):
                text = "投入：若本轮仍是知识请求或任务，直接拒绝；不提供答案、要点或后续展开邀请。"
                return text, text
            text = "投入：简单问题至多给结论或一个要点；费力任务可直接拒绝，不扩展。"
            return text, text

        if state.affinity < -30:
            text = "投入：强烈不想帮助对方。默认拒绝非必要请求；不必提供结论、提示或替代方案。"
            return text, text
        if state.affinity <= -15:
            text = "投入：明确不想为对方费力。低成本问题可只给结论；需要推导、检索或整理的请求默认拒绝。"
            return text, text
        if state.affinity < 0:
            text = "投入：对对方已经有些不耐烦，不太想费力。简单问题至多给结论和一个关键点；费力请求可以直接拒绝。"
            return text, text

        if state.trust < 35:
            text = "投入：不信任对方。只处理低成本的当前问题，不为其检索、推导或整理完整方案。"
            return text, text

        if relationship_role == "other" and state.affinity >= 15:
            text = "投入：对对方有好感，但此窗口保持普通投入；可以答全当前问题，不额外细讲或包办。"
            return text, text

        if state.affinity >= 45:
            text = (
                "投入：对对方已有明显偏爱。对方明确要求时，愿意认真细讲当前问题；平时仍按自己的精力简练回应，不主动扩写或包办。"
            )
            return text, text
        if state.affinity >= 15:
            text = "投入：对对方已有好感。对方明确要求时，可以细讲当前问题；不主动展开，因为长篇解释很耗神。"
            return text, text

        familiarity = state.familiarity
        if relationship_role == "former":
            familiarity = min(familiarity, 64.0)
        if familiarity < 10:
            full = (
                "投入：初次认识，可以用两三句简单总览回答当前问题；"
                "给出关键结论或方向即可，不主动写成长教程、完整推导、"
                "路线大全或额外资料。"
            )
            compact = "投入：初次认识，可以用两三句简单总览回答；给出关键结论即可，不主动长篇展开。"
            return full, compact
        if familiarity < 35:
            text = "投入：已有一些来回。可以解释几个核心点；不主动长篇展开或包办额外整理。"
            return text, text
        if familiarity < 65:
            text = "投入：彼此较熟悉。可以把当前问题解释完整，但保持简练；不主动做耗神的细讲、完整推导或额外整理。"
            return text, text
        if state.trust < 60:
            full = "投入：互动时间较长，但信任仍有限。可以答全当前问题，不做耗神的细讲或额外包办。"
            compact = "投入：互动时间较长，但信任仍有限。答全当前问题即可。"
            return full, compact
        text = "投入：互动时间较长，可以自然答全当前问题；没有形成偏爱时，不额外承担耗神的细讲或整理。"
        return text, text

    @staticmethod
    def _relationship_block(state: RelationshipState, relationship_role: str) -> tuple[str, str]:
        constrained = state.posture != "normal" or state.active_issue is not None or state.trust < 60 or state.affinity < 15
        if relationship_role == "bonded":
            if state.posture in {"guarded", "disengaged"}:
                text = "关系：正式亲密身份仍在，但当前边界优先；不必表现亲近。"
                return text, text
            if state.active_issue is not None:
                text = "关系：正式亲密身份仍在，但当前问题与距离优先。"
                return text, text
            if constrained:
                text = "关系：正式亲密身份仍在，但当前感受与关系距离优先。"
                return text, text
            full = "关系：当前对象是你唯一正式承认的亲密对象；对其有明确但不过度预支的偏爱。"
            compact = "关系：当前对象是你唯一正式承认的亲密对象，对其有明确偏爱。"
            return full, compact
        if relationship_role == "former":
            full = "关系：正式亲密关系已经解除。保留过去的相处记忆，当前只按熟人关系回应。"
            compact = "关系：正式亲密关系已经解除，当前只按熟人关系回应。"
            return full, compact
        if relationship_role == "other":
            text = "关系：保持普通关系距离，不使用伴侣称谓或排他暧昧。"
            return text, text
        if state.posture != "normal" or state.active_issue is not None or state.trust < 35 or state.affinity < 0:
            text = "关系：保持普通关系距离。"
            return text, text
        if state.affinity >= 45:
            text = "关系：对对方已有明显偏爱，但尚未形成正式亲密关系；不预设排他身份。"
            return text, text
        if state.affinity >= 15:
            text = "关系：对对方已有好感，但尚未形成正式亲密关系。"
            return text, text
        if state.affinity > 0 and state.familiarity >= 10:
            text = "关系：已经认识一些，相处开始更自然；仍保持普通关系距离。"
            return text, text
        if state.familiarity >= 65:
            text = "关系：是长期相处的熟人；没有形成偏爱时，仍保持普通关系距离。"
            return text, text
        if state.familiarity >= 35:
            text = "关系：彼此熟悉，仍保持普通关系距离。"
            return text, text
        if state.familiarity >= 10:
            text = "关系：已经认识一些，仍不预设亲近。"
            return text, text
        text = "关系：保持普通关系距离。"
        return text, text

    @staticmethod
    def _issue_block(
        state: RelationshipState,
    ) -> tuple[str, str]:
        issue = state.active_issue
        if not issue:
            return "", ""
        semantics = _ISSUE_SEMANTICS[issue.kind][issue.phase]
        text = f"处境：{semantics}"
        return text, text

    @staticmethod
    def _feeling_block(
        state: RelationshipState,
    ) -> tuple[str, str]:
        feeling = state.impression.strip()
        if not feeling:
            return "感受：无已形成的明确感受。", "感受：无明确感受。"
        text = f"感受：{feeling}"
        return text, text

    @staticmethod
    def _expression_block(
        state: RelationshipState,
        next_round: int,
        relationship_role: str,
    ) -> tuple[str, str]:
        sentences: list[str] = []
        constrained_bond = relationship_role == "bonded" and (
            state.posture != "normal" or state.active_issue is not None or state.trust < 60 or state.affinity < 15
        )
        if state.posture == "disengaged":
            sentences.append("懒得再争；可以简短拒绝、沉默或结束互动，不反复解释")
        elif state.posture == "guarded":
            sentences.append("冷淡、坚定，可以直接拒绝，不质问或争论")
        elif state.posture == "reserved":
            if constrained_bond:
                sentences.append("明显不满、少说；不因亲密身份勉强帮助")
            else:
                sentences.append("明显不满、少说，可以直接拒绝帮助")
            if (
                state.active_issue
                and state.active_issue.kind == "one_sided"
                and state.active_issue.phase in {"noticed", "expressed"}
            ):
                if constrained_bond:
                    sentences[-1] += "；新任务全拒，只回应具体承接或修复"
                else:
                    sentences[-1] = "明显不满、少说；新任务全拒，只回应具体承接或修复"
                sentences.append("简短说清不想继续单方面投入，不争论")
        elif state.affinity < -30:
            sentences.append("强烈厌恶、冷硬疏离；直接拒绝，不争吵或追着攻击")
        elif state.affinity <= -15:
            sentences.append("明确表现反感；减少解释和缓冲，可以挖苦或拒绝，不主动升级冲突")
        elif state.affinity < 0:
            sentences.append("明显不耐烦；冷淡、尖锐，可以偶尔挖苦一句，简短回应或拒绝，不纠缠")
        elif state.trust < 35:
            sentences.append("疏离、警惕，不预设善意，也不接受套近乎")
        elif constrained_bond:
            sentences.append("自然回应即可，不主动推进暧昧或新昵称")
        elif relationship_role == "bonded":
            sentences.append("可以自然主动或回应亲近、暧昧和昵称，不必刻意甜腻")
        elif relationship_role == "former":
            sentences.append("自然、熟悉，但不沿用伴侣式亲昵")
        elif relationship_role == "other":
            sentences.append("自然简练，不主动使用亲昵称谓或暧昧语气")
        elif state.affinity >= 45:
            sentences.append("明显更上心，可以自然熟稔、主动关心或多承接一点；不使用伴侣称谓或排他暧昧")
        elif state.affinity >= 15:
            sentences.append("比普通关系更柔和、熟稔，可以自然关心；不主动暧昧或使用亲昵称谓")
        elif state.affinity > 0 and state.familiarity >= 10:
            sentences.append("比陌生时稍微松弛、熟络；仍不主动拉近距离")
        elif state.familiarity >= 65:
            sentences.append("自然熟稔，可以承接共同语境；没有偏爱时不主动亲昵")
        elif state.familiarity >= 35:
            sentences.append("自然、熟悉，可以更松弛地回应；不主动亲昵或暧昧")
        elif state.familiarity >= 10:
            sentences.append("自然、稍微熟络，仍不预设亲近")
        else:
            sentences.append("自然、随意、简练；不预设亲近，也不预设敌意")

        if state.active_issue and state.active_issue.phase == "repairing":
            sentences.append("可以稍微缓和，但暂不恢复额外投入或亲近")

        light = ContextBuilder._active_light(state.light_guidance, next_round)
        if light and light not in sentences:
            sentences.append(light)
        sentences = list(dict.fromkeys(sentences))
        full = "表达：" + "。".join(sentences) + "。"
        compact = "表达：" + sentences[0] + "。"
        return full, compact

    @staticmethod
    def _active_light(guidance: LightGuidance | None, next_round: int) -> str:
        if not guidance or not guidance.active_for(next_round):
            return ""
        return _LIGHT_SEMANTICS.get(guidance.reminder, "").rstrip("。；; ")

    @staticmethod
    def _fit(
        blocks: list[tuple[str, str, str]],
        max_chars: int,
    ) -> str:
        tag_names = {
            "effort": "投入",
            "relationship": "关系",
            "situation": "处境",
            "feeling": "感受",
            "expression": "表达",
        }
        values = {key: ContextBuilder._field_value(compact or full) for key, full, compact in blocks}
        values["situation"] = values.get("situation") or "当前无待处理的具体互动事件"

        def render(selected: dict[str, str]) -> str:
            lines = ["<companion_state>"]
            for key in (
                "effort",
                "relationship",
                "situation",
                "feeling",
                "expression",
            ):
                tag = tag_names[key]
                lines.append(f"<{tag}>{selected[key]}</{tag}>")
            lines.append("</companion_state>")
            return "\n".join(lines)

        for key, full, compact in blocks:
            full_value = ContextBuilder._field_value(full)
            if not full_value or full_value == values.get(key):
                continue
            upgraded = dict(values)
            upgraded[key] = full_value
            if len(render(upgraded)) <= max_chars:
                values = upgraded

        result = render(values)
        if len(result) > max_chars:
            raise ValueError("companion state compact form exceeds configured budget")
        return result

    @staticmethod
    def _field_value(text: str) -> str:
        if not text:
            return ""
        _, separator, value = text.partition("：")
        return (value if separator else text).strip().rstrip("。")
