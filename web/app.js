(function () {
  "use strict";

  const token = window.__LOCAL_TOKEN__ || "";
  const pollMs = 2000;
  let pollTimer = null;
  let lastTaskId = null;

  const $ = (selector) => document.querySelector(selector);
  const $$ = (selector) => Array.from(document.querySelectorAll(selector));

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
    pill.classList.toggle("online", online);
    pill.classList.toggle("offline", !online);
    pill.innerHTML = `<i></i>${escapeHtml(text)}`;
  }

  async function api(path, options) {
    const request = Object.assign({}, options || {}, { headers: Object.assign({
      "X-Local-Token": token,
      "Content-Type": "application/json"
    }, (options && options.headers) || {}) });
    const response = await fetch(path, request);
    let data = {};
    try { data = await response.json(); } catch (_error) { data = {}; }
    if (!response.ok) {
      throw new Error(data.error || `请求失败（${response.status}）`);
    }
    return data;
  }

  function setButtonsDisabled(disabled) {
    $$(`[data-task], [data-folder]`).forEach((button) => { button.disabled = disabled; });
  }

  function renderLog(task) {
    const box = $("#log-box");
    const state = $("#log-state");
    const line = $("#result-line");
    const result = $("#result-path");
    if (!task) {
      state.textContent = "暂无任务";
      box.innerHTML = `<p class="log-empty">点击上方任意操作后，输出会显示在这里。</p>`;
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
    $("#latest-demo").textContent = shortPath(paths.latest_demo_report);
    $("#latest-snapshot").textContent = shortPath(paths.latest_snapshot);
    $("#latest-real").textContent = shortPath(paths.latest_real_report);
    $("#latest-codex").textContent = shortPath(paths.latest_codex_review);
    $("#updated-at").textContent = `最近刷新 ${new Date().toLocaleTimeString("zh-CN", { hour12: false })}`;

    const task = payload.current || (payload.recent && payload.recent[0]) || null;
    const running = Boolean(payload.current && payload.current.status === "running");
    $("#running-badge").classList.toggle("hidden", !running);
    setButtonsDisabled(running);
    renderLog(task);
    if (task && task.task_id !== lastTaskId && task.status !== "running") {
      lastTaskId = task.task_id;
      if (task.status === "failed") window.setTimeout(() => alert(`任务失败：${task.output || "请查看运行日志。"}`), 0);
    }
  }

  async function refresh() {
    try {
      const payload = await api("/api/status", { method: "GET", headers: {} });
      setConnection(true, "本地服务在线");
      renderStatus(payload);
    } catch (error) {
      setConnection(false, "服务不可用");
      $("#updated-at").textContent = error.message;
      window.clearInterval(pollTimer);
    }
  }

  async function runTask(action) {
    if (action === "codex_review") {
      const confirmed = window.confirm("报告内容会发送给 ChatGPT/Codex，并消耗账户用量。\n\n仅进行只读文字解读，不联网、不改文件、不交易。确定继续吗？");
      if (!confirmed) return;
    }
    setButtonsDisabled(true);
    try {
      await api("/api/action", {
        method: "POST",
        body: JSON.stringify({ action, confirm_usage: action === "codex_review" })
      });
      await refresh();
    } catch (error) {
      setButtonsDisabled(false);
      alert(error.message);
    }
  }

  async function openFolder(folder) {
    try {
      await api("/api/open-folder", { method: "POST", body: JSON.stringify({ folder }) });
    } catch (error) {
      alert(error.message);
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
      alert(error.message);
    }
  }

  $$('[data-task]').forEach((button) => button.addEventListener("click", () => runTask(button.dataset.task)));
  $$('[data-folder]').forEach((button) => button.addEventListener("click", () => openFolder(button.dataset.folder)));
  $$('[data-ui-action="refresh"]').forEach((button) => button.addEventListener("click", refresh));
  $$('[data-ui-action="shutdown"]').forEach((button) => button.addEventListener("click", shutdown));

  refresh();
  pollTimer = window.setInterval(refresh, pollMs);
})();
