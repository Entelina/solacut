/* ClipStudio NLE 专业前端原语库（零外部依赖）
 * 提供：
 * 1. 统一 1.25px 几何线性 SVG 图标系统（彻底告别 Unicode/Emoji 拼凑符号）
 * 2. Scrubby Hot-Text 可左右拖拉调参的热文字数值/时码控件（对标 PR / DaVinci）
 * 3. 原生暗房右键上下文菜单（遵循 modern-web-guidance `menu`：popover="auto" + 零尺寸锚点 + focusgroup="menu nomemory" + 完整回退）
 */

/* ==================== 1. 统一 1.25px 几何线性 SVG 图标库 ==================== */
const SVG_PATHS = {
  play: '<path d="M4.2 3.1l7.8 4.9-7.8 4.9V3.1z" fill="currentColor" stroke="none"/>',
  pause: '<path d="M4.5 3.2h2.4v9.6H4.5zM9.1 3.2h2.4v9.6H9.1z" fill="currentColor" stroke="none"/>',
  skipStart: '<path d="M3.5 3.5v9M12.5 3.5L6.5 8l6 4.5V3.5z"/>',
  markIn: '<path d="M6.5 3H4v10h2.5M9 8h3.5"/>',
  markOut: '<path d="M9.5 3H12v10H9.5M3.5 8H7"/>',
  loop: '<path d="M12.5 6.2A4.5 4.5 0 0 0 4.8 4.4M3.5 9.8a4.5 4.5 0 0 0 7.7 1.8M12.5 3.5v2.8H9.7M3.5 12.5V9.7h2.8"/>',
  autoStop: '<path d="M3.5 8h7M12.5 4v8M8.2 5.4L10.8 8l-2.6 2.6"/>',
  captions: '<rect x="2.2" y="3.5" width="11.6" height="9" rx="1.2"/><path d="M5 9.3h3.2M9.6 9.3h1.6M5 7h1.8M8.2 7h3"/>',
  mergeUp: '<path d="M8 12.5V4M4.8 7L8 3.8 11.2 7M4 12.5h8"/>',
  split: '<path d="M8 2.5v11M3 5.5l2.5 2.5L3 10.5M13 5.5L10.5 8l2.5 2.5"/>',
  trash: '<path d="M3.2 4.5h9.6M6 4.5V3.2h4v1.3M4.8 4.5l.6 8.3h5.2l.6-8.3"/>',
  plus: '<path d="M8 3.2v9.6M3.2 8h9.6"/>',
  retranslate: '<path d="M2.5 4.5h6M5.5 3v1.5M3.5 4.5c.3 2.1 1.6 3.8 3.6 4.7M7.5 4.5c-.5 2-1.8 3.6-3.8 4.5M8.5 12.5l2.3-5.8 2.3 5.8M9.3 10.7h3"/>',
  exportOne: '<path d="M8 10.5V3M5 5.8L8 2.8l3 3M3 10.5v2.2h10v-2.2"/>',
  folder: '<path d="M2 4.2h4.2l1.2 1.3H14v6.7a.8.8 0 0 1-.8.8H2.8a.8.8 0 0 1-.8-.8V4.2z"/>',
  refresh: '<path d="M13 8a5 5 0 1 1-1.3-3.4M13 2.8v2.6h-2.6"/>',
  diagnose: '<path d="M2 9h2.6l1.5-4.5 2.4 7 1.6-4L11.4 9H14"/>',
  spark: '<path d="M8 2.2l1.3 4.5 4.5 1.3-4.5 1.3L8 13.8l-1.3-4.5L2.2 8l4.5-1.3L8 2.2z"/>',
  check: '<path d="M3.5 8.2l3 3 6-6.2"/>',
  editWs: '<rect x="2" y="2.5" width="12" height="11" rx="1"/><path d="M2 9h12M6 2.5V9"/>',
  reviewWs: '<rect x="2" y="2.5" width="12" height="11" rx="1"/><path d="M8.5 2.5v11"/>',
  subWs: '<rect x="2" y="2.5" width="12" height="11" rx="1"/><path d="M7.5 2.5v11M9.5 6h2.5M9.5 8.5h2.5M9.5 11h2"/>',
  deliverWs: '<path d="M8 10V2.8M5 5.5L8 2.5l3 3M2.8 9.8v3a.7.7 0 0 0 .7.7h9a.7.7 0 0 0 .7-.7v-3"/>',
  panels: '<rect x="2" y="2.5" width="12" height="11" rx="1"/><path d="M6.2 2.5v11M10.5 2.5v11"/>',
  reset: '<path d="M3.5 8a4.5 4.5 0 1 0 1.2-3.1M3.5 2.8v2.7h2.7"/>',
};

export function createSvgIcon(name, size = 13) {
  const ns = "http://www.w3.org/2000/svg";
  const svg = document.createElementNS(ns, "svg");
  svg.setAttribute("viewBox", "0 0 16 16");
  svg.setAttribute("width", String(size));
  svg.setAttribute("height", String(size));
  svg.setAttribute("fill", "none");
  svg.setAttribute("stroke", "currentColor");
  svg.setAttribute("stroke-width", "1.25");
  svg.setAttribute("stroke-linecap", "round");
  svg.setAttribute("stroke-linejoin", "round");
  svg.setAttribute("aria-hidden", "true");
  svg.innerHTML = SVG_PATHS[name] || "";
  return svg;
}

/* ==================== 2. 时间码解析与格式化工具 ==================== */
export function formatTimecode(sec, showTenths = true) {
  const safe = Math.max(0, Number(sec) || 0);
  const h = Math.floor(safe / 3600);
  const m = Math.floor((safe % 3600) / 60);
  const sWhole = Math.floor(safe % 60);
  const tenths = Math.floor((safe - Math.floor(safe)) * 10 + 1e-6);
  const pad = (n) => String(n).padStart(2, "0");
  const base = h > 0 ? `${pad(h)}:${pad(m)}:${pad(sWhole)}` : `${pad(m)}:${pad(sWhole)}`;
  return showTenths ? `${base}.${tenths}` : base;
}

export function parseTimecodeInput(raw) {
  const str = String(raw || "").trim();
  if (!str) return null;
  if (/^\d+(\.\d+)?$/.test(str)) {
    return Number(str);
  }
  const parts = str.split(":").map((p) => Number(p));
  if (parts.some((n) => Number.isNaN(n) || n < 0)) return null;
  if (parts.length === 2) return parts[0] * 60 + parts[1];
  if (parts.length === 3) return parts[0] * 3600 + parts[1] * 60 + parts[2];
  return null;
}

/* ==================== 3. Scrubby Hot-Text 拖拉热文字控件 ==================== */
/**
 * 创建一个对标 PR / DaVinci 的 Scrubby Hot-Text 控件：
 * - 默认呈现等宽热文字（无边框、无上下箭头）
 * - 按住左右拖拽无级调参（Shift 5x 粗调 / Alt 0.1x 精调）
 * - 单击切换原地键盘直输（Enter 提交 / Esc 取消）
 */
export function createScrubbyControl({
  value = 0,
  min = 0,
  max = 999999,
  step = 0.1,
  precision = 2,
  mode = "number", // 'number' | 'timecode'
  suffix = "",
  placeholder = "-",
  title = null,
  onInput = null,
  onChange = null,
}) {
  const t = (k, vars) => (window.SolacutI18n ? window.SolacutI18n.t(k, vars) : k);
  const resolvedTitle = title || t("nle.scrubDefaultTitle");
  let curVal = value === null || value === undefined || value === "" ? null : Number(value);
  let editing = false;
  let minBound = min;
  let maxBound = max;

  const wrap = document.createElement("span");
  wrap.className = "scrubby-control mono-num";
  wrap.title = resolvedTitle;
  wrap.tabIndex = 0;

  const textSpan = document.createElement("span");
  textSpan.className = "scrubby-value";
  wrap.appendChild(textSpan);

  const clampVal = (v) => {
    const clamped = Math.max(minBound, Math.min(maxBound, v));
    return Number(clamped.toFixed(precision));
  };

  const renderDisplay = () => {
    if (curVal === null || Number.isNaN(curVal)) {
      textSpan.textContent = placeholder;
      wrap.classList.add("is-empty");
    } else {
      wrap.classList.remove("is-empty");
      textSpan.textContent =
        mode === "timecode"
          ? formatTimecode(curVal, true)
          : `${precision === 0 ? Math.round(curVal) : curVal.toFixed(precision)}${suffix}`;
    }
  };
  renderDisplay();

  const startInlineEdit = () => {
    if (editing) return;
    editing = true;
    wrap.classList.add("is-editing");
    const input = document.createElement("input");
    input.type = "text";
    input.className = "scrubby-inline-input mono-num";
    input.value =
      curVal === null || Number.isNaN(curVal)
        ? ""
        : mode === "timecode"
          ? formatTimecode(curVal, true)
          : String(curVal);

    const finish = (commit) => {
      if (!editing) return;
      editing = false;
      wrap.classList.remove("is-editing");
      if (commit) {
        const raw = input.value.trim();
        if (!raw && placeholder !== "-") {
          curVal = null;
          onChange?.(null);
        } else if (raw) {
          const parsed = mode === "timecode" ? parseTimecodeInput(raw) : Number(raw);
          if (parsed !== null && !Number.isNaN(parsed)) {
            curVal = clampVal(parsed);
            onInput?.(curVal);
            onChange?.(curVal);
          }
        }
      }
      input.remove();
      textSpan.classList.remove("hidden");
      renderDisplay();
    };

    input.addEventListener("keydown", (e) => {
      e.stopPropagation();
      // 修复 P2-04：中文输入法组合态按 Enter 不得提前提交
      if (e.isComposing || e.keyCode === 229) return;
      if (e.key === "Enter") {
        e.preventDefault();
        finish(true);
      } else if (e.key === "Escape") {
        e.preventDefault();
        finish(false);
      }
    });
    input.addEventListener("blur", () => finish(true));

    textSpan.classList.add("hidden");
    wrap.appendChild(input);
    input.focus();
    input.select();
  };

  wrap.addEventListener("keydown", (e) => {
    if (editing) return;
    if (e.key === "Enter" || e.key === " ") {
      e.preventDefault();
      e.stopPropagation();
      startInlineEdit();
    } else if (e.key === "ArrowRight" || e.key === "ArrowUp") {
      e.preventDefault();
      e.stopPropagation();
      const base = curVal ?? minBound;
      curVal = clampVal(base + step * (e.shiftKey ? 5 : 1));
      renderDisplay();
      onInput?.(curVal);
      onChange?.(curVal);
    } else if (e.key === "ArrowLeft" || e.key === "ArrowDown") {
      e.preventDefault();
      e.stopPropagation();
      const base = curVal ?? minBound;
      curVal = clampVal(base - step * (e.shiftKey ? 5 : 1));
      renderDisplay();
      onInput?.(curVal);
      onChange?.(curVal);
    }
  });

  wrap.addEventListener("pointerdown", (e) => {
    if (editing || e.button !== 0) return;
    e.preventDefault();
    const startX = e.clientX;
    const startVal = curVal === null || Number.isNaN(curVal) ? minBound : curVal;
    let moved = false;

    const onMove = (ev) => {
      const dx = ev.clientX - startX;
      if (!moved && Math.abs(dx) < 3) return;
      if (!moved) {
        moved = true;
        wrap.classList.add("is-scrubbing");
        document.body.classList.add("is-resizing");
      }
      const mult = ev.shiftKey ? 5 : ev.altKey ? 0.1 : 1;
      const next = clampVal(startVal + Math.round(dx / 2) * step * mult);
      if (next !== curVal) {
        curVal = next;
        renderDisplay();
        onInput?.(curVal);
      }
    };

    const onUp = () => {
      window.removeEventListener("pointermove", onMove);
      window.removeEventListener("pointerup", onUp);
      window.removeEventListener("pointercancel", onUp);
      wrap.classList.remove("is-scrubbing");
      document.body.classList.remove("is-resizing");
      if (moved) {
        onChange?.(curVal);
      } else {
        startInlineEdit();
      }
    };

    window.addEventListener("pointermove", onMove);
    window.addEventListener("pointerup", onUp);
    window.addEventListener("pointercancel", onUp);
  });

  return {
    el: wrap,
    setValue(v) {
      if (editing) return;
      curVal = v === null || v === undefined || v === "" ? null : Number(v);
      renderDisplay();
    },
    setBounds(newMin, newMax) {
      if (typeof newMin === "number" && !Number.isNaN(newMin)) minBound = newMin;
      if (typeof newMax === "number" && !Number.isNaN(newMax)) maxBound = Math.max(minBound, newMax);
    },
    getValue() {
      return curVal;
    },
  };
}

/** 将现有 <input type="number"> 绑定并替换为原生 Scrubby Hot-Text（保持原 input ID 与 .value API 完全兼容！） */
export function enhanceNumberInputToScrubby(inputEl, opts = {}) {
  if (!inputEl || inputEl.dataset.scrubbyEnhanced === "1") return null;
  inputEl.dataset.scrubbyEnhanced = "1";

  const min = inputEl.min !== "" ? Number(inputEl.min) : -999999;
  const max = inputEl.max !== "" ? Number(inputEl.max) : 999999;
  const step = inputEl.step && inputEl.step !== "any" ? Number(inputEl.step) : 1;
  const precision = step < 1 ? (String(step).split(".")[1]?.length || 1) : 0;

  const t = (k, vars) => (window.SolacutI18n ? window.SolacutI18n.t(k, vars) : k);
  const ctrl = createScrubbyControl({
    value: inputEl.value !== "" ? Number(inputEl.value) : null,
    min,
    max,
    step,
    precision,
    placeholder: inputEl.placeholder || t("common.auto"),
    suffix: opts.suffix || "",
    title: `${inputEl.title || ""}${t("nle.scrubHintSuffix")}`.trim(),
    onChange: (val) => {
      inputEl.value = val === null ? "" : String(val);
      inputEl.dispatchEvent(new Event("change", { bubbles: true }));
    },
  });

  ctrl.el.classList.add("scrubby-field-box");
  inputEl.classList.add("scrubby-hidden-input");
  inputEl.tabIndex = -1;
  inputEl.setAttribute("aria-hidden", "true");
  inputEl.insertAdjacentElement("afterend", ctrl.el);

  // 修复 P2-02：点击关联 <label for="..."> 时将焦点平滑转发到可键盘操作的 Scrubby 控件
  inputEl.addEventListener("focus", () => {
    ctrl.el.focus({ preventScroll: true });
  });

  // 拦截脚本直接写 inputEl.value 时同步更新显示
  const syncFromInput = () => {
    ctrl.setValue(inputEl.value !== "" ? Number(inputEl.value) : null);
  };
  const syncLocaleFromInput = () => {
    const newPh = inputEl.placeholder || t("common.auto");
    ctrl.el.title = `${inputEl.title || ""}${t("nle.scrubHintSuffix")}`.trim();
    if (ctrl.getValue() === null) {
      const valSpan = ctrl.el.querySelector(".scrubby-value");
      if (valSpan) valSpan.textContent = newPh;
    }
  };
  inputEl.addEventListener("change", syncFromInput);
  inputEl._syncScrubby = syncFromInput;
  inputEl._syncScrubbyLocale = syncLocaleFromInput;
  return ctrl;
}

/* ==================== 4. 原生暗房右键上下文菜单（遵循 modern-web-guidance `menu`） ==================== */
export class NleContextMenu {
  constructor() {
    this._anchor = null;
    this._popover = null;
    this._list = null;
    this._lastFocus = null;
    this._supportsPopover = Object.hasOwn(HTMLElement.prototype, "popover");
    this._supportsAnchor = CSS.supports("position-area", "block-end");
    this._supportsFocusgroup = "focusGroup" in HTMLElement.prototype;
    this._buildDom();
  }

  _buildDom() {
    // 遵循 `menu` 指南：在菜单 popover 紧前方放零尺寸 fixed 锚点，确保右键在指针坐标弹出并支持翻转防裁剪
    this._anchor = document.createElement("div");
    this._anchor.id = "ctx-menu-anchor";
    this._anchor.className = "ctx-menu-anchor";
    this._anchor.setAttribute("aria-hidden", "true");

    this._popover = document.createElement("div");
    this._popover.id = "nle-context-menu";
    this._popover.className = "nle-context-menu";
    if (this._supportsPopover) {
      this._popover.setAttribute("popover", "auto");
    }

    // 遵循 `menu` 指南：focusgroup="menu nomemory" 放在内层容器而非 popover 自身
    this._list = document.createElement("div");
    this._list.className = "ctx-menu-list";
    this._list.setAttribute("focusgroup", "menu nomemory");
    this._list.setAttribute("aria-label", window.SolacutI18n ? window.SolacutI18n.t("nle.contextMenuAria") : "Context Menu");
    if (!this._supportsFocusgroup) {
      this._list.setAttribute("role", "menu");
    }

    this._popover.appendChild(this._list);
    document.body.append(this._anchor, this._popover);

    // 键盘回退（仅当不支持原生 focusgroup 时挂载 ArrowUp/ArrowDown/Home/End）
    if (!this._supportsFocusgroup) {
      this._list.addEventListener(
        "keydown",
        (e) => {
          if (!["ArrowDown", "ArrowUp", "Home", "End"].includes(e.key)) return;
          const items = Array.from(this._list.querySelectorAll("button:not(:disabled)"));
          if (!items.length) return;
          const cur = items.indexOf(document.activeElement);
          const next =
            e.key === "Home"
              ? 0
              : e.key === "End"
                ? items.length - 1
                : (cur + (e.key === "ArrowUp" ? -1 : 1) + items.length) % items.length;
          e.preventDefault();
          e.stopImmediatePropagation();
          items[next].focus({ preventScroll: true });
        },
        true
      );
    }

    // Tab 退出关闭，Esc 恢复焦点
    this._popover.addEventListener("keydown", (e) => {
      if (e.key === "Tab") {
        this.close();
      } else if (e.key === "Escape") {
        e.preventDefault();
        this.close(true);
      }
    });

    // 非原生 popover 回退点击外部关闭
    if (!this._supportsPopover) {
      document.addEventListener("pointerdown", (e) => {
        if (
          this._popover.classList.contains("popover-fallback-open") &&
          !this._popover.contains(e.target)
        ) {
          this.close();
        }
      });
    }
  }

  /**
   * @param {number} x clientX
   * @param {number} y clientY
   * @param {Array<{type?: 'separator', label?: string, hint?: string, danger?: boolean, checked?: boolean, disabled?: boolean, onSelect?: () => void}>} items
   */
  open(x, y, items) {
    if (!items || items.length === 0) return;
    if (window.SolacutI18n) {
      this._list.setAttribute("aria-label", window.SolacutI18n.t("nle.contextMenuAria"));
    }
    this._lastFocus = document.activeElement;
    this._list.replaceChildren();

    for (const item of items) {
      if (item.type === "separator") {
        this._list.appendChild(document.createElement("hr"));
        continue;
      }
      const btn = document.createElement("button");
      btn.type = "button";
      btn.className = `ctx-menu-item${item.danger ? " is-danger" : ""}`;
      if (item.disabled) btn.disabled = true;

      if (typeof item.checked === "boolean") {
        btn.setAttribute("role", "menuitemcheckbox");
        btn.setAttribute("aria-checked", item.checked ? "true" : "false");
      } else if (!this._supportsFocusgroup) {
        btn.setAttribute("role", "menuitem");
      }

      const labelSpan = document.createElement("span");
      labelSpan.className = "ctx-item-label";
      labelSpan.textContent = item.label || "";
      btn.appendChild(labelSpan);

      if (item.hint) {
        const hintSpan = document.createElement("span");
        hintSpan.className = "hint mono-num";
        hintSpan.setAttribute("aria-hidden", "true");
        hintSpan.textContent = item.hint;
        btn.appendChild(hintSpan);
      }

      btn.addEventListener("pointermove", () => {
        if (!btn.disabled && document.activeElement !== btn) {
          btn.focus({ focusVisible: false, preventScroll: true });
        }
      });

      btn.addEventListener("click", (e) => {
        const fromKeyboard = e.detail === 0;
        this.close(fromKeyboard);
        if (!item.disabled && typeof item.onSelect === "function") {
          item.onSelect();
        }
      });

      this._list.appendChild(btn);
    }

    this._anchor.style.left = `${Math.round(x)}px`;
    this._anchor.style.top = `${Math.round(y)}px`;

    if (this._supportsPopover) {
      try {
        if (this._popover.matches(":popover-open")) this._popover.hidePopover();
      } catch {}
      try {
        this._popover.showPopover({ source: this._anchor });
      } catch {
        this._popover.showPopover();
      }
    } else {
      this._popover.classList.add("popover-fallback-open");
    }

    // 仅在不支持 CSS Anchor Positioning 时由脚本贴边防裁剪
    if (!this._supportsAnchor) {
      requestAnimationFrame(() => {
        const rect = this._popover.getBoundingClientRect();
        const left = Math.max(4, Math.min(x, window.innerWidth - rect.width - 6));
        const top = Math.max(4, Math.min(y, window.innerHeight - rect.height - 6));
        this._popover.style.left = `${Math.round(left)}px`;
        this._popover.style.top = `${Math.round(top)}px`;
      });
    }
  }

  close(restoreFocusRing = false) {
    if (this._supportsPopover) {
      try {
        if (this._popover.matches(":popover-open")) this._popover.hidePopover();
      } catch {}
    } else {
      this._popover.classList.remove("popover-fallback-open");
    }
    if (this._lastFocus && typeof this._lastFocus.focus === "function") {
      try {
        this._lastFocus.focus({ focusVisible: restoreFocusRing, preventScroll: true });
      } catch {}
    }
    this._lastFocus = null;
  }
}
