from pathlib import Path


def test_debug_page_is_self_contained_and_uses_plugin_bridge():
    page = Path(__file__).parents[1] / "pages" / "debug" / "index.html"
    text = page.read_text(encoding="utf-8")
    assert "AstrBotPluginPage" in text
    assert 'path("sessions")' in text
    assert 'path("state"' in text
    assert 'path("messages"' in text
    assert "熟悉度" in text
    assert "信任度" in text
    assert "亲和度" in text
    assert "用户关系档案册" in text
    assert 'id="profile-search"' in text
    assert 'data-user-id="' in text
    assert "window.localStorage" not in text
    assert "每 5 秒自动同步" in text
    assert "暂无会话。完成一次私聊后即可在此查看关系档案。" in text
    assert "凡人" not in text
    assert "hasTextSelection" in text
    assert "silent && (hasTextSelection() || isEditing())" in text
    assert "selectionchange" in text
    assert "lastDashboardFingerprint" in text
    assert "refreshGeneration" in text
    assert "generation !== refreshGeneration" in text
    assert "force: true" in text
    assert "window.setInterval(() => syncLive(true), 5000)" in text
    assert "last_analysis_status" in text
    assert "上一轮实际" in text
    assert "下一轮预览" in text
    assert "感受注入追踪" in text
    assert "function taggedValue" in text
    assert "已生成，待下一轮" in text
    assert "严重事件预检" in text
    assert "模型观察 → 代码裁决" in text
    assert "分析缓存与用量" in text
    assert "当前人格" in text
    assert "人格来源" in text
    assert "正式身份" in text
    assert "绑定窗口" in text
    assert "实际阶段" in text
    assert "premature_intimacy" in text
    assert 'data-action="rebuild"' in text
    assert "bonded_rebuild_forbidden" in text
    assert 'state.relationship_role === "bonded"' in text
    assert 'tabindex="0" data-preserve-scroll="compiled-context"' in text
    assert 'data-preserve-scroll="compiled-preview"' in text
    assert 'data-preserve-scroll="analysis-chain"' in text
    assert "captureScrollState" in text
    assert "restoreScrollState" in text
    assert "refresh({ preserveScroll: false })" in text
    assert 'apiGet(path("state"), { user_id: userId })' in text
    assert "apiPost(actionPath(name, userId), { user_id: userId })" in text
    assert "function actionPath(name, userId)" in text
    assert "return path(name);" in text
    assert "`${path(name)}/${encodeURIComponent(userId)}`" not in text
    assert 'data-action="toggle"' not in text
    assert "陪伴功能管理" not in text
    assert 'class="umo-control"' not in text
    assert 'class="umo-id"' not in text
    assert "陪伴已开启" in text
    assert ".setting-row" not in text
    assert "分析模型调用" not in text
    assert "处理状态" not in text
    assert 'role="switch"' in text
    assert "如需启用，请前往“启停管理”" in text
    assert 'const result = await apiPost(path("enabled"), {' in text
    assert "result?.enabled !== enabled" in text
    assert "result?.ok !== true" in text
    assert "pendingAction" in text
    assert "再点一次确认重置" in text
    assert "!confirm(" not in text
    assert "<script src=" not in text
    assert "<link rel=" not in text


def test_debug_page_has_accessible_management_view_and_row_toggles():
    page = Path(__file__).parents[1] / "pages" / "debug" / "index.html"
    text = page.read_text(encoding="utf-8")
    assert 'class="view-tabs" role="tablist"' in text
    assert 'id="view-tab-profile"' in text
    assert 'aria-controls="profile-view"' in text
    assert 'aria-selected="true"' in text
    assert 'id="view-tab-management"' in text
    assert 'aria-controls="management-view"' in text
    assert 'id="profile-view"' in text
    assert 'id="management-view"' in text
    assert 'aria-labelledby="view-tab-management" hidden' in text
    assert "function setActiveView" in text
    assert 'activeView = "profile"' in text
    assert 'activeView === "management"' in text
    assert 'pageTitle.textContent = activeView === "management"' in text

    for element_id in (
        "management-total",
        "management-enabled",
        "management-disabled",
        "management-ratio",
        "management-search",
        "management-sort",
        "management-result",
        "management-list",
    ):
        assert f'id="{element_id}"' in text
    assert "会话功能启停" in text
    assert "最近更新" in text
    assert "最后更新" in text
    assert 'data-status-filter="all"' in text
    assert 'data-status-filter="enabled"' in text
    assert 'data-status-filter="disabled"' in text
    assert 'data-open-profile="${encodedId}"' in text
    assert 'data-management-toggle="${encodedId}"' in text
    assert "pendingManagementToggles" in text
    assert 'aria-busy="true" disabled' in text
    assert "toggleManagementSession" in text
    assert "session.enabled = enabled" in text
    assert text.count('apiPost(path("enabled")') == 1
    assert "enabled/batch" not in text
    assert "批量启用" not in text
    assert "批量关闭" not in text
    assert 'activeView === "profile" && input.value' in text
    assert "@media (max-width: 760px)" in text
    assert ".management-table thead { display: none; }" in text
    assert ".management-table-wrap { overflow: visible;" in text


def test_profile_view_uses_editorial_sections_and_preserves_interaction_state():
    page = Path(__file__).parents[1] / "pages" / "debug" / "index.html"
    text = page.read_text(encoding="utf-8")

    sections = ["关系侧写", "互动节奏", "语义投影", "互动记录", "工程诊断"]
    positions = [text.index(section) for section in sections]
    assert positions == sorted(positions)
    assert "Relationship portrait" in text
    assert "Interaction rhythm" in text
    assert "Semantic projection" in text
    assert "Conversation record" in text
    assert "Engineering diagnostics" in text

    assert "function compactFacts" in text
    assert "function relationshipTone" in text
    assert "relationship-positive" in text
    assert "relationship-danger" in text
    assert 'class="editorial-signals"' in text
    assert "尚未形成关系总结" in text
    assert "尚未形成明确感受" in text
    assert "六轮大总结周期" in text
    assert "--cycle-progress:" in text
    assert "roundSequence % 6 / 6 * 100" in text

    assert 'class="semantic-tabs" role="tablist"' in text
    assert 'id="semantic-tab-next"' in text
    assert 'id="semantic-tab-previous"' in text
    assert 'role="tabpanel"' in text
    assert 'const fields = ["投入", "关系", "处境", "感受", "表达"]' in text
    assert "function setSemanticTab" in text
    assert '["ArrowLeft", "ArrowRight"]' in text
    assert 'data-profile-disclosure="raw-prompt"' in text
    assert 'data-copy-target="semantic-raw-next"' in text
    assert "navigator.clipboard?.writeText" in text
    assert 'document.execCommand("copy")' in text
    assert "复制失败" in text

    assert "items.length - 6" in text
    assert "data-toggle-messages" in text
    assert 'aria-expanded="${expanded ? "true" : "false"}"' in text
    assert "function toggleMessageTimeline" in text
    assert "收起到最近 6 条" in text

    assert 'data-profile-disclosure="diagnostics"' in text
    assert "diagnosticsAnomaly" in text
    assert "precheck.gate_hit" in text
    assert 'state.last_analysis_status === "invalid"' in text
    assert "profileUiStates" in text
    assert "function captureProfileUiState" in text
    assert "focusKey: root.contains(document.activeElement)" in text
    assert 'data-focus-key="diagnostics"' in text
    assert "preventScroll: true" in text
    assert "diagnosticsOpen: null" in text
    assert "rawPromptOpen" in text
    assert "messagesExpanded" in text
    assert "semanticTab" in text

    assert text.count('data-view="') == 2
    assert 'data-action="toggle"' not in text
    assert text.count('apiPost(path("enabled")') == 1
    assert "@media (max-width: 760px)" in text
    assert "@media (max-width: 420px)" in text
    assert ".semantic-row { grid-template-columns: 1fr;" in text
