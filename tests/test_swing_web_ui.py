from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
HTML = (REPO_ROOT / "web" / "index.html").read_text(encoding="utf-8")
APP = (REPO_ROOT / "web" / "app.js").read_text(encoding="utf-8")
STYLES = (REPO_ROOT / "web" / "styles.css").read_text(encoding="utf-8")


def test_primary_page_explains_scope_and_has_private_empty_state() -> None:
    for phrase in (
        "核心仓不动",
        "默认 20% 只是可编辑的历史模拟假设，不是建议配置",
        "日线中低频，数周到数月",
        "再等待后续日线跟随",
        "无券商、无自动交易",
        "只在本机保存",
        "不会写入 GitHub",
    ):
        assert phrase in HTML
    assert 'id="holdings-empty"' in HTML
    assert "还没有本机持仓数据" in HTML
    assert "999999" in HTML
    # The static page starts empty and must not seed a personal record.
    assert 'class="holding-card"' not in HTML
    assert 'holdings: []' in APP


def test_personal_api_paths_and_local_token_are_wired() -> None:
    for route in (
        'api("/api/holdings"',
        '"/api/swing-results"',
        '"/api/swing-action"',
        '`/api/holdings/${encodeURIComponent',
    ):
        assert route in APP
    assert '"X-Local-Token": token' in APP
    assert 'method: editing ? "PUT" : "POST"' in APP
    assert 'method: "DELETE"' in APP
    assert "expected_revision" in APP
    assert "if (!editing) payload.expected_revision = 0;" in APP
    assert 'body = { action }' in APP
    assert 'data-swing-action="analyze"' in APP
    assert 'data-swing-action="backtest"' in APP
    assert 'data-swing-action="all"' in HTML
    assert "更新全部判断与模拟" in HTML
    assert 'data-folder="private_reports"' in HTML
    assert 'item.mode === "backtest"' in APP


def test_ui_uses_observation_labels_and_delete_confirmation() -> None:
    for label in ("数据不足", "低位观察", "低位反转信号", "中性", "高位观察", "高位转弱信号"):
        assert label in APP
    for label in ("更新阶段判断", "历史模拟"):
        assert label in APP
    assert "固定扰动方向一致" in APP
    assert "不证明未来有效" in APP
    assert "window.confirm" in APP
    assert "阶段判断不是买卖指令" in HTML
    for direct_label in (">买入<", ">卖出<", ">加仓<", ">减仓<", ">清仓<", ">下单<"):
        assert direct_label not in HTML
        assert direct_label not in APP


def test_task_busy_state_disables_personal_actions_and_handles_errors_in_chinese() -> None:
    assert "function setTaskBusy(busy)" in APP
    assert "function waitForTask(taskId)" in APP
    assert "payload.task.task_id" in APP
    assert "button.disabled = busy" in APP
    assert "本机访问令牌无效" in APP
    assert "无法连接本地服务" in APP
    assert "可用的收盘日线数据不足" in APP


def test_mobile_layout_prevents_long_names_and_keeps_legacy_tools_collapsed() -> None:
    assert '<details class="legacy-tools"' in HTML
    assert "@media (max-width: 650px)" in STYLES
    assert "overflow-wrap: anywhere" in STYLES
    assert ".holding-title h3" in STYLES and "min-width: 0" in STYLES
    assert ".holding-grid" in STYLES
    assert '[data-ui-action="add-holding"]' in STYLES
    assert "white-space: nowrap" in STYLES
