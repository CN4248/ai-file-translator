/* LinguaFlow front-end — vanilla JS, no build step, no CDN. */
"use strict";

const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));

const state = {
  boot: null,
  settings: null,
  languages: [],
  langMap: new Map(),
  tasks: [],
  stats: { tasks: 0, by_status: {}, items: 0, items_by_status: {} },
  filter: "all",
  selected: new Set(),
  expanded: new Set(),
  details: new Map(),
  models: null,
  catalogQuery: "",
  view: "dashboard",
  logs: [],
  lastLogId: 0,
  logSource: null,
  logVisible: true,
  logUnread: false,
  pollTimer: null,
  installJob: null,
  installTimer: null,
  settingsDirty: false,
};

const STATUS = {
  queued: ["排队中", "s-queued"],
  running: ["翻译中", "s-running"],
  pausing: ["暂停中", "s-pausing"],
  paused: ["已暂停", "s-paused"],
  held: ["待启动", "s-held"],
  cancelling: ["取消中", "s-cancelling"],
  cancelled: ["已取消", "s-cancelled"],
  completed: ["已完成", "s-completed"],
  partial: ["部分完成", "s-partial"],
  failed: ["失败", "s-failed"],
};
const ACTIVE = new Set(["queued", "running", "pausing", "cancelling", "paused"]);
const IMAGE_EXT = new Set([".png", ".jpg", ".jpeg", ".webp", ".avif", ".bmp", ".tif", ".tiff", ".gif"]);
const RUNNING = new Set(["queued", "running", "pausing", "cancelling"]);

function icon(name, cls = "") {
  return `<svg class="ico ${cls}" aria-hidden="true"><use href="#i-${name}"/></svg>`;
}
function esc(value) {
  return String(value ?? "").replace(/[&<>"']/g, (ch) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[ch]));
}
function lang(code) {
  const l = state.langMap.get(code);
  return l ? l.native : (code || "—");
}
function langFull(code) {
  const l = state.langMap.get(code);
  return l ? `${l.native} · ${l.name}` : (code || "未知");
}
function statusMeta(status) {
  return STATUS[status] || [status || "未知", "s-cancelled"];
}
function clock(ts) {
  if (!ts) return"—";
  const d = new Date(Number(ts) * 1000);
  return d.toLocaleTimeString("zh-CN", { hour12: false, hour: "2-digit", minute: "2-digit" });
}
function ago(ts) {
  if (!ts) return "";
  const delta = Math.max(0, Date.now() / 1000 - Number(ts));
  if (delta < 45) return "刚刚";
  if (delta < 3600) return `${Math.floor(delta / 60)} 分钟前`;
  if (delta < 86400) return `${Math.floor(delta / 3600)} 小时前`;
  if (delta < 86400 * 7) return `${Math.floor(delta / 86400)} 天前`;
  return new Date(Number(ts) * 1000).toLocaleDateString("zh-CN", { month: "2-digit", day: "2-digit" });
}
function bytes(n) {
  n = Number(n) || 0;
  if (n < 1024) return `${n} B`;
  if (n < 1024 ** 2) return `${(n / 1024).toFixed(1)} KB`;
  if (n < 1024 ** 3) return `${(n / 1024 ** 2).toFixed(1)} MB`;
  return `${(n / 1024 ** 3).toFixed(2)} GB`;
}
function pct(a, b) { return b ? Math.max(0, Math.min(100, Math.round((a / b) * 100))) : 0; }

async function api(path, opts = {}) {
  const init = { method: opts.method || "GET", headers: {} };
  if (opts.body !== undefined) {
    init.headers["Content-Type"] = "application/json";
    init.body = JSON.stringify(opts.body);
  }
  let res;
  try {
    res = await fetch(path, init);
  } catch (err) {
    throw new Error("无法连接服务，请检查容器是否运行");
  }
  const text = await res.text();
  let data = null;
  if (text) {
    try { data = JSON.parse(text); } catch (e) { data = { error: text.slice(0, 300) }; }
  }
  if (!res.ok) {
    const msg = (data && (data.error || data.message)) || `请求失败（HTTP ${res.status}）`;
    throw new Error(msg);
  }
  return data;
}

/* -------------------------------- toasts -------------------------------- */
function toast(title, message = "", type = "info") {
  const stack = $("#toasts");
  const el = document.createElement("div");
  el.className = `toast ${type}`;
  const ico = type === "success" ? "check" : type === "error" ? "alert" : type === "warn" ? "alert" : "info";
  el.innerHTML = `${icon(ico)}<div><b>${esc(title)}</b>${message ? `<span>${esc(message)}</span>` : ""}</div>`;
  stack.appendChild(el);
  const remove = () => {
    el.classList.add("out");
    setTimeout(() => el.remove(), 260);
  };
  el.addEventListener("click", remove);
  setTimeout(remove, type === "error" ? 7000 : 4500);
}

function confirmDialog({ title, message, confirmText = "确认", danger = false }) {
  return new Promise((resolve) => {
    const root = $("#modalRoot");
    root.innerHTML = `
      <div class="modal-backdrop" data-cancel></div>
      <div class="modal" role="dialog" aria-modal="true" aria-label="${esc(title)}">
        <div class="modal-ico ${danger ? "danger" : ""}">${icon(danger ? "alert" : "info")}</div>
        <h3>${esc(title)}</h3>
        <p>${esc(message)}</p>
        <div class="modal-actions">
          <button class="btn btn-ghost btn-sm" data-cancel>取消</button>
          <button class="btn ${danger ? "btn-danger" : "btn-primary"} btn-sm" data-ok>${esc(confirmText)}</button>
        </div>
      </div>`;
    root.classList.add("open");
    const close = (value) => {
      root.classList.remove("open");
      root.innerHTML = "";
      resolve(value);
    };
    $$("[data-cancel]", root).forEach((el) => el.addEventListener("click", () => close(false)));
    $("[data-ok]", root).addEventListener("click", () => close(true));
    setTimeout(() => $("[data-ok]", root)?.focus(), 30);
  });
}

/* -------------------------------- routing ------------------------------- */
const PAGES = {
  dashboard: ["OVERVIEW", "工作台", "LinguaFlow · 语流翻译台"],
  tasks: ["QUEUE", "任务队列", "任务队列 · LinguaFlow"],
  models: ["OFFLINE ENGINE", "离线模型", "离线模型 · LinguaFlow"],
  settings: ["PREFERENCES", "设置", "设置 · LinguaFlow"],
};

function route() {
  const raw = (location.hash || "#/dashboard").replace(/^#\/?/, "").split("?")[0];
  const view = PAGES[raw] ? raw : "dashboard";
  state.view = view;
  $$(".view").forEach((el) => el.classList.toggle("active", el.id === `view-${view}`));
  $$(".nav-item").forEach((el) => el.classList.toggle("active", el.dataset.nav === view));
  const [eyebrow, title, docTitle] = PAGES[view];
  $("#pageEyebrow").textContent = eyebrow;
  $("#pageTitle").textContent = title;
  document.querySelector(".topbar")?.classList.toggle("compact", view !== "dashboard");
  document.title = docTitle;
  if (view === "models") loadModels();
  if (view === "settings") loadSystem();
  if (view === "tasks") renderTasks();
  if (view === "dashboard") updateStats();
  window.scrollTo({ top: 0, behavior: "smooth" });
}

function goNewTask() {
  if (location.hash !== "#/dashboard") location.hash = "#/dashboard";
  setTimeout(() => {
    $("#inputDir")?.focus();
    $("#taskForm")?.scrollIntoView({ behavior: "smooth", block: "center" });
  }, 80);
}

function setTheme(theme) {
  document.documentElement.dataset.theme = theme;
  try { localStorage.setItem("lf-theme", theme); } catch (e) {}
}

/* ------------------------------ engine / stats --------------------------- */
function renderEngine() {
  const off = state.boot?.offline || {};
  const pairs = off.offline_packages || 0;
  const aiOn = !!off.ai_configured;
  $("#offlinePairCount").textContent = pairs;
  $("#aiState").textContent = aiOn ? "已启用" : "未配置";
  $("#statOffline").textContent = pairs;
  $("#statOfflineNote").textContent = pairs ? `${off.offline_pairs?.length || pairs} 个方向可用` : "未安装";
  $("#engineBar").style.width = `${Math.min(100, Math.max(4, pairs * 12))}%`;
  $("#engineState").textContent = pairs || aiOn ? "就绪" : "待配置";
  $("#engineDot").style.background = pairs || aiOn ? "var(--mint)" : "var(--amber)";
  $("#pipeOffline").textContent = pairs ? `${pairs} 个语言包可用` : "未安装语言包";
  $("#pipeAi").textContent = aiOn ? `${off.ai_model || "OpenAI 兼容"} 已配置` : "未配置（可选）";
  $("#aiState").title = aiOn ? `AI 模型：${off.ai_model || "已配置"}` : "在设置中配置 API";
}

function updateStats() {
  if (!state.boot) return;
  const by = state.stats.by_status || {};
  const ib = state.stats.items_by_status || {};
  const total = state.stats.tasks || 0;
  const active = (by.queued || 0) + (by.running || 0) + (by.pausing || 0) + (by.cancelling || 0);
  const paused = by.paused || 0;
  const done = ib.completed || 0;
  const failed = (ib.failed || 0);
  $("#statTasks").textContent = total;
  $("#statRunning").textContent = active;
  $("#statDone").textContent = done;
  $("#statTasksNote").textContent = total ? `${by.completed || 0} 个已完成` : "等待创建";
  $("#statRunningNote").textContent = active ? `${active} 个执行中` : (paused ? `${paused} 个已暂停` : "队列空闲");
  $("#statDoneNote").textContent = failed ? `${failed} 个文件失败` : "输出至 /output";
  $("#navTaskCount").textContent = total;
  $("#statOffline").textContent = state.boot.offline?.offline_packages || 0;
  const counts = {
    all: total,
    running: (by.queued || 0) + (by.running || 0) + (by.pausing || 0) + (by.cancelling || 0),
    paused: paused,
    failed: (by.failed || 0) + (by.partial || 0),
    completed: (by.completed || 0),
  };
  $$("[data-count]").forEach((el) => { el.textContent = counts[el.dataset.count] ?? 0; });
}

/* ------------------------------- task form ------------------------------ */
function fillSelect(select, langs, { auto = false, value = null } = {}) {
  if (!select) return;
  const current = value ?? select.value;
  select.innerHTML = "";
  if (auto) {
    const o = document.createElement("option");
    o.value = "auto"; o.textContent = "自动检测";
    select.appendChild(o);
  }
  langs.forEach((l) => {
    const o = document.createElement("option");
    o.value = l.code;
    o.textContent = `${l.native} · ${l.name}`;
    select.appendChild(o);
  });
  if (current && Array.from(select.options).some((o) => o.value === current)) select.value = current;
}

function initForm() {
  const cfg = state.boot.config;
  $("#inputDir").value = cfg.input_dir || "/input";
  $("#outputDir").value = cfg.output_dir || "/output";
  fillSelect($("#targetLang"), state.languages, { value: state.settings.default_target || "zh" });
  fillSelect($("#sourceLang"), state.languages, { auto: true, value: "auto" });
  const mode = state.settings.default_mode || "offline_first";
  $("#mode").value = mode;
  syncAiStart(mode);
  $("#autoStart").checked = true;
  $("#sourceFilterMs")._setLanguages?.(state.languages);
  $("#sourceFilterMs")._setDisabled?.(false);
  syncNaming();
}

function syncAiStart(mode) {
  const el = $("#aiStart");
  if (!el) return;
  el.disabled = mode === "offline_only";
  el.checked = mode === "ai_first";
}

function syncStartSetting(mode) {
  const el = $("#startWithAi");
  if (!el) return;
  el.disabled = mode === "offline_only";
  el.checked = mode === "ai_first";
}

function syncNaming() {
  const custom = $("#outputName").value.trim();
  $("#nameSuffix").disabled = !!custom;
}

function initTaskForm() {
  const form = $("#taskForm");
  const msRoot = $("#sourceFilterMs");
  const ms = createMultiSelect(msRoot, state.languages);
  msRoot._setDisabled = (disabled) => {
    ms.setDisabled(disabled);
    if (disabled) ms.set([]);
  };

  $("#mode").addEventListener("change", (e) => syncAiStart(e.target.value));
  $("#aiStart").addEventListener("change", (e) => {
    if (e.target.checked) $("#mode").value = "ai_first";
    else if ($("#mode").value === "ai_first") $("#mode").value = "offline_first";
    syncAiStart($("#mode").value);
  });
  $("#sourceLang").addEventListener("change", (e) => {
    const auto = e.target.value === "auto";
    msRoot._setDisabled?.(!auto);
    const hint = msRoot.parentElement.querySelector("small");
    if (hint) hint.textContent = auto
      ? "自动识别源语言后，只处理命中的语言；已是目标语言的文件会跳过"
      : "已指定源语言；仍会按所选语言过滤文件，不匹配的文件会跳过";
  });

  $("#outputName").addEventListener("input", syncNaming);
  $$("[data-template]", form).forEach((btn) => {
    btn.addEventListener("click", () => {
      $("#outputName").value = btn.dataset.template;
      syncNaming();
      $("#outputName").focus();
    });
  });

  form.addEventListener("submit", createTask);
}

function taskBody() {
  let mode = $("#mode").value;
  if (mode !== "offline_only") mode = $("#aiStart").checked ? "ai_first" : "offline_first";
  return {
    input_dir: $("#inputDir").value.trim() || "/input",
    output_dir: $("#outputDir").value.trim() || "/output",
    target_lang: $("#targetLang").value,
    source_lang: $("#sourceLang").value || "auto",
    source_filter: $("#sourceFilterMs")._get ? $("#sourceFilterMs")._get() : [],
    mode,
    output_name: $("#outputName").value.trim(),
    name_suffix: $("#nameSuffix").value.trim(),
    preserve_tree: $("#preserveTree").checked,
    auto_start: $("#autoStart").checked,
  };
}

async function createTask(event) {
  event.preventDefault();
  const btn = $("#createTaskBtn");
  const errorBox = $("#formError");
  errorBox.hidden = true;
  const body = taskBody();
  if (!body.input_dir || !body.output_dir) {
    errorBox.textContent = "请填写输入与输出路径。";
    errorBox.hidden = false;
    return;
  }
  btn.disabled = true;
  const old = btn.innerHTML;
  btn.innerHTML = `<span class="spinner" style="width:14px;height:14px;border-width:1.5px"></span>正在创建…`;
  try {
    const task = await api("/api/tasks", { method: "POST", body });
    toast("任务已创建", `${task.total_files} 个文件 · ${lang(task.target_lang)}`, "success");
    state.selected.clear();
    await refreshTasks();
    location.hash = "#/tasks";
  } catch (err) {
    errorBox.textContent = err.message;
    errorBox.hidden = false;
    toast("创建失败", err.message, "error");
  } finally {
    btn.disabled = false;
    btn.innerHTML = old;
  }
}

/* ----------------------------- input scanner ---------------------------- */
const scanState = { root: '', files: [], picked: new Set(), query: '', token: 0 };

function scanOpen() {
  const root = $("#scanModal");
  root.classList.add("open");
  $("#scanAdd").disabled = true;
  $("#scanList").innerHTML = "";
  $("#scanEmpty").hidden = true;
  $("#scanSummary").textContent = "正在扫描输入目录…";
  $("#scanSelectedCount").textContent = "已选 0 个";
  $("#scanFootnote").textContent = "支持的文件才能勾选；不支持的格式会置灰显示。";
  $("#scanSearch").value = "";
  scanState.root = "";
  scanState.files = [];
  scanState.picked.clear();
  scanState.query = "";
  scanLoad();
}

async function scanLoad() {
  const input = $("#inputDir").value.trim() || "/input";
  const token = ++scanState.token;
  try {
    const data = await api("/api/scan", { method: "POST", body: { path: input } });
    if (token !== scanState.token || !$("#scanModal").classList.contains("open")) return;
    scanState.root = data.root;
    scanState.files = data.files || [];
    scanState.picked.clear();
    $("#inputDir").value = data.root;
    $("#scanSummary").textContent = `${data.total} 个文件 · ${data.supported} 个可翻译`;
    $("#scanFootnote").textContent = data.truncated
      ? "文件过多，只显示前 5000 个；请缩小输入目录后重试。"
      : "支持的文件才能勾选；不支持的格式会置灰显示。";
    if (!scanState.files.length) {
      $("#scanList").innerHTML = "";
      $("#scanEmpty").hidden = false;
      $(".scan-empty b").textContent = "没有扫描到文件";
      $(".scan-empty span").textContent = "请确认输入目录已经挂载，且文件不是隐藏文件。";
    } else {
      $("#scanEmpty").hidden = true;
    }
    renderScan();
  } catch (err) {
    if (token !== scanState.token) return;
    $("#scanSummary").textContent = "扫描失败";
    $("#scanList").innerHTML = "";
    $("#scanEmpty").hidden = false;
    $(".scan-empty b").textContent = "无法扫描输入目录";
    $(".scan-empty span").textContent = err.message;
    $("#scanFootnote").textContent = err.message;
    $("#scanAdd").disabled = true;
  }
}

function renderScan() {
  const q = scanState.query.trim().toLowerCase();
  const list = scanState.files.filter((f) => !q || f.name.toLowerCase().includes(q) || f.rel.toLowerCase().includes(q) || f.ext.includes(q));
  if (!list.length) {
    $("#scanList").innerHTML = "";
    $("#scanEmpty").hidden = false;
    $(".scan-empty b").textContent = "没有匹配的文件";
    $(".scan-empty span").textContent = "换个关键词，或清空筛选条件。";
    updateScanCount();
    return;
  }
  $("#scanEmpty").hidden = true;
  $("#scanList").innerHTML = list.map((f) => {
    const picked = scanState.picked.has(f.path);
    const ext = (f.ext || "file").replace(".", "").slice(0, 4).toUpperCase();
    return `<label class="scan-item ${picked ? "picked" : ""} ${f.supported ? "" : "unsupported"}">
      <input type="checkbox" class="scan-check" data-path="${esc(f.path)}" ${picked ? "checked" : ""} ${f.supported ? "" : "disabled"}>
      <span class="scan-file-ico ${f.supported ? "" : "unsupported"}">${icon(IMAGE_EXT.has(f.ext) ? "eye" : "file")}</span>
      <span class="scan-file-main"><b>${esc(f.rel)}</b><small>${esc(f.path)}</small></span>
      <span class="scan-file-meta"><span class="scan-badge ${f.supported ? "ok" : ""}">${esc(ext)}</span><span>${bytes(f.size)}</span></span>
    </label>`;
  }).join("");
  updateScanCount();
}

function updateScanCount() {
  const n = scanState.picked.size;
  $("#scanSelectedCount").textContent = `已选 ${n} 个`;
  $("#scanAdd").disabled = n === 0;
  $("#scanList").querySelectorAll(".scan-item").forEach((row) => {
    const cb = row.querySelector(".scan-check");
    if (cb) row.classList.toggle("picked", cb.checked);
  });
}

function scanClose() {
  scanState.token += 1;
  $("#scanModal").classList.remove("open");
}

async function addScannedTasks() {
  const chosen = scanState.files.filter((f) => scanState.picked.has(f.path) && f.supported);
  if (!chosen.length || !scanState.root) return;
  const btn = $("#scanAdd");
  const old = btn.innerHTML;
  btn.disabled = true;
  btn.innerHTML = `<span class="spinner" style="width:14px;height:14px;border-width:1.5px"></span>正在加入…`;
  try {
    const body = taskBody();
    body.input_dir = scanState.root;
    body.files = chosen.map((f) => f.path);
    body.name = `扫描选择 · ${chosen.length} 个文件`;
    const task = await api("/api/tasks", { method: "POST", body });
    toast("已加入任务队列", `${task.total_files} 个文件 · ${lang(task.target_lang)}`, "success");
    scanClose();
    state.selected.clear();
    await refreshTasks();
    location.hash = "#/tasks";
  } catch (err) {
    toast("加入失败", err.message, "error");
  } finally {
    btn.disabled = scanState.picked.size === 0;
    btn.innerHTML = old;
  }
}

function initScanner() {
  $("#scanInputBtn").addEventListener("click", scanOpen);
  $("#scanClose").addEventListener("click", scanClose);
  $("#scanCancel").addEventListener("click", scanClose);
  $$("#scanModal [data-scan-close]").forEach((el) => el.addEventListener("click", scanClose));
  $("#scanAdd").addEventListener("click", addScannedTasks);
  $("#scanSearch").addEventListener("input", (e) => {
    scanState.query = e.target.value;
    renderScan();
  });
  $("#scanSelectAll").addEventListener("click", () => {
    const q = scanState.query.trim().toLowerCase();
    scanState.files.forEach((f) => {
      if (f.supported && (!q || f.name.toLowerCase().includes(q) || f.rel.toLowerCase().includes(q) || f.ext.includes(q))) {
        scanState.picked.add(f.path);
      }
    });
    renderScan();
  });
  $("#scanClear").addEventListener("click", () => {
    scanState.picked.clear();
    renderScan();
  });
  $("#scanList").addEventListener("change", (e) => {
    const cb = e.target.closest(".scan-check");
    if (!cb) return;
    if (cb.checked) scanState.picked.add(cb.dataset.path);
    else scanState.picked.delete(cb.dataset.path);
    updateScanCount();
  });
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && $("#scanModal").classList.contains("open")) scanClose();
  });
}

/* ----------------------------- multiselect ------------------------------ */
function createMultiSelect(root, languages) {
  if (!root) return null;
  let langs = languages || [];
  const control = $(".ms-control", root);
  const panel = $(".ms-panel", root);
  const valueEl = $(".ms-value", root);
  const optionsEl = $(".ms-options", root);
  const searchEl = $(".ms-search input", root);
  let selected = new Set();
  let open = false;
  let disabled = false;

  const labelFor = (code) => {
    const l = state.langMap.get(code);
    return l ? l.native : code;
  };

  function renderValue() {
    if (!selected.size) {
      valueEl.innerHTML = `<span class="ms-value-text">全部语言</span>`;
      valueEl.className = "ms-value";
      return;
    }
    valueEl.className = "ms-value";
    valueEl.innerHTML = Array.from(selected).slice(0, 8).map((c) =>
      `<span class="ms-chip">${esc(labelFor(c))}</span>`
    ).join("") + (selected.size > 8 ? `<span class="ms-chip">+${selected.size - 8}</span>` : "");
  }

  function renderOptions(query = "") {
    const q = query.trim().toLowerCase();
    const list = langs.filter((l) => !q || l.name.toLowerCase().includes(q) || l.native.toLowerCase().includes(q) || l.code.includes(q));
    if (!list.length) {
      optionsEl.innerHTML = `<div class="ms-empty">没有匹配的语言</div>`;
      return;
    }
    optionsEl.innerHTML = list.map((l) => `
      <label class="ms-option">
        <input type="checkbox" value="${esc(l.code)}" ${selected.has(l.code) ? "checked" : ""} ${disabled ? "disabled" : ""}>
        <span>${esc(l.native)}</span>
        <span class="native">${esc(l.name)}</span>
      </label>`).join("");
  }

  function setOpen(next) {
    if (disabled) return;
    open = next;
    panel.hidden = !next;
    control.classList.toggle("open", next);
    control.setAttribute("aria-expanded", String(next));
    if (next) {
      renderOptions(searchEl.value);
      setTimeout(() => searchEl.focus(), 20);
    }
  }

  control.addEventListener("click", (e) => { e.stopPropagation(); setOpen(!open); });
  searchEl.addEventListener("input", () => renderOptions(searchEl.value));
  optionsEl.addEventListener("change", (e) => {
    const input = e.target.closest("input[type=checkbox]");
    if (!input) return;
    if (input.checked) selected.add(input.value); else selected.delete(input.value);
    renderValue();
  });
  $("[data-ms-clear]", root).addEventListener("click", () => {
    selected.clear(); renderValue(); renderOptions(searchEl.value);
  });
  $("[data-ms-done]", root).addEventListener("click", () => setOpen(false));
  document.addEventListener("click", (e) => {
    if (open && !root.contains(e.target)) setOpen(false);
  });

  const apiObj = {
    get: () => Array.from(selected),
    setLanguages: (next) => {
      langs = next || [];
      renderValue();
      if (open) renderOptions(searchEl.value);
    },
    set: (values) => { selected = new Set(values || []); renderValue(); if (open) renderOptions(searchEl.value); },
    setDisabled: (value) => {
      disabled = !!value;
      control.disabled = disabled;
      control.style.opacity = disabled ? ".55" : "";
      if (disabled) setOpen(false);
    },
  };
  root._get = apiObj.get;
  root._setLanguages = apiObj.setLanguages;
  return apiObj;
}

/* -------------------------------- tasks --------------------------------- */
async function refreshTasks({ silent = true } = {}) {
  try {
    const data = await api("/api/tasks");
    state.tasks = data.tasks || [];
    state.stats = data.stats || state.stats;
    // Drop selections for tasks that no longer exist.
    const ids = new Set(state.tasks.map((t) => t.id));
    for (const id of Array.from(state.selected)) if (!ids.has(id)) state.selected.delete(id);
    updateStats();
    updateTaskCounts();
    renderTasks();
  } catch (err) {
    if (!silent) toast("刷新失败", err.message, "error");
  }
}

function updateTaskCounts() {
  const by = state.stats.by_status || {};
  const counts = {
    all: state.stats.tasks || 0,
    running: (by.queued || 0) + (by.running || 0) + (by.pausing || 0) + (by.cancelling || 0),
    paused: (by.paused || 0) + (by.held || 0),
    failed: (by.failed || 0) + (by.partial || 0),
    completed: by.completed || 0,
  };
  $$("[data-count]").forEach((el) => { el.textContent = counts[el.dataset.count] ?? 0; });
}

function filteredTasks() {
  const f = state.filter;
  if (f === "all") return state.tasks;
  if (f === "running") return state.tasks.filter((t) => ["queued", "running", "pausing", "cancelling"].includes(t.status));
  if (f === "paused") return state.tasks.filter((t) => ["paused", "held"].includes(t.status));
  if (f === "failed") return state.tasks.filter((t) => ["failed", "partial"].includes(t.status));
  if (f === "completed") return state.tasks.filter((t) => t.status === "completed");
  return state.tasks;
}

function actionButtons(task) {
  const st = task.status;
  const id = esc(task.id);
  let html = "";
  if (["paused", "failed", "partial", "cancelled", "held"].includes(st)) {
    const isRetry = ["failed", "partial"].includes(st);
    const ico = isRetry ? "retry" : "play";
    const title = isRetry ? "重试失败文件" : st === "paused" ? "继续任务" : "开始任务";
    html += `<button class="act-btn play" data-act="${isRetry ? "retry" : "start"}" data-id="${id}" title="${title}">${icon(ico)}</button>`;
  }
  if (["running", "queued", "pausing"].includes(st)) {
    html += `<button class="act-btn pause" data-act="pause" data-id="${id}" title="暂停任务" ${st === "pausing" ? "disabled" : ""}>${icon("pause")}</button>`;
  }
  if (["running", "queued", "paused", "pausing", "cancelling"].includes(st)) {
    html += `<button class="act-btn stop" data-act="cancel" data-id="${id}" title="取消任务" ${st === "cancelling" ? "disabled" : ""}>${icon("stop")}</button>`;
  }
  if (["completed", "failed", "partial", "cancelled", "held"].includes(st)) {
    html += `<button class="act-btn del" data-act="delete" data-id="${id}" title="删除记录">${icon("trash")}</button>`;
  }
  return html;
}

function taskCard(task) {
  const [label, statusCls] = statusMeta(task.status);
  const total = task.total_files || 0;
  const done = (task.done_files || 0) + (task.skipped_files || 0);
  const progress = task.progress != null ? Math.round(task.progress * 100) : pct(done, total);
  const failed = task.failed_files || 0;
  const selected = state.selected.has(task.id);
  const expanded = state.expanded.has(task.id);
  const filter = task.source_filter?.length ? task.source_filter.map(lang).join(" / ") : "";
  const routeText = task.source_lang === "auto"
    ? `${filter ? `过滤 ${filter}` : "自动识别"} <span class="sep">→</span> <b>${lang(task.target_lang)}</b>`
    : `<b>${lang(task.source_lang)}</b> <span class="sep">→</span> <b>${lang(task.target_lang)}</b>`;
  const modeLabel = { offline_first: "离线优先", ai_first: "AI 优先", offline_only: "仅离线" }[task.mode] || task.mode;
  const detail = expanded ? renderDetail(task.id) : "";
  return `
  <article class="task-card ${selected ? "selected" : ""} ${expanded ? "expanded" : ""}" data-task="${esc(task.id)}">
    <div class="task-check">
      <label class="checkbox" title="选择任务">
        <input type="checkbox" class="task-select" data-id="${esc(task.id)}" ${selected ? "checked" : ""}>
        <span></span>
      </label>
    </div>
    <div class="task-body">
      <div class="task-top">
        <div class="task-name">
          <div class="task-title" title="${esc(task.name)}">${esc(task.name)}</div>
          <div class="task-meta">
            <span class="route">${routeText}</span>
            <span class="sep">·</span><span>${esc(modeLabel)}</span>
            <span class="sep">·</span><span>${total} 个文件</span>
            <span class="sep">·</span><span>${ago(task.created_at)}</span>
          </div>
        </div>
        <span class="status-badge ${statusCls}"><i></i>${esc(label)}</span>
        <div class="task-actions">${actionButtons(task)}</div>
      </div>
      <div class="progress-wrap">
        <div class="progress-track ${failed ? "failed" : ""}"><div class="progress-fill" style="width:${progress}%"></div></div>
        <span class="progress-nums">${done}/${total} · ${progress}%</span>
      </div>
      <div class="task-foot">
        <span class="path">${icon("folder")}${esc(task.output_dir)}</span>
        ${task.error ? `<span class="err" title="${esc(task.error)}">${icon("alert")} ${esc(task.error)}</span>` : ""}
        ${failed ? `<span class="err">${failed} 个文件失败</span>` : ""}
        ${task.skipped_files ? `<span>${task.skipped_files} 个已跳过</span>` : ""}
        <span style="margin-left:auto">${icon("clock")} ${clock(task.updated_at)}</span>
      </div>
      ${detail}
    </div>
  </article>`;
}

function renderDetail(taskId) {
  const detail = state.details.get(taskId);
  if (!detail) return `<div class="task-detail"><div class="detail-item">正在读取文件进度…</div></div>`;
  const items = detail.items || [];
  if (!items.length) return `<div class="task-detail"><div class="detail-item">暂无文件记录</div></div>`;
  const shown = items.slice(0, 300);
  const rows = shown.map((it) => {
    const [label] = statusMeta(it.status);
    return `<div class="detail-item">
      <span class="detail-dot ${esc(it.status)}"></span>
      <span class="detail-name" title="${esc(it.rel_path)}">${esc(it.rel_path)}</span>
      <span>${esc(label)}</span>
      ${it.error ? `<span class="detail-err" title="${esc(it.error)}">${esc(it.error)}</span>` : ""}
    </div>`;
  }).join("");
  return `<div class="task-detail">${rows}${items.length > 300 ? `<div class="detail-item">仅显示前 300 个文件，共 ${items.length} 个</div>` : ""}</div>`;
}

function renderTasks() {
  const list = $("#taskList");
  const empty = $("#taskEmpty");
  if (!list) return;
  const tasks = filteredTasks();
  list.innerHTML = tasks.map(taskCard).join("");
  empty.hidden = tasks.length > 0;
  if (!tasks.length) {
    const texts = {
      running: ["没有进行中的任务", "启动一个任务后，进度会实时显示在这里。"],
      paused: ["没有暂停的任务", "运行中任务暂停后会保留断点。"],
      failed: ["没有失败任务", "目前所有任务都运行顺利。"],
      completed: ["还没有已完成任务", "完成翻译后记录会显示在这里。"],
      all: ["队列还是空的", "创建一个任务，选择输入目录与目标语言，剩下的交给 LinguaFlow。"],
    }[state.filter] || ["没有任务", ""];
    $("h3", empty).textContent = texts[0];
    $("p", empty).textContent = texts[1];
    $("#emptyNewTask").style.display = state.filter === "all" ? "" : "none";
  }
  updateBulkBar();
}

async function loadDetail(taskId) {
  try {
    const detail = await api(`/api/tasks/${encodeURIComponent(taskId)}`);
    state.details.set(taskId, detail);
    renderTasks();
  } catch (err) {
    toast("读取详情失败", err.message, "error");
  }
}

function updateBulkBar() {
  const bar = $("#bulkBar");
  if (!bar) return;
  const n = state.selected.size;
  bar.hidden = n === 0;
  $("#bulkCount").textContent = n;
  $$("[data-bulk]").forEach((btn) => {
    const act = btn.dataset.bulk;
    const tasks = state.tasks.filter((t) => state.selected.has(t.id));
    let enabled = tasks.length > 0;
    if (act === "start") enabled = tasks.some((t) => ["paused", "cancelled", "failed", "partial", "held"].includes(t.status));
    if (act === "pause") enabled = tasks.some((t) => ["running", "queued"].includes(t.status));
    if (act === "cancel") enabled = tasks.some((t) => ["running", "queued", "paused", "held"].includes(t.status));
    if (act === "retry") enabled = tasks.some((t) => ["failed", "partial"].includes(t.status));
    btn.disabled = !enabled;
  });
  const deleteBtn = $("#bulkDelete");
  if (deleteBtn) deleteBtn.disabled = !state.tasks.some((t) => state.selected.has(t.id) && ["completed", "failed", "partial", "cancelled", "held"].includes(t.status));
}

async function runTaskAction(taskId, action, { quiet = false } = {}) {
  try {
    const path = action === "delete"
      ? `/api/tasks/${encodeURIComponent(taskId)}`
      : `/api/tasks/${encodeURIComponent(taskId)}/${action}`;
    if (action === "delete") {
      const task = state.tasks.find((t) => t.id === taskId);
      if (!await confirmDialog({
        title: "删除任务记录？",
        message: `将移除「${task?.name || taskId}」的任务与文件记录，输出目录中的文件不会被删除。`,
        confirmText: "删除记录", danger: true,
      })) return;
      await api(path, { method: "DELETE" });
      state.selected.delete(taskId);
      state.expanded.delete(taskId);
      state.details.delete(taskId);
      toast("记录已删除", "", "success");
    } else {
      await api(path, { method: "POST", body: {} });
      if (!quiet) {
        const labels = { start: "任务已启动", pause: "暂停请求已发送", cancel: "取消请求已发送", retry: "已重新排队失败文件" };
        toast(labels[action] || "操作完成", "", "success");
      }
    }
    await refreshTasks();
  } catch (err) {
    toast("操作失败", err.message, "error");
  }
}

async function batchAction(action) {
  const ids = Array.from(state.selected);
  if (!ids.length) return;
  if (action === "cancel" && !await confirmDialog({
    title: `取消 ${ids.length} 个任务？`,
    message: "当前段落完成后停止，已翻译的文件与进度会保留；可稍后重新启动。",
    confirmText: "取消任务", danger: true,
  })) return;
  try {
    const data = await api("/api/tasks/batch", { method: "POST", body: { action, ids } });
    const results = data.results || [];
    const ok = results.filter((r) => r.ok).length;
    const bad = results.filter((r) => !r.ok);
    state.selected.clear();
    toast("批量操作完成", `${ok} 个成功${bad.length ? `，${bad.length} 个跳过：${bad[0].error || ""}` : ""}`, bad.length ? "warn" : "success");
    await refreshTasks();
  } catch (err) {
    toast("批量操作失败", err.message, "error");
  }
}

async function batchDelete() {
  const ids = state.tasks.filter((t) => state.selected.has(t.id) && ["completed", "failed", "partial", "cancelled", "held"].includes(t.status)).map((t) => t.id);
  if (!ids.length) { toast("没有可删除的记录", "运行中或排队的任务需要先取消。", "warn"); return; }
  if (!await confirmDialog({
    title: `删除 ${ids.length} 条任务记录？`,
    message: "只会移除任务和文件进度记录，输出目录中的文件会保留。",
    confirmText: "删除记录", danger: true,
  })) return;
  let ok = 0;
  for (const id of ids) {
    try { await api(`/api/tasks/${encodeURIComponent(id)}`, { method: "DELETE" }); ok++; state.selected.delete(id); } catch (e) {}
  }
  toast("记录已清理", `${ok} 条`, "success");
  await refreshTasks();
}

async function allAction(action) {
  let tasks;
  if (action === "start") tasks = state.tasks.filter((t) => !["completed", "running", "pausing", "cancelling"].includes(t.status));
  else if (action === "pause") tasks = state.tasks.filter((t) => ["running", "queued"].includes(t.status));
  else tasks = state.tasks.filter((t) => ["running", "queued", "paused", "pausing", "held"].includes(t.status));
  if (!tasks.length) { toast("没有需要操作的任务", "", "warn"); return; }
  if (action === "cancel" && !await confirmDialog({
    title: `取消全部 ${tasks.length} 个任务？`,
    message: "所有进行中与排队的任务都会停止，已完成的翻译结果和断点都会保留。",
    confirmText: "全部取消", danger: true,
  })) return;
  const ids = tasks.map((t) => t.id);
  try {
    const data = await api("/api/tasks/batch", { method: "POST", body: { action, ids } });
    const results = data.results || [];
    const ok = results.filter((r) => r.ok).length;
    toast("全部操作完成", `${ok}/${ids.length} 个任务已处理`, "success");
    await refreshTasks();
  } catch (err) {
    toast("操作失败", err.message, "error");
  }
}

/* -------------------------------- models -------------------------------- */
async function loadModels() {
  try {
    state.models = await api("/api/models");
    renderModels();
  } catch (err) {
    toast("读取模型失败", err.message, "error");
  }
}

function renderModels() {
  if (!state.models) return;
  const installed = state.models.installed || [];
  const catalog = state.models.catalog || [];
  $("#installedCount").textContent = installed.length;
  $("#catalogCount").textContent = catalog.length;
  const list = $("#installedList");
  if (!installed.length) {
    list.innerHTML = `<div class="empty-mini">${icon("box")}还没有离线语言包<br><small>下载后即可完全离线翻译</small></div>`;
  } else {
    list.innerHTML = installed.map((m) => `
      <div class="installed-item">
        <span class="pair-badge"><span class="pair-lang">${esc(lang(m.from))}</span>${icon("arrow")}<span class="pair-lang">${esc(lang(m.to))}</span></span>
        <div class="installed-info"><b>${esc(m.name || `${m.from} → ${m.to}`)}</b><small>${bytes(m.size)} · 本地模型</small></div>
        <button class="act-btn del" data-remove-model="${esc(m.from)}/${esc(m.to)}" title="删除语言包">${icon("trash")}</button>
      </div>`).join("");
  }

  const q = state.catalogQuery.trim().toLowerCase();
  const filtered = catalog.filter((c) => !q ||
    `${c.from} ${c.to} ${c.from_name || ""} ${c.to_name || ""} ${lang(c.from)} ${lang(c.to)}`.toLowerCase().includes(q));
  const catalogList = $("#catalogList");
  if (!filtered.length) {
    catalogList.innerHTML = `<div class="catalog-empty">${catalog.length ? "没有匹配的语言包" : "目录尚未缓存。点击“刷新目录”从镜像获取，或手动导入 .argosmodel。"}</div>`;
  } else {
    catalogList.innerHTML = filtered.map((c) => `
      <div class="catalog-item">
        <span class="catalog-pair">${esc(lang(c.from))} ${icon("arrow")} ${esc(lang(c.to))}</span>
        <span class="catalog-name">${esc(c.from_name || "")} → ${esc(c.to_name || "")}</span>
        <span class="catalog-ver">v${esc(c.version || "—")}</span>
        <button class="catalog-install ${c.installed ? "installed" : ""}" data-install="${esc(c.from)}/${esc(c.to)}">
          ${icon(c.installed ? "check" : "download")}${c.installed ? "重装" : "安装"}
        </button>
      </div>`).join("");
  }
}

async function installModel(from, to) {
  from = from || $("#modelFrom").value;
  to = to || $("#modelTo").value;
  if (!from || !to) return;
  if (from === to) { toast("语言对无效", "源语言与目标语言不能相同。", "warn"); return; }
  const btn = $("#installModel");
  btn.disabled = true;
  try {
    const job = await api("/api/models/download", { method: "POST", body: { from, to } });
    state.installJob = job;
    $("#installProgress").hidden = false;
    $("#installLabel").textContent = `正在安装 ${lang(from)} → ${lang(to)}`;
    $("#installDetail").textContent = "连接模型镜像…";
    clearInterval(state.installTimer);
    state.installTimer = setInterval(() => pollInstall(job.id, from, to), 1200);
  } catch (err) {
    toast("安装失败", err.message, "error");
    btn.disabled = false;
    $("#installProgress").hidden = true;
  }
}

async function pollInstall(jobId, from, to) {
  try {
    const job = await api(`/api/models/jobs/${encodeURIComponent(jobId)}`);
    if (job.status === "success") {
      clearInterval(state.installTimer);
      $("#installProgress").hidden = true;
      $("#installModel").disabled = false;
      toast("语言包已安装", `${lang(from)} → ${lang(to)} 现在可离线使用`, "success");
      await loadModels();
      await refreshBootstrapStatus();
    } else if (job.status === "error") {
      clearInterval(state.installTimer);
      $("#installProgress").hidden = true;
      $("#installModel").disabled = false;
      toast("安装失败", job.error || "下载或解压失败", "error");
    } else {
      $("#installDetail").textContent = job.status === "running" ? "下载 / 校验 / 解压中…" : "等待开始…";
    }
  } catch (err) {
    clearInterval(state.installTimer);
    $("#installProgress").hidden = true;
    $("#installModel").disabled = false;
    toast("安装状态读取失败", err.message, "error");
  }
}

async function refreshBootstrapStatus() {
  try {
    const old = state.boot;
    state.boot = await api("/api/bootstrap");
    state.settings = state.boot.settings;
    renderEngine();
    fillSettings(state.settings);
    if (old) updateStats();
  } catch (e) {}
}

/* ------------------------------- settings ------------------------------- */
function fillSettings(s) {
  if (!s) return;
  const ai = s.ai || {};
  $("#aiEnabled").checked = !!ai.enabled;
  $("#aiBase").value = ai.base_url || "";
  $("#aiModel").value = ai.model || "";
  $("#aiKey").value = "";
  $("#aiKey").placeholder = ai.api_key_set ? "已保存，留空保持不变" : "sk-…";
  $("#keyState").textContent = ai.api_key_set ? "密钥已保存在本地数据卷" : "未设置密钥（本地兼容服务可留空）";
  $("#aiTemp").value = ai.temperature ?? 0.2;
  $("#aiTimeout").value = ai.timeout ?? 120;
  $("#aiPolish").checked = ai.polish !== false;
  fillSelect($("#defaultTarget"), state.languages, { value: s.default_target || "zh" });
  const defaultMode = s.default_mode || "offline_first";
  $("#defaultMode").value = defaultMode;
  syncStartSetting(defaultMode);
  $("#autoResume").checked = s.auto_resume !== false;
  $$("#aiPresets .preset").forEach((b) => {
    b.classList.toggle("active", b.dataset.base === ai.base_url && b.dataset.model === ai.model);
  });
}

async function saveSettings({ silent = false } = {}) {
  const ai = {
    enabled: $("#aiEnabled").checked,
    base_url: $("#aiBase").value.trim(),
    model: $("#aiModel").value.trim(),
    temperature: Number($("#aiTemp").value || 0.2),
    timeout: Number($("#aiTimeout").value || 120),
    polish: $("#aiPolish").checked,
  };
  const key = $("#aiKey").value.trim();
  if (key) ai.api_key = key;
  const body = {
    ai,
    default_target: $("#defaultTarget").value,
    default_mode: $("#defaultMode").value === "offline_only"
      ? "offline_only"
      : ($("#startWithAi").checked ? "ai_first" : "offline_first"),
    auto_resume: $("#autoResume").checked,
  };
  try {
    const saved = await api("/api/settings", { method: "PUT", body });
    state.settings = saved;
    state.boot.settings = saved;
    fillSettings(saved);
    const mode = saved.default_mode || "offline_first";
    $("#mode").value = mode;
    syncAiStart(mode);
    renderEngine();
    if (!silent) toast("设置已保存", "新的任务将使用更新后的默认值。", "success");
    return true;
  } catch (err) {
    toast("保存失败", err.message, "error");
    return false;
  }
}

async function loadSystem() {
  try {
    const sys = await api("/api/system");
    $("#sysVersion").textContent = sys.version || "—";
    $("#sysPython").textContent = sys.python || "—";
    $("#sysSoffice").textContent = sys.soffice ? "已就绪" : "未安装";
    $("#sysOcr").textContent = sys.ocr ? `已就绪 · ${(sys.ocr_languages || []).length} 种` : "未安装";
    $("#sysData").textContent = sys.data_dir || "—";
    $("#appVersion").textContent = sys.version || "1.0.0";
    const disk = sys.disk || {};
    if (disk.total) {
      const usedPct = Math.round(((disk.total - disk.free) / disk.total) * 100);
      $("#diskText").textContent = `${bytes(disk.used)} / ${bytes(disk.total)} · ${usedPct}%`;
      $("#diskBar").style.width = `${usedPct}%`;
    } else {
      $("#diskText").textContent = "不可用";
    }
  } catch (e) {}
}

/* --------------------------------- logs --------------------------------- */
function logRow(row) {
  const lvl = ["success", "warn", "error", "debug"].includes(row.level) ? row.level : "info";
  return `<div class="log-line lvl-${lvl}">
    <span class="log-time">${clock(row.ts)}</span>
    <span class="log-lvl">${esc((row.level || "info").toUpperCase())}</span>
    <span class="log-msg">${esc(row.message)}</span>
    ${row.task_id ? `<span class="log-task">#${esc(String(row.task_id).slice(0, 8))}</span>` : ""}
  </div>`;
}

function renderLogs() {
  const body = $("#logBody");
  if (!body) return;
  if (!state.logs.length) {
    body.innerHTML = `<div class="log-empty">等待日志输出…</div>`;
  } else {
    body.innerHTML = state.logs.map(logRow).join("");
  }
  $("#logCount").textContent = `${state.logs.length} 行`;
  if ($("#autoScroll").checked) body.scrollTop = body.scrollHeight;
}

function appendLog(row) {
  if (!row || !row.id) return;
  if (row.id <= state.lastLogId) return;
  state.lastLogId = row.id;
  state.logs.push(row);
  if (state.logs.length > 2500) state.logs.splice(0, state.logs.length - 2500);
  const body = $("#logBody");
  if (!body) return;
  if (state.logs.length === 1) body.innerHTML = "";
  body.insertAdjacentHTML("beforeend", logRow(row));
  while (body.children.length > 2500) body.firstElementChild.remove();
  $("#logCount").textContent = `${state.logs.length} 行`;
  if ($("#autoScroll").checked) body.scrollTop = body.scrollHeight;
  if (!state.logVisible) {
    state.logUnread = true;
    $("#logUnread").hidden = false;
  }
}

async function loadLogs() {
  try {
    const data = await api("/api/logs?limit=300");
    state.logs = data.logs || [];
    if (state.logs.length) state.lastLogId = state.logs[state.logs.length - 1].id;
    renderLogs();
  } catch (e) {
    // The stream will retry; no need to block the UI.
  }
}

function connectLogs() {
  if (!window.EventSource) {
    $("#logConn").innerHTML = `<i></i>不支持 SSE`;
    return;
  }
  if (state.logSource) state.logSource.close();
  const source = new EventSource(`/api/logs/stream?after=${state.lastLogId}`);
  state.logSource = source;
  const conn = $("#logConn");
  source.addEventListener("open", () => {
    conn.className = "log-conn on";
    conn.innerHTML = "<i></i>实时";
  });
  source.addEventListener("hello", () => {});
  source.addEventListener("log", (event) => {
    try { appendLog(JSON.parse(event.data)); } catch (e) {}
  });
  source.addEventListener("error", () => {
    conn.className = "log-conn";
    conn.innerHTML = "<i></i>重连中";
  });
}

/* -------------------------------- bootstrap ----------------------------- */
async function bootstrap() {
  $("#bootError").hidden = true;
  try {
    const data = await api("/api/bootstrap");
    state.boot = data;
    state.settings = data.settings;
    state.languages = (data.languages || []).slice().sort((a, b) => a.name.localeCompare(b.name));
    state.langMap = new Map(state.languages.map((l) => [l.code, l]));
    $("#appVersion").textContent = data.config.version || "1.0.0";
    $("#formatCount").textContent = (data.supported_extensions || []).length;
    fillSelect($("#modelFrom"), state.languages, { value: "en" });
    fillSelect($("#modelTo"), state.languages, { value: data.settings.default_target || "zh" });
    initForm();
    fillSettings(data.settings);
    renderEngine();
    route();
    await loadLogs();
    connectLogs();
    await refreshTasks();
    state.pollTimer = setInterval(() => {
      if (!document.hidden) refreshTasks();
    }, 3000);
  } catch (err) {
    $("#bootError").hidden = false;
    $("#bootErrorText").textContent = `${err.message}。请确认容器已启动，端口 5463 可访问。`;
  }
}

function init() {
  bootstrap();
  window.addEventListener("hashchange", route);
  $("#themeToggle").addEventListener("click", () => {
    setTheme(document.documentElement.dataset.theme === "dark" ? "light" : "dark");
  });
  $("#topNewTask").addEventListener("click", goNewTask);
  $("#newTaskFromTasks").addEventListener("click", goNewTask);
  $("#emptyNewTask").addEventListener("click", goNewTask);
  $("#bootRetry").addEventListener("click", bootstrap);

  initTaskForm();
  initScanner();

  $$(".filter-tab").forEach((tab) => {
    tab.addEventListener("click", () => {
      state.filter = tab.dataset.filter;
      $$(".filter-tab").forEach((t) => t.classList.toggle("active", t === tab));
      renderTasks();
    });
  });
  $("#refreshTasks").addEventListener("click", async () => {
    await refreshTasks({ silent: false });
    toast("已刷新", "任务进度来自服务器", "success");
  });
  $("#allStart").addEventListener("click", () => allAction("start"));
  $("#allPause").addEventListener("click", () => allAction("pause"));
  $("#allCancel").addEventListener("click", () => allAction("cancel"));
  $("#retryFailed").addEventListener("click", async () => {
    try {
      const data = await api("/api/tasks/retry-failed", { method: "POST", body: {} });
      toast("失败任务已重新排队", `${data.started || 0} 个任务`, "success");
      await refreshTasks();
    } catch (err) { toast("操作失败", err.message, "error"); }
  });
  $("#clearCompleted").addEventListener("click", async () => {
    if (!await confirmDialog({
      title: "清除已完成任务记录？",
      message: "所有标记为“已完成”的任务与文件进度记录都会删除，输出文件保留。此操作不可撤销。",
      confirmText: "清除记录", danger: true,
    })) return;
    try {
      const data = await api("/api/tasks/clear-completed", { method: "POST", body: {} });
      state.selected.clear();
      toast("已清除完成记录", `${data.cleared || 0} 条`, "success");
      await refreshTasks();
    } catch (err) { toast("清除失败", err.message, "error"); }
  });

  $("#taskList").addEventListener("click", (e) => {
    const action = e.target.closest("[data-act]");
    if (action) {
      e.stopPropagation();
      runTaskAction(action.dataset.id, action.dataset.act);
      return;
    }
    if (e.target.closest(".task-check")) return;
    const card = e.target.closest(".task-card");
    if (!card) return;
    const id = card.dataset.task;
    if (state.expanded.has(id)) {
      state.expanded.delete(id);
      renderTasks();
    } else {
      state.expanded.add(id);
      renderTasks();
      if (!state.details.has(id)) loadDetail(id);
    }
  });
  $("#taskList").addEventListener("change", (e) => {
    const cb = e.target.closest(".task-select");
    if (!cb) return;
    if (cb.checked) state.selected.add(cb.dataset.id); else state.selected.delete(cb.dataset.id);
    cb.closest(".task-card")?.classList.toggle("selected", cb.checked);
    updateBulkBar();
  });
  $("#bulkClear").addEventListener("click", () => { state.selected.clear(); renderTasks(); });
  $$("[data-bulk]").forEach((btn) => btn.addEventListener("click", () => batchAction(btn.dataset.bulk)));
  $("#bulkDelete").addEventListener("click", batchDelete);

  $("#importModels").addEventListener("click", async () => {
    try {
      const data = await api("/api/models/import", { method: "POST", body: {} });
      toast("预加载导入完成", `${(data.installed || []).length} 个语言包`, "success");
      await loadModels();
      await refreshBootstrapStatus();
    } catch (err) { toast("导入失败", err.message, "error"); }
  });
  $("#refreshCatalog").addEventListener("click", async (e) => {
    const btn = e.currentTarget;
    btn.disabled = true;
    const old = btn.innerHTML;
    btn.innerHTML = `<span class="spinner" style="width:13px;height:13px;border-width:1.5px"></span>刷新中`;
    try {
      const data = await api("/api/models/refresh", { method: "POST", body: {} });
      toast("目录已更新", `${data.count || 0} 个语言包`, "success");
      await loadModels();
    } catch (err) {
      toast("刷新失败", err.message, "error");
    } finally {
      btn.disabled = false;
      btn.innerHTML = old;
    }
  });
  $("#installModel").addEventListener("click", () => installModel());
  $$("#modelPresets button").forEach((btn) => btn.addEventListener("click", () => {
    const from = btn.dataset.from || "en";
    const to = btn.dataset.to;
    if (to) {
      $("#modelFrom").value = from;
      $("#modelTo").value = to;
      $("#installModel").focus();
    }
  }));
  $("#modelFrom").addEventListener("change", () => {
    if ($("#modelFrom").value === $("#modelTo").value) {
      const other = state.languages.find((l) => l.code !== $("#modelFrom").value);
      if (other) $("#modelTo").value = other.code;
    }
  });
  $("#modelTo").addEventListener("change", () => {
    if ($("#modelFrom").value === $("#modelTo").value) {
      const other = state.languages.find((l) => l.code !== $("#modelTo").value);
      if (other) $("#modelFrom").value = other.code;
    }
  });
  $("#catalogSearch").addEventListener("input", (e) => {
    state.catalogQuery = e.target.value;
    renderModels();
  });
  $("#catalogList").addEventListener("click", (e) => {
    const btn = e.target.closest("[data-install]");
    if (!btn) return;
    const [from, to] = btn.dataset.install.split("/");
    installModel(from, to);
  });
  $("#installedList").addEventListener("click", async (e) => {
    const btn = e.target.closest("[data-remove-model]");
    if (!btn) return;
    const [from, to] = btn.dataset.removeModel.split("/");
    if (!await confirmDialog({
      title: "删除离线语言包？",
      message: `${lang(from)} → ${lang(to)} 将从本地模型目录删除；之后该语言对会回退到 AI。`,
      confirmText: "删除语言包", danger: true,
    })) return;
    try {
      await api(`/api/models/${encodeURIComponent(from)}/${encodeURIComponent(to)}`, { method: "DELETE" });
      toast("语言包已删除", "", "success");
      await loadModels();
      await refreshBootstrapStatus();
    } catch (err) { toast("删除失败", err.message, "error"); }
  });

  $$("#aiPresets .preset").forEach((btn) => {
    btn.addEventListener("click", () => {
      $("#aiBase").value = btn.dataset.base;
      $("#aiModel").value = btn.dataset.model;
      $$("#aiPresets .preset").forEach((b) => b.classList.toggle("active", b === btn));
    });
  });
  $("#revealKey").addEventListener("click", () => {
    const input = $("#aiKey");
    input.type = input.type === "password" ? "text" : "password";
  });
  $("#defaultMode").addEventListener("change", (e) => syncStartSetting(e.target.value));
  $("#startWithAi").addEventListener("change", (e) => {
    if (e.target.checked) $("#defaultMode").value = "ai_first";
    else if ($("#defaultMode").value === "ai_first") $("#defaultMode").value = "offline_first";
    syncStartSetting($("#defaultMode").value);
  });
  $("#saveSettings").addEventListener("click", () => saveSettings());
  $("#saveSettingsTop").addEventListener("click", () => saveSettings());
  $("#testAi").addEventListener("click", async (e) => {
    const btn = e.currentTarget;
    const result = $("#aiTestResult");
    result.className = "";
    result.textContent = "保存配置并请求模型…";
    btn.disabled = true;
    try {
      const ok = await saveSettings({ silent: true });
      if (!ok) { result.className = "err"; result.textContent = "配置保存失败"; return; }
      const data = await api("/api/ai/test", { method: "POST", body: { target: $("#defaultTarget").value } });
      result.className = "ok";
      result.textContent = `连接成功：${data.sample || "OK"}`;
      toast("AI 连接成功", "模型已返回翻译样例", "success");
    } catch (err) {
      result.className = "err";
      result.textContent = err.message;
      toast("AI 连接失败", err.message, "error");
    } finally {
      btn.disabled = false;
    }
  });

  $("#logToggle").addEventListener("click", () => {
    state.logVisible = !state.logVisible;
    $("#logDock").style.display = state.logVisible ? "" : "none";
    if (state.logVisible) {
      state.logUnread = false;
      $("#logUnread").hidden = true;
      $("#logBody").scrollTop = $("#logBody").scrollHeight;
    }
  });
  $("#collapseLog").addEventListener("click", () => {
    $("#logDock").classList.toggle("collapsed");
  });
  $("#clearLogs").addEventListener("click", () => {
    state.logs = [];
    renderLogs();
  });
  $("#logBody").addEventListener("scroll", () => {
    const body = $("#logBody");
    const atBottom = body.scrollHeight - body.scrollTop - body.clientHeight < 40;
    if (!atBottom && $("#autoScroll").checked) {
      // Do not force the user back down while they inspect history.
      $("#autoScroll").checked = false;
    }
  });
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape") {
      const root = $("#modalRoot");
      if (root.classList.contains("open")) {
        root.classList.remove("open");
        root.innerHTML = "";
      }
      $$(".ms-panel").forEach((p) => { p.hidden = true; });
    }
  });
}

if (document.readyState === "loading") {
  document.addEventListener("DOMContentLoaded", init);
} else {
  init();
}
