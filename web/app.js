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
    adata: "AData（同花顺）",
    failover: "自动线路",
  });

  function sourceKey(value) {
    const key = String(value == null ? "" : value).trim().toLowerCase();
    return Object.prototype.hasOwnProperty.call(SOURCE_LABELS, key) ? key : "";
  }

  function normaliseSourceAttempts(result) {
    // Older results predate source_attempts; data_source remains authoritative.
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
    const barCount = Number(result && result.bars_available);
    const noValidData = Boolean(
      result && (
        result.status === "error" ||
        (!result.data_as_of && (!Number.isFinite(barCount) || barCount <= 0))
      )
    );
    const tried = [];
    attempts.forEach((item) => {
      if (item.source === "failover" || tried.includes(item.source)) return;
      tried.push(item.source);
    });
    if (noValidData || selected === "failover") {
      const suffix = tried.length > 0
        ? `（尝试过：${tried.map((source) => SOURCE_LABELS[source]).join("、")}）`
        : "";
      return `本次未取得有效日线${suffix}`;
    }
    if (selected) {
      // data_source is authoritative. Attempts are used only to explain why
      // the selected source was reached, which also keeps old caches working.
      const selectedIndex = attempts.findIndex(
        (item) => item.source === selected && item.status === "success"
      );
      const previousFailures = selectedIndex > 0
        ? attempts.slice(0, selectedIndex).filter((item) => item.status !== "success")
        : [];
      const failureLabels = [];
      previousFailures.forEach((item) => {
        if (!failureLabels.includes(SOURCE_LABELS[item.source])) failureLabels.push(SOURCE_LABELS[item.source]);
      });
      const switched = failureLabels.length > 0;
      // Legacy wording "已自动切换" is intentionally replaced by the more
      // informative "自动切换成功；此前...失败" in rendered results.
      return `本次来源：${SOURCE_LABELS[selected]}${switched
        ? `（自动切换成功；此前${failureLabels.join("、")}失败）`
        : ""}`;
    }
    return "本次来源：公开行情";
  }

  function safeString(value, fallback = "未取得") {
    if (value == null) return fallback;
    const text = String(value).trim();
    if (!text || ["undefined", "null", "nan"].includes(text.toLowerCase())) return fallback;
    return text;
  }

  function detailKey(item, section) {
    const source = item && typeof item === "object" ? item : {};
    const code = safeString(source.code || source.symbol, "");
    const assetType = safeString(source.asset_type || source.assetType, "");
    const mode = safeString(source.mode, section === "simulation" ? "backtest" : "analyze");
    return [section, mode, assetType, code].map((value) => encodeURIComponent(value)).join("|");
  }

  function captureOpenDetailKeys(container) {
    const keys = new Set();
    if (!container || typeof container.querySelectorAll !== "function") return keys;
    Array.from(container.querySelectorAll("details[data-detail-key]")).forEach((detail) => {
      const key = detail && detail.dataset && detail.dataset.detailKey;
      if (detail && detail.open && key) keys.add(key);
    });
    return keys;
  }

  function restoreOpenDetailKeys(container, keys) {
    if (!container || !keys || keys.size === 0 || typeof container.querySelectorAll !== "function") return;
    Array.from(container.querySelectorAll("details[data-detail-key]")).forEach((detail) => {
      const key = detail && detail.dataset && detail.dataset.detailKey;
      if (detail && key) detail.open = keys.has(key);
    });
  }

  function rerenderWithOpenDetails(container, render) {
    if (!container) return;
    const openKeys = captureOpenDetailKeys(container);
    container.innerHTML = typeof render === "function" ? render() : String(render || "");
    restoreOpenDetailKeys(container, openKeys);
  }

  function safeDisplayValue(value) {
    if (value == null) return "未取得";
    if (typeof value === "boolean") return value ? "是" : "否";
    if (typeof value === "number") return Number.isFinite(value) ? String(Number(value.toFixed(4))) : "未取得";
    if (Array.isArray(value)) return value.length ? value.map((item) => safeDisplayValue(item)).join("、") : "未取得";
    if (typeof value === "object") {
      const pairs = Object.entries(value).map(([key, item]) => `${safeString(key)}=${safeDisplayValue(item)}`);
      return pairs.length ? pairs.join("；") : "未取得";
    }
    return safeString(value);
  }

  function safeList(value) {
    return Array.isArray(value)
      ? value.map((item) => safeString(item, "")).filter(Boolean)
      : [];
  }

  function explanationFor(result) {
    return result && result.analysis_explanation && typeof result.analysis_explanation === "object"
      ? result.analysis_explanation
      : {};
  }

  function mainReasons(result) {
    const explanation = explanationFor(result);
    const conclusion = explanation.conclusion && typeof explanation.conclusion === "object"
      ? explanation.conclusion : {};
    const reasons = safeList(result && result.reasons);
    if (reasons.length < 2 && safeString(conclusion.why, "")) reasons.push(safeString(conclusion.why));
    if (reasons.length === 0) {
      const state = stateFor(result);
      reasons.push(STATE_META[state].explanation);
    }
    return reasons.slice(0, 4);
  }

  function researchRecommendation(result) {
    const explanation = explanationFor(result);
    return safeString(
      explanation.research_recommendation || explanation.recommendation,
      result && result.status === "error" ? "本次未形成研究结论，请先确认有效日线是否恢复。" : "继续按收盘节奏观察，核心仓不因技术信号自动改变。"
    );
  }

  function renderConditionList(conditions) {
    const list = Array.isArray(conditions) ? conditions : [];
    if (list.length === 0) return "<li>条件未评估（有效日线不足）。</li>";
    return list.map((item) => {
      if (!item || typeof item !== "object") return "";
      const label = safeString(item.label);
      const actual = safeDisplayValue(item.actual);
      const threshold = safeDisplayValue(item.threshold);
      const evaluated = item.pass === true || item.pass === false;
      const passed = item.pass === true;
      return `<li class="condition-row"><span>${escapeHtml(label)}</span><span>当前：${escapeHtml(actual)}</span><span>阈值：${escapeHtml(threshold)}</span><strong class="condition-${passed ? "pass" : evaluated ? "fail" : "pending"}">${passed ? "通过" : evaluated ? "未通过" : "未评估"}</strong></li>`;
    }).join("");
  }

  function renderExplanationGroup(title, group, fallbackConditions) {
    const source = group && typeof group === "object" ? group : {};
    const conditions = Array.isArray(source.conditions) ? source.conditions : fallbackConditions;
    const evaluated = source.evaluated !== false;
    const passed = source.pass === true;
    return `<section class="analysis-group"><h5>${escapeHtml(title)} <small>${!evaluated ? "未评估" : passed ? "全部通过" : "未全部通过"}</small></h5><ul class="condition-list">${evaluated ? renderConditionList(conditions) : "<li>条件未评估（有效日线不足）。</li>"}</ul></section>`;
  }

  function renderAnalysisDetails(result) {
    const explanation = explanationFor(result);
    const flow = safeList(explanation.analysis_flow);
    const lowGroup = explanation.low_watch && typeof explanation.low_watch === "object" ? explanation.low_watch : null;
    const highGroup = explanation.high_watch && typeof explanation.high_watch === "object" ? explanation.high_watch : null;
    const lowConditions = lowGroup ? lowGroup.conditions : explanation.low_watch_conditions;
    const highConditions = highGroup ? highGroup.conditions : explanation.high_watch_conditions;
    const trend = explanation.trend_environment && typeof explanation.trend_environment === "object" ? explanation.trend_environment : {};
    const confirmation = explanation.confirmation_path && typeof explanation.confirmation_path === "object" ? explanation.confirmation_path : {};
    const conclusion = explanation.conclusion && typeof explanation.conclusion === "object" ? explanation.conclusion : {};
    const boundary = explanation.model_boundary && typeof explanation.model_boundary === "object" ? explanation.model_boundary : {};
    const indicators = explanation.indicator_snapshot && typeof explanation.indicator_snapshot === "object"
      ? explanation.indicator_snapshot
      : (result && result.features && typeof result.features === "object" ? result.features : {});
    const previous = confirmation.previous_state && typeof confirmation.previous_state === "object" ? confirmation.previous_state : {};
    const routeMarkup = ["bottom", "top"].map((key) => {
      const group = confirmation[key];
      if (!group || typeof group !== "object") return "";
      const routes = Array.isArray(group.routes) ? group.routes : [];
      const routeText = routes.map((route) => {
        if (!route || typeof route !== "object") return "";
        return `<li>${escapeHtml(safeString(route.label))}：${escapeHtml(route.pass === true ? "通过" : "未通过")}；当前 ${escapeHtml(safeDisplayValue(route.actual))}；阈值 ${escapeHtml(safeDisplayValue(route.threshold))}</li>`;
      }).join("") || "<li>确认路径明细未提供。</li>";
      return `<section class="analysis-group"><h5>${key === "bottom" ? "低位确认" : "高位确认"} <small>${group.pass === true ? "通过" : "未通过"}</small></h5><p>前一日状态：${escapeHtml(safeString(previous.label))}</p><ul class="route-list">${routeText}</ul></section>`;
    }).join("");
    const indicatorText = Object.entries(indicators).map(([key, value]) => `<span><b>${escapeHtml(safeString(key))}</b> ${escapeHtml(safeDisplayValue(value))}</span>`).join("") || "<span>实际指标未提供。</span>";
    const flowMarkup = flow.length ? `<ol>${flow.map((item) => `<li>${escapeHtml(item)}</li>`).join("")}</ol>` : "<p>分析流程未提供。</p>";
    const trendActual = safeDisplayValue(trend.actual);
    const trendThreshold = safeDisplayValue(trend.threshold);
    return `<details class="analysis-details" data-detail-key="${escapeHtml(detailKey(result, "analysis"))}"><summary>查看完整分析过程与依据</summary><div class="analysis-details-body"><section><h4>分析流程</h4>${flowMarkup}</section>${renderExplanationGroup("低位观察三项条件", lowGroup, lowConditions)}${renderExplanationGroup("高位观察三项条件", highGroup, highConditions)}<section><h4>实际指标</h4><div class="indicator-list">${indicatorText}</div></section><section><h4>趋势环境与确认</h4><p>趋势：${escapeHtml(safeString(trend.label || trend.value))}；实际：${escapeHtml(trendActual)}；阈值：${escapeHtml(trendThreshold)}；作用：${escapeHtml(safeString(trend.explanation, "趋势只用于限制确认方向，不单独决定高低位。"))}</p>${routeMarkup || "<p>确认路径未提供。</p>"}</section><section><h4>为什么是这个结论</h4><p>${escapeHtml(safeString(conclusion.why, STATE_META[stateFor(result)].explanation))}</p><p>为什么不是低位：${escapeHtml(safeString(conclusion.why_not_low))}</p><p>为什么不是高位：${escapeHtml(safeString(conclusion.why_not_high))}</p></section><section><h4>下一次重点观察</h4><p>${escapeHtml(safeString(explanation.next_observation))}</p></section><section><h4>模型边界</h4><p>${escapeHtml(safeString(boundary.text, "模型边界未提供。"))}</p><p>严格失效规则：${escapeHtml(safeString(boundary.strict_invalidation_rule, "未提供。"))}</p><p>${escapeHtml(safeString(boundary.not_a_trade_instruction, "建议仅用于机动仓研究，核心仓不因技术信号自动改变。"))}</p></section></div></details>`;
  }

  function generatedAtFromResult(result) {
    return safeString(result && (result.generated_at || result.updated_at), "未取得");
  }

  function lastUpdateNotice(result) {
    const attempt = result && result.last_update_attempt && typeof result.last_update_attempt === "object"
      ? result.last_update_attempt : null;
    if (!attempt || safeString(attempt.status, "").toLowerCase() !== "error") return "";
    return `<aside class="last-update-warning"><strong>本次更新失败，以下为上次成功结果（生成时间/数据截止）。</strong><span>上次结果：生成时间 ${escapeHtml(generatedAtFromResult(result))}；数据截止 ${escapeHtml(safeString(dateFromResult(result)))}</span><span>本次失败时间：${escapeHtml(safeString(attempt.generated_at))}</span></aside>`;
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
    if (value == null || (typeof value === "string" && !value.trim())) return "未提供";
    const number = Number(value);
    if (!Number.isFinite(number)) return "未提供";
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
    rerenderWithOpenDetails(list, () => pageState.holdings.map((holding) => {
      const result = resultFor(holding);
      const state = stateFor(result);
      const meta = STATE_META[state];
      const ratio = Number.isFinite(holding.tactical_ratio) ? holding.tactical_ratio : 0.2;
      const reasons = mainReasons(result);
      const recommendation = result ? researchRecommendation(result) : "等待分析结果后再形成机动仓研究建议。";
      return `<article class="holding-card" data-holding-key="${escapeHtml(holdingKey(holding))}">
        <div class="holding-card-top">
          <div class="holding-title"><h3>${escapeHtml(holding.name)}</h3><span class="asset-chip">${displayAssetType(holding.asset_type)}</span></div>
          <span class="holding-code">${escapeHtml(holding.code)}</span>
        </div>
        <div class="holding-facts"><span>数量 <strong>${escapeHtml(safeDisplayValue(holding.quantity))}</strong></span><span>成本 <strong>${escapeHtml(Number.isFinite(holding.avg_cost_cny) ? holding.avg_cost_cny.toFixed(3) : "未取得")}</strong></span><span>模拟比例 <strong>${formatPercent(ratio)}</strong></span></div>
        <div class="phase-row"><span class="phase-caption">当前阶段</span>${stateMarkup(result)}</div>
        <p class="phase-explanation">${meta.explanation}</p>
        <section class="analysis-brief"><h4>主要依据</h4><ul>${reasons.map((reason) => `<li>${escapeHtml(reason)}</li>`).join("")}</ul><h4>研究建议</h4><p>${escapeHtml(recommendation)}</p></section>
        ${lastUpdateNotice(result)}
        <p class="phase-date">结果生成：${escapeHtml(generatedAtFromResult(result))}</p>
        <p class="phase-date">数据截至：${escapeHtml(safeString(dateFromResult(result)))}</p>
        <p class="phase-date">${escapeHtml(result ? sourceSummary(result) : "等待线路结果")}</p>
        ${result ? renderAnalysisDetails(result) : ""}
        <div class="holding-actions">
          <button class="button button-secondary button-small" type="button" data-swing-action="analyze" data-code="${escapeHtml(holding.code)}" data-asset-type="${escapeHtml(holding.asset_type)}">更新阶段判断</button>
          <button class="button button-quiet button-small" type="button" data-swing-action="backtest" data-code="${escapeHtml(holding.code)}" data-asset-type="${escapeHtml(holding.asset_type)}">历史模拟</button>
          <button class="text-button" type="button" data-holding-action="edit" data-code="${escapeHtml(holding.code)}" data-asset-type="${escapeHtml(holding.asset_type)}">编辑</button>
          <button class="text-button text-button-danger" type="button" data-holding-action="delete" data-code="${escapeHtml(holding.code)}" data-asset-type="${escapeHtml(holding.asset_type)}">删除</button>
        </div>
      </article>`;
    }).join(""));
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
    rerenderWithOpenDetails(list, () => displayResults.map((result) => {
      const state = stateFor(result);
      const meta = STATE_META[state];
      const holding = pageState.holdings.find((item) => holdingKey(item) === holdingKey(result));
      const reasons = mainReasons(result);
      return `<article class="result-card">
        <div class="result-card-heading"><div><strong>${escapeHtml((holding && holding.name) || result.name || result.code)}</strong><small>${escapeHtml(result.code)} · ${displayAssetType(result.asset_type)}</small></div>${stateMarkup(result)}</div>
        <p>${meta.explanation}</p>
        <div class="analysis-brief"><h4>主要依据</h4><ul>${reasons.map((reason) => `<li>${escapeHtml(reason)}</li>`).join("")}</ul><h4>研究建议</h4><p>${escapeHtml(researchRecommendation(result))}</p></div>
        ${lastUpdateNotice(result)}
        <small class="phase-date">结果生成：${escapeHtml(generatedAtFromResult(result))}</small>
        <small class="phase-date">数据截至：${escapeHtml(safeString(dateFromResult(result)))} · 仅供观察</small>
        <small class="phase-date">${escapeHtml(sourceSummary(result))}</small>
        ${renderAnalysisDetails(result)}
      </article>`;
    }).join(""));
  }

  function extractHoldings(payload) {
    return extractList(payload, "holdings").map(normaliseHolding).filter(Boolean);
  }

  function extractResults(payload) {
    const generatedAt = payload && payload.updated_at;
    return extractList(payload, "results").map((item) => {
      if (!item || typeof item !== "object") return null;
      const candidate = Object.assign({}, item);
      if (!candidate.generated_at) candidate.generated_at = generatedAt;
      return normaliseResult(candidate);
    }).filter(Boolean);
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
      const detail = friendlyError(error, error.status);
      const hasPreviousResults = pageState.results.length > 0;
      setNotice(
        "#holdings-error",
        hasPreviousResults
          ? `本次更新失败，页面保留上次结果。${detail}`
          : `本次更新失败，暂未取得新结果。${detail}`
      );
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

  function finiteMetric(value) {
    if (value == null || (typeof value === "string" && !value.trim())) return null;
    const number = Number(value);
    return Number.isFinite(number) ? number : null;
  }

  function validRobustnessCounts(robustness) {
    if (!robustness || robustness.status !== "ok") return null;
    const consistent = finiteMetric(robustness.direction_consistent_count);
    const total = finiteMetric(robustness.direction_total);
    if (!Number.isInteger(consistent) || consistent < 0) return null;
    if (!Number.isInteger(total) || total <= 0 || consistent > total) return null;
    return { consistent, total };
  }

  function simulationMetric(value, asPercent = true) {
    const number = finiteMetric(value);
    if (number == null) return "未提供";
    return asPercent ? formatPercent(number) : safeDisplayValue(number);
  }

  function compareSimulationMetric(dynamicValue, baselineValue, kind) {
    const dynamic = finiteMetric(dynamicValue);
    const baseline = finiteMetric(baselineValue);
    if (dynamic == null || baseline == null) return "未提供足够字段，无法比较。";
    const delta = dynamic - baseline;
    if (Math.abs(delta) < 1e-12) return "与全程持有基准基本相同。";
    if (kind === "drawdown") {
      return delta > 0
        ? `动态口径回撤较小（${formatPercent(dynamic)} 对 ${formatPercent(baseline)}），方向上改善。`
        : `动态口径回撤更深（${formatPercent(dynamic)} 对 ${formatPercent(baseline)}），方向上恶化。`;
    }
    return delta > 0
      ? `动态口径收益更高（${formatPercent(dynamic)} 对 ${formatPercent(baseline)}）。`
      : `动态口径收益更低（${formatPercent(dynamic)} 对 ${formatPercent(baseline)}）。`;
  }

  function renderSimulationExplanation(summary) {
    const assumptions = summary.assumptions && typeof summary.assumptions === "object" ? summary.assumptions : {};
    const buyHold = summary.buy_and_hold || summary.buyAndHold || {};
    const staticCoreCash = summary.static_core_cash || summary.staticCoreCash || {};
    const coreTactical = summary.core_tactical || summary.coreTactical || {};
    const costs = assumptions.costs_bps && typeof assumptions.costs_bps === "object" ? assumptions.costs_bps : {};
    const interval = `${safeString(summary.start_date)} 至 ${safeString(summary.end_date)}`;
    const strategyRows = [
      ["全程持有", buyHold],
      ["核心仓+现金", staticCoreCash],
      ["核心仓+动态机动", coreTactical],
    ];
    const rows = strategyRows.map(([label, metric]) => `<tr><th>${escapeHtml(label)}</th><td>收益 ${escapeHtml(simulationMetric(metric.total_return))}</td><td>最大回撤 ${escapeHtml(simulationMetric(metric.max_drawdown))}</td><td>换手 ${escapeHtml(simulationMetric(metric.turnover))}</td><td>变化次数 ${escapeHtml(simulationMetric(metric.trade_count, false))}</td></tr>`).join("");
    const robustness = summary.robustness && typeof summary.robustness === "object" ? summary.robustness : {};
    const robustnessCounts = validRobustnessCounts(robustness);
    const robustnessText = robustnessCounts
      ? `固定扰动方向一致 ${robustnessCounts.consistent}/${robustnessCounts.total}。`
      : "稳健性样本不足或未提供。";
    return `<details class="simulation-explanation" data-detail-key="${escapeHtml(detailKey(summary, "simulation"))}"><summary>查看模拟过程与结论</summary><div class="simulation-explanation-body"><p><strong>模拟区间：</strong>${escapeHtml(interval)}；<strong>机动仓比例：</strong>${escapeHtml(simulationMetric(assumptions.tactical_weight))}；核心仓比例 ${escapeHtml(simulationMetric(assumptions.core_weight))}。</p><p><strong>执行假设：</strong>${escapeHtml(safeString(assumptions.frequency, "日线收盘形成状态，下一日线开盘处理"))}；指标预热期不计绩效。成本：手续费 ${escapeHtml(simulationMetric(costs.commission, false))} bps、滑点 ${escapeHtml(simulationMetric(costs.slippage, false))} bps、税费 ${escapeHtml(simulationMetric(costs.sell_tax, false))} bps。</p><div class="simulation-table-wrap"><table class="simulation-table"><thead><tr><th>口径</th><th>总收益</th><th>最大回撤</th><th>换手</th><th>变化次数</th></tr></thead><tbody>${rows || "<tr><td colspan=\"5\">模拟统计未提供。</td></tr>"}</tbody></table></div><p><strong>与全程持有比较：</strong>${escapeHtml(compareSimulationMetric(coreTactical.total_return, buyHold.total_return, "return"))} ${escapeHtml(compareSimulationMetric(coreTactical.max_drawdown, buyHold.max_drawdown, "drawdown"))}</p><p><strong>稳健性与活动度：</strong>${escapeHtml(robustnessText)} 动态口径实际变化 ${escapeHtml(simulationMetric(summary.trade_events, false))} 次，因流动性顺延 ${escapeHtml(simulationMetric(summary.deferred_count, false))} 次；字段缺失时不补造结论。</p><p><strong>为什么不能外推未来：</strong>历史回放只是在过去区间按固定规则和成本假设重演，无法覆盖未来行情、接口缺行、成交限制、盘中波动和参数失效；它不证明未来有效。</p></div></details>`;
  }

  function renderSimulation(payload) {
    const panel = $("#simulation-panel");
    const metrics = $("#simulation-metrics");
    const detail = $("#simulation-detail");
    const note = $("#simulation-note");
    if (!panel || !metrics || !note) return;
    const summary = payload && payload.mode === "backtest"
      ? payload
      : payload && (payload.backtest || payload.simulation || payload.summary || payload.result);
    if (!summary || typeof summary !== "object") {
      panel.classList.add("hidden");
      metrics.innerHTML = "";
      if (detail) rerenderWithOpenDetails(detail, "");
      note.textContent = "";
      return;
    }
    const buyHold = summary.buy_and_hold || summary.buyAndHold || {};
    const staticCoreCash = summary.static_core_cash || summary.staticCoreCash || {};
    const coreTactical = summary.core_tactical || summary.coreTactical || {};
    const robustness = summary.robustness || {};
    const robustnessCounts = validRobustnessCounts(robustness);
    const consistency = robustnessCounts
      ? `${robustnessCounts.consistent}/${robustnessCounts.total}`
      : "稳健性样本不足或未提供";
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
    if (detail) rerenderWithOpenDetails(detail, () => renderSimulationExplanation(summary));
    const assumptions = summary.assumptions || {};
    const ratio = assumptions.tactical_weight == null ? "可编辑" : formatPercent(assumptions.tactical_weight);
    note.textContent = `三种口径使用相同有效区间，指标预热期不计绩效；稳健性仅表示固定扰动下方向是否翻转，不证明未来有效。机动仓模拟比例 ${ratio}，核心仓保持不动。`;
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
