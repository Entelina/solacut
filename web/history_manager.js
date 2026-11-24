/* ClipStudio NLE 操作历史栈管理器（对标 Premiere Pro History / DaVinci Undo History）
 * 特性：
 * 1. 支持任意步数 Undo / Redo 与点击列表直跳任意历史版本（jumpTo）；
 * 2. 支持同目标字段防抖合并（mergeKey），连续打字修改标题/字幕不会刷爆历史记录；
 * 3. 深拷贝隔离快照，保证撤回与重做 100% 确定性无引用污染。
 */

function cloneData(data) {
  if (typeof structuredClone === "function") {
    return structuredClone(data);
  }
  return JSON.parse(JSON.stringify(data));
}

function nowClockStr() {
  const d = new Date();
  const p = (n) => String(n).padStart(2, "0");
  return `${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`;
}

export class HistoryManager {
  constructor({ maxEntries = 100, onChange = null } = {}) {
    this.maxEntries = Math.max(2, Number(maxEntries) || 100);
    this.onChange = onChange;
    this.entries = [];
    this.cursor = -1;
    this._seq = 0;
  }

  /** 重置历史栈并建立初始基线版本（v0） */
  reset(snapshot, label = null) {
    const resolvedLabel = label || (window.SolacutI18n ? window.SolacutI18n.t("hist.initialState") : "Initial state");
    this._seq = 1;
    this.entries = [
      {
        id: this._seq,
        label: resolvedLabel,
        category: "init",
        timeStr: nowClockStr(),
        timestamp: Date.now(),
        mergeKey: null,
        snapshot: cloneData(snapshot),
      },
    ];
    this.cursor = 0;
    this._notify("reset");
  }

  clear() {
    this.entries = [];
    this.cursor = -1;
    this._notify("clear");
  }

  /**
   * 记录一条新的可撤销历史快照
   * @param {Object} snapshot 当前完整状态快照
   * @param {string} label 简短清晰的操作说明（如 "修改 #1 入点 19:05.8"）
   * @param {Object} [opts]
   * @param {string} [opts.category='edit'] 'init'|'trim'|'select'|'title'|'sub'|'batch'
   * @param {string|null} [opts.mergeKey=null] 短时间内连续编辑同一字段时自动合并为一条记录
   * @param {number} [opts.mergeWindowMs=900]
   */
  commit(snapshot, label, { category = "edit", mergeKey = null, mergeWindowMs = 900 } = {}) {
    const now = Date.now();
    const cur = this.entries[this.cursor];

    // 若处于栈顶且在防抖窗口内连续编辑同一目标字段，原地合并更新快照
    if (
      mergeKey &&
      cur &&
      this.cursor === this.entries.length - 1 &&
      cur.mergeKey === mergeKey &&
      now - cur.timestamp <= mergeWindowMs
    ) {
      cur.label = label;
      cur.timeStr = nowClockStr();
      cur.timestamp = now;
      cur.snapshot = cloneData(snapshot);
      this._notify("merge");
      return cur;
    }

    // 若当前不在栈顶（撤销过），新操作截断旧的“未来”重做分支
    if (this.cursor < this.entries.length - 1) {
      this.entries = this.entries.slice(0, this.cursor + 1);
    }

    this._seq += 1;
    const entry = {
      id: this._seq,
      label,
      category,
      timeStr: nowClockStr(),
      timestamp: now,
      mergeKey,
      snapshot: cloneData(snapshot),
    };
    this.entries.push(entry);

    // 超出容量时保留第 0 条初始基线，裁剪最早的中间历史
    if (this.entries.length > this.maxEntries) {
      this.entries.splice(1, this.entries.length - this.maxEntries);
    }
    this.cursor = this.entries.length - 1;
    this._notify("commit");
    return entry;
  }

  canUndo() {
    return this.cursor > 0;
  }

  canRedo() {
    return this.cursor >= 0 && this.cursor < this.entries.length - 1;
  }

  undo() {
    if (!this.canUndo()) return null;
    this.cursor -= 1;
    const entry = this.entries[this.cursor];
    this._notify("undo");
    return { entry, snapshot: cloneData(entry.snapshot) };
  }

  redo() {
    if (!this.canRedo()) return null;
    this.cursor += 1;
    const entry = this.entries[this.cursor];
    this._notify("redo");
    return { entry, snapshot: cloneData(entry.snapshot) };
  }

  /** 直接跳转到任意历史版本索引（支持一步退回任意历史节点或前进至未来节点） */
  jumpTo(index) {
    const targetIdx = Number(index);
    if (
      !Number.isInteger(targetIdx) ||
      targetIdx < 0 ||
      targetIdx >= this.entries.length ||
      targetIdx === this.cursor
    ) {
      return null;
    }
    this.cursor = targetIdx;
    const entry = this.entries[this.cursor];
    this._notify("jump");
    return { entry, snapshot: cloneData(entry.snapshot) };
  }

  getState() {
    return {
      entries: this.entries.map((e, idx) => ({
        index: idx,
        id: e.id,
        label: e.label,
        category: e.category,
        timeStr: e.timeStr,
        isCurrent: idx === this.cursor,
        isFuture: idx > this.cursor,
      })),
      cursor: this.cursor,
      total: this.entries.length,
      canUndo: this.canUndo(),
      canRedo: this.canRedo(),
      currentLabel: this.entries[this.cursor]?.label || "",
    };
  }

  _notify(reason) {
    if (typeof this.onChange === "function") {
      this.onChange({ reason, ...this.getState() });
    }
  }
}
