from __future__ import annotations

from .models import fallback_impression


class CommandsController:
    """私聊管理命令的实际实现；main 上的装饰方法仅做薄转发。"""

    def __init__(self, plugin) -> None:
        """持有插件引用以访问状态、人格、桥接与反思服务。"""
        self.plugin = plugin

    async def status(self, event):
        """查看当前私聊 UMO 的关系状态与最近语义投影。"""
        user_id = self.plugin._user_identity(event)
        state = await self.plugin._load_state(user_id)
        persona_resolution = await self.plugin.persona.resolve_persona_id(user_id)
        bond_debug = self.plugin.persona.bond_debug_payload(state, persona_resolution)
        issue = state.active_issue
        issue_text = f"{issue.kind}/{issue.phase}: {issue.summary or '-'}" if issue else "-"
        light = state.light_guidance
        light_text = f"{light.signal}/{light.reminder}，有效至第{light.expires_after_round}轮" if light else "-"
        yield event.plain_result(
            "CompanionLiteV2 状态\n"
            f"模式: {self.plugin.plugin_config.operation_mode}\n"
            f"用户: {user_id}\n"
            f"人格: {bond_debug['persona_id'] or '-'} "
            f"({bond_debug['persona_source'] or bond_debug['persona_error'] or '-'})\n"
            f"正式关系: {bond_debug['bond_status']}\n"
            f"轮次: {state.round_sequence}，最近深分析: {state.last_deep_round}\n"
            f"关系阶段: {state.relationship_stage}\n"
            f"三维: 熟悉度 {state.familiarity:.1f} / "
            f"信任 {state.trust:.1f} / 亲和 {state.affinity:.1f}\n"
            f"关系总结: {state.relationship_summary or '-'}\n"
            f"关系姿态: {state.posture}\n"
            f"当前问题: {issue_text}\n"
            f"轻提醒: {light_text}\n"
            f"主观印象: {state.impression or '-'}\n"
            f"{self.plugin.silence_bridge.silence_status_line(state)}\n"
            f"最近上下文实际注入: {'是' if state.last_context_injected else '否'}\n"
            f"最近编译文本:\n{state.last_compiled_context or '-'}"
        )

    async def bond(self, event):
        """将当前私聊窗口设为当前人格唯一的正式关系。"""
        user_id = self.plugin._user_identity(event)
        resolution = await self.plugin.persona.resolve_persona_id(user_id)
        if not resolution.persona_id:
            yield event.plain_result("这轮没认出我正在使用哪套人格，先别乱绑。")
            return
        async with self.plugin._bond_lock:
            result = self.plugin.storage.bind_persona(resolution.persona_id, user_id)
            status = str(result.get("status") or "")
            if status == "occupied":
                yield event.plain_result("这个位置已经有人了。要换，先在原来的窗口解除。")
                return
            if status == "already_bound":
                yield event.plain_result("这个位置本来就是你的，还确认什么。")
                return
            if status != "bound":
                yield event.plain_result("这次绑定没有落稳，先别把关系说死。")
                return
            async with self.plugin._response_lock(user_id):
                state = await self.plugin._load_state(user_id)
                was_former = state.former_bond
                state.former_bond = False
                if not was_former:
                    state.familiarity = max(state.familiarity, 35.0)
                    state.trust = max(state.trust, 60.0)
                    state.affinity = max(state.affinity, 15.0)
                if not state.impression:
                    state.impression = fallback_impression(state)
                self.plugin._save_state(state)
        yield event.plain_result("行，这个位置给你了。只有一个——以后怎么待我，自己掂量。")

    async def unbond(self, event):
        """解除当前窗口的正式关系，但保留既有相处状态。"""
        user_id = self.plugin._user_identity(event)
        resolution = await self.plugin.persona.resolve_persona_id(user_id)
        if not resolution.persona_id:
            yield event.plain_result("这个窗口没有可解除的关系。")
            return
        async with self.plugin._bond_lock:
            if not self.plugin.storage.unbind_persona(resolution.persona_id, user_id):
                yield event.plain_result("这个窗口没有可解除的关系。")
                return
            async with self.plugin._response_lock(user_id):
                state = await self.plugin._load_state(user_id)
                state.former_bond = True
                self.plugin._save_state(state)
        yield event.plain_result("关系名我收回了，发生过的事不清零。以后还是看你怎么待我。")

    async def reset(self, event):
        """重置当前私聊 UMO 的全部 CompanionLiteV2 数据。"""
        user_id = self.plugin._user_identity(event)
        await self.plugin.webui.reset_user(user_id)
        yield event.plain_result(f"已重置 CompanionLiteV2 独立状态: {user_id}")

    async def reflect(self, event):
        """立即尝试运行当前私聊 UMO 的深度关系分析。"""
        user_id = self.plugin._user_identity(event)
        if not self.plugin.storage.is_user_enabled(user_id):
            yield event.plain_result("CompanionLiteV2 此 UMO 已关闭，未调用分析模型")
            return
        state = await self.plugin._load_state(user_id)
        target_round = (state.round_sequence // 6) * 6
        if target_round <= 0:
            yield event.plain_result("CompanionLiteV2 深分析未执行：尚未攒满一个完整 6 轮周期")
            return
        ok = await self.plugin.reflection_service.perform(user_id, target_round, "deep")
        yield event.plain_result(
            "CompanionLiteV2 深分析已完成" if ok else "CompanionLiteV2 深分析未执行：没有完整来回或模型未返回有效结果"
        )
