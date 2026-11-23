/* ClipStudio NLE 双层多轨 Canvas 60fps 时间线引擎（融合 DaVinci Cut Page + Premiere Pro）
 * 零外部依赖，支持：
 * 1. 上层 24px 全片总览导航条（整场音频能量热力轮廓 + 全量高光色带 + 可拖/可拉边视口滑框）；
 * 2. 下层精细多轨 Canvas：
 *    - Timecode Ruler 自适应刻度标尺 + 全轨达芬奇信号红播放头 Scrubbing；
 *    - V1 高光切片轨（点击选中、双击预览、左右边缘直接拖拽 Trim 入/出点 + 实时画面跟随 + 磁吸吸附）；
 *    - A1 实时缩放高精音频波形轨（零闪烁总览占位 + 停止缩放 120ms 防抖自动向 /api/waveform 拉取局部 PCM 采样 + Step 1 能量阈值虚线）；
 *    - T1 双语字幕轨（点击跳播、左右边缘拖拽微调句长、B/C 剃刀工具精确时间点一刀断句）；
 * 3. 轨道头控制（V1/T1 锁定与显隐、A1 静音双向同步）、Alt/Ctrl+滚轮鼠标锚点缩放、滚轮水平平移、Shift+Z 适配全片。
 */

import { formatTimecode } from "/static/nle_controls.js";

const RULER_H = 22;
const TRACK_HEIGHT_PRESETS = {
  compact: { v1: 34, a1: 38, t1: 30 },
  normal: { v1: 44, a1: 54, t1: 38 },
  tall: { v1: 52, a1: 78, t1: 42 },
};
const TRIM_HANDLE_PX = 7;
const SNAP_THRESHOLD_PX = 8;

function clamp(v, min, max) {
  return Math.max(min, Math.min(max, v));
}

function chooseRulerStep(spanSec, widthPx) {
  const minPxPerMajor = 78;
  const maxMajors = Math.max(2, Math.floor(widthPx / minPxPerMajor));
  const rawStep = spanSec / maxMajors;
  const steps = [0.5, 1, 2, 5, 10, 15, 30, 60, 120, 300, 600, 900, 1800, 3600];
  for (const s of steps) {
    if (s >= rawStep) return s;
  }
  return 3600;
}

function formatRulerClock(sec) {
  const safe = Math.max(0, Math.round(sec));
  const hh = Math.floor(safe / 3600);
  const mm = Math.floor((safe % 3600) / 60);
  const ss = safe % 60;
  const p = (n) => String(n).padStart(2, "0");
  return hh > 0 ? `${p(hh)}:${p(mm)}:${p(ss)}` : `${p(mm)}:${p(ss)}`;
}

export class DualTimelineEngine {
  constructor({
    overviewCanvas,
    tracksCanvas,
    zoomSlider,
    viewSpanLabel,
    onSeek,
    onSelectClip,
    onPreviewClip,
    onTrimClipLive,
    onTrimClipCommit,
    onSelectSubtitle,
    onTrimSubtitleLive,
    onTrimSubtitleCommit,
    onRazorSubtitle,
    onClipContextMenu,
    onSubtitleContextMenu,
    onRequestWaveform,
    onToolChange,
    onSnapChange,
  }) {
    this.overviewCanvas = overviewCanvas;
    this.tracksCanvas = tracksCanvas;
    this.overviewCtx = overviewCanvas ? overviewCanvas.getContext("2d") : null;
    this.tracksCtx = tracksCanvas ? tracksCanvas.getContext("2d") : null;
    this.zoomSlider = zoomSlider;
    this.viewSpanLabel = viewSpanLabel;

    this.onSeek = onSeek;
    this.onSelectClip = onSelectClip;
    this.onPreviewClip = onPreviewClip;
    this.onTrimClipLive = onTrimClipLive;
    this.onTrimClipCommit = onTrimClipCommit;
    this.onSelectSubtitle = onSelectSubtitle;
    this.onTrimSubtitleLive = onTrimSubtitleLive;
    this.onTrimSubtitleCommit = onTrimSubtitleCommit;
    this.onRazorSubtitle = onRazorSubtitle;
    this.onClipContextMenu = onClipContextMenu;
    this.onSubtitleContextMenu = onSubtitleContextMenu;
    this.onRequestWaveform = onRequestWaveform;
    this.onToolChange = onToolChange;
    this.onSnapChange = onSnapChange;

    // 媒体与数据状态
    this.videoStem = "";
    this.duration = 0;
    this.fps = 30;
    this.currentTime = 0;
    this.highlights = [];
    this.activeClipRank = null;
    this.activeSubKey = "";
    this.peakSegments = [];

    // 波形缓存（全片总览 + 局部高清）
    this.overviewWave = null; // { start_sec, end_sec, rms, peak, rms_threshold }
    this.detailWave = null; // { start_sec, end_sec, rms, peak, rms_threshold }
    this._waveFetchTimer = null;
    this._waveFetchSeq = 0;

    // 视口与工具状态
    this.viewStart = 0;
    this.viewEnd = 120;
    this.tool = "select"; // 'select' | 'razor'
    this.snapEnabled = true;
    this.audioMuted = false;
    this.tracksState = {
      v1Visible: true,
      v1Locked: false,
      a1Visible: true,
      t1Visible: true,
      t1Locked: false,
    };
    this.viewOptions = {
      fullWaveform: false, // false=对称波形, true=全高正向波形（达芬奇 Display Full Waveform）
      showClipNames: true,
      heightPreset: "normal", // 'compact' | 'normal' | 'tall'
    };

    // 交互态
    this._ovDrag = null; // { mode: 'move'|'left'|'right', startX, origStart, origEnd }
    this._trkDrag = null;
    this._hoverInfo = null;
    this._snapGuideSec = null;
    this._drawPending = false;

    this._bindOverviewEvents();
    this._bindTracksEvents();
    this._bindZoomSlider();

    this._ro = new ResizeObserver(() => this.requestDraw());
    if (this.overviewCanvas) this._ro.observe(this.overviewCanvas);
    if (this.tracksCanvas) this._ro.observe(this.tracksCanvas);

    this.requestDraw();
  }

  /* ==================== 外部数据同步接口 ==================== */
  setMedia({ stem = "", duration = 0, fps = 30, resetView = false }) {
    const changedStem = stem !== this.videoStem;
    this.videoStem = stem || "";
    this.duration = Math.max(0, Number(duration) || 0);
    this.fps = Math.max(1, Number(fps) || 30);

    if (changedStem) {
      this.overviewWave = null;
      this.detailWave = null;
      this.peakSegments = [];
      this._waveFetchSeq += 1;
      if (this._waveFetchTimer) {
        clearTimeout(this._waveFetchTimer);
        this._waveFetchTimer = null;
      }
    }

    if (changedStem || resetView || this.viewEnd <= this.viewStart) {
      this.viewStart = 0;
      this.viewEnd = Math.max(10, this.duration || 120);
    } else if (this.duration > 0 && this.viewEnd > this.duration) {
      const span = Math.min(this.duration, this.viewEnd - this.viewStart);
      this.viewEnd = this.duration;
      this.viewStart = Math.max(0, this.viewEnd - span);
    }

    this._syncZoomControls();
    this.requestDraw();
  }

  setHighlights(highlights, activeClipRank = null) {
    this.highlights = Array.isArray(highlights) ? highlights : [];
    this.activeClipRank = activeClipRank != null ? Number(activeClipRank) : null;
    this.requestDraw();
  }

  setActiveClip(rank) {
    this.activeClipRank = rank != null ? Number(rank) : null;
    this.requestDraw();
  }

  setActiveSubKey(subKey) {
    if (this.activeSubKey !== (subKey || "")) {
      this.activeSubKey = subKey || "";
      this.requestDraw();
    }
  }

  setPlayhead(sec) {
    const next = Math.max(0, Number(sec) || 0);
    if (Math.abs(next - this.currentTime) < 0.002) return;
    this.currentTime = next;
    this.requestDraw();
  }

  setPeakSegments(peaks) {
    this.peakSegments = Array.isArray(peaks) ? peaks : [];
    this.requestDraw();
  }

  setOverviewWaveform(waveData) {
    if (!waveData || !Array.isArray(waveData.rms)) {
      this.overviewWave = null;
    } else {
      this.overviewWave = waveData;
      if (this.duration <= 0 && Number(waveData.total_duration_sec) > 0) {
        this.duration = Number(waveData.total_duration_sec);
        this.viewStart = 0;
        this.viewEnd = this.duration;
        this._syncZoomControls();
      }
    }
    this.requestDraw();
  }

  setTool(tool) {
    const next = tool === "razor" ? "razor" : "select";
    if (this.tool === next) return;
    this.tool = next;
    this._updateCursor();
    this.onToolChange?.(next);
    this.requestDraw();
  }

  setSnap(enabled) {
    this.snapEnabled = Boolean(enabled);
    this.onSnapChange?.(this.snapEnabled);
    this.requestDraw();
  }

  setAudioMuted(muted) {
    this.audioMuted = Boolean(muted);
    this.requestDraw();
  }

  setTrackState(patch) {
    Object.assign(this.tracksState, patch || {});
    this._syncHeaderHeights();
    this.requestDraw();
  }

  setViewOptions(patch) {
    Object.assign(this.viewOptions, patch || {});
    this._syncHeaderHeights();
    this.requestDraw();
  }

  _syncHeaderHeights() {
    const preset = TRACK_HEIGHT_PRESETS[this.viewOptions.heightPreset] || TRACK_HEIGHT_PRESETS.normal;
    const pane = this.tracksCanvas?.closest(".pane-timeline");
    if (!pane) return;
    pane.style.setProperty("--tl-v1-h", this.tracksState.v1Visible ? `${preset.v1}px` : "0px");
    pane.style.setProperty("--tl-a1-h", this.tracksState.a1Visible ? `${preset.a1}px` : "0px");
    pane.style.setProperty("--tl-t1-h", this.tracksState.t1Visible ? `${preset.t1}px` : "0px");
    pane.querySelector(".tl-head-v1")?.classList.toggle("is-collapsed", !this.tracksState.v1Visible);
    pane.querySelector(".tl-head-a1")?.classList.toggle("is-collapsed", !this.tracksState.a1Visible);
    pane.querySelector(".tl-head-t1")?.classList.toggle("is-collapsed", !this.tracksState.t1Visible);
  }

  fitAll() {
    const total = Math.max(10, this.duration || 120);
    this.viewStart = 0;
    this.viewEnd = total;
    this.detailWave = null;
    this._syncZoomControls();
    this.requestDraw();
  }

  fitClip(clip) {
    if (!clip || !(Number(clip.end_sec) > Number(clip.start_sec))) return;
    const total = Math.max(Number(clip.end_sec) + 5, this.duration || 0);
    const s = Number(clip.start_sec);
    const e = Number(clip.end_sec);
    const pad = Math.max(4, (e - s) * 0.18);
    this.viewStart = Math.max(0, s - pad);
    this.viewEnd = Math.min(total, e + pad);
    if (this.viewEnd - this.viewStart < 6) {
      const mid = (s + e) / 2;
      this.viewStart = Math.max(0, mid - 3);
      this.viewEnd = Math.min(total, this.viewStart + 6);
    }
    this._syncZoomControls();
    this._scheduleDetailWaveformFetch();
    this.requestDraw();
  }

  zoomByFactor(factor, anchorSec = null) {
    const total = Math.max(10, this.duration || 120);
    const curSpan = Math.max(1, this.viewEnd - this.viewStart);
    const minSpan = Math.min(total, 6);
    const nextSpan = clamp(curSpan * factor, minSpan, total);
    const anchor =
      anchorSec !== null
        ? clamp(anchorSec, this.viewStart, this.viewEnd)
        : clamp(this.currentTime, this.viewStart, this.viewEnd);
    const ratio = (anchor - this.viewStart) / curSpan;
    let nextStart = anchor - ratio * nextSpan;
    let nextEnd = nextStart + nextSpan;
    if (nextStart < 0) {
      nextStart = 0;
      nextEnd = nextSpan;
    }
    if (nextEnd > total) {
      nextEnd = total;
      nextStart = Math.max(0, total - nextSpan);
    }
    this.viewStart = nextStart;
    this.viewEnd = nextEnd;
    this._syncZoomControls();
    this._scheduleDetailWaveformFetch();
    this.requestDraw();
  }

  panBySeconds(deltaSec) {
    const total = Math.max(10, this.duration || 120);
    const span = this.viewEnd - this.viewStart;
    if (span >= total - 0.05) return;
    let nextStart = clamp(this.viewStart + deltaSec, 0, total - span);
    this.viewStart = nextStart;
    this.viewEnd = nextStart + span;
    this._scheduleDetailWaveformFetch();
    this.requestDraw();
  }

  /* ==================== 缩放滑块与按需高精波形拉取 ==================== */
  _bindZoomSlider() {
    const sl = this.zoomSlider;
    if (!sl) return;
    sl.addEventListener("input", () => {
      const total = Math.max(10, this.duration || 120);
      const minSpan = Math.min(total, 6);
      const t = clamp(Number(sl.value) / 100, 0, 1); // 0=全片, 1=最放大
      // 对数映射：长视频从 4 小时平滑过渡到 6 秒细节，手感类似达芬奇缩放滑块
      const span = total * Math.pow(minSpan / total, t);
      const curSpan = Math.max(1, this.viewEnd - this.viewStart);
      const anchor =
        this.currentTime >= this.viewStart && this.currentTime <= this.viewEnd
          ? this.currentTime
          : (this.viewStart + this.viewEnd) / 2;
      const ratio = (anchor - this.viewStart) / curSpan;
      let s = anchor - ratio * span;
      let e = s + span;
      if (s < 0) {
        s = 0;
        e = span;
      }
      if (e > total) {
        e = total;
        s = Math.max(0, total - span);
      }
      this.viewStart = s;
      this.viewEnd = e;
      this._syncZoomControls(false);
      this._scheduleDetailWaveformFetch();
      this.requestDraw();
    });
  }

  _syncZoomControls(updateSliderVal = true) {
    const total = Math.max(10, this.duration || 120);
    const minSpan = Math.min(total, 6);
    const span = clamp(this.viewEnd - this.viewStart, minSpan, total);

    if (this.zoomSlider) {
      let pct = 0;
      if (total > minSpan + 0.1) {
        const logVal = Math.log(span / total) / Math.log(minSpan / total);
        pct = Math.round(clamp(logVal, 0, 1) * 100);
      }
      if (updateSliderVal) this.zoomSlider.value = String(pct);
      this.zoomSlider.style.setProperty("--attr-min", this.zoomSlider.min || "0");
      this.zoomSlider.style.setProperty("--attr-max", this.zoomSlider.max || "100");
      this.zoomSlider.style.setProperty("--control-value", String(pct));
      this.zoomSlider.setAttribute("aria-valuetext", `${pct}%`);
    }

    if (this.viewSpanLabel) {
      this.viewSpanLabel.textContent =
        span >= total * 0.985 ? "全片" : formatRulerClock(span);
    }
  }

  _scheduleDetailWaveformFetch() {
    if (!this.onRequestWaveform || !this.videoStem || this.duration <= 0) return;
    if (this._waveFetchTimer) clearTimeout(this._waveFetchTimer);

    const span = this.viewEnd - this.viewStart;
    if (span >= this.duration * 0.72) {
      // 视野接近全片时直接用全片总览，不发多余请求
      this.detailWave = null;
      return;
    }

    this._waveFetchTimer = setTimeout(async () => {
      const seq = ++this._waveFetchSeq;
      const rect = this.tracksCanvas?.getBoundingClientRect();
      const targetPoints = clamp(Math.round((rect?.width || 800) * 0.85), 240, 1400);
      const reqStart = Math.max(0, this.viewStart - span * 0.08);
      const reqEnd = Math.min(this.duration, this.viewEnd + span * 0.08);
      try {
        const data = await this.onRequestWaveform(this.videoStem, reqStart, reqEnd, targetPoints);
        if (seq !== this._waveFetchSeq || !data || !Array.isArray(data.rms)) return;
        this.detailWave = data;
        this.requestDraw();
      } catch {
        // 静默降级使用全片总览波形
      }
    }, 120);
  }

  /* ==================== 坐标转换与轨道几何 ==================== */
  _getTrackLayout(h) {
    const preset = TRACK_HEIGHT_PRESETS[this.viewOptions.heightPreset] || TRACK_HEIGHT_PRESETS.normal;
    const topRuler = 0;
    const topV1 = RULER_H;
    const hV1 = this.tracksState.v1Visible ? preset.v1 : 0;
    const topA1 = topV1 + hV1;
    const hA1 = this.tracksState.a1Visible ? preset.a1 : 0;
    const topT1 = topA1 + hA1;
    const hT1 = this.tracksState.t1Visible ? Math.max(preset.t1, h - topT1) : 0;
    return { topRuler, hRuler: RULER_H, topV1, hV1, topA1, hA1, topT1, hT1 };
  }

  _timeToTrackX(sec, width) {
    const span = Math.max(0.1, this.viewEnd - this.viewStart);
    return ((sec - this.viewStart) / span) * width;
  }

  _trackXToTime(x, width) {
    const span = Math.max(0.1, this.viewEnd - this.viewStart);
    return this.viewStart + (x / Math.max(1, width)) * span;
  }

  _timeToOverviewX(sec, width) {
    const total = Math.max(1, this.duration || this.viewEnd || 120);
    return (sec / total) * width;
  }

  _overviewXToTime(x, width) {
    const total = Math.max(1, this.duration || this.viewEnd || 120);
    return clamp((x / Math.max(1, width)) * total, 0, total);
  }

  _collectSnapPoints(excludeClipRank = null) {
    const pts = [this.currentTime, 0];
    if (this.duration > 0) pts.push(this.duration);
    for (const h of this.highlights) {
      if (excludeClipRank !== null && Number(h.rank) === Number(excludeClipRank)) continue;
      pts.push(Number(h.start_sec) || 0, Number(h.end_sec) || 0);
    }
    for (const p of this.peakSegments) {
      const ps = Number(p.start_sec);
      if (ps >= this.viewStart && ps <= this.viewEnd) {
        pts.push(ps, Number(p.end_sec) || ps);
      }
    }
    return pts;
  }

  _snapTime(rawSec, widthPx, excludeClipRank = null) {
    if (!this.snapEnabled) {
      this._snapGuideSec = null;
      return rawSec;
    }
    const span = Math.max(0.1, this.viewEnd - this.viewStart);
    const secTol = (SNAP_THRESHOLD_PX / Math.max(1, widthPx)) * span;
    let best = rawSec;
    let bestDist = secTol;
    let snapped = null;
    for (const pt of this._collectSnapPoints(excludeClipRank)) {
      const d = Math.abs(pt - rawSec);
      if (d < bestDist) {
        bestDist = d;
        best = pt;
        snapped = pt;
      }
    }
    this._snapGuideSec = snapped;
    return best;
  }

  _hitTestTracks(x, y, w, h) {
    const lay = this._getTrackLayout(h);
    if (y < lay.hRuler) {
      return { zone: "ruler", sec: this._trackXToTime(x, w) };
    }

    // 命中 V1 高光切片轨
    if (lay.hV1 > 0 && y >= lay.topV1 && y < lay.topV1 + lay.hV1) {
      for (const item of this.highlights) {
        const x1 = this._timeToTrackX(Number(item.start_sec) || 0, w);
        const x2 = this._timeToTrackX(Number(item.end_sec) || 0, w);
        if (x2 < -6 || x1 > w + 6) continue;
        const bx1 = x1;
        const bx2 = Math.max(x1 + 6, x2);
        if (x >= bx1 - 4 && x <= bx2 + 4) {
          let edge = "body";
          if (!this.tracksState.v1Locked && this.tool === "select") {
            if (Math.abs(x - bx1) <= TRIM_HANDLE_PX) edge = "left";
            else if (Math.abs(x - bx2) <= TRIM_HANDLE_PX) edge = "right";
          }
          return { zone: "v1-clip", clip: item, edge, sec: this._trackXToTime(x, w) };
        }
      }
      return { zone: "v1-empty", sec: this._trackXToTime(x, w) };
    }

    // 命中 A1 波形轨
    if (lay.hA1 > 0 && y >= lay.topA1 && y < lay.topA1 + lay.hA1) {
      return { zone: "a1", sec: this._trackXToTime(x, w) };
    }

    // 命中 T1 双语字幕轨
    if (lay.hT1 > 0 && y >= lay.topT1) {
      for (const item of this.highlights) {
        const subs = Array.isArray(item.subtitles) ? item.subtitles : [];
        for (let idx = 0; idx < subs.length; idx += 1) {
          const s = subs[idx];
          const x1 = this._timeToTrackX(Number(s.start_sec) || 0, w);
          const x2 = this._timeToTrackX(Number(s.end_sec) || 0, w);
          if (x2 < -4 || x1 > w + 4) continue;
          const bx2 = Math.max(x1 + 4, x2);
          if (x >= x1 - 3 && x <= bx2 + 3) {
            let edge = "body";
            if (!this.tracksState.t1Locked && this.tool === "select" && bx2 - x1 >= 14) {
              if (Math.abs(x - x1) <= 5) edge = "left";
              else if (Math.abs(x - bx2) <= 5) edge = "right";
            }
            return {
              zone: "t1-sub",
              clip: item,
              sub: s,
              subIndex: idx,
              edge,
              sec: this._trackXToTime(x, w),
            };
          }
        }
      }
      return { zone: "t1-empty", sec: this._trackXToTime(x, w) };
    }

    return { zone: "empty", sec: this._trackXToTime(x, w) };
  }

  /* ==================== 上层全片总览交互 ==================== */
  _bindOverviewEvents() {
    const c = this.overviewCanvas;
    if (!c) return;

    c.addEventListener("pointerdown", (e) => {
      if (e.button !== 0) return;
      e.preventDefault();
      const r = c.getBoundingClientRect();
      const x = e.clientX - r.left;
      const w = Math.max(1, r.width);
      const vx1 = this._timeToOverviewX(this.viewStart, w);
      const vx2 = this._timeToOverviewX(this.viewEnd, w);
      const total = Math.max(10, this.duration || 120);

      c.setPointerCapture(e.pointerId);
      if (Math.abs(x - vx1) <= 6) {
        this._ovDrag = { mode: "left", startX: x, origStart: this.viewStart, origEnd: this.viewEnd };
      } else if (Math.abs(x - vx2) <= 6) {
        this._ovDrag = { mode: "right", startX: x, origStart: this.viewStart, origEnd: this.viewEnd };
      } else if (x > vx1 && x < vx2) {
        this._ovDrag = { mode: "move", startX: x, origStart: this.viewStart, origEnd: this.viewEnd };
      } else {
        // 点击空白处：立刻把视口中心空降到点击点，同时跳播播放头
        const clickSec = this._overviewXToTime(x, w);
        const span = this.viewEnd - this.viewStart;
        const ns = clamp(clickSec - span / 2, 0, Math.max(0, total - span));
        this.viewStart = ns;
        this.viewEnd = Math.min(total, ns + span);
        this.currentTime = clickSec;
        this.onSeek?.(clickSec);
        this._ovDrag = { mode: "move", startX: x, origStart: this.viewStart, origEnd: this.viewEnd };
        this._syncZoomControls();
        this._scheduleDetailWaveformFetch();
        this.requestDraw();
      }
    });

    c.addEventListener("pointermove", (e) => {
      const r = c.getBoundingClientRect();
      const x = e.clientX - r.left;
      const w = Math.max(1, r.width);
      const total = Math.max(10, this.duration || 120);
      const minSpan = Math.min(total, 6);

      if (!this._ovDrag) {
        const vx1 = this._timeToOverviewX(this.viewStart, w);
        const vx2 = this._timeToOverviewX(this.viewEnd, w);
        if (Math.abs(x - vx1) <= 6 || Math.abs(x - vx2) <= 6) {
          c.style.cursor = "ew-resize";
        } else if (x > vx1 && x < vx2) {
          c.style.cursor = "grab";
        } else {
          c.style.cursor = "pointer";
        }
        return;
      }

      const deltaSec = ((x - this._ovDrag.startX) / w) * total;
      if (this._ovDrag.mode === "move") {
        const span = this._ovDrag.origEnd - this._ovDrag.origStart;
        const ns = clamp(this._ovDrag.origStart + deltaSec, 0, Math.max(0, total - span));
        this.viewStart = ns;
        this.viewEnd = ns + span;
      } else if (this._ovDrag.mode === "left") {
        this.viewStart = clamp(this._ovDrag.origStart + deltaSec, 0, this._ovDrag.origEnd - minSpan);
      } else if (this._ovDrag.mode === "right") {
        this.viewEnd = clamp(this._ovDrag.origEnd + deltaSec, this._ovDrag.origStart + minSpan, total);
      }
      this._syncZoomControls();
      this._scheduleDetailWaveformFetch();
      this.requestDraw();
    });

    const endOv = () => {
      this._ovDrag = null;
    };
    c.addEventListener("pointerup", endOv);
    c.addEventListener("pointercancel", endOv);
  }

  /* ==================== 下层多轨精细交互 ==================== */
  _updateCursor() {
    const c = this.tracksCanvas;
    if (!c) return;
    if (this.tool === "razor") {
      c.style.cursor = "crosshair";
      return;
    }
    const h = this._hoverInfo;
    if (!h) {
      c.style.cursor = "default";
      return;
    }
    if ((h.zone === "v1-clip" || h.zone === "t1-sub") && (h.edge === "left" || h.edge === "right")) {
      c.style.cursor = "ew-resize";
    } else if (h.zone === "ruler") {
      c.style.cursor = "ew-resize";
    } else if (h.zone === "v1-clip" || h.zone === "t1-sub") {
      c.style.cursor = "pointer";
    } else {
      c.style.cursor = "default";
    }
  }

  _bindTracksEvents() {
    const c = this.tracksCanvas;
    if (!c) return;

    c.addEventListener(
      "wheel",
      (e) => {
        e.preventDefault();
        const r = c.getBoundingClientRect();
        const x = e.clientX - r.left;
        const anchorSec = this._trackXToTime(x, Math.max(1, r.width));
        if (e.altKey || e.ctrlKey || e.metaKey) {
          const factor = e.deltaY > 0 ? 1.24 : 0.8;
          this.zoomByFactor(factor, anchorSec);
        } else {
          const span = this.viewEnd - this.viewStart;
          const delta = (Math.abs(e.deltaX) > Math.abs(e.deltaY) ? e.deltaX : e.deltaY);
          this.panBySeconds((delta / Math.max(300, r.width)) * span * 0.85);
        }
      },
      { passive: false }
    );

    c.addEventListener("pointerdown", (e) => {
      const r = c.getBoundingClientRect();
      const x = e.clientX - r.left;
      const y = e.clientY - r.top;
      const w = Math.max(1, r.width);
      const h = Math.max(1, r.height);

      // 中键抓手平移
      if (e.button === 1) {
        e.preventDefault();
        c.setPointerCapture(e.pointerId);
        this._trkDrag = {
          mode: "pan",
          startX: x,
          origStart: this.viewStart,
          origEnd: this.viewEnd,
        };
        c.style.cursor = "grabbing";
        return;
      }

      if (e.button !== 0) return;
      e.preventDefault();
      const hit = this._hitTestTracks(x, y, w, h);

      // 剃刀模式：点字幕块直接在精确时间点断句！
      if (this.tool === "razor") {
        if (hit.zone === "t1-sub" && !this.tracksState.t1Locked) {
          this.onRazorSubtitle?.(hit.clip, hit.subIndex, hit.sec);
        } else {
          const sec = clamp(hit.sec, 0, this.duration || hit.sec);
          this.currentTime = sec;
          this.onSeek?.(sec);
          this.requestDraw();
        }
        return;
      }

      // V1 高光边缘拖拽 Trim
      if (hit.zone === "v1-clip" && (hit.edge === "left" || hit.edge === "right")) {
        c.setPointerCapture(e.pointerId);
        this.onSelectClip?.(hit.clip);
        this._trkDrag = {
          mode: "trim-clip",
          edge: hit.edge,
          clip: hit.clip,
          origStart: Number(hit.clip.start_sec),
          origEnd: Number(hit.clip.end_sec),
          changed: false,
        };
        return;
      }

      // T1 字幕边缘拖拽 Trim
      if (hit.zone === "t1-sub" && (hit.edge === "left" || hit.edge === "right")) {
        c.setPointerCapture(e.pointerId);
        this.onSelectClip?.(hit.clip);
        this._trkDrag = {
          mode: "trim-sub",
          edge: hit.edge,
          clip: hit.clip,
          sub: hit.sub,
          subIndex: hit.subIndex,
          origStart: Number(hit.sub.start_sec),
          origEnd: Number(hit.sub.end_sec),
          changed: false,
        };
        return;
      }

      // 单击 V1 切片块体：选中片段并跳播播放头
      if (hit.zone === "v1-clip") {
        this.onSelectClip?.(hit.clip);
        const sec = clamp(hit.sec, 0, this.duration || hit.sec);
        this.currentTime = sec;
        this.onSeek?.(sec);
        c.setPointerCapture(e.pointerId);
        this._trkDrag = { mode: "scrub" };
        this.requestDraw();
        return;
      }

      // 单击 T1 字幕块体：选中对应片段并跳播该字幕起点
      if (hit.zone === "t1-sub") {
        this.onSelectSubtitle?.(hit.clip, hit.sub, hit.subIndex);
        this.currentTime = Number(hit.sub.start_sec) || 0;
        this.requestDraw();
        return;
      }

      // 标尺或轨道空白：直接 Scrubbing 拖红线播放头
      c.setPointerCapture(e.pointerId);
      this._trkDrag = { mode: "scrub" };
      const sec = clamp(hit.sec, 0, this.duration || hit.sec);
      this.currentTime = sec;
      this.onSeek?.(sec);
      this.requestDraw();
    });

    c.addEventListener("pointermove", (e) => {
      const r = c.getBoundingClientRect();
      const x = e.clientX - r.left;
      const y = e.clientY - r.top;
      const w = Math.max(1, r.width);
      const h = Math.max(1, r.height);

      if (!this._trkDrag) {
        this._hoverInfo = this._hitTestTracks(x, y, w, h);
        this._updateCursor();
        if (this.tool === "razor") this.requestDraw();
        return;
      }

      const d = this._trkDrag;
      if (d.mode === "pan") {
        const span = d.origEnd - d.origStart;
        const total = Math.max(10, this.duration || 120);
        const deltaSec = -((x - d.startX) / w) * span;
        const ns = clamp(d.origStart + deltaSec, 0, Math.max(0, total - span));
        this.viewStart = ns;
        this.viewEnd = ns + span;
        this._scheduleDetailWaveformFetch();
        this.requestDraw();
        return;
      }

      if (d.mode === "scrub") {
        const sec = clamp(this._trackXToTime(x, w), 0, this.duration || 86400);
        this.currentTime = sec;
        this.onSeek?.(sec);
        this.requestDraw();
        return;
      }

      if (d.mode === "trim-clip") {
        const rawSec = clamp(this._trackXToTime(x, w), 0, this.duration || 86400);
        const snapped = this._snapTime(rawSec, w, d.clip.rank);
        const minDur = 1.0;
        let nextStart = Number(d.clip.start_sec);
        let nextEnd = Number(d.clip.end_sec);
        if (d.edge === "left") {
          nextStart = Number(clamp(snapped, 0, nextEnd - minDur).toFixed(2));
        } else {
          nextEnd = Number(clamp(snapped, nextStart + minDur, this.duration || 86400).toFixed(2));
        }
        if (nextStart !== Number(d.clip.start_sec) || nextEnd !== Number(d.clip.end_sec)) {
          d.changed = true;
          this.onTrimClipLive?.(d.clip, nextStart, nextEnd, d.edge);
          this.currentTime = d.edge === "left" ? nextStart : nextEnd;
          this.requestDraw();
        }
        return;
      }

      if (d.mode === "trim-sub") {
        const rawSec = clamp(this._trackXToTime(x, w), 0, this.duration || 86400);
        const clipS = Number(d.clip.start_sec) || 0;
        const clipE = Number(d.clip.end_sec) || this.duration || 86400;
        const minSubDur = 0.25;
        let nextStart = Number(d.sub.start_sec);
        let nextEnd = Number(d.sub.end_sec);
        if (d.edge === "left") {
          nextStart = Number(clamp(rawSec, clipS, nextEnd - minSubDur).toFixed(2));
        } else {
          nextEnd = Number(clamp(rawSec, nextStart + minSubDur, clipE).toFixed(2));
        }
        if (nextStart !== Number(d.sub.start_sec) || nextEnd !== Number(d.sub.end_sec)) {
          d.changed = true;
          this.onTrimSubtitleLive?.(d.clip, d.sub, d.subIndex, nextStart, nextEnd, d.edge);
          this.currentTime = d.edge === "left" ? nextStart : nextEnd;
          this.requestDraw();
        }
      }
    });

    const endDrag = () => {
      const d = this._trkDrag;
      this._trkDrag = null;
      this._snapGuideSec = null;
      if (d && d.changed) {
        if (d.mode === "trim-clip") {
          this.onTrimClipCommit?.(d.clip, d.edge);
        } else if (d.mode === "trim-sub") {
          this.onTrimSubtitleCommit?.(d.clip, d.sub, d.subIndex, d.edge);
        }
      }
      this._updateCursor();
      this.requestDraw();
    };
    c.addEventListener("pointerup", endDrag);
    c.addEventListener("pointercancel", endDrag);
    c.addEventListener("pointerleave", () => {
      if (!this._trkDrag) {
        this._hoverInfo = null;
        this.requestDraw();
      }
    });

    c.addEventListener("dblclick", (e) => {
      const r = c.getBoundingClientRect();
      const hit = this._hitTestTracks(e.clientX - r.left, e.clientY - r.top, r.width, r.height);
      if (hit.zone === "v1-clip") {
        this.onPreviewClip?.(hit.clip);
      } else if (hit.zone === "t1-sub") {
        this.onSelectSubtitle?.(hit.clip, hit.sub, hit.subIndex);
      }
    });

    c.addEventListener("contextmenu", (e) => {
      e.preventDefault();
      e.stopPropagation();
      const r = c.getBoundingClientRect();
      const hit = this._hitTestTracks(e.clientX - r.left, e.clientY - r.top, r.width, r.height);
      if (hit.zone === "v1-clip") {
        this.onClipContextMenu?.(e, hit.clip);
      } else if (hit.zone === "t1-sub") {
        this.onSubtitleContextMenu?.(e, hit.clip, hit.sub, hit.subIndex);
      }
    });
  }

  /* ==================== 60fps Canvas 渲染 ==================== */
  requestDraw() {
    if (this._drawPending) return;
    this._drawPending = true;
    requestAnimationFrame(() => {
      this._drawPending = false;
      this._drawOverview();
      this._drawTracks();
    });
  }

  _drawOverview() {
    const canvas = this.overviewCanvas;
    const ctx = this.overviewCtx;
    if (!canvas || !ctx) return;
    const r = canvas.getBoundingClientRect();
    const w = Math.floor(r.width);
    const h = Math.floor(r.height);
    if (w < 20 || h < 10) return;

    const dpr = Math.min(2, window.devicePixelRatio || 1);
    if (canvas.width !== Math.round(w * dpr) || canvas.height !== Math.round(h * dpr)) {
      canvas.width = Math.round(w * dpr);
      canvas.height = Math.round(h * dpr);
    }

    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.fillStyle = "#101012";
    ctx.fillRect(0, 0, w, h);

    // 1. 底层全片能量轮廓
    const wf = this.overviewWave;
    if (wf && Array.isArray(wf.peak) && wf.peak.length > 0) {
      const peaks = wf.peak;
      const rms = wf.rms || peaks;
      const thr = Number(wf.rms_threshold || 0);
      const maxVal = Math.max(0.15, ...peaks);
      const len = peaks.length;
      for (let px = 0; px < w; px += 2) {
        const idx = Math.min(len - 1, Math.floor((px / w) * len));
        const val = peaks[idx] / maxVal;
        const barH = Math.max(1, val * (h - 6));
        const isHot = thr > 0 && (rms[idx] || 0) >= thr;
        ctx.fillStyle = isHot ? "rgba(217, 155, 56, 0.55)" : "rgba(130, 140, 158, 0.28)";
        ctx.fillRect(px, h - 2 - barH, 1.5, barH);
      }
    }

    // 2. 全片高光切片色带
    for (const item of this.highlights) {
      const x1 = this._timeToOverviewX(Number(item.start_sec) || 0, w);
      const x2 = this._timeToOverviewX(Number(item.end_sec) || 0, w);
      const bw = Math.max(2.5, x2 - x1);
      const isSelected = item.selected !== false;
      const isActive = this.activeClipRank !== null && Number(item.rank) === Number(this.activeClipRank);
      ctx.fillStyle = isActive
        ? "rgba(217, 155, 56, 0.85)"
        : isSelected
          ? "rgba(195, 146, 62, 0.45)"
          : "rgba(110, 114, 126, 0.35)";
      ctx.fillRect(x1, 3, bw, 6);
    }

    // 3. 当前可视窗滑框（Viewport Handle）
    const vx1 = this._timeToOverviewX(this.viewStart, w);
    const vx2 = this._timeToOverviewX(this.viewEnd, w);
    const vw = Math.max(10, vx2 - vx1);
    ctx.fillStyle = "rgba(0, 0, 0, 0.48)";
    if (vx1 > 0) ctx.fillRect(0, 0, vx1, h);
    if (vx2 < w) ctx.fillRect(vx2, 0, w - vx2, h);

    ctx.strokeStyle = "rgba(220, 224, 232, 0.72)";
    ctx.lineWidth = 1;
    ctx.strokeRect(Math.round(vx1) + 0.5, 0.5, Math.max(8, Math.round(vw) - 1), h - 1);
    // 左右拖拽小竖柄
    ctx.fillStyle = "#e4e6ec";
    ctx.fillRect(Math.round(vx1), 4, 2, h - 8);
    ctx.fillRect(Math.round(vx1 + vw) - 2, 4, 2, h - 8);

    // 4. 全局红线播放头
    const playX = this._timeToOverviewX(this.currentTime, w);
    ctx.fillStyle = "#df4b42";
    ctx.fillRect(Math.round(playX) - 0.5, 0, 1.5, h);
  }

  _drawWaveformInTrack(ctx, w, topY, trackH) {
    const midY = topY + trackH / 2;
    ctx.fillStyle = "#121215";
    ctx.fillRect(0, topY, w, trackH);

    // 中轴参考线
    ctx.fillStyle = "rgba(255, 255, 255, 0.05)";
    ctx.fillRect(0, Math.round(midY), w, 1);

    // 优先使用命中当前视口的高精局部波形，否则用全片总览即时填充（零闪烁）
    let src = this.overviewWave;
    if (
      this.detailWave &&
      Array.isArray(this.detailWave.peak) &&
      this.detailWave.peak.length > 0 &&
      this.detailWave.start_sec <= this.viewEnd &&
      this.detailWave.end_sec >= this.viewStart
    ) {
      src = this.detailWave;
    }

    if (!src || !Array.isArray(src.peak) || src.peak.length === 0) {
      ctx.fillStyle = "#4d4d56";
      ctx.font = '10px "Segoe UI", sans-serif';
      ctx.textAlign = "center";
      ctx.fillText("等待音频波形（执行体检或粗筛后自动展示）", w / 2, midY + 4);
      return;
    }

    const peaks = src.peak;
    const rmsArr = src.rms || peaks;
    const srcStart = Number(src.start_sec || 0);
    const srcEnd = Math.max(srcStart + 0.1, Number(src.end_sec || this.duration || 1));
    const srcSpan = srcEnd - srcStart;
    const len = peaks.length;

    // 计算稳健归一化基准，防止单次爆麦压扁整轨
    const globalRef = Math.max(0.18, Number(src.rms_threshold || 0) * 2.2, 0.28);
    const fullMode = Boolean(this.viewOptions.fullWaveform);
    const halfH = fullMode ? trackH - 4 : trackH * 0.45;
    const baseBottom = topY + trackH - 2;
    const muted = this.audioMuted;

    for (let px = 0; px < w; px += 1) {
      const t0 = this._trackXToTime(px, w);
      const t1 = this._trackXToTime(px + 1, w);
      if (t1 < srcStart || t0 > srcEnd) continue;

      const i0 = clamp(Math.floor(((t0 - srcStart) / srcSpan) * len), 0, len - 1);
      const i1 = clamp(Math.ceil(((t1 - srcStart) / srcSpan) * len), i0, len - 1);
      let pk = 0;
      let rm = 0;
      for (let i = i0; i <= i1; i += 1) {
        if (peaks[i] > pk) pk = peaks[i];
        if (rmsArr[i] > rm) rm = rmsArr[i];
      }

      const pkH = Math.min(halfH, (pk / globalRef) * halfH);
      const rmH = Math.min(halfH, (rm / globalRef) * halfH);

      if (pkH > 0.5) {
        ctx.fillStyle = muted ? "rgba(110, 112, 120, 0.25)" : "rgba(72, 134, 108, 0.36)";
        if (fullMode) ctx.fillRect(px, baseBottom - pkH, 1, pkH);
        else ctx.fillRect(px, midY - pkH, 1, pkH * 2);
      }
      if (rmH > 0.5) {
        const hot = Number(src.rms_threshold || 0) > 0 && rm >= Number(src.rms_threshold);
        ctx.fillStyle = muted
          ? "rgba(128, 130, 138, 0.4)"
          : hot
            ? "rgba(217, 155, 56, 0.78)"
            : "rgba(88, 178, 122, 0.72)";
        if (fullMode) ctx.fillRect(px, baseBottom - rmH, 1, rmH);
        else ctx.fillRect(px, midY - rmH, 1, rmH * 2);
      }
    }

    // Step 1 音频能量阈值虚线
    const thr = Number(src.rms_threshold || 0);
    if (thr > 0) {
      const thrOffset = Math.min(halfH - 2, (thr / globalRef) * halfH);
      const thrY = fullMode ? Math.round(baseBottom - thrOffset) + 0.5 : Math.round(midY - thrOffset) + 0.5;
      ctx.save();
      ctx.strokeStyle = "rgba(217, 155, 56, 0.45)";
      ctx.lineWidth = 1;
      ctx.setLineDash([3, 3]);
      ctx.beginPath();
      ctx.moveTo(0, thrY);
      ctx.lineTo(w, thrY);
      ctx.stroke();
      ctx.restore();
    }
  }

  _drawTracks() {
    const canvas = this.tracksCanvas;
    const ctx = this.tracksCtx;
    if (!canvas || !ctx) return;
    const r = canvas.getBoundingClientRect();
    const w = Math.floor(r.width);
    const h = Math.floor(r.height);
    if (w < 20 || h < 20) return;

    const dpr = Math.min(2, window.devicePixelRatio || 1);
    if (canvas.width !== Math.round(w * dpr) || canvas.height !== Math.round(h * dpr)) {
      canvas.width = Math.round(w * dpr);
      canvas.height = Math.round(h * dpr);
    }

    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.fillStyle = "#141417";
    ctx.fillRect(0, 0, w, h);

    const lay = this._getTrackLayout(h);
    const span = Math.max(0.1, this.viewEnd - this.viewStart);

    /* ---------- 1. Timecode Ruler ---------- */
    ctx.fillStyle = "#1b1b1f";
    ctx.fillRect(0, 0, w, lay.hRuler);
    ctx.fillStyle = "#09090a";
    ctx.fillRect(0, lay.hRuler - 1, w, 1);

    const majorStep = chooseRulerStep(span, w);
    const minorStep = majorStep / 5;
    const firstMinor = Math.floor(this.viewStart / minorStep) * minorStep;

    ctx.font = '10px "Cascadia Mono", Consolas, monospace';
    ctx.textAlign = "left";
    for (let t = firstMinor; t <= this.viewEnd + minorStep; t += minorStep) {
      if (t < 0) continue;
      const tx = Math.round(this._timeToTrackX(t, w));
      if (tx < -40 || tx > w + 40) continue;
      const isMajor = Math.abs(t / majorStep - Math.round(t / majorStep)) < 0.01;
      if (isMajor) {
        ctx.fillStyle = "#484852";
        ctx.fillRect(tx, 10, 1, lay.hRuler - 11);
        ctx.fillStyle = "#8a8a96";
        ctx.fillText(formatRulerClock(t), tx + 4, 12);
        // 极淡纵向参考网格线
        ctx.fillStyle = "rgba(255, 255, 255, 0.025)";
        ctx.fillRect(tx, lay.hRuler, 1, h - lay.hRuler);
      } else {
        ctx.fillStyle = "#2d2d34";
        ctx.fillRect(tx, 16, 1, lay.hRuler - 17);
      }
    }

    /* ---------- 2. V1 高光切片轨 ---------- */
    if (lay.hV1 > 0) {
      ctx.fillStyle = "#16161a";
      ctx.fillRect(0, lay.topV1, w, lay.hV1);
      ctx.fillStyle = "#09090a";
      ctx.fillRect(0, lay.topV1 + lay.hV1 - 1, w, 1);

      for (const item of this.highlights) {
        const s = Number(item.start_sec) || 0;
        const e = Number(item.end_sec) || s + 1;
        if (e < this.viewStart || s > this.viewEnd) continue;

        const x1 = this._timeToTrackX(s, w);
        const x2 = this._timeToTrackX(e, w);
        const drawX = Math.max(0, x1);
        const drawW = Math.max(3, Math.min(w, x2) - drawX);
        const y = lay.topV1 + 5;
        const bh = lay.hV1 - 11;

        const isSelected = item.selected !== false;
        const isActive = this.activeClipRank !== null && Number(item.rank) === Number(this.activeClipRank);
        const isHovered = this._hoverInfo?.zone === "v1-clip" && this._hoverInfo.clip === item;

        // 达芬奇风格低饱和工业色块（零塑料霓虹）
        ctx.fillStyle = isActive
          ? "rgba(217, 155, 56, 0.28)"
          : isSelected
            ? "rgba(88, 122, 158, 0.30)"
            : "rgba(68, 68, 76, 0.26)";
        ctx.fillRect(drawX, y, drawW, bh);

        // 顶部色带细条
        ctx.fillStyle = isActive
          ? "#d99b38"
          : isSelected
            ? "#6e92b8"
            : "#52525c";
        ctx.fillRect(drawX, y, drawW, 2.5);

        // 描边
        ctx.strokeStyle = isActive
          ? "#e6b050"
          : isHovered
            ? "rgba(220, 224, 232, 0.55)"
            : "rgba(255, 255, 255, 0.14)";
        ctx.lineWidth = 1;
        ctx.strokeRect(drawX + 0.5, y + 0.5, Math.max(2, drawW - 1), bh - 1);

        // 悬停在左右 Trim 热区时高亮微把手
        if (isHovered && !this.tracksState.v1Locked && this.tool === "select") {
          if (this._hoverInfo.edge === "left" && x1 >= -2 && x1 <= w) {
            ctx.fillStyle = "#f0ba59";
            ctx.fillRect(Math.round(x1), y, 3, bh);
          } else if (this._hoverInfo.edge === "right" && x2 >= 0 && x2 <= w + 2) {
            ctx.fillStyle = "#f0ba59";
            ctx.fillRect(Math.round(x2) - 3, y, 3, bh);
          }
        }

        // 块内信息标签（空间足够且开启名称显示时裁剪绘制）
        if (this.viewOptions.showClipNames && drawW >= 34) {
          ctx.save();
          ctx.beginPath();
          ctx.rect(drawX + 4, y + 2, drawW - 8, bh - 4);
          ctx.clip();
          ctx.font = '600 11px "Segoe UI", "Microsoft YaHei UI", sans-serif';
          ctx.fillStyle = isSelected ? "#e8e8ec" : "#8b8b96";
          const label = `#${item.rank} ${item.title_zh || item.title_en || ""}`;
          ctx.fillText(label, drawX + 5, y + 16);

          if (drawW >= 95) {
            ctx.font = '10px "Cascadia Mono", Consolas, monospace';
            ctx.fillStyle = "#9696a2";
            const dur = Math.max(0, e - s).toFixed(1);
            ctx.fillText(`${Number(item.score || 0).toFixed(1)}分 · ${dur}s`, drawX + 5, y + 28);
          }
          ctx.restore();
        }
      }
    }

    /* ---------- 3. A1 音频波形轨 ---------- */
    if (lay.hA1 > 0) {
      this._drawWaveformInTrack(ctx, w, lay.topA1, lay.hA1);
      ctx.fillStyle = "#09090a";
      ctx.fillRect(0, lay.topA1 + lay.hA1 - 1, w, 1);
    }

    /* ---------- 4. T1 双语字幕轨 ---------- */
    if (lay.hT1 > 0) {
      ctx.fillStyle = "#151518";
      ctx.fillRect(0, lay.topT1, w, lay.hT1);

      const subY = lay.topT1 + 5;
      const subH = Math.min(26, lay.hT1 - 10);

      for (const item of this.highlights) {
        const subs = Array.isArray(item.subtitles) ? item.subtitles : [];
        for (let idx = 0; idx < subs.length; idx += 1) {
          const s = subs[idx];
          const st = Number(s.start_sec) || 0;
          const ed = Number(s.end_sec) || st + 0.5;
          if (ed < this.viewStart || st > this.viewEnd) continue;

          const x1 = this._timeToTrackX(st, w);
          const x2 = this._timeToTrackX(ed, w);
          const drawX = Math.max(0, x1);
          const drawW = Math.max(2, Math.min(w, x2) - drawX);
          const key = `${item.rank}:${idx}`;
          const isCur = key === this.activeSubKey;
          const isHov = this._hoverInfo?.zone === "t1-sub" && this._hoverInfo.sub === s;

          ctx.fillStyle = isCur
            ? "rgba(217, 155, 56, 0.32)"
            : isHov
              ? "rgba(150, 162, 182, 0.28)"
              : "rgba(122, 132, 148, 0.18)";
          ctx.fillRect(drawX, subY, drawW, subH);

          ctx.strokeStyle = isCur
            ? "#d99b38"
            : "rgba(176, 188, 206, 0.28)";
          ctx.lineWidth = 1;
          ctx.strokeRect(drawX + 0.5, subY + 0.5, Math.max(1.5, drawW - 1), subH - 1);

          if (drawW >= 32) {
            ctx.save();
            ctx.beginPath();
            ctx.rect(drawX + 3, subY + 1, drawW - 6, subH - 2);
            ctx.clip();
            ctx.font = '10.5px "Segoe UI", "Microsoft YaHei UI", sans-serif';
            ctx.fillStyle = isCur ? "#f5f5f7" : "#c6c8d0";
            ctx.fillText((s.text_tgt || s.text_zh || s.text_en || "").trim(), drawX + 4, subY + 16);
            ctx.restore();
          }
        }
      }
    }

    /* ---------- 5. 磁吸金色辅助竖线 ---------- */
    if (this._snapGuideSec !== null) {
      const sx = Math.round(this._timeToTrackX(this._snapGuideSec, w)) + 0.5;
      ctx.save();
      ctx.strokeStyle = "#f0ba59";
      ctx.lineWidth = 1;
      ctx.setLineDash([4, 2]);
      ctx.beginPath();
      ctx.moveTo(sx, lay.hRuler);
      ctx.lineTo(sx, h);
      ctx.stroke();
      ctx.restore();
    }

    /* ---------- 6. 剃刀悬停虚线刀标 ---------- */
    if (this.tool === "razor" && this._hoverInfo?.zone === "t1-sub") {
      const rx = Math.round(this._timeToTrackX(this._hoverInfo.sec, w)) + 0.5;
      ctx.save();
      ctx.strokeStyle = "#df4b42";
      ctx.lineWidth = 1;
      ctx.setLineDash([2, 2]);
      ctx.beginPath();
      ctx.moveTo(rx, lay.topT1);
      ctx.lineTo(rx, lay.topT1 + lay.hT1);
      ctx.stroke();
      ctx.restore();
    }

    /* ---------- 7. 全轨达芬奇信号红播放头 ---------- */
    const playX = this._timeToTrackX(this.currentTime, w);
    if (playX >= -6 && playX <= w + 6) {
      const cx = Math.round(playX) + 0.5;
      ctx.fillStyle = "#df4b42";
      ctx.beginPath();
      ctx.moveTo(cx - 5, 0);
      ctx.lineTo(cx + 5, 0);
      ctx.lineTo(cx + 5, 7);
      ctx.lineTo(cx, 12);
      ctx.lineTo(cx - 5, 7);
      ctx.closePath();
      ctx.fill();
      ctx.fillRect(Math.round(playX) - 0.5, 11, 1.5, h - 11);
    }

    /* ---------- 8. 拖拽 Trim 浮动实时时码提示 ---------- */
    if (this._trkDrag && (this._trkDrag.mode === "trim-clip" || this._trkDrag.mode === "trim-sub")) {
      const d = this._trkDrag;
      const tSec =
        d.mode === "trim-clip"
          ? d.edge === "left"
            ? Number(d.clip.start_sec)
            : Number(d.clip.end_sec)
          : d.edge === "left"
            ? Number(d.sub.start_sec)
            : Number(d.sub.end_sec);
      const tx = clamp(this._timeToTrackX(tSec, w), 38, w - 58);
      const label = formatTimecode(tSec, true);
      ctx.fillStyle = "rgba(18, 18, 20, 0.92)";
      ctx.fillRect(tx - 34, lay.hRuler + 3, 68, 17);
      ctx.strokeStyle = "#d99b38";
      ctx.lineWidth = 1;
      ctx.strokeRect(tx - 34 + 0.5, lay.hRuler + 3.5, 67, 16);
      ctx.fillStyle = "#f0ba59";
      ctx.font = '600 10px "Cascadia Mono", Consolas, monospace';
      ctx.textAlign = "center";
      ctx.fillText(label, tx, lay.hRuler + 14.5);
    }
  }
}
