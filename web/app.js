(function () {
  "use strict";

  const token = window.__LOCAL_TOKEN__ || "";
  const pollMs = 2000;
  let pollTimer = null;
  let lastTaskId = null;
  const pageState = {
    holdings: [],
    results: [],
    editingKey: null,
    busy: false,
    localBusy: false,
  };

  const $ = (selector) => document.querySelector(selector);
  const $$ = (selector) => Array.from(document.querySelectorAll(selector));

  const STATE_META = {
    DATA_INSUFFICIENT: {
      label: "数据不足",
      tone: "muted",
      explanation: "可用的完整日线还不够，暂不做阶段判断。",
    },
    LOW_WATCH: {
      label: "低位观察",
      tone: "teal",
      explanation: "价格进入相对低位区域，但仍需要后续收盘数据确认。",
    },
    BOTTOM_CONFIRMED: {
      label: "低位反转信号",
      tone: "teal-strong",
      explanation: "先进入低位观察区，后续日线出现向上跟随；不代表底部已经被证明。",
    },
    NEUTRAL: {
      label: "中性",
      tone: "neutral",
      explanation: "当前没有足够一致的高低位信号，继续按收盘节奏观察。",
    },
    HIGH_WATCH: {
      label: "高位观察",
      tone: "amber",
      explanation: "价格进入相对高位区域，留意后续收盘数据的变化。",
    },
    TOP_CONFIRMED: {
      label: "高位转弱信号",
      tone: "amber-strong",
      explanation: "先进入高位观察区，后续日线出现向下跟随；不代表顶部已经被证明。",
    },
  };

  function escapeHtml(value) {
    return String(value == null ? "" : value)
      .replaceAll("&", "&amp;")
      .replaceAll("<", "&lt;")
      .replaceAll(">", "&gt;")
      .replaceAll('"', "&quot;")
      .replaceAll("'", "&#039;");
  }

  function shortPath(value) {
    if (!value) return "尚未生成";
    const parts = String(value).split(/[\\/]/);
    return parts[parts.length - 1] || value;
  }

  function setConnection(online, text) {
    const pill = $("#connection-pill");
    if (!pill) return;
    pill.classList.toggle("online", online);
    pill.classList.toggle("offline", !online);
    pill.innerHTML = `<i></i>${escapeHtml(text)}`;
  }

  function friendlyError(error, status) {
    const message = error && error.message ? error.message : String(error || "");
    if (status === 401 || status === 403 || /令牌|token|forbidden/i.test(message)) {
      return "本机访问令牌无效，请关闭页面后重新双击 run-web.cmd。";
    }
    if (/数据不足|历史|日线|bars|history/i.test(message)) {
      return "可用的收盘日线数据不足，暂时无法完成阶段判断；请稍后重试。";
    }
    if (error && (error.name === "TypeError" || /Failed to fetch|NetworkError|网络/i.test(message))) {
      return "无法连接本地服务，请确认入口程序仍在运行。";
    }
    return message || "本地操作未完成，请查看页面提示。";
  }

  async function api(path, options) {
    const requestOptions = Object.assign({}, options || {});
    const headers = Object.assign({ "X-Local-Token": token }, requestOptions.headers || {});
    if (requestOptions.body != null) headers["Content-Type"] = "application/json";
    requestOptions.headers = headers;
    let response;
    try {
      response = await fetch(path, requestOptions);
    } catch (error) {
      throw new Error(friendlyError(error));
    }
    let data = {};
    try {
      data = await response.json();
    } catch (_error) {
      data = {};
    }
    if (!response.ok) {
      const error = new Error(data.error || `请求失败（${response.status}）`);
      error.status = response.status;
      throw error;
    }
    return data;
  }

  function setTaskBusy(busy) {
    pageState.busy = busy;
    $$(`[data-task], [data-folder], [data-swing-action], [data-holding-action], [data-ui-action="add-holding"], [data-ui-action="cancel-holding"]`).forEach((button) => {
      button.disabled = busy;
    });
    const form = $("#holding-form");
    if (form) {
      form.querySelectorAll("input, select, button").forEach((element) => {
        element.disabled = busy;
      });
      form.classList.toggle("is-busy", busy);
    }
  }

  function setLocalBusy(busy) {
    pageState.localBusy = busy;
    setTaskBusy(busy);
  }

  function setNotice(elementId, message) {
    const element = $(elementId);
    if (!element) return;
    element.textContent = message || "";
    element.classList.toggle("hidden", !message);
  }

  function renderLog(task) {
    const box = $("#log-box");
    const state = $("#log-state");
    const line = $("#result-line");
    const result = $("#result-path");
    if (!box || !state || !line || !result) return;
    if (!task) {
      state.textContent = "暂无任务";
      box.innerHTML = `<p class="log-empty">点击原有工具操作后，输出会显示在这里。</p>`;
      line.classList.add("hidden");
      return;
    }
    const statusText = task.status === "running" ? "运行中" : (task.status === "succeeded" ? "已完成" : "失败");
    state.textContent = `${task.label} · ${statusText}`;
    const output = task.output || (task.status === "running" ? "正在等待工作流输出……" : "没有文本输出。");
    box.innerHTML = `<div>${escapeHtml(output)}</div>`;
    box.scrollTop = box.scrollHeight;
    if (task.result_path) {
      result.textContent = task.result_path;
      line.classList.remove("hidden");
    } else {
      line.classList.add("hidden");
    }
  }

  function renderStatus(payload) {
    const paths = payload.paths || {};
    const demo = $("#latest-demo");
    const snapshot = $("#latest-snapshot");
    const real = $("#latest-real");
    const codex = $("#latest-codex");
    const updated = $("#updated-at");
    if (demo) demo.textContent = shortPath(paths.latest_demo_report);
    if (snapshot) snapshot.textContent = shortPath(paths.latest_snapshot);
    if (real) real.textContent = shortPath(paths.latest_real_report);
    if (codex) codex.textContent = shortPath(paths.latest_codex_review);
    if (updated) updated.textContent = `最近刷新 ${new Date().toLocaleTimeString("zh-CN", { hour12: false })}`;

    const task = payload.current || (payload.recent && payload.recent[0]) || null;
    const running = Boolean(payload.current && payload.current.status === "running");
    const runningBadge = $("#running-badge");
    if (runningBadge) runningBadge.classList.toggle("hidden", !running);
    setTaskBusy(pageState.localBusy || running);
    renderLog(task);
    if (task && task.task_id !== lastTaskId && task.status !== "running") {
      lastTaskId = task.task_id;
      if (task.status === "failed") window.setTimeout(() => alert(`原有任务失败：${task.output || "请查看运行日志。"}`), 0);
    }
  }

  function extractList(payload, key) {
    if (Array.isArray(payload)) return payload;
    if (payload && Array.isArray(payload[key])) return payload[key];
    if (payload && payload.data && Array.isArray(payload.data[key])) return payload.data[key];
    return [];
  }

  function normaliseHolding(item) {
    if (!item || typeof item !== "object") return null;
    const code = String(item.code || "").trim();
    const assetType = String(item.asset_type || item.assetType || "stock").toLowerCase();
    if (!code || !["stock", "etf"].includes(assetType)) return null;
    return {
      code,
      name: String(item.name || "未命名标的"),
      asset_type: assetType,
      quantity: Number(item.quantity || 0),
      avg_cost_cny: Number(item.avg_cost_cny ?? item.cost ?? 0),
      tactical_ratio: Number(item.tactical_ratio ?? item.tacticalRatio ?? 0.2),
      revision: item.revision,
    };
  }

  function holdingKey(holding) {
    return `${holding.asset_type}/${holding.code}`;
  }

  function normaliseResult(item) {
    if (!item || typeof item !== "object") return null;
    const code = String(item.code || item.symbol || "").trim();
    const assetType = String(item.asset_type || item.assetType || "stock").toLowerCase();
    if (!code) return null;
    return Object.assign({}, item, { code, asset_type: assetType });
  }

  const SOURCE_LABELS = Object.freeze({
    eastmoney: "东方财富",
    tencent: "腾讯",
    baostock: "BaoStock",
    tushare: "Tushare Pro",
    failover: "自动线路",
  });

  function sourceKey(value) {
    const key = String(value == null ? "" : value).trim().toLowerCase();
    return Object.prototype.hasOwnProperty.call(SOURCE_LABELS, key) ? key : "";
  }

  function normaliseSourceAttempts(result) {
    if (!result || !Array.isArray(result.source_attempts)) return [];
    return result.source_attempts.map((item) => {
      if (!item || typeof item !== "object") return null;
      const source = sourceKey(item.source);
      if (!source) return null;
      const rawStatus = String(item.status == null ? "" : item.status).trim().toLowerCase();
      const status = rawStatus === "success" ? "success" : (rawStatus === "unsupported" ? "unsupported" : "failed");
      return { source, status };
    }).filter(Boolean);
  }

  function sourceSummary(result) {
    const selected = sourceKey(result && result.data_source);
    const attempts = normaliseSourceAttempts(result);
    const successful = attempts.filter((item) => item.status === "success" && item.source !== "failover");
    let actual = "";
    if (attempts.length > 0) {
      actual = successful.length > 0 ? successful[successful.length - 1].source : "";
    } else if (selected && selected !== "failover") {
      // Older results predate source_attempts; data_source is their only provenance.
      actual = selected;
    }
    if (actual) {
      const successIndex = attempts.findIndex((item) => item.source === actual && item.status === "success");
      const switched = successIndex > 0 && attempts.slice(0, successIndex).some((item) => item.status !== "success");
      return `本次来源：${SOURCE_LABELS[actual]}${switched ? "（已自动切换）" : ""}`;
    }

    const tried = [];
    attempts.forEach((item) => {
      if (item.source === "failover" || tried.includes(item.source)) return;
      tried.push(item.source);
    });
    return tried.length > 0
      ? `自动线路均不可用（尝试过：${tried.map((source) => SOURCE_LABELS[source]).join("、")}）`
      : "自动线路均不可用";
  }

  function resultFor(holding) {
    const key = holdingKey(holding);
    const matches = pageState.results.filter((item) => holdingKey(item) === key);
    return matches.find((item) => item.mode === "analyze") || matches[0] || null;
  }

  function stateFor(result) {
    const raw = result && (result.state || result.phase || result.status);
    return STATE_META[raw] ? raw : "DATA_INSUFFICIENT";
  }

  function stateMarkup(result) {
    const state = stateFor(result);
    const meta = STATE_META[state];
    return `<span class="phase-badge phase-${meta.tone}">${meta.label}</span>`;
  }

  function dateFromResult(result) {
    if (!result) return "等待收盘数据";
    return result.completed_through || result.data_as_of || result.as_of || result.trade_date || "已请求更新";
  }

  function formatPercent(value) {
    const number = Number(value);
    if (!Number.isFinite(number)) return "—";
    const percent = Math.abs(number) <= 1 ? number * 100 : number;
    return `${percent.toFixed(1)}%`;
  }

  function displayAssetType(value) {
    return value === "etf" ? "ETF" : "股票";
  }

  function renderHoldings() {
    const empty = $("#holdings-empty");
    const list = $("#holdings-list");
    if (!empty || !list) return;
    empty.classList.toggle("hidden", pageState.holdings.length > 0);
    list.classList.toggle("hidden", pageState.holdings.length === 0);
    list.innerHTML = pageState.holdings.map((holding) => {
      const result = resultFor(holding);
      const state = stateFor(result);
      const meta = STATE_META[state];
      const ratio = Number.isFinite(holding.tactical_ratio) ? holding.tactical_ratio : 0.2;
      return `<article class="holding-card" data-holding-key="${escapeHtml(holdingKey(holding))}">
        <div class="holding-card-top">
          <div class="holding-title"><h3>${escapeHtml(holding.name)}</h3><span class="asset-chip">${displayAssetType(holding.asset_type)}</span></div>
          <span class="holding-code">${escapeHtml(holding.code)}</span>
        </div>
        <div class="holding-facts"><span>数量 <strong>${escapeHtml(holding.quantity)}</strong></span><span>成本 <strong>${escapeHtml(holding.avg_cost_cny.toFixed ? holding.avg_cost_cny.toFixed(3) : holding.avg_cost_cny)}</strong></span><span>模拟比例 <strong>${formatPercent(ratio)}</strong></span></div>
        <div class="phase-row"><span class="phase-caption">当前阶段</span>${stateMarkup(result)}</div>
        <p class="phase-explanation">${meta.explanation}</p>
        <p class="phase-date">数据截至：${escapeHtml(dateFromResult(result))}</p>
        <p class="phase-date">${escapeHtml(result ? sourceSummary(result) : "等待线路结果")}</p>
        <div class="holding-actions">
          <button class="button button-secondary button-small" type="button" data-swing-action="analyze" data-code="${escapeHtml(holding.code)}" data-asset-type="${escapeHtml(holding.asset_type)}">更新阶段判断</button>
          <button class="button button-quiet button-small" type="button" data-swing-action="backtest" data-code="${escapeHtml(holding.code)}" data-asset-type="${escapeHtml(holding.asset_type)}">历史模拟</button>
          <button class="text-button" type="button" data-holding-action="edit" data-code="${escapeHtml(holding.code)}" data-asset-type="${escapeHtml(holding.asset_type)}">编辑</button>
          <button class="text-button text-button-danger" type="button" data-holding-action="delete" data-code="${escapeHtml(holding.code)}" data-asset-type="${escapeHtml(holding.asset_type)}">删除</button>
        </div>
      </article>`;
    }).join("");
  }

  function renderResults() {
    const empty = $("#results-empty");
    const list = $("#swing-results");
    if (!empty || !list) return;
    const byKey = new Map();
    pageState.results.forEach((item) => {
      const key = holdingKey(item);
      if (!byKey.has(key) || item.mode === "analyze") byKey.set(key, item);
    });
    const displayResults = Array.from(byKey.values());
    empty.classList.toggle("hidden", displayResults.length > 0);
    list.classList.toggle("hidden", displayResults.length === 0);
    list.innerHTML = displayResults.map((result) => {
      const state = stateFor(result);
      const meta = STATE_META[state];
      const holding = pageState.holdings.find((item) => holdingKey(item) === holdingKey(result));
      return `<article class="result-card">
        <div class="result-card-heading"><div><strong>${escapeHtml((holding && holding.name) || result.name || result.code)}</strong><small>${escapeHtml(result.code)} · ${displayAssetType(result.asset_type)}</small></div>${stateMarkup(result)}</div>
        <p>${meta.explanation}</p>
        <small class="phase-date">数据截至：${escapeHtml(dateFromResult(result))} · 仅供观察</small>
        <small class="phase-date">${escapeHtml(sourceSummary(result))}</small>
      </article>`;
    }).join("");
  }

  function extractHoldings(payload) {
    return extractList(payload, "holdings").map(normaliseHolding).filter(Boolean);
  }

  function extractResults(payload) {
    return extractList(payload, "results").map(normaliseResult).filter(Boolean);
  }

  async function loadHoldings(showError) {
    try {
      const payload = await api("/api/holdings", { method: "GET" });
      pageState.holdings = extractHoldings(payload);
      renderHoldings();
      if (showError) setNotice("#holdings-error", "");
      return payload;
    } catch (error) {
      if (showError) setNotice("#holdings-error", friendlyError(error, error.status));
      throw error;
    }
  }

  async function loadResults(showError) {
    try {
      const payload = await api("/api/swing-results", { method: "GET" });
      pageState.results = extractResults(payload);
      renderHoldings();
      renderResults();
      renderSimulation(pageState.results.find((item) => item.mode === "backtest") || null);
      const updated = $("#swing-updated-at");
      if (updated) updated.textContent = payload.updated_at || `最近刷新 ${new Date().toLocaleTimeString("zh-CN", { hour12: false })}`;
      return payload;
    } catch (error) {
      if (showError) setNotice("#holdings-error", friendlyError(error, error.status));
      throw error;
    }
  }

  async function refreshSwingData(showError) {
    await Promise.allSettled([loadHoldings(showError), loadResults(showError)]);
  }

  function resetHoldingForm() {
    pageState.editingKey = null;
    const form = $("#holding-form");
    if (!form) return;
    form.reset();
    $("#holding-form-title").textContent = "新增持仓";
    $("#holding-code").readOnly = false;
    $("#holding-asset-type").disabled = false;
    $("#holding-tactical").value = "20";
    updateTacticalOutput();
    setNotice("#holding-form-error", "");
  }

  function openHoldingForm(holding) {
    const form = $("#holding-form");
    if (!form) return;
    if (holding) {
      pageState.editingKey = holdingKey(holding);
      $("#holding-form-title").textContent = "编辑本机记录";
      $("#holding-code").value = holding.code;
      $("#holding-code").readOnly = true;
      $("#holding-name").value = holding.name;
      $("#holding-asset-type").value = holding.asset_type;
      $("#holding-asset-type").disabled = true;
      $("#holding-quantity").value = holding.quantity;
      $("#holding-cost").value = holding.avg_cost_cny;
      $("#holding-tactical").value = Math.round((holding.tactical_ratio || 0.2) * 100);
      updateTacticalOutput();
    } else {
      resetHoldingForm();
    }
    form.classList.remove("hidden");
    form.scrollIntoView({ behavior: "smooth", block: "nearest" });
    $("#holding-name").focus();
  }

  function closeHoldingForm() {
    const form = $("#holding-form");
    if (form) form.classList.add("hidden");
    resetHoldingForm();
  }

  function updateTacticalOutput() {
    const input = $("#holding-tactical");
    const output = $("#holding-tactical-output");
    if (input && output) output.textContent = `${input.value}%`;
  }

  function formPayload() {
    return {
      code: $("#holding-code").value.trim(),
      name: $("#holding-name").value.trim(),
      asset_type: $("#holding-asset-type").value,
      quantity: Number($("#holding-quantity").value),
      avg_cost_cny: Number($("#holding-cost").value),
      tactical_ratio: Number($("#holding-tactical").value) / 100,
    };
  }

  async function saveHolding(event) {
    event.preventDefault();
    const form = $("#holding-form");
    if (!form || pageState.busy) return;
    if (!form.checkValidity()) {
      form.reportValidity();
      return;
    }
    const payload = formPayload();
    const current = pageState.holdings.find((item) => holdingKey(item) === pageState.editingKey);
    const editing = Boolean(pageState.editingKey);
    if (current && current.revision != null) payload.expected_revision = current.revision;
    if (!editing) payload.expected_revision = 0;
    if (editing && (!current || current.revision == null)) {
      setNotice("#holding-form-error", "本机记录版本不可用，请先刷新后再编辑。");
      return;
    }
    const path = editing ? `/api/holdings/${encodeURIComponent(payload.asset_type)}/${encodeURIComponent(payload.code)}` : "/api/holdings";
    setLocalBusy(true);
    setNotice("#holding-form-error", "");
    try {
      await api(path, { method: editing ? "PUT" : "POST", body: JSON.stringify(payload) });
      closeHoldingForm();
      await refreshSwingData(true);
      setNotice("#holdings-error", "本机持仓记录已更新。");
    } catch (error) {
      setNotice("#holding-form-error", friendlyError(error, error.status));
    } finally {
      setLocalBusy(false);
    }
  }

  async function deleteHolding(code, assetType, name, revision) {
    if (pageState.busy) return;
    if (!Number.isInteger(revision)) {
      setNotice("#holdings-error", "本机记录版本不可用，请先刷新后再删除。");
      return;
    }
    if (!window.confirm(`确定删除“${name || code}”这条本机记录吗？历史报告文件不会因此删除。`)) return;
    setLocalBusy(true);
    try {
      await api(`/api/holdings/${encodeURIComponent(assetType)}/${encodeURIComponent(code)}`, {
        method: "DELETE",
        body: JSON.stringify({ expected_revision: revision }),
      });
      await refreshSwingData(true);
      setNotice("#holdings-error", "本机持仓记录已删除。");
    } catch (error) {
      setNotice("#holdings-error", friendlyError(error, error.status));
    } finally {
      setLocalBusy(false);
    }
  }

  async function runSwingAction(action, code, assetType) {
    if (pageState.busy) return;
    setLocalBusy(true);
    setNotice("#holdings-error", "");
    const body = { action };
    if (code && assetType) {
      body.code = code;
      body.asset_type = assetType;
    }
    try {
      const payload = await api("/api/swing-action", { method: "POST", body: JSON.stringify(body) });
      if (payload.task && payload.task.task_id) await waitForTask(payload.task.task_id);
      const directResults = extractResults(payload);
      if (directResults.length > 0) pageState.results = directResults;
      await loadResults(true);
      renderHoldings();
      if (action === "backtest" || action === "all") {
        const matching = pageState.results.find((item) =>
          item.mode === "backtest" && (!code || (item.code === code && item.asset_type === assetType))
        );
        renderSimulation(matching || null);
      }
      setNotice("#holdings-error", "阶段判断或历史模拟已更新，请以收盘数据和历史结果为准。");
    } catch (error) {
      setNotice("#holdings-error", friendlyError(error, error.status));
    } finally {
      setLocalBusy(false);
    }
  }

  async function waitForTask(taskId) {
    // Swing work runs in the local TaskManager. Keep the personal controls
    // disabled until that fixed task actually finishes, rather than treating
    // an HTTP 202 acknowledgement as a completed analysis.
    for (let attempt = 0; attempt < 600; attempt += 1) {
      await new Promise((resolve) => window.setTimeout(resolve, 1000));
      const payload = await api("/api/status", { method: "GET", headers: {} });
      renderStatus(payload);
      const task = payload.current && payload.current.task_id === taskId
        ? payload.current
        : (payload.recent || []).find((item) => item.task_id === taskId);
      if (!task || task.status === "running") continue;
      if (task.status !== "succeeded") throw new Error(task.output || "波段任务未完成。");
      return task;
    }
    throw new Error("波段任务等待时间过长，请查看原有运行日志后再刷新。");
  }

  function renderSimulation(payload) {
    const panel = $("#simulation-panel");
    const metrics = $("#simulation-metrics");
    const note = $("#simulation-note");
    if (!panel || !metrics || !note) return;
    const summary = payload && payload.mode === "backtest"
      ? payload
      : payload && (payload.backtest || payload.simulation || payload.summary || payload.result);
    if (!summary || typeof summary !== "object") {
      panel.classList.add("hidden");
      metrics.innerHTML = "";
      note.textContent = "";
      return;
    }
    const buyHold = summary.buy_and_hold || summary.buyAndHold || {};
    const staticCoreCash = summary.static_core_cash || summary.staticCoreCash || {};
    const coreTactical = summary.core_tactical || summary.coreTactical || {};
    const robustness = summary.robustness || {};
    const consistency = robustness.status === "ok"
      ? `${robustness.direction_consistent_count}/${robustness.direction_total}`
      : "样本不足";
    const cards = [
      ["长期持有回放", formatPercent(buyHold.total_return)],
      ["静态核心+现金", formatPercent(staticCoreCash.total_return)],
      ["动态核心+机动", formatPercent(coreTactical.total_return)],
      ["动态最大回撤", formatPercent(coreTactical.max_drawdown)],
      ["动态年化波动", formatPercent(coreTactical.annualized_volatility)],
      ["平均市场暴露", formatPercent(coreTactical.market_exposure)],
      ["固定扰动方向一致", consistency],
    ];
    panel.classList.remove("hidden");
    metrics.innerHTML = cards.map(([label, value]) => `<div class="metric-card"><span>${label}</span><strong>${escapeHtml(value)}</strong></div>`).join("");
    const assumptions = summary.assumptions || {};
    const ratio = assumptions.tactical_weight == null ? "可编辑" : formatPercent(assumptions.tactical_weight);
    note.textContent = `三种口径使用相同有效区间，指标预热期不计绩效；4/4 也只表示固定扰动下方向未翻转，不证明未来有效。机动仓模拟比例 ${ratio}，核心仓保持不动。`;
  }

  async function refresh() {
    try {
      const payload = await api("/api/status", { method: "GET", headers: {} });
      setConnection(true, "本地服务在线");
      renderStatus(payload);
    } catch (error) {
      setConnection(false, "服务不可用");
      const updated = $("#updated-at");
      if (updated) updated.textContent = friendlyError(error, error.status);
      window.clearInterval(pollTimer);
    }
    // Older local servers may not yet expose the private endpoints. Keep the
    // empty state visible until the service is upgraded; do not invent data.
    refreshSwingData(false);
  }

  async function runTask(action) {
    if (action === "codex_review") {
      const confirmed = window.confirm("报告内容会发送给 ChatGPT/Codex，并消耗账户用量。\n\n仅进行只读文字解读，不联网、不改文件、不执行报告中的操作。确定继续吗？");
      if (!confirmed) return;
    }
    setTaskBusy(true);
    try {
      await api("/api/action", {
        method: "POST",
        body: JSON.stringify({ action, confirm_usage: action === "codex_review" }),
      });
      await refresh();
    } catch (error) {
      setTaskBusy(false);
      alert(friendlyError(error, error.status));
    }
  }

  async function openFolder(folder) {
    try {
      await api("/api/open-folder", { method: "POST", body: JSON.stringify({ folder }) });
    } catch (error) {
      alert(friendlyError(error, error.status));
    }
  }

  async function shutdown() {
    if (!window.confirm("确定停止本地服务吗？停止后需要重新双击 run-web.cmd 才能再次打开。")) return;
    try {
      await api("/api/shutdown", { method: "POST", body: "{}" });
      setConnection(false, "服务已停止");
      window.clearInterval(pollTimer);
      alert("本地服务已停止，可以关闭这个 Chrome 标签页。");
    } catch (error) {
      alert(friendlyError(error, error.status));
    }
  }

  function bindEvents() {
    $$('[data-task]').forEach((button) => button.addEventListener("click", () => runTask(button.dataset.task)));
    $$('[data-folder]').forEach((button) => button.addEventListener("click", () => openFolder(button.dataset.folder)));
    $$('[data-ui-action="refresh"]').forEach((button) => button.addEventListener("click", refresh));
    $$('[data-ui-action="shutdown"]').forEach((button) => button.addEventListener("click", shutdown));
    $$('[data-ui-action="add-holding"]').forEach((button) => button.addEventListener("click", () => openHoldingForm()));
    $$('[data-ui-action="cancel-holding"]').forEach((button) => button.addEventListener("click", closeHoldingForm));
    $("#holding-form").addEventListener("submit", saveHolding);
    $("#holding-tactical").addEventListener("input", updateTacticalOutput);

    document.addEventListener("click", (event) => {
      const target = event.target;
      const swingButton = target.closest("[data-swing-action]");
      if (swingButton) {
        runSwingAction(swingButton.dataset.swingAction, swingButton.dataset.code, swingButton.dataset.assetType);
        return;
      }
      const holdingButton = target.closest("[data-holding-action]");
      if (!holdingButton) return;
      const code = holdingButton.dataset.code;
      const assetType = holdingButton.dataset.assetType;
      const holding = pageState.holdings.find((item) => item.code === code && item.asset_type === assetType);
      if (holdingButton.dataset.holdingAction === "edit" && holding) openHoldingForm(holding);
      if (holdingButton.dataset.holdingAction === "delete") deleteHolding(code, assetType, holding && holding.name, holding && holding.revision);
    });
  }

  bindEvents();
  resetHoldingForm();
  renderHoldings();
  renderResults();
  refresh();
  pollTimer = window.setInterval(refresh, pollMs);
})();
