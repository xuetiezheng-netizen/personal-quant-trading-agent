from __future__ import annotations

import subprocess
import textwrap
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


def test_swing_cards_show_public_source_labels_and_automatic_switch() -> None:
    for mapping in (
        'eastmoney: "东方财富"',
        'tencent: "腾讯"',
        'baostock: "BaoStock"',
        'tushare: "Tushare Pro"',
        'adata: "AData（同花顺）"',
        'failover: "自动线路"',
    ):
        assert mapping in APP
    for phrase in ("本次来源：", "已自动切换", "本次未取得有效日线", "尝试过："):
        assert phrase in APP
    # The same source summary is rendered on both the holding and result cards.
    assert APP.count("sourceSummary(result)") >= 2


def test_swing_source_display_supports_legacy_results_and_hides_provider_details() -> None:
    assert "Array.isArray(result.source_attempts)" in APP
    assert "Older results predate source_attempts" in APP
    assert "data_source" in APP
    assert "data_source is authoritative" in APP
    # reason_code is intentionally never copied into the UI-safe attempt object.
    assert "reason_code" not in APP


def test_task_busy_state_disables_personal_actions_and_handles_errors_in_chinese() -> None:
    assert "function setTaskBusy(busy)" in APP
    assert "function waitForTask(taskId)" in APP
    assert "payload.task.task_id" in APP
    assert "button.disabled = busy" in APP
    assert "本机访问令牌无效" in APP
    assert "无法连接本地服务" in APP
    assert "可用的收盘日线数据不足" in APP
    assert "本次更新失败，页面保留上次结果" in APP


def test_web_helpers_runtime_handle_sources_stale_results_and_missing_values() -> None:
    script = textwrap.dedent(
        """
        const assert = require("assert");
        const fs = require("fs");
        const vm = require("vm");
        const source = fs.readFileSync(process.argv[1], "utf8");
        const marker = "\\n  bindEvents();";
        const cut = source.lastIndexOf(marker);
        assert(cut > 0);
        const instrumented = `${source.slice(0, cut)}
          globalThis.__swingTest = {
            sourceSummary,
            lastUpdateNotice,
            renderAnalysisDetails,
            formatPercent,
            finiteMetric,
            simulationMetric,
            renderSimulationExplanation,
            detailKey,
            rerenderWithOpenDetails,
          };
        })();`;
        const context = { window: { __LOCAL_TOKEN__: "" }, document: {} };
        vm.runInNewContext(instrumented, context, { filename: "web/app.js" });
        const {
          sourceSummary,
          lastUpdateNotice,
          renderAnalysisDetails,
          formatPercent,
          finiteMetric,
          simulationMetric,
          renderSimulationExplanation,
          detailKey,
          rerenderWithOpenDetails,
          } = context.__swingTest;

        assert.strictEqual(formatPercent(null), "未提供");
        assert.strictEqual(formatPercent(undefined), "未提供");
        assert.strictEqual(formatPercent(""), "未提供");
        assert.strictEqual(finiteMetric(null), null);
        assert.strictEqual(finiteMetric(undefined), null);
        assert.strictEqual(finiteMetric(""), null);
        assert.strictEqual(simulationMetric(null), "未提供");

        const switched = {
          status: "ok",
          data_source: "adata",
          data_as_of: "2025-05-30",
          bars_available: 150,
          source_attempts: [
            { source: "eastmoney", status: "failed" },
            { source: "adata", status: "success" },
          ],
        };
        assert.strictEqual(
          sourceSummary(switched),
          "本次来源：AData（同花顺）（自动切换成功；此前东方财富失败）"
        );

        const allFailed = {
          status: "error",
          data_source: "failover",
          bars_available: 0,
          source_attempts: [
            { source: "eastmoney", status: "failed" },
            { source: "adata", status: "failed" },
          ],
        };
        assert.strictEqual(
          sourceSummary(allFailed),
          "本次未取得有效日线（尝试过：东方财富、AData（同花顺））"
        );

        const stale = {
          status: "ok",
          state: "NEUTRAL",
          data_source: "adata",
          data_as_of: "2025-05-30",
          generated_at: "2026-09-01T16:00:00+08:00",
          last_update_attempt: {
            status: "error",
            generated_at: "2026-09-02T16:00:00+08:00",
          },
        };
        const staleMarkup = lastUpdateNotice(stale);
        assert(staleMarkup.includes("本次更新失败，以下为上次成功结果"));
        assert(staleMarkup.includes("生成时间 2026-09-01T16:00:00+08:00"));
        assert(staleMarkup.includes("数据截止 2025-05-30"));

        const missingMarkup = renderAnalysisDetails({
          state: "NEUTRAL",
          analysis_explanation: {
            analysis_flow: [],
            low_watch: { evaluated: false, pass: false, conditions: [] },
            high_watch: { evaluated: false, pass: false, conditions: [] },
            trend_environment: {},
            confirmation_path: {},
            conclusion: {},
            model_boundary: {},
            indicator_snapshot: { rsi: null },
          },
        });
        assert(missingMarkup.includes("未评估"));
        assert(!/undefined|nan/i.test(missingMarkup));

        const missingSimulationMarkup = renderSimulationExplanation({});
        assert(missingSimulationMarkup.includes("无法比较"));
        assert(missingSimulationMarkup.includes("稳健性样本不足或未提供"));
        assert(!missingSimulationMarkup.includes("0.0%"));
        assert(!missingSimulationMarkup.includes("4/4"));
        assert(!/undefined|nan/i.test(missingSimulationMarkup));

        const incompleteRobustnessMarkup = renderSimulationExplanation({
          robustness: { status: "ok", direction_consistent_count: 4 },
        });
        assert(incompleteRobustnessMarkup.includes("稳健性样本不足或未提供"));
        assert(!incompleteRobustnessMarkup.includes("4/4"));

        const insufficientRobustnessMarkup = renderSimulationExplanation({
          robustness: { status: "data_insufficient", direction_consistent_count: 4, direction_total: 4 },
        });
        assert(insufficientRobustnessMarkup.includes("稳健性样本不足或未提供"));
        assert(!insufficientRobustnessMarkup.includes("4/4"));

        function detailsContainer(markup) {
          const container = { nodes: [], querySelectorAll: () => container.nodes };
          Object.defineProperty(container, "innerHTML", {
            get: () => container.markup,
            set: (value) => {
              container.markup = String(value || "");
              const nodes = [];
              const tags = /<details\\b([^>]*)>/g;
              let match;
              while ((match = tags.exec(container.markup)) !== null) {
                const keyMatch = match[1].match(/data-detail-key="([^"]*)"/);
                if (!keyMatch) continue;
                nodes.push({
                  dataset: { detailKey: keyMatch[1] },
                  open: /\\bopen\\b/.test(match[1]),
                });
              }
              container.nodes = nodes;
            },
          });
          container.innerHTML = markup;
          return container;
        }

        const firstResult = { code: "159922", asset_type: "etf", mode: "analyze" };
        const otherResult = { code: "600000", asset_type: "stock", mode: "analyze" };
        const firstKey = detailKey(firstResult, "analysis");
        const otherKey = detailKey(otherResult, "analysis");
        assert.notStrictEqual(firstKey, otherKey);
        assert(!detailKey({ code: '<unsafe"text>' }, "analysis").includes("<"));
        const detailContainer = detailsContainer(
          `<details data-detail-key="${firstKey}" open></details><details data-detail-key="${otherKey}"></details>`
        );
        rerenderWithOpenDetails(
          detailContainer,
          () => `<details data-detail-key="${otherKey}"></details><details data-detail-key="${firstKey}"></details>`
        );
        const firstNode = detailContainer.nodes.find((node) => node.dataset.detailKey === firstKey);
        const otherNode = detailContainer.nodes.find((node) => node.dataset.detailKey === otherKey);
        assert(firstNode && firstNode.open);
        assert(otherNode && !otherNode.open);
        console.log("web helper runtime checks passed");
        """
    )
    result = subprocess.run(
        ["node", "-e", script, str(REPO_ROOT / "web" / "app.js")],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    assert result.returncode == 0, result.stderr or result.stdout
    assert "web helper runtime checks passed" in result.stdout


def test_detailed_analysis_and_backtest_explanations_are_rendered_without_nan() -> None:
    for phrase in (
        "主要依据",
        "研究建议",
        "查看完整分析过程与依据",
        "低位观察三项条件",
        "高位观察三项条件",
        "实际指标",
        "为什么不是低位",
        "下一次重点观察",
        "严格失效规则",
        "not_a_trade_instruction",
        "查看模拟过程与结论",
        "为什么不能外推未来",
        "safeDisplayValue",
    ):
        assert phrase in APP
    assert 'id="simulation-detail"' in HTML
    assert "4/4" not in APP
    assert "Number.isFinite" in APP
    assert 'data-detail-key="' in APP
    assert 'querySelectorAll("details[data-detail-key]")' in APP
    assert "undefined" in APP
    assert "NaN" not in APP


def test_mobile_layout_prevents_long_names_and_keeps_legacy_tools_collapsed() -> None:
    assert '<details class="legacy-tools"' in HTML
    assert "@media (max-width: 650px)" in STYLES
    assert "overflow-wrap: anywhere" in STYLES
    assert ".holding-title h3" in STYLES and "min-width: 0" in STYLES
    assert ".holding-grid" in STYLES
    assert '[data-ui-action="add-holding"]' in STYLES
    assert "white-space: nowrap" in STYLES
