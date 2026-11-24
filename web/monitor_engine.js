/* ClipStudio NLE 自研 60fps 专业节目监视器引擎（零外部依赖）
 * 提供：
 * 1. SMPTE 精确时码（HH:MM:SS:FF，基于真实 FPS）与单击直输跳帧；
 * 2. requestVideoFrameCallback 60fps 帧级同步（告别 250ms 滞后的 timeupdate）；
 * 3. Web Audio API L/R 双声道分段 LED dBFS 实时电平表（含 Peak Hold 与 0dB Clip 红灯）；
 * 4. 当前片段局部微型进度槽（Clip Mini-Scrubber：入出点区间 + 字幕句分布 + 红线 Scrubbing）；
 * 5. 9:16 竖屏裁切安全框（含顶部标题区预演）与画布缩放控制。
 */

export function formatSmpte(sec, fps = 30) {
  const safeFps = Math.max(1, Math.min(240, Math.round(Number(fps) || 30)));
  const safe = Math.max(0, Number(sec) || 0);
  const totalFrames = Math.floor(safe * safeFps + 1e-6);
  const ff = totalFrames % safeFps;
  const totalSec = Math.floor(totalFrames / safeFps);
  const ss = totalSec % 60;
  const mm = Math.floor((totalSec % 3600) / 60);
  const hh = Math.floor(totalSec / 3600);
  const p = (n) => String(n).padStart(2, "0");
  return `${p(hh)}:${p(mm)}:${p(ss)}:${p(ff)}`;
}

export function parseSmpteOrTimecode(raw, fps = 30) {
  const s = String(raw || "").trim();
  if (!s) return null;
  if (/^\d+(\.\d+)?$/.test(s)) return Number(s);
  const parts = s.split(":").map((x) => Number(x));
  if (parts.some((n) => Number.isNaN(n) || n < 0)) return null;
  const safeFps = Math.max(1, Math.round(Number(fps) || 30));
  if (parts.length === 4) {
    // 严格 SMPTE 进制校验（分 < 60，秒 < 60，帧 < fps）
    if (parts[1] >= 60 || parts[2] >= 60 || parts[3] >= safeFps) return null;
    return parts[0] * 3600 + parts[1] * 60 + parts[2] + parts[3] / safeFps;
  }
  if (parts.length === 3) {
    if (parts[1] >= 60 || parts[2] >= 60) return null;
    return parts[0] * 3600 + parts[1] * 60 + parts[2];
  }
  if (parts.length === 2) {
    if (parts[1] >= 60) return null;
    return parts[0] * 60 + parts[1];
  }
  return null;
}

/* ==================== 1. Web Audio 立体声 dBFS 实时电平表 ==================== */
class StereoAudioMeter {
  constructor(videoEl, canvasEl) {
    this.videoEl = videoEl;
    this.canvasEl = canvasEl;
    this.ctx = canvasEl ? canvasEl.getContext("2d") : null;

    this.audioCtx = null;
    this.sourceNode = null;
    this.analyserL = null;
    this.analyserR = null;
    this.bufL = null;
    this.bufR = null;

    this.levelL = 0;
    this.levelR = 0;
    this.peakL = 0;
    this.peakR = 0;
    this.peakHoldUntilL = 0;
    this.peakHoldUntilR = 0;
    this.clippedL = false;
    this.clippedR = false;
    this.rafId = null;

    if (this.canvasEl) {
      this.canvasEl.addEventListener("click", () => {
        this.clippedL = false;
        this.clippedR = false;
        this.draw();
      });
      this._resizeObserver = new ResizeObserver(() => this.draw());
      this._resizeObserver.observe(this.canvasEl);
    }
    this.draw();
  }

  ensureAudioGraph() {
    if (this.audioCtx || !this.videoEl) {
      if (this.audioCtx && this.audioCtx.state === "suspended") {
        this.audioCtx.resume().catch(() => {});
      }
      return;
    }
    const AC = window.AudioContext || window.webkitAudioContext;
    if (!AC) return;
    try {
      const ctx = new AC();
      const src = ctx.createMediaElementSource(this.videoEl);
      const splitter = ctx.createChannelSplitter(2);
      const anL = ctx.createAnalyser();
      const anR = ctx.createAnalyser();
      anL.fftSize = 1024;
      anR.fftSize = 1024;
      anL.smoothingTimeConstant = 0.25;
      anR.smoothingTimeConstant = 0.25;

      // 主声道直通扬声器，同时旁路送入双声道分析仪（绝不干扰原音播放）
      src.connect(ctx.destination);
      src.connect(splitter);
      splitter.connect(anL, 0);
      splitter.connect(anR, 1);

      this.audioCtx = ctx;
      this.sourceNode = src;
      this.analyserL = anL;
      this.analyserR = anR;
      this.bufL = new Float32Array(anL.fftSize);
      this.bufR = new Float32Array(anR.fftSize);
    } catch {
      // 某些受限环境无法建 AudioContext 时静默退化，不影响播放
    }
  }

  start() {
    this.ensureAudioGraph();
    if (this.rafId != null) return;
    const tick = (now) => {
      const active = this._sampleChannelLevels(now);
      this.draw();
      if (active || !this.videoEl.paused) {
        this.rafId = requestAnimationFrame(tick);
      } else {
        this.rafId = null;
      }
    };
    this.rafId = requestAnimationFrame(tick);
  }

  _measureNorm(analyser, buf) {
    if (!analyser || !buf) return { norm: 0, clip: false };
    analyser.getFloatTimeDomainData(buf);
    let maxAbs = 0;
    let sumSq = 0;
    for (let i = 0; i < buf.length; i += 1) {
      const v = Math.abs(buf[i]);
      if (v > maxAbs) maxAbs = v;
      sumSq += v * v;
    }
    const rms = Math.sqrt(sumSq / buf.length);
    const val = Math.max(rms * 1.35, maxAbs * 0.78);
    if (val <= 0.0005) return { norm: 0, clip: false };
    const db = 20 * Math.log10(val);
    const norm = Math.max(0, Math.min(1, (db + 60) / 60));
    return { norm, clip: maxAbs >= 0.992 };
  }

  _sampleChannelLevels(now) {
    const isPlaying = this.videoEl && !this.videoEl.paused && !this.videoEl.muted && this.videoEl.volume > 0;
    const mL = isPlaying ? this._measureNorm(this.analyserL, this.bufL) : { norm: 0, clip: false };
    const mR = isPlaying ? this._measureNorm(this.analyserR, this.bufR) : { norm: 0, clip: false };

    const decay = 0.065;
    this.levelL = mL.norm >= this.levelL ? mL.norm : Math.max(0, this.levelL - decay);
    this.levelR = mR.norm >= this.levelR ? mR.norm : Math.max(0, this.levelR - decay);

    if (this.levelL >= this.peakL) {
      this.peakL = this.levelL;
      this.peakHoldUntilL = now + 480;
    } else if (now > this.peakHoldUntilL) {
      this.peakL = Math.max(this.levelL, this.peakL - 0.018);
    }

    if (this.levelR >= this.peakR) {
      this.peakR = this.levelR;
      this.peakHoldUntilR = now + 480;
    } else if (now > this.peakHoldUntilR) {
      this.peakR = Math.max(this.levelR, this.peakR - 0.018);
    }

    if (mL.clip) this.clippedL = true;
    if (mR.clip) this.clippedR = true;

    return this.levelL > 0.003 || this.levelR > 0.003 || this.peakL > 0.003 || this.peakR > 0.003;
  }

  draw() {
    const canvas = this.canvasEl;
    const ctx = this.ctx;
    if (!canvas || !ctx) return;

    const rect = canvas.getBoundingClientRect();
    if (rect.width < 4 || rect.height < 20) return;
    const w = Math.round(rect.width);
    const h = Math.round(rect.height);
    const dpr = Math.min(2, window.devicePixelRatio || 1);

    if (canvas.width !== Math.round(w * dpr) || canvas.height !== Math.round(h * dpr)) {
      canvas.width = Math.round(w * dpr);
      canvas.height = Math.round(h * dpr);
    }

    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, w, h);
    ctx.fillStyle = "#0e0e10";
    ctx.fillRect(0, 0, w, h);

    const padX = 4;
    const clipH = 5;
    const topY = 4 + clipH + 3;
    const bottomPad = 14;
    const meterH = Math.max(24, h - topY - bottomPad);
    const gap = 2;
    const barW = Math.max(4, Math.floor((w - padX * 2 - gap) / 2));
    const xL = padX;
    const xR = padX + barW + gap;

    // 顶部 0dB Clip 指示灯
    ctx.fillStyle = this.clippedL ? "#df4b42" : "#222226";
    ctx.fillRect(xL, 4, barW, clipH);
    ctx.fillStyle = this.clippedR ? "#df4b42" : "#222226";
    ctx.fillRect(xR, 4, barW, clipH);

    // 26 段离散 LED（低开销、清晰广播硬件质感）
    const segCount = Math.max(14, Math.min(32, Math.floor(meterH / 5)));
    const segGap = 1.5;
    const segH = (meterH - segGap * (segCount - 1)) / segCount;

    const drawChannel = (x, level, peak) => {
      for (let i = 0; i < segCount; i += 1) {
        const frac = (i + 1) / segCount; // 0=底(-60dB), 1=顶(0dB)
        const y = topY + meterH - (i + 1) * segH - i * segGap;
        const lit = level >= frac - 0.5 / segCount;

        if (frac >= 0.94) {
          ctx.fillStyle = lit ? "#df4b42" : "#251919";
        } else if (frac >= 0.78) {
          ctx.fillStyle = lit ? "#d99b38" : "#262119";
        } else {
          ctx.fillStyle = lit ? "#37a855" : "#18211b";
        }
        ctx.fillRect(x, y, barW, Math.max(1.5, segH));
      }

      // Peak Hold 横线
      if (peak > 0.01) {
        const py = topY + meterH * (1 - peak);
        ctx.fillStyle = peak >= 0.94 ? "#ff685e" : peak >= 0.78 ? "#f0ba59" : "#d8dce2";
        ctx.fillRect(x, Math.max(topY, Math.min(topY + meterH - 1.5, py)), barW, 1.5);
      }
    };

    drawChannel(xL, this.levelL, this.peakL);
    drawChannel(xR, this.levelR, this.peakR);

    // 广播级 dB 刻度参考线（-6 / -12 / -24 / -48 dB）
    ctx.fillStyle = "rgba(255, 255, 255, 0.22)";
    for (const db of [-6, -12, -24, -48]) {
      const frac = (db + 60) / 60;
      const ly = Math.round(topY + meterH * (1 - frac));
      ctx.fillRect(xL + barW, ly, gap, 1);
    }

    // 底部 L / R 标识
    ctx.fillStyle = "#6b6b74";
    ctx.font = '600 9px "Cascadia Mono", Consolas, monospace';
    ctx.textAlign = "center";
    ctx.fillText("L", xL + barW / 2, h - 3);
    ctx.fillText("R", xR + barW / 2, h - 3);
  }
}

/* ==================== 2. 局部片段微型进度槽（Clip Mini-Scrubber） ==================== */
class ClipMiniScrubber {
  constructor(canvasEl, { onSeek }) {
    this.canvasEl = canvasEl;
    this.ctx = canvasEl ? canvasEl.getContext("2d") : null;
    this.onSeek = onSeek;

    this.viewStart = 0;
    this.viewEnd = 60;
    this.clipStart = null;
    this.clipEnd = null;
    this.subtitles = [];
    this.currentTime = 0;
    this.duration = 0;
    this.hoverSec = null;
    this._dragging = false;

    if (this.canvasEl) {
      this._bindPointer();
      this._ro = new ResizeObserver(() => this.draw());
      this._ro.observe(this.canvasEl);
    }
  }

  setContext({ duration = 0, currentTime = 0, clip = null }) {
    this.duration = Math.max(0, Number(duration) || 0);
    this.currentTime = Math.max(0, Number(currentTime) || 0);

    if (clip && Number(clip.end_sec) > Number(clip.start_sec)) {
      const s = Number(clip.start_sec);
      const e = Number(clip.end_sec);
      const pad = Math.min(6, Math.max(2, (e - s) * 0.1));
      this.clipStart = s;
      this.clipEnd = e;
      this.viewStart = Math.max(0, s - pad);
      this.viewEnd = this.duration > 0 ? Math.min(this.duration, e + pad) : e + pad;
      this.subtitles = Array.isArray(clip.subtitles) ? clip.subtitles : [];
    } else {
      this.clipStart = null;
      this.clipEnd = null;
      this.viewStart = 0;
      this.viewEnd = Math.max(1, this.duration || 60);
      this.subtitles = [];
    }
    this.draw();
  }

  setTime(sec) {
    this.currentTime = Math.max(0, Number(sec) || 0);
    this.draw();
  }

  _xToTime(clientX) {
    const r = this.canvasEl.getBoundingClientRect();
    const frac = Math.max(0, Math.min(1, (clientX - r.left) / Math.max(1, r.width)));
    return this.viewStart + frac * Math.max(0.01, this.viewEnd - this.viewStart);
  }

  _timeToX(sec, width) {
    const span = Math.max(0.01, this.viewEnd - this.viewStart);
    return ((sec - this.viewStart) / span) * width;
  }

  _bindPointer() {
    const el = this.canvasEl;
    el.addEventListener("pointerdown", (e) => {
      if (e.button !== 0) return;
      e.preventDefault();
      this._dragging = true;
      el.setPointerCapture(e.pointerId);
      const t = this._xToTime(e.clientX);
      this.currentTime = t;
      this.onSeek?.(t, true);
      this.draw();
    });
    el.addEventListener("pointermove", (e) => {
      const t = this._xToTime(e.clientX);
      this.hoverSec = t;
      if (this._dragging) {
        this.currentTime = t;
        this.onSeek?.(t, true);
      }
      this.draw();
    });
    const endDrag = () => {
      if (!this._dragging) return;
      this._dragging = false;
      this.onSeek?.(this.currentTime, false);
    };
    el.addEventListener("pointerup", endDrag);
    el.addEventListener("pointercancel", endDrag);
    el.addEventListener("pointerleave", () => {
      this.hoverSec = null;
      this.draw();
    });
  }

  draw() {
    const canvas = this.canvasEl;
    const ctx = this.ctx;
    if (!canvas || !ctx) return;

    const r = canvas.getBoundingClientRect();
    // 修复 P2-06：面板隐藏（0×0）时跳过离屏重分配
    if (r.width < 10 || r.height < 8) return;
    const w = Math.round(r.width);
    const h = Math.round(r.height);
    const dpr = Math.min(2, window.devicePixelRatio || 1);

    if (canvas.width !== Math.round(w * dpr) || canvas.height !== Math.round(h * dpr)) {
      canvas.width = Math.round(w * dpr);
      canvas.height = Math.round(h * dpr);
    }

    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, w, h);

    // 凹槽底色
    ctx.fillStyle = "#101012";
    ctx.fillRect(0, 0, w, h);

    // 顶部极细刻度标点
    const span = Math.max(0.1, this.viewEnd - this.viewStart);
    const tickStep = span <= 30 ? 2 : span <= 120 ? 10 : span <= 600 ? 30 : 300;
    const firstTick = Math.ceil(this.viewStart / tickStep) * tickStep;
    ctx.fillStyle = "#2b2b31";
    for (let t = firstTick; t < this.viewEnd; t += tickStep) {
      const tx = Math.round(this._timeToX(t, w));
      ctx.fillRect(tx, 0, 1, 4);
    }

    // 轨道中槽
    const trackY = 7;
    const trackH = h - 11;
    ctx.fillStyle = "#1b1b1f";
    ctx.fillRect(0, trackY, w, trackH);

    // 若聚焦片段，绘制入出点高亮区
    if (this.clipStart !== null && this.clipEnd !== null) {
      const x1 = Math.max(0, this._timeToX(this.clipStart, w));
      const x2 = Math.min(w, this._timeToX(this.clipEnd, w));
      ctx.fillStyle = "rgba(217, 155, 56, 0.16)";
      ctx.fillRect(x1, trackY, Math.max(2, x2 - x1), trackH);

      // 本段每句字幕分布微块
      ctx.fillStyle = "rgba(180, 196, 220, 0.34)";
      for (const s of this.subtitles) {
        const sx1 = Math.max(0, this._timeToX(Number(s.start_sec) || 0, w));
        const sx2 = Math.min(w, this._timeToX(Number(s.end_sec) || 0, w));
        if (sx2 > sx1) {
          ctx.fillRect(sx1, trackY + trackH - 3, Math.max(1.5, sx2 - sx1 - 0.8), 2.5);
        }
      }

      // 入/出点琥珀立柱
      ctx.fillStyle = "#d99b38";
      ctx.fillRect(Math.round(x1), trackY - 1, 1.5, trackH + 2);
      ctx.fillRect(Math.round(x2) - 1, trackY - 1, 1.5, trackH + 2);
    } else {
      // 全片已播进度条
      const px = Math.max(0, Math.min(w, this._timeToX(this.currentTime, w)));
      ctx.fillStyle = "#3a3a44";
      ctx.fillRect(0, trackY, px, trackH);
    }

    // 悬停预览微线
    if (this.hoverSec !== null) {
      const hx = Math.round(this._timeToX(this.hoverSec, w));
      ctx.fillStyle = "rgba(255, 255, 255, 0.25)";
      ctx.fillRect(hx, 0, 1, h);
    }

    // 达芬奇信号红播放头竖线 + 顶部小三角
    const playX = this._timeToX(this.currentTime, w);
    if (playX >= -2 && playX <= w + 2) {
      const cx = Math.max(1, Math.min(w - 1, playX));
      ctx.fillStyle = "#df4b42";
      ctx.beginPath();
      ctx.moveTo(cx - 4, 0);
      ctx.lineTo(cx + 4, 0);
      ctx.lineTo(cx, 5);
      ctx.closePath();
      ctx.fill();
      ctx.fillRect(Math.round(cx) - 0.5, 4, 1.5, h - 4);
    }
  }
}

/* ==================== 3. 节目监视器总控制器 ==================== */
export class ProgramMonitorEngine {
  constructor({
    videoEl,
    frameEl,
    meterCanvas,
    scrubberCanvas,
    timecodeBtn,
    durationEl,
    techBadgeEl,
    safeGuideEl,
    zoomSelectEl,
    safeToggleEl,
    safeModeSelectEl,
    playToggleBtn,
    muteBtn,
    dimBtn,
    cinemaBtn,
    volumeSlider,
    onFrameTick,
    onSeekRequest,
    onPlayStateChange,
    onStageContextMenu,
    onTimecodeContextMenu,
  }) {
    this.videoEl = videoEl;
    this.frameEl = frameEl;
    this.timecodeBtn = timecodeBtn;
    this.durationEl = durationEl;
    this.techBadgeEl = techBadgeEl;
    this.safeGuideEl = safeGuideEl;
    this.zoomSelectEl = zoomSelectEl;
    this.safeToggleEl = safeToggleEl;
    this.safeModeSelectEl = safeModeSelectEl;
    this.playToggleBtn = playToggleBtn;
    this.muteBtn = muteBtn;
    this.dimBtn = dimBtn;
    this.cinemaBtn = cinemaBtn;
    this.volumeSlider = volumeSlider;
    this.onFrameTick = onFrameTick;
    this.onSeekRequest = onSeekRequest;
    this.onPlayStateChange = onPlayStateChange;
    this.onStageContextMenu = onStageContextMenu;
    this.onTimecodeContextMenu = onTimecodeContextMenu;

    this.fps = 30;
    this.zoomMode = "fit";
    this.showSafe916 = false;
    this.safeMode = "9:16"; // '9:16' | '4:5' | '1:1' | 'title'
    this.dimmed = false;
    this.userVolume = 1;
    this.cinemaActive = false;
    this.activeClip = null;
    this._vfcId = null;
    this._rafId = null;
    this._editingTc = false;
    this._lastVol = 1;
    this._internalVolChange = false;

    this.meter = new StereoAudioMeter(videoEl, meterCanvas);
    this.isScrubbing = false;
    this.scrubber = new ClipMiniScrubber(scrubberCanvas, {
      onSeek: (sec, isDragging) => {
        this.isScrubbing = Boolean(isDragging);
        if (this.videoEl && this.videoEl.src) {
          const clamped = Math.max(0, Math.min(this.videoEl.duration || sec, sec));
          this.videoEl.currentTime = clamped;
          this.onSeekRequest?.(clamped, Boolean(isDragging));
          this.refreshFrameUI();
        }
      },
    });

    this._bindEvents();
    this._syncVolumeSlider();
    this.syncSafeGuideLayout();
  }

  setMediaInfo(info) {
    this.fps = Math.max(1, Number(info?.fps) || 30);
    if (this.techBadgeEl) {
      if (info && info.width && info.height) {
        const fpsLabel = Math.abs(this.fps - Math.round(this.fps)) < 0.05
          ? String(Math.round(this.fps))
          : this.fps.toFixed(2);
        this.techBadgeEl.textContent = `${info.height}p · ${fpsLabel}fps`;
        this.techBadgeEl.title = `${info.width}×${info.height} · ${info.video_codec || ""} · ${fpsLabel}fps`;
        this.techBadgeEl.classList.remove("hidden");
      } else {
        this.techBadgeEl.textContent = "";
        this.techBadgeEl.classList.add("hidden");
      }
    }
    this.refreshFrameUI();
    this.syncSafeGuideLayout();
  }

  setActiveClip(clip) {
    this.activeClip = clip || null;
    this.scrubber.setContext({
      duration: this.videoEl?.duration || 0,
      currentTime: this.videoEl?.currentTime || 0,
      clip: this.activeClip,
    });
    this._syncDurationBadge();
  }

  _syncDurationBadge() {
    if (!this.durationEl) return;
    if (this.activeClip && Number(this.activeClip.end_sec) > Number(this.activeClip.start_sec)) {
      const dur = Number(this.activeClip.end_sec) - Number(this.activeClip.start_sec);
      this.durationEl.textContent = `DUR ${formatSmpte(dur, this.fps)}`;
    } else {
      this.durationEl.textContent = formatSmpte(this.videoEl?.duration || 0, this.fps);
    }
  }

  refreshFrameUI() {
    const cur = this.videoEl ? Number(this.videoEl.currentTime || 0) : 0;
    if (this.timecodeBtn && !this._editingTc) {
      this.timecodeBtn.textContent = formatSmpte(cur, this.fps);
    }
    this.scrubber.setTime(cur);
    this.onFrameTick?.(cur);
  }

  _startFrameLoop() {
    this._stopFrameLoop();
    const v = this.videoEl;
    if (!v) return;

    if (typeof v.requestVideoFrameCallback === "function") {
      const onVfc = () => {
        this.refreshFrameUI();
        if (!v.paused && !v.ended) {
          this._vfcId = v.requestVideoFrameCallback(onVfc);
        } else {
          this._vfcId = null;
        }
      };
      this._vfcId = v.requestVideoFrameCallback(onVfc);
    } else {
      const onRaf = () => {
        this.refreshFrameUI();
        if (!v.paused && !v.ended) {
          this._rafId = requestAnimationFrame(onRaf);
        } else {
          this._rafId = null;
        }
      };
      this._rafId = requestAnimationFrame(onRaf);
    }
  }

  _stopFrameLoop() {
    const v = this.videoEl;
    if (this._vfcId != null && v && typeof v.cancelVideoFrameCallback === "function") {
      v.cancelVideoFrameCallback(this._vfcId);
      this._vfcId = null;
    }
    if (this._rafId != null) {
      cancelAnimationFrame(this._rafId);
      this._rafId = null;
    }
  }

  _syncPlayButton() {
    if (!this.playToggleBtn || !this.videoEl) return;
    const t = (k, vars) => (window.SolacutI18n ? window.SolacutI18n.t(k, vars) : k);
    const playing = !this.videoEl.paused && !this.videoEl.ended;
    this.playToggleBtn.dataset.playing = playing ? "true" : "false";
    this.playToggleBtn.setAttribute("aria-label", playing ? t("mon.pause") : t("mon.play"));
    this.playToggleBtn.title = playing ? t("mon.pauseTitle") : t("mon.playTitle");
    this.onPlayStateChange?.(playing);
  }

  _applyEffectiveVolume() {
    const v = this.videoEl;
    if (!v) return;
    this._internalVolChange = true;
    const eff = this.dimmed ? Math.max(0.01, this.userVolume * 0.1) : this.userVolume;
    v.volume = Math.max(0, Math.min(1, eff));
    this._internalVolChange = false;
    this._syncVolumeSlider();
  }

  toggleDim(forceState = null) {
    this.dimmed = forceState === null ? !this.dimmed : Boolean(forceState);
    this._applyEffectiveVolume();
    return this.dimmed;
  }

  setSafeMode(mode, enable = true) {
    if (mode === "off") {
      this.showSafe916 = false;
    } else {
      this.safeMode = ["9:16", "4:5", "1:1", "title"].includes(mode) ? mode : "9:16";
      this.showSafe916 = Boolean(enable);
    }
    if (this.safeToggleEl) this.safeToggleEl.checked = this.showSafe916;
    if (this.safeModeSelectEl) {
      this.safeModeSelectEl.value = this.showSafe916 ? this.safeMode : "off";
    }
    this.syncSafeGuideLayout();
  }

  setZoomMode(mode) {
    this.zoomMode = ["fit", "50", "100", "150"].includes(String(mode)) ? String(mode) : "fit";
    if (this.zoomSelectEl) this.zoomSelectEl.value = this.zoomMode;
    this._applyZoom();
  }

  toggleCinemaMode(forceState = null) {
    const target = forceState === null ? !this.cinemaActive : Boolean(forceState);
    this.cinemaActive = target;
    document.body.classList.toggle("cinema-viewer-active", target);
    if (this.cinemaBtn) {
      this.cinemaBtn.classList.toggle("active", target);
      this.cinemaBtn.setAttribute("aria-pressed", target ? "true" : "false");
    }
    requestAnimationFrame(() => {
      this.syncSafeGuideLayout();
      this.meter.draw();
    });
    return target;
  }

  _syncVolumeSlider() {
    const sl = this.volumeSlider;
    const v = this.videoEl;
    if (!sl || !v) return;
    if (!this._internalVolChange && !this.dimmed) {
      this.userVolume = v.volume ?? 1;
    }
    const pct = v.muted ? 0 : Math.round((this.userVolume ?? 1) * 100);
    sl.value = String(pct);
    // 遵循 modern-web-guidance `slider`：同步 --attr-min / --attr-max / --control-value
    sl.style.setProperty("--attr-min", sl.min || "0");
    sl.style.setProperty("--attr-max", sl.max || "100");
    sl.style.setProperty("--control-value", String(pct));
    sl.setAttribute("aria-valuetext", `${pct}%${this.dimmed ? " (DIM -20dB)" : ""}`);
    if (this.muteBtn) {
      const t = (k, vars) => (window.SolacutI18n ? window.SolacutI18n.t(k, vars) : k);
      this.muteBtn.dataset.muted = v.muted || pct === 0 ? "true" : "false";
      this.muteBtn.title = v.muted || pct === 0 ? t("mon.unmute") : t("mon.mutePct", { pct });
      this.muteBtn.setAttribute("aria-label", v.muted || pct === 0 ? t("mon.unmute") : t("mon.mute"));
    }
    if (this.dimBtn) {
      this.dimBtn.dataset.active = this.dimmed ? "true" : "false";
      this.dimBtn.setAttribute("aria-pressed", this.dimmed ? "true" : "false");
    }
  }

  refreshLocale() {
    this._syncPlayButton();
    this._syncVolumeSlider();
    if (this.frameEl && window.SolacutI18n) {
      this.frameEl.setAttribute("data-cinema-hint", window.SolacutI18n.t("mon.cinemaHint"));
    }
  }

  /** 根据视频原生比例与容器尺寸精确对齐多比例安全框（9:16 / 4:5 / 1:1 / 动作标题安全框） */
  syncSafeGuideLayout() {
    if (!this.safeGuideEl || !this.frameEl || !this.videoEl) return;
    this.safeGuideEl.classList.toggle("hidden", !this.showSafe916);
    const subOverlay = this.frameEl.querySelector("#video-sub-overlay");
    if (!this.showSafe916) {
      if (subOverlay) {
        subOverlay.style.left = "";
        subOverlay.style.right = "";
        subOverlay.style.bottom = "";
      }
      return;
    }

    const fr = this.frameEl.getBoundingClientRect();
    if (fr.width < 10 || fr.height < 10) return;
    const vw = this.videoEl.videoWidth || 1920;
    const vh = this.videoEl.videoHeight || 1080;
    const containerRatio = fr.width / Math.max(1, fr.height);
    const videoRatio = vw / Math.max(1, vh);

    let drawW = fr.width;
    let drawH = fr.height;
    if (videoRatio > containerRatio) {
      drawH = fr.width / videoRatio;
    } else {
      drawW = fr.height * videoRatio;
    }

    // 修复 P1-04：同步乘入当前监看缩放倍率，确保 50% / 100%原生 / 150% 下安全框与真实画面严丝合缝
    const zoomScale = this._computeZoomScale(fr, drawW, drawH);
    const scaledW = drawW * zoomScale;
    const scaledH = drawH * zoomScale;

    let cropW = scaledW;
    let cropH = scaledH;
    if (this.safeMode === "9:16") {
      cropH = scaledH;
      cropW = Math.min(scaledW, cropH * (9 / 16));
    } else if (this.safeMode === "4:5") {
      cropH = scaledH;
      cropW = Math.min(scaledW, cropH * (4 / 5));
    } else if (this.safeMode === "1:1") {
      cropH = scaledH;
      cropW = Math.min(scaledW, cropH);
    } else if (this.safeMode === "title") {
      cropW = scaledW * 0.9;
      cropH = scaledH * 0.9;
    }

    const offsetX = (fr.width - cropW) / 2;
    const offsetY = (fr.height - cropH) / 2;

    this.safeGuideEl.dataset.mode = this.safeMode;
    this.safeGuideEl.style.setProperty("--crop-x", `${Math.round(offsetX)}px`);
    this.safeGuideEl.style.setProperty("--crop-y", `${Math.round(offsetY)}px`);
    this.safeGuideEl.style.setProperty("--crop-w", `${Math.round(cropW)}px`);
    this.safeGuideEl.style.setProperty("--crop-h", `${Math.round(cropH)}px`);
    const tag = this.safeGuideEl.querySelector(".safe-crop-tag");
    if (tag) {
      tag.textContent = this.safeMode === "title" ? "ACTION 90% / TITLE 80%" : this.safeMode;
    }

    // 开启竖屏/方屏安全框时，同步收窄双语字幕叠层宽度并贴齐安全底线，避免字幕穿框越界
    if (subOverlay) {
      if (this.showSafe916 && ["9:16", "4:5", "1:1"].includes(this.safeMode)) {
        subOverlay.style.left = `${Math.round(Math.max(0, offsetX) + 6)}px`;
        subOverlay.style.right = `${Math.round(Math.max(0, fr.width - (offsetX + cropW)) + 6)}px`;
        subOverlay.style.bottom = `${Math.round(Math.max(12, fr.height - (offsetY + cropH) + cropH * 0.14))}px`;
      } else {
        subOverlay.style.left = "";
        subOverlay.style.right = "";
        subOverlay.style.bottom = "";
      }
    }
  }

  _computeZoomScale(fr = null, fitW = 0, fitH = 0) {
    if (this.zoomMode === "fit") return 1;
    if (this.zoomMode === "50") return 0.5;
    if (this.zoomMode === "150") return 1.5;
    if (this.zoomMode === "100") {
      // 修复 P1-04：100% 代表按视频原生像素 1:1 显示（与 Fit 区分开）
      const rect = fr || this.frameEl?.getBoundingClientRect();
      const vw = this.videoEl?.videoWidth || 0;
      const vh = this.videoEl?.videoHeight || 0;
      if (rect && vw > 0 && vh > 0 && rect.width > 10 && rect.height > 10) {
        const baseW = fitW || Math.min(rect.width, rect.height * (vw / vh));
        const ratio = vw / Math.max(1, baseW);
        return Math.max(0.25, Math.min(4, Number(ratio.toFixed(3))));
      }
      return 1;
    }
    return 1;
  }

  _applyZoom() {
    if (!this.videoEl) return;
    const s = this._computeZoomScale();
    this.videoEl.style.transform = Math.abs(s - 1) < 0.005 ? "" : `scale(${s})`;
    this.syncSafeGuideLayout();
  }

  _openTimecodeEditor() {
    if (this._editingTc || !this.timecodeBtn || !this.videoEl?.src) return;
    this._editingTc = true;
    const curText = formatSmpte(this.videoEl.currentTime || 0, this.fps);
    const input = document.createElement("input");
    input.type = "text";
    input.className = "tc-inline-input mono-num";
    input.value = curText;

    const finish = (commit) => {
      if (!this._editingTc) return;
      this._editingTc = false;
      if (commit) {
        const parsed = parseSmpteOrTimecode(input.value, this.fps);
        if (parsed !== null && !Number.isNaN(parsed)) {
          const clamped = Math.max(0, Math.min(this.videoEl.duration || parsed, parsed));
          this.videoEl.currentTime = clamped;
          this.onSeekRequest?.(clamped);
        }
      }
      input.remove();
      this.timecodeBtn.classList.remove("hidden");
      this.refreshFrameUI();
    };

    input.addEventListener("keydown", (e) => {
      e.stopPropagation();
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

    this.timecodeBtn.classList.add("hidden");
    this.timecodeBtn.insertAdjacentElement("afterend", input);
    input.focus();
    input.select();
  }

  _bindEvents() {
    const v = this.videoEl;
    if (!v) return;

    v.addEventListener("play", () => {
      this._syncPlayButton();
      this._startFrameLoop();
      this.meter.start();
    });
    v.addEventListener("pause", () => {
      this._syncPlayButton();
      this._stopFrameLoop();
      this.refreshFrameUI();
    });
    v.addEventListener("ended", () => {
      this._syncPlayButton();
      this._stopFrameLoop();
      this.refreshFrameUI();
    });
    v.addEventListener("loadedmetadata", () => {
      this.scrubber.setContext({
        duration: v.duration || 0,
        currentTime: v.currentTime || 0,
        clip: this.activeClip,
      });
      this._syncDurationBadge();
      this.syncSafeGuideLayout();
      this.refreshFrameUI();
    });
    v.addEventListener("seeked", () => this.refreshFrameUI());
    v.addEventListener("volumechange", () => this._syncVolumeSlider());

    // 点击纯黑视频画布直接播放/暂停（自然 NLE 体验）
    v.addEventListener("click", () => {
      if (!v.src) return;
      if (v.paused) v.play().catch(() => {});
      else v.pause();
    });

    this.playToggleBtn?.addEventListener("click", () => {
      if (!v.src) return;
      if (v.paused) v.play().catch(() => {});
      else v.pause();
    });

    this.timecodeBtn?.addEventListener("click", () => this._openTimecodeEditor());
    this.timecodeBtn?.addEventListener("contextmenu", (e) => {
      if (typeof this.onTimecodeContextMenu === "function") {
        e.preventDefault();
        e.stopPropagation();
        this.onTimecodeContextMenu(e, formatSmpte(v.currentTime || 0, this.fps));
      }
    });

    this.frameEl?.addEventListener("contextmenu", (e) => {
      if (typeof this.onStageContextMenu === "function") {
        e.preventDefault();
        e.stopPropagation();
        this.onStageContextMenu(e);
      }
    });

    this.zoomSelectEl?.addEventListener("change", (e) => {
      this.zoomMode = e.target.value || "fit";
      this._applyZoom();
    });

    this.safeToggleEl?.addEventListener("change", (e) => {
      this.showSafe916 = Boolean(e.target.checked);
      if (this.safeModeSelectEl) {
        this.safeModeSelectEl.value = this.showSafe916 ? this.safeMode : "off";
      }
      this.syncSafeGuideLayout();
    });

    this.safeModeSelectEl?.addEventListener("change", (e) => {
      const mode = e.target.value || "off";
      this.setSafeMode(mode, mode !== "off");
    });

    this.cinemaBtn?.addEventListener("click", () => this.toggleCinemaMode());
    this.dimBtn?.addEventListener("click", () => this.toggleDim());

    this.volumeSlider?.addEventListener("input", () => {
      const val = Number(this.volumeSlider.value) / 100;
      v.muted = val <= 0.001;
      this.userVolume = Math.max(0, Math.min(1, val));
      // 修复 P2-03：记录任意 > 0 的真实低音量，取消静音时精确复原不强抬 20%
      if (val > 0.001) this._lastVol = val;
      this._applyEffectiveVolume();
    });

    this.muteBtn?.addEventListener("click", () => {
      if (v.muted || this.userVolume <= 0.001) {
        v.muted = false;
        this.userVolume = this._lastVol > 0.001 ? this._lastVol : 0.5;
      } else {
        this._lastVol = this.userVolume;
        v.muted = true;
      }
      this._applyEffectiveVolume();
    });

    if (this.frameEl) {
      this._frameRo = new ResizeObserver(() => {
        if (this.zoomMode === "100") this._applyZoom();
        else this.syncSafeGuideLayout();
      });
      this._frameRo.observe(this.frameEl);
    }
  }
}
