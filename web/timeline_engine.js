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

const RULER_H = 26;
const TRACK_HEIGHT_PRESETS = {
  compact: { v1: 40, a1: 46, t1: 36 },
  normal: { v1: 52, a1: 64, t1: 44 },
  tall: { v1: 62, a1: 88, t1: 50 },
};
const UI_FONT_FAMILY =
  '"Segoe UI", "Yu Gothic UI", "Malgun Gothic", "Microsoft YaHei UI", "PingFang SC", sans-serif';
const MONO_FONT_FAMILY = '"Cascadia Mono", "Consolas", monospace';
const TRIM_HANDLE_PX = 7;
const SNAP_THRESHOLD_PX = 8;

function clamp(v, min, max) {
  return Math.max(min, Math.min(max, v));
}

function fitTextEllipsis(ctx, text, maxWidth) {
  const str = String(text ?? "");
  if (!str || maxWidth <= 0) return "";
  if (ctx.measureText(str).width <= maxWidth) return str;
  const ellipsis = "…";
  const ellW = ctx.measureText(ellipsis).width;
  if (ellW > maxWidth) return "";
  const chars = Array.from(str);
  let lo = 0;
  let hi = chars.length - 1;
  let best = 0;
  while (lo <= hi) {
    const mid = (lo + hi) >> 1;
    const cand = chars.slice(0, mid).join("") + ellipsis;
    if (ctx.measureText(cand).width <= maxWidth) {
      best = mid;
      lo = mid + 1;
    } else {
      hi = mid - 1;
    }
  }
  return best > 0 ? chars.slice(0, best).join("") + ellipsis : ellipsis;
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
    onChapterClick,
    onChapterContextMenu,
    onRulerContextMenu,
    onChatPeakClick,
    onChatPeakContextMenu,
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
    this.onChapterClick = onChapterClick;
    this.onChapterContextMenu = onChapterContextMenu;
    this.onRulerContextMenu = onRulerContextMenu;
    this.onChatPeakClick = onChatPeakClick;
    this.onChatPeakContextMenu = onChatPeakContextMenu;
    this.onRequestWaveform = onRequestWaveform;
    this.onToolChange = onToolChange;
    this.onSnapChange = onSnapChange;

    // 媒体与数据状态
    this.mediaKey = "";
    this.videoStem = "";
    this.duration = 0;
    this.fps = 30;
    this.currentTime = 0;
    this.highlights = [];
    this.activeClipRank = null;
    this.activeSubKey = "";
    this.peakSegments = [];
    this.chapters = [];
    this.deadAirSegments = [];
    this.chatRadar = null; // { buckets, chat_peaks, fusion_peaks, search }

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

    this._syncHeaderHeights();
    this.requestDraw();
  }

  /* ==================== 外部数据同步接口 ==================== */
  setMedia({ stem = "", mediaKey = "", duration = 0, fps = 30, resetView = false }) {
    const nextStem = stem || "";
    const nextMediaKey = mediaKey || nextStem;
    const changedStem = nextStem !== this.videoStem;
    const changedMedia = nextMediaKey !== this.mediaKey || changedStem;
    this.mediaKey = nextMediaKey;
    this.videoStem = nextStem;
    this.duration = Math.max(0, Number(duration) || 0);
    this.fps = Math.max(1, Number(fps) || 30);

    if (changedMedia || resetView) {
      this.overviewWave = null;
      this.detailWave = null;
      this.peakSegments = [];
      this.chapters = [];
      this.deadAirSegments = [];
      this.chatRadar = null;
      this._waveFetchSeq += 1;
      if (this._waveFetchTimer) {
        clearTimeout(this._waveFetchTimer);
        this._waveFetchTimer = null;
      }
    }

    if (changedMedia || resetView || this.viewEnd <= this.viewStart) {
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

  setChapters(chapters) {
    this.chapters = Array.isArray(chapters) ? chapters : [];
    this.requestDraw();
  }

  setDeadAirSegments(segments) {
    this.deadAirSegments = Array.isArray(segments) ? segments : [];
    this.requestDraw();
  }

  setChatRadar(radarData) {
    this.chatRadar = radarData && radarData.exists !== false ? radarData : null;
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
      const fullLabel = window.SolacutI18n ? window.SolacutI18n.t("tl.spanAll") : "Full";
      this.viewSpanLabel.textContent =
        span >= total * 0.985 ? fullLabel : formatRulerClock(span);
    }
  }

  refreshLocale() {
    this._syncZoomControls(false);
    this.requestDraw();
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
      const reqMediaKey = this.mediaKey;
      const rect = this.tracksCanvas?.getBoundingClientRect();
      const targetPoints = clamp(Math.round((rect?.width || 800) * 0.85), 240, 1400);
      const reqStart = Math.max(0, this.viewStart - span * 0.08);
      const reqEnd = Math.min(this.duration, this.viewEnd + span * 0.08);
      try {
        const data = await this.onRequestWaveform(this.videoStem, reqStart, reqEnd, targetPoints);
        if (seq !== this._waveFetchSeq || reqMediaKey !== this.mediaKey || !data || !Array.isArray(data.rms)) return;
        this.detailWave = data;
        this.requestDraw();
      } catch {
        // 静默降级使用全片总览波形
      }
    }, 120);
  }

  /* ==================== 坐标转换与轨道几何 ==================== */
  _getCanvasMetrics(canvas, e = null) {
    const r = canvas.getBoundingClientRect();
    const w = Math.max(1, canvas.clientWidth || Math.round(r.width));
    const h = Math.max(1, canvas.clientHeight || Math.round(r.height));
    const dpr = Math.min(3, window.devicePixelRatio || 1);
    const physW = Math.max(1, Math.round((r.width || w) * dpr));
    const physH = Math.max(1, Math.round((r.height || h) * dpr));
    const out = { r, w, h, dpr, physW, physH, scaleX: physW / w, scaleY: physH / h, x: 0, y: 0 };
    if (e) {
      out.x = (e.clientX - r.left) * (w / Math.max(1, r.width));
      out.y = (e.clientY - r.top) * (h / Math.max(1, r.height));
    }
    return out;
  }

  _getTrackLayout(h) {
    const preset = TRACK_HEIGHT_PRESETS[this.viewOptions.heightPreset] || TRACK_HEIGHT_PRESETS.normal;
    const topRuler = 0;
    const topV1 = RULER_H;
    const hV1 = this.tracksState.v1Visible ? preset.v1 : 0;
    const topA1 = topV1 + hV1;
    const hA1 = this.tracksState.a1Visible ? preset.a1 : 0;
    const topT1 = topA1 + hA1;
    const hT1 = this.tracksState.t1Visible ? preset.t1 : 0;
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
    for (const ch of this.chapters) {
      const cs = Number(ch.start_sec);
      if (cs >= this.viewStart && cs <= this.viewEnd) {
        pts.push(cs);
      }
    }
    const chatPeaks = this.chatRadar?.chat_peaks || [];
    for (const cp of chatPeaks) {
      const ps = Number(cp.peak_sec ?? cp.start_sec);
      if (ps >= this.viewStart && ps <= this.viewEnd) {
        pts.push(ps);
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
      // 优先检测标尺底部的 OBS 章节翡翠绿菱形标记
      if (this.chapters.length > 0 && y >= 4) {
        let bestCh = null;
        let bestDx = 9;
        for (const ch of this.chapters) {
          const cx = this._timeToTrackX(Number(ch.start_sec) || 0, w);
          if (cx < -10 || cx > w + 10) continue;
          const dx = Math.abs(x - cx);
          if (dx <= bestDx) {
            bestDx = dx;
            bestCh = ch;
          }
        }
        if (bestCh) {
          return { zone: "chapter", chapter: bestCh, sec: Number(bestCh.start_sec) || 0 };
        }
      }
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

    // 命中 A1 波形轨（优先命中顶部弹幕爆点锚点）
    if (lay.hA1 > 0 && y >= lay.topA1 && y < lay.topA1 + lay.hA1) {
      const chatPeaks = this.chatRadar?.chat_peaks || [];
      if (chatPeaks.length > 0 && y <= lay.topA1 + 18) {
        let bestPeak = null;
        let bestDx = 9;
        for (const cp of chatPeaks) {
          const px = this._timeToTrackX(Number(cp.peak_sec ?? cp.start_sec) || 0, w);
          if (px < -10 || px > w + 10) continue;
          const dx = Math.abs(x - px);
          if (dx <= bestDx) {
            bestDx = dx;
            bestPeak = cp;
          }
        }
        if (bestPeak) {
          return {
            zone: "chat-peak",
            peak: bestPeak,
            sec: Number(bestPeak.peak_sec ?? bestPeak.start_sec) || 0,
          };
        }
      }
      return { zone: "a1", sec: this._trackXToTime(x, w) };
    }

    // 命中 T1 双语字幕轨
    if (lay.hT1 > 0 && y >= lay.topT1 && y < lay.topT1 + lay.hT1) {
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
      const { x, w } = this._getCanvasMetrics(c, e);
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
      const { x, w } = this._getCanvasMetrics(c, e);
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
    } else if (h.zone === "v1-clip" || h.zone === "t1-sub" || h.zone === "chapter" || h.zone === "chat-peak") {
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
        const { x, w } = this._getCanvasMetrics(c, e);
        const anchorSec = this._trackXToTime(x, w);
        if (e.altKey || e.ctrlKey || e.metaKey) {
          const factor = e.deltaY > 0 ? 1.24 : 0.8;
          this.zoomByFactor(factor, anchorSec);
        } else {
          const span = this.viewEnd - this.viewStart;
          const delta = (Math.abs(e.deltaX) > Math.abs(e.deltaY) ? e.deltaX : e.deltaY);
          this.panBySeconds((delta / Math.max(300, w)) * span * 0.85);
        }
      },
      { passive: false }
    );

    c.addEventListener("pointerdown", (e) => {
      const { x, y, w, h } = this._getCanvasMetrics(c, e);

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

      // 单击 OBS 章节标记：跳播到章节起点
      if (hit.zone === "chapter") {
        const sec = clamp(Number(hit.chapter.start_sec) || 0, 0, this.duration || 86400);
        this.currentTime = sec;
        this.onSeek?.(sec);
        this.onChapterClick?.(hit.chapter);
        this.requestDraw();
        return;
      }

      // 单击弹幕爆点锚点：跳播到爆点时刻
      if (hit.zone === "chat-peak") {
        const sec = clamp(Number(hit.peak.peak_sec ?? hit.peak.start_sec) || 0, 0, this.duration || 86400);
        this.currentTime = sec;
        this.onSeek?.(sec);
        this.onChatPeakClick?.(hit.peak);
        this.requestDraw();
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
      const { x, y, w, h } = this._getCanvasMetrics(c, e);

      if (!this._trkDrag) {
        const prevZone = this._hoverInfo?.zone;
        const prevCh = this._hoverInfo?.chapter;
        const prevPeak = this._hoverInfo?.peak;
        this._hoverInfo = this._hitTestTracks(x, y, w, h);
        this._updateCursor();
        const hoverMarkerChanged =
          prevZone !== this._hoverInfo?.zone ||
          prevCh !== this._hoverInfo?.chapter ||
          prevPeak !== this._hoverInfo?.peak;
        if (
          this.tool === "razor" ||
          hoverMarkerChanged ||
          this._hoverInfo?.zone === "chapter" ||
          this._hoverInfo?.zone === "chat-peak"
        ) {
          this.requestDraw();
        }
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
      const { x, y, w, h } = this._getCanvasMetrics(c, e);
      const hit = this._hitTestTracks(x, y, w, h);
      if (hit.zone === "v1-clip") {
        this.onPreviewClip?.(hit.clip);
      } else if (hit.zone === "t1-sub") {
        this.onSelectSubtitle?.(hit.clip, hit.sub, hit.subIndex);
      }
    });

    c.addEventListener("contextmenu", (e) => {
      e.preventDefault();
      e.stopPropagation();
      const { x, y, w, h } = this._getCanvasMetrics(c, e);
      const hit = this._hitTestTracks(x, y, w, h);
      if (hit.zone === "chapter") {
        this.onChapterContextMenu?.(e, hit.chapter);
      } else if (hit.zone === "ruler") {
        this.onRulerContextMenu?.(e, hit.sec);
      } else if (hit.zone === "chat-peak") {
        this.onChatPeakContextMenu?.(e, hit.peak);
      } else if (hit.zone === "v1-clip") {
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
    const { w, h, physW, physH, scaleX, scaleY } = this._getCanvasMetrics(canvas);
    if (w < 20 || h < 10) return;

    if (canvas.width !== physW || canvas.height !== physH) {
      canvas.width = physW;
      canvas.height = physH;
    }

    ctx.setTransform(scaleX, 0, 0, scaleY, 0, 0);
    ctx.fillStyle = "#0e0f12";
    ctx.fillRect(0, 0, w, h);

    // 1. 底层全片 Dead Air 死寂空白区
    if (this.deadAirSegments.length > 0) {
      ctx.fillStyle = "#191a1f";
      for (const seg of this.deadAirSegments) {
        const dx1 = this._timeToOverviewX(Number(seg.start_sec) || 0, w);
        const dx2 = this._timeToOverviewX(Number(seg.end_sec) || 0, w);
        if (dx2 > dx1) {
          ctx.fillRect(dx1, 0, Math.max(1.5, dx2 - dx1), h);
        }
      }
    }

    // 1b. 底层全片单色实心音频能量轮廓
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
        ctx.fillStyle = isHot ? "#695332" : "#2c2f38";
        ctx.fillRect(px, h - 2 - barH, 1.3, barH);
      }
    }

    // 1c. 弹幕细钢蓝折线叠加（含梗词命中柱）
    const chatBuckets = this.chatRadar?.buckets;
    if (Array.isArray(chatBuckets) && chatBuckets.length > 0) {
      ctx.save();
      ctx.beginPath();
      let started = false;
      for (const b of chatBuckets) {
        const midSec = ((Number(b.start_sec) || 0) + (Number(b.end_sec) || Number(b.start_sec) || 0)) * 0.5;
        const bx = this._timeToOverviewX(midSec, w);
        const norm = clamp(Number(b.norm) || 0, 0, 1);
        const by = h - 2 - norm * (h - 7);
        if (!started) {
          ctx.moveTo(bx, by);
          started = true;
        } else {
          ctx.lineTo(bx, by);
        }
      }
      if (started) {
        ctx.strokeStyle = "#6aa5d4";
        ctx.lineWidth = 1;
        ctx.stroke();
      }
      const searchCurve = this.chatRadar?.search?.curve;
      if (Array.isArray(searchCurve) && searchCurve.length > 0) {
        ctx.fillStyle = "#7cb8e6";
        for (const sb of searchCurve) {
          if (!(Number(sb.hits) > 0)) continue;
          const sx1 = this._timeToOverviewX(Number(sb.start_sec) || 0, w);
          const sx2 = this._timeToOverviewX(Number(sb.end_sec) || Number(sb.start_sec) + 2, w);
          const sh = Math.max(2, clamp(Number(sb.norm) || 0, 0, 1) * (h - 6));
          ctx.fillRect(sx1, h - 2 - sh, Math.max(2, sx2 - sx1), sh);
        }
      }
      ctx.restore();
    }

    // 2. 全片高光切片顶部 4px 实心色条
    for (const item of this.highlights) {
      const x1 = this._timeToOverviewX(Number(item.start_sec) || 0, w);
      const x2 = this._timeToOverviewX(Number(item.end_sec) || 0, w);
      const bw = Math.max(2.5, x2 - x1);
      const isSelected = item.selected !== false;
      const isReviewed = String(item.review_status || "draft").toLowerCase() === "reviewed";
      const isActive = this.activeClipRank !== null && Number(item.rank) === Number(this.activeClipRank);
      ctx.fillStyle = isActive
        ? "#d8962e"
        : !isSelected
          ? "#454852"
          : isReviewed
            ? "#2fa15a"
            : "#4b7ea8";
      ctx.fillRect(x1, 0, bw, 4);
    }

    // 2b. OBS 章节翡翠绿微针
    if (this.chapters.length > 0) {
      ctx.fillStyle = "#38b27c";
      for (const ch of this.chapters) {
        const cx = Math.round(this._timeToOverviewX(Number(ch.start_sec) || 0, w));
        ctx.fillRect(cx - 1, 0, 2, 5);
      }
    }

    // 3. 当前可视窗滑框（Viewport Handle）
    const vx1 = this._timeToOverviewX(this.viewStart, w);
    const vx2 = this._timeToOverviewX(this.viewEnd, w);
    const vw = Math.max(10, vx2 - vx1);
    ctx.fillStyle = "rgba(8, 8, 10, 0.55)";
    if (vx1 > 0) ctx.fillRect(0, 0, vx1, h);
    if (vx2 < w) ctx.fillRect(vx2, 0, w - vx2, h);

    ctx.strokeStyle = "#d4d7e0";
    ctx.lineWidth = 1;
    ctx.strokeRect(Math.round(vx1) + 0.5, 0.5, Math.max(8, Math.round(vw) - 1), h - 1);
    // 左右拖拽小竖柄
    ctx.fillStyle = "#d4d7e0";
    ctx.fillRect(Math.round(vx1), 4, 2, h - 8);
    ctx.fillRect(Math.round(vx1 + vw) - 2, 4, 2, h - 8);

    // 4. 全局红线播放头
    const playX = this._timeToOverviewX(this.currentTime, w);
    ctx.fillStyle = "#df4b42";
    ctx.fillRect(Math.round(playX) - 0.5, 0, 1.5, h);
  }

  _drawDeadAirOverlay(ctx, w, topY, trackH) {
    if (!this.deadAirSegments.length || trackH <= 0) return;
    ctx.save();
    for (const seg of this.deadAirSegments) {
      const s = Number(seg.start_sec) || 0;
      const e = Number(seg.end_sec) || s;
      if (e < this.viewStart || s > this.viewEnd || e <= s) continue;
      const x1 = Math.max(0, this._timeToTrackX(s, w));
      const x2 = Math.min(w, this._timeToTrackX(e, w));
      const bw = x2 - x1;
      if (bw < 1.5) continue;

      ctx.fillStyle = "rgba(116, 120, 132, 0.14)";
      ctx.fillRect(x1, topY, bw, trackH);

      // 克制 45° 斜纹暗灰线（仅在区域足够宽时绘制，性能极致）
      if (bw >= 6) {
        ctx.save();
        ctx.beginPath();
        ctx.rect(x1, topY, bw, trackH);
        ctx.clip();
        ctx.strokeStyle = "rgba(156, 162, 174, 0.16)";
        ctx.lineWidth = 1;
        const step = 9;
        ctx.beginPath();
        for (let sx = x1 - trackH; sx < x2 + step; sx += step) {
          ctx.moveTo(sx, topY + trackH);
          ctx.lineTo(sx + trackH, topY);
        }
        ctx.stroke();
        ctx.restore();
      }
    }
    ctx.restore();
  }

  _drawChatRadarInTrack(ctx, w, topY, trackH) {
    if (!this.chatRadar || trackH <= 0) return;
    const buckets = this.chatRadar.buckets;
    if (Array.isArray(buckets) && buckets.length > 0) {
      ctx.save();
      ctx.beginPath();
      ctx.rect(0, topY, w, trackH);
      ctx.clip();

      const topEdge = topY + 4;
      const baseY = topY + trackH - 3;
      const usableH = Math.max(10, baseY - topEdge);
      let started = false;
      let firstX = 0;
      let lastX = 0;

      ctx.beginPath();
      for (const b of buckets) {
        const bs = Number(b.start_sec) || 0;
        const be = Number(b.end_sec) || bs;
        if (be < this.viewStart - 30 || bs > this.viewEnd + 30) continue;
        const midX = this._timeToTrackX((bs + be) * 0.5, w);
        const norm = clamp(Number(b.norm) || 0, 0, 1);
        const y = baseY - norm * usableH;
        if (!started) {
          firstX = midX;
          ctx.moveTo(midX, y);
          started = true;
        } else {
          ctx.lineTo(midX, y);
        }
        lastX = midX;
      }

      if (started) {
        ctx.strokeStyle = "#6aa5d4";
        ctx.lineWidth = 1.5;
        ctx.stroke();

        ctx.lineTo(lastX, baseY);
        ctx.lineTo(firstX, baseY);
        ctx.closePath();
        ctx.fillStyle = "rgba(106, 165, 212, 0.08)";
        ctx.fill();
      }

      // 梗词搜索命中曲线（钢蓝柱）
      const searchCurve = this.chatRadar.search?.curve;
      if (Array.isArray(searchCurve) && searchCurve.length > 0) {
        ctx.fillStyle = "#7cb8e6";
        for (const sb of searchCurve) {
          if (!(Number(sb.hits) > 0)) continue;
          const ss = Number(sb.start_sec) || 0;
          const se = Number(sb.end_sec) || ss + 1;
          if (se < this.viewStart || ss > this.viewEnd) continue;
          const sx1 = Math.max(0, this._timeToTrackX(ss, w));
          const sx2 = Math.min(w, this._timeToTrackX(se, w));
          const sh = Math.max(3, clamp(Number(sb.norm) || 0, 0, 1) * usableH);
          ctx.fillRect(sx1, baseY - sh, Math.max(2.5, sx2 - sx1), sh);
        }
      }

      // Top 弹幕爆点锚点（A1 顶部钢蓝锚点圆点）
      const chatPeaks = this.chatRadar.chat_peaks || [];
      for (const cp of chatPeaks) {
        const pSec = Number(cp.peak_sec ?? cp.start_sec) || 0;
        if (pSec < this.viewStart || pSec > this.viewEnd) continue;
        const px = this._timeToTrackX(pSec, w);
        const isHov = this._hoverInfo?.zone === "chat-peak" && this._hoverInfo.peak === cp;
        const cy = topY + 8;
        ctx.beginPath();
        ctx.arc(px, cy, isHov ? 4.5 : 3.2, 0, Math.PI * 2);
        ctx.fillStyle = isHov ? "#b8dcf7" : "#7cb8e6";
        ctx.fill();
        ctx.strokeStyle = "#11161d";
        ctx.lineWidth = 1;
        ctx.stroke();
      }

      ctx.restore();
    }
  }

  _drawWaveformInTrack(ctx, w, topY, trackH) {
    const midY = topY + trackH / 2;
    const fullMode = Boolean(this.viewOptions.fullWaveform);

    // Dead Air 死寂空白区暗灰斜纹标注（不铺实心轨道底，以免遮挡标尺网格线与 OBS 章节投射线）
    this._drawDeadAirOverlay(ctx, w, topY, trackH);

    // 中轴参考线
    if (!fullMode) {
      ctx.fillStyle = "rgba(255, 255, 255, 0.05)";
      ctx.fillRect(0, Math.round(midY), w, 1);
    }

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
      this._drawChatRadarInTrack(ctx, w, topY, trackH);
      if (!this.chatRadar) {
        ctx.save();
        ctx.fillStyle = "#7e828f";
        ctx.font = `12px ${UI_FONT_FAMILY}`;
        ctx.textAlign = "center";
        ctx.textBaseline = "middle";
        const waitMsg = window.SolacutI18n
          ? window.SolacutI18n.t("tl.waitWave")
          : "Waiting for audio waveform";
        ctx.fillText(waitMsg, Math.round(w / 2), Math.round(midY));
        ctx.restore();
      }
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
    const halfH = fullMode ? trackH - 4 : trackH * 0.45;
    const baseBottom = topY + trackH - 2;
    const muted = this.audioMuted;
    const thr = Number(src.rms_threshold || 0);

    // Step 1 高能阈值参考虚线（对称模式画上下双边，全高模式画单条）
    if (thr > 0) {
      const thrOffset = Math.min(halfH - 2, (thr / globalRef) * halfH);
      ctx.save();
      ctx.strokeStyle = "#473c28";
      ctx.lineWidth = 1;
      ctx.setLineDash([3, 3]);
      ctx.beginPath();
      if (fullMode) {
        const thrY = Math.round(baseBottom - thrOffset) + 0.5;
        ctx.moveTo(0, thrY);
        ctx.lineTo(w, thrY);
      } else {
        const thrYTop = Math.round(midY - thrOffset) + 0.5;
        const thrYBot = Math.round(midY + thrOffset) + 0.5;
        ctx.moveTo(0, thrYTop);
        ctx.lineTo(w, thrYTop);
        ctx.moveTo(0, thrYBot);
        ctx.lineTo(w, thrYBot);
      }
      ctx.stroke();
      ctx.restore();
    }

    // 离散实心柱步进（单色相双明度 Peak 外包络 + 完全不透明 RMS 内核）
    const stepPx = 2;
    const barW = 1.3;
    for (let px = 0; px < w; px += stepPx) {
      const t0 = this._trackXToTime(px, w);
      const t1 = this._trackXToTime(px + stepPx, w);
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
        ctx.fillStyle = muted ? "#23252b" : "#1c3936";
        if (fullMode) ctx.fillRect(px, baseBottom - pkH, barW, pkH);
        else ctx.fillRect(px, midY - pkH, barW, pkH * 2);
      }
      if (rmH > 0.5) {
        const hot = thr > 0 && rm >= thr;
        ctx.fillStyle = muted ? "#3e414a" : hot ? "#d8962e" : "#359c8e";
        if (fullMode) ctx.fillRect(px, baseBottom - rmH, barW, rmH);
        else ctx.fillRect(px, midY - rmH, barW, rmH * 2);
      }
    }

    // 叠加弹幕折线与 Top 爆点锚点
    this._drawChatRadarInTrack(ctx, w, topY, trackH);
  }

  _drawTracks() {
    const canvas = this.tracksCanvas;
    const ctx = this.tracksCtx;
    if (!canvas || !ctx) return;
    const { w, h, physW, physH, scaleX, scaleY } = this._getCanvasMetrics(canvas);
    if (w < 20 || h < 20) return;

    if (canvas.width !== physW || canvas.height !== physH) {
      canvas.width = physW;
      canvas.height = physH;
    }

    ctx.setTransform(scaleX, 0, 0, scaleY, 0, 0);
    ctx.fillStyle = "#131417";
    ctx.fillRect(0, 0, w, h);

    const lay = this._getTrackLayout(h);
    const span = Math.max(0.1, this.viewEnd - this.viewStart);

    /* ---------- 1. 标尺底条与三条轨道底色 + 黑分界线（先铺底，防止网格线与章节投射线被遮挡） ---------- */
    ctx.fillStyle = "#1b1b1f";
    ctx.fillRect(0, 0, w, lay.hRuler);
    ctx.fillStyle = "#09090a";
    ctx.fillRect(0, lay.hRuler - 1, w, 1);

    if (lay.hV1 > 0) {
      ctx.fillStyle = "#141518";
      ctx.fillRect(0, lay.topV1, w, lay.hV1);
      ctx.fillStyle = "#09090a";
      ctx.fillRect(0, lay.topV1 + lay.hV1 - 1, w, 1);
    }

    if (lay.hA1 > 0) {
      ctx.fillStyle = "#0e0f12";
      ctx.fillRect(0, lay.topA1, w, lay.hA1);
      ctx.fillStyle = "#09090a";
      ctx.fillRect(0, lay.topA1 + lay.hA1 - 1, w, 1);
    }

    if (lay.hT1 > 0) {
      ctx.fillStyle = "#131417";
      ctx.fillRect(0, lay.topT1, w, lay.hT1);
      ctx.fillStyle = "#09090a";
      ctx.fillRect(0, lay.topT1 + lay.hT1 - 1, w, 1);
    }

    const tracksBottom = lay.topT1 + lay.hT1;
    if (tracksBottom < h) {
      ctx.fillStyle = "#101114";
      ctx.fillRect(0, tracksBottom, w, h - tracksBottom);
    }

    /* ---------- 2. Timecode Ruler 刻度与全轨纵向参考竖线 ---------- */
    const majorStep = chooseRulerStep(span, w);
    const minorStep = majorStep / 5;
    const firstMinor = Math.floor(this.viewStart / minorStep) * minorStep;

    ctx.font = `11.5px ${MONO_FONT_FAMILY}`;
    ctx.textAlign = "left";
    ctx.textBaseline = "alphabetic";
    for (let t = firstMinor; t <= this.viewEnd + minorStep; t += minorStep) {
      if (t < 0) continue;
      const tx = Math.round(this._timeToTrackX(t, w));
      if (tx < -40 || tx > w + 40) continue;
      const isMajor = Math.abs(t / majorStep - Math.round(t / majorStep)) < 0.01;
      if (isMajor) {
        ctx.fillStyle = "#484852";
        ctx.fillRect(tx, 12, 1, lay.hRuler - 13);
        ctx.fillStyle = "#8a8a96";
        ctx.fillText(formatRulerClock(t), tx + 4, 14);
        // 极淡纵向参考网格线（穿透 V1/A1/T1 底色）
        ctx.fillStyle = "#1c1d22";
        ctx.fillRect(tx, lay.hRuler, 1, h - lay.hRuler);
      } else {
        ctx.fillStyle = "#2d2d34";
        ctx.fillRect(tx, 18, 1, lay.hRuler - 19);
      }
    }

    /* ---------- 2b. OBS 章节投射虚线与标尺底部菱形标记 ---------- */
    if (this.chapters.length > 0) {
      for (const ch of this.chapters) {
        const cs = Number(ch.start_sec) || 0;
        if (cs < this.viewStart || cs > this.viewEnd) continue;
        const cx = Math.round(this._timeToTrackX(cs, w)) + 0.5;
        const isHov = this._hoverInfo?.zone === "chapter" && this._hoverInfo.chapter === ch;

        // 向下投射参考虚线
        ctx.save();
        ctx.strokeStyle = isHov ? "rgba(102, 209, 160, 0.55)" : "rgba(62, 168, 120, 0.24)";
        ctx.lineWidth = 1;
        ctx.setLineDash([2, 3]);
        ctx.beginPath();
        ctx.moveTo(cx, lay.hRuler);
        ctx.lineTo(cx, h);
        ctx.stroke();
        ctx.restore();

        // 标尺底部菱形
        const cy = lay.hRuler - 6;
        const rad = isHov ? 5.2 : 4.2;
        ctx.beginPath();
        ctx.moveTo(cx, cy - rad);
        ctx.lineTo(cx + rad, cy);
        ctx.lineTo(cx, cy + rad);
        ctx.lineTo(cx - rad, cy);
        ctx.closePath();
        ctx.fillStyle = isHov ? "#66d1a0" : "#38b27c";
        ctx.fill();
        ctx.strokeStyle = "#0b2419";
        ctx.lineWidth = 1;
        ctx.stroke();
      }
    }

    /* ---------- 3. V1 高光切片轨（100% 实心哑光硬件质感） ---------- */
    if (lay.hV1 > 0) {
      for (const item of this.highlights) {
        const s = Number(item.start_sec) || 0;
        const e = Number(item.end_sec) || s + 1;
        if (e < this.viewStart || s > this.viewEnd) continue;

        const x1 = this._timeToTrackX(s, w);
        const x2 = this._timeToTrackX(e, w);
        const drawX = Math.round(Math.max(0, x1));
        const drawW = Math.max(3, Math.round(Math.min(w, x2)) - drawX);
        const y = lay.topV1 + 5;
        const bh = Math.max(16, lay.hV1 - 11);

        const isSelected = item.selected !== false;
        const isReviewed = String(item.review_status || "draft").toLowerCase() === "reviewed";
        const isActive = this.activeClipRank !== null && Number(item.rank) === Number(this.activeClipRank);
        const isHovered = this._hoverInfo?.zone === "v1-clip" && this._hoverInfo.clip === item;

        // 实心块底
        if (isActive) {
          ctx.fillStyle = isReviewed ? "#22362c" : "#2b313c";
        } else if (!isSelected) {
          ctx.fillStyle = "#1d1e23";
        } else if (isReviewed) {
          ctx.fillStyle = "#1a2b23";
        } else {
          ctx.fillStyle = "#202934";
        }
        ctx.fillRect(drawX, y, drawW, bh);

        // 顶部语义色冠 + 内嵌/激活外框（互不压线冲淡）
        if (isActive) {
          const capColor = isReviewed ? "#38b866" : "#df9d36";
          ctx.fillStyle = capColor;
          ctx.fillRect(drawX + 1.5, y + 1.5, Math.max(1, drawW - 3), 3);

          ctx.strokeStyle = "#eef0f5";
          ctx.lineWidth = 1.5;
          ctx.strokeRect(drawX + 0.75, y + 0.75, Math.max(2, drawW - 1.5), bh - 1.5);
        } else {
          const borderColor = !isSelected
            ? (isHovered ? "#4f5361" : "#2d2f38")
            : isReviewed
              ? (isHovered ? "#3e6e56" : "#284738")
              : (isHovered ? "#4a627d" : "#314152");
          ctx.strokeStyle = borderColor;
          ctx.lineWidth = 1;
          ctx.strokeRect(drawX + 0.5, y + 0.5, Math.max(2, drawW - 1), bh - 1);

          const capH = isSelected ? 3 : 2.5;
          ctx.fillStyle = !isSelected ? "#40434d" : isReviewed ? "#2fa15a" : "#4b7ea8";
          ctx.fillRect(drawX, y, drawW, capH);
        }

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

        // 块内信息标签（空间足够且开启名称显示时采用二分省略号截断绘制）
        if (this.viewOptions.showClipNames && drawW >= 34) {
          const textPadX = 6;
          const maxTextW = drawW - textPadX * 2;
          if (maxTextW >= 12) {
            const textX = Math.round(drawX + textPadX);
            ctx.save();
            ctx.beginPath();
            ctx.rect(drawX + 2, y + 2, drawW - 4, bh - 4);
            ctx.clip();

            ctx.font = `600 12.5px ${UI_FONT_FAMILY}`;
            ctx.fillStyle = isActive ? "#f4f6fa" : isSelected ? "#eaedf2" : "#858994";
            ctx.textAlign = "left";
            const labelPrefix = isReviewed ? "✓ " : "";
            const rawLabel = `${labelPrefix}#${item.rank} ${item.title_tgt || item.title_zh || item.title_src || item.title_en || ""}`;
            const label = fitTextEllipsis(ctx, rawLabel, maxTextW);

            if (bh < 40) {
              ctx.textBaseline = "middle";
              ctx.fillText(label, textX, Math.round(y + bh / 2));
            } else {
              ctx.textBaseline = "alphabetic";
              ctx.fillText(label, textX, Math.round(y + 18));

              if (drawW >= 95) {
                ctx.font = `11.5px ${MONO_FONT_FAMILY}`;
                ctx.fillStyle = isActive || isSelected ? "#9ca2b0" : "#6c707b";
                const dur = Math.max(0, e - s).toFixed(1);
                const hasScore =
                  item.score !== null &&
                  item.score !== undefined &&
                  item.score !== "" &&
                  Number.isFinite(Number(item.score));
                let metaText = "";
                if (hasScore) {
                  const scoreStr = Number(item.score).toFixed(1);
                  metaText = window.SolacutI18n
                    ? window.SolacutI18n.t("tl.scoreDur", { score: scoreStr, dur })
                    : `${scoreStr} pts · ${dur}s`;
                } else {
                  const srcRaw = String(item.source || "").trim().toLowerCase();
                  const srcTag =
                    srcRaw === "obs_chapter"
                      ? "OBS"
                      : srcRaw === "chat_peak"
                        ? "CHAT"
                        : srcRaw === "transcript"
                          ? "TR"
                          : srcRaw === "manual"
                            ? "MAN"
                            : "—";
                  metaText = window.SolacutI18n
                    ? window.SolacutI18n.t("tl.srcDur", { src: srcTag, dur })
                    : `${srcTag} · ${dur}s`;
                }
                const fittedMeta = fitTextEllipsis(ctx, metaText, maxTextW);
                ctx.fillText(fittedMeta, textX, Math.round(y + 34));
              }
            }
            ctx.restore();
          }
        }
      }
    }

    /* ---------- 4. A1 音频波形与弹幕轨 ---------- */
    if (lay.hA1 > 0) {
      this._drawWaveformInTrack(ctx, w, lay.topA1, lay.hA1);
    }

    /* ---------- 5. T1 双语字幕轨（实心炭钢卡片 + 1.5px 黑缝防粘连 + 垂直居中） ---------- */
    if (lay.hT1 > 0) {
      const subY = lay.topT1 + 5;
      const subH = Math.max(22, lay.hT1 - 10);

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
          const drawW = Math.max(2, Math.min(w, x2) - drawX - 1.5);
          const key = `${item.rank}:${idx}`;
          const isCur = key === this.activeSubKey;
          const isHov = this._hoverInfo?.zone === "t1-sub" && this._hoverInfo.sub === s;

          ctx.fillStyle = isCur ? "#2e333f" : isHov ? "#2a2e38" : "#22252d";
          ctx.fillRect(drawX, subY, drawW, subH);

          ctx.strokeStyle = isCur ? "#d8962e" : isHov ? "#525868" : "#353945";
          ctx.lineWidth = 1;
          ctx.strokeRect(
            Math.round(drawX) + 0.5,
            subY + 0.5,
            Math.max(1.5, Math.round(drawW) - 1),
            subH - 1
          );

          if (isCur) {
            ctx.fillStyle = "#d8962e";
            ctx.fillRect(drawX, subY, Math.min(3, drawW), subH);
          }

          if (drawW >= 24) {
            const textPadX = 6;
            const maxTextW = drawW - textPadX * 2;
            if (maxTextW >= 12) {
              const rawSub = (s.text_tgt || s.text_zh || s.text_src || s.text_en || "").trim();
              if (rawSub) {
                ctx.save();
                ctx.beginPath();
                ctx.rect(drawX + 2, subY + 1, drawW - 4, subH - 2);
                ctx.clip();
                ctx.font = `12.5px ${UI_FONT_FAMILY}`;
                ctx.fillStyle = isCur ? "#ffffff" : "#e0e3eb";
                ctx.textAlign = "left";
                ctx.textBaseline = "middle";
                const fittedSub = fitTextEllipsis(ctx, rawSub, maxTextW);
                ctx.fillText(fittedSub, Math.round(drawX + textPadX), Math.round(subY + subH / 2));
                ctx.restore();
              }
            }
          }
        }
      }
    }

    /* ---------- 6. 磁吸金色辅助竖线 ---------- */
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

    /* ---------- 7. 剃刀悬停虚线刀标 ---------- */
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

    /* ---------- 8. 全轨达芬奇信号红播放头 ---------- */
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

    /* ---------- 9. 拖拽 Trim 浮动实时时码提示 ---------- */
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
      const boxW = 82;
      const boxH = 21;
      const tx = Math.round(
        clamp(this._timeToTrackX(tSec, w), boxW / 2 + 4, Math.max(boxW / 2 + 4, w - boxW / 2 - 4))
      );
      const bx = Math.round(tx - boxW / 2);
      const by = lay.hRuler + 3;
      const label = formatTimecode(tSec, true);
      ctx.save();
      ctx.fillStyle = "#141519";
      ctx.fillRect(bx, by, boxW, boxH);
      ctx.strokeStyle = "#d99b38";
      ctx.lineWidth = 1;
      ctx.strokeRect(bx + 0.5, by + 0.5, boxW - 1, boxH - 1);
      ctx.fillStyle = "#f0ba59";
      ctx.font = `600 12px ${MONO_FONT_FAMILY}`;
      ctx.textAlign = "center";
      ctx.textBaseline = "middle";
      ctx.fillText(label, tx, Math.round(by + boxH / 2));
      ctx.restore();
    }

    /* ---------- 10. 悬停 OBS 章节 / 弹幕爆点 暗房浮动 Tooltip ---------- */
    if (!this._trkDrag && (this._hoverInfo?.zone === "chapter" || this._hoverInfo?.zone === "chat-peak")) {
      let tipText = "";
      let anchorSec = 0;
      let accentColor = "#38b27c";
      let tipY = lay.hRuler + 4;
      if (this._hoverInfo.zone === "chapter") {
        const ch = this._hoverInfo.chapter;
        anchorSec = Number(ch.start_sec) || 0;
        tipText = `◆ ${ch.title || `Chapter ${ch.index}`} · ${formatRulerClock(anchorSec)}`;
        accentColor = "#38b27c";
        tipY = lay.hRuler + 4;
      } else if (this._hoverInfo.zone === "chat-peak") {
        const cp = this._hoverInfo.peak;
        anchorSec = Number(cp.peak_sec ?? cp.start_sec) || 0;
        const terms = Array.isArray(cp.top_terms) && cp.top_terms.length ? ` · ${cp.top_terms.slice(0, 3).join(", ")}` : "";
        tipText = `⚡ ${cp.msg_count || 0} msg (z=${Number(cp.z_score || 0).toFixed(1)})${terms} · ${formatRulerClock(anchorSec)}`;
        accentColor = "#7cb8e6";
        tipY = lay.topA1 + 16;
      }
      if (tipText) {
        ctx.save();
        ctx.font = `500 12px ${UI_FONT_FAMILY}`;
        const padX = 10;
        const metrics = ctx.measureText(tipText);
        const boxW = Math.min(w - 12, Math.ceil(metrics.width) + padX * 2);
        const boxH = 24;
        const rawX = this._timeToTrackX(anchorSec, w) - boxW / 2;
        const bx = Math.round(clamp(rawX, 6, Math.max(6, w - boxW - 6)));
        const by = Math.round(clamp(tipY, lay.hRuler + 2, Math.max(lay.hRuler + 2, h - boxH - 4)));
        ctx.fillStyle = "#121318";
        ctx.fillRect(bx, by, boxW, boxH);
        ctx.strokeStyle = accentColor;
        ctx.lineWidth = 1;
        ctx.strokeRect(bx + 0.5, by + 0.5, boxW - 1, boxH - 1);
        ctx.fillStyle = "#ececf0";
        ctx.textAlign = "left";
        ctx.textBaseline = "middle";
        const fittedTip = fitTextEllipsis(ctx, tipText, boxW - padX * 2);
        ctx.fillText(fittedTip, bx + padX, Math.round(by + boxH / 2));
        ctx.restore();
      }
    }
  }
}
