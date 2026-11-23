/* Solacut NLE 专业桌面工作台前端控制器（原生 ES 模块，零外部 CDN 依赖） */

import { DockEngine } from "/static/dock_engine.js";
import { HistoryManager } from "/static/history_manager.js";
import { ProgramMonitorEngine, formatSmpte, parseSmpteOrTimecode } from "/static/monitor_engine.js";
import { DualTimelineEngine } from "/static/timeline_engine.js";
import { NleAppMenuBar } from "/static/app_menubar.js";
import {
  NleContextMenu,
  createScrubbyControl,
  createSvgIcon,
  enhanceNumberInputToScrubby,
  formatTimecode,
} from "/static/nle_controls.js";

const MEDIA_VIEW_KEY = "clipstudio_media_view";

const state = {
  config: null,
  videos: [],
  checkedVideoPaths: new Set(),
  activeVideo: null,
  highlightsData: null,
  initialClipBounds: new Map(), // rank -> { start_sec, end_sec } 供 Alt+I/O/X 还原边界
  sortBy: "rank", // 'rank' | 'time'
  onlySelected: false,
  clipSearchQuery: "",
  subFindMatches: [],
  subFindCursor: -1,
  mediaViewMode: (() => {
    try {
      return localStorage.getItem(MEDIA_VIEW_KEY) === "list" ? "list" : "grid";
    } catch {
      return "grid";
    }
  })(),
  mediaStageFilter: "all",
  mediaSortBy: "name",
  mediaInfoCache: new Map(),
  previewSegment: null, // { rank, start_sec, end_sec }
  activeClipRank: null, // 当前在检查器「片段」页与「字幕」面板选中的高光 rank
  retranslateTargetRanks: null, // null=全部，[rank]=单条定点重翻
  overlaySubMode: "bilingual", // 'bilingual' | 'zh' | 'en' | 'off'
  activeSubKey: "",
  activeTaskId: null,
  pollTimer: null,
  saveTimer: null,
  statusMsgTimer: null,
  audioProbeSeq: 0,
  mediaInfoSeq: 0,
  waveformSeq: 0,
  videoFps: 30, // 由 /api/media-info 实时写入真实帧率
};

let dock = null;
let ctxMenu = null;
let appMenuBar = null;
let historyMgr = null;
let monitorEngine = null;
let timelineEngine = null;
let userGlossaryTerms = [];
let clipStartScrubby = null;
let clipEndScrubby = null;

const el = (id) => document.getElementById(id);

function clampNum(val, min, max) {
  return Math.max(min, Math.min(max, Number(val) || min));
}

function formatClock(sec) {
  return formatTimecode(sec, false);
}

/** 底部 28px 达芬奇栏静默反馈 */
function setStatusMessage(msg, isError = false, holdMs = 3500) {
  const node = el("status-live-msg");
  if (!node) return;
  node.textContent = msg || "";
  node.classList.toggle("is-error", Boolean(isError));
  if (state.statusMsgTimer) clearTimeout(state.statusMsgTimer);
  if (msg && holdMs > 0) {
    state.statusMsgTimer = setTimeout(() => {
      node.textContent = "";
      node.classList.remove("is-error");
    }, holdMs);
  }
}

function showToast(msg, isError = false, quiet = false) {
  setStatusMessage(msg, isError);
  if (quiet && !isError) return;
  const box = el("toast-container");
  if (!box) return;
  const node = document.createElement("div");
  node.className = isError ? "toast error" : "toast";
  node.textContent = msg;
  box.appendChild(node);
  setTimeout(() => node.remove(), isError ? 4200 : 2800);
}

function updateStatusBarCounts() {
  const node = el("status-selection-text");
  if (node) {
    const totalVideos = state.videos.length;
    const allClips = state.highlightsData?.highlights || [];
    const selectedClips = allClips.filter((h) => h.selected !== false).length;
    node.textContent = allClips.length
      ? `素材 ${totalVideos} · 片段 ${selectedClips}/${allClips.length}`
      : `素材 ${totalVideos}`;
  }
  updateMediaCheckedIndicator();
}

function updateMediaCheckedIndicator() {
  const lbl = el("media-checked-label");
  const chkAll = el("chk-select-all-videos");
  const total = state.videos.length;
  const checked = state.checkedVideoPaths.size;

  if (lbl) {
    lbl.textContent = checked > 0 ? `已选 ${checked} 个` : state.activeVideo ? "当前视频" : "未选素材";
  }
  if (chkAll) {
    if (total === 0 || checked === 0) {
      chkAll.checked = false;
      chkAll.indeterminate = false;
    } else if (checked >= total) {
      chkAll.checked = true;
      chkAll.indeterminate = false;
    } else {
      chkAll.checked = false;
      chkAll.indeterminate = true;
    }
  }
}

function getLocalSessionToken() {
  return document.querySelector('meta[name="solacut-token"]')?.getAttribute("content") || "";
}

async function apiFetch(url, options = {}) {
  const token = getLocalSessionToken();
  const baseHeaders = { "Content-Type": "application/json" };
  if (token) {
    baseHeaders["X-Solacut-Token"] = token;
  }
  const resp = await fetch(url, {
    credentials: "same-origin",
    ...options,
    headers: { ...baseHeaders, ...(options.headers || {}) },
  });
  let body = null;
  try {
    body = await resp.json();
  } catch {
    body = null;
  }
  if (!resp.ok) {
    throw new Error((body && body.detail) || `请求失败（HTTP ${resp.status}）`);
  }
  return body;
}

/* ==================== 主题三态控制（默认纯黑硬件暗房） ==================== */
const THEME_STORAGE_KEY = "ghc_color_scheme";

function getSystemPrefersDark() {
  return window.matchMedia && window.matchMedia("(prefers-color-scheme: dark)").matches;
}

function applyThemeToMeta(mode) {
  const meta = document.querySelector('meta[name="color-scheme"]');
  if (meta) {
    meta.content = mode === "light" || mode === "dark" ? mode : "light dark";
  }
}

function syncThemeControls() {
  let stored = null;
  try {
    stored = localStorage.getItem(THEME_STORAGE_KEY);
  } catch {
    stored = null;
  }
  const activeMode =
    stored === "light" || stored === "dark" || stored === "system" ? stored : "dark";
  applyThemeToMeta(activeMode);

  const radio = document.querySelector(`input[name="theme-mode"][value="${activeMode}"]`);
  if (radio) radio.checked = true;

  const sysLabel = el("theme-system-label");
  if (sysLabel) {
    sysLabel.textContent = `跟随系统(${getSystemPrefersDark() ? "暗" : "亮"})`;
  }
}

function initThemeListeners() {
  syncThemeControls();

  document.querySelectorAll('input[name="theme-mode"]').forEach((radio) => {
    radio.addEventListener("change", (e) => {
      try {
        localStorage.setItem(THEME_STORAGE_KEY, e.target.value);
      } catch {}
      syncThemeControls();
    });
  });

  if (window.matchMedia) {
    window.matchMedia("(prefers-color-scheme: dark)").addEventListener("change", () => {
      syncThemeControls();
    });
  }

  window.addEventListener("storage", (e) => {
    if (e.key === THEME_STORAGE_KEY) syncThemeControls();
  });
}

/* ==================== 自由停靠引擎 + 底部达芬奇导航 + 原生右键菜单 ==================== */
const PANEL_TITLES = {
  media: "媒体池",
  monitor: "监视器",
  timeline: "时间线",
  clips: "高光",
  subtitles: "字幕",
  inspector: "检查器",
  queue: "队列",
  history: "历史",
};

function installToolbarFocusgroupFallback(toolbarEl) {
  if (!toolbarEl || "focusGroup" in HTMLElement.prototype) return;
  const items = Array.from(toolbarEl.querySelectorAll("button"));
  if (items.length === 0) return;

  items.forEach((b, i) => {
    b.tabIndex = i === 0 ? 0 : -1;
  });

  toolbarEl.addEventListener("focusin", ({ target }) => {
    if (items.includes(target)) {
      items.forEach((b) => {
        b.tabIndex = b === target ? 0 : -1;
      });
    }
  });

  toolbarEl.addEventListener("keydown", (e) => {
    const idx = items.indexOf(document.activeElement);
    if (idx < 0) return;
    const rtl = getComputedStyle(toolbarEl).direction === "rtl";
    let nextIdx = -1;
    if (e.key === "ArrowRight") nextIdx = (idx + (rtl ? -1 : 1) + items.length) % items.length;
    else if (e.key === "ArrowLeft") nextIdx = (idx + (rtl ? 1 : -1) + items.length) % items.length;
    else if (e.key === "Home") nextIdx = 0;
    else if (e.key === "End") nextIdx = items.length - 1;
    if (nextIdx >= 0) {
      e.preventDefault();
      items[nextIdx].focus();
    }
  });
}

function syncDockChrome(snapshot) {
  if (!snapshot) return;

  document.querySelectorAll(".preset-btn[data-preset]").forEach((btn) => {
    const active = btn.dataset.preset === snapshot.preset;
    btn.setAttribute("aria-pressed", active ? "true" : "false");
  });

  const paneLbl = el("status-active-pane");
  if (paneLbl) {
    const baseName = PANEL_TITLES[snapshot.focusedPanelId] || snapshot.focusedPanelId;
    paneLbl.textContent = snapshot.maximizedPanelId ? `${baseName} [MAX]` : baseName;
  }

  const listEl = el("panels-toggle-list");
  if (listEl) {
    const openSet = new Set(snapshot.openPanels || []);
    listEl.replaceChildren();
    for (const p of snapshot.allPanels || []) {
      const btn = document.createElement("button");
      btn.type = "button";
      btn.className = "panel-toggle-btn";
      const isOpen = openSet.has(p.id);
      btn.setAttribute("aria-pressed", isOpen ? "true" : "false");
      if (isOpen && openSet.size <= 1) btn.title = "工作区至少保留一个面板";

      const nameSpan = document.createElement("span");
      nameSpan.textContent = p.title;
      const dot = document.createElement("span");
      dot.className = "pt-dot";
      dot.setAttribute("aria-hidden", "true");
      btn.append(nameSpan, dot);

      btn.addEventListener("click", () => {
        dock?.togglePanelVisibility(p.id);
      });
      listEl.appendChild(btn);
    }
  }
}

function switchInspectorTab(tabKey) {
  const tabs = ["clip", "analyze", "export", "glossary"];
  if (!tabs.includes(tabKey)) return;
  document.querySelectorAll(".tab-btn").forEach((b) => {
    const active = b.dataset.tab === tabKey;
    b.classList.toggle("active", active);
    b.setAttribute("aria-selected", active ? "true" : "false");
  });
  el("tab-panel-clip")?.classList.toggle("hidden", tabKey !== "clip");
  el("tab-panel-analyze")?.classList.toggle("hidden", tabKey !== "analyze");
  el("tab-panel-export")?.classList.toggle("hidden", tabKey !== "export");
  el("tab-panel-glossary")?.classList.toggle("hidden", tabKey !== "glossary");
}

function initGlobalContextMenu() {
  ctxMenu = new NleContextMenu();
  // 屏蔽非文本输入区的 Edge 原生网页右键菜单，彻底消除浏览器套壳感
  document.addEventListener("contextmenu", (e) => {
    const target = e.target;
    if (target && target.closest("input, textarea, [contenteditable='true']")) return;
    e.preventDefault();
  });
}

function initDockWorkspace() {
  const rootEl = el("dock-root");
  const parkingEl = el("dock-parking");
  if (!rootEl || !parkingEl) return;

  dock = new DockEngine({
    rootEl,
    parkingEl,
    panels: [
      { id: "media", title: PANEL_TITLES.media, el: el("pane-media") },
      { id: "monitor", title: PANEL_TITLES.monitor, el: el("pane-monitor") },
      { id: "timeline", title: PANEL_TITLES.timeline, el: el("pane-timeline") },
      { id: "clips", title: PANEL_TITLES.clips, el: el("pane-clips") },
      { id: "subtitles", title: PANEL_TITLES.subtitles, el: el("pane-subtitles") },
      { id: "inspector", title: PANEL_TITLES.inspector, el: el("pane-inspector") },
      { id: "queue", title: PANEL_TITLES.queue, el: el("pane-queue") },
      { id: "history", title: PANEL_TITLES.history, el: el("pane-history") },
    ],
    onChange: (snap) => {
      syncDockChrome(snap);
      // 修复 P1-06：若监视器被隐藏或切出可视区，立即终止 J/K/L 倒放 rAF 防止后台空转
      if (!snap.openPanels?.includes("monitor")) {
        stopShuttle(true);
        el("review-video")?.pause();
      }
      if (snap.reason === "dock" || snap.reason === "reorder") {
        setStatusMessage("工作区停靠已保存");
      } else if (snap.reason === "reset") {
        setStatusMessage("工作区已重置");
      }
    },
    onTabContextMenu: (e, panelId) => {
      const snap = dock.getStateSnapshot();
      const canHide = (snap.openPanels || []).length > 1;
      ctxMenu?.open(e.clientX, e.clientY, [
        {
          label: snap.maximizedPanelId === panelId ? "还原面板" : "最大化此面板",
          hint: "`",
          onSelect: () => dock.toggleMaximize(panelId),
        },
        {
          label: "隐藏此面板",
          disabled: !canHide,
          onSelect: () => dock.hidePanel(panelId),
        },
        { type: "separator" },
        {
          label: "重置当前工作区",
          onSelect: () => dock.resetCurrentPreset(),
        },
      ]);
    },
  });

  syncDockChrome(dock.getStateSnapshot());
  installToolbarFocusgroupFallback(el("workspace-toolbar"));

  document.querySelectorAll(".preset-btn[data-preset]").forEach((btn) => {
    btn.addEventListener("click", () => {
      const preset = btn.dataset.preset || "edit";
      dock.setPreset(preset);
      if (preset === "deliver") {
        switchInspectorTab("export");
      } else if (preset === "subtitle") {
        ensureActiveClipForSubtitles();
        renderActiveSubtitlesPane();
      }
      setStatusMessage(`工作区：${btn.textContent.trim()}`);
    });
  });

  const doReset = () => {
    dock?.resetCurrentPreset();
    hidePopoverSafe(el("panels-popover"));
  };
  el("btn-reset-layout")?.addEventListener("click", doReset);
  el("btn-popover-reset-layout")?.addEventListener("click", doReset);

  el("global-task-pill")?.addEventListener("click", () => {
    dock?.focusPanel("queue");
  });
}

/* ==================== Popover 与原生 <dialog> ==================== */
function hidePopoverSafe(pop) {
  if (!pop) return;
  if (Object.hasOwn(HTMLElement.prototype, "popover") && typeof pop.hidePopover === "function") {
    try {
      pop.hidePopover();
    } catch {}
  }
  pop.classList.remove("popover-fallback-open");
}

function wirePopoverFallback(triggerEl, popEl) {
  if (!triggerEl || !popEl) return;
  if (Object.hasOwn(HTMLElement.prototype, "popover")) return;
  triggerEl.addEventListener("click", () => {
    popEl.classList.toggle("popover-fallback-open");
  });
  document.addEventListener("click", (e) => {
    if (
      popEl.classList.contains("popover-fallback-open") &&
      !popEl.contains(e.target) &&
      !triggerEl.contains(e.target)
    ) {
      popEl.classList.remove("popover-fallback-open");
    }
  });
}

function updateDirSummaryText() {
  const inRaw = (el("input-dir-field")?.value || "").trim();
  const outRaw = (el("output-dir-field")?.value || "").trim();
  const shortName = (p) => {
    const clean = p.replace(/[\\/]+$/, "");
    const parts = clean.split(/[\\/]/).filter(Boolean);
    return parts.length > 0 ? parts[parts.length - 1] : clean || "-";
  };
  const label = el("dir-summary-text");
  if (label) {
    label.textContent = `${shortName(inRaw)} → ${shortName(outRaw)}`;
    label.title = `输入：${inRaw}\n输出：${outRaw}`;
  }
}

function initPopovers() {
  wirePopoverFallback(el("btn-dirs-popover"), el("dirs-popover"));
  wirePopoverFallback(el("btn-panels-popover"), el("panels-popover"));
  wirePopoverFallback(el("btn-tl-view-opts"), el("tl-view-options-popover"));
  el("btn-close-dirs-popover")?.addEventListener("click", () => hidePopoverSafe(el("dirs-popover")));
}

function setupModalDialogLightDismiss(dialog) {
  if (!dialog) return;
  dialog.addEventListener("close", () => {
    dialog.returnValue = "";
  });
  if (!("closedBy" in HTMLDialogElement.prototype)) {
    const isOutside = (e) => {
      const r = dialog.getBoundingClientRect();
      return e.clientX < r.left || e.clientX > r.right || e.clientY < r.top || e.clientY > r.bottom;
    };
    let pressedOutside = false;
    dialog.addEventListener("pointerdown", (e) => {
      pressedOutside = isOutside(e);
    });
    dialog.addEventListener("pointerup", (e) => {
      if (pressedOutside && isOutside(e)) dialog.close();
    });
  }
}

function wireDialogTriggerFallback(btnId, dialogId) {
  const btn = el(btnId);
  const dialog = el(dialogId);
  if (!btn || !dialog) return;
  setupModalDialogLightDismiss(dialog);
  if (!("commandForElement" in HTMLButtonElement.prototype)) {
    btn.addEventListener("click", () => {
      if (!dialog.open && typeof dialog.showModal === "function") {
        dialog.showModal();
      }
    });
  }
}

function initDialogs() {
  wireDialogTriggerFallback("btn-open-settings", "settings-dialog");
  wireDialogTriggerFallback("btn-open-shortcuts", "shortcuts-dialog");
  setupModalDialogLightDismiss(el("retranslate-dialog"));
}

function initScrubbyInputs() {
  document.querySelectorAll("input.scrubby-auto").forEach((inputEl) => {
    enhanceNumberInputToScrubby(inputEl, { suffix: inputEl.dataset.suffix || "" });
  });
}

/* ==================== 配置与专家词库 ==================== */
function syncAsrProviderVisibility() {
  const provider = el("sel-asr-provider")?.value || "deepseek";
  el("local-whisper-model-group")?.classList.toggle("hidden", provider !== "local");
}

function hydrateSettingsForm(cfg) {
  el("cfg-base-url").value = cfg.base_url || "https://api.deepseek.com/v1";
  el("cfg-llm-model").value = cfg.screen_llm_model || cfg.llm_model || "deepseek-flash";
  el("cfg-api-key").value = "";
  el("cfg-key-status").textContent = cfg.has_api_key ? `${cfg.api_key_masked}` : "未配置";

  const asrProv = cfg.asr_provider || "deepseek";
  el("cfg-asr-provider").value = asrProv;
  el("sel-asr-provider").value = asrProv;
  syncAsrProviderVisibility();

  el("cfg-asr-base-url").value = cfg.asr_base_url || "";
  el("cfg-asr-model").value = cfg.asr_model || "";
  el("cfg-asr-api-key").value = "";
  el("cfg-asr-key-status").textContent = cfg.asr_api_key_masked
    ? cfg.asr_api_key_masked
    : cfg.has_asr_api_key
      ? "复用主Key"
      : "未配置";

  el("cfg-asr-hotwords").value = cfg.asr_hotwords || "";
  if (!el("input-asr-hotwords").value.trim() && cfg.asr_hotwords) {
    el("input-asr-hotwords").value = cfg.asr_hotwords;
  }

  el("cfg-polish-model").value = cfg.polish_llm_model || "";
  el("cfg-polish-base-url").value = cfg.polish_llm_base_url || "";
  el("cfg-polish-api-key").value = "";
  el("cfg-polish-key-status").textContent = cfg.polish_api_key_masked
    ? cfg.polish_api_key_masked
    : cfg.has_polish_api_key
      ? "复用主Key"
      : "未配置";

  el("cfg-screen-effort").value = cfg.screen_reasoning_effort || "";
  el("cfg-polish-effort").value = cfg.polish_reasoning_effort || "";
}

function renderUserGlossaryList() {
  const box = el("user-glossary-list");
  if (!box) return;
  box.replaceChildren();
  if (!userGlossaryTerms.length) {
    const empty = document.createElement("div");
    empty.className = "empty-hint";
    empty.textContent = "暂无专属词条";
    box.appendChild(empty);
    return;
  }
  userGlossaryTerms.forEach((term, idx) => {
    const row = document.createElement("div");
    row.className = "glossary-item";

    const textWrap = document.createElement("div");
    textWrap.className = "glossary-item-text";
    const main = document.createElement("strong");
    main.textContent = `${term.en} → ${term.zh}`;
    textWrap.appendChild(main);

    const subParts = [];
    if (term.aliases?.length) subParts.push(`误听: ${term.aliases.join(", ")}`);
    if (term.note) subParts.push(term.note);
    if (subParts.length) {
      const sub = document.createElement("span");
      sub.textContent = subParts.join(" · ");
      sub.title = sub.textContent;
      textWrap.appendChild(sub);
    }

    const btnDel = document.createElement("button");
    btnDel.type = "button";
    btnDel.className = "sub-op-btn danger";
    btnDel.title = "删除词条";
    btnDel.setAttribute("aria-label", "删除词条");
    btnDel.appendChild(createSvgIcon("trash", 12));
    btnDel.addEventListener("click", async () => {
      userGlossaryTerms.splice(idx, 1);
      await persistUserGlossary();
    });
    row.append(textWrap, btnDel);
    box.appendChild(row);
  });
}

async function persistUserGlossary() {
  try {
    const res = await apiFetch("/api/corpus/user-glossary", {
      method: "POST",
      body: JSON.stringify({ terms: userGlossaryTerms }),
    });
    userGlossaryTerms = res.terms || [];
    renderUserGlossaryList();
    await loadCorpusStatus();
    setStatusMessage(`专属词库已保存 (${userGlossaryTerms.length} 条)`);
  } catch (err) {
    showToast(err.message, true);
  }
}

async function loadCorpusStatus() {
  try {
    const [stats, glossary] = await Promise.all([
      apiFetch("/api/corpus/stats"),
      apiFetch("/api/corpus/user-glossary"),
    ]);
    userGlossaryTerms = glossary.terms || [];
    renderUserGlossaryList();

    const badge = el("corpus-stats-badge");
    const detail = el("corpus-stats-detail");
    const totalPairs = Number(stats.total_parallel_pairs || 0);
    const commPairs = Number(stats.commercial_safe_pairs || 0);
    const lpMap = stats.lang_pairs || {};
    const lpKeys = Object.keys(lpMap).filter((k) => Number(lpMap[k]?.total_pairs || 0) > 0);
    if (badge) {
      badge.textContent = lpKeys.length > 1
        ? `${totalPairs.toLocaleString()} 句 · ${lpKeys.length}语种`
        : `${totalPairs.toLocaleString()} 句`;
    }
    if (detail) {
      const lpSummary = lpKeys
        .map((k) => `${k.replace("_", "→").toUpperCase()} ${Math.round(Number(lpMap[k].total_pairs || 0) / 1000)}k`)
        .join(" / ");
      detail.textContent =
        `商用安全 ${commPairs.toLocaleString()} · 术语 ${stats.gaming_terms_count || 0} · ` +
        `误听 ${stats.asr_mishearings_count || 0} · 反直译 ${stats.idioms_anti_literal_count || 0} · ` +
        `专属 ${userGlossaryTerms.length}` +
        (lpSummary ? `\n${lpSummary}` : "");
    }
  } catch {}
}

const SUB_LANG_META = {
  zh: { short: "中", name: "简体中文", code: "ZH" },
  en: { short: "英", name: "英语", code: "EN" },
  ja: { short: "日", name: "日语", code: "JA" },
  ko: { short: "韩", name: "韩语", code: "KO" },
  ru: { short: "俄", name: "俄语", code: "RU" },
  es: { short: "西", name: "西语", code: "ES" },
};

function getActiveLanguagePair() {
  const srcRaw = (
    state.highlightsData?.source_language ||
    state.selectedVideo?.source_language ||
    el("input-asr-lang")?.value ||
    "en"
  )
    .trim()
    .toLowerCase();
  const src = SUB_LANG_META[srcRaw] ? srcRaw : "en";
  let tgtRaw = (
    state.highlightsData?.target_language ||
    state.selectedVideo?.target_language ||
    el("sel-target-lang")?.value ||
    "auto"
  )
    .trim()
    .toLowerCase();
  if (!tgtRaw || tgtRaw === "auto" || !SUB_LANG_META[tgtRaw]) {
    tgtRaw = src === "zh" ? "en" : "zh";
  }
  const tgt = tgtRaw === src ? (src === "zh" ? "en" : "zh") : tgtRaw;
  return {
    src,
    tgt,
    srcMeta: SUB_LANG_META[src] || SUB_LANG_META.en,
    tgtMeta: SUB_LANG_META[tgt] || SUB_LANG_META.zh,
    isZhToEn: src === "zh" && tgt === "en",
  };
}

function resolveSubPairTexts(sub, lp = getActiveLanguagePair()) {
  const rawTgt = (sub?.text_tgt || "").trim();
  const rawSrc = (sub?.text_src || "").trim();
  const zh = (sub?.text_zh || "").trim();
  const en = (sub?.text_en || "").trim();
  const orig = (sub?.text_orig || "").trim();
  if (rawTgt || rawSrc) {
    return {
      tgtText: rawTgt || zh || en || orig,
      srcText: rawSrc || orig || en || zh,
    };
  }
  if (lp.isZhToEn) {
    return { tgtText: en || zh, srcText: zh || orig };
  }
  return { tgtText: zh || en || orig, srcText: en || orig || zh };
}

function syncLanguagePairUI() {
  const lp = getActiveLanguagePair();
  const pairLabel = `${lp.srcMeta.short}${lp.tgtMeta.short}`;

  const exportSel = el("sel-subtitle-lang");
  if (exportSel) {
    const mapText = {
      bilingual: `${pairLabel}双语 (${lp.tgtMeta.short}上 / ${lp.srcMeta.short}下)`,
      bilingual_rev: `${pairLabel}双语 (${lp.srcMeta.short}上 / ${lp.tgtMeta.short}下)`,
      target: `仅${lp.tgtMeta.name} (${lp.tgtMeta.code})`,
      source: `仅${lp.srcMeta.name} (${lp.srcMeta.code})`,
    };
    Array.from(exportSel.options).forEach((opt) => {
      if (mapText[opt.value]) opt.textContent = mapText[opt.value];
    });
  }

  const overlaySel = el("sel-overlay-sub-mode");
  if (overlaySel) {
    const mapOverlay = {
      bilingual: `CC ${pairLabel}双语`,
      bilingual_rev: `CC ${lp.srcMeta.short}/${lp.tgtMeta.short}`,
      target: `CC ${lp.tgtMeta.name}`,
      source: `CC ${lp.srcMeta.name}`,
      off: "CC 关",
    };
    Array.from(overlaySel.options).forEach((opt) => {
      if (mapOverlay[opt.value]) opt.textContent = mapOverlay[opt.value];
    });
  }
}

/* ==================== 媒体池扫描与渲染 ==================== */
async function loadConfigAndScan() {
  const cfg = await apiFetch("/api/config");
  state.config = cfg;
  el("input-dir-field").value = cfg.input_dir;
  el("output-dir-field").value = cfg.output_dir;
  updateDirSummaryText();

  hydrateSettingsForm(cfg);
  loadCorpusStatus();
  await scanVideos();
}

async function persistDirsAndScan() {
  const inputDir = el("input-dir-field").value.trim();
  const outputDir = el("output-dir-field").value.trim();
  updateDirSummaryText();
  await apiFetch("/api/config", {
    method: "POST",
    body: JSON.stringify({ input_dir: inputDir, output_dir: outputDir }),
  });
  await scanVideos();
}

async function scanVideos() {
  const inDir = encodeURIComponent(el("input-dir-field").value.trim());
  const outDir = encodeURIComponent(el("output-dir-field").value.trim());
  const data = await apiFetch(`/api/videos?input_dir=${inDir}&output_dir=${outDir}`);
  state.videos = data.videos || [];
  renderVideoList();
  updateStatusBarCounts();

  if (state.activeVideo) {
    const refreshed = state.videos.find((v) => v.video_path === state.activeVideo.video_path);
    if (refreshed) {
      state.activeVideo = refreshed;
      renderDiagnosticsBar(refreshed.diagnostics);
      if (refreshed.has_highlights) {
        // 修复 P1-10：同一视频任务完成后刷新高光数据时保留撤销栈（压入新版本而非清空历史）
        await loadHighlightsForVideo(refreshed, false, false);
      }
    }
  } else if (state.videos.length > 0) {
    const preferred = state.videos.find((v) => v.has_highlights) || state.videos[0];
    await selectActiveVideo(preferred);
  }
}

function splitFilenameForEllipsis(relPath) {
  const dotIdx = relPath.lastIndexOf(".");
  if (dotIdx > 0 && relPath.length - dotIdx <= 6) {
    return [relPath.slice(0, dotIdx), relPath.slice(dotIdx)];
  }
  return [relPath, ""];
}

function getFilteredSortedVideos() {
  const keyword = (el("video-search-input")?.value || "").trim().toLowerCase();
  const stageFilter = state.mediaStageFilter || "all";
  const sortBy = state.mediaSortBy || "name";
  const gradeRank = { S: 0, A: 1, B: 2, WARN: 3 };

  let list = state.videos.filter((v) => {
    if (
      keyword &&
      !v.filename.toLowerCase().includes(keyword) &&
      !v.rel_path.toLowerCase().includes(keyword)
    ) {
      return false;
    }
    if (stageFilter === "pending") return !v.has_highlights;
    if (stageFilter === "analyzed") return Boolean(v.has_highlights) && v.stage !== "exported";
    if (stageFilter === "exported") return v.stage === "exported";
    return true;
  });

  if (sortBy === "size") {
    list = [...list].sort((a, b) => Number(b.size_mb || 0) - Number(a.size_mb || 0));
  } else if (sortBy === "grade") {
    list = [...list].sort((a, b) => {
      const ga = a.grade in gradeRank ? gradeRank[a.grade] : 9;
      const gb = b.grade in gradeRank ? gradeRank[b.grade] : 9;
      return ga - gb || a.filename.localeCompare(b.filename);
    });
  } else {
    list = [...list].sort((a, b) => a.filename.localeCompare(b.filename));
  }
  return list;
}

function buildVideoContextMenuItems(video) {
  const isChecked = state.checkedVideoPaths.has(video.video_path);
  return [
    {
      label: "在监视器中加载",
      onSelect: () => selectActiveVideo(video),
    },
    {
      label: "加入批量队列",
      checked: isChecked,
      onSelect: () => {
        if (isChecked) state.checkedVideoPaths.delete(video.video_path);
        else state.checkedVideoPaths.add(video.video_path);
        renderVideoList();
      },
    },
    { type: "separator" },
    {
      label: "零消耗音频体检",
      onSelect: () => triggerDiagnose([video.video_path]),
    },
    {
      label: "AI 高光粗筛",
      onSelect: () => triggerAnalyze([video.video_path]),
    },
    { type: "separator" },
    {
      label: "打开输出目录",
      onSelect: async () => {
        const target = video.clips_dir || el("output-dir-field").value.trim();
        if (target) {
          await apiFetch("/api/open-folder", {
            method: "POST",
            body: JSON.stringify({ path: target }),
          });
        }
      },
    },
  ];
}

function syncMediaViewButtons() {
  const isGrid = state.mediaViewMode === "grid";
  el("btn-media-view-grid")?.classList.toggle("active", isGrid);
  el("btn-media-view-list")?.classList.toggle("active", !isGrid);
  el("video-list")?.classList.toggle("view-grid", isGrid);
}

function renderVideoList() {
  const container = el("video-list");
  if (!container) return;
  syncMediaViewButtons();

  const filtered = getFilteredSortedVideos();
  el("video-count-badge").textContent = String(filtered.length);
  container.replaceChildren();

  if (filtered.length === 0) {
    const hint = document.createElement("div");
    hint.className = "empty-hint";
    hint.textContent = "未找到视频素材";
    container.appendChild(hint);
    updateMediaCheckedIndicator();
    return;
  }

  const isGrid = state.mediaViewMode === "grid";

  for (const video of filtered) {
    const isActive = Boolean(state.activeVideo && state.activeVideo.video_path === video.video_path);
    const sizeText =
      video.size_mb >= 1024 ? `${(video.size_mb / 1024).toFixed(2)}GB` : `${video.size_mb}MB`;
    const cachedInfo = state.mediaInfoCache.get(video.video_path);
    const techTag =
      cachedInfo && cachedInfo.height
        ? `${cachedInfo.height}p${Math.round(Number(cachedInfo.fps) || 30)}`
        : "";
    const stageText = video.has_highlights
      ? `${video.stage_label} ${video.selected_count}/${video.highlight_count}`
      : video.stage_label;

    const chk = document.createElement("input");
    chk.type = "checkbox";
    chk.checked = state.checkedVideoPaths.has(video.video_path);
    chk.title = "加入批量任务";
    chk.addEventListener("click", (e) => e.stopPropagation());
    chk.addEventListener("change", () => {
      if (chk.checked) state.checkedVideoPaths.add(video.video_path);
      else state.checkedVideoPaths.delete(video.video_path);
      updateMediaCheckedIndicator();
    });

    const title = document.createElement("div");
    title.className = "video-item-name";
    title.title = video.rel_path;
    const [stemPart, extPart] = splitFilenameForEllipsis(video.rel_path);
    const stemSpan = document.createElement("span");
    stemSpan.className = "fn-stem";
    stemSpan.textContent = stemPart;
    title.appendChild(stemSpan);
    if (extPart) {
      const extSpan = document.createElement("span");
      extSpan.className = "fn-ext";
      extSpan.textContent = extPart;
      title.appendChild(extSpan);
    }

    if (isGrid) {
      const card = document.createElement("div");
      card.className = `video-card-grid${isActive ? " active" : ""}`;
      card.setAttribute("role", "listitem");

      const thumbBox = document.createElement("div");
      thumbBox.className = "video-thumb-box";

      const fallback = document.createElement("div");
      fallback.className = "video-thumb-fallback mono-num";
      fallback.textContent = "16:9";

      const thumbSec = video.total_duration_sec > 45 ? 30 : Math.min(5, (video.total_duration_sec || 10) * 0.25);
      const img = document.createElement("img");
      img.className = "video-thumb-img";
      img.loading = "lazy";
      img.decoding = "async";
      img.alt = video.filename;
      // 修复 P1-09：与后端 /api/thumbnail?path=...&time=...&w=... 参数严格一致
      img.src = `/api/thumbnail?path=${encodeURIComponent(video.video_path)}&time=${thumbSec.toFixed(1)}&w=320`;
      img.addEventListener("load", () => fallback.classList.add("hidden"));
      img.addEventListener("error", () => {
        img.classList.add("hidden");
        fallback.classList.remove("hidden");
      });

      const topLeft = document.createElement("div");
      topLeft.className = "video-thumb-top-left";
      topLeft.appendChild(chk);
      if (video.grade) {
        const gBadge = document.createElement("span");
        gBadge.className = "grade-mini mono-num";
        gBadge.dataset.grade = video.grade;
        gBadge.textContent = video.grade === "WARN" ? "!" : video.grade;
        topLeft.appendChild(gBadge);
      }

      const topRight = document.createElement("div");
      topRight.className = "video-thumb-top-right";
      const badge = document.createElement("span");
      badge.className = `stage-pill ${video.stage || ""}`;
      badge.textContent = stageText;
      topRight.appendChild(badge);

      thumbBox.append(fallback, img, topLeft, topRight);

      if (techTag) {
        const techEl = document.createElement("span");
        techEl.className = "video-thumb-tech mono-num";
        techEl.textContent = techTag;
        thumbBox.appendChild(techEl);
      }

      if (video.total_duration_str && video.total_duration_str !== "-") {
        const durEl = document.createElement("span");
        durEl.className = "video-thumb-dur mono-num";
        durEl.textContent = video.total_duration_str;
        thumbBox.appendChild(durEl);
      }

      const foot = document.createElement("div");
      foot.className = "video-card-foot";
      const subRow = document.createElement("div");
      subRow.className = "video-card-sub mono-num";
      subRow.textContent = sizeText;
      foot.append(title, subRow);

      card.append(thumbBox, foot);
      card.addEventListener("click", () => selectActiveVideo(video));
      card.addEventListener("contextmenu", (e) => {
        e.preventDefault();
        e.stopPropagation();
        ctxMenu?.open(e.clientX, e.clientY, buildVideoContextMenuItems(video));
      });
      container.appendChild(card);
    } else {
      const card = document.createElement("div");
      card.className = `video-item${isActive ? " active" : ""}`;
      card.setAttribute("role", "listitem");

      const body = document.createElement("div");
      body.className = "video-item-body";

      const meta = document.createElement("div");
      meta.className = "video-item-meta";

      const leftMeta = document.createElement("span");
      leftMeta.className = "mono-num";
      const parts = [];
      if (video.total_duration_str && video.total_duration_str !== "-") parts.push(video.total_duration_str);
      if (techTag) parts.push(techTag);
      parts.push(sizeText);
      leftMeta.textContent = parts.join(" · ");

      if (video.grade) {
        const gBadge = document.createElement("span");
        gBadge.className = "grade-mini mono-num";
        gBadge.dataset.grade = video.grade;
        gBadge.textContent = video.grade === "WARN" ? "!" : video.grade;
        meta.appendChild(gBadge);
      }

      const badge = document.createElement("span");
      badge.className = `stage-pill ${video.stage || ""}`;
      badge.textContent = stageText;

      meta.append(leftMeta, badge);
      body.append(title, meta);
      card.append(chk, body);

      card.addEventListener("click", () => selectActiveVideo(video));
      card.addEventListener("contextmenu", (e) => {
        e.preventDefault();
        e.stopPropagation();
        ctxMenu?.open(e.clientX, e.clientY, buildVideoContextMenuItems(video));
      });
      container.appendChild(card);
    }
  }
  updateMediaCheckedIndicator();
}

async function loadAudioStreamsForVideo(video) {
  const selectEl = el("sel-audio-stream");
  const badgeEl = el("audio-stream-badge");
  if (!selectEl || !badgeEl || !video) return;

  const reqSeq = ++state.audioProbeSeq;
  badgeEl.textContent = "...";

  try {
    const data = await apiFetch(`/api/audio-streams?path=${encodeURIComponent(video.video_path)}`);
    if (reqSeq !== state.audioProbeSeq) return;

    const streams = Array.isArray(data.streams) ? data.streams : [];
    selectEl.replaceChildren();

    if (streams.length === 0) {
      const opt = document.createElement("option");
      opt.value = "0";
      opt.textContent = "默认轨 (#0)";
      selectEl.appendChild(opt);
      badgeEl.textContent = "无音轨";
      return;
    }

    for (const st of streams) {
      const opt = document.createElement("option");
      opt.value = String(st.audio_index);
      opt.textContent = st.label || `轨 #${st.audio_index}`;
      selectEl.appendChild(opt);
    }

    const prevIdx = Number(video.audio_stream);
    const hasPrevValid =
      Boolean(video.has_peaks) && streams.some((s) => Number(s.audio_index) === prevIdx);
    const targetIdx = hasPrevValid ? prevIdx : Number(data.recommended_stream || 0);
    selectEl.value = String(targetIdx);

    const hasMicRec = streams.some((s) => Boolean(s.is_likely_mic));
    if (streams.length >= 2) {
      badgeEl.textContent = hasMicRec
        ? `${streams.length}轨·荐#${data.recommended_stream}`
        : `${streams.length}轨`;
    } else {
      badgeEl.textContent = "单轨";
    }
  } catch {
    if (reqSeq !== state.audioProbeSeq) return;
    selectEl.replaceChildren();
    const opt = document.createElement("option");
    opt.value = "0";
    opt.textContent = "默认轨 (#0)";
    selectEl.appendChild(opt);
    badgeEl.textContent = "#0";
  }
}

async function loadMediaInfoForVideo(video) {
  if (!video) return;
  const reqSeq = ++state.mediaInfoSeq;
  try {
    const info = await apiFetch(`/api/media-info?path=${encodeURIComponent(video.video_path)}`);
    if (reqSeq !== state.mediaInfoSeq) return;
    state.mediaInfoCache.set(video.video_path, info);
    state.videoFps = Math.max(1, Number(info?.fps) || 30);
    monitorEngine?.setMediaInfo(info);
    const fpsBadge = el("tl-fps-badge");
    if (fpsBadge) {
      fpsBadge.textContent = `${Math.round(state.videoFps)}fps`;
    }
    timelineEngine?.setMedia({
      stem: video.stem,
      duration: Number(info?.duration_sec) || Number(video.total_duration_sec) || el("review-video")?.duration || 0,
      fps: state.videoFps,
      resetView: false,
    });
    renderVideoList();
  } catch {
    if (reqSeq !== state.mediaInfoSeq) return;
    state.videoFps = 30;
    monitorEngine?.setMediaInfo(null);
  }
}

async function loadOverviewWaveformForVideo(video) {
  if (!video || !timelineEngine) return;
  const seq = ++state.waveformSeq;
  const outDir = encodeURIComponent(el("output-dir-field").value.trim());
  try {
    const wf = await apiFetch(
      `/api/waveform?stem=${encodeURIComponent(video.stem)}&output_dir=${outDir}&points=1200`
    );
    if (seq !== state.waveformSeq) return;
    timelineEngine.setOverviewWaveform(wf);
  } catch {
    if (seq !== state.waveformSeq) return;
    timelineEngine.setOverviewWaveform(null);
  }

  // 若已有音频体检结果，顺手加载 peak_segments 给磁吸吸附点使用
  if (video.has_peaks) {
    try {
      const diag = await apiFetch(
        `/api/diagnostics?stem=${encodeURIComponent(video.stem)}&output_dir=${outDir}`
      );
      if (seq !== state.waveformSeq) return;
      timelineEngine.setPeakSegments(diag.peak_segments || []);
    } catch {}
  }
}

function syncTimelineData() {
  if (!timelineEngine) return;
  const items = state.highlightsData?.highlights || [];
  timelineEngine.setHighlights(items, state.activeClipRank);
  timelineEngine.setActiveSubKey(state.activeSubKey);
}

function syncMonitorClipContext() {
  const cur = getClipByRank(state.previewSegment?.rank ?? state.activeClipRank);
  monitorEngine?.setActiveClip(cur);
  syncTimelineData();
}

function getVideoDurationLimit() {
  const videoEl = el("review-video");
  const domDur = videoEl && Number.isFinite(videoEl.duration) && videoEl.duration > 0 ? videoEl.duration : 0;
  const metaDur = Number(state.activeVideo?.total_duration_sec) || Number(state.highlightsData?.total_duration_sec) || 0;
  return domDur || metaDur || 86400;
}

async function selectActiveVideo(video) {
  stopShuttle(true);
  const isDifferentVideo = !state.activeVideo || state.activeVideo.video_path !== video.video_path;

  // 修复 P0-01：切换不同视频前立即 Flush 未落盘的 600ms 防抖保存，彻底消灭静默丢修改与“待保存*”卡死
  if (isDifferentVideo && state.saveTimer && state.highlightsData) {
    await saveHighlightsNow(true);
  }
  if (isDifferentVideo && state.saveTimer) {
    clearTimeout(state.saveTimer);
    state.saveTimer = null;
  }

  state.activeVideo = video;
  state.previewSegment = null;
  if (isDifferentVideo) {
    state.activeClipRank = null;
    state.activeSubKey = "";
    state.clipSearchQuery = "";
    state.initialClipBounds = new Map();
    if (el("save-status-text")) el("save-status-text").textContent = "已同步";
    // 修复 P1-08：切视频清空跨视频表单与旧媒体规格，防止张冠李戴导出错误 4K 原片
    if (el("override-video-path")) el("override-video-path").value = "";
    if (el("retranslate-context")) el("retranslate-context").value = "";
    if (el("retranslate-hotwords")) el("retranslate-hotwords").value = "";
    if (el("clip-search-input")) el("clip-search-input").value = "";
    closeSubFindBar();
    monitorEngine?.setMediaInfo(null);
  }
  renderVideoList();

  el("hud-video-name").textContent = video.filename;
  el("hud-clip-title").textContent = "";

  const streamTag = el("tl-a1-stream-tag");
  if (streamTag) streamTag.textContent = `#${Number(video.audio_stream || 0)}`;

  const videoPlayer = el("review-video");
  const streamUrl = `/api/video-stream?path=${encodeURIComponent(video.video_path)}`;
  if (videoPlayer.getAttribute("src") !== streamUrl) {
    videoPlayer.src = streamUrl;
  }

  timelineEngine?.setMedia({
    stem: video.stem,
    duration: Number(video.total_duration_sec) || 0,
    fps: state.videoFps,
    resetView: isDifferentVideo,
  });

  loadAudioStreamsForVideo(video);
  loadMediaInfoForVideo(video);
  loadOverviewWaveformForVideo(video);
  renderDiagnosticsBar(video.diagnostics);

  if (video.has_highlights) {
    await loadHighlightsForVideo(video, true, isDifferentVideo);
  } else {
    state.highlightsData = null;
    state.activeClipRank = null;
    historyMgr?.clear();
    renderHighlightsList();
    renderActiveSubtitlesPane();
    renderClipInspector();
    syncMonitorClipContext();
  }
  syncVideoSubtitleOverlay();
  updateStatusBarCounts();
}

/* ==================== 直播音频体检热力条（唯一精简展示，零重复） ==================== */
function renderDiagnosticsBar(diag) {
  const bar = el("diagnostics-bar");
  const emptyView = el("diag-empty-view");
  const dataView = el("diag-data-view");
  if (!bar) return;

  if (!state.activeVideo) {
    bar.classList.add("hidden");
    return;
  }

  bar.classList.remove("hidden");

  if (!diag) {
    emptyView.classList.remove("hidden");
    dataView.classList.add("hidden");
    return;
  }

  emptyView.classList.add("hidden");
  dataView.classList.remove("hidden");

  const gradeBadge = el("diag-grade-badge");
  gradeBadge.dataset.grade = diag.grade || "A";
  gradeBadge.textContent = diag.grade_label || diag.grade || "-";

  const active = state.activeVideo;
  let audioMetaSuffix = "";
  if (active && active.vocal_bandpass !== null && active.vocal_bandpass !== undefined) {
    audioMetaSuffix = ` · #${Number(active.audio_stream || 0)}(${active.vocal_bandpass ? "带通" : "全频"})`;
  }
  const metricParts = [];
  if (diag.burst_ratio !== undefined && diag.burst_ratio !== null) {
    metricParts.push(`${diag.burst_ratio}x`);
  }
  if (diag.strong_peaks_per_hour !== undefined && diag.strong_peaks_per_hour !== null) {
    metricParts.push(`${diag.strong_peaks_per_hour}/h`);
  }
  if (diag.golden_bucket_label) {
    metricParts.push(String(diag.golden_bucket_label));
  }
  el("diag-metrics-text").textContent = `${metricParts.join(" · ")}${audioMetaSuffix}`.replace(/^ · /, "");
  el("diag-advice-text").textContent = diag.advice || "";
  el("diag-advice-text").title = diag.advice || "";

  const rec = diag.recommendation || {};
  const modeText = rec.asr_mode === "full" ? "全片" : `Top ${rec.top_k_peaks ?? 10}`;
  el("diag-rec-summary").textContent = modeText;

  const barsBox = el("diag-heatmap-bars");
  barsBox.replaceChildren();
  const buckets = Array.isArray(diag.hourly_heatmap) ? diag.hourly_heatmap : [];
  for (const b of buckets) {
    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = "heat-bar-item";
    const isGolden =
      b.label === diag.golden_bucket_label && (b.strong_peaks > 0 || b.total_peaks > 0);
    btn.dataset.golden = isGolden ? "true" : "false";
    btn.title = `${b.label}：强峰 ${b.strong_peaks} / 总 ${b.total_peaks} · 点击跳至 ${formatClock(b.start_sec)}`;

    const fill = document.createElement("span");
    fill.className = "heat-bar-fill";
    fill.style.height = `${Math.max(10, Number(b.heat_pct) || 0)}%`;

    const lbl = document.createElement("span");
    lbl.className = "heat-bar-label mono-num";
    lbl.textContent = b.label;

    btn.append(fill, lbl);
    btn.addEventListener("click", () => {
      const videoEl = el("review-video");
      if (!videoEl || !videoEl.src) return;
      state.previewSegment = null;
      videoEl.currentTime = Number(b.start_sec) || 0;
      videoEl.play().catch(() => {});
      el("hud-clip-title").textContent = `${b.label} (${b.strong_peaks} 强峰)`;
      setStatusMessage(`跳至 ${b.label} (${formatClock(b.start_sec)})`);
    });
    barsBox.appendChild(btn);
  }
}

function applyRecommendedParams() {
  const diag = state.activeVideo?.diagnostics;
  if (!diag || !diag.recommendation) {
    showToast("请先对当前视频执行体检", true);
    return;
  }
  const rec = diag.recommendation;
  el("sel-asr-mode").value = rec.asr_mode || "peaks";
  el("num-top-k-peaks").value = String(rec.top_k_peaks || 20);
  el("num-top-k-peaks")._syncScrubby?.();
  el("num-top-percent").value = String(rec.top_percent || 15);
  el("num-top-percent")._syncScrubby?.();

  dock?.focusPanel("inspector");
  switchInspectorTab("analyze");
  const desc = rec.asr_mode === "full" ? "全片转写" : `高峰 Top ${rec.top_k_peaks}`;
  setStatusMessage(`已套用推荐参数：${desc}`);
}

/* ==================== 操作历史管理（PR History / DaVinci Undo History） ==================== */
function captureEditableSnapshot() {
  if (!state.highlightsData || !Array.isArray(state.highlightsData.highlights)) return null;
  return {
    highlights: state.highlightsData.highlights,
    activeClipRank: state.activeClipRank,
  };
}

function recordHistory(label, category = "edit", mergeKey = null) {
  if (!historyMgr) return;
  const snap = captureEditableSnapshot();
  if (!snap) return;
  historyMgr.commit(snap, label, { category, mergeKey });
}

function renderHistoryPane(histState) {
  const canUndo = Boolean(histState?.canUndo);
  const canRedo = Boolean(histState?.canRedo);
  const total = Number(histState?.total || 0);
  const cursor = Number(histState?.cursor ?? -1);

  for (const id of ["btn-undo", "btn-hist-undo"]) {
    const b = el(id);
    if (b) b.disabled = !canUndo;
  }
  for (const id of ["btn-redo", "btn-hist-redo"]) {
    const b = el(id);
    if (b) b.disabled = !canRedo;
  }
  const revertBtn = el("btn-hist-revert");
  if (revertBtn) revertBtn.disabled = cursor <= 0;

  const badge = el("history-step-badge");
  if (badge) {
    badge.textContent = total > 0 ? `${cursor + 1}/${total}` : "0/0";
  }

  const listEl = el("history-list");
  if (!listEl) return;
  listEl.replaceChildren();

  const entries = Array.isArray(histState?.entries) ? histState.entries : [];
  if (entries.length === 0) {
    const empty = document.createElement("div");
    empty.className = "empty-hint";
    empty.textContent = "暂无操作记录";
    listEl.appendChild(empty);
    return;
  }

  entries.forEach((entry) => {
    const row = document.createElement("button");
    row.type = "button";
    row.className = "history-item";
    row.setAttribute("role", "listitem");
    row.dataset.histIdx = String(entry.index);
    if (entry.isCurrent) row.classList.add("is-current");
    if (entry.isFuture) row.classList.add("is-future");
    row.title = entry.isCurrent
      ? `当前版本：${entry.label}`
      : `点击跳转至 v${entry.index}：${entry.label}`;

    const idxSpan = document.createElement("span");
    idxSpan.className = "hist-idx mono-num";
    idxSpan.textContent = `v${entry.index}`;

    const dot = document.createElement("span");
    dot.className = "hist-dot";
    dot.dataset.cat = entry.category || "edit";
    dot.setAttribute("aria-hidden", "true");

    const lbl = document.createElement("span");
    lbl.className = "hist-label";
    lbl.textContent = entry.label;

    const timeSpan = document.createElement("span");
    timeSpan.className = "hist-time mono-num";
    timeSpan.textContent = entry.timeStr;

    row.append(idxSpan, dot, lbl, timeSpan);
    row.addEventListener("click", () => performJumpHistory(entry.index));
    row.addEventListener("contextmenu", (e) => {
      e.preventDefault();
      e.stopPropagation();
      ctxMenu?.open(e.clientX, e.clientY, [
        {
          label: `跳转至 v${entry.index}：${entry.label}`,
          disabled: entry.isCurrent,
          onSelect: () => performJumpHistory(entry.index),
        },
        { type: "separator" },
        { label: "撤销上一步", hint: "Ctrl+Z", disabled: !canUndo, onSelect: () => performUndo() },
        { label: "重做下一步", hint: "Ctrl+Shift+Z", disabled: !canRedo, onSelect: () => performRedo() },
        { label: "回到初始版本 (v0)", disabled: cursor <= 0, onSelect: () => performJumpHistory(0) },
      ]);
    });
    listEl.appendChild(row);
  });

  const curRow = listEl.querySelector(".history-item.is-current");
  curRow?.scrollIntoView({ block: "nearest" });
}

function applyHistoryResult(result, actionWord) {
  if (!result || !state.highlightsData) return false;
  state.highlightsData.highlights = result.snapshot.highlights;
  state.activeClipRank = result.snapshot.activeClipRank;

  // 若正在预览的片段边界变了，同步刷新 previewSegment
  if (state.previewSegment) {
    const cur = getClipByRank(state.previewSegment.rank);
    if (cur) {
      state.previewSegment.start_sec = Number(cur.start_sec);
      state.previewSegment.end_sec = Number(cur.end_sec);
      el("hud-clip-title").textContent = `#${cur.rank} ${cur.title_zh || ""}`;
    } else {
      state.previewSegment = null;
    }
  }

  renderHighlightsList();
  renderActiveSubtitlesPane();
  renderClipInspector();
  syncMonitorClipContext();
  syncVideoSubtitleOverlay();
  scheduleAutoSave();
  setStatusMessage(`${actionWord}：${result.entry.label}`);
  return true;
}

function performUndo() {
  if (!historyMgr) return false;
  const res = historyMgr.undo();
  if (!res) {
    setStatusMessage("已在最早历史版本");
    return false;
  }
  return applyHistoryResult(res, "已撤销 →");
}

function performRedo() {
  if (!historyMgr) return false;
  const res = historyMgr.redo();
  if (!res) {
    setStatusMessage("已在最新历史版本");
    return false;
  }
  return applyHistoryResult(res, "已重做 →");
}

function performJumpHistory(index) {
  if (!historyMgr) return false;
  const res = historyMgr.jumpTo(index);
  if (!res) return false;
  return applyHistoryResult(res, `已跳转至 v${index}`);
}

function initHistoryManager() {
  historyMgr = new HistoryManager({
    maxEntries: 100,
    onChange: (snap) => renderHistoryPane(snap),
  });
  renderHistoryPane(historyMgr.getState());
}

/* ==================== 高光数据、28px 高密度索引表、检查器片段页、独立字幕表 ==================== */
async function loadHighlightsForVideo(video, resetResultBox = true, resetHistory = true) {
  const outDir = encodeURIComponent(el("output-dir-field").value.trim());
  try {
    const data = await apiFetch(
      `/api/highlights?stem=${encodeURIComponent(video.stem)}&output_dir=${outDir}`
    );
    state.highlightsData = data;
    if (resetHistory || state.initialClipBounds.size === 0) {
      state.initialClipBounds = new Map();
      for (const h of data.highlights || []) {
        state.initialClipBounds.set(Number(h.rank), {
          start_sec: Number(h.start_sec) || 0,
          end_sec: Number(h.end_sec) || 0,
        });
      }
    }
    ensureActiveClipForSubtitles();
    if (historyMgr) {
      const snap = captureEditableSnapshot();
      if (snap) {
        if (resetHistory || historyMgr.getState().total === 0) {
          historyMgr.reset(snap, `初始加载 · ${video.filename}`);
        } else {
          historyMgr.commit(snap, `刷新工程数据 · ${video.filename}`, { category: "batch" });
        }
      }
    }
    renderHighlightsList();
    syncLanguagePairUI();
    renderActiveSubtitlesPane();
    renderClipInspector();
    syncMonitorClipContext();
    if (resetResultBox) renderExportManifestSummary(data.clips_manifest);
  } catch {
    state.highlightsData = null;
    state.activeClipRank = null;
    historyMgr?.clear();
    renderHighlightsList();
    syncLanguagePairUI();
    renderActiveSubtitlesPane();
    renderClipInspector();
    syncMonitorClipContext();
    if (resetResultBox) renderExportManifestSummary(null);
  }
  updateStatusBarCounts();
}

function getClipByRank(rank) {
  if (rank == null || !state.highlightsData?.highlights) return null;
  return state.highlightsData.highlights.find((h) => Number(h.rank) === Number(rank)) || null;
}

function ensureActiveClipForSubtitles() {
  const items = state.highlightsData?.highlights || [];
  if (!items.length) {
    state.activeClipRank = null;
    return null;
  }
  if (state.activeClipRank != null && getClipByRank(state.activeClipRank)) {
    return getClipByRank(state.activeClipRank);
  }
  const fallbackRank = state.previewSegment?.rank ?? items[0].rank;
  state.activeClipRank = Number(fallbackRank);
  return getClipByRank(state.activeClipRank);
}

function selectActiveClip(item, shouldPreview = false) {
  if (!item) return;
  state.activeClipRank = Number(item.rank);
  syncHighlightRowStates();
  renderActiveSubtitlesPane();
  renderClipInspector();
  syncMonitorClipContext();
  if (shouldPreview) {
    previewHighlightSegment(item);
  }
}

function scheduleAutoSave() {
  el("save-status-text").textContent = "待保存*";
  if (state.saveTimer) clearTimeout(state.saveTimer);
  state.saveTimer = setTimeout(() => {
    saveHighlightsNow(true);
  }, 600);
}

async function saveHighlightsNow(silent = false) {
  if (!state.highlightsData) return;
  if (state.saveTimer) {
    clearTimeout(state.saveTimer);
    state.saveTimer = null;
  }
  el("save-status-text").textContent = "保存中...";
  try {
    const res = await apiFetch("/api/highlights", {
      method: "PUT",
      body: JSON.stringify({
        highlights_json_path: state.highlightsData.highlights_json_path,
        highlights: state.highlightsData.highlights,
      }),
    });
    // 修复 P0-02：若服务端对越界边界做了规范化 clamp，同步回写前端状态防止前后端撕裂
    if (Array.isArray(res.highlights) && state.highlightsData) {
      let clampedAny = false;
      const byRank = new Map(res.highlights.map((h) => [Number(h.rank), h]));
      for (const local of state.highlightsData.highlights || []) {
        const srv = byRank.get(Number(local.rank));
        if (srv && (srv.start_sec !== local.start_sec || srv.end_sec !== local.end_sec)) {
          local.start_sec = srv.start_sec;
          local.end_sec = srv.end_sec;
          local.duration_sec = srv.duration_sec;
          local.start_str = srv.start_str;
          local.end_str = srv.end_str;
          clampedAny = true;
        }
      }
      if (clampedAny) {
        syncHighlightRowStates();
        renderClipInspector();
        syncMonitorClipContext();
      }
    }
    el("save-status-text").textContent = "已同步";
    if (state.activeVideo) {
      state.activeVideo.selected_count = res.selected_count;
      renderVideoList();
    }
    updateStatusBarCounts();
    if (!silent) setStatusMessage(`已保存 (${res.selected_count} 条已选)`);
  } catch (err) {
    el("save-status-text").textContent = "保存失败";
    showToast(err.message, true);
  }
}

function getDisplayHighlights() {
  if (!state.highlightsData || !Array.isArray(state.highlightsData.highlights)) return [];
  let list = [...state.highlightsData.highlights];
  if (state.onlySelected) {
    list = list.filter((h) => h.selected !== false);
  }
  const q = (state.clipSearchQuery || "").trim().toLowerCase();
  if (q) {
    list = list.filter((h) => {
      if (String(h.title_zh || "").toLowerCase().includes(q)) return true;
      if (String(h.title_en || "").toLowerCase().includes(q)) return true;
      if (String(h.summary_zh || "").toLowerCase().includes(q)) return true;
      if (Array.isArray(h.tags) && h.tags.some((t) => String(t).toLowerCase().includes(q))) return true;
      if (
        Array.isArray(h.subtitles) &&
        h.subtitles.some(
          (s) =>
            String(s.text_zh || "").toLowerCase().includes(q) ||
            String(s.text_en || "").toLowerCase().includes(q)
        )
      ) {
        return true;
      }
      return false;
    });
  }
  if (state.sortBy === "time") {
    list.sort((a, b) => Number(a.start_sec) - Number(b.start_sec));
  } else {
    list.sort((a, b) => Number(a.rank) - Number(b.rank));
  }
  return list;
}

function updateHighlightSelectionBadges() {
  const allItems = state.highlightsData?.highlights || [];
  const selectedTotal = allItems.filter((h) => h.selected !== false).length;
  el("selected-stats-badge").textContent = `${selectedTotal}/${allItems.length}`;
  el("btn-export-selected").textContent = `导出已选 (${selectedTotal})`;
  el("btn-export-selected").disabled = selectedTotal === 0;
  updateStatusBarCounts();
}

function syncHighlightRowStates() {
  updateHighlightSelectionBadges();
  const allItems = state.highlightsData?.highlights || [];
  const byRank = new Map(allItems.map((h) => [Number(h.rank), h]));

  document.querySelectorAll("#highlights-container tr[data-rank]").forEach((row) => {
    const rank = Number(row.dataset.rank);
    const item = byRank.get(rank);
    if (!item) return;

    const isSelected = item.selected !== false;
    const isActive =
      (state.activeClipRank != null && Number(state.activeClipRank) === rank) ||
      (state.previewSegment && Number(state.previewSegment.rank) === rank);

    row.classList.toggle("unselected", !isSelected);
    row.classList.toggle("is-active", Boolean(isActive));

    const chk = row.querySelector(".hl-select-chk");
    if (chk) chk.checked = isSelected;

    const spanTd = row.querySelector(".clip-col-span");
    const durTd = row.querySelector(".clip-col-dur");
    const titleTd = row.querySelector(".clip-title-text");
    const subsTd = row.querySelector(".clip-col-subs");
    const dur = Math.max(0, Number(item.end_sec) - Number(item.start_sec));
    if (spanTd) spanTd.textContent = `${formatClock(item.start_sec)}–${formatClock(item.end_sec)}`;
    if (durTd) durTd.textContent = `${dur.toFixed(1)}s`;
    if (titleTd) {
      titleTd.textContent = item.title_zh || item.title_en || "";
      titleTd.title = item.title_zh || "";
    }
    if (subsTd) subsTd.textContent = String(Array.isArray(item.subtitles) ? item.subtitles.length : 0);
  });
  syncTimelineData();
}

/** 渲染右侧检查器「片段」一等属性页（承接中英标题、Scrubby 精准出入点、完整梗概、标签、AI 理由） */
function renderClipInspector() {
  const emptyEl = el("clip-prop-empty");
  const bodyEl = el("clip-prop-content");
  if (!emptyEl || !bodyEl) return;

  const item = ensureActiveClipForSubtitles();
  if (!item) {
    emptyEl.classList.remove("hidden");
    bodyEl.classList.add("hidden");
    return;
  }

  emptyEl.classList.add("hidden");
  bodyEl.classList.remove("hidden");

  el("clip-prop-rank-badge").textContent = `#${item.rank}`;
  el("clip-prop-selected").checked = item.selected !== false;

  const scoreVal = Number(item.score || 0);
  const scoreBadge = el("clip-prop-score-badge");
  scoreBadge.textContent = `${scoreVal.toFixed(1)}`;
  scoreBadge.dataset.tier = scoreVal >= 9.0 ? "high" : "normal";

  const titleZh = el("clip-prop-title-zh");
  const titleEn = el("clip-prop-title-en");
  const summaryEl = el("clip-prop-summary");
  if (document.activeElement !== titleZh) titleZh.value = item.title_zh || "";
  if (document.activeElement !== titleEn) titleEn.value = item.title_en || "";
  if (document.activeElement !== summaryEl) summaryEl.value = item.summary_zh || "";

  const dur = Math.max(0, Number(item.end_sec) - Number(item.start_sec));
  el("clip-prop-dur").textContent = `${dur.toFixed(1)}s`;

  // 构建或更新 Scrubby Hot-Text 出入点控件（修复 P0-02：随当前视频真实总时长动态约束上限）
  const durLimit = getVideoDurationLimit();
  const startSlot = el("clip-prop-start-slot");
  const endSlot = el("clip-prop-end-slot");
  if (startSlot && !clipStartScrubby) {
    clipStartScrubby = createScrubbyControl({
      value: Number(item.start_sec || 0),
      min: 0,
      max: durLimit,
      step: 0.1,
      precision: 2,
      mode: "timecode",
      title: "入点时间（左右拖拉精调，单击直输 MM:SS.t 或秒数）",
      onChange: (val) => {
        const cur = getClipByRank(state.activeClipRank);
        if (!cur || val === null) return;
        const limit = getVideoDurationLimit();
        const maxStart = Math.max(0, Math.min(limit - 0.5, Number(cur.end_sec) - 0.5));
        const clamped = Math.max(0, Math.min(maxStart, val));
        cur.start_sec = Number(clamped.toFixed(2));
        cur.start_str = formatClock(cur.start_sec);
        cur.duration_sec = Number(Math.max(0, Number(cur.end_sec) - cur.start_sec).toFixed(2));
        if (state.previewSegment && Number(state.previewSegment.rank) === Number(cur.rank)) {
          state.previewSegment.start_sec = cur.start_sec;
        }
        clipStartScrubby.setValue(cur.start_sec);
        el("clip-prop-dur").textContent = `${cur.duration_sec.toFixed(1)}s`;
        syncHighlightRowStates();
        renderActiveSubtitlesPane();
        syncMonitorClipContext();
        recordHistory(`修改 #${cur.rank} 入点 ${formatTimecode(cur.start_sec, true)}`, "trim");
        scheduleAutoSave();
      },
    });
    clipStartScrubby.el.classList.add("scrubby-field-box");
    startSlot.replaceChildren(clipStartScrubby.el);
  }
  if (endSlot && !clipEndScrubby) {
    clipEndScrubby = createScrubbyControl({
      value: Number(item.end_sec || 0),
      min: 0.5,
      max: durLimit,
      step: 0.1,
      precision: 2,
      mode: "timecode",
      title: "出点时间（左右拖拉精调，单击直输 MM:SS.t 或秒数）",
      onChange: (val) => {
        const cur = getClipByRank(state.activeClipRank);
        if (!cur || val === null) return;
        const limit = getVideoDurationLimit();
        const clamped = Math.max(Number(cur.start_sec) + 0.5, Math.min(limit, val));
        cur.end_sec = Number(clamped.toFixed(2));
        cur.end_str = formatClock(cur.end_sec);
        cur.duration_sec = Number(Math.max(0, cur.end_sec - Number(cur.start_sec)).toFixed(2));
        if (state.previewSegment && Number(state.previewSegment.rank) === Number(cur.rank)) {
          state.previewSegment.end_sec = cur.end_sec;
        }
        clipEndScrubby.setValue(cur.end_sec);
        el("clip-prop-dur").textContent = `${cur.duration_sec.toFixed(1)}s`;
        syncHighlightRowStates();
        renderActiveSubtitlesPane();
        syncMonitorClipContext();
        recordHistory(`修改 #${cur.rank} 出点 ${formatTimecode(cur.end_sec, true)}`, "trim");
        scheduleAutoSave();
      },
    });
    clipEndScrubby.el.classList.add("scrubby-field-box");
    endSlot.replaceChildren(clipEndScrubby.el);
  }
  clipStartScrubby?.setBounds(0, Math.max(0, durLimit - 0.5));
  clipEndScrubby?.setBounds(0.5, durLimit);
  clipStartScrubby?.setValue(Number(item.start_sec || 0));
  clipEndScrubby?.setValue(Number(item.end_sec || 0));

  // 标签胶囊
  const tagsBox = el("clip-prop-tags");
  tagsBox.replaceChildren();
  const tags = Array.isArray(item.tags) ? item.tags : [];
  if (tags.length === 0) {
    const emptyTag = document.createElement("span");
    emptyTag.className = "muted-label";
    emptyTag.textContent = "-";
    tagsBox.appendChild(emptyTag);
  } else {
    for (const t of tags) {
      const chip = document.createElement("span");
      chip.className = "tag-chip";
      chip.textContent = String(t);
      tagsBox.appendChild(chip);
    }
  }
}

/** 渲染 28px 高密度专业高光索引表（对标 PR Markers / DaVinci Timeline Index） */
function renderHighlightsList() {
  const container = el("highlights-container");
  if (!container) return;
  const prevScrollTop = container.scrollTop;
  container.replaceChildren();

  updateHighlightSelectionBadges();

  if (!state.highlightsData) {
    const empty = document.createElement("div");
    empty.className = "empty-hint";
    empty.textContent = state.activeVideo ? "无高光数据" : "未选择素材";
    container.appendChild(empty);
    return;
  }

  const displayList = getDisplayHighlights();
  if (displayList.length === 0) {
    const empty = document.createElement("div");
    empty.className = "empty-hint";
    empty.textContent = "无匹配片段";
    container.appendChild(empty);
    return;
  }

  const table = document.createElement("table");
  table.className = "clip-index-table";
  table.innerHTML = `
    <thead>
      <tr>
        <th class="clip-col-chk"></th>
        <th class="clip-col-rank">#</th>
        <th class="clip-col-score">分</th>
        <th class="clip-col-span">入点–出点</th>
        <th class="clip-col-dur">时长</th>
        <th class="clip-col-title">标题</th>
        <th class="clip-col-subs">句</th>
        <th class="clip-col-acts"></th>
      </tr>
    </thead>
  `;

  const tbody = document.createElement("tbody");

  for (const item of displayList) {
    const tr = document.createElement("tr");
    tr.dataset.rank = String(item.rank);
    if (item.selected === false) tr.classList.add("unselected");
    const isActive =
      (state.activeClipRank != null && Number(state.activeClipRank) === Number(item.rank)) ||
      (state.previewSegment && Number(state.previewSegment.rank) === Number(item.rank));
    if (isActive) tr.classList.add("is-active");

    // 1. 勾选
    const tdChk = document.createElement("td");
    tdChk.className = "clip-col-chk";
    const chk = document.createElement("input");
    chk.type = "checkbox";
    chk.className = "hl-select-chk";
    chk.checked = item.selected !== false;
    chk.title = "纳入导出";
    chk.addEventListener("click", (e) => e.stopPropagation());
    chk.addEventListener("change", () => {
      item.selected = chk.checked;
      if (state.onlySelected) renderHighlightsList();
      else syncHighlightRowStates();
      if (Number(state.activeClipRank) === Number(item.rank)) renderClipInspector();
      recordHistory(`${item.selected ? "勾选" : "取消勾选"} #${item.rank}`, "select");
      scheduleAutoSave();
    });
    tdChk.appendChild(chk);

    // 2. Rank
    const tdRank = document.createElement("td");
    tdRank.className = "clip-col-rank mono-num";
    tdRank.textContent = String(item.rank);

    // 3. Score
    const tdScore = document.createElement("td");
    tdScore.className = "clip-col-score";
    const scoreVal = Number(item.score || 0);
    const pill = document.createElement("span");
    pill.className = "score-pill mono-num";
    pill.dataset.tier = scoreVal >= 9.0 ? "high" : "normal";
    pill.textContent = scoreVal.toFixed(1);
    tdScore.appendChild(pill);

    // 4. 入出点
    const tdSpan = document.createElement("td");
    tdSpan.className = "clip-col-span mono-num";
    tdSpan.textContent = `${formatClock(item.start_sec)}–${formatClock(item.end_sec)}`;

    // 5. 时长
    const tdDur = document.createElement("td");
    tdDur.className = "clip-col-dur mono-num";
    const dur = Math.max(0, Number(item.end_sec) - Number(item.start_sec));
    tdDur.textContent = `${dur.toFixed(1)}s`;

    // 6. 中文标题（若回退带极简琥珀点提示）
    const tdTitle = document.createElement("td");
    tdTitle.className = "clip-col-title";
    if (item.translation_status === "fallback") {
      const dot = document.createElement("span");
      dot.className = "fb-dot";
      dot.title = "上次精修触发回退兜底，可点重翻";
      tdTitle.appendChild(dot);
    }
    const titleSpan = document.createElement("span");
    titleSpan.className = "clip-title-text";
    titleSpan.textContent = item.title_zh || item.title_en || "";
    titleSpan.title = `${item.title_zh || ""}\n${item.summary_zh || ""}`.trim();
    tdTitle.appendChild(titleSpan);

    // 7. 字幕句数
    const tdSubs = document.createElement("td");
    tdSubs.className = "clip-col-subs mono-num";
    tdSubs.textContent = String(Array.isArray(item.subtitles) ? item.subtitles.length : 0);

    // 8. 极简 1.25px 几何 SVG 图标操作（彻底去荧光蓝砖！）
    const tdActs = document.createElement("td");
    tdActs.className = "clip-col-acts";
    const actsWrap = document.createElement("div");
    actsWrap.className = "clip-row-acts";

    const btnPlay = document.createElement("button");
    btnPlay.type = "button";
    btnPlay.className = "clip-row-btn";
    btnPlay.title = "播放预览";
    btnPlay.setAttribute("aria-label", "播放预览");
    btnPlay.appendChild(createSvgIcon("play", 12));
    btnPlay.addEventListener("click", (e) => {
      e.stopPropagation();
      previewHighlightSegment(item);
    });

    const btnRe = document.createElement("button");
    btnRe.type = "button";
    btnRe.className = "clip-row-btn";
    btnRe.title = "定点重翻本条双语字幕";
    btnRe.setAttribute("aria-label", "定点重翻");
    btnRe.appendChild(createSvgIcon("retranslate", 12));
    btnRe.addEventListener("click", (e) => {
      e.stopPropagation();
      openRetranslateDialog([item.rank], item.title_zh);
    });

    const btnExp = document.createElement("button");
    btnExp.type = "button";
    btnExp.className = "clip-row-btn";
    btnExp.title = "单独导出本条";
    btnExp.setAttribute("aria-label", "单独导出");
    btnExp.appendChild(createSvgIcon("exportOne", 12));
    btnExp.addEventListener("click", (e) => {
      e.stopPropagation();
      triggerExport([item.rank]);
    });

    actsWrap.append(btnPlay, btnRe, btnExp);
    tdActs.appendChild(actsWrap);

    tr.append(tdChk, tdRank, tdScore, tdSpan, tdDur, tdTitle, tdSubs, tdActs);

    tr.addEventListener("click", () => selectActiveClip(item, false));
    tr.addEventListener("dblclick", () => selectActiveClip(item, true));
    tr.addEventListener("contextmenu", (e) => {
      e.preventDefault();
      e.stopPropagation();
      selectActiveClip(item, false);
      ctxMenu?.open(e.clientX, e.clientY, [
        {
          label: "播放预览本段",
          hint: "双击",
          onSelect: () => previewHighlightSegment(item),
        },
        {
          label: "纳入成片导出",
          hint: "S",
          checked: item.selected !== false,
          onSelect: () => {
            item.selected = item.selected === false;
            if (state.onlySelected) renderHighlightsList();
            else syncHighlightRowStates();
            renderClipInspector();
            recordHistory(`${item.selected ? "勾选" : "取消勾选"} #${item.rank}`, "select");
            scheduleAutoSave();
          },
        },
        { type: "separator" },
        {
          label: "在字幕面板精编",
          onSelect: () => dock?.focusPanel("subtitles"),
        },
        {
          label: "在检查器编辑属性",
          onSelect: () => {
            dock?.focusPanel("inspector");
            switchInspectorTab("clip");
          },
        },
        { type: "separator" },
        {
          label: "定点重翻本条双语",
          onSelect: () => openRetranslateDialog([item.rank], item.title_zh),
        },
        {
          label: "单独导出本条切片",
          onSelect: () => triggerExport([item.rank]),
        },
      ]);
    });

    tbody.appendChild(tr);
  }

  table.appendChild(tbody);
  container.appendChild(table);
  container.scrollTop = prevScrollTop;
  syncVideoSubtitleOverlay();
}

/** 渲染独立「字幕」面板（Scrubby Hot-Text 时码 + 统一 SVG 图标 + ASR 纠错微标 + 右键菜单） */
function renderActiveSubtitlesPane() {
  const listEl = el("sub-pane-list");
  const clipLbl = el("sub-pane-clip-label");
  const countBadge = el("sub-pane-count");
  if (!listEl || !clipLbl || !countBadge) return;

  // 修复 P2-06：保留并还原字幕表滚动位置，重绘不跳回顶部
  const prevScrollTop = listEl.scrollTop;
  const item = ensureActiveClipForSubtitles();
  listEl.replaceChildren();

  if (!item) {
    clipLbl.textContent = "未选中片段";
    countBadge.textContent = "0";
    const empty = document.createElement("div");
    empty.className = "empty-hint";
    empty.textContent = "选中高光后在此精编双语字幕";
    listEl.appendChild(empty);
    return;
  }

  const subs = Array.isArray(item.subtitles) ? item.subtitles : (item.subtitles = []);
  clipLbl.textContent = `#${item.rank} ${item.title_zh || ""}`;
  clipLbl.title = clipLbl.textContent;
  countBadge.textContent = String(subs.length);

  if (subs.length === 0) {
    const empty = document.createElement("div");
    empty.className = "empty-hint";
    empty.textContent = "暂无字幕";
    listEl.appendChild(empty);
    return;
  }

  subs.forEach((s, idx) => {
    const row = document.createElement("div");
    row.className = "sub-editor-row";
    row.dataset.subKey = `${item.rank}:${idx}`;
    if (state.activeSubKey === row.dataset.subKey) {
      row.classList.add("active-sub");
    }

    const doMergeUp = () => {
      if (idx <= 0) return;
      const prev = subs[idx - 1];
      prev.end_sec = Number(Math.max(Number(prev.end_sec), Number(s.end_sec)).toFixed(2));
      prev.text_zh = `${(prev.text_zh || "").trim()} ${(s.text_zh || "").trim()}`.trim();
      prev.text_en = `${(prev.text_en || "").trim()} ${(s.text_en || "").trim()}`.trim();
      prev.text_orig = `${(prev.text_orig || "").trim()} ${(s.text_orig || "").trim()}`.trim();
      subs.splice(idx, 1);
      renderActiveSubtitlesPane();
      syncHighlightRowStates();
      syncVideoSubtitleOverlay();
      recordHistory(`合并 #${item.rank} 第${idx + 1}句`, "sub");
      scheduleAutoSave();
    };

    const doSplit = () => {
      const st = Number(s.start_sec || 0);
      const ed = Number(s.end_sec || st + 1);
      if (ed - st < 0.6) {
        setStatusMessage("本句过短无法拆分", true);
        return;
      }
      const videoEl = el("review-video");
      const curT = videoEl ? Number(videoEl.currentTime || 0) : 0;
      const usePlayhead = curT > st + 0.25 && curT < ed - 0.25;
      const mid = Number((usePlayhead ? curT : (st + ed) / 2).toFixed(2));
      const ratio = clampNum((mid - st) / Math.max(0.1, ed - st), 0.15, 0.85);
      const zhText = (s.text_zh || "").trim();
      const zhMid = Math.max(1, Math.min(zhText.length - 1, Math.round(zhText.length * ratio)));
      const enWords = (s.text_en || "").trim().split(/\s+/).filter(Boolean);
      const enMid = Math.max(1, Math.min(enWords.length - 1, Math.round(enWords.length * ratio)));

      const secondHalf = {
        start_sec: mid,
        end_sec: Number(ed.toFixed(2)),
        text_orig: s.text_orig || "",
        text_zh: zhText.slice(zhMid).trim(),
        text_en: enWords.slice(enMid).join(" ").trim(),
      };
      s.end_sec = mid;
      s.text_zh = zhText.slice(0, zhMid).trim();
      s.text_en = enWords.slice(0, enMid).join(" ").trim();
      subs.splice(idx + 1, 0, secondHalf);
      renderActiveSubtitlesPane();
      syncHighlightRowStates();
      syncVideoSubtitleOverlay();
      recordHistory(`拆分 #${item.rank} 第${idx + 1}句`, "sub");
      scheduleAutoSave();
    };

    const doDelete = () => {
      subs.splice(idx, 1);
      renderActiveSubtitlesPane();
      syncHighlightRowStates();
      syncVideoSubtitleOverlay();
      recordHistory(`删除 #${item.rank} 第${idx + 1}句`, "sub");
      scheduleAutoSave();
    };

    // 左列：跳播图标 + Scrubby Hot-Text 起止时码（彻底告别 1145.77 纯秒数心算）
    const timeCol = document.createElement("div");
    timeCol.className = "sub-time-col";

    const tcLine = document.createElement("div");
    tcLine.className = "sub-tc-line";

    const jumpBtn = document.createElement("button");
    jumpBtn.type = "button";
    jumpBtn.className = "sub-jump-icon";
    jumpBtn.title = "跳播本句";
    jumpBtn.setAttribute("aria-label", "跳播本句");
    jumpBtn.appendChild(createSvgIcon("play", 10));
    jumpBtn.addEventListener("click", () => jumpToSubtitleLine(item, s));

    const startScrub = createScrubbyControl({
      value: Number(s.start_sec || 0),
      min: Math.max(0, Number(item.start_sec) || 0),
      max: Number(item.end_sec) || 86400,
      step: 0.05,
      precision: 2,
      mode: "timecode",
      title: "本句起点（左右拖拉微调，单击直输）",
      onChange: (val) => {
        if (val === null) return;
        s.start_sec = Number(Math.max(Number(item.start_sec) || 0, val).toFixed(2));
        if (Number(s.end_sec) <= s.start_sec) {
          s.end_sec = Number((s.start_sec + 0.5).toFixed(2));
          endScrub.setValue(s.end_sec);
        }
        syncVideoSubtitleOverlay();
        recordHistory(`调 #${item.rank} 第${idx + 1}句起点`, "sub");
        scheduleAutoSave();
      },
    });

    const sep = document.createElement("span");
    sep.className = "sub-tc-sep";
    sep.textContent = "–";

    const endScrub = createScrubbyControl({
      value: Number(s.end_sec || 0),
      min: Number(s.start_sec || 0) + 0.25,
      max: Number(item.end_sec) || 86400,
      step: 0.05,
      precision: 2,
      mode: "timecode",
      title: "本句终点（左右拖拉微调，单击直输）",
      onChange: (val) => {
        if (val === null) return;
        const minEnd = Number(s.start_sec || 0) + 0.25;
        const maxEnd = Math.max(minEnd, Number(item.end_sec) || 86400);
        s.end_sec = Number(clampNum(val, minEnd, maxEnd).toFixed(2));
        endScrub.setValue(s.end_sec);
        syncVideoSubtitleOverlay();
        recordHistory(`调 #${item.rank} 第${idx + 1}句终点`, "sub");
        scheduleAutoSave();
      },
    });

    tcLine.append(jumpBtn, startScrub.el, sep, endScrub.el);
    timeCol.appendChild(tcLine);

    // 若存在 ASR 原文误听纠错差异，给极简微标悬停对比
    const origRaw = (s.text_orig || "").trim();
    const enClean = (s.text_en || "").trim();
    if (origRaw && enClean && origRaw.toLowerCase() !== enClean.toLowerCase()) {
      const corr = document.createElement("span");
      corr.className = "sub-corr-tag";
      corr.textContent = "ASR 纠偏";
      corr.title = `ASR 原文：${origRaw}\n精修英文：${enClean}`;
      timeCol.appendChild(corr);
    }

    // P1-10：字幕字数 / 时长安全告警（防烧录成片换行遮挡或闪屏）
    const subDur = Math.max(0, Number(s.end_sec || 0) - Number(s.start_sec || 0));
    const zhLen = (s.text_zh || "").trim().length;
    if (zhLen > 18 || subDur < 0.7 || subDur > 6.5) {
      const warn = document.createElement("span");
      warn.className = "sub-warn-tag mono-num";
      const reasons = [];
      if (zhLen > 18) reasons.push(`中文 ${zhLen}字>18`);
      if (subDur < 0.7) reasons.push(`时长 ${subDur.toFixed(2)}s 偏短`);
      if (subDur > 6.5) reasons.push(`时长 ${subDur.toFixed(1)}s 偏长`);
      warn.textContent = zhLen > 18 ? `${zhLen}字` : `${subDur.toFixed(1)}s`;
      warn.title = `字幕节奏提醒：${reasons.join(" · ")}`;
      timeCol.appendChild(warn);
    }

    // 中列：动态多语种双行精编（上行：目标译文，下行：源语言原文）
    const lp = getActiveLanguagePair();
    const pairTexts = resolveSubPairTexts(s, lp);
    const textCol = document.createElement("div");
    textCol.className = "sub-text-col";

    const inZh = document.createElement("input");
    inZh.type = "text";
    inZh.className = "sub-input-zh";
    inZh.value = pairTexts.tgtText;
    inZh.placeholder = `${lp.tgtMeta.name} (${lp.tgtMeta.code} 译文)`;
    inZh.addEventListener("input", () => {
      const val = inZh.value;
      s.text_tgt = val;
      if (lp.isZhToEn) s.text_en = val;
      else s.text_zh = val;
      syncVideoSubtitleOverlay();
      recordHistory(`改 #${item.rank} 第${idx + 1}句译文`, "sub", `sub_zh_${item.rank}_${idx}`);
      scheduleAutoSave();
    });

    const inEn = document.createElement("input");
    inEn.type = "text";
    inEn.className = "sub-input-en";
    inEn.value = pairTexts.srcText;
    inEn.placeholder = `${lp.srcMeta.name} (${lp.srcMeta.code} 原文)`;
    inEn.addEventListener("input", () => {
      const val = inEn.value;
      s.text_src = val;
      if (lp.isZhToEn) s.text_zh = val;
      else s.text_en = val;
      syncVideoSubtitleOverlay();
      recordHistory(`改 #${item.rank} 第${idx + 1}句原文`, "sub", `sub_en_${item.rank}_${idx}`);
      scheduleAutoSave();
    });

    textCol.append(inZh, inEn);

    // 右列：统一 1.25px SVG 图标动作
    const opsCol = document.createElement("div");
    opsCol.className = "sub-ops-col";

    if (idx > 0) {
      const btnMerge = document.createElement("button");
      btnMerge.type = "button";
      btnMerge.className = "sub-op-btn";
      btnMerge.title = "合并至上一句";
      btnMerge.setAttribute("aria-label", "合并至上一句");
      btnMerge.appendChild(createSvgIcon("mergeUp", 12));
      btnMerge.addEventListener("click", doMergeUp);
      opsCol.appendChild(btnMerge);
    }

    const btnSplit = document.createElement("button");
    btnSplit.type = "button";
    btnSplit.className = "sub-op-btn";
    btnSplit.title = "拆分本句（优先按播放头位置）";
    btnSplit.setAttribute("aria-label", "拆分本句");
    btnSplit.appendChild(createSvgIcon("split", 12));
    btnSplit.addEventListener("click", doSplit);

    const btnDel = document.createElement("button");
    btnDel.type = "button";
    btnDel.className = "sub-op-btn danger";
    btnDel.title = "删除本句";
    btnDel.setAttribute("aria-label", "删除本句");
    btnDel.appendChild(createSvgIcon("trash", 12));
    btnDel.addEventListener("click", doDelete);

    opsCol.append(btnSplit, btnDel);
    row.append(timeCol, textCol, opsCol);

    row.addEventListener("contextmenu", (e) => {
      if (e.target && e.target.closest("input, textarea")) return;
      e.preventDefault();
      e.stopPropagation();
      ctxMenu?.open(e.clientX, e.clientY, [
        { label: "跳播本句", onSelect: () => jumpToSubtitleLine(item, s) },
        { type: "separator" },
        {
          label: "吸附至上一句结尾",
          disabled: idx === 0,
          onSelect: () => {
            if (idx <= 0) return;
            const prevEnd = Number(subs[idx - 1].end_sec || 0);
            if (prevEnd < Number(s.end_sec || 0) - 0.25) {
              s.start_sec = Number(prevEnd.toFixed(2));
              renderActiveSubtitlesPane();
              syncVideoSubtitleOverlay();
              syncMonitorClipContext();
              recordHistory(`吸附 #${item.rank} 第${idx + 1}句起点`, "sub");
              scheduleAutoSave();
            }
          },
        },
        { label: "合并至上一句", disabled: idx === 0, onSelect: doMergeUp },
        { label: "拆分本句（按播放头/中点）", onSelect: doSplit },
        { type: "separator" },
        { label: "删除本句", danger: true, onSelect: doDelete },
      ]);
    });

    listEl.appendChild(row);
  });
  listEl.scrollTop = prevScrollTop;
}

/* ==================== 播放器控制、实时双语叠字与快捷键 ==================== */
function findSubtitleAtTime(t) {
  const highlights = state.highlightsData?.highlights || [];
  if (!highlights.length) return null;

  const ordered = [];
  if (state.previewSegment) {
    const cur = highlights.find((h) => Number(h.rank) === Number(state.previewSegment.rank));
    if (cur) ordered.push(cur);
  }
  for (const h of highlights) {
    if (!ordered.includes(h)) ordered.push(h);
  }

  for (const h of ordered) {
    const subs = Array.isArray(h.subtitles) ? h.subtitles : [];
    for (let i = 0; i < subs.length; i += 1) {
      const s = subs[i];
      if (t >= Number(s.start_sec) - 0.05 && t <= Number(s.end_sec) + 0.08) {
        return { rank: h.rank, index: i, sub: s, key: `${h.rank}:${i}` };
      }
    }
  }
  return null;
}

function syncVideoSubtitleOverlay() {
  const videoEl = el("review-video");
  const box = el("video-sub-overlay");
  const zhEl = el("overlay-sub-zh");
  const enEl = el("overlay-sub-en");
  if (!videoEl || !box || !zhEl || !enEl) return;

  const mode = state.overlaySubMode || "bilingual";
  const hit = findSubtitleAtTime(videoEl.currentTime || 0);
  const newKey = hit ? hit.key : "";

  if (newKey !== state.activeSubKey) {
    if (state.activeSubKey) {
      document
        .querySelectorAll(`.sub-editor-row[data-sub-key="${state.activeSubKey}"]`)
        .forEach((n) => n.classList.remove("active-sub"));
    }
    state.activeSubKey = newKey;
    if (newKey) {
      const rows = document.querySelectorAll(`.sub-editor-row[data-sub-key="${newKey}"]`);
      rows.forEach((n) => {
        n.classList.add("active-sub");
        const activeTag = document.activeElement?.tagName?.toLowerCase() || "";
        if (activeTag !== "input" && activeTag !== "textarea") {
          n.scrollIntoView({ block: "nearest", behavior: "smooth" });
        }
      });
    }
  }

  if (mode === "off" || !hit) {
    box.classList.add("hidden");
    zhEl.textContent = "";
    enEl.textContent = "";
    return;
  }

  const lp = getActiveLanguagePair();
  const { tgtText, srcText } = resolveSubPairTexts(hit.sub, lp);
  if (mode === "target" || mode === "zh") {
    zhEl.textContent = mode === "zh" ? (hit.sub.text_zh || tgtText) : tgtText;
    enEl.textContent = "";
  } else if (mode === "source" || mode === "en") {
    zhEl.textContent = "";
    enEl.textContent = mode === "en" ? (hit.sub.text_en || srcText) : srcText;
  } else if (mode === "bilingual_rev") {
    zhEl.textContent = srcText;
    enEl.textContent = tgtText !== srcText ? tgtText : "";
  } else {
    zhEl.textContent = tgtText;
    enEl.textContent = srcText !== tgtText ? srcText : "";
  }
  box.classList.toggle("hidden", !zhEl.textContent && !enEl.textContent);
}

function previewHighlightSegment(item) {
  const videoEl = el("review-video");
  if (!videoEl || !videoEl.src) return;
  stopShuttle(true);

  state.previewSegment = {
    rank: item.rank,
    start_sec: Number(item.start_sec),
    end_sec: Number(item.end_sec),
  };
  state.activeClipRank = Number(item.rank);
  el("hud-clip-title").textContent = `#${item.rank} ${item.title_zh || ""}`;

  syncHighlightRowStates();
  renderActiveSubtitlesPane();
  renderClipInspector();
  syncMonitorClipContext();

  videoEl.currentTime = Number(item.start_sec);
  videoEl.play().catch(() => {});
  syncVideoSubtitleOverlay();
}

function jumpToSubtitleLine(item, sub) {
  const videoEl = el("review-video");
  if (!videoEl || !videoEl.src) return;
  state.previewSegment = {
    rank: item.rank,
    start_sec: Number(item.start_sec),
    end_sec: Number(item.end_sec),
  };
  state.activeClipRank = Number(item.rank);
  el("hud-clip-title").textContent = `#${item.rank} ${item.title_zh || ""}`;
  syncHighlightRowStates();
  videoEl.currentTime = Math.max(0, Number(sub.start_sec || 0));
  videoEl.play().catch(() => {});
  syncVideoSubtitleOverlay();
}

function markPreviewBoundary(edge) {
  const videoEl = el("review-video");
  const targetRank = state.previewSegment?.rank ?? state.activeClipRank;
  const target = getClipByRank(targetRank);
  if (!videoEl || !target) {
    setStatusMessage("请先选中一个高光片段", true);
    return;
  }
  if (!state.previewSegment || Number(state.previewSegment.rank) !== Number(target.rank)) {
    state.previewSegment = {
      rank: target.rank,
      start_sec: Number(target.start_sec),
      end_sec: Number(target.end_sec),
    };
  }

  const now = Number((videoEl.currentTime || 0).toFixed(2));
  if (edge === "in") {
    if (now >= Number(target.end_sec) - 0.5) {
      setStatusMessage("入点不能晚于出点", true);
      return;
    }
    target.start_sec = now;
    target.start_str = formatClock(now);
    state.previewSegment.start_sec = now;
    setStatusMessage(`#${target.rank} 入点 ${formatTimecode(now, true)}`);
  } else {
    if (now <= Number(target.start_sec) + 0.5) {
      setStatusMessage("出点不能早于入点", true);
      return;
    }
    target.end_sec = now;
    target.end_str = formatClock(now);
    state.previewSegment.end_sec = now;
    setStatusMessage(`#${target.rank} 出点 ${formatTimecode(now, true)}`);
  }
  target.duration_sec = Number(
    Math.max(0, Number(target.end_sec) - Number(target.start_sec)).toFixed(2)
  );
  syncHighlightRowStates();
  renderClipInspector();
  renderActiveSubtitlesPane();
  syncMonitorClipContext();
  recordHistory(
    `修改 #${target.rank} ${edge === "in" ? "入点" : "出点"} ${formatTimecode(now, true)}`,
    "trim"
  );
  scheduleAutoSave();
}

/* ==================== PR / DaVinci 级 J/K/L 变速正反梭巡与完整快捷键引擎 ==================== */
const SHUTTLE_SPEEDS = [1, 2, 4, 8];
const shuttle = {
  dir: 0, // -1 反向, 0 静止, 1 正向
  level: 0, // 0..3 对应 1x/2x/4x/8x
  slowActive: false,
  kHeld: false,
  revRaf: null,
  lastTs: 0,
};

function syncShuttleBadge() {
  const badge = el("hud-shuttle-badge");
  if (!badge) return;
  if (shuttle.dir === 0 && !shuttle.slowActive) {
    badge.classList.add("hidden");
    badge.classList.remove("is-reverse");
    return;
  }
  const speed = shuttle.slowActive ? 0.5 : SHUTTLE_SPEEDS[shuttle.level] || 1;
  if (shuttle.dir < 0) {
    badge.textContent = `◀◀ -${speed}x`;
    badge.classList.add("is-reverse");
    badge.classList.remove("hidden");
  } else if (shuttle.dir > 0 && (speed !== 1 || shuttle.slowActive)) {
    badge.textContent = `▶▶ ${speed}x`;
    badge.classList.remove("is-reverse");
    badge.classList.remove("hidden");
  } else {
    badge.classList.add("hidden");
    badge.classList.remove("is-reverse");
  }
}

function stopShuttle(resetPlaybackRate = true) {
  if (shuttle.revRaf != null) {
    cancelAnimationFrame(shuttle.revRaf);
    shuttle.revRaf = null;
  }
  shuttle.dir = 0;
  shuttle.level = 0;
  shuttle.slowActive = false;
  shuttle.lastTs = 0;
  if (resetPlaybackRate) {
    const videoEl = el("review-video");
    const uiRate = Number(el("sel-playback-rate")?.value) || 1.0;
    if (videoEl) videoEl.playbackRate = uiRate;
  }
  syncShuttleBadge();
}

function startReverseShuttleLoop(videoEl, speed) {
  if (shuttle.revRaf != null) {
    cancelAnimationFrame(shuttle.revRaf);
    shuttle.revRaf = null;
  }
  videoEl.pause();
  shuttle.lastTs = performance.now();

  const tick = (now) => {
    if (shuttle.dir >= 0) {
      shuttle.revRaf = null;
      return;
    }
    const dt = Math.min(0.1, (now - shuttle.lastTs) / 1000);
    shuttle.lastTs = now;
    const curSpeed = shuttle.slowActive ? 0.5 : SHUTTLE_SPEEDS[shuttle.level] || 1;
    const nextT = Math.max(0, (videoEl.currentTime || 0) - dt * curSpeed);
    videoEl.currentTime = nextT;
    monitorEngine?.refreshFrameUI();

    if (nextT <= 0.001) {
      stopShuttle(true);
      setStatusMessage("已倒退至片头");
      return;
    }
    shuttle.revRaf = requestAnimationFrame(tick);
  };
  shuttle.revRaf = requestAnimationFrame(tick);
}

function triggerShuttle(dir, isRepeat = false) {
  const videoEl = el("review-video");
  if (!videoEl || !videoEl.src) return;

  // K 按住配合 J/L：进入专业 0.5x 慢放前进 / -0.5x 慢速倒放
  if (shuttle.kHeld) {
    if (isRepeat) return;
    shuttle.slowActive = true;
    shuttle.dir = dir;
    shuttle.level = 0;
    if (dir > 0) {
      if (shuttle.revRaf != null) {
        cancelAnimationFrame(shuttle.revRaf);
        shuttle.revRaf = null;
      }
      videoEl.playbackRate = 0.5;
      videoEl.play().catch(() => {});
    } else {
      startReverseShuttleLoop(videoEl, 0.5);
    }
    syncShuttleBadge();
    return;
  }

  if (isRepeat) return;
  shuttle.slowActive = false;

  if (dir > 0) {
    if (shuttle.revRaf != null) {
      cancelAnimationFrame(shuttle.revRaf);
      shuttle.revRaf = null;
    }
    if (shuttle.dir > 0 && !videoEl.paused) {
      shuttle.level = Math.min(SHUTTLE_SPEEDS.length - 1, shuttle.level + 1);
    } else {
      shuttle.dir = 1;
      shuttle.level = 0;
    }
    const spd = SHUTTLE_SPEEDS[shuttle.level];
    videoEl.playbackRate = spd;
    videoEl.play().catch(() => {});
    syncShuttleBadge();
    if (spd > 1) setStatusMessage(`快进梭巡 ${spd}x`);
  } else {
    if (shuttle.dir < 0) {
      shuttle.level = Math.min(SHUTTLE_SPEEDS.length - 1, shuttle.level + 1);
    } else {
      shuttle.dir = -1;
      shuttle.level = 0;
    }
    const spd = SHUTTLE_SPEEDS[shuttle.level];
    startReverseShuttleLoop(videoEl, spd);
    syncShuttleBadge();
    setStatusMessage(`反向梭巡 -${spd}x`);
  }
}

function stepVideoFrames(frameDelta) {
  const videoEl = el("review-video");
  if (!videoEl || !videoEl.src) return;
  stopShuttle(true);
  videoEl.pause();
  const fps = Math.max(1, Number(state.videoFps) || 30);
  const nextT = clampNum(
    (videoEl.currentTime || 0) + frameDelta / fps,
    0,
    videoEl.duration || 999999
  );
  videoEl.currentTime = nextT;
  monitorEngine?.refreshFrameUI();
}

function stepVideoSeconds(secDelta) {
  const videoEl = el("review-video");
  if (!videoEl || !videoEl.src) return;
  stopShuttle(true);
  const nextT = clampNum(
    (videoEl.currentTime || 0) + secDelta,
    0,
    videoEl.duration || 999999
  );
  videoEl.currentTime = nextT;
  monitorEngine?.refreshFrameUI();
}

function navigateHighlightByOffset(delta) {
  const list = getDisplayHighlights();
  if (!list.length) return;
  const curIdx = list.findIndex((h) => Number(h.rank) === Number(state.activeClipRank));
  const nextIdx =
    curIdx < 0
      ? 0
      : Math.max(0, Math.min(list.length - 1, curIdx + delta));
  const target = list[nextIdx];
  if (!target) return;
  selectActiveClip(target, false);
  const row = document.querySelector(`#highlights-container tr[data-rank="${target.rank}"]`);
  row?.scrollIntoView({ block: "nearest" });
  setStatusMessage(`已选中 #${target.rank} ${target.title_zh || ""}`);
}

function insertSubtitleAtPlayhead() {
  const item = ensureActiveClipForSubtitles();
  if (!item) {
    setStatusMessage("请先选中一个高光片段", true);
    return;
  }
  const subs = Array.isArray(item.subtitles) ? item.subtitles : (item.subtitles = []);
  const videoEl = el("review-video");
  const curT = videoEl ? Number(videoEl.currentTime || 0) : 0;
  const inClip = curT >= Number(item.start_sec) && curT < Number(item.end_sec) - 0.5;
  const last = subs[subs.length - 1];
  const startSec = inClip
    ? curT
    : last
      ? Number(last.end_sec || item.start_sec)
      : Number(item.start_sec || 0);
  const endSec = Math.min(Number(item.end_sec || startSec + 2), startSec + 2.0);
  subs.push({
    start_sec: Number(startSec.toFixed(2)),
    end_sec: Number(Math.max(startSec + 0.5, endSec).toFixed(2)),
    text_orig: "",
    text_zh: "",
    text_en: "",
  });
  subs.sort((a, b) => Number(a.start_sec) - Number(b.start_sec));
  renderActiveSubtitlesPane();
  syncHighlightRowStates();
  recordHistory(`插入 #${item.rank} 字幕`, "sub");
  scheduleAutoSave();
  setStatusMessage(`已在 ${formatTimecode(startSec, true)} 插入新字幕`);
}

function splitSubtitleLineAtTime(item, subIndex, cutSec = null) {
  if (!item || !Array.isArray(item.subtitles)) return false;
  const s = item.subtitles[subIndex];
  if (!s) return false;

  const st = Number(s.start_sec || 0);
  const ed = Number(s.end_sec || st + 1);
  if (ed - st < 0.6) {
    setStatusMessage("当前字幕句过短无法拆分", true);
    return false;
  }
  const hasCut = cutSec !== null && cutSec > st + 0.25 && cutSec < ed - 0.25;
  const mid = Number((hasCut ? cutSec : (st + ed) / 2).toFixed(2));
  const ratio = clampNum((mid - st) / Math.max(0.1, ed - st), 0.15, 0.85);
  const zhText = (s.text_zh || "").trim();
  const enWords = (s.text_en || "").trim().split(/\s+/).filter(Boolean);
  const origWords = (s.text_orig || "").trim().split(/\s+/).filter(Boolean);

  // 修复 P2-01：极短单字/单词字幕不允许劈出空后半句，并按比例同步拆分 text_orig
  if (zhText.length <= 1 && enWords.length <= 1) {
    setStatusMessage("本句文本过短，无法继续拆分", true);
    return false;
  }

  const zhMid = zhText.length > 1 ? Math.max(1, Math.min(zhText.length - 1, Math.round(zhText.length * ratio))) : zhText.length;
  const enMid = enWords.length > 1 ? Math.max(1, Math.min(enWords.length - 1, Math.round(enWords.length * ratio))) : enWords.length;
  const origMid = origWords.length > 1 ? Math.max(1, Math.min(origWords.length - 1, Math.round(origWords.length * ratio))) : origWords.length;

  const secondHalf = {
    start_sec: mid,
    end_sec: Number(ed.toFixed(2)),
    text_orig: origWords.length > 1 ? origWords.slice(origMid).join(" ").trim() : "",
    text_zh: zhText.length > 1 ? zhText.slice(zhMid).trim() : "",
    text_en: enWords.length > 1 ? enWords.slice(enMid).join(" ").trim() : "",
  };
  s.end_sec = mid;
  if (origWords.length > 1) s.text_orig = origWords.slice(0, origMid).join(" ").trim();
  if (zhText.length > 1) s.text_zh = zhText.slice(0, zhMid).trim();
  if (enWords.length > 1) s.text_en = enWords.slice(0, enMid).join(" ").trim();
  item.subtitles.splice(subIndex + 1, 0, secondHalf);

  state.activeClipRank = Number(item.rank);
  renderActiveSubtitlesPane();
  syncHighlightRowStates();
  syncVideoSubtitleOverlay();
  syncMonitorClipContext();
  recordHistory(`剃刀拆分 #${item.rank} 第${subIndex + 1}句`, "sub");
  scheduleAutoSave();
  setStatusMessage(`已在 ${formatTimecode(mid, true)} 拆分字幕`);
  return true;
}

function splitSubtitleAtPlayhead() {
  const videoEl = el("review-video");
  if (!videoEl || !state.highlightsData) return;
  const curT = Number(videoEl.currentTime || 0);
  const hit = findSubtitleAtTime(curT);
  if (!hit) {
    setStatusMessage("当前播放头位置未命中字幕句", true);
    return;
  }
  const item = getClipByRank(hit.rank);
  if (!item) return;
  splitSubtitleLineAtTime(item, hit.index, curT);
}

/* ==================== 本地达芬奇 21.1 逆向 P0 剪辑/标记/索引/字幕查找替换动作 ==================== */
function resetClipBoundary(which = "both") {
  const target = getClipByRank(state.previewSegment?.rank ?? state.activeClipRank);
  if (!target) {
    setStatusMessage("请先选中一个高光片段", true);
    return;
  }
  const orig = state.initialClipBounds.get(Number(target.rank));
  if (!orig) {
    setStatusMessage("无初始边界记录", true);
    return;
  }
  const curS = Number(target.start_sec || 0);
  const curE = Number(target.end_sec || 0);
  let nextS = curS;
  let nextE = curE;
  if (which === "in" || which === "both") nextS = orig.start_sec;
  if (which === "out" || which === "both") nextE = orig.end_sec;
  if (nextE <= nextS + 0.5) {
    setStatusMessage("还原后入点晚于出点，已取消", true);
    return;
  }
  target.start_sec = Number(nextS.toFixed(2));
  target.end_sec = Number(nextE.toFixed(2));
  target.start_str = formatClock(target.start_sec);
  target.end_str = formatClock(target.end_sec);
  target.duration_sec = Number(Math.max(0, target.end_sec - target.start_sec).toFixed(2));
  if (state.previewSegment && Number(state.previewSegment.rank) === Number(target.rank)) {
    state.previewSegment.start_sec = target.start_sec;
    state.previewSegment.end_sec = target.end_sec;
  }
  syncHighlightRowStates();
  renderClipInspector();
  syncMonitorClipContext();
  const labelMap = { in: "还原入点", out: "还原出点", both: "还原出入点" };
  recordHistory(`${labelMap[which] || "还原边界"} #${target.rank}`, "trim");
  scheduleAutoSave();
  setStatusMessage(`#${target.rank} 已${labelMap[which] || "还原边界"}`);
}

function trimClipToPlayhead(edge) {
  const videoEl = el("review-video");
  const target = getClipByRank(state.previewSegment?.rank ?? state.activeClipRank);
  if (!videoEl || !target) {
    setStatusMessage("请先选中一个高光片段", true);
    return;
  }
  const curT = Number((videoEl.currentTime || 0).toFixed(2));
  if (edge === "start") {
    if (curT < Number(target.start_sec) || curT >= Number(target.end_sec) - 1.0) {
      setStatusMessage("请将播放头置于当前片段内部再裁头", true);
      return;
    }
    target.start_sec = curT;
    target.start_str = formatClock(curT);
  } else {
    if (curT <= Number(target.start_sec) + 1.0 || curT > Number(target.end_sec)) {
      setStatusMessage("请将播放头置于当前片段内部再裁尾", true);
      return;
    }
    target.end_sec = curT;
    target.end_str = formatClock(curT);
  }
  target.duration_sec = Number(Math.max(0, Number(target.end_sec) - Number(target.start_sec)).toFixed(2));
  if (state.previewSegment && Number(state.previewSegment.rank) === Number(target.rank)) {
    state.previewSegment.start_sec = Number(target.start_sec);
    state.previewSegment.end_sec = Number(target.end_sec);
  }
  syncHighlightRowStates();
  renderClipInspector();
  syncMonitorClipContext();
  recordHistory(
    `${edge === "start" ? "裁头" : "裁尾"}到指针 #${target.rank} (${formatTimecode(curT, true)})`,
    "trim"
  );
  scheduleAutoSave();
  setStatusMessage(`#${target.rank} 已${edge === "start" ? "裁头" : "裁尾"}至 ${formatTimecode(curT, true)}`);
}

function splitActiveClipAtPlayhead() {
  const videoEl = el("review-video");
  const list = state.highlightsData?.highlights;
  const target = getClipByRank(state.previewSegment?.rank ?? state.activeClipRank);
  if (!videoEl || !Array.isArray(list) || !target) {
    setStatusMessage("请先选中一个高光片段", true);
    return false;
  }
  const curT = Number((videoEl.currentTime || 0).toFixed(2));
  const st = Number(target.start_sec || 0);
  const ed = Number(target.end_sec || 0);
  if (curT <= st + 1.0 || curT >= ed - 1.0) {
    setStatusMessage("播放头需在片段内部且距首尾至少 1 秒", true);
    return false;
  }

  const allSubs = Array.isArray(target.subtitles) ? target.subtitles : [];
  const leftSubs = [];
  const rightSubs = [];
  for (const s of allSubs) {
    const mid = (Number(s.start_sec || 0) + Number(s.end_sec || 0)) / 2;
    if (mid < curT) leftSubs.push(s);
    else rightSubs.push(s);
  }

  const maxRank = list.reduce((m, h) => Math.max(m, Number(h.rank) || 0), 0);
  const rightClip = {
    ...JSON.parse(JSON.stringify(target)),
    rank: maxRank + 1,
    start_sec: curT,
    end_sec: ed,
    start_str: formatClock(curT),
    end_str: formatClock(ed),
    duration_sec: Number((ed - curT).toFixed(2)),
    title_zh: `${target.title_zh || ""} (下)`.trim(),
    subtitles: rightSubs,
  };

  target.end_sec = curT;
  target.end_str = formatClock(curT);
  target.duration_sec = Number((curT - st).toFixed(2));
  target.subtitles = leftSubs;

  const idx = list.indexOf(target);
  list.splice(idx + 1, 0, rightClip);
  state.initialClipBounds.set(rightClip.rank, { start_sec: rightClip.start_sec, end_sec: rightClip.end_sec });

  renderHighlightsList();
  renderActiveSubtitlesPane();
  renderClipInspector();
  syncMonitorClipContext();
  recordHistory(`分割高光 #${target.rank} → #${rightClip.rank}`, "trim");
  scheduleAutoSave();
  setStatusMessage(`已在 ${formatTimecode(curT, true)} 拆分为 #${target.rank} 与 #${rightClip.rank}`);
  return true;
}

function toggleActiveClipEnabled() {
  if (!state.highlightsData) return;
  const target = getClipByRank(state.previewSegment?.rank ?? state.activeClipRank);
  if (!target) return;
  target.selected = target.selected === false;
  if (state.onlySelected) renderHighlightsList();
  else syncHighlightRowStates();
  renderClipInspector();
  recordHistory(`${target.selected ? "启用" : "禁用"}导出 #${target.rank}`, "select");
  scheduleAutoSave();
  setStatusMessage(`#${target.rank} ${target.selected ? "已启用导出" : "已禁用导出"}`);
}

function exportClipsIndexCsv() {
  const list = getDisplayHighlights();
  if (!list.length) {
    showToast("当前无高光可导出索引", true);
    return;
  }
  const esc = (v) => `"${String(v ?? "").replace(/"/g, '""')}"`;
  const rows = [["Rank", "Selected", "Score", "Start", "End", "Duration(s)", "Title_ZH", "Title_EN", "Subtitles", "Summary"]];
  for (const h of list) {
    const dur = Math.max(0, Number(h.end_sec || 0) - Number(h.start_sec || 0));
    rows.push([
      h.rank,
      h.selected !== false ? "YES" : "NO",
      Number(h.score || 0).toFixed(1),
      formatClock(h.start_sec),
      formatClock(h.end_sec),
      dur.toFixed(2),
      h.title_zh || "",
      h.title_en || "",
      Array.isArray(h.subtitles) ? h.subtitles.length : 0,
      h.summary_zh || "",
    ]);
  }
  const csv = "\ufeff" + rows.map((r) => r.map(esc).join(",")).join("\r\n");
  const blob = new Blob([csv], { type: "text/csv;charset=utf-8;" });
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = `${state.activeVideo?.stem || "solacut"}_edit_index.csv`;
  document.body.appendChild(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
  setStatusMessage(`已导出索引表 (${list.length} 条)`);
}

/* ==================== 字幕全文查找与替换（跨全部高光 + 原子撤销） ==================== */
function openSubFindBar(focusReplace = false) {
  dock?.focusPanel("subtitles");
  const bar = el("sub-find-bar");
  const btn = el("btn-sub-find-toggle");
  if (!bar) return;
  bar.classList.remove("hidden");
  btn?.setAttribute("aria-pressed", "true");
  const targetInput = focusReplace ? el("sub-replace-input") : el("sub-find-input");
  targetInput?.focus();
  targetInput?.select();
  runSubFind(false);
}

function closeSubFindBar() {
  const bar = el("sub-find-bar");
  const btn = el("btn-sub-find-toggle");
  bar?.classList.add("hidden");
  btn?.setAttribute("aria-pressed", "false");
  state.subFindMatches = [];
  state.subFindCursor = -1;
  document.querySelectorAll(".sub-editor-row.find-hit").forEach((n) => n.classList.remove("find-hit"));
}

function runSubFind(jumpToFirst = false) {
  const qRaw = (el("sub-find-input")?.value || "").trim();
  const countEl = el("sub-find-count");
  state.subFindMatches = [];
  state.subFindCursor = -1;
  document.querySelectorAll(".sub-editor-row.find-hit").forEach((n) => n.classList.remove("find-hit"));

  if (!qRaw || !state.highlightsData?.highlights) {
    if (countEl) countEl.textContent = "0/0";
    return;
  }
  const q = qRaw.toLowerCase();
  for (const h of state.highlightsData.highlights) {
    const subs = Array.isArray(h.subtitles) ? h.subtitles : [];
    subs.forEach((s, idx) => {
      const zh = String(s.text_zh || "").toLowerCase();
      const en = String(s.text_en || "").toLowerCase();
      if (zh.includes(q) || en.includes(q)) {
        state.subFindMatches.push({ rank: Number(h.rank), subIndex: idx });
      }
    });
  }
  if (countEl) {
    countEl.textContent = state.subFindMatches.length > 0 ? `1/${state.subFindMatches.length}` : "0/0";
  }
  if (jumpToFirst && state.subFindMatches.length > 0) {
    stepSubFindMatch(1);
  }
}

function stepSubFindMatch(delta) {
  const total = state.subFindMatches.length;
  const countEl = el("sub-find-count");
  if (!total) {
    if (countEl) countEl.textContent = "0/0";
    return;
  }
  if (state.subFindCursor < 0) state.subFindCursor = 0;
  else state.subFindCursor = (state.subFindCursor + delta + total) % total;

  const hit = state.subFindMatches[state.subFindCursor];
  if (countEl) countEl.textContent = `${state.subFindCursor + 1}/${total}`;

  const clip = getClipByRank(hit.rank);
  if (!clip) return;
  if (Number(state.activeClipRank) !== hit.rank) {
    selectActiveClip(clip, false);
  }
  document.querySelectorAll(".sub-editor-row.find-hit").forEach((n) => n.classList.remove("find-hit"));
  const row = document.querySelector(`.sub-editor-row[data-sub-key="${hit.rank}:${hit.subIndex}"]`);
  if (row) {
    row.classList.add("find-hit");
    row.scrollIntoView({ block: "center", behavior: "smooth" });
  }
}

function replaceTextCaseInsensitive(src, findStr, repStr) {
  if (!src || !findStr) return { text: src, count: 0 };
  const escaped = findStr.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  const re = new RegExp(escaped, "gi");
  let count = 0;
  const text = String(src).replace(re, () => {
    count += 1;
    return repStr;
  });
  return { text, count };
}

function replaceCurrentSubMatch() {
  const findStr = (el("sub-find-input")?.value || "").trim();
  const repStr = el("sub-replace-input")?.value ?? "";
  if (!findStr || !state.subFindMatches.length) return;
  if (state.subFindCursor < 0) state.subFindCursor = 0;
  const hit = state.subFindMatches[state.subFindCursor];
  const clip = getClipByRank(hit.rank);
  const sub = clip?.subtitles?.[hit.subIndex];
  if (!sub) return;
  const rZh = replaceTextCaseInsensitive(sub.text_zh || "", findStr, repStr);
  const rEn = replaceTextCaseInsensitive(sub.text_en || "", findStr, repStr);
  const total = rZh.count + rEn.count;
  if (total === 0) return;
  sub.text_zh = rZh.text;
  sub.text_en = rEn.text;
  renderActiveSubtitlesPane();
  syncVideoSubtitleOverlay();
  syncTimelineData();
  recordHistory(`替换字幕 "${findStr}" → "${repStr}"`, "sub");
  scheduleAutoSave();
  runSubFind(true);
  setStatusMessage(`已替换 ${total} 处`);
}

function replaceAllSubMatches() {
  const findStr = (el("sub-find-input")?.value || "").trim();
  const repStr = el("sub-replace-input")?.value ?? "";
  if (!findStr || !state.highlightsData?.highlights) return;
  let total = 0;
  for (const h of state.highlightsData.highlights) {
    for (const s of h.subtitles || []) {
      const rZh = replaceTextCaseInsensitive(s.text_zh || "", findStr, repStr);
      const rEn = replaceTextCaseInsensitive(s.text_en || "", findStr, repStr);
      if (rZh.count > 0) s.text_zh = rZh.text;
      if (rEn.count > 0) s.text_en = rEn.text;
      total += rZh.count + rEn.count;
    }
  }
  if (total === 0) {
    setStatusMessage(`未找到 "${findStr}"`, true);
    return;
  }
  renderActiveSubtitlesPane();
  syncVideoSubtitleOverlay();
  syncTimelineData();
  recordHistory(`批量替换字幕 "${findStr}" → "${repStr}" (${total}处)`, "sub");
  scheduleAutoSave();
  runSubFind(false);
  setStatusMessage(`已全部替换 ${total} 处（可 Ctrl+Z 撤销）`);
}

/* ==================== 原生级顶部 10 组达芬奇应用菜单栏 ==================== */
function initAppMenuBar() {
  const container = el("app-menubar");
  if (!container) return;

  const hasClip = () => Boolean(getClipByRank(state.previewSegment?.rank ?? state.activeClipRank));
  const hasVideo = () => Boolean(el("review-video")?.src);
  const histSnap = () => historyMgr?.getState() || { canUndo: false, canRedo: false, cursor: -1 };
  const dockSnap = () => dock?.getStateSnapshot() || { openPanels: [], allPanels: [], preset: "edit" };

  appMenuBar = new NleAppMenuBar(container, [
    {
      id: "file",
      label: "文件",
      getItems: () => [
        { label: "刷新素材目录", onSelect: () => persistDirsAndScan() },
        {
          label: "切换输入 / 输出目录...",
          onSelect: () => {
            const btn = el("btn-dirs-popover");
            btn?.click();
          },
        },
        { type: "separator" },
        {
          label: "立即保存审片与字幕",
          hint: "Ctrl+S",
          disabled: !state.highlightsData,
          onSelect: () => saveHighlightsNow(false),
        },
        {
          label: "导出高光编辑索引 (.csv)",
          disabled: !state.highlightsData,
          onSelect: () => exportClipsIndexCsv(),
        },
        {
          label: "导出已选切片",
          hint: "Ctrl+E",
          disabled: !state.highlightsData,
          onSelect: () => triggerExport(null),
        },
        {
          label: "打开输出目录",
          onSelect: () => el("btn-open-clips-dir")?.click(),
        },
        { type: "separator" },
        {
          label: "接口与外观设置...",
          hint: "Ctrl+,",
          onSelect: () => el("settings-dialog")?.showModal?.(),
        },
      ],
    },
    {
      id: "edit",
      label: "编辑",
      getItems: () => {
        const hs = histSnap();
        return [
          { label: "撤销", hint: "Ctrl+Z", disabled: !hs.canUndo, onSelect: () => performUndo() },
          { label: "重做", hint: "Ctrl+Shift+Z", disabled: !hs.canRedo, onSelect: () => performRedo() },
          { label: "操作历史记录", hint: "Ctrl+H", onSelect: () => dock?.focusPanel("history") },
          {
            label: "回到初始版本 (v0)",
            disabled: hs.cursor <= 0,
            onSelect: () => performJumpHistory(0),
          },
          { type: "separator" },
          {
            label: "全选高光",
            disabled: !state.highlightsData,
            onSelect: () => el("btn-hl-select-all")?.click(),
          },
          {
            label: "清空高光勾选",
            disabled: !state.highlightsData,
            onSelect: () => el("btn-hl-unselect-all")?.click(),
          },
          { type: "separator" },
          {
            label: "在字幕中查找与替换...",
            hint: "Ctrl+F",
            disabled: !state.highlightsData,
            onSelect: () => openSubFindBar(false),
          },
        ];
      },
    },
    {
      id: "trim",
      label: "修剪",
      getItems: () => [
        {
          label: "选择 / 拖边修剪模式",
          hint: "V",
          checked: timelineEngine?.tool === "select",
          onSelect: () => timelineEngine?.setTool("select"),
        },
        {
          label: "剃刀拆字幕模式",
          hint: "B",
          checked: timelineEngine?.tool === "razor",
          onSelect: () => timelineEngine?.setTool("razor"),
        },
        { type: "separator" },
        {
          label: "裁头到播放头 (Trim Start)",
          hint: "Shift+[",
          disabled: !hasClip(),
          onSelect: () => trimClipToPlayhead("start"),
        },
        {
          label: "裁尾到播放头 (Trim End)",
          hint: "Shift+]",
          disabled: !hasClip(),
          onSelect: () => trimClipToPlayhead("end"),
        },
      ],
    },
    {
      id: "timeline",
      label: "时间线",
      getItems: () => [
        {
          label: "在播放头拆分字幕",
          hint: "C / Ctrl+\\",
          disabled: !state.highlightsData,
          onSelect: () => splitSubtitleAtPlayhead(),
        },
        {
          label: "在播放头分割高光切片",
          hint: "Ctrl+Shift+\\",
          disabled: !hasClip(),
          onSelect: () => splitActiveClipAtPlayhead(),
        },
        {
          label: "在播放头插入新字幕",
          hint: "Alt+N",
          disabled: !hasClip(),
          onSelect: () => insertSubtitleAtPlayhead(),
        },
        { type: "separator" },
        {
          label: "磁吸吸附 (Snapping)",
          hint: "N",
          checked: Boolean(timelineEngine?.snapEnabled),
          onSelect: () => timelineEngine?.setSnap(!timelineEngine.snapEnabled),
        },
        {
          label: "全高正向波形 (Full Waveform)",
          checked: Boolean(timelineEngine?.viewOptions?.fullWaveform),
          onSelect: () => {
            const next = !timelineEngine?.viewOptions?.fullWaveform;
            if (el("chk-tl-opt-full-wave")) el("chk-tl-opt-full-wave").checked = next;
            timelineEngine?.setViewOptions({ fullWaveform: next });
          },
        },
        { type: "separator" },
        { label: "适配全片视野", hint: "Shift+Z", onSelect: () => timelineEngine?.fitAll() },
        {
          label: "聚焦当前高光片段",
          disabled: !hasClip(),
          onSelect: () => el("btn-tl-fit-clip")?.click(),
        },
        { label: "放大时间线", hint: "=", onSelect: () => timelineEngine?.zoomByFactor(0.65) },
        { label: "缩小时间线", hint: "-", onSelect: () => timelineEngine?.zoomByFactor(1.5) },
      ],
    },
    {
      id: "clip",
      label: "片段",
      getItems: () => {
        const cur = getClipByRank(state.previewSegment?.rank ?? state.activeClipRank);
        return [
          {
            label: "启用 / 禁用导出",
            hint: "D / S",
            checked: Boolean(cur && cur.selected !== false),
            disabled: !cur,
            onSelect: () => toggleActiveClipEnabled(),
          },
          {
            label: "播放预览当前段",
            hint: "Enter",
            disabled: !cur,
            onSelect: () => cur && previewHighlightSegment(cur),
          },
          { type: "separator" },
          {
            label: "定点重翻当前双语字幕...",
            disabled: !cur,
            onSelect: () => cur && openRetranslateDialog([cur.rank], cur.title_zh),
          },
          {
            label: "单独导出当前切片",
            disabled: !cur,
            onSelect: () => cur && triggerExport([cur.rank]),
          },
          { type: "separator" },
          {
            label: "还原 AI 初始入出点",
            hint: "Alt+X",
            disabled: !cur,
            onSelect: () => resetClipBoundary("both"),
          },
        ];
      },
    },
    {
      id: "mark",
      label: "标记",
      getItems: () => [
        { label: "设为入点 (Mark In)", hint: "I", disabled: !hasClip(), onSelect: () => markPreviewBoundary("in") },
        { label: "设为出点 (Mark Out)", hint: "O", disabled: !hasClip(), onSelect: () => markPreviewBoundary("out") },
        { type: "separator" },
        { label: "还原入点 (Clear In)", hint: "Alt+I", disabled: !hasClip(), onSelect: () => resetClipBoundary("in") },
        { label: "还原出点 (Clear Out)", hint: "Alt+O", disabled: !hasClip(), onSelect: () => resetClipBoundary("out") },
        { label: "还原出入点 (Clear I/O)", hint: "Alt+X", disabled: !hasClip(), onSelect: () => resetClipBoundary("both") },
        { type: "separator" },
        {
          label: "跳至入点 (Go to In)",
          hint: "Shift+I",
          disabled: !hasClip(),
          onSelect: () => {
            const cur = getClipByRank(state.previewSegment?.rank ?? state.activeClipRank);
            if (cur && el("review-video")?.src) {
              stopShuttle(true);
              el("review-video").currentTime = Number(cur.start_sec) || 0;
              monitorEngine?.refreshFrameUI();
            }
          },
        },
        {
          label: "跳至出点 (Go to Out)",
          hint: "Shift+O",
          disabled: !hasClip(),
          onSelect: () => el("btn-jump-clip-end")?.click(),
        },
      ],
    },
    {
      id: "view",
      label: "视图",
      getItems: () => [
        {
          label: "影院全屏预览 (Cinema Viewer)",
          hint: "P",
          checked: Boolean(monitorEngine?.cinemaActive),
          disabled: !hasVideo(),
          onSelect: () => monitorEngine?.toggleCinemaMode(),
        },
        {
          label: "无边框全屏窗口",
          hint: "F11",
          checked: Boolean(desktopState.fullscreen),
          onSelect: () => toggleAppFullscreen(),
        },
        { type: "separator" },
        {
          label: "监视器缩放：适配 (Fit)",
          checked: monitorEngine?.zoomMode === "fit",
          onSelect: () => monitorEngine?.setZoomMode("fit"),
        },
        {
          label: "监视器缩放：100%",
          checked: monitorEngine?.zoomMode === "100",
          onSelect: () => monitorEngine?.setZoomMode("100"),
        },
        {
          label: "监视器缩放：150%",
          checked: monitorEngine?.zoomMode === "150",
          onSelect: () => monitorEngine?.setZoomMode("150"),
        },
        { type: "separator" },
        {
          label: "安全框：9:16 竖屏裁切",
          checked: Boolean(monitorEngine?.showSafe916 && monitorEngine?.safeMode === "9:16"),
          onSelect: () =>
            monitorEngine?.setSafeMode("9:16", !(monitorEngine.showSafe916 && monitorEngine.safeMode === "9:16")),
        },
        {
          label: "安全框：4:5 竖屏",
          checked: Boolean(monitorEngine?.showSafe916 && monitorEngine?.safeMode === "4:5"),
          onSelect: () =>
            monitorEngine?.setSafeMode("4:5", !(monitorEngine.showSafe916 && monitorEngine.safeMode === "4:5")),
        },
        {
          label: "安全框：动作 / 标题边界",
          checked: Boolean(monitorEngine?.showSafe916 && monitorEngine?.safeMode === "title"),
          onSelect: () =>
            monitorEngine?.setSafeMode("title", !(monitorEngine.showSafe916 && monitorEngine.safeMode === "title")),
        },
        {
          label: "关闭安全框",
          checked: !monitorEngine?.showSafe916,
          onSelect: () => monitorEngine?.setSafeMode("off", false),
        },
      ],
    },
    {
      id: "playback",
      label: "播放",
      getItems: () => [
        {
          label: "播放 / 暂停",
          hint: "Space",
          disabled: !hasVideo(),
          onSelect: () => el("btn-play-toggle")?.click(),
        },
        {
          label: "从入点播到出点",
          hint: "Alt+/",
          disabled: !hasClip(),
          onSelect: () => {
            const cur = getClipByRank(state.previewSegment?.rank ?? state.activeClipRank);
            if (cur) previewHighlightSegment(cur);
          },
        },
        {
          label: "循环播放当前片段",
          hint: "Ctrl+/ · M",
          checked: Boolean(el("chk-loop-segment")?.checked),
          onSelect: () => {
            const chk = el("chk-loop-segment");
            if (chk) {
              chk.checked = !chk.checked;
              setStatusMessage(chk.checked ? "已开启片段循环" : "已关闭片段循环");
            }
          },
        },
        { type: "separator" },
        { label: "正向加速梭巡", hint: "L", disabled: !hasVideo(), onSelect: () => triggerShuttle(1, false) },
        { label: "急停", hint: "K", disabled: !hasVideo(), onSelect: () => { stopShuttle(true); el("review-video")?.pause(); } },
        { label: "反向加速梭巡", hint: "J", disabled: !hasVideo(), onSelect: () => triggerShuttle(-1, false) },
        { type: "separator" },
        { label: "上一帧 / 下一帧", hint: "← / →", disabled: !hasVideo(), onSelect: () => stepVideoFrames(1) },
        {
          label: "监听衰减 -20dB (DIM)",
          checked: Boolean(monitorEngine?.dimmed),
          onSelect: () => {
            const on = monitorEngine?.toggleDim();
            setStatusMessage(on ? "监听衰减 DIM (-20dB)：开" : "监听衰减 DIM：关");
          },
        },
      ],
    },
    {
      id: "workspace",
      label: "工作区",
      getItems: () => {
        const snap = dockSnap();
        const openSet = new Set(snap.openPanels || []);
        const panelItems = (snap.allPanels || []).map((p) => ({
          label: `面板：${p.title}`,
          checked: openSet.has(p.id),
          disabled: openSet.has(p.id) && openSet.size <= 1,
          onSelect: () => dock?.togglePanelVisibility(p.id),
        }));
        return [
          { label: "剪辑工作区", hint: "Shift+1", checked: snap.preset === "edit", onSelect: () => dock?.setPreset("edit") },
          { label: "审片工作区", hint: "Shift+2", checked: snap.preset === "review", onSelect: () => dock?.setPreset("review") },
          {
            label: "字幕工作区",
            hint: "Shift+3",
            checked: snap.preset === "subtitle",
            onSelect: () => {
              dock?.setPreset("subtitle");
              ensureActiveClipForSubtitles();
              renderActiveSubtitlesPane();
            },
          },
          {
            label: "交付工作区",
            hint: "Shift+4",
            checked: snap.preset === "deliver",
            onSelect: () => {
              dock?.setPreset("deliver");
              switchInspectorTab("export");
            },
          },
          { type: "separator" },
          ...panelItems,
          { type: "separator" },
          { label: "最大化 / 还原当前面板", hint: "`", onSelect: () => dock?.toggleMaximize() },
          { label: "重置当前工作区布局", onSelect: () => dock?.resetCurrentPreset() },
        ];
      },
    },
    {
      id: "help",
      label: "帮助",
      getItems: () => [
        {
          label: "快捷键速查表",
          hint: "?",
          onSelect: () => el("shortcuts-dialog")?.showModal?.(),
        },
      ],
    },
  ]);
}

/* ==================== Step C：双层多轨 Canvas 时间线初始化与全链路联动 ==================== */
function syncTimelineToolButtons(tool) {
  el("btn-tl-tool-select")?.classList.toggle("active", tool === "select");
  el("btn-tl-tool-razor")?.classList.toggle("active", tool === "razor");
}

function initTimelineEngine() {
  const videoEl = el("review-video");
  timelineEngine = new DualTimelineEngine({
    overviewCanvas: el("tl-overview-canvas"),
    tracksCanvas: el("tl-tracks-canvas"),
    zoomSlider: el("tl-zoom-slider"),
    viewSpanLabel: el("tl-view-span"),
    onSeek: (sec) => {
      if (!videoEl || !videoEl.src) return;
      stopShuttle(true);
      videoEl.currentTime = clampNum(sec, 0, videoEl.duration || sec);
      monitorEngine?.refreshFrameUI();
    },
    onSelectClip: (clip) => {
      if (Number(state.activeClipRank) !== Number(clip.rank)) {
        selectActiveClip(clip, false);
      }
    },
    onPreviewClip: (clip) => {
      selectActiveClip(clip, true);
    },
    onTrimClipLive: (clip, nextStart, nextEnd, edge) => {
      clip.start_sec = nextStart;
      clip.end_sec = nextEnd;
      clip.start_str = formatClock(nextStart);
      clip.end_str = formatClock(nextEnd);
      clip.duration_sec = Number(Math.max(0, nextEnd - nextStart).toFixed(2));
      if (state.previewSegment && Number(state.previewSegment.rank) === Number(clip.rank)) {
        state.previewSegment.start_sec = nextStart;
        state.previewSegment.end_sec = nextEnd;
      }
      // PR/达芬奇级体验：拖边 Trim 时监视器画面实时跟随到当前修剪帧
      if (videoEl && videoEl.src) {
        videoEl.pause();
        videoEl.currentTime = edge === "left" ? nextStart : nextEnd;
        monitorEngine?.refreshFrameUI();
      }
      if (Number(state.activeClipRank) === Number(clip.rank)) {
        clipStartScrubby?.setValue(nextStart);
        clipEndScrubby?.setValue(nextEnd);
        const durEl = el("clip-prop-dur");
        if (durEl) durEl.textContent = `${clip.duration_sec.toFixed(1)}s`;
      }
      syncHighlightRowStates();
      monitorEngine?.setActiveClip(getClipByRank(state.previewSegment?.rank ?? state.activeClipRank));
    },
    onTrimClipCommit: (clip, edge) => {
      const val = edge === "left" ? clip.start_sec : clip.end_sec;
      renderActiveSubtitlesPane();
      recordHistory(
        `时间线修剪 #${clip.rank} ${edge === "left" ? "入点" : "出点"} ${formatTimecode(val, true)}`,
        "trim"
      );
      scheduleAutoSave();
      setStatusMessage(`#${clip.rank} ${edge === "left" ? "入点" : "出点"} → ${formatTimecode(val, true)}`);
    },
    onSelectSubtitle: (clip, sub) => {
      selectActiveClip(clip, false);
      jumpToSubtitleLine(clip, sub);
    },
    onTrimSubtitleLive: (clip, sub, subIndex, nextStart, nextEnd, edge) => {
      sub.start_sec = nextStart;
      sub.end_sec = nextEnd;
      if (videoEl && videoEl.src) {
        videoEl.pause();
        videoEl.currentTime = edge === "left" ? nextStart : nextEnd;
        monitorEngine?.refreshFrameUI();
      }
      syncVideoSubtitleOverlay();
    },
    onTrimSubtitleCommit: (clip, sub, subIndex, edge) => {
      if (Number(state.activeClipRank) === Number(clip.rank)) {
        renderActiveSubtitlesPane();
      }
      syncMonitorClipContext();
      recordHistory(
        `时间线调 #${clip.rank} 第${subIndex + 1}句${edge === "left" ? "起点" : "终点"}`,
        "sub"
      );
      scheduleAutoSave();
    },
    onRazorSubtitle: (clip, subIndex, sec) => {
      splitSubtitleLineAtTime(clip, subIndex, sec);
    },
    onClipContextMenu: (e, clip) => {
      selectActiveClip(clip, false);
      ctxMenu?.open(e.clientX, e.clientY, [
        { label: "播放预览本段", hint: "双击", onSelect: () => previewHighlightSegment(clip) },
        { label: "时间线聚焦本段", onSelect: () => timelineEngine?.fitClip(clip) },
        {
          label: "启用 / 禁用导出",
          hint: "D / S",
          checked: clip.selected !== false,
          onSelect: () => {
            clip.selected = clip.selected === false;
            if (state.onlySelected) renderHighlightsList();
            else syncHighlightRowStates();
            renderClipInspector();
            syncTimelineData();
            recordHistory(`${clip.selected ? "勾选" : "取消勾选"} #${clip.rank}`, "select");
            scheduleAutoSave();
          },
        },
        { type: "separator" },
        { label: "裁头到播放头", hint: "Shift+[", onSelect: () => trimClipToPlayhead("start") },
        { label: "裁尾到播放头", hint: "Shift+]", onSelect: () => trimClipToPlayhead("end") },
        { label: "在播放头分割切片", hint: "Ctrl+Shift+\\", onSelect: () => splitActiveClipAtPlayhead() },
        { label: "还原 AI 初始入出点", hint: "Alt+X", onSelect: () => resetClipBoundary("both") },
        { type: "separator" },
        { label: "定点重翻本条双语", onSelect: () => openRetranslateDialog([clip.rank], clip.title_zh) },
        { label: "单独导出本条切片", onSelect: () => triggerExport([clip.rank]) },
      ]);
    },
    onSubtitleContextMenu: (e, clip, sub, subIndex) => {
      selectActiveClip(clip, false);
      ctxMenu?.open(e.clientX, e.clientY, [
        { label: "跳播本句", onSelect: () => jumpToSubtitleLine(clip, sub) },
        {
          label: "在此句中点拆分",
          onSelect: () => splitSubtitleLineAtTime(clip, subIndex, null),
        },
        {
          label: "在字幕面板编辑",
          onSelect: () => dock?.focusPanel("subtitles"),
        },
        { type: "separator" },
        {
          label: "删除本句",
          danger: true,
          onSelect: () => {
            clip.subtitles.splice(subIndex, 1);
            renderActiveSubtitlesPane();
            syncHighlightRowStates();
            syncVideoSubtitleOverlay();
            recordHistory(`删除 #${clip.rank} 第${subIndex + 1}句`, "sub");
            scheduleAutoSave();
          },
        },
      ]);
    },
    onRequestWaveform: async (stem, start, end, points) => {
      const outDir = encodeURIComponent(el("output-dir-field").value.trim());
      return apiFetch(
        `/api/waveform?stem=${encodeURIComponent(stem)}&output_dir=${outDir}&start=${start.toFixed(2)}&end=${end.toFixed(2)}&points=${points}`
      );
    },
    onToolChange: (tool) => syncTimelineToolButtons(tool),
    onSnapChange: (enabled) => {
      const chk = el("chk-tl-snap");
      if (chk) chk.checked = enabled;
    },
  });

  // 工具栏、视图选项 Popover 与轨道头事件绑定
  el("btn-tl-tool-select")?.addEventListener("click", () => timelineEngine?.setTool("select"));
  el("btn-tl-tool-razor")?.addEventListener("click", () => timelineEngine?.setTool("razor"));
  el("chk-tl-snap")?.addEventListener("change", (e) => timelineEngine?.setSnap(e.target.checked));
  el("btn-tl-split-playhead")?.addEventListener("click", () => {
    const videoElCur = el("review-video");
    const hit = videoElCur ? findSubtitleAtTime(Number(videoElCur.currentTime || 0)) : null;
    if (hit) splitSubtitleAtPlayhead();
    else splitActiveClipAtPlayhead();
  });
  el("chk-tl-opt-full-wave")?.addEventListener("change", (e) =>
    timelineEngine?.setViewOptions({ fullWaveform: e.target.checked })
  );
  el("chk-tl-opt-clip-names")?.addEventListener("change", (e) =>
    timelineEngine?.setViewOptions({ showClipNames: e.target.checked })
  );
  el("chk-tl-opt-a1-vis")?.addEventListener("change", (e) =>
    timelineEngine?.setTrackState({ a1Visible: e.target.checked })
  );
  document.querySelectorAll(".tl-height-seg [data-tl-height]").forEach((btn) => {
    btn.addEventListener("click", () => {
      document.querySelectorAll(".tl-height-seg [data-tl-height]").forEach((b) => b.classList.remove("active"));
      btn.classList.add("active");
      timelineEngine?.setViewOptions({ heightPreset: btn.dataset.tlHeight || "normal" });
    });
  });
  el("btn-tl-fit-all")?.addEventListener("click", () => timelineEngine?.fitAll());
  el("btn-tl-fit-clip")?.addEventListener("click", () => {
    const cur = getClipByRank(state.previewSegment?.rank ?? state.activeClipRank);
    if (cur) timelineEngine?.fitClip(cur);
    else setStatusMessage("请先选中一条高光片段", true);
  });
  el("btn-tl-zoom-in")?.addEventListener("click", () => timelineEngine?.zoomByFactor(0.65));
  el("btn-tl-zoom-out")?.addEventListener("click", () => timelineEngine?.zoomByFactor(1.5));

  el("chk-tl-v1-lock")?.addEventListener("change", (e) =>
    timelineEngine?.setTrackState({ v1Locked: e.target.checked })
  );
  el("chk-tl-v1-vis")?.addEventListener("change", (e) =>
    timelineEngine?.setTrackState({ v1Visible: e.target.checked })
  );
  el("chk-tl-t1-lock")?.addEventListener("change", (e) =>
    timelineEngine?.setTrackState({ t1Locked: e.target.checked })
  );
  el("chk-tl-t1-vis")?.addEventListener("change", (e) => {
    const vis = e.target.checked;
    timelineEngine?.setTrackState({ t1Visible: vis });
    const subSel = el("sel-overlay-sub-mode");
    if (subSel) {
      if (!vis) {
        state.overlaySubMode = "off";
        subSel.value = "off";
      } else if (state.overlaySubMode === "off") {
        state.overlaySubMode = "bilingual";
        subSel.value = "bilingual";
      }
      syncVideoSubtitleOverlay();
    }
  });

  const syncA1MuteBtn = () => {
    const muted = Boolean(videoEl?.muted || (videoEl?.volume ?? 1) <= 0.001);
    const btn = el("btn-tl-a1-mute");
    if (btn) btn.dataset.active = muted ? "true" : "false";
    timelineEngine?.setAudioMuted(muted);
  };
  el("btn-tl-a1-mute")?.addEventListener("click", () => {
    el("btn-monitor-mute")?.click();
  });
  videoEl?.addEventListener("volumechange", syncA1MuteBtn);
  videoEl?.addEventListener("loadedmetadata", () => {
    if (state.activeVideo && videoEl.duration) {
      timelineEngine?.setMedia({
        stem: state.activeVideo.stem,
        duration: videoEl.duration,
        fps: state.videoFps,
        resetView: false,
      });
    }
  });
  syncA1MuteBtn();
}

function isTextInputElement(node) {
  if (!node) return false;
  if (node.isContentEditable) return true;
  const tag = node.tagName ? node.tagName.toLowerCase() : "";
  if (tag === "textarea" || tag === "select") return true;
  if (tag === "input") {
    const t = (node.type || "text").toLowerCase();
    return !["checkbox", "radio", "range", "button", "submit", "reset"].includes(t);
  }
  return false;
}

function initPlayerEvents() {
  const videoEl = el("review-video");

  // 初始化自研 60fps 专业节目监视器 + 实时 L/R dBFS 电平表 + 局部微型进度槽 + 多比例安全框 + DIM + 影院全屏
  monitorEngine = new ProgramMonitorEngine({
    videoEl,
    frameEl: el("monitor-video-frame"),
    meterCanvas: el("monitor-audio-meter"),
    scrubberCanvas: el("monitor-clip-scrubber"),
    timecodeBtn: el("hud-time-display"),
    durationEl: el("hud-duration-display"),
    techBadgeEl: el("hud-tech-badge"),
    safeGuideEl: el("safe-guide-916"),
    zoomSelectEl: el("sel-monitor-zoom"),
    safeToggleEl: el("chk-safe-916"),
    safeModeSelectEl: el("sel-safe-mode"),
    playToggleBtn: el("btn-play-toggle"),
    muteBtn: el("btn-monitor-mute"),
    dimBtn: el("btn-monitor-dim"),
    cinemaBtn: el("btn-monitor-cinema"),
    volumeSlider: el("monitor-volume-slider"),
    onFrameTick: (curSec) => {
      syncVideoSubtitleOverlay();
      timelineEngine?.setPlayhead(curSec);
      timelineEngine?.setActiveSubKey(state.activeSubKey);
      // 修复 P0-03：正在拖动 Scrubber 时不与自动循环/到点暂停打架回弹
      if (monitorEngine?.isScrubbing) return;
      if (
        state.previewSegment &&
        !videoEl.paused &&
        curSec >= state.previewSegment.end_sec
      ) {
        if (el("chk-loop-segment")?.checked) {
          videoEl.currentTime = state.previewSegment.start_sec;
        } else if (el("chk-auto-pause-end")?.checked) {
          stopShuttle(true);
          videoEl.pause();
        }
      }
    },
    onSeekRequest: (sec) => {
      stopShuttle(true);
      // 修复 P0-03：用户手动把进度头拖到当前预览段出点之外（微调 O 点看尾部上下文）时，解除自动回弹/掐停锁
      if (
        state.previewSegment &&
        (sec > state.previewSegment.end_sec + 0.05 || sec < state.previewSegment.start_sec - 0.05)
      ) {
        state.previewSegment = null;
      }
    },
    onStageContextMenu: (e) => {
      const hasVid = Boolean(videoEl?.src);
      const cur = getClipByRank(state.previewSegment?.rank ?? state.activeClipRank);
      ctxMenu?.open(e.clientX, e.clientY, [
        {
          label: videoEl.paused ? "播放" : "暂停",
          hint: "Space",
          disabled: !hasVid,
          onSelect: () => el("btn-play-toggle")?.click(),
        },
        {
          label: "从入点播到出点",
          hint: "Alt+/",
          disabled: !cur,
          onSelect: () => cur && previewHighlightSegment(cur),
        },
        {
          label: "循环当前片段",
          hint: "Ctrl+/ · M",
          checked: Boolean(el("chk-loop-segment")?.checked),
          onSelect: () => {
            const c = el("chk-loop-segment");
            if (c) c.checked = !c.checked;
          },
        },
        { type: "separator" },
        { label: "设为入点", hint: "I", disabled: !cur, onSelect: () => markPreviewBoundary("in") },
        { label: "设为出点", hint: "O", disabled: !cur, onSelect: () => markPreviewBoundary("out") },
        { label: "还原初始入出点", hint: "Alt+X", disabled: !cur, onSelect: () => resetClipBoundary("both") },
        { type: "separator" },
        {
          label: "9:16 竖屏安全框",
          checked: Boolean(monitorEngine?.showSafe916 && monitorEngine?.safeMode === "9:16"),
          onSelect: () =>
            monitorEngine?.setSafeMode("9:16", !(monitorEngine.showSafe916 && monitorEngine.safeMode === "9:16")),
        },
        {
          label: "动作 / 标题安全框",
          checked: Boolean(monitorEngine?.showSafe916 && monitorEngine?.safeMode === "title"),
          onSelect: () =>
            monitorEngine?.setSafeMode("title", !(monitorEngine.showSafe916 && monitorEngine.safeMode === "title")),
        },
        {
          label: "影院全屏预览",
          hint: "P",
          checked: Boolean(monitorEngine?.cinemaActive),
          disabled: !hasVid,
          onSelect: () => monitorEngine?.toggleCinemaMode(),
        },
      ]);
    },
    onTimecodeContextMenu: (e, tcStr) => {
      ctxMenu?.open(e.clientX, e.clientY, [
        {
          label: `复制时间码 (${tcStr})`,
          onSelect: async () => {
            try {
              await navigator.clipboard.writeText(tcStr);
              setStatusMessage(`已复制时码：${tcStr}`);
            } catch {
              setStatusMessage(`当前时码：${tcStr}`);
            }
          },
        },
        {
          label: "粘贴时间码并跳帧",
          disabled: !videoEl?.src,
          onSelect: async () => {
            try {
              const raw = await navigator.clipboard.readText();
              const sec = parseSmpteOrTimecode(raw, state.videoFps);
              if (sec !== null && !Number.isNaN(sec)) {
                stopShuttle(true);
                videoEl.currentTime = clampNum(sec, 0, videoEl.duration || sec);
                monitorEngine?.refreshFrameUI();
                setStatusMessage(`已跳转至 ${formatSmpte(videoEl.currentTime, state.videoFps)}`);
              } else {
                setStatusMessage("剪贴板不是合法时间码", true);
              }
            } catch {
              setStatusMessage("无法读取剪贴板", true);
            }
          },
        },
      ]);
    },
  });

  videoEl.addEventListener("ended", () => stopShuttle(true));
  videoEl.addEventListener("error", () => {
    stopShuttle(true);
    if (!videoEl.getAttribute("src")) return;
    const errCode = videoEl.error?.code;
    const name = state.activeVideo?.filename || "当前视频";
    let hint = `无法在内置播放器预览 ${name}`;
    if (errCode === 3 || errCode === 4) {
      hint = `${name} 封装或编码暂不被浏览器内核直接解码（常见于 MKV / HEVC），但仍可正常 AI 粗筛与无损切片导出`;
    } else if (errCode === 2) {
      hint = `加载视频流中断，请检查文件是否被移动或占用`;
    }
    setStatusMessage(hint, true);
    showToast(hint, true);
  });

  // 修复 P1-06：窗口失焦或隐藏时强制复位 K 键按住态与倒放 rAF，绝不卡死 0.5x 或后台空转
  const resetShuttleOnBlur = () => {
    shuttle.kHeld = false;
    if (shuttle.dir < 0 || shuttle.slowActive) {
      stopShuttle(true);
      videoEl.pause();
    }
  };
  window.addEventListener("blur", resetShuttleOnBlur);
  document.addEventListener("visibilitychange", () => {
    if (document.hidden) resetShuttleOnBlur();
  });

  el("sel-playback-rate")?.addEventListener("change", (e) => {
    stopShuttle(false);
    videoEl.playbackRate = Number(e.target.value) || 1.0;
  });
  el("sel-overlay-sub-mode")?.addEventListener("change", (e) => {
    state.overlaySubMode = e.target.value || "bilingual";
    syncVideoSubtitleOverlay();
  });
  el("btn-mark-in")?.addEventListener("click", () => markPreviewBoundary("in"));
  el("btn-mark-out")?.addEventListener("click", () => markPreviewBoundary("out"));
  el("btn-jump-clip-start")?.addEventListener("click", () => {
    const target = getClipByRank(state.previewSegment?.rank ?? state.activeClipRank);
    if (!target || !videoEl.src) {
      setStatusMessage("未选中预览片段", true);
      return;
    }
    stopShuttle(true);
    previewHighlightSegment(target);
  });
  el("btn-jump-clip-end")?.addEventListener("click", () => {
    const target = getClipByRank(state.previewSegment?.rank ?? state.activeClipRank);
    if (!target || !videoEl.src) return;
    stopShuttle(true);
    videoEl.pause();
    videoEl.currentTime = Number(target.end_sec) || 0;
    monitorEngine?.refreshFrameUI();
  });
  el("btn-step-prev-frame")?.addEventListener("click", () => stepVideoFrames(-1));
  el("btn-step-next-frame")?.addEventListener("click", () => stepVideoFrames(1));
  el("btn-shuttle-rev")?.addEventListener("click", () => triggerShuttle(-1, false));
  el("btn-shuttle-fwd")?.addEventListener("click", () => triggerShuttle(1, false));

  // 监听 K+J / K+L 慢放组合键释放
  document.addEventListener("keyup", (e) => {
    if (e.code === "KeyK") {
      shuttle.kHeld = false;
      if (shuttle.slowActive) {
        stopShuttle(true);
        videoEl.pause();
      }
    } else if ((e.code === "KeyJ" || e.code === "KeyL") && shuttle.slowActive) {
      stopShuttle(true);
      videoEl.pause();
    }
  });

  document.addEventListener("keydown", (e) => {
    if (e.isComposing || e.keyCode === 229) return;
    const mod = e.ctrlKey || e.metaKey;

    // Ctrl+S 始终全局生效
    if (mod && e.code === "KeyS") {
      e.preventDefault();
      saveHighlightsNow(false);
      return;
    }

    // Esc 优先退出影院全屏或关闭字幕查找栏
    if (e.key === "Escape") {
      if (monitorEngine?.cinemaActive) {
        e.preventDefault();
        monitorEngine.toggleCinemaMode(false);
        return;
      }
      const findBar = el("sub-find-bar");
      if (findBar && !findBar.classList.contains("hidden") && findBar.contains(document.activeElement)) {
        e.preventDefault();
        closeSubFindBar();
        return;
      }
    }

    const activeEl = document.activeElement;
    const activeTag = activeEl ? activeEl.tagName.toLowerCase() : "";
    const isTyping = isTextInputElement(activeEl);

    // 修复 P2-04：已接入 HistoryManager 的标题/备注/字幕文本框内按 Ctrl+Z / Ctrl+Shift+Z 统一走全局历史栈，防止原生 undo 与历史栈冲突
    if (
      isTyping &&
      mod &&
      !e.altKey &&
      (e.code === "KeyZ" || e.code === "KeyY") &&
      activeEl?.closest("#tab-panel-clip, #pane-subtitles")
    ) {
      e.preventDefault();
      if (e.code === "KeyZ" && !e.shiftKey) performUndo();
      else performRedo();
      return;
    }

    // Ctrl+F 打开字幕查找或聚焦高光搜索（即使在输入框内也可呼出）
    if (mod && !e.altKey && !e.shiftKey && e.code === "KeyF") {
      e.preventDefault();
      if (dock?.focusedPanelId === "clips") {
        el("clip-search-input")?.focus();
        el("clip-search-input")?.select();
      } else {
        openSubFindBar(false);
      }
      return;
    }

    if (isTyping) return;
    if (document.querySelector("dialog[open]")) return;
    // 修复 P1-07：右键菜单或顶栏下拉菜单/浮层打开时，屏蔽全局单键快捷键（防止 ↑/↓ 误切高光）
    if (document.querySelector(":popover-open, .popover-fallback-open")) return;

    // Ctrl / Cmd 组合键（撤销、重做、历史、拆分、波纹裁切、循环、设置、快捷导出）
    if (mod && !e.altKey) {
      if (e.code === "KeyZ" && !e.shiftKey) {
        e.preventDefault();
        performUndo();
        return;
      }
      if ((e.code === "KeyZ" && e.shiftKey) || e.code === "KeyY") {
        e.preventDefault();
        performRedo();
        return;
      }
      if (e.code === "KeyH") {
        e.preventDefault();
        dock?.focusPanel("history");
        return;
      }
      if (e.code === "Comma") {
        e.preventDefault();
        stopShuttle(true);
        videoEl.pause();
        el("settings-dialog")?.showModal?.();
        return;
      }
      if (e.code === "KeyE") {
        e.preventDefault();
        triggerExport(null);
        return;
      }
      if (e.code === "Slash") {
        e.preventDefault();
        const loopChk = el("chk-loop-segment");
        if (loopChk) {
          loopChk.checked = !loopChk.checked;
          setStatusMessage(loopChk.checked ? "已开启片段循环" : "已关闭片段循环");
        }
        return;
      }
      if (e.code === "Backslash") {
        e.preventDefault();
        if (e.shiftKey) splitActiveClipAtPlayhead();
        else splitSubtitleAtPlayhead();
        return;
      }
      if (e.shiftKey && e.code === "BracketLeft") {
        e.preventDefault();
        trimClipToPlayhead("start");
        return;
      }
      if (e.shiftKey && e.code === "BracketRight") {
        e.preventDefault();
        trimClipToPlayhead("end");
        return;
      }
      return;
    }

    // Alt 组合键：Alt+N 插字幕、Alt+I/O/X 还原边界、Alt+/ 从入点播到出点
    if (e.altKey && !e.ctrlKey && !e.metaKey) {
      if (e.code === "KeyN") {
        e.preventDefault();
        insertSubtitleAtPlayhead();
      } else if (e.code === "KeyI") {
        e.preventDefault();
        resetClipBoundary("in");
      } else if (e.code === "KeyO") {
        e.preventDefault();
        resetClipBoundary("out");
      } else if (e.code === "KeyX") {
        e.preventDefault();
        resetClipBoundary("both");
      } else if (e.code === "Slash") {
        e.preventDefault();
        const cur = getClipByRank(state.previewSegment?.rank ?? state.activeClipRank);
        if (cur) previewHighlightSegment(cur);
      }
      return;
    }

    // ? 打开快捷键速查（修复 P1-06：打开弹窗前立刻停止倒放 rAF）
    if (e.key === "?" || (e.shiftKey && e.code === "Slash")) {
      e.preventDefault();
      stopShuttle(true);
      videoEl.pause();
      const dlg = el("shortcuts-dialog");
      if (dlg && !dlg.open && typeof dlg.showModal === "function") dlg.showModal();
      return;
    }

    // Shift 组合快捷键（工作区 1~4、跳入/出点、裁头/尾到播放头、±1秒大步进、适配全片）
    if (e.shiftKey) {
      if (e.code === "Digit1") {
        e.preventDefault();
        dock?.setPreset("edit");
        return;
      }
      if (e.code === "Digit2") {
        e.preventDefault();
        dock?.setPreset("review");
        return;
      }
      if (e.code === "Digit3") {
        e.preventDefault();
        dock?.setPreset("subtitle");
        ensureActiveClipForSubtitles();
        renderActiveSubtitlesPane();
        return;
      }
      if (e.code === "Digit4") {
        e.preventDefault();
        dock?.setPreset("deliver");
        switchInspectorTab("export");
        return;
      }
      if (e.code === "KeyI") {
        e.preventDefault();
        const cur = getClipByRank(state.previewSegment?.rank ?? state.activeClipRank);
        if (cur && videoEl.src) {
          stopShuttle(true);
          videoEl.currentTime = Number(cur.start_sec) || 0;
          monitorEngine?.refreshFrameUI();
          setStatusMessage(`跳至 #${cur.rank} 入点 ${formatTimecode(cur.start_sec, true)}`);
        }
        return;
      }
      if (e.code === "KeyO") {
        e.preventDefault();
        const cur = getClipByRank(state.previewSegment?.rank ?? state.activeClipRank);
        if (cur && videoEl.src) {
          // 修复 P2-07：与走带按钮行为完全一致（停梭巡、暂停、同步帧 UI）
          stopShuttle(true);
          videoEl.pause();
          videoEl.currentTime = Number(cur.end_sec) || 0;
          monitorEngine?.refreshFrameUI();
          setStatusMessage(`跳至 #${cur.rank} 出点 ${formatTimecode(cur.end_sec, true)}`);
        }
        return;
      }
      if (e.code === "BracketLeft") {
        e.preventDefault();
        trimClipToPlayhead("start");
        return;
      }
      if (e.code === "BracketRight") {
        e.preventDefault();
        trimClipToPlayhead("end");
        return;
      }
      if (e.code === "ArrowLeft") {
        e.preventDefault();
        stepVideoSeconds(-1);
        return;
      }
      if (e.code === "ArrowRight") {
        e.preventDefault();
        stepVideoSeconds(1);
        return;
      }
      if (e.code === "KeyZ") {
        e.preventDefault();
        timelineEngine?.fitAll();
        setStatusMessage("时间线适配全片");
        return;
      }
      return;
    }

    const code = e.code;
    if (code === "KeyV") {
      e.preventDefault();
      timelineEngine?.setTool("select");
      setStatusMessage("工具：选择 / 拖边修剪 (V)");
      return;
    }
    if (code === "KeyB") {
      e.preventDefault();
      timelineEngine?.setTool("razor");
      setStatusMessage("工具：剃刀拆字幕 (B)");
      return;
    }
    if (code === "KeyN") {
      e.preventDefault();
      if (timelineEngine) {
        const next = !timelineEngine.snapEnabled;
        timelineEngine.setSnap(next);
        setStatusMessage(next ? "时间线磁吸：开" : "时间线磁吸：关");
      }
      return;
    }
    if (code === "KeyP") {
      e.preventDefault();
      if (videoEl.src) {
        const on = monitorEngine?.toggleCinemaMode();
        setStatusMessage(on ? "已进入影院全屏 (Esc / P 退出)" : "已退出影院全屏");
      }
      return;
    }
    if (code === "Equal" || code === "NumpadAdd") {
      e.preventDefault();
      timelineEngine?.zoomByFactor(0.68);
      return;
    }
    if (code === "Minus" || code === "NumpadSubtract") {
      e.preventDefault();
      timelineEngine?.zoomByFactor(1.45);
      return;
    }
    if (code === "Space") {
      // 修复 P0-04：无论按钮还是 checkbox/radio/range 获焦，按 Space 永远优先播控而非误切按钮/复选框
      if (activeTag === "button" || activeTag === "input") activeEl.blur();
      e.preventDefault();
      if (shuttle.dir !== 0 || shuttle.slowActive) {
        stopShuttle(true);
        videoEl.pause();
      } else if (videoEl.paused) {
        videoEl.play().catch(() => {});
      } else {
        videoEl.pause();
      }
    } else if (code === "KeyK") {
      e.preventDefault();
      shuttle.kHeld = true;
      stopShuttle(true);
      videoEl.pause();
    } else if (code === "KeyJ") {
      e.preventDefault();
      triggerShuttle(-1, e.repeat);
    } else if (code === "KeyL") {
      e.preventDefault();
      triggerShuttle(1, e.repeat);
    } else if (code === "ArrowLeft") {
      e.preventDefault();
      stepVideoFrames(-1);
    } else if (code === "ArrowRight") {
      e.preventDefault();
      stepVideoFrames(1);
    } else if (code === "Comma") {
      e.preventDefault();
      stepVideoSeconds(-1);
    } else if (code === "Period") {
      e.preventDefault();
      stepVideoSeconds(1);
    } else if (code === "Home" || code === "Semicolon") {
      e.preventDefault();
      stopShuttle(true);
      videoEl.currentTime = 0;
      monitorEngine?.refreshFrameUI();
    } else if (code === "End" || code === "Quote") {
      e.preventDefault();
      stopShuttle(true);
      if (videoEl.duration) videoEl.currentTime = Math.max(0, videoEl.duration - 0.1);
      monitorEngine?.refreshFrameUI();
    } else if (code === "KeyI") {
      e.preventDefault();
      markPreviewBoundary("in");
    } else if (code === "KeyO") {
      e.preventDefault();
      markPreviewBoundary("out");
    } else if (code === "Slash") {
      e.preventDefault();
      const target = getClipByRank(state.previewSegment?.rank ?? state.activeClipRank);
      if (target) {
        stopShuttle(true);
        previewHighlightSegment(target);
      }
    } else if (code === "ArrowUp") {
      e.preventDefault();
      navigateHighlightByOffset(-1);
    } else if (code === "ArrowDown") {
      e.preventDefault();
      navigateHighlightByOffset(1);
    } else if (code === "Enter" && activeTag !== "button") {
      const target = getClipByRank(state.activeClipRank);
      if (target) {
        e.preventDefault();
        stopShuttle(true);
        previewHighlightSegment(target);
      }
    } else if (code === "KeyC") {
      e.preventDefault();
      splitSubtitleAtPlayhead();
    } else if (code === "KeyM") {
      e.preventDefault();
      const loopChk = el("chk-loop-segment");
      if (loopChk) {
        loopChk.checked = !loopChk.checked;
        setStatusMessage(loopChk.checked ? "已开启片段循环" : "已关闭片段循环");
      }
    } else if ((code === "KeyS" || code === "KeyD") && state.highlightsData) {
      e.preventDefault();
      toggleActiveClipEnabled();
    }
  });
}

/* ==================== 后台任务提交与轮询 ==================== */
function startTaskPolling(taskId) {
  state.activeTaskId = taskId;
  if (state.pollTimer) clearInterval(state.pollTimer);
  let consecutiveErrors = 0;

  const pollOnce = async () => {
    try {
      const task = await apiFetch(`/api/tasks/${taskId}`);
      consecutiveErrors = 0;
      const pill = el("global-task-pill");
      pill.dataset.state = task.status;
      el("global-task-text").textContent = `${task.percent}% ${task.message}`;
      el("task-progress-label").textContent = `${task.percent}%`;
      el("task-progress-fill").style.width = `${task.percent}%`;
      const logPre = el("task-log-output");
      logPre.textContent = (task.logs || []).join("\n");
      logPre.scrollTop = logPre.scrollHeight;

      if (task.status === "completed" || task.status === "failed" || task.status === "cancelled") {
        clearInterval(state.pollTimer);
        state.pollTimer = null;
        if (task.status === "completed") {
          showToast(`${task.title} 完成`);
          await scanVideos();
          if (task.task_type === "export" && task.result) {
            renderExportManifestSummary(task.result);
          }
        } else if (task.status === "cancelled") {
          showToast(`已取消：${task.title}`);
        } else {
          showToast(task.error || "任务失败，请查看队列日志", true);
        }
      }
    } catch {
      consecutiveErrors += 1;
      if (consecutiveErrors >= 8) {
        clearInterval(state.pollTimer);
        state.pollTimer = null;
        showToast("后台任务状态轮询中断，请检查本地服务状态", true);
      }
    }
  };

  pollOnce();
  state.pollTimer = setInterval(pollOnce, 1200);
}

async function triggerDiagnose(videoPaths) {
  if (!videoPaths || videoPaths.length === 0) {
    showToast("请先选择视频", true);
    return;
  }
  try {
    const task = await apiFetch("/api/tasks/diagnose", {
      method: "POST",
      body: JSON.stringify({
        video_paths: videoPaths,
        output_dir: el("output-dir-field").value.trim(),
        top_percent: Number(el("num-top-percent").value) || 15,
        audio_stream: Number(el("sel-audio-stream").value) || 0,
        vocal_bandpass: el("chk-vocal-bandpass").checked,
        force_rerun: false,
      }),
    });
    showToast(`开始体检：${task.title}`, false, true);
    startTaskPolling(task.task_id);
  } catch (err) {
    showToast(err.message, true);
  }
}

async function triggerAnalyze(videoPaths) {
  if (!videoPaths || videoPaths.length === 0) {
    showToast("请先选择视频", true);
    return;
  }

  const topKVal = el("num-hl-count").value.trim();
  const minVal = el("num-min-clip").value.trim();
  const maxVal = el("num-max-clip").value.trim();
  const asrProvider = el("sel-asr-provider").value || "deepseek";
  const hotwordsVal = el("input-asr-hotwords").value.trim();

  const payload = {
    video_paths: videoPaths,
    output_dir: el("output-dir-field").value.trim(),
    top_percent: Number(el("num-top-percent").value) || 15,
    audio_stream: Number(el("sel-audio-stream").value) || 0,
    vocal_bandpass: el("chk-vocal-bandpass").checked,
    asr_provider: asrProvider,
    asr_mode: el("sel-asr-mode").value,
    top_k_peaks: Number(el("num-top-k-peaks").value) || 20,
    asr_model: asrProvider === "local" ? el("sel-whisper-model").value : null,
    hotwords: hotwordsVal || null,
    normalize_audio: el("chk-asr-normalize").checked,
    language: el("input-asr-lang").value.trim() || null,
    target_language: el("sel-target-lang")?.value || "auto",
    top_k_highlights: topKVal ? Number(topKVal) : null,
    min_clip_sec: minVal ? Number(minVal) : null,
    max_clip_sec: maxVal ? Number(maxVal) : null,
    bilingual_subtitles: el("chk-bilingual-translate").checked,
    force_rerun: el("chk-force-rerun").checked,
  };

  try {
    const task = await apiFetch("/api/tasks/analyze", {
      method: "POST",
      body: JSON.stringify(payload),
    });
    showToast(`已入队：${task.title}`);
    startTaskPolling(task.task_id);
  } catch (err) {
    showToast(err.message, true);
  }
}

function openRetranslateDialog(ranks = null, clipTitle = "") {
  if (!state.highlightsData) {
    showToast("当前无高光可重翻", true);
    return;
  }
  state.retranslateTargetRanks = Array.isArray(ranks) && ranks.length > 0 ? ranks : null;
  const dialog = el("retranslate-dialog");
  const desc = el("retranslate-target-desc");
  const hwInput = el("retranslate-hotwords");
  if (hwInput && !hwInput.value.trim()) {
    hwInput.value = el("input-asr-hotwords")?.value.trim() || state.config?.asr_hotwords || "";
  }
  const srcSel = el("retranslate-src-lang");
  const tgtSel = el("retranslate-tgt-lang");
  if (srcSel) srcSel.value = state.highlightsData?.source_language || el("input-asr-lang")?.value || "";
  if (tgtSel) tgtSel.value = state.highlightsData?.target_language || el("sel-target-lang")?.value || "auto";
  if (desc) {
    desc.textContent = state.retranslateTargetRanks
      ? `定点重刷 #${state.retranslateTargetRanks.join(", #")} ${clipTitle || ""}`
      : "重刷全部高光多语种双语字幕（保留入出点与勾选）";
  }
  if (dialog && typeof dialog.showModal === "function") {
    if (!dialog.open) dialog.showModal();
  } else {
    triggerRetranslate(state.retranslateTargetRanks, hwInput?.value.trim() || null, null);
  }
}

async function triggerRetranslate(
  ranks = null,
  hotwordsOverride = null,
  extraContext = null,
  sourceLangOverride = null,
  targetLangOverride = null
) {
  if (!state.highlightsData) return;
  await saveHighlightsNow(true);
  const hotwordsVal =
    hotwordsOverride !== null ? hotwordsOverride : el("input-asr-hotwords").value.trim();
  try {
    const task = await apiFetch("/api/tasks/retranslate", {
      method: "POST",
      body: JSON.stringify({
        highlights_json_path: state.highlightsData.highlights_json_path,
        hotwords: hotwordsVal || null,
        extra_context: extraContext || null,
        source_language: sourceLangOverride || null,
        target_language: targetLangOverride && targetLangOverride !== "auto" ? targetLangOverride : null,
        ranks: Array.isArray(ranks) && ranks.length > 0 ? ranks : null,
      }),
    });
    showToast(`已入队：${task.title}`);
    startTaskPolling(task.task_id);
  } catch (err) {
    showToast(err.message, true);
  }
}

async function triggerExport(specificRanks = null) {
  if (!state.highlightsData) {
    showToast("请先完成 AI 粗筛", true);
    return;
  }
  await saveHighlightsNow(true);

  const selectedMode =
    document.querySelector('input[name="export-mode"]:checked')?.value || "lossless";
  const payload = {
    highlights_json_path: state.highlightsData.highlights_json_path,
    video_path: el("override-video-path").value.trim() || null,
    mode: selectedMode,
    subtitle_lang: el("sel-subtitle-lang").value,
    show_title_banner: el("chk-title-banner").checked,
    burn_subtitles: el("chk-burn-subtitles").checked,
    encoder: el("sel-encoder").value,
    ranks: specificRanks,
  };

  try {
    const task = await apiFetch("/api/tasks/export", {
      method: "POST",
      body: JSON.stringify(payload),
    });
    showToast(`开始导出：${task.title}`);
    startTaskPolling(task.task_id);
  } catch (err) {
    showToast(err.message, true);
  }
}

function renderExportManifestSummary(manifest) {
  const box = el("export-result-box");
  const summary = el("export-result-summary");
  if (!box || !summary) return;
  summary.replaceChildren();

  if (!manifest || !Array.isArray(manifest.clips) || manifest.clips.length === 0) {
    box.classList.add("hidden");
    return;
  }
  box.classList.remove("hidden");

  const metaLine = document.createElement("div");
  metaLine.textContent = `${manifest.mode} · ${manifest.exported_count}条 · ${manifest.elapsed_sec}s`;
  summary.appendChild(metaLine);

  for (const c of manifest.clips.slice(0, 5)) {
    const itemLine = document.createElement("div");
    itemLine.textContent = `#${c.rank} (${c.size_mb}MB) ${c.title_zh}`;
    summary.appendChild(itemLine);
  }
}

/* ==================== 事件绑定 ==================== */
function bindEvents() {
  el("btn-browse-input")?.addEventListener("click", async () => {
    try {
      const res = await apiFetch("/api/browse-folder", {
        method: "POST",
        body: JSON.stringify({ initial_dir: el("input-dir-field").value, title: "选择输入目录" }),
      });
      if (res.path) {
        el("input-dir-field").value = res.path;
        await persistDirsAndScan();
      }
    } catch (err) {
      showToast(err.message, true);
    }
  });

  el("btn-browse-output")?.addEventListener("click", async () => {
    try {
      const res = await apiFetch("/api/browse-folder", {
        method: "POST",
        body: JSON.stringify({ initial_dir: el("output-dir-field").value, title: "选择输出目录" }),
      });
      if (res.path) {
        el("output-dir-field").value = res.path;
        await persistDirsAndScan();
      }
    } catch (err) {
      showToast(err.message, true);
    }
  });

  el("btn-browse-override-video")?.addEventListener("click", async () => {
    try {
      const res = await apiFetch("/api/browse-file", {
        method: "POST",
        body: JSON.stringify({ initial_dir: el("input-dir-field").value, title: "选择原片视频" }),
      });
      if (res.path) {
        el("override-video-path").value = res.path;
        setStatusMessage(`已选原片：${res.path}`);
      }
    } catch (err) {
      showToast(err.message, true);
    }
  });

  el("btn-scan-refresh")?.addEventListener("click", () => persistDirsAndScan());
  el("input-dir-field")?.addEventListener("change", () => persistDirsAndScan());
  el("output-dir-field")?.addEventListener("change", () => persistDirsAndScan());
  el("video-search-input")?.addEventListener("input", () => renderVideoList());

  el("btn-media-view-grid")?.addEventListener("click", () => {
    state.mediaViewMode = "grid";
    try { localStorage.setItem(MEDIA_VIEW_KEY, "grid"); } catch {}
    renderVideoList();
  });
  el("btn-media-view-list")?.addEventListener("click", () => {
    state.mediaViewMode = "list";
    try { localStorage.setItem(MEDIA_VIEW_KEY, "list"); } catch {}
    renderVideoList();
  });
  el("sel-media-stage-filter")?.addEventListener("change", (e) => {
    state.mediaStageFilter = e.target.value || "all";
    renderVideoList();
  });
  el("sel-media-sort")?.addEventListener("change", (e) => {
    state.mediaSortBy = e.target.value || "name";
    renderVideoList();
  });

  el("chk-select-all-videos")?.addEventListener("change", (e) => {
    const visible = getFilteredSortedVideos();
    if (e.target.checked) {
      for (const v of visible) state.checkedVideoPaths.add(v.video_path);
    } else {
      for (const v of visible) state.checkedVideoPaths.delete(v.video_path);
    }
    renderVideoList();
  });

  el("btn-batch-diagnose")?.addEventListener("click", () => {
    const paths = Array.from(state.checkedVideoPaths);
    if (paths.length === 0 && state.activeVideo) paths.push(state.activeVideo.video_path);
    triggerDiagnose(paths);
  });

  el("btn-quick-diagnose-current")?.addEventListener("click", () => {
    if (!state.activeVideo) return;
    triggerDiagnose([state.activeVideo.video_path]);
  });

  el("btn-apply-rec-peaks")?.addEventListener("click", () => applyRecommendedParams());

  el("btn-batch-analyze")?.addEventListener("click", () => {
    const paths = Array.from(state.checkedVideoPaths);
    if (paths.length === 0 && state.activeVideo) paths.push(state.activeVideo.video_path);
    triggerAnalyze(paths);
  });

  el("btn-analyze-current")?.addEventListener("click", () => {
    if (!state.activeVideo) {
      showToast("请先选择视频", true);
      return;
    }
    triggerAnalyze([state.activeVideo.video_path]);
  });

  el("btn-export-selected")?.addEventListener("click", () => triggerExport(null));

  el("btn-open-clips-dir")?.addEventListener("click", async () => {
    const target = state.activeVideo?.clips_dir || el("output-dir-field").value.trim();
    if (!target) return;
    try {
      await apiFetch("/api/open-folder", {
        method: "POST",
        body: JSON.stringify({ path: target }),
      });
    } catch (err) {
      showToast(err.message, true);
    }
  });

  el("btn-undo")?.addEventListener("click", () => performUndo());
  el("btn-redo")?.addEventListener("click", () => performRedo());
  el("btn-focus-history")?.addEventListener("click", () => dock?.focusPanel("history"));
  el("btn-hist-undo")?.addEventListener("click", () => performUndo());
  el("btn-hist-redo")?.addEventListener("click", () => performRedo());
  el("btn-hist-revert")?.addEventListener("click", () => performJumpHistory(0));

  el("btn-save-highlights")?.addEventListener("click", () => saveHighlightsNow(false));
  el("btn-retranslate-highlights")?.addEventListener("click", () => openRetranslateDialog(null));

  // 检查器「片段」页双向绑定（全部接入可撤销历史记录）
  el("clip-prop-selected")?.addEventListener("change", (e) => {
    const item = getClipByRank(state.activeClipRank);
    if (!item) return;
    item.selected = e.target.checked;
    if (state.onlySelected) renderHighlightsList();
    else syncHighlightRowStates();
    recordHistory(`${item.selected ? "勾选" : "取消勾选"} #${item.rank}`, "select");
    scheduleAutoSave();
  });

  el("clip-prop-title-zh")?.addEventListener("input", (e) => {
    const item = getClipByRank(state.activeClipRank);
    if (!item) return;
    item.title_zh = e.target.value;
    if (state.previewSegment && Number(state.previewSegment.rank) === Number(item.rank)) {
      el("hud-clip-title").textContent = `#${item.rank} ${item.title_zh}`;
    }
    el("sub-pane-clip-label").textContent = `#${item.rank} ${item.title_zh}`;
    syncHighlightRowStates();
    recordHistory(`编辑 #${item.rank} 中文标题`, "title", `title_zh_${item.rank}`);
    scheduleAutoSave();
  });

  el("clip-prop-title-en")?.addEventListener("input", (e) => {
    const item = getClipByRank(state.activeClipRank);
    if (!item) return;
    item.title_en = e.target.value;
    recordHistory(`编辑 #${item.rank} 英文标题`, "title", `title_en_${item.rank}`);
    scheduleAutoSave();
  });

  el("clip-prop-summary")?.addEventListener("input", (e) => {
    const item = getClipByRank(state.activeClipRank);
    if (!item) return;
    item.summary_zh = e.target.value;
    recordHistory(`编辑 #${item.rank} 备注`, "title", `summary_${item.rank}`);
    scheduleAutoSave();
  });

  el("btn-clip-prop-preview")?.addEventListener("click", () => {
    const item = getClipByRank(state.activeClipRank);
    if (item) {
      stopShuttle(true);
      previewHighlightSegment(item);
    }
  });
  el("btn-clip-prop-retranslate")?.addEventListener("click", () => {
    const item = getClipByRank(state.activeClipRank);
    if (item) openRetranslateDialog([item.rank], item.title_zh);
  });
  el("btn-clip-prop-export")?.addEventListener("click", () => {
    const item = getClipByRank(state.activeClipRank);
    if (item) triggerExport([item.rank]);
  });

  // 字幕面板加句（复用统一函数并写入历史）
  el("btn-sub-add-line")?.addEventListener("click", () => insertSubtitleAtPlayhead());

  el("btn-sub-retranslate-active")?.addEventListener("click", () => {
    const item = ensureActiveClipForSubtitles();
    if (!item) return;
    openRetranslateDialog([item.rank], item.title_zh);
  });

  el("btn-confirm-retranslate")?.addEventListener("click", async () => {
    const hw = el("retranslate-hotwords")?.value.trim() || "";
    const ctx = el("retranslate-context")?.value.trim() || "";
    const srcL = el("retranslate-src-lang")?.value || "";
    const tgtL = el("retranslate-tgt-lang")?.value || "auto";
    if (hw && el("input-asr-hotwords")) el("input-asr-hotwords").value = hw;
    el("retranslate-dialog")?.close();
    await triggerRetranslate(state.retranslateTargetRanks, hw, ctx, srcL, tgtL);
  });

  el("input-asr-lang")?.addEventListener("change", () => syncLanguagePairUI());
  el("sel-target-lang")?.addEventListener("change", () => syncLanguagePairUI());

  el("sel-asr-provider")?.addEventListener("change", () => syncAsrProviderVisibility());

  el("btn-hl-select-all")?.addEventListener("click", () => {
    if (!state.highlightsData) return;
    state.highlightsData.highlights.forEach((h) => {
      h.selected = true;
    });
    if (state.onlySelected) renderHighlightsList();
    else syncHighlightRowStates();
    renderClipInspector();
    recordHistory("全选全部高光", "batch");
    scheduleAutoSave();
  });

  el("btn-hl-unselect-all")?.addEventListener("click", () => {
    if (!state.highlightsData) return;
    state.highlightsData.highlights.forEach((h) => {
      h.selected = false;
    });
    if (state.onlySelected) renderHighlightsList();
    else syncHighlightRowStates();
    renderClipInspector();
    recordHistory("清空高光勾选", "batch");
    scheduleAutoSave();
  });

  el("chk-only-selected")?.addEventListener("change", (e) => {
    state.onlySelected = e.target.checked;
    renderHighlightsList();
  });

  // 高光索引搜索与 CSV 导出（P0-09）
  el("clip-search-input")?.addEventListener("input", (e) => {
    state.clipSearchQuery = e.target.value || "";
    renderHighlightsList();
  });
  el("btn-export-clips-csv")?.addEventListener("click", () => exportClipsIndexCsv());

  // 字幕面板全文查找与替换（P0-10）
  el("btn-sub-find-toggle")?.addEventListener("click", () => {
    const bar = el("sub-find-bar");
    if (bar && bar.classList.contains("hidden")) openSubFindBar(false);
    else closeSubFindBar();
  });
  el("btn-sub-find-close")?.addEventListener("click", () => closeSubFindBar());
  el("sub-find-input")?.addEventListener("input", () => runSubFind(true));
  el("sub-find-input")?.addEventListener("keydown", (e) => {
    if (e.isComposing || e.keyCode === 229) return;
    if (e.key === "Enter") {
      e.preventDefault();
      stepSubFindMatch(e.shiftKey ? -1 : 1);
    } else if (e.key === "Escape") {
      e.preventDefault();
      closeSubFindBar();
    }
  });
  el("btn-sub-find-prev")?.addEventListener("click", () => stepSubFindMatch(-1));
  el("btn-sub-find-next")?.addEventListener("click", () => stepSubFindMatch(1));
  el("btn-sub-replace-one")?.addEventListener("click", () => replaceCurrentSubMatch());
  el("btn-sub-replace-all")?.addEventListener("click", () => replaceAllSubMatches());

  // 打开模态弹窗按钮点击前自动停止倒放 rAF（P1-06）
  for (const id of ["btn-open-shortcuts", "btn-open-settings"]) {
    el(id)?.addEventListener("click", () => {
      stopShuttle(true);
      el("review-video")?.pause();
    });
  }

  document.querySelectorAll(".seg-btn").forEach((btn) => {
    btn.addEventListener("click", () => {
      document.querySelectorAll(".seg-btn").forEach((b) => b.classList.remove("active"));
      btn.classList.add("active");
      state.sortBy = btn.dataset.sort;
      renderHighlightsList();
    });
  });

  document.querySelectorAll(".tab-btn").forEach((btn) => {
    btn.addEventListener("click", () => {
      switchInspectorTab(btn.dataset.tab || "clip");
    });
  });

  el("btn-add-glossary-term")?.addEventListener("click", async () => {
    const enVal = el("glossary-in-en")?.value.trim() || "";
    const zhVal = el("glossary-in-zh")?.value.trim() || "";
    const aliasesVal = el("glossary-in-aliases")?.value.trim() || "";
    const noteVal = el("glossary-in-note")?.value.trim() || "";
    if (!enVal || !zhVal) {
      showToast("请填写源语词条与目标译名", true);
      return;
    }
    const aliases = aliasesVal
      ? aliasesVal
          .split(/[,，;；]+/)
          .map((s) => s.trim())
          .filter(Boolean)
      : [];
    const existingIdx = userGlossaryTerms.findIndex(
      (t) => t.en.toLowerCase() === enVal.toLowerCase()
    );
    const entry = { en: enVal, zh: zhVal, aliases, note: noteVal };
    if (existingIdx >= 0) userGlossaryTerms[existingIdx] = entry;
    else userGlossaryTerms.unshift(entry);
    el("glossary-in-en").value = "";
    el("glossary-in-zh").value = "";
    el("glossary-in-aliases").value = "";
    el("glossary-in-note").value = "";
    await persistUserGlossary();
  });

  el("btn-save-settings")?.addEventListener("click", async () => {
    try {
      const cfg = await apiFetch("/api/config", {
        method: "POST",
        body: JSON.stringify({
          base_url: el("cfg-base-url").value.trim(),
          llm_model: el("cfg-llm-model").value.trim(),
          api_key: el("cfg-api-key").value.trim() || null,
          asr_provider: el("cfg-asr-provider").value,
          asr_base_url: el("cfg-asr-base-url").value.trim(),
          asr_model: el("cfg-asr-model").value.trim(),
          asr_api_key: el("cfg-asr-api-key").value.trim() || null,
          asr_hotwords: el("cfg-asr-hotwords").value.trim(),
          screen_llm_model: el("cfg-llm-model").value.trim(),
          screen_reasoning_effort: el("cfg-screen-effort").value,
          polish_llm_model: el("cfg-polish-model").value.trim(),
          polish_llm_base_url: el("cfg-polish-base-url").value.trim(),
          polish_llm_api_key: el("cfg-polish-api-key").value.trim() || null,
          polish_reasoning_effort: el("cfg-polish-effort").value,
        }),
      });
      state.config = cfg;
      hydrateSettingsForm(cfg);
      el("settings-dialog").close();
      setStatusMessage("配置已保存至 .env");
    } catch (err) {
      showToast(err.message, true);
    }
  });

  el("btn-expand-corpus")?.addEventListener("click", async () => {
    try {
      const task = await apiFetch("/api/corpus/expand", {
        method: "POST",
        body: JSON.stringify({ commercial_only: false }),
      });
      showToast(`已入队：${task.title}`);
      startTaskPolling(task.task_id);
    } catch (err) {
      showToast(err.message, true);
    }
  });

  el("btn-load-demo-inline")?.addEventListener("click", () => loadDemoProjectIntoWorkbench());

  el("onboarding-provider-preset")?.addEventListener("change", (e) => {
    const baseEl = el("onboarding-base-url");
    if (!baseEl) return;
    baseEl.value = e.target.value === "openai" ? "https://api.openai.com/v1" : "https://api.deepseek.com";
  });

  el("btn-onboarding-save")?.addEventListener("click", async () => {
    const keyVal = el("onboarding-api-key")?.value.trim() || "";
    const baseVal = el("onboarding-base-url")?.value.trim() || "https://api.deepseek.com";
    const preset = el("onboarding-provider-preset")?.value || "deepseek";
    if (!keyVal) {
      showToast("请先粘贴 API Key，或点击左下角体验 30 秒示例", true);
      return;
    }
    try {
      const cfg = await apiFetch("/api/config", {
        method: "POST",
        body: JSON.stringify({
          base_url: baseVal,
          api_key: keyVal,
          asr_provider: preset === "deepseek" ? "deepseek" : "openai_compatible",
          asr_base_url: baseVal,
          asr_api_key: keyVal,
        }),
      });
      state.config = cfg;
      hydrateSettingsForm(cfg);
      el("onboarding-dialog")?.close();
      showToast("API Key 已安全保存至本机 .env，开剪！");
    } catch (err) {
      showToast(err.message, true);
    }
  });

  el("btn-onboarding-demo")?.addEventListener("click", async () => {
    el("onboarding-dialog")?.close();
    await loadDemoProjectIntoWorkbench();
  });
}

async function loadDemoProjectIntoWorkbench() {
  try {
    setStatusMessage("正在载入 30 秒示例工程...");
    const res = await apiFetch("/api/onboarding/load-demo", { method: "POST" });
    await scanVideos();
    const target = state.videos.find(
      (v) => v.stem === res.stem || v.video_path === res.video_path
    );
    if (target) {
      await selectVideo(target);
      showToast("已载入 30 秒示例工程：可直接审片、改字幕、试导出");
    } else {
      showToast("示例工程已导入输入目录");
    }
  } catch (err) {
    showToast(`载入示例工程失败：${err.message}`, true);
  }
}

async function checkOnboardingOnBoot() {
  try {
    const st = await apiFetch("/api/onboarding/status");
    const demoBtn = el("btn-onboarding-demo");
    if (demoBtn) demoBtn.classList.toggle("hidden", !st.has_demo);
    if (!st.has_api_key && !sessionStorage.getItem("clipstudio_onboarding_dismissed")) {
      const dlg = el("onboarding-dialog");
      if (dlg && typeof dlg.showModal === "function" && !dlg.open) {
        dlg.showModal();
        dlg.addEventListener(
          "close",
          () => sessionStorage.setItem("clipstudio_onboarding_dismissed", "1"),
          { once: true }
        );
      }
    }
  } catch {}
}

/* ==================== 原生无边框桌面窗口控制 ==================== */
let desktopState = {
  isDesktop: false,
  maximized: false,
  fullscreen: false,
};

function syncDesktopWindowUI() {
  const ctrlBox = el("desktop-window-controls");
  const resizeBox = el("win-resize-handles");
  const btnFs = el("btn-win-fullscreen");
  const btnMax = el("btn-win-max");

  document.body.classList.toggle("desktop-shell", desktopState.isDesktop);
  document.body.classList.toggle("desktop-fullscreen", desktopState.fullscreen);
  document.body.classList.toggle("desktop-maximized", desktopState.maximized);

  if (ctrlBox) ctrlBox.classList.toggle("hidden", !desktopState.isDesktop);
  if (resizeBox) {
    const showHandles = desktopState.isDesktop && !desktopState.fullscreen && !desktopState.maximized;
    resizeBox.classList.toggle("hidden", !showHandles);
  }
  if (btnFs) {
    btnFs.classList.toggle("active", desktopState.fullscreen);
    btnFs.title = desktopState.fullscreen ? "退出全屏 (F11)" : "无边框全屏 (F11)";
  }
  if (btnMax) {
    btnMax.classList.toggle("active", desktopState.maximized);
    btnMax.title = desktopState.maximized ? "还原" : "最大化";
  }
}

async function toggleAppFullscreen() {
  if (desktopState.isDesktop && window.pywebview?.api?.toggle_fullscreen) {
    try {
      const st = await window.pywebview.api.toggle_fullscreen();
      if (st) {
        desktopState.maximized = Boolean(st.maximized);
        desktopState.fullscreen = Boolean(st.fullscreen);
      }
      syncDesktopWindowUI();
      return;
    } catch {}
  }
  try {
    if (!document.fullscreenElement) {
      await document.documentElement.requestFullscreen();
    } else {
      await document.exitFullscreen();
    }
  } catch {}
}

function initDesktopShell() {
  const activateDesktopBridge = async () => {
    if (!window.pywebview?.api) return;
    desktopState.isDesktop = true;
    try {
      const st = await window.pywebview.api.get_window_state();
      if (st) {
        desktopState.maximized = Boolean(st.maximized);
        desktopState.fullscreen = Boolean(st.fullscreen);
      }
    } catch {}
    syncDesktopWindowUI();
  };

  if (window.pywebview?.api) {
    activateDesktopBridge();
  } else {
    window.addEventListener("pywebviewready", activateDesktopBridge, { once: true });
  }

  const topbar = el("app-topbar");
  const isInteractiveInTopbar = (target) =>
    Boolean(
      target &&
        target.closest(
          "button, input, select, a, label, .win-controls, .app-menubar, .menubar-trigger, .menubar-popover"
        )
    );

  topbar?.addEventListener("mousedown", (e) => {
    if (e.button !== 0 || !desktopState.isDesktop || desktopState.fullscreen) return;
    if (e.detail >= 2 || isInteractiveInTopbar(e.target)) return;
    window.pywebview?.api?.start_drag
      ?.()
      .then((st) => {
        if (st) {
          desktopState.maximized = Boolean(st.maximized);
          syncDesktopWindowUI();
        }
      })
      .catch(() => {});
  });

  topbar?.addEventListener("dblclick", (e) => {
    if (!desktopState.isDesktop || isInteractiveInTopbar(e.target)) return;
    window.pywebview?.api?.toggle_maximize
      ?.()
      .then((st) => {
        if (st) {
          desktopState.maximized = Boolean(st.maximized);
          desktopState.fullscreen = Boolean(st.fullscreen);
          syncDesktopWindowUI();
        }
      })
      .catch(() => {});
  });

  document.querySelectorAll("#win-resize-handles .win-edge").forEach((edgeEl) => {
    edgeEl.addEventListener("mousedown", (e) => {
      if (e.button !== 0 || !desktopState.isDesktop) return;
      e.preventDefault();
      window.pywebview?.api?.start_resize?.(edgeEl.dataset.edge || "").catch(() => {});
    });
  });

  el("btn-win-min")?.addEventListener("click", () => {
    window.pywebview?.api?.minimize?.().catch(() => {});
  });
  el("btn-win-max")?.addEventListener("click", async () => {
    try {
      const st = await window.pywebview?.api?.toggle_maximize?.();
      if (st) {
        desktopState.maximized = Boolean(st.maximized);
        desktopState.fullscreen = Boolean(st.fullscreen);
        syncDesktopWindowUI();
      }
    } catch {}
  });
  el("btn-win-fullscreen")?.addEventListener("click", () => toggleAppFullscreen());
  el("btn-win-close")?.addEventListener("click", () => {
    window.pywebview?.api?.close?.().catch(() => {});
  });

  document.addEventListener("keydown", (e) => {
    if (e.key === "F11") {
      e.preventDefault();
      toggleAppFullscreen();
    }
  });
}

(async function bootstrap() {
  initDesktopShell();
  initThemeListeners();
  initGlobalContextMenu();
  initHistoryManager();
  initDockWorkspace();
  initPopovers();
  initDialogs();
  initScrubbyInputs();
  initPlayerEvents();
  initTimelineEngine();
  initAppMenuBar();
  bindEvents();
  try {
    await loadConfigAndScan();
    syncLanguagePairUI();
    await checkOnboardingOnBoot();
  } catch (err) {
    showToast(`初始化失败：${err.message}`, true);
  }
})();
