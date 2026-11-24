/* ClipStudio NLE 自由停靠布局引擎（Premiere Pro 级 Binary Split Tree + 5-Zone 拖拽停靠 + 反引号全屏放大）
 * 零外部依赖，支持：
 * 1. 任意面板拖拽到目标格子的 上/下/左/右 裂变停靠、拖入中心/Tab栏合并为标签组或重排顺序、拖至工作区外缘全高/全宽停靠；
 * 2. 遵循 modern-web-guidance `move-dom-element-without-losing-state`：优先使用原生 Element.prototype.moveBefore 原子重排 DOM，
 *    拖拽或反引号最大化/还原期间绝不中断 <video> 播放与输入框焦点；
 * 3. 多套专业工作区预设（剪辑 / 审片 / 字幕 / 交付），每套独立持久化到 localStorage 并支持一键重置。
 */

const STORAGE_KEY_V2 = "clipstudio_nle_dock_v5";
const MIN_SPLIT_RATIO = 0.12;
const MAX_SPLIT_RATIO = 0.88;
const DRAG_THRESHOLD_PX = 6;
const ROOT_EDGE_ZONE_PX = 20;

let _uidSeq = 1;
function nextNodeId(prefix = "n") {
  _uidSeq += 1;
  return `${prefix}_${Date.now().toString(36)}_${_uidSeq}`;
}

/** 原子移动 DOM 节点（优先使用现代原生 moveBefore 保留视频播放与输入焦点状态，优雅回退 insertBefore） */
export function atomicMove(parent, node, refNode = null) {
  if (!parent || !node) return;
  if (node.parentElement === parent && node.nextSibling === refNode) return;
  if ("moveBefore" in Element.prototype) {
    try {
      parent.moveBefore(node, refNode);
      return;
    } catch {
      // 某些跨阴影/边界场景兜底回退
    }
  }
  parent.insertBefore(node, refNode);
}

function deepClone(obj) {
  return JSON.parse(JSON.stringify(obj));
}

function makeTabs(tabs, active = null) {
  const list = [...tabs];
  return {
    type: "tabs",
    id: nextNodeId("t"),
    tabs: list,
    active: active && list.includes(active) ? active : list[0],
  };
}

function makeSplit(dir, ratio, first, second) {
  return {
    type: "split",
    id: nextNodeId("s"),
    dir, // 'h' = 左右横分, 'v' = 上下纵分
    ratio,
    first,
    second,
  };
}

/** 4 套对标 PR / DaVinci Resolve 的默认工作区树形预设（含一等双层多轨时间线面板） */
export function createPresetTree(presetId) {
  switch (presetId) {
    case "review":
      // 审片模式：左大屏监视器 + 左下双层时间线 | 右高光/字幕并排
      return makeSplit(
        "h",
        0.56,
        makeSplit(
          "v",
          0.58,
          makeTabs(["monitor"], "monitor"),
          makeTabs(["timeline"], "timeline")
        ),
        makeSplit(
          "v",
          0.52,
          makeTabs(["clips"], "clips"),
          makeTabs(["subtitles", "transcript", "queue"], "subtitles")
        )
      );

    case "subtitle":
      // 字幕精校模式：左上监视器 + 左下多轨时间线 | 右半全高双语字幕与全片逐字稿
      return makeSplit(
        "h",
        0.5,
        makeSplit(
          "v",
          0.52,
          makeTabs(["monitor"], "monitor"),
          makeTabs(["timeline", "media"], "timeline")
        ),
        makeSplit(
          "v",
          0.32,
          makeTabs(["clips"], "clips"),
          makeTabs(["subtitles", "transcript", "inspector"], "subtitles")
        )
      );

    case "deliver":
      // 交付模式：左媒体池 | 中上监视器 + 中下时间线/高光/字幕/逐字稿 | 右上检查器(交付) + 右下队列
      return makeSplit(
        "h",
        0.18,
        makeTabs(["media"], "media"),
        makeSplit(
          "h",
          0.6,
          makeSplit(
            "v",
            0.54,
            makeTabs(["monitor"], "monitor"),
            makeTabs(["timeline", "clips", "subtitles", "transcript"], "timeline")
          ),
          makeSplit(
            "v",
            0.66,
            makeTabs(["inspector"], "inspector"),
            makeTabs(["queue"], "queue")
          )
        )
      );

    case "edit":
    default:
      // 剪辑默认模式（对标 DaVinci Edit Page）：上层 媒体池 | 节目监视器 | 检查器，下层 双层多轨时间线 | 高光&字幕&逐字稿
      return makeSplit(
        "v",
        0.55,
        makeSplit(
          "h",
          0.19,
          makeTabs(["media", "history", "queue"], "media"),
          makeSplit(
            "h",
            0.73,
            makeTabs(["monitor"], "monitor"),
            makeTabs(["inspector"], "inspector")
          )
        ),
        makeSplit(
          "h",
          0.62,
          makeTabs(["timeline"], "timeline"),
          makeTabs(["clips", "subtitles", "transcript"], "clips")
        )
      );
  }
}

export class DockEngine {
  /**
   * @param {Object} opts
   * @param {HTMLElement} opts.rootEl 工作区挂载根容器
   * @param {HTMLElement} opts.parkingEl 隐藏面板的离屏保态停泊容器
   * @param {Array<{id: string, title: string, icon?: string, el: HTMLElement}>} opts.panels
   * @param {(state: Object) => void} [opts.onChange]
   */
  constructor({ rootEl, parkingEl, panels, onChange = null, onTabContextMenu = null }) {
    this.rootEl = rootEl;
    this.parkingEl = parkingEl;
    this.onChange = onChange;
    this.onTabContextMenu = onTabContextMenu;

    this.panelMap = new Map();
    this.panelOrder = [];
    for (const p of panels) {
      this.panelMap.set(p.id, p);
      this.panelOrder.push(p.id);
      if (p.el && this.parkingEl) {
        atomicMove(this.parkingEl, p.el, null);
      }
    }

    this.activePreset = "edit";
    this.presetTrees = {};
    this.tree = null;
    this.focusedPanelId = "monitor";
    this.hoveredPanelId = null;
    this.maximizedPanelId = null;

    this._drag = null;
    this._dropOverlay = null;
    this._dragGhost = null;
    this._maxOverlay = null;
    this._treeHost = null;

    this._initChrome();
    this._loadState();
    this._bindGlobalEvents();
    this.render();
  }

  _initChrome() {
    this.rootEl.classList.add("dock-root");
    this.rootEl.replaceChildren();

    this._treeHost = document.createElement("div");
    this._treeHost.className = "dock-tree-host";
    this.rootEl.appendChild(this._treeHost);

    this._maxOverlay = document.createElement("div");
    this._maxOverlay.className = "dock-max-overlay hidden";
    this.rootEl.appendChild(this._maxOverlay);

    this._dropOverlay = document.createElement("div");
    this._dropOverlay.className = "dock-drop-overlay hidden";
    this.rootEl.appendChild(this._dropOverlay);

    this._dragGhost = document.createElement("div");
    this._dragGhost.className = "dock-drag-ghost hidden";
    document.body.appendChild(this._dragGhost);
  }

  _loadState() {
    let saved = null;
    try {
      const raw = localStorage.getItem(STORAGE_KEY_V2);
      if (raw) saved = JSON.parse(raw);
    } catch {
      saved = null;
    }

    const validPresets = ["edit", "review", "subtitle", "deliver"];
    this.activePreset =
      saved && validPresets.includes(saved.activePreset) ? saved.activePreset : "edit";

    const needsTranscriptMigration =
      Boolean(saved) && Number(saved.version || 0) < 3 && this.panelMap.has("transcript");

    this.presetTrees = {};
    for (const pid of validPresets) {
      const candidate = saved?.presetTrees?.[pid];
      const normalized = candidate ? this._normalizeTree(deepClone(candidate)) : null;
      const tree = normalized || createPresetTree(pid);
      if (needsTranscriptMigration && !this._findTabsNodeContaining("transcript", tree)) {
        const targetGroup =
          this._findTabsNodeContaining("subtitles", tree) ||
          this._findTabsNodeContaining("clips", tree);
        if (targetGroup && !targetGroup.tabs.includes("transcript")) {
          targetGroup.tabs.push("transcript");
        }
      }
      this.presetTrees[pid] = tree;
    }
    this.tree = this.presetTrees[this.activePreset];
    if (needsTranscriptMigration) {
      this._saveState();
    }
  }

  _saveState() {
    if (this.tree) {
      this.presetTrees[this.activePreset] = this.tree;
    }
    try {
      localStorage.setItem(
        STORAGE_KEY_V2,
        JSON.stringify({
          version: 3,
          activePreset: this.activePreset,
          presetTrees: this.presetTrees,
        })
      );
    } catch {
      // 忽略私密模式配额限制
    }
  }

  /** 校验、去重并自动剪枝退化的节点 */
  _normalizeTree(node) {
    const seen = new Set();
    const clean = (n) => {
      if (!n || typeof n !== "object") return null;
      if (n.type === "tabs") {
        const tabs = Array.isArray(n.tabs)
          ? n.tabs.filter((id) => this.panelMap.has(id) && !seen.has(id) && (seen.add(id), true))
          : [];
        if (tabs.length === 0) return null;
        return {
          type: "tabs",
          id: typeof n.id === "string" && n.id ? n.id : nextNodeId("t"),
          tabs,
          active: tabs.includes(n.active) ? n.active : tabs[0],
        };
      }
      if (n.type === "split") {
        const first = clean(n.first);
        const second = clean(n.second);
        if (!first && !second) return null;
        if (!first) return second;
        if (!second) return first;
        const dir = n.dir === "v" ? "v" : "h";
        const ratio = Math.max(MIN_SPLIT_RATIO, Math.min(MAX_SPLIT_RATIO, Number(n.ratio) || 0.5));
        return {
          type: "split",
          id: typeof n.id === "string" && n.id ? n.id : nextNodeId("s"),
          dir,
          ratio,
          first,
          second,
        };
      }
      return null;
    };
    return clean(node);
  }

  getOpenPanelIds() {
    const ids = new Set();
    const walk = (n) => {
      if (!n) return;
      if (n.type === "tabs") {
        n.tabs.forEach((id) => ids.add(id));
      } else if (n.type === "split") {
        walk(n.first);
        walk(n.second);
      }
    };
    walk(this.tree);
    return ids;
  }

  _findTabsNodeById(nodeId, node = this.tree) {
    if (!node) return null;
    if (node.type === "tabs") return node.id === nodeId ? node : null;
    return this._findTabsNodeById(nodeId, node.first) || this._findTabsNodeById(nodeId, node.second);
  }

  _findTabsNodeContaining(panelId, node = this.tree) {
    if (!node) return null;
    if (node.type === "tabs") return node.tabs.includes(panelId) ? node : null;
    return (
      this._findTabsNodeContaining(panelId, node.first) ||
      this._findTabsNodeContaining(panelId, node.second)
    );
  }

  _removePanelFromTree(node, panelId) {
    if (!node) return null;
    if (node.type === "tabs") {
      const idx = node.tabs.indexOf(panelId);
      if (idx === -1) return node;
      const nextTabs = node.tabs.filter((id) => id !== panelId);
      if (nextTabs.length === 0) return null;
      const nextActive =
        node.active === panelId ? nextTabs[Math.min(idx, nextTabs.length - 1)] : node.active;
      return { ...node, tabs: nextTabs, active: nextActive };
    }
    if (node.type === "split") {
      const first = this._removePanelFromTree(node.first, panelId);
      const second = this._removePanelFromTree(node.second, panelId);
      if (!first && !second) return null;
      if (!first) return second;
      if (!second) return first;
      return { ...node, first, second };
    }
    return node;
  }

  _insertPanelIntoTarget(node, targetNodeId, panelId, zone, insertIndex = -1) {
    if (!node) return null;
    if (node.type === "tabs" && node.id === targetNodeId) {
      if (zone === "center") {
        const nextTabs = node.tabs.filter((id) => id !== panelId);
        if (insertIndex >= 0 && insertIndex <= nextTabs.length) {
          nextTabs.splice(insertIndex, 0, panelId);
        } else {
          nextTabs.push(panelId);
        }
        return { ...node, tabs: nextTabs, active: panelId };
      }
      const newLeaf = makeTabs([panelId], panelId);
      if (zone === "left") return makeSplit("h", 0.45, newLeaf, node);
      if (zone === "right") return makeSplit("h", 0.55, node, newLeaf);
      if (zone === "top") return makeSplit("v", 0.45, newLeaf, node);
      if (zone === "bottom") return makeSplit("v", 0.55, node, newLeaf);
      return node;
    }
    if (node.type === "split") {
      return {
        ...node,
        first: this._insertPanelIntoTarget(node.first, targetNodeId, panelId, zone, insertIndex),
        second: this._insertPanelIntoTarget(node.second, targetNodeId, panelId, zone, insertIndex),
      };
    }
    return node;
  }

  /** 切换工作区预设 */
  setPreset(presetId) {
    if (!["edit", "review", "subtitle", "deliver"].includes(presetId)) return;
    this.maximizedPanelId = null;
    this.activePreset = presetId;
    if (!this.presetTrees[presetId]) {
      this.presetTrees[presetId] = createPresetTree(presetId);
    }
    this.tree = this.presetTrees[presetId];
    this._saveState();
    this.render();
    this._emitChange("preset");
  }

  /** 重置当前预设为出厂树形布局 */
  resetCurrentPreset() {
    this.maximizedPanelId = null;
    this.tree = createPresetTree(this.activePreset);
    this.presetTrees[this.activePreset] = this.tree;
    this._saveState();
    this.render();
    this._emitChange("reset");
  }

  /** 激活并聚焦指定面板（若面板当前被隐藏则自动挂回工作区） */
  focusPanel(panelId) {
    if (!this.panelMap.has(panelId)) return;
    const holder = this._findTabsNodeContaining(panelId);
    if (!holder) {
      this.showPanel(panelId);
      return;
    }
    const changed = holder.active !== panelId || this.focusedPanelId !== panelId;
    holder.active = panelId;
    this.focusedPanelId = panelId;
    if (this.maximizedPanelId && this.maximizedPanelId !== panelId) {
      this.maximizedPanelId = null;
    }
    if (changed) {
      this._saveState();
      this.render();
      this._emitChange("focus");
    }
  }

  /** 显示隐藏中的面板 */
  showPanel(panelId) {
    if (!this.panelMap.has(panelId)) return;
    const existing = this._findTabsNodeContaining(panelId);
    if (existing) {
      this.focusPanel(panelId);
      return;
    }
    // 修复 P1-05：最大化状态下唤起其他未打开面板时必须先退出最大化遮罩
    if (this.maximizedPanelId && this.maximizedPanelId !== panelId) {
      this.maximizedPanelId = null;
    }
    const leaf = makeTabs([panelId], panelId);
    if (!this.tree) {
      this.tree = leaf;
    } else if (panelId === "media" || panelId === "queue" || panelId === "history") {
      this.tree = makeSplit("h", 0.2, leaf, this.tree);
    } else if (panelId === "inspector") {
      this.tree = makeSplit("h", 0.76, this.tree, leaf);
    } else if (panelId === "timeline") {
      this.tree = makeSplit("v", 0.56, this.tree, leaf);
    } else {
      this.tree = makeSplit("v", 0.55, this.tree, leaf);
    }
    this.focusedPanelId = panelId;
    this._saveState();
    this.render();
    this._emitChange("show");
  }

  /** 关闭/隐藏单个面板（确保工作区至少保留 1 个可见面板） */
  hidePanel(panelId) {
    const openIds = this.getOpenPanelIds();
    if (!openIds.has(panelId) || openIds.size <= 1) return false;
    if (this.maximizedPanelId === panelId) {
      this.maximizedPanelId = null;
    }
    const nextTree = this._removePanelFromTree(this.tree, panelId);
    if (!nextTree) return false;
    this.tree = nextTree;
    if (this.focusedPanelId === panelId) {
      const remaining = Array.from(this.getOpenPanelIds());
      this.focusedPanelId = remaining[0] || "monitor";
    }
    this._saveState();
    this.render();
    this._emitChange("hide");
    return true;
  }

  togglePanelVisibility(panelId) {
    if (this.getOpenPanelIds().has(panelId)) {
      return this.hidePanel(panelId);
    }
    this.showPanel(panelId);
    return true;
  }

  /** Premiere Pro 标志性 ` (反引号) 一键面板全屏最大化 / 还原 */
  toggleMaximize(panelId = null) {
    if (this.maximizedPanelId && (!panelId || panelId === this.maximizedPanelId)) {
      this.maximizedPanelId = null;
      this.render();
      this._emitChange("restore");
      return false;
    }
    const openIds = this.getOpenPanelIds();
    const target =
      (panelId && openIds.has(panelId) && panelId) ||
      (this.hoveredPanelId && openIds.has(this.hoveredPanelId) && this.hoveredPanelId) ||
      (this.focusedPanelId && openIds.has(this.focusedPanelId) && this.focusedPanelId) ||
      Array.from(openIds)[0];

    if (!target) return false;
    this.maximizedPanelId = target;
    this.focusedPanelId = target;
    this.render();
    this._emitChange("maximize");
    return true;
  }

  getStateSnapshot() {
    return {
      preset: this.activePreset,
      focusedPanelId: this.focusedPanelId,
      maximizedPanelId: this.maximizedPanelId,
      openPanels: Array.from(this.getOpenPanelIds()),
      allPanels: this.panelOrder.map((id) => ({
        id,
        title: this.panelMap.get(id)?.title || id,
      })),
    };
  }

  _emitChange(reason) {
    if (typeof this.onChange === "function") {
      this.onChange({ reason, ...this.getStateSnapshot() });
    }
  }

  /** 执行拖拽停靠落子 */
  _applyDrop(dragPanelId, targetInfo) {
    if (!targetInfo || !dragPanelId) return;

    // 1. 根级全宽/全高边缘停靠
    if (targetInfo.kind === "root") {
      const pruned = this._removePanelFromTree(this.tree, dragPanelId);
      if (!pruned) return;
      const leaf = makeTabs([dragPanelId], dragPanelId);
      if (targetInfo.zone === "left") {
        this.tree = makeSplit("h", 0.22, leaf, pruned);
      } else if (targetInfo.zone === "right") {
        this.tree = makeSplit("h", 0.78, pruned, leaf);
      } else if (targetInfo.zone === "top") {
        this.tree = makeSplit("v", 0.34, leaf, pruned);
      } else if (targetInfo.zone === "bottom") {
        this.tree = makeSplit("v", 0.68, pruned, leaf);
      }
      this.focusedPanelId = dragPanelId;
      this._saveState();
      this.render();
      this._emitChange("dock");
      return;
    }

    // 2. 目标 TabsNode 5-Zone 停靠
    const { targetNodeId, zone, insertIndex } = targetInfo;
    const sourceNode = this._findTabsNodeContaining(dragPanelId);
    if (!sourceNode) return;

    // 拖到自身所在单 Tab 格子的任何非重排区 -> 忽略无意义动作
    if (sourceNode.id === targetNodeId && sourceNode.tabs.length === 1) {
      return;
    }

    // 同一 TabsNode 内直接拖拽重排 Tab 顺序
    if (sourceNode.id === targetNodeId && zone === "center") {
      const oldIdx = sourceNode.tabs.indexOf(dragPanelId);
      const nextTabs = sourceNode.tabs.filter((id) => id !== dragPanelId);
      let dest = insertIndex >= 0 ? insertIndex : nextTabs.length;
      if (oldIdx < dest) dest = Math.max(0, dest - 1);
      dest = Math.max(0, Math.min(nextTabs.length, dest));
      nextTabs.splice(dest, 0, dragPanelId);
      sourceNode.tabs = nextTabs;
      sourceNode.active = dragPanelId;
      this.focusedPanelId = dragPanelId;
      this._saveState();
      this.render();
      this._emitChange("reorder");
      return;
    }

    // 跨组迁移或拆出新分屏
    const pruned = this._removePanelFromTree(this.tree, dragPanelId);
    if (!pruned) return;

    // 若目标节点因同组拆出而保留了 id，直接定位插入；否则做稳健兜底
    const targetExists = Boolean(this._findTabsNodeById(targetNodeId, pruned));
    if (!targetExists) {
      this.tree = makeSplit("h", 0.5, pruned, makeTabs([dragPanelId], dragPanelId));
    } else {
      this.tree = this._insertPanelIntoTarget(
        pruned,
        targetNodeId,
        dragPanelId,
        zone,
        insertIndex ?? -1
      );
    }

    this.focusedPanelId = dragPanelId;
    this._saveState();
    this.render();
    this._emitChange("dock");
  }

  /** 渲染整棵二叉分割树（所有面板内容区通过 atomicMove 原子重挂，绝不重置视频与表单状态） */
  render() {
    // 1. 先记录哪些面板内容会在本次渲染中可见
    const visibleActiveIds = new Set();
    if (this.maximizedPanelId && this.panelMap.has(this.maximizedPanelId)) {
      visibleActiveIds.add(this.maximizedPanelId);
    } else {
      const collectActive = (n) => {
        if (!n) return;
        if (n.type === "tabs") visibleActiveIds.add(n.active);
        else if (n.type === "split") {
          collectActive(n.first);
          collectActive(n.second);
        }
      };
      collectActive(this.tree);
    }

    // 2. 把不可见面板先行原子移至离屏保态停泊区，避免重建壳节点时误销毁面板真实 DOM
    for (const [id, p] of this.panelMap.entries()) {
      if (!visibleActiveIds.has(id) && p.el && p.el.parentElement !== this.parkingEl) {
        atomicMove(this.parkingEl, p.el, null);
      }
    }

    // 3. 处理最大化态（PR 反引号模式）
    if (this.maximizedPanelId && this.panelMap.has(this.maximizedPanelId)) {
      const panel = this.panelMap.get(this.maximizedPanelId);
      this._treeHost.classList.add("is-behind-max");
      this._maxOverlay.classList.remove("hidden");

      // 先暂存当前活跃面板到停泊区之外（若当前就在 maxOverlay body 内保持原样）
      const existingBody = this._maxOverlay.querySelector(".dock-pane-content");
      if (existingBody && existingBody.firstElementChild === panel.el) {
        const titleNode = this._maxOverlay.querySelector(".dock-max-title");
        if (titleNode) titleNode.textContent = panel.title;
        return;
      }

      this._maxOverlay.replaceChildren();
      const header = document.createElement("div");
      header.className = "dock-tabbar dock-max-bar focused";

      const badge = document.createElement("div");
      badge.className = "dock-tab active dock-max-tab";
      const titleSpan = document.createElement("span");
      titleSpan.className = "dock-max-title";
      titleSpan.textContent = panel.title;
      badge.appendChild(titleSpan);

      const t = (k, vars) => (window.SolacutI18n ? window.SolacutI18n.t(k, vars) : k);
      const hint = document.createElement("span");
      hint.className = "dock-max-hint mono-num";
      hint.textContent = t("dock.maxHint");

      const actions = document.createElement("div");
      actions.className = "dock-pane-actions";
      const restoreBtn = document.createElement("button");
      restoreBtn.type = "button";
      restoreBtn.className = "dock-icon-btn active";
      restoreBtn.title = t("dock.restoreTitle");
      restoreBtn.setAttribute("aria-label", t("dock.restorePanel"));
      restoreBtn.innerHTML =
        '<svg viewBox="0 0 14 14" width="12" height="12" fill="none" stroke="currentColor" stroke-width="1.5"><path d="M5.5 2v3.5H2M8.5 12V8.5H12M12 5.5H8.5V2M2 8.5h3.5V12"/></svg>';
      restoreBtn.addEventListener("click", () => this.toggleMaximize(this.maximizedPanelId));
      actions.appendChild(restoreBtn);

      header.append(badge, hint, actions);
      header.addEventListener("dblclick", (e) => {
        if (!e.target.closest("button")) this.toggleMaximize(this.maximizedPanelId);
      });

      const body = document.createElement("div");
      body.className = "dock-pane-content";
      body.dataset.panelId = panel.id;

      // 先将外壳挂载进已在 document 中的 _maxOverlay，确保 moveBefore 双方同属已连接文档树
      this._maxOverlay.append(header, body);
      atomicMove(body, panel.el, null);
      return;
    }

    // 4. 正常二叉分割树渲染：
    // 先把新骨架连入 document，再对已连接的源/目标槽位调用原生 moveBefore 原子迁移面板，最后销毁旧壳
    this._treeHost.classList.remove("is-behind-max");
    this._maxOverlay.classList.add("hidden");

    const prevHostChildren = Array.from(this._treeHost.children);
    const pendingMounts = [];
    const newRootDom = this._buildNodeDom(this.tree, pendingMounts);
    if (newRootDom) {
      this._treeHost.appendChild(newRootDom);
    }
    for (const { body, panelEl } of pendingMounts) {
      atomicMove(body, panelEl, null);
    }
    for (const oldChild of prevHostChildren) {
      oldChild.remove();
    }
    this._maxOverlay.replaceChildren();
  }

  _buildNodeDom(node, pendingMounts) {
    if (!node) return null;

    if (node.type === "split") {
      const container = document.createElement("div");
      container.className = `dock-split dock-split-${node.dir}`;
      container.dataset.splitId = node.id;

      const firstSlot = document.createElement("div");
      firstSlot.className = "dock-slot";
      firstSlot.style.flex = `0 0 calc(${(node.ratio * 100).toFixed(3)}% - 2px)`;

      const gutter = document.createElement("div");
      gutter.className = `dock-gutter dock-gutter-${node.dir}`;
      gutter.setAttribute("role", "separator");
      gutter.setAttribute("aria-orientation", node.dir === "h" ? "vertical" : "horizontal");
      gutter.tabIndex = 0;

      const secondSlot = document.createElement("div");
      secondSlot.className = "dock-slot";
      secondSlot.style.flex = "1 1 0%";

      const firstDom = this._buildNodeDom(node.first, pendingMounts);
      const secondDom = this._buildNodeDom(node.second, pendingMounts);
      if (firstDom) firstSlot.appendChild(firstDom);
      if (secondDom) secondSlot.appendChild(secondDom);

      this._bindGutterDrag(gutter, container, firstSlot, node);

      container.append(firstSlot, gutter, secondSlot);
      return container;
    }

    if (node.type === "tabs") {
      const openCount = this.getOpenPanelIds().size;
      const isFocused = node.tabs.includes(this.focusedPanelId) && node.active === this.focusedPanelId;
      const group = document.createElement("section");
      group.className = `dock-tabs-group${isFocused ? " is-focused" : ""}`;
      group.dataset.nodeId = node.id;
      group.dataset.activePanel = node.active;

      group.addEventListener("pointerenter", () => {
        this.hoveredPanelId = node.active;
      });
      group.addEventListener("pointerdown", () => {
        if (this.focusedPanelId !== node.active) {
          this.focusedPanelId = node.active;
          this._syncFocusVisuals();
          this._emitChange("focus");
        }
      });

      const tabbar = document.createElement("div");
      tabbar.className = "dock-tabbar";
      tabbar.setAttribute("role", "tablist");
      tabbar.dataset.nodeId = node.id;

      const tabList = document.createElement("div");
      tabList.className = "dock-tab-list";

      node.tabs.forEach((panelId) => {
        const panel = this.panelMap.get(panelId);
        if (!panel) return;
        const isActive = node.active === panelId;
        const tabBtn = document.createElement("button");
        tabBtn.type = "button";
        tabBtn.className = `dock-tab${isActive ? " active" : ""}`;
        tabBtn.setAttribute("role", "tab");
        tabBtn.setAttribute("aria-selected", isActive ? "true" : "false");
        tabBtn.dataset.panelId = panelId;
        tabBtn.dataset.nodeId = node.id;
        const t = (k, vars) => (window.SolacutI18n ? window.SolacutI18n.t(k, vars) : k);
        tabBtn.title = t("dock.tabTooltip", { title: panel.title });

        const label = document.createElement("span");
        label.className = "dock-tab-label";
        label.textContent = panel.title;
        tabBtn.appendChild(label);

        tabBtn.addEventListener("click", () => {
          if (this._drag?.didDrag) return;
          if (node.active !== panelId || this.focusedPanelId !== panelId) {
            node.active = panelId;
            this.focusedPanelId = panelId;
            this._saveState();
            this.render();
            this._emitChange("tab");
          }
        });

        tabBtn.addEventListener("pointerdown", (e) => {
          if (e.button !== 0) return;
          this._startTabDragCandidate(e, panelId, node.id);
        });

        tabBtn.addEventListener("contextmenu", (e) => {
          if (typeof this.onTabContextMenu === "function") {
            e.preventDefault();
            e.stopPropagation();
            this.onTabContextMenu(e, panelId, node.id);
          }
        });

        tabList.appendChild(tabBtn);
      });

      const tDock = (k, vars) => (window.SolacutI18n ? window.SolacutI18n.t(k, vars) : k);
      const actions = document.createElement("div");
      actions.className = "dock-pane-actions";

      const maxBtn = document.createElement("button");
      maxBtn.type = "button";
      maxBtn.className = "dock-icon-btn";
      maxBtn.dataset.dockAction = "max";
      maxBtn.title = tDock("dock.maximizeTitle");
      maxBtn.setAttribute("aria-label", tDock("dock.maximizePanel"));
      maxBtn.innerHTML =
        '<svg viewBox="0 0 14 14" width="12" height="12" fill="none" stroke="currentColor" stroke-width="1.5"><path d="M2 5.5V2h3.5M8.5 2H12v3.5M12 8.5V12H8.5M5.5 12H2V8.5"/></svg>';
      maxBtn.addEventListener("click", (e) => {
        e.stopPropagation();
        this.toggleMaximize(node.active);
      });
      actions.appendChild(maxBtn);

      if (openCount > 1) {
        const closeBtn = document.createElement("button");
        closeBtn.type = "button";
        closeBtn.className = "dock-icon-btn";
        closeBtn.dataset.dockAction = "hide";
        closeBtn.title = tDock("dock.hideTitle");
        closeBtn.setAttribute("aria-label", tDock("dock.hidePanel"));
        closeBtn.innerHTML =
          '<svg viewBox="0 0 14 14" width="11" height="11" fill="none" stroke="currentColor" stroke-width="1.6"><path d="M3 3l8 8M11 3l-8 8"/></svg>';
        closeBtn.addEventListener("click", (e) => {
          e.stopPropagation();
          this.hidePanel(node.active);
        });
        actions.appendChild(closeBtn);
      }

      tabbar.append(tabList, actions);
      tabbar.addEventListener("dblclick", (e) => {
        if (!e.target.closest("button")) {
          this.toggleMaximize(node.active);
        }
      });

      const body = document.createElement("div");
      body.className = "dock-pane-content";
      body.dataset.panelId = node.active;

      const activePanel = this.panelMap.get(node.active);
      if (activePanel?.el) {
        pendingMounts.push({ body, panelEl: activePanel.el });
      }

      group.append(tabbar, body);
      return group;
    }

    return null;
  }

  _syncFocusVisuals() {
    this._treeHost.querySelectorAll(".dock-tabs-group").forEach((g) => {
      g.classList.toggle("is-focused", g.dataset.activePanel === this.focusedPanelId);
    });
  }

  _bindGutterDrag(gutter, container, firstSlot, splitNode) {
    gutter.addEventListener("pointerdown", (e) => {
      if (e.button !== 0) return;
      e.preventDefault();
      gutter.setPointerCapture(e.pointerId);
      gutter.classList.add("dragging");
      document.body.classList.add("is-resizing");

      const rect = container.getBoundingClientRect();
      const total = splitNode.dir === "h" ? rect.width : rect.height;
      const startPos = splitNode.dir === "h" ? e.clientX : e.clientY;
      const startRatio = splitNode.ratio;
      const minPx = splitNode.dir === "h" ? 170 : 120;
      const minRatio = Math.max(MIN_SPLIT_RATIO, Math.min(0.45, minPx / Math.max(total, 1)));
      const maxRatio = 1 - minRatio;

      const onMove = (ev) => {
        const curPos = splitNode.dir === "h" ? ev.clientX : ev.clientY;
        const delta = (curPos - startPos) / Math.max(total, 1);
        const next = Math.max(minRatio, Math.min(maxRatio, startRatio + delta));
        splitNode.ratio = Number(next.toFixed(4));
        firstSlot.style.flex = `0 0 calc(${(splitNode.ratio * 100).toFixed(3)}% - 2px)`;
      };

      const onUp = () => {
        gutter.classList.remove("dragging");
        document.body.classList.remove("is-resizing");
        gutter.removeEventListener("pointermove", onMove);
        gutter.removeEventListener("pointerup", onUp);
        gutter.removeEventListener("pointercancel", onUp);
        this._saveState();
      };

      gutter.addEventListener("pointermove", onMove);
      gutter.addEventListener("pointerup", onUp);
      gutter.addEventListener("pointercancel", onUp);
    });

    gutter.addEventListener("dblclick", () => {
      splitNode.ratio = 0.5;
      firstSlot.style.flex = "0 0 calc(50% - 2px)";
      this._saveState();
    });

    gutter.addEventListener("keydown", (e) => {
      const step = 0.03;
      let delta = 0;
      if (splitNode.dir === "h" && e.key === "ArrowLeft") delta = -step;
      else if (splitNode.dir === "h" && e.key === "ArrowRight") delta = step;
      else if (splitNode.dir === "v" && e.key === "ArrowUp") delta = -step;
      else if (splitNode.dir === "v" && e.key === "ArrowDown") delta = step;
      if (!delta) return;
      e.preventDefault();
      e.stopPropagation();
      splitNode.ratio = Math.max(MIN_SPLIT_RATIO, Math.min(MAX_SPLIT_RATIO, splitNode.ratio + delta));
      firstSlot.style.flex = `0 0 calc(${(splitNode.ratio * 100).toFixed(3)}% - 2px)`;
      this._saveState();
    });
  }

  _startTabDragCandidate(e, panelId, sourceNodeId) {
    const panel = this.panelMap.get(panelId);
    if (!panel) return;
    const startX = e.clientX;
    const startY = e.clientY;

    this._drag = {
      panelId,
      sourceNodeId,
      startX,
      startY,
      didDrag: false,
      target: null,
    };

    const onMove = (ev) => {
      if (!this._drag) return;
      const dx = ev.clientX - startX;
      const dy = ev.clientY - startY;
      if (!this._drag.didDrag) {
        if (Math.hypot(dx, dy) < DRAG_THRESHOLD_PX) return;
        this._drag.didDrag = true;
        document.body.classList.add("is-dock-dragging", "is-resizing");
        this._dragGhost.textContent = panel.title;
        this._dragGhost.classList.remove("hidden");
      }

      this._dragGhost.style.transform = `translate3d(${ev.clientX + 12}px, ${ev.clientY + 12}px, 0)`;
      const hit = this._hitTestDropZone(ev.clientX, ev.clientY, panelId, sourceNodeId);
      this._drag.target = hit;
      this._renderDropOverlay(hit);
    };

    const onUp = () => {
      window.removeEventListener("pointermove", onMove);
      window.removeEventListener("pointerup", onUp);
      window.removeEventListener("pointercancel", onUp);

      const d = this._drag;
      this._drag = null;
      document.body.classList.remove("is-dock-dragging", "is-resizing");
      this._dragGhost.classList.add("hidden");
      this._dropOverlay.classList.add("hidden");

      if (d?.didDrag && d.target) {
        this._applyDrop(d.panelId, d.target);
      }
    };

    window.addEventListener("pointermove", onMove);
    window.addEventListener("pointerup", onUp);
    window.addEventListener("pointercancel", onUp);
  }

  /** PR 风格 5-Zone + 外缘全边停靠命中计算 */
  _hitTestDropZone(clientX, clientY, dragPanelId, sourceNodeId) {
    const rootRect = this.rootEl.getBoundingClientRect();
    if (
      clientX < rootRect.left ||
      clientX > rootRect.right ||
      clientY < rootRect.top ||
      clientY > rootRect.bottom
    ) {
      return null;
    }

    // 1. 优先检查整个工作区最外缘 20px（拖到最左/最右/最顶/最底形成全高/全宽边栏）
    const openCount = this.getOpenPanelIds().size;
    if (openCount > 1) {
      if (clientX - rootRect.left <= ROOT_EDGE_ZONE_PX) {
        return {
          kind: "root",
          zone: "left",
          rect: { left: 0, top: 0, width: Math.round(rootRect.width * 0.22), height: rootRect.height },
        };
      }
      if (rootRect.right - clientX <= ROOT_EDGE_ZONE_PX) {
        const w = Math.round(rootRect.width * 0.22);
        return {
          kind: "root",
          zone: "right",
          rect: { left: rootRect.width - w, top: 0, width: w, height: rootRect.height },
        };
      }
      if (clientY - rootRect.top <= ROOT_EDGE_ZONE_PX) {
        return {
          kind: "root",
          zone: "top",
          rect: { left: 0, top: 0, width: rootRect.width, height: Math.round(rootRect.height * 0.32) },
        };
      }
      if (rootRect.bottom - clientY <= ROOT_EDGE_ZONE_PX) {
        const h = Math.round(rootRect.height * 0.32);
        return {
          kind: "root",
          zone: "bottom",
          rect: { left: 0, top: rootRect.height - h, width: rootRect.width, height: h },
        };
      }
    }

    // 2. 命中当前指针下的 TabsNode
    const groups = this._treeHost.querySelectorAll(".dock-tabs-group");
    let hitGroup = null;
    let gRect = null;
    for (const g of groups) {
      const r = g.getBoundingClientRect();
      if (clientX >= r.left && clientX <= r.right && clientY >= r.top && clientY <= r.bottom) {
        hitGroup = g;
        gRect = r;
        break;
      }
    }
    if (!hitGroup || !gRect) return null;

    const targetNodeId = hitGroup.dataset.nodeId;
    const targetNode = this._findTabsNodeById(targetNodeId);
    if (!targetNode) return null;

    // 只有一个 Tab 且拖向自己格子时不显示停靠落点
    if (targetNodeId === sourceNodeId && targetNode.tabs.length <= 1) {
      return null;
    }

    const relLeft = gRect.left - rootRect.left;
    const relTop = gRect.top - rootRect.top;

    // 若落在 TabBar 内，直接视为合并 Tab / 重排 Tab
    const tabbar = hitGroup.querySelector(".dock-tabbar");
    const tbRect = tabbar ? tabbar.getBoundingClientRect() : null;
    if (tbRect && clientY <= tbRect.bottom + 4) {
      const tabEls = Array.from(tabbar.querySelectorAll(".dock-tab"));
      let insertIndex = tabEls.length;
      for (let i = 0; i < tabEls.length; i += 1) {
        const tr = tabEls[i].getBoundingClientRect();
        if (clientX < tr.left + tr.width * 0.5) {
          insertIndex = i;
          break;
        }
      }
      return {
        kind: "node",
        targetNodeId,
        zone: "center",
        insertIndex,
        rect: { left: relLeft, top: relTop, width: gRect.width, height: gRect.height },
      };
    }

    // 在内容区域内按 PR 5-Zone 归一化判定
    const nx = (clientX - gRect.left) / Math.max(gRect.width, 1);
    const ny = (clientY - gRect.top) / Math.max(gRect.height, 1);

    if (nx >= 0.24 && nx <= 0.76 && ny >= 0.24 && ny <= 0.76) {
      return {
        kind: "node",
        targetNodeId,
        zone: "center",
        insertIndex: targetNode.tabs.length,
        rect: { left: relLeft, top: relTop, width: gRect.width, height: gRect.height },
      };
    }

    const dists = [
      { zone: "left", d: nx },
      { zone: "right", d: 1 - nx },
      { zone: "top", d: ny },
      { zone: "bottom", d: 1 - ny },
    ];
    dists.sort((a, b) => a.d - b.d);
    const best = dists[0].zone;

    let overlayRect;
    if (best === "left") {
      overlayRect = { left: relLeft, top: relTop, width: gRect.width * 0.5, height: gRect.height };
    } else if (best === "right") {
      overlayRect = {
        left: relLeft + gRect.width * 0.5,
        top: relTop,
        width: gRect.width * 0.5,
        height: gRect.height,
      };
    } else if (best === "top") {
      overlayRect = { left: relLeft, top: relTop, width: gRect.width, height: gRect.height * 0.5 };
    } else {
      overlayRect = {
        left: relLeft,
        top: relTop + gRect.height * 0.5,
        width: gRect.width,
        height: gRect.height * 0.5,
      };
    }

    return {
      kind: "node",
      targetNodeId,
      zone: best,
      insertIndex: -1,
      rect: overlayRect,
    };
  }

  _renderDropOverlay(hit) {
    if (!hit || !hit.rect) {
      this._dropOverlay.classList.add("hidden");
      return;
    }
    this._dropOverlay.classList.remove("hidden");
    this._dropOverlay.dataset.zone = hit.zone;
    this._dropOverlay.style.left = `${Math.round(hit.rect.left)}px`;
    this._dropOverlay.style.top = `${Math.round(hit.rect.top)}px`;
    this._dropOverlay.style.width = `${Math.max(12, Math.round(hit.rect.width))}px`;
    this._dropOverlay.style.height = `${Math.max(12, Math.round(hit.rect.height))}px`;
  }

  /** 原地更新面板标题与提示文案，绝不重建 DOM 破坏视频播放状态 */
  updatePanelTitles(titleMap) {
    if (!titleMap || typeof titleMap !== "object") return;
    const t = (k, vars) => (window.SolacutI18n ? window.SolacutI18n.t(k, vars) : k);
    for (const [id, title] of Object.entries(titleMap)) {
      const panel = this.panelMap.get(id);
      if (panel && title) panel.title = title;
    }
    this.rootEl?.querySelectorAll(".dock-tab[data-panel-id]").forEach((tabBtn) => {
      const pid = tabBtn.dataset.panelId;
      const panel = this.panelMap.get(pid);
      if (!panel) return;
      const lbl = tabBtn.querySelector(".dock-tab-label");
      if (lbl) lbl.textContent = panel.title;
      tabBtn.title = t("dock.tabTooltip", { title: panel.title });
    });
    this.rootEl?.querySelectorAll('.dock-icon-btn[data-dock-action="max"]').forEach((btn) => {
      btn.title = t("dock.maximizeTitle");
      btn.setAttribute("aria-label", t("dock.maximizePanel"));
    });
    this.rootEl?.querySelectorAll('.dock-icon-btn[data-dock-action="hide"]').forEach((btn) => {
      btn.title = t("dock.hideTitle");
      btn.setAttribute("aria-label", t("dock.hidePanel"));
    });
    if (this.maximizedPanelId && this._maxOverlay) {
      const panel = this.panelMap.get(this.maximizedPanelId);
      const titleNode = this._maxOverlay.querySelector(".dock-max-title");
      if (titleNode && panel) titleNode.textContent = panel.title;
      const hintNode = this._maxOverlay.querySelector(".dock-max-hint");
      if (hintNode) hintNode.textContent = t("dock.maxHint");
      const restoreBtn = this._maxOverlay.querySelector(".dock-pane-actions button");
      if (restoreBtn) {
        restoreBtn.title = t("dock.restoreTitle");
        restoreBtn.setAttribute("aria-label", t("dock.restorePanel"));
      }
    }
  }

  _bindGlobalEvents() {
    document.addEventListener("keydown", (e) => {
      if (e.ctrlKey || e.altKey || e.metaKey) return;
      const activeEl = document.activeElement;
      const tag = activeEl ? activeEl.tagName.toLowerCase() : "";
      const inputType = tag === "input" ? (activeEl.type || "text").toLowerCase() : "";
      const isTextInput =
        (tag === "input" && !["checkbox", "radio", "range", "button", "submit", "reset"].includes(inputType)) ||
        tag === "textarea" ||
        tag === "select" ||
        Boolean(activeEl && activeEl.isContentEditable);

      if (document.querySelector("dialog[open]")) return;
      if (document.querySelector(":popover-open, .popover-fallback-open")) return;

      // Premiere Pro 标志性 ` (反引号) 悬停面板一键全屏放大 / 还原
      if (e.code === "Backquote" && !isTextInput) {
        e.preventDefault();
        this.toggleMaximize();
        return;
      }

      // 最大化状态下按 Esc 快速还原（影院全屏优先由 app.js 接管退出）
      if (
        e.key === "Escape" &&
        this.maximizedPanelId &&
        !isTextInput &&
        !document.body.classList.contains("cinema-viewer-active")
      ) {
        e.preventDefault();
        this.toggleMaximize(this.maximizedPanelId);
      }
    });
  }
}
