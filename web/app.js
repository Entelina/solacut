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
  sortBy: "time", // 'time' | 'rank'
  onlySelected: false,
  reviewFilter: "all", // 'all' | 'reviewed' | 'draft'
  clipSearchQuery: "",
  subFindMatches: [],
  subFindCursor: -1,
  chapters: [],
  chaptersSeq: 0,
  deadAirSegments: [],
  skipDeadAirEnabled: false,
  lastDeadAirSkipAt: 0,
  bypassDeadAirSeg: null, // 用户显式拖入某静音段时记录该段，离段前绝不弹飞
  contextDialogSeq: 0,
  transcriptData: null,
  transcriptQuery: "",
  transcriptSelectedIds: new Set(),
  transcriptLastClickedIdx: -1,
  transcriptSeq: 0,
  chatRadarData: null,
  chatKeywordQuery: "",
  chatRadarSeq: 0,
  mediaViewMode: (() => {
    try {
      return localStorage.getItem(MEDIA_VIEW_KEY) === "list" ? "list" : "grid";
    } catch {
      return "grid";
    }
  })(),
  mediaStageFilter: "all",
  mediaSortBy: "name",
  mediaBins: { folders: [], assignments: {}, tags: {} },
  activeBinScope: "all", // 'all' | 'unfiled' | <folderId>
  editingBinId: null,
  selectedBinColor: "#4f8ef7",
  draggingVideoPaths: [],
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
  highlightsSeq: 0,
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

/* ==================== 6-Language UI i18n Helpers ==================== */
function t(key, vars = null) {
  return window.SolacutI18n ? window.SolacutI18n.t(key, vars) : key;
}

function getUiLang() {
  return window.SolacutI18n ? window.SolacutI18n.getLang() : "en";
}

function getPanelTitles() {
  return {
    media: t("panel.media"),
    monitor: t("panel.monitor"),
    timeline: t("panel.timeline"),
    clips: t("panel.clips"),
    subtitles: t("panel.subtitles"),
    transcript: t("panel.transcript"),
    inspector: t("panel.inspector"),
    queue: t("panel.queue"),
    history: t("panel.history"),
  };
}

function getStageLabel(stage) {
  const key = `media.stage.${stage || "idle"}`;
  const val = t(key);
  return val === key ? t("media.stage.idle") : val;
}

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
      ? t("ws.mediaAndClipsCount", { videos: totalVideos, selected: selectedClips, total: allClips.length })
      : t("ws.mediaCountOnly", { videos: totalVideos });
  }
  updateMediaCheckedIndicator();
}

function updateMediaCheckedIndicator() {
  const lbl = el("media-checked-label");
  const chkAll = el("chk-select-all-videos");
  const total = state.videos.length;
  const checked = state.checkedVideoPaths.size;

  if (lbl) {
    lbl.textContent =
      checked > 0
        ? t("media.checkedCount", { count: checked })
        : state.activeVideo
          ? t("media.currentVideo")
          : t("media.noSelection");
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
    throw new Error((body && body.detail) || t("toast.reqFailed", { status: resp.status }));
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
    const modeShort = getSystemPrefersDark()
      ? t("dlg.settings.themeDarkShort")
      : t("dlg.settings.themeLightShort");
    sysLabel.textContent = t("dlg.settings.themeSystemState", { mode: modeShort });
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
    if (e.key === window.SolacutI18n?.STORAGE_KEY && e.newValue) {
      window.SolacutI18n.setLang(e.newValue);
    }
  });
}

/* ==================== 自由停靠引擎 + 底部达芬奇导航 + 原生右键菜单 ==================== */
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
  const titles = getPanelTitles();

  document.querySelectorAll(".preset-btn[data-preset]").forEach((btn) => {
    const active = btn.dataset.preset === snapshot.preset;
    btn.setAttribute("aria-pressed", active ? "true" : "false");
  });

  const paneLbl = el("status-active-pane");
  if (paneLbl) {
    const baseName = titles[snapshot.focusedPanelId] || snapshot.focusedPanelId;
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
      if (isOpen && openSet.size <= 1) btn.title = t("dock.keepOne");

      const nameSpan = document.createElement("span");
      nameSpan.textContent = titles[p.id] || p.title;
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
  const titles = getPanelTitles();

  dock = new DockEngine({
    rootEl,
    parkingEl,
    panels: [
      { id: "media", title: titles.media, el: el("pane-media") },
      { id: "monitor", title: titles.monitor, el: el("pane-monitor") },
      { id: "timeline", title: titles.timeline, el: el("pane-timeline") },
      { id: "clips", title: titles.clips, el: el("pane-clips") },
      { id: "subtitles", title: titles.subtitles, el: el("pane-subtitles") },
      { id: "transcript", title: titles.transcript, el: el("pane-transcript") },
      { id: "inspector", title: titles.inspector, el: el("pane-inspector") },
      { id: "queue", title: titles.queue, el: el("pane-queue") },
      { id: "history", title: titles.history, el: el("pane-history") },
    ],
    onChange: (snap) => {
      syncDockChrome(snap);
      // 修复 P1-06：若监视器被隐藏或切出可视区，立即终止 J/K/L 倒放 rAF 防止后台空转
      if (!snap.openPanels?.includes("monitor")) {
        stopShuttle(true);
        el("review-video")?.pause();
      }
      if (snap.reason === "dock" || snap.reason === "reorder") {
        setStatusMessage(t("ws.dockSaved"));
      } else if (snap.reason === "reset") {
        setStatusMessage(t("ws.dockReset"));
      }
    },
    onTabContextMenu: (e, panelId) => {
      const snap = dock.getStateSnapshot();
      const canHide = (snap.openPanels || []).length > 1;
      ctxMenu?.open(e.clientX, e.clientY, [
        {
          label: snap.maximizedPanelId === panelId ? t("dock.restorePanel") : t("dock.maximizePanel"),
          hint: "`",
          onSelect: () => dock.toggleMaximize(panelId),
        },
        {
          label: t("dock.hidePanel"),
          disabled: !canHide,
          onSelect: () => dock.hidePanel(panelId),
        },
        { type: "separator" },
        {
          label: t("menu.workspace.reset"),
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
      setStatusMessage(t("ws.switched", { name: btn.textContent.trim() }));
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

function positionFloatingPopoverFallback(triggerEl, popEl) {
  if (!triggerEl || !popEl) return;
  const r = triggerEl.getBoundingClientRect();
  const rect = popEl.getBoundingClientRect();
  const popW = Math.max(160, Math.round(rect.width || popEl.offsetWidth || 320));
  const popH = Math.max(80, Math.round(rect.height || popEl.offsetHeight || 220));
  const vw = window.innerWidth;
  const vh = window.innerHeight;
  const alignRight = popEl.id === "chat-radar-popover";
  const preferUp = popEl.classList.contains("anchor-up");

  let left = alignRight ? r.right - popW : r.left;
  if (left + popW > vw - 8) left = vw - popW - 8;
  if (left < 8) left = 8;

  let top = preferUp ? r.top - popH - 6 : r.bottom + 6;
  if (preferUp && top < 8) top = Math.min(r.bottom + 6, Math.max(8, vh - popH - 8));
  if (!preferUp && top + popH > vh - 8) top = Math.max(8, r.top - popH - 6);

  popEl.style.left = `${Math.round(left)}px`;
  popEl.style.top = `${Math.round(top)}px`;
}

function wirePopoverFallback(triggerEl, popEl) {
  if (!triggerEl || !popEl) return;
  const supportsPopover = Object.hasOwn(HTMLElement.prototype, "popover");
  const supportsAnchor = CSS.supports("position-area: block-end");

  if (supportsPopover) {
    if (!supportsAnchor) {
      popEl.addEventListener("toggle", (e) => {
        if (e.newState === "open") {
          popEl.style.position = "fixed";
          popEl.style.inset = "auto";
          popEl.style.margin = "0";
          positionFloatingPopoverFallback(triggerEl, popEl);
        }
      });
    }
    return;
  }

  triggerEl.addEventListener("click", (e) => {
    e.stopPropagation();
    const opening = !popEl.classList.contains("popover-fallback-open");
    popEl.classList.toggle("popover-fallback-open", opening);
    if (opening) {
      requestAnimationFrame(() => positionFloatingPopoverFallback(triggerEl, popEl));
    }
  });
  window.addEventListener("resize", () => {
    if (popEl.classList.contains("popover-fallback-open")) {
      positionFloatingPopoverFallback(triggerEl, popEl);
    }
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
    label.title = t("topbar.dirsTooltip", { inDir: inRaw, outDir: outRaw });
  }
}

function initPopovers() {
  wirePopoverFallback(el("btn-dirs-popover"), el("dirs-popover"));
  wirePopoverFallback(el("btn-panels-popover"), el("panels-popover"));
  wirePopoverFallback(el("btn-tl-view-opts"), el("tl-view-options-popover"));
  wirePopoverFallback(el("btn-tl-chat-radar"), el("chat-radar-popover"));
  el("btn-close-dirs-popover")?.addEventListener("click", () => hidePopoverSafe(el("dirs-popover")));
  el("btn-close-chat-popover")?.addEventListener("click", () => hidePopoverSafe(el("chat-radar-popover")));
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
  const shortcutsDlg = el("shortcuts-dialog");
  setupModalDialogLightDismiss(shortcutsDlg);
  if (shortcutsDlg && typeof MutationObserver === "function") {
    const mo = new MutationObserver(() => {
      if (shortcutsDlg.open) {
        filterShortcutsTable(el("shortcut-search-input")?.value || "");
      }
    });
    mo.observe(shortcutsDlg, { attributes: true, attributeFilter: ["open"] });
  }
  setupModalDialogLightDismiss(el("retranslate-dialog"));
  setupModalDialogLightDismiss(el("context-dialog"));
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
  el("cfg-key-status").textContent = cfg.has_api_key ? `${cfg.api_key_masked}` : t("dlg.settings.notSet");

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
      ? t("dlg.settings.reuseMainKey")
      : t("dlg.settings.notSet");

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
      ? t("dlg.settings.reuseMainKey")
      : t("dlg.settings.notSet");

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
    empty.textContent = t("insp.glossary.empty");
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
    if (term.aliases?.length) subParts.push(t("insp.glossary.aliasPrefix", { aliases: term.aliases.join(", ") }));
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
    btnDel.title = t("insp.glossary.deleteTitle");
    btnDel.setAttribute("aria-label", t("insp.glossary.deleteTitle"));
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
    setStatusMessage(t("insp.glossary.savedStatus", { count: userGlossaryTerms.length }));
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
        ? t("insp.glossary.badgeMulti", { pairs: totalPairs.toLocaleString(), langs: lpKeys.length })
        : t("insp.glossary.badgeSingle", { pairs: totalPairs.toLocaleString() });
    }
    if (detail) {
      const lpSummary = lpKeys
        .map((k) => `${k.replace("_", "→").toUpperCase()} ${Math.round(Number(lpMap[k].total_pairs || 0) / 1000)}k`)
        .join(" / ");
      detail.textContent =
        t("insp.glossary.detail", {
          comm: commPairs.toLocaleString(),
          terms: stats.gaming_terms_count || 0,
          asr: stats.asr_mishearings_count || 0,
          idioms: stats.idioms_anti_literal_count || 0,
          custom: userGlossaryTerms.length,
        }) + (lpSummary ? `\n${lpSummary}` : "");
    }
  } catch {}
}

/* ==================== 6x6 Subtitle Language Engine & Monitor CC Popover Menu ==================== */
const SUPPORTED_SUB_LANGS = ["en", "zh", "ja", "ko", "ru", "es"];
const SUB_LANG_CJK_SET = new Set(["zh", "ja", "ko"]);

function getSubLangMeta(code) {
  const clean = String(code || "en").trim().toLowerCase();
  const c = SUPPORTED_SUB_LANGS.includes(clean) ? clean : "en";
  return {
    code: c.toUpperCase(),
    name: window.SolacutI18n ? window.SolacutI18n.getLangName(c) : c.toUpperCase(),
    short: window.SolacutI18n ? window.SolacutI18n.getLangShort(c) : c.toUpperCase(),
    cjk: SUB_LANG_CJK_SET.has(c),
  };
}

function formatSubtitlePairLabel(src, tgt) {
  return window.SolacutI18n
    ? window.SolacutI18n.formatPairLabel(src, tgt)
    : `${String(src || "en").toUpperCase()}→${String(tgt || "zh").toUpperCase()}`;
}

function normalizeOverlaySubMode(raw) {
  const m = String(raw || "bilingual").trim().toLowerCase();
  if (m === "bilingual_src_top" || m === "bilingual_rev") return "bilingual_src_top";
  if (m === "target_only" || m === "target" || m === "zh_only" || m === "zh") return "target_only";
  if (m === "source_only" || m === "source" || m === "en_only" || m === "en") return "source_only";
  if (m === "off" || m === "none") return "off";
  return "bilingual";
}

function getActiveLanguagePair(overrideTargetLang = null) {
  const srcRaw = (
    state.highlightsData?.source_language ||
    state.activeVideo?.source_language ||
    el("input-asr-lang")?.value ||
    "en"
  )
    .trim()
    .toLowerCase();
  const src = SUPPORTED_SUB_LANGS.includes(srcRaw) ? srcRaw : "en";

  let actualTgtRaw = (
    state.highlightsData?.target_language ||
    state.activeVideo?.target_language ||
    el("sel-target-lang")?.value ||
    "auto"
  )
    .trim()
    .toLowerCase();
  if (!actualTgtRaw || actualTgtRaw === "auto" || !SUPPORTED_SUB_LANGS.includes(actualTgtRaw)) {
    actualTgtRaw = src === "zh" ? "en" : "zh";
  }
  const actualTgt = actualTgtRaw;

  const pickRaw = (overrideTargetLang || "").trim().toLowerCase();
  const tgt = pickRaw && SUPPORTED_SUB_LANGS.includes(pickRaw) ? pickRaw : actualTgt;

  return {
    src,
    tgt,
    actualTgt,
    srcMeta: getSubLangMeta(src),
    tgtMeta: getSubLangMeta(tgt),
    actualTgtMeta: getSubLangMeta(actualTgt),
    isZhToEn: src === "zh" && actualTgt === "en",
    isSameLang: src === actualTgt,
    isDesiredSameLang: src === tgt,
  };
}

function resolveClipTitles(item, lp = getActiveLanguagePair()) {
  if (!item) return { tgtTitle: "", srcTitle: "" };
  const rawTgt = String(item.title_tgt || "").trim();
  const rawSrc = String(item.title_src || "").trim();
  const zh = String(item.title_zh || "").trim();
  const en = String(item.title_en || "").trim();
  if (rawTgt || rawSrc) {
    return {
      tgtTitle: rawTgt || (lp.actualTgt === "en" ? en || zh : zh || en),
      srcTitle: rawSrc || (lp.src === "zh" ? zh || en : en || zh),
    };
  }
  if (lp.isZhToEn) {
    return { tgtTitle: en || zh, srcTitle: zh || en };
  }
  return { tgtTitle: zh || en, srcTitle: en || zh };
}

function writeClipTitleTgt(item, val, lp = getActiveLanguagePair()) {
  if (!item) return;
  const text = String(val ?? "");
  item.title_tgt = text;
  if (lp.isZhToEn) item.title_en = text;
  else item.title_zh = text;
}

function writeClipTitleSrc(item, val, lp = getActiveLanguagePair()) {
  if (!item) return;
  const text = String(val ?? "");
  item.title_src = text;
  if (lp.isZhToEn) item.title_zh = text;
  else item.title_en = text;
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

function writeSubPairTexts(sub, tgtText, srcText, lp = getActiveLanguagePair()) {
  if (!sub) return;
  const cleanTgt = String(tgtText ?? "").trim();
  const cleanSrc = String(srcText ?? "").trim();
  sub.text_tgt = cleanTgt;
  sub.text_src = cleanSrc;
  if (lp.isZhToEn) {
    sub.text_en = cleanTgt;
    sub.text_zh = cleanSrc;
  } else {
    sub.text_zh = cleanTgt;
    sub.text_en = cleanSrc;
  }
}

function splitTextByRatio(text, ratio, isCjk = false) {
  const clean = String(text || "").trim();
  if (!clean) return ["", ""];
  if (isCjk || !/\s/.test(clean)) {
    if (clean.length <= 1) return [clean, ""];
    const mid = Math.max(1, Math.min(clean.length - 1, Math.round(clean.length * ratio)));
    return [clean.slice(0, mid).trim(), clean.slice(mid).trim()];
  }
  const words = clean.split(/\s+/).filter(Boolean);
  if (words.length <= 1) return [clean, ""];
  const mid = Math.max(1, Math.min(words.length - 1, Math.round(words.length * ratio)));
  return [words.slice(0, mid).join(" ").trim(), words.slice(mid).join(" ").trim()];
}

function getCcModeMenuLabel(mode, lp, useDesired = false) {
  const tgtMeta = useDesired ? lp.tgtMeta : lp.actualTgtMeta;
  const tgtCode = useDesired ? lp.tgt : lp.actualTgt;
  const same = lp.src === tgtCode;
  const pair = formatSubtitlePairLabel(lp.src, tgtCode);
  const vars = {
    pair,
    srcShort: lp.srcMeta.short,
    tgtShort: tgtMeta.short,
    srcCode: lp.srcMeta.code,
    tgtCode: tgtMeta.code,
    srcName: lp.srcMeta.name,
    tgtName: tgtMeta.name,
  };
  if (mode === "bilingual") {
    return same ? t("cc.mode.bilingualSame", vars) : t("cc.mode.bilingual", vars);
  }
  if (mode === "bilingual_src_top") return t("cc.mode.bilingual_src_top", vars);
  if (mode === "target_only") return t("cc.mode.target_only", vars);
  if (mode === "source_only") return t("cc.mode.source_only", vars);
  if (mode === "none") return t("cc.mode.none", vars);
  return t("cc.mode.off", vars);
}

function getCcButtonLabel(mode, lp) {
  const norm = normalizeOverlaySubMode(mode);
  const pair = formatSubtitlePairLabel(lp.src, lp.actualTgt);
  const vars = {
    pair,
    srcShort: lp.srcMeta.short,
    tgtShort: lp.actualTgtMeta.short,
    srcCode: lp.srcMeta.code,
    tgtCode: lp.actualTgtMeta.code,
  };
  if (norm === "bilingual") {
    return lp.isSameLang ? t("cc.btn.bilingualSame", vars) : t("cc.btn.bilingual", vars);
  }
  if (norm === "bilingual_src_top") return t("cc.btn.bilingual_src_top", vars);
  if (norm === "target_only") return t("cc.btn.target_only", vars);
  if (norm === "source_only") return t("cc.btn.source_only", vars);
  return t("cc.btn.off", vars);
}

function positionPopoverMenuFallback(triggerEl, popEl, preferUp = false) {
  if (!triggerEl || !popEl || CSS.supports("position-area: block-end")) return;
  const r = triggerEl.getBoundingClientRect();
  const popW = popEl.offsetWidth || 240;
  const popH = popEl.offsetHeight || 260;
  const vw = window.innerWidth;
  const vh = window.innerHeight;
  let left = r.right - popW;
  if (left < 6) left = Math.min(r.left, vw - popW - 6);
  left = Math.max(6, Math.min(left, vw - popW - 6));
  let top = preferUp ? r.top - popH - 4 : r.bottom + 4;
  if (preferUp && top < 6) top = Math.min(r.bottom + 4, vh - popH - 6);
  if (!preferUp && top + popH > vh - 6) top = Math.max(6, r.top - popH - 4);
  popEl.style.left = `${Math.round(left)}px`;
  popEl.style.top = `${Math.round(top)}px`;
}

function wireMenuPopoverInteractions(triggerEl, popEl, listEl, { preferUp = false, onBeforeOpen = null } = {}) {
  if (!triggerEl || !popEl || !listEl) return;
  const supportsPopover = Object.hasOwn(HTMLElement.prototype, "popover");
  const supportsCommand = "commandForElement" in HTMLButtonElement.prototype;
  const supportsFocusgroup = "focusGroup" in HTMLElement.prototype;

  if (!supportsFocusgroup) {
    listEl.setAttribute("role", "menu");
  }

  const isOpen = () => {
    try {
      if (supportsPopover && popEl.matches(":popover-open")) return true;
    } catch {}
    return popEl.classList.contains("popover-fallback-open");
  };

  const closeMenu = (restoreFocus = false) => {
    hidePopoverSafe(popEl);
    triggerEl.setAttribute("aria-expanded", "false");
    if (restoreFocus) triggerEl.focus({ preventScroll: true });
  };

  const openMenu = () => {
    onBeforeOpen?.();
    if (supportsPopover && typeof popEl.showPopover === "function") {
      try {
        popEl.showPopover({ source: triggerEl });
      } catch {
        try {
          popEl.showPopover();
        } catch {}
      }
    } else {
      popEl.classList.add("popover-fallback-open");
      triggerEl.setAttribute("aria-expanded", "true");
    }
    positionPopoverMenuFallback(triggerEl, popEl, preferUp);
  };

  if (supportsPopover) {
    popEl.addEventListener("beforetoggle", (e) => {
      const opening = e.newState === "open";
      if (opening) onBeforeOpen?.();
      triggerEl.setAttribute("aria-expanded", opening ? "true" : "false");
    });
    popEl.addEventListener("toggle", (e) => {
      const opened = e.newState === "open";
      triggerEl.setAttribute("aria-expanded", opened ? "true" : "false");
      if (opened) {
        positionPopoverMenuFallback(triggerEl, popEl, preferUp);
      }
    });
  }

  // Fallback click wiring when browser lacks native Invoker Commands (`commandfor`) or Popover API
  if (!supportsCommand || !supportsPopover) {
    triggerEl.addEventListener("click", (e) => {
      e.preventDefault();
      e.stopPropagation();
      if (isOpen()) closeMenu(false);
      else openMenu();
    });
  }

  if (!supportsPopover) {
    document.addEventListener("pointerdown", (e) => {
      if (isOpen() && !popEl.contains(e.target) && !triggerEl.contains(e.target)) {
        closeMenu(false);
      }
    });
  }

  // Keyboard menu navigation fallback + Escape/Tab close
  listEl.addEventListener("keydown", (e) => {
    if (e.key === "Escape") {
      e.preventDefault();
      e.stopPropagation();
      closeMenu(true);
      return;
    }
    if (e.key === "Tab") {
      closeMenu(false);
      return;
    }
    if (!supportsFocusgroup && ["ArrowDown", "ArrowUp", "Home", "End"].includes(e.key)) {
      const items = Array.from(listEl.querySelectorAll("button:not(:disabled)"));
      if (!items.length) return;
      const idx = items.indexOf(document.activeElement);
      let next = 0;
      if (e.key === "Home") next = 0;
      else if (e.key === "End") next = items.length - 1;
      else if (e.key === "ArrowDown") next = idx < 0 ? 0 : (idx + 1) % items.length;
      else if (e.key === "ArrowUp") next = idx <= 0 ? items.length - 1 : idx - 1;
      e.preventDefault();
      e.stopPropagation();
      items[next]?.focus({ preventScroll: true });
    }
  });

  return { openMenu, closeMenu, isOpen };
}

function handleCcQuickSwitchTargetLang(targetCode) {
  const clean = SUPPORTED_SUB_LANGS.includes(targetCode) ? targetCode : "zh";
  const lp = getActiveLanguagePair();
  const targetMeta = getSubLangMeta(clean);

  if (el("sel-export-target-lang")) el("sel-export-target-lang").value = clean;

  if (!state.highlightsData) {
    if (el("sel-target-lang")) el("sel-target-lang").value = clean;
    syncLanguagePairUI(false);
    setStatusMessage(t("cc.defaultTargetUpdated", { lang: targetMeta.name, code: targetMeta.code }));
    return;
  }

  if (clean === lp.actualTgt) {
    syncLanguagePairUI(false);
    return;
  }

  // Open retranslate dialog pre-filled with chosen target language for one-click confirmation
  openRetranslateDialog(null, "", {
    srcLang: lp.src,
    tgtLang: clean,
    customDesc: t("cc.retranslatePromptDesc", {
      fromName: lp.actualTgtMeta.name,
      toName: targetMeta.name,
      toCode: targetMeta.code,
    }),
  });
}

function renderCcMenu() {
  const listEl = el("cc-menu-list");
  const btnEl = el("btn-cc-menu");
  const btnLbl = el("cc-btn-label");
  const lp = getActiveLanguagePair();
  const curMode = normalizeOverlaySubMode(state.overlaySubMode);
  state.overlaySubMode = curMode;

  if (btnLbl) {
    btnLbl.textContent = getCcButtonLabel(curMode, lp);
  }
  if (btnEl) {
    btnEl.classList.toggle("is-off", curMode === "off");
  }
  if (!listEl) return;

  listEl.replaceChildren();

  const pairDisplay = lp.isSameLang
    ? `${lp.srcMeta.code}`
    : `${lp.srcMeta.code} → ${lp.actualTgtMeta.code}`;

  const overlayHead = document.createElement("div");
  overlayHead.className = "nle-menu-group-label";
  overlayHead.textContent = t("cc.groupOverlay", { pair: pairDisplay });
  listEl.appendChild(overlayHead);

  const modes = ["bilingual", "bilingual_src_top", "target_only", "source_only", "off"];
  for (const modeKey of modes) {
    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = "nle-menu-item";
    btn.setAttribute("role", "menuitemradio");
    btn.setAttribute("aria-checked", curMode === modeKey ? "true" : "false");
    btn.dataset.ccMode = modeKey;

    const lbl = document.createElement("span");
    lbl.className = "nle-menu-item-label";
    lbl.textContent = getCcModeMenuLabel(modeKey, lp, false);
    btn.appendChild(lbl);

    btn.addEventListener("click", () => {
      state.overlaySubMode = modeKey;
      const t1Vis = el("chk-tl-t1-vis");
      if (t1Vis) t1Vis.checked = modeKey !== "off";
      timelineEngine?.setTrackState({ t1Visible: modeKey !== "off" });
      renderCcMenu();
      syncVideoSubtitleOverlay();
      hidePopoverSafe(el("cc-menu"));
      el("btn-cc-menu")?.setAttribute("aria-expanded", "false");
    });
    listEl.appendChild(btn);
  }

  listEl.appendChild(document.createElement("hr"));

  const switchHead = document.createElement("div");
  switchHead.className = "nle-menu-group-label";
  switchHead.textContent = t("cc.groupSwitchTarget");
  listEl.appendChild(switchHead);

  for (const langCode of SUPPORTED_SUB_LANGS) {
    const meta = getSubLangMeta(langCode);
    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = "nle-menu-item";
    btn.setAttribute("role", "menuitemradio");
    btn.setAttribute("aria-checked", lp.actualTgt === langCode ? "true" : "false");
    btn.dataset.ccTargetLang = langCode;

    const lbl = document.createElement("span");
    lbl.className = "nle-menu-item-label";
    lbl.textContent = meta.name;
    const codeBadge = document.createElement("span");
    codeBadge.className = "nle-menu-item-code mono-num";
    codeBadge.textContent = meta.code;
    btn.append(lbl, codeBadge);

    btn.addEventListener("click", () => {
      hidePopoverSafe(el("cc-menu"));
      el("btn-cc-menu")?.setAttribute("aria-expanded", "false");
      handleCcQuickSwitchTargetLang(langCode);
    });
    listEl.appendChild(btn);
  }
}

function initCcMenu() {
  renderCcMenu();
  wireMenuPopoverInteractions(el("btn-cc-menu"), el("cc-menu"), el("cc-menu-list"), {
    preferUp: true,
    onBeforeOpen: () => renderCcMenu(),
  });
}

function syncUiLanguageControls() {
  const cur = getUiLang();
  const codeSpan = el("ui-lang-code");
  if (codeSpan) codeSpan.textContent = cur.toUpperCase();

  document.querySelectorAll("#ui-lang-menu [data-ui-lang-opt]").forEach((btn) => {
    const active = btn.dataset.uiLangOpt === cur;
    btn.setAttribute("aria-checked", active ? "true" : "false");
  });

  const cfgSel = el("cfg-ui-lang");
  if (cfgSel && cfgSel.value !== cur) {
    cfgSel.value = cur;
  }
}

function refreshAllUiLocale() {
  window.SolacutI18n?.applyDom(document);
  syncUiLanguageControls();
  syncThemeControls();
  updateDirSummaryText();
  updateStatusBarCounts();

  appMenuBar?.refreshLabels();
  if (dock) {
    dock.updatePanelTitles(getPanelTitles());
    syncDockChrome(dock.getStateSnapshot());
  }
  monitorEngine?.refreshLocale();
  timelineEngine?.refreshLocale();

  document.querySelectorAll("input.scrubby-auto").forEach((inp) => {
    inp._syncScrubbyLocale?.();
  });

  const pill = el("global-task-pill");
  if (pill && pill.dataset.state === "idle") {
    const txt = el("global-task-text");
    if (txt) txt.textContent = t("common.ready");
  }
  const saveStatus = el("save-status-text");
  if (saveStatus && !state.saveTimer) {
    saveStatus.textContent = t("ws.synced");
  }

  if (state.config) hydrateSettingsForm(state.config);
  syncLanguagePairUI(false);
  renderVideoList();
  renderDiagnosticsBar();
  renderHighlightsList();
  renderActiveSubtitlesPane();
  renderTranscriptPane();
  renderClipInspector();
  renderChatRadarPopover();
  syncChaptersToolbarButton();
  syncExportModeUI();
  renderHistoryPane();
  renderUserGlossaryList();
  loadCorpusStatus();
  filterShortcutsTable(el("shortcut-search-input")?.value || "");
}

const UI_SCALE_STEPS = [90, 100, 110, 125];

function getUiScalePct() {
  try {
    const raw = Number(
      document.documentElement.dataset.uiScale || localStorage.getItem("solacut_ui_scale") || 100
    );
    if (UI_SCALE_STEPS.includes(raw)) return raw;
  } catch {}
  return 100;
}

function applyUiScale(pct, persist = true) {
  const num = Number(pct);
  const resolved = UI_SCALE_STEPS.includes(num) ? num : 100;
  const ratio = resolved / 100;
  document.documentElement.dataset.uiScale = String(resolved);
  document.documentElement.style.setProperty("--ui-zoom", String(ratio));
  document.documentElement.style.zoom = resolved === 100 ? "" : String(ratio);
  const sel = el("cfg-ui-scale");
  if (sel && sel.value !== String(resolved)) {
    sel.value = String(resolved);
  }
  if (persist) {
    try {
      localStorage.setItem("solacut_ui_scale", String(resolved));
    } catch {}
  }
  requestAnimationFrame(() => {
    timelineEngine?.requestDraw();
    monitorEngine?.syncSafeGuideLayout?.();
    monitorEngine?.refreshFrameUI?.();
    monitorEngine?.audioMeter?.draw?.();
    monitorEngine?.scrubber?.draw?.();
  });
}

function stepUiScale(delta) {
  const cur = getUiScalePct();
  const idx = UI_SCALE_STEPS.indexOf(cur);
  const nextIdx = Math.max(0, Math.min(UI_SCALE_STEPS.length - 1, (idx < 0 ? 1 : idx) + delta));
  const next = UI_SCALE_STEPS[nextIdx];
  applyUiScale(next, true);
  setStatusMessage(`${t("dlg.cfg.uiScale")}: ${next}%`);
}

function getMediaCacheVersion(video) {
  const token =
    video?.cache_token ||
    video?.video_mtime_ns ||
    (video?.size_mb != null ? `${video.size_mb}_${video.total_duration_sec || 0}` : "1");
  return encodeURIComponent(String(token));
}

function showDarkroomConfirmDialog({ title = "", message = "", confirmText = "" } = {}) {
  const dlg = el("confirm-dialog");
  if (!dlg || typeof dlg.showModal !== "function") {
    return Promise.resolve(window.confirm(message || title));
  }
  const titleEl = el("confirm-dialog-title");
  const msgEl = el("confirm-dialog-message");
  const okEl = el("confirm-dialog-ok");
  if (titleEl) titleEl.textContent = title || t("dlg.confirm.title");
  if (msgEl) msgEl.textContent = message || "";
  if (okEl) okEl.textContent = confirmText || t("common.confirm");

  return new Promise((resolve) => {
    dlg.returnValue = "cancel";
    dlg.addEventListener(
      "close",
      () => {
        resolve(dlg.returnValue === "confirm");
      },
      { once: true }
    );
    dlg.showModal();
  });
}

function localizeTaskMessage(rawMsg) {
  const text = String(rawMsg || "").trim();
  if (!text || getUiLang() === "zh" || !/[\u4e00-\u9fff]/.test(text)) {
    return text;
  }
  const prefixMatch = text.match(/^(\[[^\]]+\]\s*)/);
  const prefix = prefixMatch ? prefixMatch[1] : "";
  const body = prefix ? text.slice(prefix.length) : text;
  if (/重新翻译/.test(body)) return `${prefix}Retranslating selected subtitles...`;
  if (/翻译|润色/.test(body)) return `${prefix}Translating subtitles...`;
  if (/抽取音频|提取音频|16k/i.test(body)) return `${prefix}Extracting audio...`;
  if (/体检|声学|峰值|波形/.test(body)) return `${prefix}Analyzing audio energy...`;
  if (/识别|转写|ASR|Whisper/i.test(body)) return `${prefix}Transcribing speech...`;
  if (/初筛|筛选|高光/.test(body)) return `${prefix}Screening highlight candidates...`;
  if (/导出|渲染|压制|切片/.test(body)) return `${prefix}Exporting clips...`;
  if (/词库|语料|索引/.test(body)) return `${prefix}Building corpus index...`;
  if (/弹幕/.test(body)) return `${prefix}Indexing chat log...`;
  if (/完成/.test(body)) return `${prefix}Completed`;
  if (/失败|错误/.test(body)) return `${prefix}Task failed`;
  return `${prefix}Processing...`;
}

function initUiLanguageSwitcher() {
  syncUiLanguageControls();
  applyUiScale(getUiScalePct(), false);
  wireMenuPopoverInteractions(el("btn-ui-lang"), el("ui-lang-menu"), el("ui-lang-menu-list"), {
    preferUp: false,
    onBeforeOpen: () => syncUiLanguageControls(),
  });

  document.querySelectorAll("#ui-lang-menu [data-ui-lang-opt]").forEach((btn) => {
    btn.addEventListener("click", () => {
      const next = btn.dataset.uiLangOpt || "en";
      hidePopoverSafe(el("ui-lang-menu"));
      el("btn-ui-lang")?.setAttribute("aria-expanded", "false");
      window.SolacutI18n?.setLang(next);
    });
  });

  el("cfg-ui-lang")?.addEventListener("change", (e) => {
    window.SolacutI18n?.setLang(e.target.value);
  });

  el("cfg-ui-scale")?.addEventListener("change", (e) => {
    applyUiScale(e.target.value, true);
  });

  window.SolacutI18n?.onChange(() => {
    refreshAllUiLocale();
  });
}

function syncLanguagePairUI(syncExportSelectFromData = false) {
  const exportTgtSel = el("sel-export-target-lang");
  const baseLp = getActiveLanguagePair();
  if (exportTgtSel && syncExportSelectFromData && SUPPORTED_SUB_LANGS.includes(baseLp.actualTgt)) {
    exportTgtSel.value = baseLp.actualTgt;
  }

  const desiredTgt = exportTgtSel?.value || baseLp.actualTgt;
  const lp = getActiveLanguagePair(desiredTgt);
  const hasMismatch = Boolean(state.highlightsData && lp.tgt !== lp.actualTgt);

  const exportSel = el("sel-subtitle-lang");
  if (exportSel) {
    Array.from(exportSel.options).forEach((opt) => {
      const val = opt.value;
      if (["bilingual", "bilingual_src_top", "target_only", "source_only", "none"].includes(val)) {
        opt.textContent = getCcModeMenuLabel(val, lp, true);
      }
    });
  }

  const switchBtn = el("btn-export-switch-lang");
  const switchBtnLabel = hasMismatch
    ? t("insp.export.switchLangTo", { code: lp.tgtMeta.code, short: lp.tgtMeta.short })
    : t("insp.export.switchLang");

  if (switchBtn) {
    switchBtn.classList.toggle("primary-subtle", hasMismatch);
    switchBtn.disabled = !state.highlightsData;
    switchBtn.textContent = switchBtnLabel;
  }

  const hintEl = el("export-lang-pair-hint");
  if (hintEl) {
    if (!state.highlightsData) {
      hintEl.textContent = t("insp.export.hintDefault");
      hintEl.classList.remove("is-mismatch");
    } else if (hasMismatch) {
      hintEl.textContent = t("insp.export.hintMismatch", {
        src: lp.srcMeta.name,
        actualTgt: lp.actualTgtMeta.name,
        btn: switchBtnLabel,
        desiredTgt: lp.tgtMeta.name,
        code: lp.tgtMeta.code,
      });
      hintEl.classList.add("is-mismatch");
    } else if (lp.isSameLang) {
      hintEl.textContent = t("insp.export.hintReadySame", {
        src: lp.srcMeta.name,
        srcCode: lp.srcMeta.code,
      });
      hintEl.classList.remove("is-mismatch");
    } else {
      hintEl.textContent = t("insp.export.hintReady", {
        src: lp.srcMeta.name,
        srcCode: lp.srcMeta.code,
        tgt: lp.actualTgtMeta.name,
        tgtCode: lp.actualTgtMeta.code,
      });
      hintEl.classList.remove("is-mismatch");
    }
  }

  const subPill = el("sub-pane-lang-pill");
  if (subPill) {
    subPill.textContent = `${lp.srcMeta.code} → ${lp.actualTgtMeta.code}`;
    subPill.title = t("sub.langPillTitle", { src: lp.srcMeta.name, tgt: lp.actualTgtMeta.name });
  }

  // 动态同步检查器「片段」页标题标签与占位符
  const lblTgt = el("lbl-clip-prop-title-tgt");
  const lblSrc = el("lbl-clip-prop-title-src");
  const inTgt = el("clip-prop-title-zh");
  const inSrc = el("clip-prop-title-en");
  if (lblTgt) {
    lblTgt.textContent = t("insp.clip.titleTgtLabel", {
      code: lp.actualTgtMeta.code,
      short: lp.actualTgtMeta.short,
    });
  }
  if (lblSrc) {
    lblSrc.textContent = t("insp.clip.titleSrcLabel", {
      code: lp.srcMeta.code,
      short: lp.srcMeta.short,
    });
  }
  if (inTgt) {
    inTgt.placeholder = t("insp.clip.titleTgtPlaceholder", { name: lp.actualTgtMeta.name });
  }
  if (inSrc) {
    inSrc.placeholder = t("insp.clip.titleSrcPlaceholder", { name: lp.srcMeta.name });
  }

  renderCcMenu();
}

/* ==================== 媒体池虚拟文件夹（Bin）与 5 色标签 ==================== */
const MEDIA_BINS_LS_KEY = "solacut_media_bins_v1";
const MEDIA_TAG_ORDER = { red: 0, orange: 1, green: 2, blue: 3, purple: 4 };
function getMediaTagMeta(color) {
  if (!color || !(color in MEDIA_TAG_ORDER)) return null;
  return { label: t(`media.tag.${color}`), order: MEDIA_TAG_ORDER[color] };
}

async function loadMediaBins() {
  try {
    const data = await apiFetch("/api/media-bins");
    state.mediaBins = {
      folders: Array.isArray(data?.folders) ? data.folders : [],
      assignments: data?.assignments && typeof data.assignments === "object" ? data.assignments : {},
      tags: data?.tags && typeof data.tags === "object" ? data.tags : {},
    };
    try {
      localStorage.setItem(MEDIA_BINS_LS_KEY, JSON.stringify(state.mediaBins));
    } catch {}
    return;
  } catch {
    try {
      const cached = JSON.parse(localStorage.getItem(MEDIA_BINS_LS_KEY) || "null");
      if (cached && Array.isArray(cached.folders)) {
        state.mediaBins = {
          folders: cached.folders,
          assignments: cached.assignments || {},
          tags: cached.tags || {},
        };
      }
    } catch {}
  }
}

async function persistMediaBins() {
  try {
    localStorage.setItem(MEDIA_BINS_LS_KEY, JSON.stringify(state.mediaBins));
  } catch {}
  try {
    const res = await apiFetch("/api/media-bins", {
      method: "PUT",
      body: JSON.stringify(state.mediaBins),
    });
    if (res && Array.isArray(res.folders)) {
      state.mediaBins = {
        folders: res.folders,
        assignments: res.assignments || {},
        tags: res.tags || {},
      };
    }
  } catch (err) {
    showToast(t("media.binSaveError", { msg: err.message }), true);
  }
}

function getBinFolderById(folderId) {
  return (state.mediaBins.folders || []).find((f) => f.id === folderId) || null;
}

function openMediaBinDialog(editFolder = null) {
  const dlg = el("media-bin-dialog");
  const titleEl = el("media-bin-dialog-title");
  const inputEl = el("media-bin-name-input");
  if (!dlg || !inputEl) return;

  state.editingBinId = editFolder ? editFolder.id : null;
  state.selectedBinColor = editFolder?.color || "#4f8ef7";
  if (titleEl) titleEl.textContent = editFolder ? t("dlg.bin.titleEdit") : t("dlg.bin.titleNew");
  inputEl.value = editFolder?.name || "";

  document.querySelectorAll("#media-bin-color-picker .bin-color-dot").forEach((dot) => {
    dot.classList.toggle("active", dot.dataset.binColor === state.selectedBinColor);
  });

  if (!dlg.open && typeof dlg.showModal === "function") dlg.showModal();
  setTimeout(() => {
    inputEl.focus();
    inputEl.select();
  }, 20);
}

async function saveMediaBinFromDialog() {
  const inputEl = el("media-bin-name-input");
  const name = (inputEl?.value || "").trim();
  if (!name) {
    showToast(t("media.binNameRequired"), true);
    inputEl?.focus();
    return;
  }

  const folders = state.mediaBins.folders || [];
  if (state.editingBinId) {
    const target = folders.find((f) => f.id === state.editingBinId);
    if (target) {
      target.name = name;
      target.color = state.selectedBinColor || "#4f8ef7";
    }
    setStatusMessage(t("media.binUpdated", { name }));
  } else {
    const newId = `bin_${Date.now().toString(36)}_${Math.random().toString(36).slice(2, 6)}`;
    folders.push({
      id: newId,
      name,
      color: state.selectedBinColor || "#4f8ef7",
      created_at: Date.now() / 1000,
    });
    state.mediaBins.folders = folders;
    setStatusMessage(t("media.binCreated", { name }));
  }

  el("media-bin-dialog")?.close();
  renderVideoList();
  await persistMediaBins();
}

async function deleteMediaBin(folderId) {
  const target = getBinFolderById(folderId);
  if (!target) return;
  state.mediaBins.folders = (state.mediaBins.folders || []).filter((f) => f.id !== folderId);
  const assign = state.mediaBins.assignments || {};
  for (const [vpath, bid] of Object.entries(assign)) {
    if (bid === folderId) delete assign[vpath];
  }
  if (state.activeBinScope === folderId) {
    state.activeBinScope = "all";
  }
  renderVideoList();
  setStatusMessage(t("media.binDeleted", { name: target.name }));
  await persistMediaBins();
}

async function moveVideosToBin(videoPaths, folderId) {
  const paths = Array.from(new Set((videoPaths || []).filter(Boolean)));
  if (paths.length === 0) return;
  const assign = state.mediaBins.assignments || {};
  for (const vp of paths) {
    if (folderId) assign[vp] = folderId;
    else delete assign[vp];
  }
  state.mediaBins.assignments = assign;
  const folder = folderId ? getBinFolderById(folderId) : null;
  const destLabel = folder ? `"${folder.name}"` : `"${t("media.binUnfiled")}"`;
  renderVideoList();
  showToast(t("media.binMoved", { count: paths.length, dest: destLabel }));
  await persistMediaBins();
}

async function setVideosColorTag(videoPaths, tagColor) {
  const paths = Array.from(new Set((videoPaths || []).filter(Boolean)));
  if (paths.length === 0) return;
  const tags = state.mediaBins.tags || {};
  for (const vp of paths) {
    if (tagColor) tags[vp] = tagColor;
    else delete tags[vp];
  }
  state.mediaBins.tags = tags;
  renderVideoList();
  setStatusMessage(
    tagColor ? t("media.tagApplied", { label: getMediaTagMeta(tagColor)?.label || tagColor }) : t("media.tagCleared")
  );
  await persistMediaBins();
}

function renderMediaBinBar() {
  const bar = el("media-bin-bar");
  if (!bar) return;
  const allBtn = bar.querySelector('[data-bin-scope="all"]');
  const unfiledBtn = bar.querySelector('[data-bin-scope="unfiled"]');
  const sep = el("media-bin-active-sep");
  const curName = el("media-bin-active-name");

  const folder =
    state.activeBinScope !== "all" && state.activeBinScope !== "unfiled"
      ? getBinFolderById(state.activeBinScope)
      : null;
  if (state.activeBinScope !== "all" && state.activeBinScope !== "unfiled" && !folder) {
    state.activeBinScope = "all";
  }

  allBtn?.classList.toggle("active", state.activeBinScope === "all");
  unfiledBtn?.classList.toggle("active", state.activeBinScope === "unfiled");

  if (folder) {
    sep?.classList.remove("hidden");
    if (curName) {
      curName.classList.remove("hidden");
      curName.textContent = folder.name;
      curName.style.color = folder.color || "";
    }
  } else {
    sep?.classList.add("hidden");
    curName?.classList.add("hidden");
  }
}

function buildMediaPoolEmptyContextMenuItems() {
  return [
    {
      label: t("media.ctx.newBin"),
      onSelect: () => openMediaBinDialog(null),
    },
    { type: "separator" },
    {
      label: t("media.ctx.showAll"),
      checked: state.activeBinScope === "all",
      onSelect: () => {
        state.activeBinScope = "all";
        renderVideoList();
      },
    },
    {
      label: t("media.ctx.showUnfiled"),
      checked: state.activeBinScope === "unfiled",
      onSelect: () => {
        state.activeBinScope = "unfiled";
        renderVideoList();
      },
    },
    { type: "separator" },
    {
      label: t("media.ctx.refresh"),
      onSelect: () => persistDirsAndScan(),
    },
    {
      label: t("media.ctx.openInput"),
      onSelect: async () => {
        const target = el("input-dir-field")?.value.trim();
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

function buildBinFolderContextMenuItems(folder) {
  const insideVideos = state.videos.filter(
    (v) => state.mediaBins.assignments?.[v.video_path] === folder.id
  );
  return [
    {
      label: t("media.ctx.openBin", { name: folder.name }),
      onSelect: () => {
        state.activeBinScope = folder.id;
        renderVideoList();
      },
    },
    {
      label: t("media.ctx.renameBin"),
      onSelect: () => openMediaBinDialog(folder),
    },
    {
      label: t("media.ctx.checkAllBin", { count: insideVideos.length }),
      disabled: insideVideos.length === 0,
      onSelect: () => {
        for (const v of insideVideos) state.checkedVideoPaths.add(v.video_path);
        renderVideoList();
        showToast(t("media.binCheckedToast", { name: folder.name, count: insideVideos.length }));
      },
    },
    { type: "separator" },
    {
      label: t("media.ctx.deleteBin"),
      danger: true,
      onSelect: () => deleteMediaBin(folder.id),
    },
  ];
}

/* ==================== 媒体池扫描与渲染 ==================== */
async function loadConfigAndScan() {
  const [cfg] = await Promise.all([apiFetch("/api/config"), loadMediaBins()]);
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
      loadOverviewWaveformForVideo(refreshed);
      if (refreshed.has_transcript) {
        loadTranscriptForVideo(refreshed);
      }
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
  const scope = state.activeBinScope || "all";
  const assign = state.mediaBins.assignments || {};
  const tags = state.mediaBins.tags || {};
  const gradeRank = { S: 0, A: 1, B: 2, WARN: 3 };

  let list = state.videos.filter((v) => {
    const binId = assign[v.video_path] || "";
    if (!keyword) {
      if (scope === "unfiled" && binId) return false;
      if (scope !== "all" && scope !== "unfiled" && binId !== scope) return false;
      // 在「全部」视图下，若已有文件夹，已归类到文件夹里的视频折叠在对应文件夹内，避免重复散落
      if (scope === "all" && binId && getBinFolderById(binId)) return false;
    } else if (scope !== "all" && scope !== "unfiled" && binId !== scope) {
      return false;
    }

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
  } else if (sortBy === "tag") {
    list = [...list].sort((a, b) => {
      const ta = tags[a.video_path] ? getMediaTagMeta(tags[a.video_path])?.order ?? 8 : 9;
      const tb = tags[b.video_path] ? getMediaTagMeta(tags[b.video_path])?.order ?? 8 : 9;
      return ta - tb || a.filename.localeCompare(b.filename);
    });
  } else {
    list = [...list].sort((a, b) => a.filename.localeCompare(b.filename));
  }
  return list;
}

function buildVideoContextMenuItems(video) {
  const isChecked = state.checkedVideoPaths.has(video.video_path);
  const targetPaths =
    isChecked && state.checkedVideoPaths.size > 1
      ? Array.from(state.checkedVideoPaths)
      : [video.video_path];
  const batchSuffix = targetPaths.length > 1 ? t("media.batchSuffix", { count: targetPaths.length }) : "";
  const curBinId = state.mediaBins.assignments?.[video.video_path] || "";
  const curTag = state.mediaBins.tags?.[video.video_path] || "";
  const folders = state.mediaBins.folders || [];

  const moveItems = folders.map((f) => ({
    label: t("media.ctx.moveToBin", { name: f.name, suffix: batchSuffix }),
    checked: curBinId === f.id,
    onSelect: () => moveVideosToBin(targetPaths, f.id),
  }));
  if (curBinId) {
    moveItems.push({
      label: t("media.ctx.removeFromBin", { suffix: batchSuffix }),
      onSelect: () => moveVideosToBin(targetPaths, ""),
    });
  }
  moveItems.push({
    label: t("media.ctx.newBinEllipsis"),
    onSelect: () => openMediaBinDialog(null),
  });

  const tagItems = [
    ...Object.keys(MEDIA_TAG_ORDER).map((key) => ({
      label: t("media.ctx.setTag", { label: getMediaTagMeta(key)?.label || key, suffix: batchSuffix }),
      checked: curTag === key,
      onSelect: () => setVideosColorTag(targetPaths, key),
    })),
    ...(curTag
      ? [
          {
            label: t("media.ctx.clearTag", { suffix: batchSuffix }),
            onSelect: () => setVideosColorTag(targetPaths, ""),
          },
        ]
      : []),
  ];

  return [
    {
      label: t("media.ctx.loadMonitor"),
      onSelect: () => selectActiveVideo(video),
    },
    {
      label: t("media.ctx.addToBatch"),
      checked: isChecked,
      onSelect: () => {
        if (isChecked) state.checkedVideoPaths.delete(video.video_path);
        else state.checkedVideoPaths.add(video.video_path);
        renderVideoList();
      },
    },
    { type: "separator" },
    ...moveItems,
    { type: "separator" },
    ...tagItems,
    { type: "separator" },
    {
      label: t("media.ctx.runDiagnose"),
      onSelect: () => triggerDiagnose([video.video_path]),
    },
    {
      label: t("media.ctx.runAnalyze"),
      onSelect: () => triggerAnalyze([video.video_path]),
    },
    { type: "separator" },
    {
      label: t("media.ctx.openOutput"),
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

function attachVideoDragEvents(cardEl, video) {
  cardEl.draggable = true;
  cardEl.addEventListener("dragstart", (e) => {
    const paths =
      state.checkedVideoPaths.has(video.video_path) && state.checkedVideoPaths.size > 1
        ? Array.from(state.checkedVideoPaths)
        : [video.video_path];
    state.draggingVideoPaths = paths;
    cardEl.classList.add("is-dragging");
    if (e.dataTransfer) {
      e.dataTransfer.effectAllowed = "move";
      e.dataTransfer.setData("text/plain", JSON.stringify(paths));
    }
  });
  cardEl.addEventListener("dragend", () => {
    state.draggingVideoPaths = [];
    cardEl.classList.remove("is-dragging");
    document.querySelectorAll(".is-drop-target").forEach((n) => n.classList.remove("is-drop-target"));
  });
}

function renderVideoList() {
  const container = el("video-list");
  if (!container) return;
  syncMediaViewButtons();
  renderMediaBinBar();

  const keyword = (el("video-search-input")?.value || "").trim().toLowerCase();
  const filtered = getFilteredSortedVideos();
  const showFolders = state.activeBinScope === "all" && !keyword;
  const folders = showFolders ? state.mediaBins.folders || [] : [];

  el("video-count-badge").textContent = String(state.videos.length);
  container.replaceChildren();

  // 1. 渲染自定义媒体池虚拟文件夹（支持点击进入、右键管理与直接拖入素材分类）
  for (const folder of folders) {
    const insideCount = state.videos.filter(
      (v) => state.mediaBins.assignments?.[v.video_path] === folder.id
    ).length;
    const fCard = document.createElement("div");
    fCard.className = "bin-folder-card";
    fCard.style.setProperty("--bin-color", folder.color || "#4f8ef7");
    fCard.title = t("media.folderTooltip", { name: folder.name, count: insideCount });

    const head = document.createElement("div");
    head.className = "bin-folder-head";
    const iconSvg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
    iconSvg.setAttribute("viewBox", "0 0 16 16");
    iconSvg.setAttribute("width", "15");
    iconSvg.setAttribute("height", "15");
    iconSvg.setAttribute("fill", "none");
    iconSvg.setAttribute("stroke", "currentColor");
    iconSvg.setAttribute("stroke-width", "1.3");
    iconSvg.classList.add("bin-folder-icon");
    const pth = document.createElementNS("http://www.w3.org/2000/svg", "path");
    pth.setAttribute("d", "M2 4.2h4.2l1.2 1.3H14v6.7a.8.8 0 0 1-.8.8H2.8a.8.8 0 0 1-.8-.8V4.2z");
    iconSvg.appendChild(pth);

    const fName = document.createElement("span");
    fName.className = "bin-folder-name";
    fName.textContent = folder.name;

    const fCnt = document.createElement("span");
    fCnt.className = "bin-folder-count mono-num";
    fCnt.textContent = `${insideCount}`;

    head.append(iconSvg, fName, fCnt);
    fCard.appendChild(head);

    if (state.mediaViewMode === "grid") {
      const fSub = document.createElement("div");
      fSub.className = "bin-folder-sub mono-num";
      fSub.textContent = t("media.folderSub", { count: insideCount });
      fCard.appendChild(fSub);
    }

    fCard.addEventListener("click", () => {
      state.activeBinScope = folder.id;
      renderVideoList();
    });
    fCard.addEventListener("contextmenu", (e) => {
      e.preventDefault();
      e.stopPropagation();
      ctxMenu?.open(e.clientX, e.clientY, buildBinFolderContextMenuItems(folder));
    });
    fCard.addEventListener("dragover", (e) => {
      e.preventDefault();
      if (e.dataTransfer) e.dataTransfer.dropEffect = "move";
      fCard.classList.add("is-drop-target");
    });
    fCard.addEventListener("dragleave", () => fCard.classList.remove("is-drop-target"));
    fCard.addEventListener("drop", (e) => {
      e.preventDefault();
      fCard.classList.remove("is-drop-target");
      let paths = state.draggingVideoPaths;
      if (!paths.length && e.dataTransfer) {
        try {
          paths = JSON.parse(e.dataTransfer.getData("text/plain") || "[]");
        } catch {
          paths = [];
        }
      }
      if (paths.length) moveVideosToBin(paths, folder.id);
    });
    container.appendChild(fCard);
  }

  if (filtered.length === 0 && folders.length === 0) {
    const hint = document.createElement("div");
    hint.className = "empty-hint";
    hint.textContent =
      state.activeBinScope === "all"
        ? t("media.emptyAll")
        : t("media.emptyScope");
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
    const localizedStage = getStageLabel(video.stage);
    const stageText = video.has_highlights
      ? `${localizedStage} ${video.selected_count}/${video.highlight_count}`
      : localizedStage;
    const tagColor = state.mediaBins.tags?.[video.video_path] || "";

    const chk = document.createElement("input");
    chk.type = "checkbox";
    chk.checked = state.checkedVideoPaths.has(video.video_path);
    chk.title = t("media.chkBatchTitle");
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
    if (tagColor) {
      const tagDot = document.createElement("span");
      tagDot.className = "media-tag-dot";
      tagDot.dataset.tag = tagColor;
      tagDot.title = getMediaTagMeta(tagColor)?.label || tagColor;
      title.appendChild(tagDot);
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
      // 修复 P1-09：与后端 /api/thumbnail?path=...&time=...&w=... 参数严格一致，并附加 cache_token 穿透旧缓存
      img.src = `/api/thumbnail?path=${encodeURIComponent(video.video_path)}&time=${thumbSec.toFixed(1)}&w=320&v=${getMediaCacheVersion(video)}`;
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
      attachVideoDragEvents(card, video);
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

      attachVideoDragEvents(card, video);
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
      opt.textContent = t("insp.analyze.defaultTrack");
      selectEl.appendChild(opt);
      badgeEl.textContent = t("insp.analyze.noAudioTrack");
      return;
    }

    for (const st of streams) {
      const opt = document.createElement("option");
      opt.value = String(st.audio_index);
      opt.textContent = st.label || t("insp.analyze.trackN", { idx: st.audio_index });
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
        ? t("insp.analyze.multiTrackRec", { count: streams.length, rec: data.recommended_stream })
        : t("insp.analyze.multiTrack", { count: streams.length });
    } else {
      badgeEl.textContent = t("insp.analyze.singleTrack");
    }
  } catch {
    if (reqSeq !== state.audioProbeSeq) return;
    selectEl.replaceChildren();
    const opt = document.createElement("option");
    opt.value = "0";
    opt.textContent = t("insp.analyze.defaultTrack");
    selectEl.appendChild(opt);
    badgeEl.textContent = "#0";
  }
}

async function loadMediaInfoForVideo(video) {
  if (!video) return;
  const reqSeq = ++state.mediaInfoSeq;
  const cacheVer = getMediaCacheVersion(video);
  const mediaCacheKey = `${video.video_path}::${video.cache_token || ""}`;
  try {
    const info = await apiFetch(`/api/media-info?path=${encodeURIComponent(video.video_path)}&v=${cacheVer}`);
    if (reqSeq !== state.mediaInfoSeq) return;
    state.mediaInfoCache.set(video.video_path, info);
    state.mediaInfoCache.set(mediaCacheKey, info);
    state.videoFps = Math.max(1, Number(info?.fps) || 30);
    monitorEngine?.setMediaInfo(info);
    const fpsBadge = el("tl-fps-badge");
    if (fpsBadge) {
      fpsBadge.textContent = `${Math.round(state.videoFps)}fps`;
    }
    timelineEngine?.setMedia({
      stem: video.stem,
      mediaKey: mediaCacheKey,
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
  const cacheVer = getMediaCacheVersion(video);
  try {
    const wf = await apiFetch(
      `/api/waveform?stem=${encodeURIComponent(video.stem)}&output_dir=${outDir}&points=1200&v=${cacheVer}`
    );
    if (seq !== state.waveformSeq) return;
    timelineEngine.setOverviewWaveform(wf);
  } catch {
    if (seq !== state.waveformSeq) return;
    timelineEngine.setOverviewWaveform(null);
  }

  // 若已有音频体检结果，顺手加载 peak_segments 与 dead_air_segments 给磁吸与时间线灰显使用
  if (video.has_peaks) {
    try {
      const diag = await apiFetch(
        `/api/diagnostics?stem=${encodeURIComponent(video.stem)}&output_dir=${outDir}&v=${cacheVer}`
      );
      if (seq !== state.waveformSeq) return;
      timelineEngine.setPeakSegments(diag.peak_segments || []);
      state.deadAirSegments = Array.isArray(diag.dead_air_segments) ? diag.dead_air_segments : [];
      timelineEngine.setDeadAirSegments(state.deadAirSegments);
    } catch {
      if (seq !== state.waveformSeq) return;
      state.deadAirSegments = [];
      timelineEngine.setDeadAirSegments([]);
    }
  } else {
    state.deadAirSegments = [];
    timelineEngine.setDeadAirSegments([]);
  }
}

function syncChaptersToolbarButton() {
  const btn = el("btn-tl-chapters");
  const lbl = el("tl-chapters-label");
  const cnt = el("tl-chapters-count");
  if (!btn || !lbl) return;
  const count = state.chapters.length;
  btn.classList.toggle("hidden", count === 0);
  lbl.textContent = t("tl.chaptersBtnLabel", { count });
  if (cnt) cnt.textContent = String(count);
}

async function loadChaptersForVideo(video) {
  if (!video) return;
  const seq = ++state.chaptersSeq;
  try {
    const res = await apiFetch(`/api/chapters?video_path=${encodeURIComponent(video.video_path)}`);
    if (seq !== state.chaptersSeq) return;
    state.chapters = Array.isArray(res.chapters) ? res.chapters : [];
  } catch {
    if (seq !== state.chaptersSeq) return;
    state.chapters = [];
  }
  timelineEngine?.setChapters(state.chapters);
  syncChaptersToolbarButton();
}

function recordUserSeekDeadAirBypass(targetSec) {
  const sec = Number(targetSec);
  if (!Number.isFinite(sec) || !state.deadAirSegments.length) {
    state.bypassDeadAirSeg = null;
    return;
  }
  const hit = state.deadAirSegments.find(
    (seg) => sec >= Number(seg.start_sec) - 0.1 && sec <= Number(seg.end_sec) + 0.1
  );
  state.bypassDeadAirSeg = hit
    ? { start_sec: Number(hit.start_sec), end_sec: Number(hit.end_sec) }
    : null;
}

function syncSkipDeadAirUI() {
  const btn = el("btn-skip-dead-air");
  if (!btn) return;
  const on = Boolean(state.skipDeadAirEnabled);
  btn.dataset.active = on ? "true" : "false";
  btn.setAttribute("aria-pressed", on ? "true" : "false");
}

function toggleSkipDeadAir(forceState = null) {
  state.skipDeadAirEnabled = forceState === null ? !state.skipDeadAirEnabled : Boolean(forceState);
  if (!state.skipDeadAirEnabled) {
    state.bypassDeadAirSeg = null;
  }
  syncSkipDeadAirUI();
  setStatusMessage(state.skipDeadAirEnabled ? t("mon.skipAirOn") : t("mon.skipAirOff"));
  return state.skipDeadAirEnabled;
}

function jumpMonitorToTime(sec, autoPlay = true) {
  const videoEl = el("review-video");
  if (!videoEl || !videoEl.src) return;
  stopShuttle(true);
  state.previewSegment = null;
  const limit = getVideoDurationLimit();
  const target = clampNum(sec, 0, limit);
  recordUserSeekDeadAirBypass(target);
  videoEl.currentTime = target;
  monitorEngine?.refreshFrameUI();
  if (autoPlay) videoEl.play().catch(() => {});
  syncVideoSubtitleOverlay();
}

async function createManualClipsFromSpecs(specs, historyLabel = null) {
  if (!state.activeVideo || !Array.isArray(specs) || specs.length === 0) {
    showToast(t("toast.selectVideoFirst"), true);
    return null;
  }
  if (state.saveTimer && state.highlightsData) {
    await saveHighlightsNow(true);
  }
  const outDir = el("output-dir-field")?.value.trim() || null;
  try {
    const hadHighlights = Boolean(state.highlightsData && Array.isArray(state.highlightsData.highlights));
    const res = await apiFetch("/api/highlights/create-manual", {
      method: "POST",
      body: JSON.stringify({
        video_path: state.activeVideo.video_path,
        output_dir: outDir,
        items: specs,
      }),
    });
    const prevManifest = state.highlightsData?.clips_manifest || null;
    state.highlightsData = { ...res, clips_manifest: prevManifest };
    for (const h of state.highlightsData.highlights || []) {
      const r = Number(h.rank);
      if (!state.initialClipBounds.has(r)) {
        state.initialClipBounds.set(r, {
          start_sec: Number(h.start_sec) || 0,
          end_sec: Number(h.end_sec) || 0,
        });
      }
    }
    const addedRanks = Array.isArray(res.added_ranks) ? res.added_ranks : [];
    const focusRank = addedRanks.length > 0 ? addedRanks[addedRanks.length - 1] : state.activeClipRank;
    if (focusRank != null) state.activeClipRank = Number(focusRank);

    if (state.activeVideo) {
      state.activeVideo.has_highlights = true;
      state.activeVideo.stage = state.activeVideo.has_clips ? "exported" : "analyzed";
      state.activeVideo.highlight_count = (state.highlightsData.highlights || []).length;
      state.activeVideo.selected_count = Number(res.selected_count || 0);
      state.activeVideo.reviewed_count = Number(res.reviewed_count || 0);
      state.activeVideo.highlights_json_path = res.highlights_json_path || state.activeVideo.highlights_json_path;
    }

    if (historyMgr) {
      const snap = captureEditableSnapshot();
      if (snap) {
        const lbl = historyLabel || t("hist.act.createManual", { count: addedRanks.length || specs.length });
        if (!hadHighlights || historyMgr.getState().total === 0) {
          historyMgr.reset({ highlights: [], activeClipRank: null }, t("hist.emptyProject"));
          historyMgr.commit(snap, lbl, { category: "edit" });
        } else {
          historyMgr.commit(snap, lbl, { category: "edit" });
        }
      }
    }

    renderVideoList();
    renderHighlightsList();
    syncLanguagePairUI(true);
    renderActiveSubtitlesPane();
    renderClipInspector();
    syncMonitorClipContext();
    updateStatusBarCounts();
    const addedCount = addedRanks.length || specs.length;
    showToast(t("tr.createdClipsToast", { count: addedCount }), false, true);
    return res;
  } catch (err) {
    showToast(err.message, true);
    return null;
  }
}

async function convertChaptersToClips(chaptersSubset = null) {
  const list = Array.isArray(chaptersSubset) && chaptersSubset.length > 0 ? chaptersSubset : state.chapters;
  if (!list || list.length === 0) {
    showToast(t("tl.noChapters"), true);
    return;
  }
  const durLimit = getVideoDurationLimit();
  const specs = [];
  for (let i = 0; i < list.length; i += 1) {
    const ch = list[i];
    const startSec = Math.max(0, Number(ch.start_sec) || 0);
    let endSec = Number(ch.end_sec) || 0;
    if (endSec <= startSec + 1.0) {
      const nextCh = state.chapters.find((c) => Number(c.start_sec) > startSec + 1.0);
      endSec = nextCh ? Math.min(Number(nextCh.start_sec), startSec + 60) : startSec + 30;
    }
    endSec = Math.min(durLimit, Math.max(startSec + 2.0, endSec));
    if (endSec <= startSec) continue;
    const title = String(ch.title || `Chapter ${ch.index || i + 1}`).trim();
    specs.push({
      start_sec: Number(startSec.toFixed(2)),
      end_sec: Number(endSec.toFixed(2)),
      title_tgt: title,
      title_src: title,
      summary_zh: title,
      review_status: "reviewed",
      source: "obs_chapter",
      tags: ["OBS"],
    });
  }
  if (!specs.length) return;
  await createManualClipsFromSpecs(specs, t("hist.act.importChapters", { count: specs.length }));
}

function appendHighlightedText(container, text, query) {
  const raw = String(text || "");
  const tokens = String(query || "")
    .trim()
    .split(/\s+/)
    .filter(Boolean);
  if (!tokens.length) {
    container.textContent = raw;
    return;
  }
  const escaped = tokens.map((tok) => tok.replace(/[.*+?^${}()|[\]\\]/g, "\\$&"));
  const re = new RegExp(`(${escaped.join("|")})`, "gi");
  let lastIdx = 0;
  let match = null;
  while ((match = re.exec(raw)) !== null) {
    if (match.index > lastIdx) {
      container.appendChild(document.createTextNode(raw.slice(lastIdx, match.index)));
    }
    const mark = document.createElement("mark");
    mark.textContent = match[0];
    container.appendChild(mark);
    lastIdx = match.index + match[0].length;
    if (match[0].length === 0) break;
  }
  if (lastIdx < raw.length) {
    container.appendChild(document.createTextNode(raw.slice(lastIdx)));
  }
}

function updateTranscriptSelectionControls() {
  const createBtn = el("btn-transcript-create-clip");
  const clearSelBtn = el("btn-transcript-clear-sel");
  const cnt = state.transcriptSelectedIds.size;
  if (createBtn) {
    createBtn.disabled = cnt === 0 || !state.activeVideo;
    createBtn.textContent =
      cnt > 0 ? t("tr.createClipCount", { count: cnt }) : t("tr.createClipBtn");
  }
  if (clearSelBtn) {
    clearSelBtn.classList.toggle("hidden", cnt === 0);
  }
}

async function loadTranscriptForVideo(video, queryOverride = null) {
  if (!video) {
    state.transcriptData = null;
    renderTranscriptPane();
    return;
  }
  const seq = ++state.transcriptSeq;
  const outDir = encodeURIComponent(el("output-dir-field").value.trim());
  const q = queryOverride !== null ? queryOverride : state.transcriptQuery;
  const qParam = q && q.trim() ? `&q=${encodeURIComponent(q.trim())}` : "";
  try {
    const res = await apiFetch(
      `/api/transcript?stem=${encodeURIComponent(video.stem)}&output_dir=${outDir}${qParam}`
    );
    if (seq !== state.transcriptSeq) return;
    state.transcriptData = res;
  } catch {
    if (seq !== state.transcriptSeq) return;
    state.transcriptData = { exists: false, segments: [], total_count: 0, matched_count: 0 };
  }
  renderTranscriptPane();
}

function renderTranscriptPane() {
  const listEl = el("transcript-list");
  const badgeEl = el("transcript-count-badge");
  const clearSearchBtn = el("btn-transcript-clear-search");
  if (!listEl) return;

  const q = (state.transcriptQuery || "").trim();
  if (clearSearchBtn) clearSearchBtn.classList.toggle("hidden", !q);
  listEl.replaceChildren();

  if (!state.activeVideo) {
    if (badgeEl) badgeEl.textContent = "0";
    const empty = document.createElement("div");
    empty.className = "empty-hint";
    empty.textContent = t("tr.emptyNoVideo");
    listEl.appendChild(empty);
    updateTranscriptSelectionControls();
    return;
  }

  const data = state.transcriptData;
  if (!data || !data.exists) {
    if (badgeEl) badgeEl.textContent = "0";
    const empty = document.createElement("div");
    empty.className = "empty-hint";
    empty.textContent = t("tr.emptyNoTranscript");
    listEl.appendChild(empty);
    updateTranscriptSelectionControls();
    return;
  }

  const segs = Array.isArray(data.segments) ? data.segments : [];
  if (badgeEl) {
    badgeEl.textContent = q ? `${data.matched_count}/${data.total_count}` : String(data.total_count);
  }
  if (segs.length === 0) {
    const empty = document.createElement("div");
    empty.className = "empty-hint";
    empty.textContent = q ? t("tr.emptyNoMatch") : t("tr.emptyNoTranscript");
    listEl.appendChild(empty);
    updateTranscriptSelectionControls();
    return;
  }

  const activeClip = getClipByRank(state.activeClipRank);
  const frag = document.createDocumentFragment();
  const displayLimit = Math.min(segs.length, 600);
  for (let i = 0; i < displayLimit; i += 1) {
    const seg = segs[i];
    const row = document.createElement("div");
    row.className = "tr-row";
    row.setAttribute("role", "listitem");
    const isSel = state.transcriptSelectedIds.has(seg.id);
    if (isSel) row.classList.add("is-selected");
    if (
      activeClip &&
      seg.end >= Number(activeClip.start_sec) &&
      seg.start <= Number(activeClip.end_sec)
    ) {
      row.classList.add("is-current-clip");
    }

    const chk = document.createElement("input");
    chk.type = "checkbox";
    chk.checked = isSel;
    chk.setAttribute("aria-label", formatClock(seg.start));
    chk.addEventListener("click", (e) => {
      e.stopPropagation();
      if (e.shiftKey && state.transcriptLastClickedIdx >= 0) {
        const lo = Math.min(state.transcriptLastClickedIdx, i);
        const hi = Math.max(state.transcriptLastClickedIdx, i);
        for (let k = lo; k <= hi; k += 1) {
          if (chk.checked) state.transcriptSelectedIds.add(segs[k].id);
          else state.transcriptSelectedIds.delete(segs[k].id);
        }
        renderTranscriptPane();
        return;
      }
      if (chk.checked) state.transcriptSelectedIds.add(seg.id);
      else state.transcriptSelectedIds.delete(seg.id);
      state.transcriptLastClickedIdx = i;
      row.classList.toggle("is-selected", chk.checked);
      updateTranscriptSelectionControls();
    });

    const tcSpan = document.createElement("span");
    tcSpan.className = "tr-row-time mono-num";
    tcSpan.textContent = formatClock(seg.start);

    const txtSpan = document.createElement("span");
    txtSpan.className = "tr-row-text";
    appendHighlightedText(txtSpan, seg.text, q);

    const addBtn = document.createElement("button");
    addBtn.type = "button";
    addBtn.className = "nle-btn-xs tr-row-add";
    addBtn.textContent = t("tr.quickClipBtn");
    addBtn.title = t("tr.quickClipTitle");
    addBtn.addEventListener("click", async (e) => {
      e.stopPropagation();
      const limit = getVideoDurationLimit();
      const sSec = Math.max(0, Number(seg.start) - 1.5);
      const eSec = Math.min(limit, Math.max(Number(seg.end) + 1.5, sSec + 3.0));
      const title = String(seg.text || "").slice(0, 48).trim() || `Clip ${formatClock(seg.start)}`;
      await createManualClipsFromSpecs(
        [
          {
            start_sec: Number(sSec.toFixed(2)),
            end_sec: Number(eSec.toFixed(2)),
            title_tgt: title,
            title_src: title,
            summary_zh: seg.text,
            review_status: "reviewed",
            source: "transcript",
            tags: ["Transcript"],
          },
        ],
        t("hist.act.createFromTranscript")
      );
    });

    row.append(chk, tcSpan, txtSpan, addBtn);
    row.addEventListener("click", () => {
      jumpMonitorToTime(seg.start, true);
      setStatusMessage(t("tr.jumpedLine", { clock: formatClock(seg.start) }));
    });
    frag.appendChild(row);
  }
  if (segs.length > displayLimit) {
    const more = document.createElement("div");
    more.className = "empty-hint";
    more.textContent = t("tr.truncatedHint", { shown: displayLimit, total: segs.length });
    frag.appendChild(more);
  }
  listEl.appendChild(frag);
  updateTranscriptSelectionControls();
}

async function createClipFromSelectedTranscript() {
  const segs = state.transcriptData?.segments || [];
  const chosen = segs.filter((s) => state.transcriptSelectedIds.has(s.id));
  if (chosen.length === 0) return;
  const minStart = Math.min(...chosen.map((s) => Number(s.start) || 0));
  const maxEnd = Math.max(...chosen.map((s) => Number(s.end) || Number(s.start) + 1));
  const limit = getVideoDurationLimit();
  const sSec = Math.max(0, minStart - 0.8);
  const eSec = Math.min(limit, Math.max(maxEnd + 0.8, sSec + 2.5));
  const combinedText = chosen.map((s) => s.text).join(" ").trim();
  const title = combinedText.slice(0, 52) || `Clip ${formatClock(sSec)}`;
  const res = await createManualClipsFromSpecs(
    [
      {
        start_sec: Number(sSec.toFixed(2)),
        end_sec: Number(eSec.toFixed(2)),
        title_tgt: title,
        title_src: title,
        summary_zh: combinedText.slice(0, 180),
        review_status: "reviewed",
        source: "transcript",
        tags: ["Transcript"],
      },
    ],
    t("hist.act.createFromTranscript")
  );
  if (res) {
    state.transcriptSelectedIds.clear();
    state.transcriptLastClickedIdx = -1;
    renderTranscriptPane();
  }
}

async function openContext30sDialog(clip) {
  const target = clip || getClipByRank(state.activeClipRank);
  if (!target || !state.activeVideo) {
    showToast(t("toast.selectClipFirst"), true);
    return;
  }
  const dlg = el("context-dialog");
  const body = el("context-dialog-body");
  const rangeBadge = el("context-dialog-range");
  if (!dlg || !body) return;

  const seq = ++state.contextDialogSeq;
  const targetVideoPath = state.activeVideo.video_path;
  const targetStem = state.activeVideo.stem;

  const sSec = Number(target.start_sec) || 0;
  const eSec = Number(target.end_sec) || sSec + 5;
  const winStart = Math.max(0, sSec - 30);
  const winEnd = eSec + 30;
  const midSec = (sSec + eSec) * 0.5;
  const halfWin = Math.min(600, Math.max(30, (eSec - sSec) * 0.5 + 30));

  if (rangeBadge) {
    rangeBadge.textContent = `${formatClock(winStart)} – ${formatClock(winEnd)}`;
  }
  body.replaceChildren();
  const loading = document.createElement("div");
  loading.className = "empty-hint";
  loading.textContent = t("common.loading");
  body.appendChild(loading);

  stopShuttle(true);
  if (!dlg.open && typeof dlg.showModal === "function") {
    dlg.showModal();
  }

  try {
    const outDir = encodeURIComponent(el("output-dir-field").value.trim());
    const res = await apiFetch(
      `/api/transcript?stem=${encodeURIComponent(targetStem)}&output_dir=${outDir}&around_sec=${midSec.toFixed(2)}&window_sec=${halfWin.toFixed(2)}`
    );
    if (seq !== state.contextDialogSeq || state.activeVideo?.video_path !== targetVideoPath || !dlg.open) {
      return;
    }
    body.replaceChildren();
    if (!res.exists || !Array.isArray(res.segments) || res.segments.length === 0) {
      const empty = document.createElement("div");
      empty.className = "empty-hint";
      empty.textContent = t("dlg.context.empty");
      body.appendChild(empty);
      return;
    }

    let firstInClipBtn = null;
    for (const seg of res.segments) {
      const btn = document.createElement("button");
      btn.type = "button";
      btn.className = "ctx-line-btn";
      const inClip = seg.end >= sSec - 0.1 && seg.start <= eSec + 0.1;
      if (inClip) {
        btn.classList.add("in-clip");
        if (!firstInClipBtn) firstInClipBtn = btn;
      }

      const tc = document.createElement("span");
      tc.className = "ctx-line-time mono-num";
      tc.textContent = formatClock(seg.start);

      const txt = document.createElement("span");
      txt.className = "ctx-line-text";
      txt.textContent = seg.text;

      btn.append(tc, txt);
      btn.addEventListener("click", () => {
        jumpMonitorToTime(seg.start, true);
        setStatusMessage(t("tr.jumpedLine", { clock: formatClock(seg.start) }));
      });
      body.appendChild(btn);
    }
    firstInClipBtn?.scrollIntoView({ block: "center" });
  } catch (err) {
    if (seq !== state.contextDialogSeq || state.activeVideo?.video_path !== targetVideoPath || !dlg.open) {
      return;
    }
    body.replaceChildren();
    const errEl = document.createElement("div");
    errEl.className = "empty-hint";
    errEl.textContent = err.message;
    body.appendChild(errEl);
  }
}

async function loadChatRadarForVideo(video, queryOverride = null) {
  if (!video) {
    state.chatRadarData = null;
    timelineEngine?.setChatRadar(null);
    renderChatRadarPopover();
    return;
  }
  const seq = ++state.chatRadarSeq;
  const outDir = encodeURIComponent(el("output-dir-field").value.trim());
  const q = queryOverride !== null ? queryOverride : state.chatKeywordQuery;
  const qParam = q && q.trim() ? `&q=${encodeURIComponent(q.trim())}` : "";
  try {
    const res = await apiFetch(
      `/api/chat/heatmap?stem=${encodeURIComponent(video.stem)}&output_dir=${outDir}${qParam}`
    );
    if (seq !== state.chatRadarSeq) return;
    state.chatRadarData = res && res.exists ? res : null;
  } catch {
    if (seq !== state.chatRadarSeq) return;
    state.chatRadarData = null;
  }
  timelineEngine?.setChatRadar(state.chatRadarData);
  renderChatRadarPopover();
}

async function importChatFileForActiveVideo(chatPath) {
  if (!state.activeVideo) {
    showToast(t("toast.selectVideoFirst"), true);
    return;
  }
  const cleanPath = String(chatPath || "").trim();
  if (!cleanPath) return;
  try {
    const outDir = el("output-dir-field")?.value.trim() || null;
    const res = await apiFetch("/api/chat/import", {
      method: "POST",
      body: JSON.stringify({
        stem: state.activeVideo.stem,
        chat_path: cleanPath,
        output_dir: outDir,
        bucket_sec: 5.0,
      }),
    });
    state.chatRadarData = res;
    state.chatKeywordQuery = "";
    if (el("chat-keyword-input")) el("chat-keyword-input").value = "";
    timelineEngine?.setChatRadar(res);
    renderChatRadarPopover();
    showToast(
      t("chat.importedToast", {
        count: Number(res.message_count || 0).toLocaleString(),
        peaks: (res.chat_peaks || []).length,
      }),
      false,
      true
    );
  } catch (err) {
    showToast(err.message, true);
  }
}

async function createClipFromChatPeak(peak) {
  if (!peak) return;
  const limit = getVideoDurationLimit();
  const sSec = Math.max(0, Number(peak.start_sec ?? peak.peak_sec ?? 0) - 8.0);
  const eSec = Math.min(limit, Math.max(Number(peak.end_sec ?? sSec + 15) + 5.0, sSec + 15.0));
  const terms = Array.isArray(peak.top_terms) && peak.top_terms.length ? peak.top_terms.slice(0, 3).join(" / ") : "Chat Spike";
  const title = `${terms} (${formatClock(peak.peak_sec ?? peak.start_sec)})`;
  await createManualClipsFromSpecs(
    [
      {
        start_sec: Number(sSec.toFixed(2)),
        end_sec: Number(eSec.toFixed(2)),
        title_tgt: title,
        title_src: title,
        summary_zh: Array.isArray(peak.sample_messages) ? peak.sample_messages.slice(0, 3).join(" | ") : terms,
        review_status: "reviewed",
        source: "chat_peak",
        tags: ["Chat"],
      },
    ],
    t("hist.act.createFromChat")
  );
}

function renderChatRadarPopover() {
  const btn = el("btn-tl-chat-radar");
  const tlChatCount = el("tl-chat-count");
  const statusBadge = el("chat-radar-status-badge");
  const kwCount = el("chat-keyword-count");
  const listEl = el("chat-radar-list");
  const data = state.chatRadarData;

  if (btn) btn.dataset.active = data ? "true" : "false";
  if (tlChatCount) {
    const peakCount = Array.isArray(data?.fusion_peaks) && data.fusion_peaks.length
      ? data.fusion_peaks.length
      : Array.isArray(data?.chat_peaks)
        ? data.chat_peaks.length
        : 0;
    tlChatCount.textContent = data ? String(peakCount || Number(data.message_count || 0)) : "0";
  }
  if (statusBadge) {
    statusBadge.textContent = data
      ? t("chat.loadedBadge", { count: Number(data.message_count || 0).toLocaleString() })
      : t("chat.notLoaded");
  }
  if (!listEl) return;
  listEl.replaceChildren();

  if (!data) {
    if (kwCount) kwCount.classList.add("hidden");
    const empty = document.createElement("div");
    empty.className = "empty-hint";
    empty.textContent = t("chat.emptyHint");
    listEl.appendChild(empty);
    return;
  }

  const searchRes = data.search;
  if (searchRes && (state.chatKeywordQuery || "").trim()) {
    if (kwCount) {
      kwCount.classList.remove("hidden");
      kwCount.textContent = String(searchRes.match_count || 0);
    }
    const topBuckets = Array.isArray(searchRes.top_buckets) ? searchRes.top_buckets : [];
    if (topBuckets.length === 0) {
      const empty = document.createElement("div");
      empty.className = "empty-hint";
      empty.textContent = t("chat.noKeywordHits");
      listEl.appendChild(empty);
      return;
    }
    for (const b of topBuckets) {
      const row = document.createElement("div");
      row.className = "chat-peak-item";

      const main = document.createElement("div");
      main.className = "chat-peak-main";
      const head = document.createElement("div");
      head.className = "chat-peak-head mono-num";
      head.textContent = `${formatClock(b.start_sec)} – ${formatClock(b.end_sec)} · ${t("chat.hitsCount", { count: b.hits })}`;
      const sub = document.createElement("div");
      sub.className = "chat-peak-terms";
      sub.textContent = searchRes.query;
      main.append(head, sub);

      const addBtn = document.createElement("button");
      addBtn.type = "button";
      addBtn.className = "nle-btn-xs";
      addBtn.textContent = t("tr.quickClipBtn");
      addBtn.addEventListener("click", (e) => {
        e.stopPropagation();
        createClipFromChatPeak({
          start_sec: b.start_sec,
          end_sec: b.end_sec,
          peak_sec: b.start_sec,
          top_terms: searchRes.terms || [searchRes.query],
        });
      });

      row.append(main, addBtn);
      row.addEventListener("click", () => {
        jumpMonitorToTime(b.start_sec, true);
        setStatusMessage(t("chat.jumpedPeak", { clock: formatClock(b.start_sec) }));
      });
      listEl.appendChild(row);
    }
    return;
  }

  if (kwCount) kwCount.classList.add("hidden");
  const displayPeaks =
    Array.isArray(data.fusion_peaks) && data.fusion_peaks.length > 0
      ? data.fusion_peaks
      : Array.isArray(data.chat_peaks)
        ? data.chat_peaks
        : [];
  if (displayPeaks.length === 0) {
    const empty = document.createElement("div");
    empty.className = "empty-hint";
    empty.textContent = t("chat.noPeaks");
    listEl.appendChild(empty);
    return;
  }

  for (const pk of displayPeaks.slice(0, 12)) {
    const row = document.createElement("div");
    row.className = "chat-peak-item";

    const main = document.createElement("div");
    main.className = "chat-peak-main";

    const head = document.createElement("div");
    head.className = "chat-peak-head mono-num";
    const srcCode = pk.fusion_source || "chat";
    const srcTag = document.createElement("span");
    srcTag.className = "chat-source-tag";
    srcTag.dataset.src = srcCode;
    srcTag.textContent = srcCode === "audio+chat" ? "A+C" : srcCode === "audio" ? "AUD" : "CHAT";

    const timeSpan = document.createElement("span");
    const peakSec = Number(pk.peak_sec ?? pk.start_sec ?? 0);
    const msgCnt = Number(pk.chat_msg_count ?? pk.msg_count ?? 0);
    timeSpan.textContent = `${formatClock(peakSec)} · ${msgCnt > 0 ? `${msgCnt} msg` : `${Number(pk.fusion_score || 0).toFixed(2)}`}`;
    head.append(srcTag, timeSpan);

    const termsEl = document.createElement("div");
    termsEl.className = "chat-peak-terms";
    const terms = Array.isArray(pk.top_terms) ? pk.top_terms : [];
    const samples = Array.isArray(pk.sample_messages) ? pk.sample_messages : [];
    termsEl.textContent = terms.length ? terms.join(" · ") : samples[0] || "-";
    termsEl.title = samples.join("\n") || termsEl.textContent;

    main.append(head, termsEl);

    const addBtn = document.createElement("button");
    addBtn.type = "button";
    addBtn.className = "nle-btn-xs";
    addBtn.textContent = t("tr.quickClipBtn");
    addBtn.title = t("tr.quickClipTitle");
    addBtn.addEventListener("click", (e) => {
      e.stopPropagation();
      createClipFromChatPeak(pk);
    });

    row.append(main, addBtn);
    row.addEventListener("click", () => {
      jumpMonitorToTime(peakSec, true);
      setStatusMessage(t("chat.jumpedPeak", { clock: formatClock(peakSec) }));
    });
    listEl.appendChild(row);
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

async function selectActiveVideo(video, { forceReload = false } = {}) {
  stopShuttle(true);
  const isDifferentVideo = Boolean(
    forceReload ||
      !state.activeVideo ||
      state.activeVideo.video_path !== video.video_path ||
      (video.cache_token && state.activeVideo.cache_token && state.activeVideo.cache_token !== video.cache_token)
  );

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
    state.onlySelected = false;
    state.clipSearchQuery = "";
    state.reviewFilter = "all";
    state.chapters = [];
    state.deadAirSegments = [];
    state.lastDeadAirSkipAt = 0;
    state.bypassDeadAirSeg = null;
    state.videoFps = 30;
    state.contextDialogSeq += 1;
    state.transcriptData = null;
    state.transcriptQuery = "";
    state.transcriptSelectedIds = new Set();
    state.transcriptLastClickedIdx = -1;
    state.chatRadarData = null;
    state.chatKeywordQuery = "";
    state.initialClipBounds = new Map();
    if (el("chk-only-selected")) el("chk-only-selected").checked = false;
    if (el("tl-fps-badge")) el("tl-fps-badge").textContent = "30fps";
    if (el("save-status-text")) el("save-status-text").textContent = t("ws.synced");
    // 修复 P1-08：切视频清空跨视频表单与旧媒体规格，防止张冠李戴导出错误 4K 原片
    if (el("override-video-path")) el("override-video-path").value = "";
    if (el("retranslate-context")) el("retranslate-context").value = "";
    if (el("retranslate-hotwords")) el("retranslate-hotwords").value = "";
    if (el("clip-search-input")) el("clip-search-input").value = "";
    if (el("sel-clip-review-filter")) el("sel-clip-review-filter").value = "all";
    if (el("transcript-search-input")) el("transcript-search-input").value = "";
    if (el("chat-keyword-input")) el("chat-keyword-input").value = "";
    if (el("chat-path-input")) el("chat-path-input").value = "";
    if (el("context-dialog")?.open) el("context-dialog").close();
    hidePopoverSafe(el("chat-radar-popover"));
    closeSubFindBar();
    state.highlightsData = null;
    state.highlightsSeq += 1;
    historyMgr?.clear();
    monitorEngine?.setMediaInfo(null);
    syncChaptersToolbarButton();
    renderChatRadarPopover();
    renderTranscriptPane();
    renderHighlightsList();
    renderActiveSubtitlesPane();
    renderClipInspector();
    syncMonitorClipContext();
    renderExportManifestSummary(null);
  }
  renderVideoList();

  el("hud-video-name").textContent = video.filename;
  el("hud-clip-title").textContent = "";

  const streamTag = el("tl-a1-stream-tag");
  if (streamTag) streamTag.textContent = `#${Number(video.audio_stream || 0)}`;

  const videoPlayer = el("review-video");
  const streamUrl = `/api/video-stream?path=${encodeURIComponent(video.video_path)}&v=${getMediaCacheVersion(video)}`;
  if (videoPlayer.getAttribute("src") !== streamUrl || forceReload) {
    videoPlayer.src = streamUrl;
    videoPlayer.load();
  }

  timelineEngine?.setMedia({
    stem: video.stem,
    mediaKey: `${video.video_path}::${video.cache_token || ""}`,
    duration: Number(video.total_duration_sec) || 0,
    fps: state.videoFps,
    resetView: isDifferentVideo,
  });

  loadAudioStreamsForVideo(video);
  loadMediaInfoForVideo(video);
  loadOverviewWaveformForVideo(video);
  loadChaptersForVideo(video);
  loadTranscriptForVideo(video);
  loadChatRadarForVideo(video);
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
function renderDiagnosticsBar(diag = state.activeVideo?.diagnostics) {
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
  const gradeCode = diag.grade || "A";
  gradeBadge.dataset.grade = gradeCode;
  const localizedGradeLabel = t(`diag.gradeLabel.${gradeCode}`);
  const localizedAdvice = t(`diag.advice.${gradeCode}`);
  gradeBadge.textContent = localizedGradeLabel;
  gradeBadge.title = localizedAdvice;

  const active = state.activeVideo;
  let audioMetaSuffix = "";
  if (active && active.vocal_bandpass !== null && active.vocal_bandpass !== undefined) {
    audioMetaSuffix = ` · #${Number(active.audio_stream || 0)}(${active.vocal_bandpass ? t("diag.bandpass") : t("diag.fullband")})`;
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
  el("diag-advice-text").textContent = localizedAdvice;
  el("diag-advice-text").title = localizedAdvice;

  const rec = diag.recommendation || {};
  const modeText = rec.asr_mode === "full" ? t("diag.fullMode") : `Top ${rec.top_k_peaks ?? 10}`;
  const recSummaryEl = el("diag-rec-summary");
  recSummaryEl.textContent = modeText;
  const recReasonCode = rec.reason_code || diag.rec_reason_code || "";
  if (recReasonCode) {
    recSummaryEl.title = t(`diag.recReason.${recReasonCode}`);
  } else {
    recSummaryEl.removeAttribute("title");
  }

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
    btn.title = t("diag.binTooltip", {
      label: b.label,
      strong: b.strong_peaks,
      total: b.total_peaks,
      clock: formatClock(b.start_sec),
    });

    const fill = document.createElement("span");
    fill.className = "heat-bar-fill";
    fill.style.height = `${Math.max(10, Number(b.heat_pct) || 0)}%`;

    const lbl = document.createElement("span");
    lbl.className = "heat-bar-label mono-num";
    lbl.textContent = b.label;

    btn.append(fill, lbl);
    btn.addEventListener("click", () => {
      jumpMonitorToTime(Number(b.start_sec) || 0, true);
      el("hud-clip-title").textContent = t("diag.strongPeaks", { label: b.label, strong: b.strong_peaks });
      setStatusMessage(t("diag.jumpedBin", { label: b.label, clock: formatClock(b.start_sec) }));
    });
    barsBox.appendChild(btn);
  }
}

function applyRecommendedParams() {
  const diag = state.activeVideo?.diagnostics;
  if (!diag || !diag.recommendation) {
    showToast(t("diag.scanFirst"), true);
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
  const desc = rec.asr_mode === "full" ? t("diag.recFull") : t("diag.recPeaks", { k: rec.top_k_peaks });
  setStatusMessage(t("diag.appliedRec", { desc }));
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

function renderHistoryPane(histState = historyMgr?.getState()) {
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
    empty.textContent = t("hist.empty");
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
      ? t("hist.currentTip", { label: entry.label })
      : t("hist.jumpTip", { idx: entry.index, label: entry.label });

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
          label: t("hist.ctx.jump", { idx: entry.index, label: entry.label }),
          disabled: entry.isCurrent,
          onSelect: () => performJumpHistory(entry.index),
        },
        { type: "separator" },
        { label: t("hist.ctx.undo"), hint: "Ctrl+Z", disabled: !canUndo, onSelect: () => performUndo() },
        { label: t("hist.ctx.redo"), hint: "Ctrl+Shift+Z", disabled: !canRedo, onSelect: () => performRedo() },
        { label: t("hist.ctx.revertV0"), disabled: cursor <= 0, onSelect: () => performJumpHistory(0) },
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
      const { tgtTitle } = resolveClipTitles(cur);
      el("hud-clip-title").textContent = `#${cur.rank} ${tgtTitle}`;
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
  setStatusMessage(`${actionWord} ${result.entry.label}`);
  return true;
}

function performUndo() {
  if (!historyMgr) return false;
  const res = historyMgr.undo();
  if (!res) {
    setStatusMessage(t("hist.atOldest"));
    return false;
  }
  return applyHistoryResult(res, t("hist.undone"));
}

function performRedo() {
  if (!historyMgr) return false;
  const res = historyMgr.redo();
  if (!res) {
    setStatusMessage(t("hist.atNewest"));
    return false;
  }
  return applyHistoryResult(res, t("hist.redone"));
}

function performJumpHistory(index) {
  if (!historyMgr) return false;
  const res = historyMgr.jumpTo(index);
  if (!res) return false;
  return applyHistoryResult(res, t("hist.jumped", { idx: index }));
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
  const seq = ++state.highlightsSeq;
  const targetVideoPath = video?.video_path || "";
  const outDir = encodeURIComponent(el("output-dir-field").value.trim());
  try {
    const data = await apiFetch(
      `/api/highlights?stem=${encodeURIComponent(video.stem)}&output_dir=${outDir}`
    );
    if (seq !== state.highlightsSeq || state.activeVideo?.video_path !== targetVideoPath) return;
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
          historyMgr.reset(snap, t("hist.initialLoad", { file: video.filename }));
        } else {
          historyMgr.commit(snap, t("hist.reloadProject", { file: video.filename }), { category: "batch" });
        }
      }
    }
    renderHighlightsList();
    syncLanguagePairUI(true);
    renderActiveSubtitlesPane();
    renderClipInspector();
    syncMonitorClipContext();
    if (resetResultBox) renderExportManifestSummary(data.clips_manifest);
  } catch {
    if (seq !== state.highlightsSeq || state.activeVideo?.video_path !== targetVideoPath) return;
    state.highlightsData = null;
    state.activeClipRank = null;
    historyMgr?.clear();
    renderHighlightsList();
    syncLanguagePairUI(true);
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
  renderTranscriptPane();
  renderClipInspector();
  syncMonitorClipContext();
  if (shouldPreview) {
    previewHighlightSegment(item);
  } else {
    const videoEl = el("review-video");
    if (
      videoEl?.src &&
      (videoEl.paused || (state.previewSegment && Number(state.previewSegment.rank) !== Number(item.rank)))
    ) {
      const targetSec = Number(item.start_sec) || 0;
      state.previewSegment = null;
      recordUserSeekDeadAirBypass(targetSec);
      videoEl.currentTime = targetSec;
      timelineEngine?.setPlayhead(targetSec);
      monitorEngine?.refreshFrameUI();
      syncVideoSubtitleOverlay();
    }
  }
}

function scheduleAutoSave() {
  el("save-status-text").textContent = t("ws.unsaved");
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
  el("save-status-text").textContent = t("ws.saving");
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
    el("save-status-text").textContent = t("ws.synced");
    if (state.activeVideo) {
      state.activeVideo.selected_count = res.selected_count;
      if (typeof res.reviewed_count === "number") {
        state.activeVideo.reviewed_count = res.reviewed_count;
      }
      renderVideoList();
    }
    updateStatusBarCounts();
    if (!silent) setStatusMessage(t("ws.savedSelected", { count: res.selected_count }));
  } catch (err) {
    el("save-status-text").textContent = t("ws.saveFailed");
    showToast(err.message, true);
  }
}

function getClipReviewStatus(item) {
  return String(item?.review_status || "draft").trim().toLowerCase() === "reviewed"
    ? "reviewed"
    : "draft";
}

function hasValidClipScore(item) {
  return (
    item != null &&
    item.score !== null &&
    item.score !== undefined &&
    item.score !== "" &&
    Number.isFinite(Number(item.score))
  );
}

function getClipSourceBadge(item) {
  const src = String(item?.source || "").trim().toLowerCase();
  if (src === "obs_chapter") return "OBS";
  if (src === "chat_peak") return "CHAT";
  if (src === "transcript") return "TR";
  if (src === "manual") return "MAN";
  return "—";
}

function applyScoreBadge(badgeEl, item) {
  if (!badgeEl) return;
  if (hasValidClipScore(item)) {
    const scoreVal = Number(item.score);
    badgeEl.textContent = scoreVal.toFixed(1);
    badgeEl.dataset.tier = scoreVal >= 9.0 ? "high" : "normal";
  } else {
    badgeEl.textContent = getClipSourceBadge(item);
    badgeEl.dataset.tier = "source";
  }
}

function deleteClipByRank(rank) {
  const list = state.highlightsData?.highlights;
  if (!Array.isArray(list) || rank == null) return false;
  const idx = list.findIndex((h) => Number(h.rank) === Number(rank));
  if (idx < 0) return false;

  const [removed] = list.splice(idx, 1);
  if (state.previewSegment && Number(state.previewSegment.rank) === Number(removed.rank)) {
    state.previewSegment = null;
    if (el("hud-clip-title")) el("hud-clip-title").textContent = "";
  }
  if (Number(state.activeClipRank) === Number(removed.rank)) {
    const nextItem = list[idx] || list[idx - 1] || null;
    state.activeClipRank = nextItem ? Number(nextItem.rank) : null;
  }
  if (state.activeVideo) {
    state.activeVideo.highlight_count = list.length;
    state.activeVideo.selected_count = list.filter((h) => h.selected !== false).length;
    state.activeVideo.reviewed_count = list.filter((h) => getClipReviewStatus(h) === "reviewed").length;
  }

  renderVideoList();
  renderHighlightsList();
  renderActiveSubtitlesPane();
  renderTranscriptPane();
  renderClipInspector();
  syncMonitorClipContext();
  syncVideoSubtitleOverlay();
  updateStatusBarCounts();
  recordHistory(t("hist.act.deleteClip", { rank: removed.rank }), "batch");
  scheduleAutoSave();
  setStatusMessage(t("clips.deletedStatus", { rank: removed.rank }));
  return true;
}

function formatReviewFlagLabel(flag) {
  const key = `clips.flag.${String(flag || "").trim()}`;
  const translated = t(key);
  return translated === key ? String(flag || "") : translated;
}

function toggleClipReviewStatus(item) {
  const target = item || getClipByRank(state.previewSegment?.rank ?? state.activeClipRank);
  if (!target) return;
  const nextStatus = getClipReviewStatus(target) === "reviewed" ? "draft" : "reviewed";
  target.review_status = nextStatus;
  if (state.reviewFilter !== "all") {
    renderHighlightsList();
  } else {
    syncHighlightRowStates();
  }
  renderClipInspector();
  syncTimelineData();
  recordHistory(
    t(nextStatus === "reviewed" ? "hist.act.markReviewed" : "hist.act.markDraft", { rank: target.rank }),
    "select"
  );
  scheduleAutoSave();
  setStatusMessage(
    t(nextStatus === "reviewed" ? "clips.markedReviewedStatus" : "clips.markedDraftStatus", {
      rank: target.rank,
    })
  );
}

function getDisplayHighlights() {
  if (!state.highlightsData || !Array.isArray(state.highlightsData.highlights)) return [];
  let list = [...state.highlightsData.highlights];
  if (state.onlySelected) {
    list = list.filter((h) => h.selected !== false);
  }
  if (state.reviewFilter === "reviewed") {
    list = list.filter((h) => getClipReviewStatus(h) === "reviewed");
  } else if (state.reviewFilter === "draft") {
    list = list.filter((h) => getClipReviewStatus(h) !== "reviewed");
  }
  const q = (state.clipSearchQuery || "").trim().toLowerCase();
  if (q) {
    list = list.filter((h) => {
      if (String(h.title_tgt || "").toLowerCase().includes(q)) return true;
      if (String(h.title_src || "").toLowerCase().includes(q)) return true;
      if (String(h.title_zh || "").toLowerCase().includes(q)) return true;
      if (String(h.title_en || "").toLowerCase().includes(q)) return true;
      if (String(h.summary_zh || "").toLowerCase().includes(q)) return true;
      if (Array.isArray(h.tags) && h.tags.some((tg) => String(tg).toLowerCase().includes(q))) return true;
      if (
        Array.isArray(h.subtitles) &&
        h.subtitles.some(
          (s) =>
            String(s.text_tgt || "").toLowerCase().includes(q) ||
            String(s.text_src || "").toLowerCase().includes(q) ||
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
  const selectedItems = allItems.filter((h) => h.selected !== false);
  const selectedTotal = selectedItems.length;
  const reviewedSelectedTotal = selectedItems.filter(
    (h) => getClipReviewStatus(h) === "reviewed"
  ).length;
  const onlyReviewed = Boolean(el("chk-only-reviewed")?.checked);
  const effectiveCount = onlyReviewed ? reviewedSelectedTotal : selectedTotal;
  el("selected-stats-badge").textContent = `${selectedTotal}/${allItems.length}`;
  el("btn-export-selected").textContent = onlyReviewed
    ? t("insp.export.exportReviewedBtn", { count: effectiveCount })
    : t("insp.export.exportBtn", { count: effectiveCount });
  el("btn-export-selected").disabled = effectiveCount === 0;
  const onlyRevHint = el("export-only-reviewed-hint");
  if (onlyRevHint) {
    onlyRevHint.classList.toggle("hidden", !(onlyReviewed && selectedTotal > 0 && reviewedSelectedTotal === 0));
  }
  updateStatusBarCounts();
}

function syncHighlightRowStates() {
  updateHighlightSelectionBadges();
  const allItems = state.highlightsData?.highlights || [];
  const byRank = new Map(allItems.map((h) => [Number(h.rank), h]));
  const lp = getActiveLanguagePair();

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
    const { tgtTitle } = resolveClipTitles(item, lp);
    if (spanTd) {
      spanTd.textContent = formatClock(item.start_sec);
      spanTd.title = `${formatClock(item.start_sec)}–${formatClock(item.end_sec)}`;
    }
    if (durTd) durTd.textContent = `${dur.toFixed(1)}s`;
    if (titleTd) {
      titleTd.textContent = tgtTitle;
      titleTd.title = tgtTitle;
    }
    if (subsTd) subsTd.textContent = String(Array.isArray(item.subtitles) ? item.subtitles.length : 0);
    const reviewBtn = row.querySelector(".review-pill");
    if (reviewBtn) {
      const st = getClipReviewStatus(item);
      reviewBtn.dataset.status = st;
      reviewBtn.textContent = st === "reviewed" ? t("clips.statusReviewed") : t("clips.statusDraft");
    }
  });
  syncTimelineData();
}

/** 渲染右侧检查器「片段」一等属性页 */
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

  const scoreBadge = el("clip-prop-score-badge");
  applyScoreBadge(scoreBadge, item);

  const reviewBtn = el("btn-clip-prop-review");
  if (reviewBtn) {
    const st = getClipReviewStatus(item);
    reviewBtn.dataset.status = st;
    reviewBtn.textContent = st === "reviewed" ? t("clips.statusReviewed") : t("clips.statusDraft");
  }
  const flagsRow = el("clip-prop-flags-row");
  const flagsBox = el("clip-prop-flags");
  const reviewFlags = Array.isArray(item.review_flags) ? item.review_flags : [];
  if (flagsRow && flagsBox) {
    flagsBox.replaceChildren();
    flagsRow.classList.toggle("hidden", reviewFlags.length === 0);
    for (const fl of reviewFlags) {
      const chip = document.createElement("span");
      chip.className = "review-flag-chip";
      chip.textContent = formatReviewFlagLabel(fl);
      flagsBox.appendChild(chip);
    }
  }

  const lp = getActiveLanguagePair();
  const { tgtTitle, srcTitle } = resolveClipTitles(item, lp);
  const titleZh = el("clip-prop-title-zh");
  const titleEn = el("clip-prop-title-en");
  const summaryEl = el("clip-prop-summary");
  if (document.activeElement !== titleZh) titleZh.value = tgtTitle;
  if (document.activeElement !== titleEn) titleEn.value = srcTitle;
  if (document.activeElement !== summaryEl) summaryEl.value = item.summary_zh || "";

  const dur = Math.max(0, Number(item.end_sec) - Number(item.start_sec));
  el("clip-prop-dur").textContent = `${dur.toFixed(1)}s`;

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
      title: t("insp.clip.inScrubTitle"),
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
        recordHistory(t("hist.act.setIn", { rank: cur.rank, tc: formatTimecode(cur.start_sec, true) }), "trim");
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
      title: t("insp.clip.outScrubTitle"),
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
        recordHistory(t("hist.act.setOut", { rank: cur.rank, tc: formatTimecode(cur.end_sec, true) }), "trim");
        scheduleAutoSave();
      },
    });
    clipEndScrubby.el.classList.add("scrubby-field-box");
    endSlot.replaceChildren(clipEndScrubby.el);
  }
  if (clipStartScrubby) clipStartScrubby.el.title = t("insp.clip.inScrubTitle");
  if (clipEndScrubby) clipEndScrubby.el.title = t("insp.clip.outScrubTitle");
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
    for (const tagItem of tags) {
      const chip = document.createElement("span");
      chip.className = "tag-chip";
      chip.textContent = String(tagItem);
      tagsBox.appendChild(chip);
    }
  }
}

/** 渲染 28px 高密度专业高光索引表 */
function renderHighlightsList() {
  const container = el("highlights-container");
  if (!container) return;
  const prevScrollTop = container.scrollTop;
  container.replaceChildren();

  updateHighlightSelectionBadges();

  if (!state.highlightsData) {
    const empty = document.createElement("div");
    empty.className = "empty-hint";
    empty.textContent = state.activeVideo ? t("clips.emptyNoData") : t("clips.emptyNoVideo");
    container.appendChild(empty);
    return;
  }

  const displayList = getDisplayHighlights();
  if (displayList.length === 0) {
    const empty = document.createElement("div");
    empty.className = "empty-hint";
    empty.textContent = t("clips.emptyNoMatch");
    container.appendChild(empty);
    return;
  }

  const lp = getActiveLanguagePair();
  const table = document.createElement("table");
  table.className = "clip-index-table";
  const thead = document.createElement("thead");
  const headTr = document.createElement("tr");
  const cols = [
    ["clip-col-chk", ""],
    ["clip-col-rank", "#"],
    ["clip-col-review", t("clips.colReview")],
    ["clip-col-score", t("clips.colScore")],
    ["clip-col-span", t("clips.colStart")],
    ["clip-col-dur", t("clips.colDur")],
    ["clip-col-title", t("clips.colTitle")],
    ["clip-col-subs", t("clips.colSubs")],
    ["clip-col-acts", ""],
  ];
  for (const [cls, txt] of cols) {
    const th = document.createElement("th");
    th.className = cls;
    th.textContent = txt;
    headTr.appendChild(th);
  }
  thead.appendChild(headTr);
  table.appendChild(thead);

  const tbody = document.createElement("tbody");

  for (const item of displayList) {
    const tr = document.createElement("tr");
    tr.dataset.rank = String(item.rank);
    if (item.selected === false) tr.classList.add("unselected");
    const isActive =
      (state.activeClipRank != null && Number(state.activeClipRank) === Number(item.rank)) ||
      (state.previewSegment && Number(state.previewSegment.rank) === Number(item.rank));
    if (isActive) tr.classList.add("is-active");
    const { tgtTitle } = resolveClipTitles(item, lp);

    const tdChk = document.createElement("td");
    tdChk.className = "clip-col-chk";
    const chk = document.createElement("input");
    chk.type = "checkbox";
    chk.className = "hl-select-chk";
    chk.checked = item.selected !== false;
    chk.title = t("clips.includeExport");
    chk.addEventListener("click", (e) => e.stopPropagation());
    chk.addEventListener("change", () => {
      item.selected = chk.checked;
      if (state.onlySelected) renderHighlightsList();
      else syncHighlightRowStates();
      if (Number(state.activeClipRank) === Number(item.rank)) renderClipInspector();
      recordHistory(
        t(item.selected ? "hist.act.check" : "hist.act.uncheck", { rank: item.rank }),
        "select"
      );
      scheduleAutoSave();
    });
    tdChk.appendChild(chk);

    const tdRank = document.createElement("td");
    tdRank.className = "clip-col-rank mono-num";
    tdRank.textContent = String(item.rank);

    const tdReview = document.createElement("td");
    tdReview.className = "clip-col-review";
    const reviewPill = document.createElement("button");
    reviewPill.type = "button";
    reviewPill.className = "review-pill";
    const rStatus = getClipReviewStatus(item);
    reviewPill.dataset.status = rStatus;
    reviewPill.textContent = rStatus === "reviewed" ? t("clips.statusReviewed") : t("clips.statusDraft");
    reviewPill.title = t("clips.toggleReviewTitle");
    reviewPill.addEventListener("click", (e) => {
      e.stopPropagation();
      toggleClipReviewStatus(item);
    });
    tdReview.appendChild(reviewPill);

    const tdScore = document.createElement("td");
    tdScore.className = "clip-col-score";
    const pill = document.createElement("span");
    pill.className = "score-pill mono-num";
    applyScoreBadge(pill, item);
    tdScore.appendChild(pill);

    const tdSpan = document.createElement("td");
    tdSpan.className = "clip-col-span mono-num";
    tdSpan.textContent = formatClock(item.start_sec);
    tdSpan.title = `${formatClock(item.start_sec)}–${formatClock(item.end_sec)}`;

    const tdDur = document.createElement("td");
    tdDur.className = "clip-col-dur mono-num";
    const dur = Math.max(0, Number(item.end_sec) - Number(item.start_sec));
    tdDur.textContent = `${dur.toFixed(1)}s`;

    const tdTitle = document.createElement("td");
    tdTitle.className = "clip-col-title";
    const reviewFlags = Array.isArray(item.review_flags) ? [...item.review_flags] : [];
    if (item.translation_status === "fallback" && !reviewFlags.includes("fallback_translation")) {
      reviewFlags.unshift("fallback_translation");
    }
    if (reviewFlags.length > 0) {
      const flagBadge = document.createElement("span");
      flagBadge.className = "review-flag-badge mono-num";
      flagBadge.textContent = `!${reviewFlags.length}`;
      flagBadge.title = reviewFlags.map((fl) => `• ${formatReviewFlagLabel(fl)}`).join("\n");
      tdTitle.appendChild(flagBadge);
    }
    const titleSpan = document.createElement("span");
    titleSpan.className = "clip-title-text";
    titleSpan.textContent = tgtTitle;
    titleSpan.title = `${tgtTitle}\n${item.summary_zh || ""}`.trim();
    tdTitle.appendChild(titleSpan);

    const tdSubs = document.createElement("td");
    tdSubs.className = "clip-col-subs mono-num";
    tdSubs.textContent = String(Array.isArray(item.subtitles) ? item.subtitles.length : 0);

    const tdActs = document.createElement("td");
    tdActs.className = "clip-col-acts";
    const actsWrap = document.createElement("div");
    actsWrap.className = "clip-row-acts";

    const btnPlay = document.createElement("button");
    btnPlay.type = "button";
    btnPlay.className = "clip-row-btn";
    btnPlay.title = t("clips.btnPlay");
    btnPlay.setAttribute("aria-label", t("clips.btnPlay"));
    btnPlay.appendChild(createSvgIcon("play", 12));
    btnPlay.addEventListener("click", (e) => {
      e.stopPropagation();
      previewHighlightSegment(item);
    });

    const btnRe = document.createElement("button");
    btnRe.type = "button";
    btnRe.className = "clip-row-btn";
    btnRe.title = t("clips.btnRetranslate");
    btnRe.setAttribute("aria-label", t("clips.btnRetranslate"));
    btnRe.appendChild(createSvgIcon("retranslate", 12));
    btnRe.addEventListener("click", (e) => {
      e.stopPropagation();
      openRetranslateDialog([item.rank], tgtTitle);
    });

    const btnExp = document.createElement("button");
    btnExp.type = "button";
    btnExp.className = "clip-row-btn";
    btnExp.title = t("clips.btnExportOne");
    btnExp.setAttribute("aria-label", t("clips.btnExportOne"));
    btnExp.appendChild(createSvgIcon("exportOne", 12));
    btnExp.addEventListener("click", (e) => {
      e.stopPropagation();
      triggerExport([item.rank]);
    });

    actsWrap.append(btnPlay, btnRe, btnExp);
    tdActs.appendChild(actsWrap);

    tr.append(tdChk, tdRank, tdReview, tdScore, tdSpan, tdDur, tdTitle, tdSubs, tdActs);

    tr.addEventListener("click", () => selectActiveClip(item, false));
    tr.addEventListener("dblclick", () => selectActiveClip(item, true));
    tr.addEventListener("contextmenu", (e) => {
      e.preventDefault();
      e.stopPropagation();
      selectActiveClip(item, false);
      ctxMenu?.open(e.clientX, e.clientY, [
        {
          label: t("clips.ctx.preview"),
          hint: t("clips.ctx.dblClickHint"),
          onSelect: () => previewHighlightSegment(item),
        },
        {
          label: t("clips.ctx.include"),
          hint: "D",
          checked: item.selected !== false,
          onSelect: () => {
            item.selected = item.selected === false;
            if (state.onlySelected) renderHighlightsList();
            else syncHighlightRowStates();
            renderClipInspector();
            recordHistory(
              t(item.selected ? "hist.act.check" : "hist.act.uncheck", { rank: item.rank }),
              "select"
            );
            scheduleAutoSave();
          },
        },
        {
          label: t("clips.ctx.toggleReview"),
          hint: "R",
          checked: getClipReviewStatus(item) === "reviewed",
          onSelect: () => toggleClipReviewStatus(item),
        },
        {
          label: t("sub.context30sBtn"),
          onSelect: () => openContext30sDialog(item),
        },
        { type: "separator" },
        {
          label: t("clips.ctx.editSubs"),
          onSelect: () => dock?.focusPanel("subtitles"),
        },
        {
          label: t("clips.ctx.editProps"),
          onSelect: () => {
            dock?.focusPanel("inspector");
            switchInspectorTab("clip");
          },
        },
        { type: "separator" },
        {
          label: t("clips.ctx.retranslate"),
          onSelect: () => openRetranslateDialog([item.rank], tgtTitle),
        },
        {
          label: t("clips.ctx.exportOne"),
          onSelect: () => triggerExport([item.rank]),
        },
        { type: "separator" },
        {
          label: t("clips.ctx.delete"),
          hint: "Delete",
          danger: true,
          onSelect: () => deleteClipByRank(item.rank),
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

/** 渲染独立「字幕」面板 */
function renderActiveSubtitlesPane() {
  const listEl = el("sub-pane-list");
  const clipLbl = el("sub-pane-clip-label");
  const countBadge = el("sub-pane-count");
  if (!listEl || !clipLbl || !countBadge) return;

  const prevScrollTop = listEl.scrollTop;
  const item = ensureActiveClipForSubtitles();
  listEl.replaceChildren();

  if (!item) {
    clipLbl.textContent = t("sub.noClip");
    countBadge.textContent = "0";
    const empty = document.createElement("div");
    empty.className = "empty-hint";
    empty.textContent = t("sub.emptySelectClip");
    listEl.appendChild(empty);
    return;
  }

  const lp = getActiveLanguagePair();
  const { tgtTitle } = resolveClipTitles(item, lp);
  const subs = Array.isArray(item.subtitles) ? item.subtitles : (item.subtitles = []);
  clipLbl.textContent = `#${item.rank} ${tgtTitle}`;
  clipLbl.title = clipLbl.textContent;
  countBadge.textContent = String(subs.length);

  if (subs.length === 0) {
    const empty = document.createElement("div");
    empty.className = "empty-hint";
    empty.textContent = t("sub.emptyNoLines");
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
      const lpCur = getActiveLanguagePair();
      const prev = subs[idx - 1];
      const prevPair = resolveSubPairTexts(prev, lpCur);
      const curPair = resolveSubPairTexts(s, lpCur);
      const tgtSep = lpCur.actualTgtMeta.cjk ? "" : " ";
      const srcSep = lpCur.srcMeta.cjk ? "" : " ";
      prev.end_sec = Number(Math.max(Number(prev.end_sec), Number(s.end_sec)).toFixed(2));
      writeSubPairTexts(
        prev,
        [prevPair.tgtText, curPair.tgtText].filter(Boolean).join(tgtSep),
        [prevPair.srcText, curPair.srcText].filter(Boolean).join(srcSep),
        lpCur
      );
      prev.text_orig = [String(prev.text_orig || "").trim(), String(s.text_orig || "").trim()]
        .filter(Boolean)
        .join(srcSep);
      subs.splice(idx, 1);
      renderActiveSubtitlesPane();
      syncHighlightRowStates();
      syncVideoSubtitleOverlay();
      syncTimelineData();
      recordHistory(t("hist.act.mergeSub", { rank: item.rank, idx: idx + 1 }), "sub");
      scheduleAutoSave();
    };

    const doSplit = () => {
      const videoEl = el("review-video");
      const curT = videoEl ? Number(videoEl.currentTime || 0) : 0;
      const st = Number(s.start_sec || 0);
      const ed = Number(s.end_sec || st + 1);
      const usePlayhead = curT > st + 0.25 && curT < ed - 0.25;
      splitSubtitleLineAtTime(item, idx, usePlayhead ? curT : null);
    };

    const doDelete = () => {
      subs.splice(idx, 1);
      renderActiveSubtitlesPane();
      syncHighlightRowStates();
      syncVideoSubtitleOverlay();
      recordHistory(t("hist.act.delSub", { rank: item.rank, idx: idx + 1 }), "sub");
      scheduleAutoSave();
    };

    const timeCol = document.createElement("div");
    timeCol.className = "sub-time-col";

    const tcLine = document.createElement("div");
    tcLine.className = "sub-tc-line";

    const jumpBtn = document.createElement("button");
    jumpBtn.type = "button";
    jumpBtn.className = "sub-jump-icon";
    jumpBtn.title = t("sub.jumpLineTitle");
    jumpBtn.setAttribute("aria-label", t("sub.jumpLineTitle"));
    jumpBtn.appendChild(createSvgIcon("play", 10));
    jumpBtn.addEventListener("click", () => jumpToSubtitleLine(item, s));

    const startScrub = createScrubbyControl({
      value: Number(s.start_sec || 0),
      min: Math.max(0, Number(item.start_sec) || 0),
      max: Number(item.end_sec) || 86400,
      step: 0.05,
      precision: 2,
      mode: "timecode",
      title: t("sub.startScrubTitle"),
      onChange: (val) => {
        if (val === null) return;
        s.start_sec = Number(Math.max(Number(item.start_sec) || 0, val).toFixed(2));
        if (Number(s.end_sec) <= s.start_sec) {
          s.end_sec = Number((s.start_sec + 0.5).toFixed(2));
          endScrub.setValue(s.end_sec);
        }
        syncVideoSubtitleOverlay();
        recordHistory(t("hist.act.subStart", { rank: item.rank, idx: idx + 1 }), "sub");
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
      title: t("sub.endScrubTitle"),
      onChange: (val) => {
        if (val === null) return;
        const minEnd = Number(s.start_sec || 0) + 0.25;
        const maxEnd = Math.max(minEnd, Number(item.end_sec) || 86400);
        s.end_sec = Number(clampNum(val, minEnd, maxEnd).toFixed(2));
        endScrub.setValue(s.end_sec);
        syncVideoSubtitleOverlay();
        recordHistory(t("hist.act.subEnd", { rank: item.rank, idx: idx + 1 }), "sub");
        scheduleAutoSave();
      },
    });

    tcLine.append(jumpBtn, startScrub.el, sep, endScrub.el);
    timeCol.appendChild(tcLine);

    const pairTexts = resolveSubPairTexts(s, lp);

    // 若存在 ASR 原文误听纠错差异，对比源语言原文与 ASR 原文
    const origRaw = (s.text_orig || "").trim();
    const srcClean = pairTexts.srcText;
    if (origRaw && srcClean && origRaw.toLowerCase() !== srcClean.toLowerCase()) {
      const corr = document.createElement("span");
      corr.className = "sub-corr-tag";
      corr.textContent = t("sub.asrCorrected");
      corr.title = t("sub.asrCorrectedTip", { raw: origRaw, clean: srcClean });
      timeCol.appendChild(corr);
    }

    const subDur = Math.max(0, Number(s.end_sec || 0) - Number(s.start_sec || 0));
    const tgtLen = pairTexts.tgtText.length;
    const maxLenLimit = lp.actualTgtMeta.cjk ? 18 : 44;
    if (tgtLen > maxLenLimit || subDur < 0.7 || subDur > 6.5) {
      const warn = document.createElement("span");
      warn.className = "sub-warn-tag mono-num";
      const reasons = [];
      if (tgtLen > maxLenLimit) {
        reasons.push(t("sub.paceLenWarn", { lang: lp.actualTgtMeta.name, len: tgtLen, max: maxLenLimit }));
      }
      if (subDur < 0.7) reasons.push(t("sub.paceShortWarn", { dur: subDur.toFixed(2) }));
      if (subDur > 6.5) reasons.push(t("sub.paceLongWarn", { dur: subDur.toFixed(1) }));
      warn.textContent = tgtLen > maxLenLimit ? t("sub.paceCharBadge", { len: tgtLen }) : `${subDur.toFixed(1)}s`;
      warn.title = t("sub.paceTitle", { reasons: reasons.join(" · ") });
      timeCol.appendChild(warn);
    }

    const textCol = document.createElement("div");
    textCol.className = "sub-text-col";

    const inZh = document.createElement("input");
    inZh.type = "text";
    inZh.className = "sub-input-zh";
    inZh.value = pairTexts.tgtText;
    inZh.placeholder = lp.isSameLang
      ? t("sub.tgtSamePlaceholder", { name: lp.actualTgtMeta.name, code: lp.actualTgtMeta.code })
      : t("sub.tgtPlaceholder", { name: lp.actualTgtMeta.name, code: lp.actualTgtMeta.code });
    inZh.addEventListener("input", () => {
      const val = inZh.value;
      s.text_tgt = val;
      if (lp.isZhToEn) s.text_en = val;
      else s.text_zh = val;
      syncVideoSubtitleOverlay();
      recordHistory(t("hist.act.subTgt", { rank: item.rank, idx: idx + 1 }), "sub", `sub_zh_${item.rank}_${idx}`);
      scheduleAutoSave();
    });

    const inEn = document.createElement("input");
    inEn.type = "text";
    inEn.className = "sub-input-en";
    inEn.value = pairTexts.srcText;
    inEn.placeholder = t("sub.srcPlaceholder", { name: lp.srcMeta.name, code: lp.srcMeta.code });
    inEn.addEventListener("input", () => {
      const val = inEn.value;
      s.text_src = val;
      if (lp.isZhToEn) s.text_zh = val;
      else s.text_en = val;
      syncVideoSubtitleOverlay();
      recordHistory(t("hist.act.subSrc", { rank: item.rank, idx: idx + 1 }), "sub", `sub_en_${item.rank}_${idx}`);
      scheduleAutoSave();
    });

    textCol.append(inZh, inEn);

    const opsCol = document.createElement("div");
    opsCol.className = "sub-ops-col";

    if (idx > 0) {
      const btnMerge = document.createElement("button");
      btnMerge.type = "button";
      btnMerge.className = "sub-op-btn";
      btnMerge.title = t("sub.btnMergeTitle");
      btnMerge.setAttribute("aria-label", t("sub.btnMergeTitle"));
      btnMerge.appendChild(createSvgIcon("mergeUp", 12));
      btnMerge.addEventListener("click", doMergeUp);
      opsCol.appendChild(btnMerge);
    }

    const btnSplit = document.createElement("button");
    btnSplit.type = "button";
    btnSplit.className = "sub-op-btn";
    btnSplit.title = t("sub.btnSplitTitle");
    btnSplit.setAttribute("aria-label", t("sub.btnSplitTitle"));
    btnSplit.appendChild(createSvgIcon("split", 12));
    btnSplit.addEventListener("click", doSplit);

    const btnDel = document.createElement("button");
    btnDel.type = "button";
    btnDel.className = "sub-op-btn danger";
    btnDel.title = t("sub.btnDeleteTitle");
    btnDel.setAttribute("aria-label", t("sub.btnDeleteTitle"));
    btnDel.appendChild(createSvgIcon("trash", 12));
    btnDel.addEventListener("click", doDelete);

    opsCol.append(btnSplit, btnDel);
    row.append(timeCol, textCol, opsCol);

    row.addEventListener("contextmenu", (e) => {
      if (e.target && e.target.closest("input, textarea")) return;
      e.preventDefault();
      e.stopPropagation();
      ctxMenu?.open(e.clientX, e.clientY, [
        { label: t("sub.jumpLineTitle"), onSelect: () => jumpToSubtitleLine(item, s) },
        { type: "separator" },
        {
          label: t("sub.ctx.snapPrev"),
          disabled: idx === 0,
          onSelect: () => {
            if (idx <= 0) return;
            const prevEnd = Number(subs[idx - 1].end_sec || 0);
            if (prevEnd < Number(s.end_sec || 0) - 0.25) {
              s.start_sec = Number(prevEnd.toFixed(2));
              renderActiveSubtitlesPane();
              syncVideoSubtitleOverlay();
              syncMonitorClipContext();
              recordHistory(t("hist.act.snapSub", { rank: item.rank, idx: idx + 1 }), "sub");
              scheduleAutoSave();
            }
          },
        },
        { label: t("sub.btnMergeTitle"), disabled: idx === 0, onSelect: doMergeUp },
        { label: t("sub.ctx.split"), onSelect: doSplit },
        { type: "separator" },
        { label: t("sub.btnDeleteTitle"), danger: true, onSelect: doDelete },
      ]);
    });

    listEl.appendChild(row);
  });
  listEl.scrollTop = prevScrollTop;
}

/* ==================== 播放器控制、实时 6×6 多语种字幕叠层 ==================== */
function findSubtitleAtTime(tSec) {
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
      if (tSec >= Number(s.start_sec) - 0.05 && tSec <= Number(s.end_sec) + 0.08) {
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

  const mode = normalizeOverlaySubMode(state.overlaySubMode);
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
  if (mode === "target_only") {
    zhEl.textContent = tgtText;
    enEl.textContent = "";
  } else if (mode === "source_only") {
    zhEl.textContent = srcText;
    enEl.textContent = "";
  } else if (mode === "bilingual_src_top") {
    zhEl.textContent = srcText;
    enEl.textContent = tgtText && tgtText !== srcText ? tgtText : "";
  } else {
    zhEl.textContent = tgtText;
    enEl.textContent = srcText && srcText !== tgtText ? srcText : "";
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
  const { tgtTitle } = resolveClipTitles(item);
  el("hud-clip-title").textContent = `#${item.rank} ${tgtTitle}`;

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
  const { tgtTitle } = resolveClipTitles(item);
  el("hud-clip-title").textContent = `#${item.rank} ${tgtTitle}`;
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
    setStatusMessage(t("toast.selectClipFirst"), true);
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
      setStatusMessage(t("toast.inAfterOut"), true);
      return;
    }
    target.start_sec = now;
    target.start_str = formatClock(now);
    state.previewSegment.start_sec = now;
    setStatusMessage(t("toast.setInStatus", { rank: target.rank, tc: formatTimecode(now, true) }));
  } else {
    if (now <= Number(target.start_sec) + 0.5) {
      setStatusMessage(t("toast.outBeforeIn"), true);
      return;
    }
    target.end_sec = now;
    target.end_str = formatClock(now);
    state.previewSegment.end_sec = now;
    setStatusMessage(t("toast.setOutStatus", { rank: target.rank, tc: formatTimecode(now, true) }));
  }
  target.duration_sec = Number(
    Math.max(0, Number(target.end_sec) - Number(target.start_sec)).toFixed(2)
  );
  syncHighlightRowStates();
  renderClipInspector();
  renderActiveSubtitlesPane();
  syncMonitorClipContext();
  recordHistory(
    t(edge === "in" ? "hist.act.setIn" : "hist.act.setOut", { rank: target.rank, tc: formatTimecode(now, true) }),
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
      setStatusMessage(t("mon.shuttleStart"));
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
    if (spd > 1) setStatusMessage(t("mon.shuttleFwd", { spd }));
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
    setStatusMessage(t("mon.shuttleRev", { spd }));
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
  recordUserSeekDeadAirBypass(nextT);
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
  recordUserSeekDeadAirBypass(nextT);
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
  const { tgtTitle } = resolveClipTitles(target);
  setStatusMessage(t("toast.selectedClip", { rank: target.rank, title: tgtTitle }));
}

function insertSubtitleAtPlayhead() {
  const item = ensureActiveClipForSubtitles();
  if (!item) {
    setStatusMessage(t("toast.selectClipFirst"), true);
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
    text_tgt: "",
    text_src: "",
    text_zh: "",
    text_en: "",
  });
  subs.sort((a, b) => Number(a.start_sec) - Number(b.start_sec));
  renderActiveSubtitlesPane();
  syncHighlightRowStates();
  recordHistory(t("hist.act.insertSub", { rank: item.rank }), "sub");
  scheduleAutoSave();
  setStatusMessage(t("sub.inserted", { tc: formatTimecode(startSec, true) }));
}

function splitSubtitleLineAtTime(item, subIndex, cutSec = null) {
  if (!item || !Array.isArray(item.subtitles)) return false;
  const s = item.subtitles[subIndex];
  if (!s) return false;

  const st = Number(s.start_sec || 0);
  const ed = Number(s.end_sec || st + 1);
  if (ed - st < 0.6) {
    setStatusMessage(t("sub.tooShortToSplit"), true);
    return false;
  }
  const hasCut = cutSec !== null && cutSec > st + 0.25 && cutSec < ed - 0.25;
  const mid = Number((hasCut ? cutSec : (st + ed) / 2).toFixed(2));
  const ratio = clampNum((mid - st) / Math.max(0.1, ed - st), 0.15, 0.85);

  const lp = getActiveLanguagePair();
  const { tgtText, srcText } = resolveSubPairTexts(s, lp);
  const [tgtA, tgtB] = splitTextByRatio(tgtText, ratio, lp.actualTgtMeta.cjk);
  const [srcA, srcB] = splitTextByRatio(srcText, ratio, lp.srcMeta.cjk);
  const [origA, origB] = splitTextByRatio(s.text_orig || srcText, ratio, lp.srcMeta.cjk);

  if (!tgtB && !srcB) {
    setStatusMessage(t("sub.textTooShortToSplit"), true);
    return false;
  }

  const secondHalf = {
    start_sec: mid,
    end_sec: Number(ed.toFixed(2)),
    text_orig: origB,
  };
  writeSubPairTexts(secondHalf, tgtB, srcB, lp);

  s.end_sec = mid;
  s.text_orig = origA;
  writeSubPairTexts(s, tgtA, srcA, lp);
  item.subtitles.splice(subIndex + 1, 0, secondHalf);

  state.activeClipRank = Number(item.rank);
  renderActiveSubtitlesPane();
  syncHighlightRowStates();
  syncVideoSubtitleOverlay();
  syncTimelineData();
  syncMonitorClipContext();
  recordHistory(t("hist.act.splitSub", { rank: item.rank, idx: subIndex + 1 }), "sub");
  scheduleAutoSave();
  setStatusMessage(t("sub.splitDone", { tc: formatTimecode(mid, true) }));
  return true;
}

function splitSubtitleAtPlayhead({ silent = false } = {}) {
  const videoEl = el("review-video");
  if (!videoEl || !state.highlightsData) return false;
  const curT = Number(videoEl.currentTime || 0);
  const hit = findSubtitleAtTime(curT);
  if (!hit) {
    if (!silent) setStatusMessage(t("sub.noHitAtPlayhead"), true);
    return false;
  }
  const item = getClipByRank(hit.rank);
  if (!item) return false;
  return Boolean(splitSubtitleLineAtTime(item, hit.index, curT));
}

function smartSplitAtPlayhead() {
  if (splitSubtitleAtPlayhead({ silent: true })) return true;
  if (splitActiveClipAtPlayhead({ silent: true })) return true;
  setStatusMessage(t("toast.splitFallbackHint"), true);
  return false;
}

/* ==================== 本地达芬奇 21.1 逆向 P0 剪辑/标记/索引/字幕查找替换动作 ==================== */
function resetClipBoundary(which = "both") {
  const target = getClipByRank(state.previewSegment?.rank ?? state.activeClipRank);
  if (!target) {
    setStatusMessage(t("toast.selectClipFirst"), true);
    return;
  }
  const orig = state.initialClipBounds.get(Number(target.rank));
  if (!orig) {
    setStatusMessage(t("toast.noOriginalBounds"), true);
    return;
  }
  const curS = Number(target.start_sec || 0);
  const curE = Number(target.end_sec || 0);
  let nextS = curS;
  let nextE = curE;
  if (which === "in" || which === "both") nextS = orig.start_sec;
  if (which === "out" || which === "both") nextE = orig.end_sec;
  if (nextE <= nextS + 0.5) {
    setStatusMessage(t("toast.resetBoundsInvalid"), true);
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
  const histKey = which === "in" ? "hist.act.resetIn" : which === "out" ? "hist.act.resetOut" : "hist.act.resetBoth";
  const whichLbl =
    which === "in" ? t("menu.mark.clearIn") : which === "out" ? t("menu.mark.clearOut") : t("menu.mark.clearIO");
  recordHistory(t(histKey, { rank: target.rank }), "trim");
  scheduleAutoSave();
  setStatusMessage(t("toast.resetBoundsDone", { rank: target.rank, which: whichLbl }));
}

function trimClipToPlayhead(edge) {
  const videoEl = el("review-video");
  const target = getClipByRank(state.previewSegment?.rank ?? state.activeClipRank);
  if (!videoEl || !target) {
    setStatusMessage(t("toast.selectClipFirst"), true);
    return;
  }
  const curT = Number((videoEl.currentTime || 0).toFixed(2));
  if (edge === "start") {
    if (curT < Number(target.start_sec) || curT >= Number(target.end_sec) - 1.0) {
      setStatusMessage(t("toast.playheadInsideStart"), true);
      return;
    }
    target.start_sec = curT;
    target.start_str = formatClock(curT);
  } else {
    if (curT <= Number(target.start_sec) + 1.0 || curT > Number(target.end_sec)) {
      setStatusMessage(t("toast.playheadInsideEnd"), true);
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
  const tcStr = formatTimecode(curT, true);
  recordHistory(
    t(edge === "start" ? "hist.act.trimStart" : "hist.act.trimEnd", { rank: target.rank, tc: tcStr }),
    "trim"
  );
  scheduleAutoSave();
  setStatusMessage(t(edge === "start" ? "toast.trimmedStart" : "toast.trimmedEnd", { rank: target.rank, tc: tcStr }));
}

function splitActiveClipAtPlayhead({ silent = false } = {}) {
  const videoEl = el("review-video");
  const list = state.highlightsData?.highlights;
  const target = getClipByRank(state.previewSegment?.rank ?? state.activeClipRank);
  if (!videoEl || !Array.isArray(list) || !target) {
    if (!silent) setStatusMessage(t("toast.selectClipFirst"), true);
    return false;
  }
  const curT = Number((videoEl.currentTime || 0).toFixed(2));
  const st = Number(target.start_sec || 0);
  const ed = Number(target.end_sec || 0);
  if (curT <= st + 1.0 || curT >= ed - 1.0) {
    if (!silent) setStatusMessage(t("toast.splitClipNeedMargin"), true);
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

  const lp = getActiveLanguagePair();
  const { tgtTitle } = resolveClipTitles(target, lp);
  const part2Title = `${tgtTitle}${t("toast.splitClipPart2Suffix")}`.trim();
  const maxRank = list.reduce((m, h) => Math.max(m, Number(h.rank) || 0), 0);
  const rightClip = {
    ...JSON.parse(JSON.stringify(target)),
    rank: maxRank + 1,
    start_sec: curT,
    end_sec: ed,
    start_str: formatClock(curT),
    end_str: formatClock(ed),
    duration_sec: Number((ed - curT).toFixed(2)),
    subtitles: rightSubs,
  };
  writeClipTitleTgt(rightClip, part2Title, lp);

  target.end_sec = curT;
  target.end_str = formatClock(curT);
  target.duration_sec = Number((curT - st).toFixed(2));
  target.subtitles = leftSubs;
  if (state.previewSegment && Number(state.previewSegment.rank) === Number(target.rank)) {
    state.previewSegment.end_sec = curT;
  }

  const idx = list.indexOf(target);
  list.splice(idx + 1, 0, rightClip);
  state.initialClipBounds.set(rightClip.rank, { start_sec: rightClip.start_sec, end_sec: rightClip.end_sec });

  renderHighlightsList();
  renderActiveSubtitlesPane();
  renderClipInspector();
  syncMonitorClipContext();
  recordHistory(t("hist.act.splitClip", { left: target.rank, right: rightClip.rank }), "trim");
  scheduleAutoSave();
  setStatusMessage(t("toast.splitClipDone", { tc: formatTimecode(curT, true), left: target.rank, right: rightClip.rank }));
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
  recordHistory(
    t(target.selected ? "hist.act.enableClip" : "hist.act.disableClip", { rank: target.rank }),
    "select"
  );
  scheduleAutoSave();
  setStatusMessage(t(target.selected ? "toast.clipEnabled" : "toast.clipDisabled", { rank: target.rank }));
}

function exportClipsIndexCsv() {
  const list = getDisplayHighlights();
  if (!list.length) {
    showToast(t("toast.noClipsForCsv"), true);
    return;
  }
  const lp = getActiveLanguagePair();
  const esc = (v) => {
    let s = String(v ?? "");
    if (/^[\s]*[=+\-@\t\r]/.test(s)) {
      s = `'${s}`;
    }
    return `"${s.replace(/"/g, '""')}"`;
  };
  const rows = [
    ["Rank", "Selected", "Score", "Start", "End", "Duration(s)", `Title_${lp.actualTgtMeta.code}`, `Title_${lp.srcMeta.code}`, "Subtitles", "Summary"],
  ];
  for (const h of list) {
    const dur = Math.max(0, Number(h.end_sec || 0) - Number(h.start_sec || 0));
    const { tgtTitle, srcTitle } = resolveClipTitles(h, lp);
    const scoreCell = hasValidClipScore(h) ? Number(h.score).toFixed(1) : getClipSourceBadge(h);
    rows.push([
      h.rank,
      h.selected !== false ? "YES" : "NO",
      scoreCell,
      formatClock(h.start_sec),
      formatClock(h.end_sec),
      dur.toFixed(2),
      tgtTitle,
      srcTitle,
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
  setStatusMessage(t("toast.csvExported", { count: list.length }));
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
  const lp = getActiveLanguagePair();
  for (const h of state.highlightsData.highlights) {
    const subs = Array.isArray(h.subtitles) ? h.subtitles : [];
    subs.forEach((s, idx) => {
      const { tgtText, srcText } = resolveSubPairTexts(s, lp);
      if (tgtText.toLowerCase().includes(q) || srcText.toLowerCase().includes(q)) {
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
  const lp = getActiveLanguagePair();
  const pair = resolveSubPairTexts(sub, lp);
  const rTgt = replaceTextCaseInsensitive(pair.tgtText, findStr, repStr);
  const rSrc = replaceTextCaseInsensitive(pair.srcText, findStr, repStr);
  const total = rTgt.count + rSrc.count;
  if (total === 0) return;
  writeSubPairTexts(sub, rTgt.text, rSrc.text, lp);
  renderActiveSubtitlesPane();
  syncVideoSubtitleOverlay();
  syncTimelineData();
  recordHistory(t("hist.act.replaceSub", { from: findStr, to: repStr }), "sub");
  scheduleAutoSave();
  runSubFind(true);
  setStatusMessage(t("sub.replacedCount", { count: total }));
}

function replaceAllSubMatches() {
  const findStr = (el("sub-find-input")?.value || "").trim();
  const repStr = el("sub-replace-input")?.value ?? "";
  if (!findStr || !state.highlightsData?.highlights) return;
  const lp = getActiveLanguagePair();
  let total = 0;
  for (const h of state.highlightsData.highlights) {
    for (const s of h.subtitles || []) {
      const pair = resolveSubPairTexts(s, lp);
      const rTgt = replaceTextCaseInsensitive(pair.tgtText, findStr, repStr);
      const rSrc = replaceTextCaseInsensitive(pair.srcText, findStr, repStr);
      if (rTgt.count > 0 || rSrc.count > 0) {
        writeSubPairTexts(s, rTgt.text, rSrc.text, lp);
      }
      total += rTgt.count + rSrc.count;
    }
  }
  if (total === 0) {
    setStatusMessage(t("sub.notFound", { query: findStr }), true);
    return;
  }
  renderActiveSubtitlesPane();
  syncVideoSubtitleOverlay();
  syncTimelineData();
  recordHistory(t("hist.act.replaceAllSub", { from: findStr, to: repStr, count: total }), "sub");
  scheduleAutoSave();
  runSubFind(false);
  setStatusMessage(t("sub.replacedAll", { count: total }));
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
      getLabel: () => t("menu.file"),
      getItems: () => [
        { label: t("menu.file.refresh"), onSelect: () => persistDirsAndScan() },
        {
          label: t("menu.file.switchDirs"),
          onSelect: () => {
            const btn = el("btn-dirs-popover");
            btn?.click();
          },
        },
        { type: "separator" },
        {
          label: t("menu.file.save"),
          hint: "Ctrl+S",
          disabled: !state.highlightsData,
          onSelect: () => saveHighlightsNow(false),
        },
        {
          label: t("menu.file.exportCsv"),
          disabled: !state.highlightsData,
          onSelect: () => exportClipsIndexCsv(),
        },
        {
          label: t("menu.file.exportSelected"),
          hint: "Ctrl+E",
          disabled: !state.highlightsData,
          onSelect: () => triggerExport(null),
        },
        {
          label: t("menu.file.openOutDir"),
          onSelect: () => el("btn-open-clips-dir")?.click(),
        },
        { type: "separator" },
        {
          label: t("menu.file.settings"),
          hint: "Ctrl+,",
          onSelect: () => el("settings-dialog")?.showModal?.(),
        },
      ],
    },
    {
      id: "edit",
      getLabel: () => t("menu.edit"),
      getItems: () => {
        const hs = histSnap();
        return [
          { label: t("menu.edit.undo"), hint: "Ctrl+Z", disabled: !hs.canUndo, onSelect: () => performUndo() },
          { label: t("menu.edit.redo"), hint: "Ctrl+Shift+Z", disabled: !hs.canRedo, onSelect: () => performRedo() },
          { label: t("menu.edit.history"), hint: "Ctrl+H", onSelect: () => dock?.focusPanel("history") },
          {
            label: t("menu.edit.revert"),
            disabled: hs.cursor <= 0,
            onSelect: () => performJumpHistory(0),
          },
          { type: "separator" },
          {
            label: t("menu.edit.selectAll"),
            disabled: !state.highlightsData,
            onSelect: () => el("btn-hl-select-all")?.click(),
          },
          {
            label: t("menu.edit.clearAll"),
            disabled: !state.highlightsData,
            onSelect: () => el("btn-hl-unselect-all")?.click(),
          },
          { type: "separator" },
          {
            label: t("menu.edit.findSub"),
            hint: "Ctrl+F",
            disabled: !state.highlightsData,
            onSelect: () => openSubFindBar(false),
          },
        ];
      },
    },
    {
      id: "trim",
      getLabel: () => t("menu.trim"),
      getItems: () => [
        {
          label: t("menu.trim.selectMode"),
          hint: "V",
          checked: timelineEngine?.tool === "select",
          onSelect: () => timelineEngine?.setTool("select"),
        },
        {
          label: t("menu.trim.razorMode"),
          hint: "B",
          checked: timelineEngine?.tool === "razor",
          onSelect: () => timelineEngine?.setTool("razor"),
        },
        { type: "separator" },
        {
          label: t("menu.trim.trimStart"),
          hint: "Shift+[",
          disabled: !hasClip(),
          onSelect: () => trimClipToPlayhead("start"),
        },
        {
          label: t("menu.trim.trimEnd"),
          hint: "Shift+]",
          disabled: !hasClip(),
          onSelect: () => trimClipToPlayhead("end"),
        },
      ],
    },
    {
      id: "timeline",
      getLabel: () => t("menu.timeline"),
      getItems: () => [
        {
          label: t("menu.timeline.splitSub"),
          hint: "C / Ctrl+\\",
          disabled: !state.highlightsData,
          onSelect: () => splitSubtitleAtPlayhead(),
        },
        {
          label: t("menu.timeline.splitClip"),
          hint: "Ctrl+Shift+\\",
          disabled: !hasClip(),
          onSelect: () => splitActiveClipAtPlayhead(),
        },
        {
          label: t("menu.timeline.insertSub"),
          hint: "Alt+N",
          disabled: !hasClip(),
          onSelect: () => insertSubtitleAtPlayhead(),
        },
        { type: "separator" },
        {
          label: t("menu.timeline.snapping"),
          hint: "N",
          checked: Boolean(timelineEngine?.snapEnabled),
          onSelect: () => timelineEngine?.setSnap(!timelineEngine.snapEnabled),
        },
        {
          label: t("menu.timeline.fullWave"),
          checked: Boolean(timelineEngine?.viewOptions?.fullWaveform),
          onSelect: () => {
            const next = !timelineEngine?.viewOptions?.fullWaveform;
            if (el("chk-tl-opt-full-wave")) el("chk-tl-opt-full-wave").checked = next;
            timelineEngine?.setViewOptions({ fullWaveform: next });
          },
        },
        { type: "separator" },
        { label: t("menu.timeline.fitAll"), hint: "Shift+Z", onSelect: () => timelineEngine?.fitAll() },
        {
          label: t("menu.timeline.fitClip"),
          disabled: !hasClip(),
          onSelect: () => el("btn-tl-fit-clip")?.click(),
        },
        { label: t("menu.timeline.zoomIn"), hint: "=", onSelect: () => timelineEngine?.zoomByFactor(0.65) },
        { label: t("menu.timeline.zoomOut"), hint: "-", onSelect: () => timelineEngine?.zoomByFactor(1.5) },
      ],
    },
    {
      id: "clip",
      getLabel: () => t("menu.clip"),
      getItems: () => {
        const cur = getClipByRank(state.previewSegment?.rank ?? state.activeClipRank);
        return [
          {
            label: t("menu.clip.toggleExport"),
            hint: "D",
            checked: Boolean(cur && cur.selected !== false),
            disabled: !cur,
            onSelect: () => toggleActiveClipEnabled(),
          },
          {
            label: t("clips.ctx.toggleReview"),
            hint: "R",
            checked: Boolean(cur && getClipReviewStatus(cur) === "reviewed"),
            disabled: !cur,
            onSelect: () => cur && toggleClipReviewStatus(cur),
          },
          {
            label: t("sub.context30sBtn"),
            disabled: !cur,
            onSelect: () => cur && openContext30sDialog(cur),
          },
          {
            label: t("menu.clip.preview"),
            hint: "Enter",
            disabled: !cur,
            onSelect: () => cur && previewHighlightSegment(cur),
          },
          { type: "separator" },
          {
            label: t("menu.clip.retranslate"),
            disabled: !cur,
            onSelect: () => cur && openRetranslateDialog([cur.rank], resolveClipTitles(cur).tgtTitle),
          },
          {
            label: t("menu.clip.exportOne"),
            disabled: !cur,
            onSelect: () => cur && triggerExport([cur.rank]),
          },
          { type: "separator" },
          {
            label: t("menu.clip.resetIO"),
            hint: "Alt+X",
            disabled: !cur,
            onSelect: () => resetClipBoundary("both"),
          },
          { type: "separator" },
          {
            label: t("menu.clip.delete"),
            hint: "Delete",
            danger: true,
            disabled: !cur,
            onSelect: () => cur && deleteClipByRank(cur.rank),
          },
        ];
      },
    },
    {
      id: "mark",
      getLabel: () => t("menu.mark"),
      getItems: () => [
        { label: t("menu.mark.in"), hint: "I", disabled: !hasClip(), onSelect: () => markPreviewBoundary("in") },
        { label: t("menu.mark.out"), hint: "O", disabled: !hasClip(), onSelect: () => markPreviewBoundary("out") },
        { type: "separator" },
        { label: t("menu.mark.clearIn"), hint: "Alt+I", disabled: !hasClip(), onSelect: () => resetClipBoundary("in") },
        { label: t("menu.mark.clearOut"), hint: "Alt+O", disabled: !hasClip(), onSelect: () => resetClipBoundary("out") },
        { label: t("menu.mark.clearIO"), hint: "Alt+X", disabled: !hasClip(), onSelect: () => resetClipBoundary("both") },
        { type: "separator" },
        {
          label: t("menu.mark.goIn"),
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
          label: t("menu.mark.goOut"),
          hint: "Shift+O",
          disabled: !hasClip(),
          onSelect: () => el("btn-jump-clip-end")?.click(),
        },
      ],
    },
    {
      id: "view",
      getLabel: () => t("menu.view"),
      getItems: () => [
        {
          label: t("menu.view.cinema"),
          hint: "P",
          checked: Boolean(monitorEngine?.cinemaActive),
          disabled: !hasVideo(),
          onSelect: () => monitorEngine?.toggleCinemaMode(),
        },
        {
          label: t("menu.view.fullscreen"),
          hint: "F11",
          checked: Boolean(desktopState.fullscreen),
          onSelect: () => toggleAppFullscreen(),
        },
        { type: "separator" },
        {
          label: t("menu.view.zoomIn"),
          hint: "Ctrl+=",
          disabled: getUiScalePct() >= UI_SCALE_STEPS[UI_SCALE_STEPS.length - 1],
          onSelect: () => stepUiScale(1),
        },
        {
          label: t("menu.view.zoomOut"),
          hint: "Ctrl+-",
          disabled: getUiScalePct() <= UI_SCALE_STEPS[0],
          onSelect: () => stepUiScale(-1),
        },
        {
          label: t("menu.view.zoomReset"),
          hint: "Ctrl+0",
          checked: getUiScalePct() === 100,
          onSelect: () => applyUiScale(100, true),
        },
        { type: "separator" },
        {
          label: t("menu.view.zoomFit"),
          checked: monitorEngine?.zoomMode === "fit",
          onSelect: () => monitorEngine?.setZoomMode("fit"),
        },
        {
          label: t("menu.view.zoom100"),
          checked: monitorEngine?.zoomMode === "100",
          onSelect: () => monitorEngine?.setZoomMode("100"),
        },
        {
          label: t("menu.view.zoom150"),
          checked: monitorEngine?.zoomMode === "150",
          onSelect: () => monitorEngine?.setZoomMode("150"),
        },
        { type: "separator" },
        {
          label: t("menu.view.safe916"),
          checked: Boolean(monitorEngine?.showSafe916 && monitorEngine?.safeMode === "9:16"),
          onSelect: () =>
            monitorEngine?.setSafeMode("9:16", !(monitorEngine.showSafe916 && monitorEngine.safeMode === "9:16")),
        },
        {
          label: t("menu.view.safe45"),
          checked: Boolean(monitorEngine?.showSafe916 && monitorEngine?.safeMode === "4:5"),
          onSelect: () =>
            monitorEngine?.setSafeMode("4:5", !(monitorEngine.showSafe916 && monitorEngine.safeMode === "4:5")),
        },
        {
          label: t("menu.view.safeTitle"),
          checked: Boolean(monitorEngine?.showSafe916 && monitorEngine?.safeMode === "title"),
          onSelect: () =>
            monitorEngine?.setSafeMode("title", !(monitorEngine.showSafe916 && monitorEngine.safeMode === "title")),
        },
        {
          label: t("menu.view.safeOff"),
          checked: !monitorEngine?.showSafe916,
          onSelect: () => monitorEngine?.setSafeMode("off", false),
        },
      ],
    },
    {
      id: "playback",
      getLabel: () => t("menu.playback"),
      getItems: () => [
        {
          label: t("menu.playback.toggle"),
          hint: "Space",
          disabled: !hasVideo(),
          onSelect: () => el("btn-play-toggle")?.click(),
        },
        {
          label: t("menu.playback.inToOut"),
          hint: "Alt+/",
          disabled: !hasClip(),
          onSelect: () => {
            const cur = getClipByRank(state.previewSegment?.rank ?? state.activeClipRank);
            if (cur) previewHighlightSegment(cur);
          },
        },
        {
          label: t("menu.playback.loop"),
          hint: "Ctrl+/ · M",
          checked: Boolean(el("chk-loop-segment")?.checked),
          onSelect: () => {
            const chk = el("chk-loop-segment");
            if (chk) {
              chk.checked = !chk.checked;
              setStatusMessage(chk.checked ? t("mon.loopOn") : t("mon.loopOff"));
            }
          },
        },
        { type: "separator" },
        { label: t("menu.playback.fwd"), hint: "L", disabled: !hasVideo(), onSelect: () => triggerShuttle(1, false) },
        { label: t("menu.playback.stop"), hint: "K", disabled: !hasVideo(), onSelect: () => { stopShuttle(true); el("review-video")?.pause(); } },
        { label: t("menu.playback.rev"), hint: "J", disabled: !hasVideo(), onSelect: () => triggerShuttle(-1, false) },
        { type: "separator" },
        { label: t("menu.playback.step"), hint: "← / →", disabled: !hasVideo(), onSelect: () => stepVideoFrames(1) },
        {
          label: t("menu.playback.dim"),
          checked: Boolean(monitorEngine?.dimmed),
          onSelect: () => {
            const on = monitorEngine?.toggleDim();
            setStatusMessage(on ? t("mon.dimOn") : t("mon.dimOff"));
          },
        },
        {
          label: t("menu.playback.skipAir"),
          checked: Boolean(state.skipDeadAirEnabled),
          onSelect: () => toggleSkipDeadAir(),
        },
      ],
    },
    {
      id: "workspace",
      getLabel: () => t("menu.workspace"),
      getItems: () => {
        const snap = dockSnap();
        const titles = getPanelTitles();
        const openSet = new Set(snap.openPanels || []);
        const panelItems = (snap.allPanels || []).map((p) => ({
          label: t("menu.workspace.panelItem", { title: titles[p.id] || p.title }),
          checked: openSet.has(p.id),
          disabled: openSet.has(p.id) && openSet.size <= 1,
          onSelect: () => dock?.togglePanelVisibility(p.id),
        }));
        return [
          { label: t("ws.editTitle"), hint: "Shift+1", checked: snap.preset === "edit", onSelect: () => dock?.setPreset("edit") },
          { label: t("ws.reviewTitle"), hint: "Shift+2", checked: snap.preset === "review", onSelect: () => dock?.setPreset("review") },
          {
            label: t("ws.subtitleTitle"),
            hint: "Shift+3",
            checked: snap.preset === "subtitle",
            onSelect: () => {
              dock?.setPreset("subtitle");
              ensureActiveClipForSubtitles();
              renderActiveSubtitlesPane();
            },
          },
          {
            label: t("ws.deliverTitle"),
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
          { label: t("menu.workspace.maxRestore"), hint: "`", onSelect: () => dock?.toggleMaximize() },
          { label: t("menu.workspace.reset"), onSelect: () => dock?.resetCurrentPreset() },
        ];
      },
    },
    {
      id: "help",
      getLabel: () => t("menu.help"),
      getItems: () => [
        {
          label: t("menu.help.manual"),
          hint: "F1",
          onSelect: () => openManualDialog("quickstart"),
        },
        {
          label: t("menu.help.shortcuts"),
          hint: "?",
          onSelect: () => openShortcutsDialog(),
        },
        {
          label: t("menu.help.demo"),
          onSelect: () => loadDemoProjectIntoWorkbench(),
        },
        { type: "separator" },
        {
          label: t("menu.help.privacy"),
          onSelect: () => openManualDialog("faq"),
        },
        {
          label: t("menu.help.about"),
          onSelect: () => el("about-dialog")?.showModal?.(),
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
      const target = clampNum(sec, 0, videoEl.duration || sec);
      recordUserSeekDeadAirBypass(target);
      videoEl.currentTime = target;
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
      const tcStr = formatTimecode(val, true);
      renderActiveSubtitlesPane();
      recordHistory(
        t(edge === "left" ? "hist.act.tlTrimIn" : "hist.act.tlTrimOut", { rank: clip.rank, tc: tcStr }),
        "trim"
      );
      scheduleAutoSave();
      setStatusMessage(
        t(edge === "left" ? "toast.setInStatus" : "toast.setOutStatus", { rank: clip.rank, tc: tcStr })
      );
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
        t(edge === "left" ? "hist.act.tlSubStart" : "hist.act.tlSubEnd", { rank: clip.rank, idx: subIndex + 1 }),
        "sub"
      );
      scheduleAutoSave();
    },
    onRazorSubtitle: (clip, subIndex, sec) => {
      splitSubtitleLineAtTime(clip, subIndex, sec);
    },
    onClipContextMenu: (e, clip) => {
      selectActiveClip(clip, false);
      const { tgtTitle } = resolveClipTitles(clip);
      ctxMenu?.open(e.clientX, e.clientY, [
        { label: t("clips.ctx.preview"), hint: t("clips.ctx.dblClickHint"), onSelect: () => previewHighlightSegment(clip) },
        { label: t("tl.ctx.focusClip"), onSelect: () => timelineEngine?.fitClip(clip) },
        {
          label: t("tl.ctx.toggleExport"),
          hint: "D",
          checked: clip.selected !== false,
          onSelect: () => {
            clip.selected = clip.selected === false;
            if (state.onlySelected) renderHighlightsList();
            else syncHighlightRowStates();
            renderClipInspector();
            syncTimelineData();
            recordHistory(
              t(clip.selected ? "hist.act.check" : "hist.act.uncheck", { rank: clip.rank }),
              "select"
            );
            scheduleAutoSave();
          },
        },
        {
          label: t("clips.ctx.toggleReview"),
          hint: "R",
          checked: getClipReviewStatus(clip) === "reviewed",
          onSelect: () => toggleClipReviewStatus(clip),
        },
        {
          label: t("sub.context30sBtn"),
          onSelect: () => openContext30sDialog(clip),
        },
        { type: "separator" },
        { label: t("tl.ctx.trimStart"), hint: "Shift+[", onSelect: () => trimClipToPlayhead("start") },
        { label: t("tl.ctx.trimEnd"), hint: "Shift+]", onSelect: () => trimClipToPlayhead("end") },
        { label: t("tl.ctx.splitClip"), hint: "Ctrl+Shift+\\", onSelect: () => splitActiveClipAtPlayhead() },
        { label: t("tl.ctx.resetIO"), hint: "Alt+X", onSelect: () => resetClipBoundary("both") },
        { type: "separator" },
        { label: t("clips.ctx.retranslate"), onSelect: () => openRetranslateDialog([clip.rank], tgtTitle) },
        { label: t("clips.ctx.exportOne"), onSelect: () => triggerExport([clip.rank]) },
        { type: "separator" },
        {
          label: t("clips.ctx.delete"),
          hint: "Delete",
          danger: true,
          onSelect: () => deleteClipByRank(clip.rank),
        },
      ]);
    },
    onSubtitleContextMenu: (e, clip, sub, subIndex) => {
      selectActiveClip(clip, false);
      ctxMenu?.open(e.clientX, e.clientY, [
        { label: t("sub.jumpLineTitle"), onSelect: () => jumpToSubtitleLine(clip, sub) },
        {
          label: t("tl.ctx.splitSubMid"),
          onSelect: () => splitSubtitleLineAtTime(clip, subIndex, null),
        },
        {
          label: t("tl.ctx.editInSubs"),
          onSelect: () => dock?.focusPanel("subtitles"),
        },
        { type: "separator" },
        {
          label: t("sub.btnDeleteTitle"),
          danger: true,
          onSelect: () => {
            clip.subtitles.splice(subIndex, 1);
            renderActiveSubtitlesPane();
            syncHighlightRowStates();
            syncVideoSubtitleOverlay();
            recordHistory(t("hist.act.delSub", { rank: clip.rank, idx: subIndex + 1 }), "sub");
            scheduleAutoSave();
          },
        },
      ]);
    },
    onChapterClick: (ch) => {
      setStatusMessage(
        t("tl.chapterJumped", {
          title: ch.title || `Chapter ${ch.index}`,
          clock: formatClock(ch.start_sec),
        })
      );
    },
    onChapterContextMenu: (e, ch) => {
      ctxMenu?.open(e.clientX, e.clientY, [
        {
          label: t("tl.ctx.jumpChapter", { title: ch.title || `Chapter ${ch.index}` }),
          onSelect: () => jumpMonitorToTime(ch.start_sec, true),
        },
        {
          label: t("tl.ctx.chapterToClip"),
          onSelect: () => convertChaptersToClips([ch]),
        },
        { type: "separator" },
        {
          label: t("tl.ctx.allChaptersToClips", { count: state.chapters.length }),
          disabled: state.chapters.length === 0,
          onSelect: () => convertChaptersToClips(state.chapters),
        },
      ]);
    },
    onRulerContextMenu: (e, sec) => {
      const items = [
        {
          label: t("tl.ctx.jumpHere", { clock: formatClock(sec) }),
          onSelect: () => jumpMonitorToTime(sec, true),
        },
      ];
      if (state.chapters.length > 0) {
        items.push({ type: "separator" });
        items.push({
          label: t("tl.ctx.allChaptersToClips", { count: state.chapters.length }),
          onSelect: () => convertChaptersToClips(state.chapters),
        });
      }
      ctxMenu?.open(e.clientX, e.clientY, items);
    },
    onChatPeakClick: (pk) => {
      const pSec = Number(pk.peak_sec ?? pk.start_sec ?? 0);
      setStatusMessage(t("chat.jumpedPeak", { clock: formatClock(pSec) }));
    },
    onChatPeakContextMenu: (e, pk) => {
      const pSec = Number(pk.peak_sec ?? pk.start_sec ?? 0);
      ctxMenu?.open(e.clientX, e.clientY, [
        {
          label: t("tl.ctx.jumpHere", { clock: formatClock(pSec) }),
          onSelect: () => jumpMonitorToTime(pSec, true),
        },
        {
          label: t("chat.ctx.peakToClip"),
          onSelect: () => createClipFromChatPeak(pk),
        },
      ]);
    },
    onRequestWaveform: async (stem, start, end, points) => {
      const outDir = encodeURIComponent(el("output-dir-field").value.trim());
      const cacheVer = getMediaCacheVersion(state.activeVideo);
      return apiFetch(
        `/api/waveform?stem=${encodeURIComponent(stem)}&output_dir=${outDir}&start=${start.toFixed(2)}&end=${end.toFixed(2)}&points=${points}&v=${cacheVer}`
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
    smartSplitAtPlayhead();
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
    else setStatusMessage(t("toast.selectClipFirst"), true);
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
    if (!vis) {
      state.overlaySubMode = "off";
    } else if (normalizeOverlaySubMode(state.overlaySubMode) === "off") {
      state.overlaySubMode = "bilingual";
    }
    renderCcMenu();
    syncVideoSubtitleOverlay();
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
        mediaKey: `${state.activeVideo.video_path}::${state.activeVideo.cache_token || ""}`,
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
      // 播放头离开用户手动豁免的静音段后才清除豁免
      if (
        state.bypassDeadAirSeg &&
        (curSec < state.bypassDeadAirSeg.start_sec - 0.25 || curSec > state.bypassDeadAirSeg.end_sec + 0.25)
      ) {
        state.bypassDeadAirSeg = null;
      }
      // 修复 P0-03：正在拖动 Scrubber 时不与自动循环/到点暂停打架回弹
      if (monitorEngine?.isScrubbing) return;
      // 重写 Skip Dead Air：豁免用户手动拖入段 + 局部循环防死锁 + 片尾优雅暂停
      if (
        state.skipDeadAirEnabled &&
        !videoEl.paused &&
        shuttle.dir >= 0 &&
        state.deadAirSegments.length > 0 &&
        !(el("chk-loop-segment")?.checked && state.previewSegment)
      ) {
        const nowTs = performance.now();
        if (nowTs - state.lastDeadAirSkipAt > 800) {
          const hitAir = state.deadAirSegments.find(
            (seg) => curSec >= Number(seg.start_sec) + 0.2 && curSec < Number(seg.end_sec) - 0.35
          );
          const isBypassed = Boolean(
            hitAir &&
              state.bypassDeadAirSeg &&
              Math.abs(Number(hitAir.start_sec) - state.bypassDeadAirSeg.start_sec) < 0.25 &&
              Math.abs(Number(hitAir.end_sec) - state.bypassDeadAirSeg.end_sec) < 0.25
          );
          if (hitAir && !isBypassed) {
            const rawDur =
              Number.isFinite(videoEl.duration) && videoEl.duration > 0
                ? videoEl.duration
                : getVideoDurationLimit();
            const maxPlayable = Math.max(0, rawDur - 0.15);
            const hitEnd = Number(hitAir.end_sec);
            const conflictsPreviewLoop = Boolean(
              state.previewSegment && hitEnd >= state.previewSegment.end_sec - 0.25
            );
            if (!conflictsPreviewLoop) {
              if (hitEnd >= maxPlayable) {
                state.lastDeadAirSkipAt = nowTs;
                stopShuttle(true);
                videoEl.currentTime = maxPlayable;
                videoEl.pause();
                setStatusMessage(t("mon.skipAirEndPaused"), false, 2400);
                return;
              }
              const skipTarget = Math.min(maxPlayable, hitEnd);
              if (skipTarget > curSec + 0.5) {
                state.lastDeadAirSkipAt = nowTs;
                videoEl.currentTime = skipTarget;
                setStatusMessage(
                  t("mon.skippedDeadAir", {
                    dur: Math.round(Number(hitAir.duration_sec) || skipTarget - Number(hitAir.start_sec)),
                  }),
                  false,
                  2200
                );
                return;
              }
            }
          }
        }
      }
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
      recordUserSeekDeadAirBypass(sec);
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
          label: videoEl.paused ? t("mon.play") : t("mon.pause"),
          hint: "Space",
          disabled: !hasVid,
          onSelect: () => el("btn-play-toggle")?.click(),
        },
        {
          label: t("mon.ctx.playInToOut"),
          hint: "Alt+/",
          disabled: !cur,
          onSelect: () => cur && previewHighlightSegment(cur),
        },
        {
          label: t("mon.ctx.loopClip"),
          hint: "Ctrl+/ · M",
          checked: Boolean(el("chk-loop-segment")?.checked),
          onSelect: () => {
            const c = el("chk-loop-segment");
            if (c) c.checked = !c.checked;
          },
        },
        { type: "separator" },
        { label: t("mon.ctx.markIn"), hint: "I", disabled: !cur, onSelect: () => markPreviewBoundary("in") },
        { label: t("mon.ctx.markOut"), hint: "O", disabled: !cur, onSelect: () => markPreviewBoundary("out") },
        { label: t("mon.ctx.resetIO"), hint: "Alt+X", disabled: !cur, onSelect: () => resetClipBoundary("both") },
        { type: "separator" },
        {
          label: t("mon.ctx.safe916"),
          checked: Boolean(monitorEngine?.showSafe916 && monitorEngine?.safeMode === "9:16"),
          onSelect: () =>
            monitorEngine?.setSafeMode("9:16", !(monitorEngine.showSafe916 && monitorEngine.safeMode === "9:16")),
        },
        {
          label: t("mon.ctx.safeTitle"),
          checked: Boolean(monitorEngine?.showSafe916 && monitorEngine?.safeMode === "title"),
          onSelect: () =>
            monitorEngine?.setSafeMode("title", !(monitorEngine.showSafe916 && monitorEngine.safeMode === "title")),
        },
        {
          label: t("mon.ctx.cinema"),
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
          label: t("mon.ctx.copyTc", { tc: tcStr }),
          onSelect: async () => {
            try {
              await navigator.clipboard.writeText(tcStr);
              setStatusMessage(t("mon.tcCopied", { tc: tcStr }));
            } catch {
              setStatusMessage(t("mon.tcCurrent", { tc: tcStr }));
            }
          },
        },
        {
          label: t("mon.ctx.pasteTc"),
          disabled: !videoEl?.src,
          onSelect: async () => {
            try {
              const raw = await navigator.clipboard.readText();
              const sec = parseSmpteOrTimecode(raw, state.videoFps);
              if (sec !== null && !Number.isNaN(sec)) {
                stopShuttle(true);
                const clamped = clampNum(sec, 0, videoEl.duration || sec);
                recordUserSeekDeadAirBypass(clamped);
                videoEl.currentTime = clamped;
                monitorEngine?.refreshFrameUI();
                setStatusMessage(t("mon.tcJumped", { tc: formatSmpte(videoEl.currentTime, state.videoFps) }));
              } else {
                setStatusMessage(t("mon.tcInvalid"), true);
              }
            } catch {
              setStatusMessage(t("mon.tcReadErr"), true);
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
    const name = state.activeVideo?.filename || t("media.currentVideo");
    let hint = t("mon.errPreview", { name });
    if (errCode === 3 || errCode === 4) {
      hint = t("mon.errCodec", { name });
    } else if (errCode === 2) {
      hint = t("mon.errNetwork");
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
  el("btn-mark-in")?.addEventListener("click", () => markPreviewBoundary("in"));
  el("btn-mark-out")?.addEventListener("click", () => markPreviewBoundary("out"));
  el("btn-jump-clip-start")?.addEventListener("click", () => {
    const target = getClipByRank(state.previewSegment?.rank ?? state.activeClipRank);
    if (!target || !videoEl.src) {
      setStatusMessage(t("mon.noPreviewClip"), true);
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

    // Ctrl+S 仅在无模态弹窗打开时全局保存，避免弹窗内误保存
    if (mod && e.code === "KeyS") {
      if (document.querySelector("dialog[open]")) return;
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

    // Ctrl+F 打开字幕查找或聚焦高光/逐字稿搜索（即使在输入框内也可呼出）
    if (mod && !e.altKey && !e.shiftKey && e.code === "KeyF") {
      if (document.querySelector("dialog[open]")) return;
      e.preventDefault();
      if (dock?.focusedPanelId === "transcript") {
        el("transcript-search-input")?.focus();
        el("transcript-search-input")?.select();
      } else if (dock?.focusedPanelId === "clips") {
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
          setStatusMessage(loopChk.checked ? t("mon.loopOn") : t("mon.loopOff"));
        }
        return;
      }
      if (e.code === "Backslash") {
        e.preventDefault();
        if (e.shiftKey) splitActiveClipAtPlayhead();
        else smartSplitAtPlayhead();
        return;
      }
      if (e.code === "Equal" || e.code === "NumpadAdd") {
        e.preventDefault();
        stepUiScale(1);
        return;
      }
      if (e.code === "Minus" || e.code === "NumpadSubtract") {
        e.preventDefault();
        stepUiScale(-1);
        return;
      }
      if (e.code === "Digit0" || e.code === "Numpad0") {
        e.preventDefault();
        applyUiScale(100, true);
        setStatusMessage(`${t("dlg.cfg.uiScale")}: 100%`);
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

    // F1 打开内置新手操作手册
    if (e.code === "F1") {
      e.preventDefault();
      stopShuttle(true);
      videoEl.pause();
      openManualDialog("quickstart");
      return;
    }

    // ? 打开快捷键速查（修复 P1-06：打开弹窗前立刻停止倒放 rAF）
    if (e.key === "?" || (e.shiftKey && e.code === "Slash")) {
      e.preventDefault();
      stopShuttle(true);
      videoEl.pause();
      openShortcutsDialog();
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
          setStatusMessage(t("toast.setInStatus", { rank: cur.rank, tc: formatTimecode(cur.start_sec, true) }));
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
          setStatusMessage(t("toast.setOutStatus", { rank: cur.rank, tc: formatTimecode(cur.end_sec, true) }));
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
        setStatusMessage(t("tl.fitAllStatus"));
        return;
      }
      return;
    }

    const code = e.code;
    if (code === "KeyV") {
      e.preventDefault();
      timelineEngine?.setTool("select");
      setStatusMessage(t("tl.toolSelectStatus"));
      return;
    }
    if (code === "KeyB") {
      e.preventDefault();
      timelineEngine?.setTool("razor");
      setStatusMessage(t("tl.toolRazorStatus"));
      return;
    }
    if (code === "KeyN") {
      e.preventDefault();
      if (timelineEngine) {
        const next = !timelineEngine.snapEnabled;
        timelineEngine.setSnap(next);
        setStatusMessage(next ? t("tl.snapOn") : t("tl.snapOff"));
      }
      return;
    }
    if (code === "KeyP") {
      e.preventDefault();
      if (videoEl.src) {
        const on = monitorEngine?.toggleCinemaMode();
        setStatusMessage(on ? t("mon.cinemaOn") : t("mon.cinemaOff"));
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
      // 保留 checkbox / radio 键盘无障碍勾选，普通按钮/滑块获焦时优先全局播控
      const activeInputType = activeTag === "input" ? (activeEl?.type || "").toLowerCase() : "";
      if (activeTag === "input" && (activeInputType === "checkbox" || activeInputType === "radio")) {
        return;
      }
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
    } else if (code === "Delete") {
      if (state.activeClipRank != null && !e.target?.closest?.("#pane-subtitles")) {
        e.preventDefault();
        deleteClipByRank(state.activeClipRank);
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
      recordUserSeekDeadAirBypass(0);
      videoEl.currentTime = 0;
      monitorEngine?.refreshFrameUI();
    } else if (code === "End" || code === "Quote") {
      e.preventDefault();
      stopShuttle(true);
      if (videoEl.duration) {
        const endT = Math.max(0, videoEl.duration - 0.1);
        recordUserSeekDeadAirBypass(endT);
        videoEl.currentTime = endT;
      }
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
      smartSplitAtPlayhead();
    } else if (code === "KeyM") {
      e.preventDefault();
      const loopChk = el("chk-loop-segment");
      if (loopChk) {
        loopChk.checked = !loopChk.checked;
        setStatusMessage(loopChk.checked ? t("mon.loopOn") : t("mon.loopOff"));
      }
    } else if (code === "KeyD" && state.highlightsData) {
      e.preventDefault();
      toggleActiveClipEnabled();
    } else if (code === "KeyR" && state.highlightsData) {
      e.preventDefault();
      toggleClipReviewStatus(null);
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
      el("global-task-text").textContent = `${task.percent}% ${localizeTaskMessage(task.message)}`;
      el("task-progress-label").textContent = `${task.percent}%`;
      el("task-progress-fill").style.width = `${task.percent}%`;
      const logPre = el("task-log-output");
      logPre.textContent = (task.logs || []).join("\n");
      logPre.scrollTop = logPre.scrollHeight;

      if (task.status === "completed" || task.status === "failed" || task.status === "cancelled") {
        clearInterval(state.pollTimer);
        state.pollTimer = null;
        if (task.status === "completed") {
          showToast(t("queue.completed", { title: task.title }));
          await scanVideos();
          if (task.task_type === "export" && task.result) {
            renderExportManifestSummary(task.result);
          }
        } else if (task.status === "cancelled") {
          showToast(t("queue.cancelled", { title: task.title }));
        } else {
          showToast(task.error || t("queue.failed"), true);
        }
      }
    } catch {
      consecutiveErrors += 1;
      if (consecutiveErrors >= 8) {
        clearInterval(state.pollTimer);
        state.pollTimer = null;
        showToast(t("queue.pollLost"), true);
      }
    }
  };

  pollOnce();
  state.pollTimer = setInterval(pollOnce, 1200);
}

async function triggerDiagnose(videoPaths) {
  if (!videoPaths || videoPaths.length === 0) {
    showToast(t("toast.selectVideoFirst"), true);
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
    showToast(t("queue.diagStarted", { title: task.title }), false, true);
    startTaskPolling(task.task_id);
  } catch (err) {
    showToast(err.message, true);
  }
}

async function triggerAnalyze(videoPaths) {
  if (!videoPaths || videoPaths.length === 0) {
    showToast(t("toast.selectVideoFirst"), true);
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
    showToast(t("queue.queued", { title: task.title }));
    startTaskPolling(task.task_id);
  } catch (err) {
    showToast(err.message, true);
  }
}

function openRetranslateDialog(ranks = null, clipTitle = "", opts = {}) {
  if (!state.highlightsData) {
    showToast(t("toast.noHighlightsRetranslate"), true);
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
  if (srcSel) {
    srcSel.value = opts.srcLang ?? state.highlightsData?.source_language ?? el("input-asr-lang")?.value ?? "";
  }
  if (tgtSel) {
    tgtSel.value = opts.tgtLang ?? state.highlightsData?.target_language ?? el("sel-target-lang")?.value ?? "auto";
  }
  if (desc) {
    if (opts.customDesc) {
      desc.textContent = opts.customDesc;
    } else {
      desc.textContent = state.retranslateTargetRanks
        ? t("dlg.retranslate.descTargeted", {
            ranks: state.retranslateTargetRanks.join(", #"),
            title: clipTitle || "",
          }).trim()
        : t("dlg.retranslate.descAll");
    }
  }
  if (dialog && typeof dialog.showModal === "function") {
    if (!dialog.open) dialog.showModal();
  } else {
    triggerRetranslate(
      state.retranslateTargetRanks,
      hwInput?.value.trim() || null,
      null,
      srcSel?.value || null,
      tgtSel?.value || null
    );
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
    showToast(t("queue.queued", { title: task.title }));
    startTaskPolling(task.task_id);
  } catch (err) {
    showToast(err.message, true);
  }
}

function syncExportModeUI() {
  const mode =
    document.querySelector('input[name="export-mode"]:checked')?.value || "nle_only";
  const isNleOnly = mode === "nle_only";
  const isLossless = mode === "lossless";
  const isReencode = mode === "vertical" || mode === "landscape";
  const isVertical = mode === "vertical";

  el("export-mode-hint")?.classList.toggle("hidden", !isNleOnly);
  // 断舍离 3：动态按需显隐无关交付控件，Title Banner 仅 9:16 显现
  el("export-encoder-row")?.classList.toggle("hidden", !isReencode);
  el("export-burn-sub-wrap")?.classList.toggle("hidden", !isReencode);
  el("export-title-banner-wrap")?.classList.toggle("hidden", !isVertical);

  const encSel = el("sel-encoder");
  const burnChk = el("chk-burn-subtitles");
  const bannerChk = el("chk-title-banner");
  if (encSel) encSel.disabled = isNleOnly || isLossless;
  if (burnChk) burnChk.disabled = isNleOnly || isLossless;
  if (bannerChk) bannerChk.disabled = !isVertical;
}

async function triggerExport(specificRanks = null) {
  if (!state.highlightsData) {
    showToast(t("toast.analyzeFirstExport"), true);
    return;
  }
  await saveHighlightsNow(true);

  const selectedMode =
    document.querySelector('input[name="export-mode"]:checked')?.value || "nle_only";
  const exportTgtSel = el("sel-export-target-lang");
  const subModeVal = el("sel-subtitle-lang")?.value || "bilingual";
  // 修复 P0-03：nle_only 极速 XML 工程导出豁免目标语种下拉框差异阻断
  if (
    selectedMode !== "nle_only" &&
    exportTgtSel &&
    !["source", "source_only", "none"].includes(subModeVal)
  ) {
    const lp = getActiveLanguagePair(exportTgtSel.value);
    if (lp.tgt !== lp.actualTgt) {
      const btnLabel = t("insp.export.switchLangTo", { code: lp.tgtMeta.code, short: lp.tgtMeta.short });
      showToast(
        t("toast.exportLangMismatch", {
          actual: lp.actualTgtMeta.name,
          desired: lp.tgtMeta.name,
          btn: btnLabel,
        }),
        true
      );
      return;
    }
  }

  const onlyReviewed = Boolean(el("chk-only-reviewed")?.checked);
  // 断舍离 2：切到视频切片模式且取消 Only Reviewed 时，若含未审校 Draft 片段则弹一次克制确认
  if (selectedMode !== "nle_only" && !onlyReviewed) {
    const allItems = state.highlightsData.highlights || [];
    const targetItems =
      Array.isArray(specificRanks) && specificRanks.length > 0
        ? allItems.filter((h) => specificRanks.some((r) => Number(r) === Number(h.rank)))
        : allItems.filter((h) => h.selected !== false);
    const draftCount = targetItems.filter((h) => getClipReviewStatus(h) !== "reviewed").length;
    if (draftCount > 0) {
      const ok = await showDarkroomConfirmDialog({
        title: t("dlg.confirm.draftExportTitle"),
        message: t("dlg.confirm.draftExportMsg", { draftCount }),
        confirmText: t("dlg.confirm.draftExportOk"),
      });
      if (!ok) return;
    }
  }

  const isReencode = selectedMode === "vertical" || selectedMode === "landscape";
  const payload = {
    highlights_json_path: state.highlightsData.highlights_json_path,
    video_path: el("override-video-path").value.trim() || null,
    mode: selectedMode,
    subtitle_lang: subModeVal,
    subtitle_mode: subModeVal,
    show_title_banner: selectedMode === "vertical" && Boolean(el("chk-title-banner")?.checked),
    burn_subtitles: isReencode && subModeVal !== "none" && Boolean(el("chk-burn-subtitles")?.checked),
    only_reviewed: onlyReviewed,
    encoder: el("sel-encoder").value,
    ranks: specificRanks,
  };

  try {
    const task = await apiFetch("/api/tasks/export", {
      method: "POST",
      body: JSON.stringify(payload),
    });
    showToast(t("queue.exportStarted", { title: task.title }));
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

  const hasClips = Boolean(manifest && Array.isArray(manifest.clips) && manifest.clips.length > 0);
  const hasNleFiles = Boolean(
    manifest &&
      (manifest.fcpxml_path ||
        manifest.premiere_xml_path ||
        manifest.markers_edl_path ||
        manifest.markers_csv_path)
  );
  if (!manifest || (!hasClips && !hasNleFiles)) {
    box.classList.add("hidden");
    return;
  }
  box.classList.remove("hidden");

  const metaLine = document.createElement("div");
  metaLine.textContent =
    manifest.mode === "nle_only"
      ? t("insp.export.manifestNleSummary", {
          count: manifest.selected_count ?? manifest.exported_count ?? 0,
          sec: manifest.elapsed_sec ?? 0,
        })
      : t("insp.export.manifestSummary", {
          mode: manifest.mode,
          count: manifest.exported_count,
          sec: manifest.elapsed_sec,
        });
  summary.appendChild(metaLine);

  if (hasNleFiles) {
    const nleRow = document.createElement("div");
    nleRow.className = "result-nle-links";
    const fileSpecs = [
      ["FCPXML", manifest.fcpxml_path],
      ["Premiere XML", manifest.premiere_xml_path],
      ["EDL", manifest.markers_edl_path],
      ["CSV", manifest.markers_csv_path],
    ];
    for (const [label, fullPath] of fileSpecs) {
      if (!fullPath) continue;
      const chip = document.createElement("span");
      chip.className = "result-nle-chip mono-num";
      chip.textContent = label;
      chip.title = String(fullPath);
      nleRow.appendChild(chip);
    }
    if (Number(manifest.chapters_count || 0) > 0) {
      const chChip = document.createElement("span");
      chChip.className = "result-nle-chip mono-num";
      chChip.textContent = t("insp.export.chaptersEmbedded", { count: manifest.chapters_count });
      nleRow.appendChild(chChip);
    }
    summary.appendChild(nleRow);
  }

  for (const c of (manifest.clips || []).slice(0, 5)) {
    const itemLine = document.createElement("div");
    const sizeTag =
      manifest.mode === "nle_only"
        ? `(SRT ${Number(c.srt_line_count || 0)})`
        : `(${c.size_mb}MB)`;
    itemLine.textContent = `#${c.rank} ${sizeTag} ${c.title_tgt || c.title_zh || c.title_en || ""}`;
    summary.appendChild(itemLine);
  }
}

/* ==================== 事件绑定 ==================== */
function bindEvents() {
  el("btn-browse-input")?.addEventListener("click", async () => {
    try {
      const res = await apiFetch("/api/browse-folder", {
        method: "POST",
        body: JSON.stringify({ initial_dir: el("input-dir-field").value, title: t("toast.browseInput") }),
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
        body: JSON.stringify({ initial_dir: el("output-dir-field").value, title: t("toast.browseOutput") }),
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
        body: JSON.stringify({ initial_dir: el("input-dir-field").value, title: t("toast.browseMaster") }),
      });
      if (res.path) {
        el("override-video-path").value = res.path;
        setStatusMessage(t("toast.masterSelected", { path: res.path }));
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
      showToast(t("toast.selectVideoFirst"), true);
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

  // 检查器「片段」页双向绑定（全部接入可撤销历史记录 + 同步规范化多语种标题字段）
  el("clip-prop-selected")?.addEventListener("change", (e) => {
    const item = getClipByRank(state.activeClipRank);
    if (!item) return;
    item.selected = e.target.checked;
    if (state.onlySelected) renderHighlightsList();
    else syncHighlightRowStates();
    recordHistory(
      t(item.selected ? "hist.act.check" : "hist.act.uncheck", { rank: item.rank }),
      "select"
    );
    scheduleAutoSave();
  });

  el("clip-prop-title-zh")?.addEventListener("input", (e) => {
    const item = getClipByRank(state.activeClipRank);
    if (!item) return;
    const lp = getActiveLanguagePair();
    writeClipTitleTgt(item, e.target.value, lp);
    if (state.previewSegment && Number(state.previewSegment.rank) === Number(item.rank)) {
      el("hud-clip-title").textContent = `#${item.rank} ${e.target.value}`;
    }
    el("sub-pane-clip-label").textContent = `#${item.rank} ${e.target.value}`;
    syncHighlightRowStates();
    recordHistory(t("hist.act.editTitleTgt", { rank: item.rank }), "title", `title_zh_${item.rank}`);
    scheduleAutoSave();
  });

  el("clip-prop-title-en")?.addEventListener("input", (e) => {
    const item = getClipByRank(state.activeClipRank);
    if (!item) return;
    const lp = getActiveLanguagePair();
    writeClipTitleSrc(item, e.target.value, lp);
    recordHistory(t("hist.act.editTitleSrc", { rank: item.rank }), "title", `title_en_${item.rank}`);
    scheduleAutoSave();
  });

  el("clip-prop-summary")?.addEventListener("input", (e) => {
    const item = getClipByRank(state.activeClipRank);
    if (!item) return;
    item.summary_zh = e.target.value;
    recordHistory(t("hist.act.editNotes", { rank: item.rank }), "title", `summary_${item.rank}`);
    scheduleAutoSave();
  });

  el("btn-clip-prop-preview")?.addEventListener("click", () => {
    const item = getClipByRank(state.activeClipRank);
    if (item) {
      stopShuttle(true);
      previewHighlightSegment(item);
    }
  });
  el("btn-clip-prop-review")?.addEventListener("click", () => {
    const item = getClipByRank(state.activeClipRank);
    if (item) toggleClipReviewStatus(item);
  });
  el("btn-clip-prop-context-30s")?.addEventListener("click", () => {
    const item = getClipByRank(state.activeClipRank);
    if (item) openContext30sDialog(item);
  });
  el("btn-sub-context-30s")?.addEventListener("click", () => {
    const item = ensureActiveClipForSubtitles();
    if (item) openContext30sDialog(item);
  });
  el("btn-clip-prop-retranslate")?.addEventListener("click", () => {
    const item = getClipByRank(state.activeClipRank);
    if (item) openRetranslateDialog([item.rank], resolveClipTitles(item).tgtTitle);
  });
  el("btn-clip-prop-export")?.addEventListener("click", () => {
    const item = getClipByRank(state.activeClipRank);
    if (item) triggerExport([item.rank]);
  });
  el("btn-clip-prop-delete")?.addEventListener("click", () => {
    if (state.activeClipRank != null) deleteClipByRank(state.activeClipRank);
  });
  el("btn-skip-dead-air")?.addEventListener("click", () => toggleSkipDeadAir());

  // 高光人工校对状态筛选
  el("sel-clip-review-filter")?.addEventListener("change", (e) => {
    state.reviewFilter = e.target.value || "all";
    renderHighlightsList();
  });

  // 全片逐字稿面板检索与选中句段新建片段
  let trSearchTimer = null;
  el("transcript-search-input")?.addEventListener("input", (e) => {
    state.transcriptQuery = e.target.value || "";
    if (trSearchTimer) clearTimeout(trSearchTimer);
    trSearchTimer = setTimeout(() => {
      if (state.activeVideo) loadTranscriptForVideo(state.activeVideo, state.transcriptQuery);
    }, 220);
  });
  el("btn-transcript-clear-search")?.addEventListener("click", () => {
    state.transcriptQuery = "";
    if (el("transcript-search-input")) el("transcript-search-input").value = "";
    if (state.activeVideo) loadTranscriptForVideo(state.activeVideo, "");
  });
  el("btn-transcript-create-clip")?.addEventListener("click", () => createClipFromSelectedTranscript());
  el("btn-transcript-clear-sel")?.addEventListener("click", () => {
    state.transcriptSelectedIds.clear();
    state.transcriptLastClickedIdx = -1;
    renderTranscriptPane();
  });

  // 时间线 OBS 章节一键转高光 & Chat Radar 弹幕导入与梗词搜索
  el("btn-tl-chapters")?.addEventListener("click", () => convertChaptersToClips(state.chapters));
  // 修复 P1-01：Browse 按钮永远无条件弹系统文件选择器并立即导入，彻底告别死锁旧路径
  el("btn-chat-import-browse")?.addEventListener("click", async () => {
    if (!state.activeVideo) {
      showToast(t("toast.selectVideoFirst"), true);
      return;
    }
    try {
      const res = await apiFetch("/api/browse-file", {
        method: "POST",
        body: JSON.stringify({
          initial_dir: el("input-dir-field")?.value || null,
          title: t("chat.browseTitle"),
          kind: "chat",
        }),
      });
      if (res.path) {
        if (el("chat-path-input")) el("chat-path-input").value = res.path;
        await importChatFileForActiveVideo(res.path);
      }
    } catch (err) {
      showToast(err.message, true);
    }
  });
  const loadTypedChatPath = async () => {
    const typedPath = (el("chat-path-input")?.value || "").trim();
    if (!typedPath) {
      showToast(t("chat.enterPathFirst"), true);
      el("chat-path-input")?.focus();
      return;
    }
    await importChatFileForActiveVideo(typedPath);
  };
  el("btn-chat-import-load")?.addEventListener("click", loadTypedChatPath);
  el("chat-path-input")?.addEventListener("keydown", (e) => {
    if (e.key === "Enter") {
      e.preventDefault();
      loadTypedChatPath();
    }
  });
  const runChatKeywordSearch = () => {
    state.chatKeywordQuery = (el("chat-keyword-input")?.value || "").trim();
    if (state.activeVideo) loadChatRadarForVideo(state.activeVideo, state.chatKeywordQuery);
  };
  el("btn-chat-keyword-run")?.addEventListener("click", runChatKeywordSearch);
  el("chat-keyword-input")?.addEventListener("keydown", (e) => {
    if (e.key === "Enter") {
      e.preventDefault();
      runChatKeywordSearch();
    }
  });
  el("chat-keyword-input")?.addEventListener("search", runChatKeywordSearch);

  // 交付面板模式分段与 Only Reviewed 开关联动
  document.querySelectorAll('input[name="export-mode"]').forEach((radio) => {
    radio.addEventListener("change", () => syncExportModeUI());
  });
  el("chk-only-reviewed")?.addEventListener("change", () => updateHighlightSelectionBadges());

  // 字幕面板加句（复用统一函数并写入历史）
  el("btn-sub-add-line")?.addEventListener("click", () => insertSubtitleAtPlayhead());

  el("btn-sub-retranslate-active")?.addEventListener("click", () => {
    const item = ensureActiveClipForSubtitles();
    if (!item) return;
    openRetranslateDialog([item.rank], resolveClipTitles(item).tgtTitle);
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

  // 交付页六语种译文切换 & 一键重翻联动（支持 6×6 全组合含同语种精修）
  el("sel-export-target-lang")?.addEventListener("change", () => syncLanguagePairUI(false));
  el("btn-export-switch-lang")?.addEventListener("click", () => {
    if (!state.highlightsData) {
      showToast(t("toast.analyzeFirstSwitchLang"), true);
      return;
    }
    const desiredTgt = el("sel-export-target-lang")?.value || "zh";
    const lp = getActiveLanguagePair(desiredTgt);
    triggerRetranslate(null, null, null, lp.src, desiredTgt);
  });
  el("sub-pane-lang-pill")?.addEventListener("click", () => openRetranslateDialog(null));

  // 媒体池新建文件夹、空白区右键、面包屑导航与拖拽移出
  el("btn-media-new-folder")?.addEventListener("click", () => openMediaBinDialog(null));
  el("video-list")?.addEventListener("contextmenu", (e) => {
    if (e.target.closest(".video-card-grid, .video-item, .bin-folder-card")) return;
    e.preventDefault();
    ctxMenu?.open(e.clientX, e.clientY, buildMediaPoolEmptyContextMenuItems());
  });
  el("media-bin-bar")?.addEventListener("click", (e) => {
    const btn = e.target.closest(".bin-crumb");
    if (!btn) return;
    state.activeBinScope = btn.dataset.binScope || "all";
    renderVideoList();
  });
  const unfiledCrumb = document.querySelector('#media-bin-bar [data-bin-scope="unfiled"]');
  if (unfiledCrumb) {
    unfiledCrumb.addEventListener("dragover", (e) => {
      e.preventDefault();
      unfiledCrumb.classList.add("is-drop-target");
    });
    unfiledCrumb.addEventListener("dragleave", () => unfiledCrumb.classList.remove("is-drop-target"));
    unfiledCrumb.addEventListener("drop", (e) => {
      e.preventDefault();
      unfiledCrumb.classList.remove("is-drop-target");
      let paths = state.draggingVideoPaths;
      if (!paths.length && e.dataTransfer) {
        try {
          paths = JSON.parse(e.dataTransfer.getData("text/plain") || "[]");
        } catch {
          paths = [];
        }
      }
      if (paths.length) moveVideosToBin(paths, "");
    });
  }
  el("media-bin-color-picker")?.addEventListener("click", (e) => {
    const dot = e.target.closest(".bin-color-dot");
    if (!dot) return;
    state.selectedBinColor = dot.dataset.binColor || "#4f8ef7";
    document.querySelectorAll("#media-bin-color-picker .bin-color-dot").forEach((d) => {
      d.classList.toggle("active", d === dot);
    });
  });
  el("btn-confirm-media-bin")?.addEventListener("click", () => saveMediaBinFromDialog());
  el("media-bin-name-input")?.addEventListener("keydown", (e) => {
    if (e.key === "Enter") {
      e.preventDefault();
      saveMediaBinFromDialog();
    }
  });

  // 全量快捷键实时检索与内置新手操作手册章节切换
  el("shortcut-search-input")?.addEventListener("input", (e) => filterShortcutsTable(e.target.value));
  el("manual-nav")?.addEventListener("click", (e) => {
    const btn = e.target.closest(".manual-nav-btn");
    if (!btn) return;
    switchManualSection(btn.dataset.manualSec || "quickstart");
  });
  el("btn-manual-load-demo")?.addEventListener("click", async () => {
    el("manual-dialog")?.close();
    await loadDemoProjectIntoWorkbench();
  });
  el("btn-manual-open-shortcuts")?.addEventListener("click", () => {
    el("manual-dialog")?.close();
    openShortcutsDialog();
  });

  el("sel-asr-provider")?.addEventListener("change", () => syncAsrProviderVisibility());

  el("btn-hl-select-all")?.addEventListener("click", () => {
    if (!state.highlightsData) return;
    state.highlightsData.highlights.forEach((h) => {
      h.selected = true;
    });
    if (state.onlySelected) renderHighlightsList();
    else syncHighlightRowStates();
    renderClipInspector();
    recordHistory(t("hist.act.selectAll"), "batch");
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
    recordHistory(t("hist.act.clearAll"), "batch");
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

  // 打开模态弹窗按钮点击前自动停止倒放 rAF；快捷键按钮统一走 openShortcutsDialog() 动态刷新真实计数
  el("btn-open-settings")?.addEventListener("click", () => {
    stopShuttle(true);
    el("review-video")?.pause();
  });
  el("btn-open-shortcuts")?.addEventListener("click", () => {
    stopShuttle(true);
    el("review-video")?.pause();
    openShortcutsDialog();
  });

  document.querySelectorAll("#pane-clips .seg-btn[data-sort]").forEach((btn) => {
    btn.addEventListener("click", () => {
      document.querySelectorAll("#pane-clips .seg-btn[data-sort]").forEach((b) => b.classList.remove("active"));
      btn.classList.add("active");
      state.sortBy = btn.dataset.sort || "time";
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
      showToast(t("insp.glossary.requiredFields"), true);
      return;
    }
    const aliases = aliasesVal
      ? aliasesVal
          .split(/[,，;；]+/)
          .map((s) => s.trim())
          .filter(Boolean)
      : [];
    const existingIdx = userGlossaryTerms.findIndex(
      (item) => item.en.toLowerCase() === enVal.toLowerCase()
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
      setStatusMessage(t("dlg.settings.savedEnv"));
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
      showToast(t("queue.queued", { title: task.title }));
      startTaskPolling(task.task_id);
    } catch (err) {
      showToast(err.message, true);
    }
  });

  el("onboarding-provider-preset")?.addEventListener("change", (e) => {
    const baseEl = el("onboarding-base-url");
    if (!baseEl) return;
    baseEl.value = e.target.value === "openai" ? "https://api.openai.com/v1" : "https://api.deepseek.com/v1";
  });

  el("btn-onboarding-save")?.addEventListener("click", async () => {
    const keyVal = el("onboarding-api-key")?.value.trim() || "";
    const baseVal = el("onboarding-base-url")?.value.trim() || "https://api.deepseek.com/v1";
    const preset = el("onboarding-provider-preset")?.value || "deepseek";
    if (!keyVal) {
      showToast(t("dlg.onboarding.needKey"), true);
      return;
    }
    const normalizedBase = baseVal.replace(/\/+$/, "");
    // 修复 P0-3：DeepSeek 预设清空 ASR_BASE_URL 让后端自动路由官方 /v1/tools/asr；OpenAI 兼容自动拼 /audio/transcriptions
    const asrProvider = preset === "deepseek" ? "deepseek" : "openai_compatible";
    const asrBaseUrl =
      preset === "deepseek"
        ? ""
        : normalizedBase.endsWith("/audio/transcriptions")
          ? normalizedBase
          : `${normalizedBase}/audio/transcriptions`;
    try {
      const cfg = await apiFetch("/api/config", {
        method: "POST",
        body: JSON.stringify({
          base_url: baseVal,
          api_key: keyVal,
          asr_provider: asrProvider,
          asr_base_url: asrBaseUrl,
          asr_api_key: keyVal,
        }),
      });
      state.config = cfg;
      hydrateSettingsForm(cfg);
      el("onboarding-dialog")?.close();
      showToast(t("dlg.onboarding.saved"));
    } catch (err) {
      showToast(err.message, true);
    }
  });

  el("btn-onboarding-demo")?.addEventListener("click", async () => {
    el("onboarding-dialog")?.close();
    await loadDemoProjectIntoWorkbench();
  });
}

function openShortcutsDialog() {
  const dlg = el("shortcuts-dialog");
  const searchEl = el("shortcut-search-input");
  if (searchEl) {
    searchEl.value = "";
    filterShortcutsTable("");
  }
  if (dlg && !dlg.open && typeof dlg.showModal === "function") {
    dlg.showModal();
    setTimeout(() => searchEl?.focus(), 20);
  }
}

function filterShortcutsTable(query) {
  const q = String(query || "").trim().toLowerCase();
  const grid = el("shortcut-sections-grid");
  if (!grid) return;
  let visibleCount = 0;
  grid.querySelectorAll(".shortcut-sec").forEach((sec) => {
    let secHits = 0;
    sec.querySelectorAll(".shortcut-row").forEach((row) => {
      const matched = !q || (row.textContent || "").toLowerCase().includes(q);
      row.classList.toggle("is-hidden", !matched);
      if (matched) {
        secHits += 1;
        visibleCount += 1;
      }
    });
    sec.classList.toggle("is-hidden", secHits === 0);
  });
  const badge = el("shortcuts-count-badge");
  if (badge) badge.textContent = t("dlg.shortcuts.count", { count: visibleCount });
}

function switchManualSection(secId = "quickstart") {
  document.querySelectorAll("#manual-nav .manual-nav-btn").forEach((btn) => {
    btn.classList.toggle("active", btn.dataset.manualSec === secId);
  });
  document.querySelectorAll("#manual-content .manual-section").forEach((sec) => {
    sec.classList.toggle("hidden", sec.dataset.manualPanel !== secId);
  });
  const box = el("manual-content");
  if (box) box.scrollTop = 0;
}

function openManualDialog(secId = "quickstart") {
  switchManualSection(secId);
  const dlg = el("manual-dialog");
  if (dlg && !dlg.open && typeof dlg.showModal === "function") {
    dlg.showModal();
  }
}

async function loadDemoProjectIntoWorkbench() {
  try {
    setStatusMessage(t("toast.loadingDemo"));
    const res = await apiFetch("/api/onboarding/load-demo", { method: "POST" });
    await scanVideos();
    const target = state.videos.find(
      (v) => v.stem === res.stem || v.video_path === res.video_path
    );
    if (target) {
      await selectActiveVideo(target, { forceReload: true });
      showToast(t("toast.demoLoaded"));
    } else {
      showToast(t("toast.demoImported"));
    }
  } catch (err) {
    showToast(t("toast.demoFailed", { msg: err.message }), true);
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
    btnFs.title = desktopState.fullscreen ? t("topbar.winExitFullscreen") : t("topbar.winFullscreen");
  }
  if (btnMax) {
    btnMax.classList.toggle("active", desktopState.maximized);
    btnMax.title = desktopState.maximized ? t("topbar.winRestore") : t("topbar.winMax");
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
  const applyHostState = (st) => {
    if (!st) return;
    desktopState.isDesktop = true;
    desktopState.maximized = Boolean(st.maximized);
    desktopState.fullscreen = Boolean(st.fullscreen);
    syncDesktopWindowUI();
  };

  window.syncDesktopStateFromHost = applyHostState;

  const activateDesktopBridge = async () => {
    if (!window.pywebview?.api) return;
    desktopState.isDesktop = true;
    try {
      const st = await window.pywebview.api.get_window_state();
      if (st) applyHostState(st);
      else syncDesktopWindowUI();
    } catch {
      syncDesktopWindowUI();
    }
  };

  if (window.pywebview?.api) {
    activateDesktopBridge();
  } else {
    window.addEventListener("pywebviewready", activateDesktopBridge, { once: true });
  }

  window.addEventListener("focus", () => {
    if (!desktopState.isDesktop || !window.pywebview?.api?.get_window_state) return;
    window.pywebview.api
      .get_window_state()
      .then((st) => applyHostState(st))
      .catch(() => {});
  });

  const topbar = el("app-topbar");
  const isInteractiveInTopbar = (target) =>
    Boolean(
      target &&
        target.closest(
          "button, input, select, a, label, .win-controls, .app-menubar, .menubar-trigger, .menubar-popover, .ui-lang-switcher, .ui-lang-menu-popover"
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
  window.SolacutI18n?.applyDom(document);
  initDesktopShell();
  initThemeListeners();
  initUiLanguageSwitcher();
  initGlobalContextMenu();
  initHistoryManager();
  initDockWorkspace();
  initPopovers();
  initDialogs();
  initScrubbyInputs();
  initPlayerEvents();
  initCcMenu();
  initTimelineEngine();
  initAppMenuBar();
  bindEvents();
  syncSkipDeadAirUI();
  syncExportModeUI();
  filterShortcutsTable("");
  try {
    await loadConfigAndScan();
    syncLanguagePairUI();
    await checkOnboardingOnBoot();
  } catch (err) {
    showToast(t("toast.initFailed", { msg: err.message }), true);
  }
})();
