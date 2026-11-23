/* ClipStudio NLE 原生级顶部应用菜单栏引擎（对标 DaVinci Resolve 21.1 Top Menu Bar）
 * 严格遵循 modern-web-guidance `app-menus` + `viewport-aware-dropdowns`：
 * - 单 Tab 停靠站（Roving tabindex），ArrowLeft / ArrowRight 跨一级菜单无缝切换；
 * - 任一菜单打开后，鼠标悬停至另一一级菜单瞬间切换（零过渡闪烁）；
 * - 原生 popover="auto" 顶层展示 + CSS Anchor Positioning 边缘自翻转 + 完整键盘/脚本回退；
 * - 动态渲染菜单项启用/禁用/勾选状态与右侧等宽快捷键提示。
 */

export class NleAppMenuBar {
  /**
   * @param {HTMLElement} containerEl role="menubar" 容器
   * @param {Array<{id: string, label: string, getItems: () => Array}>} menus
   */
  constructor(containerEl, menus) {
    this.containerEl = containerEl;
    this.menus = menus || [];
    this.triggers = [];
    this.popovers = new Map(); // trigger -> popoverEl
    this.menuDefByTrigger = new Map();
    this._windowBlurred = false;
    this._enterOnOpen = new WeakSet();
    this._openMenuEl = null;

    this._build();
    this._bindGlobal();
  }

  _build() {
    if (!this.containerEl) return;
    this.containerEl.replaceChildren();
    this.containerEl.setAttribute("role", "menubar");
    this.containerEl.setAttribute("aria-label", "应用菜单");

    const hasPopover = Object.hasOwn(HTMLElement.prototype, "popover");

    this.menus.forEach((def, idx) => {
      const trigger = document.createElement("button");
      trigger.type = "button";
      trigger.id = `menubar-trigger-${def.id}`;
      trigger.className = "menubar-trigger";
      trigger.setAttribute("role", "menuitem");
      trigger.setAttribute("aria-haspopup", "menu");
      trigger.setAttribute("aria-expanded", "false");
      trigger.tabIndex = idx === 0 ? 0 : -1;
      trigger.textContent = def.label;

      const popId = `menubar-pop-${def.id}`;
      trigger.setAttribute("aria-controls", popId);

      const pop = document.createElement("div");
      pop.id = popId;
      pop.className = "menubar-popover";
      if (hasPopover) {
        pop.setAttribute("popover", "auto");
      }

      const list = document.createElement("div");
      list.className = "menubar-menu-list";
      list.setAttribute("focusgroup", "menu nomemory");
      list.setAttribute("role", "menu");
      list.setAttribute("aria-labelledby", trigger.id);
      pop.appendChild(list);

      this.triggers.push(trigger);
      this.popovers.set(trigger, pop);
      this.menuDefByTrigger.set(trigger, def);

      // 同步在 beforetoggle 刷新菜单项与 aria-expanded，消除异步 toggle 造成的 1 帧状态滞后
      pop.addEventListener("beforetoggle", (e) => {
        const opening = e.newState === "open";
        if (opening) {
          this._renderMenuItems(trigger, pop);
          this._positionFallbackIfNeeded(trigger, pop);
          this._openMenuEl = pop;
        } else if (this._openMenuEl === pop) {
          this._openMenuEl = null;
        }
        trigger.setAttribute("aria-expanded", opening ? "true" : "false");
      });

      pop.addEventListener("toggle", (e) => {
        const opened = e.newState === "open";
        trigger.setAttribute("aria-expanded", opened ? "true" : "false");
        if (opened) {
          this._openMenuEl = pop;
          this._positionFallbackIfNeeded(trigger, pop);
          if (this._enterOnOpen.has(pop)) {
            this._enterOnOpen.delete(pop);
            this._focusFirstItem(pop);
          }
        } else {
          if (this._openMenuEl === pop) this._openMenuEl = null;
        }
      });

      // 点击通过 showPopover({ source: trigger }) 建立精确隐式锚点并即时渲染最新勾选/禁用态
      trigger.addEventListener("click", (e) => {
        e.preventDefault();
        e.stopPropagation();
        this.toggleMenu(trigger, false);
      });

      // 已有任一菜单打开时，鼠标滑过相邻一级菜单瞬间切换（原生桌面软件体验）
      trigger.addEventListener("pointerenter", () => {
        if (!this._isAnyOpen()) return;
        if (this._isPopOpen(pop)) return;
        this.openMenu(trigger, false);
      });

      // 键盘导航（一级触发器）
      trigger.addEventListener("keydown", (e) => {
        if (e.key === "ArrowRight") {
          e.preventDefault();
          e.stopPropagation();
          this._moveToTrigger(trigger, 1, this._isAnyOpen());
        } else if (e.key === "ArrowLeft") {
          e.preventDefault();
          e.stopPropagation();
          this._moveToTrigger(trigger, -1, this._isAnyOpen());
        } else if (e.key === "ArrowDown" || e.key === "Home") {
          e.preventDefault();
          e.stopPropagation();
          this.openMenu(trigger, true);
        } else if (e.key === "End") {
          e.preventDefault();
          e.stopPropagation();
          this.openMenu(trigger, false);
          this._focusLastItem(pop);
        } else if (e.key === "Escape") {
          if (this._isAnyOpen()) {
            e.preventDefault();
            this.closeAll();
          }
        }
      });

      // 菜单内部键盘导航（左右切相邻菜单、上下循环项、Esc 关菜单回触发器）
      list.addEventListener("keydown", (e) => {
        if (e.key === "ArrowRight") {
          e.preventDefault();
          e.stopPropagation();
          this._moveToTrigger(trigger, 1, true);
        } else if (e.key === "ArrowLeft") {
          e.preventDefault();
          e.stopPropagation();
          this._moveToTrigger(trigger, -1, true);
        } else if (e.key === "Escape") {
          e.preventDefault();
          e.stopPropagation();
          this.closeAll();
          trigger.focus({ preventScroll: true });
        } else if (e.key === "Tab") {
          this.closeAll();
        } else if (
          !("focusGroup" in HTMLElement.prototype) &&
          ["ArrowDown", "ArrowUp", "Home", "End"].includes(e.key)
        ) {
          const items = this._getEnabledItems(pop);
          if (!items.length) return;
          const idx = items.indexOf(document.activeElement);
          let nextIdx = 0;
          if (e.key === "Home") nextIdx = 0;
          else if (e.key === "End") nextIdx = items.length - 1;
          else if (e.key === "ArrowDown") nextIdx = idx < 0 ? 0 : (idx + 1) % items.length;
          else if (e.key === "ArrowUp") nextIdx = idx <= 0 ? items.length - 1 : idx - 1;
          e.preventDefault();
          e.stopPropagation();
          items[nextIdx]?.focus({ preventScroll: true });
        }
      });

      this.containerEl.append(trigger, pop);
    });
  }

  _isPopOpen(pop) {
    if (!pop) return false;
    try {
      if (pop.matches(":popover-open")) return true;
    } catch {}
    return pop.classList.contains("popover-fallback-open");
  }

  _isAnyOpen() {
    for (const pop of this.popovers.values()) {
      if (this._isPopOpen(pop)) return true;
    }
    return false;
  }

  _getEnabledItems(pop) {
    return Array.from(pop.querySelectorAll(".menubar-item")).filter(
      (b) => !b.disabled && b.getAttribute("aria-disabled") !== "true"
    );
  }

  _focusFirstItem(pop) {
    const items = this._getEnabledItems(pop);
    items[0]?.focus({ preventScroll: true });
  }

  _focusLastItem(pop) {
    const items = this._getEnabledItems(pop);
    items[items.length - 1]?.focus({ preventScroll: true });
  }

  _positionFallbackIfNeeded(trigger, pop) {
    if (CSS.supports("position-area: block-end")) return;
    const r = trigger.getBoundingClientRect();
    const popW = pop.offsetWidth || 210;
    const vw = window.innerWidth;
    const left = Math.max(4, Math.min(r.left, vw - popW - 6));
    pop.style.left = `${Math.round(left)}px`;
    pop.style.top = `${Math.round(r.bottom + 1)}px`;
  }

  _renderMenuItems(trigger, pop) {
    const def = this.menuDefByTrigger.get(trigger);
    const list = pop.querySelector(".menubar-menu-list");
    if (!def || !list) return;

    const items = typeof def.getItems === "function" ? def.getItems() : [];
    list.replaceChildren();

    for (const item of items) {
      if (!item) continue;
      if (item.type === "separator") {
        const hr = document.createElement("div");
        hr.className = "menubar-sep";
        hr.setAttribute("role", "separator");
        list.appendChild(hr);
        continue;
      }

      const btn = document.createElement("button");
      btn.type = "button";
      btn.className = "menubar-item";
      if (item.danger) btn.classList.add("danger");
      if (typeof item.checked === "boolean") {
        btn.setAttribute("role", "menuitemcheckbox");
        btn.setAttribute("aria-checked", item.checked ? "true" : "false");
      } else {
        btn.setAttribute("role", "menuitem");
      }
      if (item.disabled) btn.disabled = true;

      const checkSpan = document.createElement("span");
      checkSpan.className = "menubar-check";
      checkSpan.textContent = item.checked ? "✓" : "";

      const lblSpan = document.createElement("span");
      lblSpan.className = "menubar-label";
      lblSpan.textContent = item.label || "";

      btn.append(checkSpan, lblSpan);

      if (item.hint) {
        const hintSpan = document.createElement("span");
        hintSpan.className = "menubar-hint mono-num";
        hintSpan.textContent = item.hint;
        btn.appendChild(hintSpan);
      }

      btn.addEventListener("click", (e) => {
        e.stopPropagation();
        if (item.disabled) return;
        this.closeAll();
        try {
          item.onSelect?.();
        } catch {}
      });

      list.appendChild(btn);
    }
  }

  openMenu(trigger, focusFirst = false) {
    const pop = this.popovers.get(trigger);
    if (!pop) return;
    this.closeAll(pop);
    this._renderMenuItems(trigger, pop);
    this.triggers.forEach((t) => {
      t.tabIndex = t === trigger ? 0 : -1;
    });

    if (focusFirst) this._enterOnOpen.add(pop);

    if (Object.hasOwn(HTMLElement.prototype, "popover") && typeof pop.showPopover === "function") {
      try {
        pop.showPopover({ source: trigger });
      } catch {
        try {
          pop.showPopover();
        } catch {}
      }
    } else {
      pop.classList.add("popover-fallback-open");
      trigger.setAttribute("aria-expanded", "true");
      this._openMenuEl = pop;
      this._positionFallbackIfNeeded(trigger, pop);
      if (focusFirst) this._focusFirstItem(pop);
    }
  }

  toggleMenu(trigger, focusFirst = false) {
    const pop = this.popovers.get(trigger);
    if (!pop) return;
    if (this._isPopOpen(pop)) {
      this.closeAll();
    } else {
      this.openMenu(trigger, focusFirst);
    }
  }

  closeAll(exceptPop = null) {
    for (const [trigger, pop] of this.popovers.entries()) {
      if (pop === exceptPop) continue;
      if (this._isPopOpen(pop)) {
        if (Object.hasOwn(HTMLElement.prototype, "popover") && typeof pop.hidePopover === "function") {
          try {
            pop.hidePopover();
          } catch {}
        }
        pop.classList.remove("popover-fallback-open");
      }
      trigger.setAttribute("aria-expanded", "false");
    }
    if (!exceptPop) this._openMenuEl = null;
  }

  _moveToTrigger(fromTrigger, delta, keepOpen) {
    const idx = this.triggers.indexOf(fromTrigger);
    if (idx < 0) return;
    const next = this.triggers[(idx + delta + this.triggers.length) % this.triggers.length];
    this.triggers.forEach((t) => {
      t.tabIndex = t === next ? 0 : -1;
    });
    if (keepOpen) {
      this.openMenu(next, false);
    } else {
      this.closeAll();
    }
    next.focus({ preventScroll: true });
  }

  _bindGlobal() {
    window.addEventListener("blur", () => {
      this._windowBlurred = true;
      this.closeAll();
    });
    window.addEventListener("focus", () => {
      setTimeout(() => {
        this._windowBlurred = false;
      }, 60);
    });
    document.addEventListener("pointerdown", (e) => {
      if (!this._isAnyOpen()) return;
      if (!this.containerEl.contains(e.target)) {
        this.closeAll();
      }
    });
  }
}
