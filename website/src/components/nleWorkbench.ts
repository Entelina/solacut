/**
 * 镇馆交互组件：微缩 Solacut NLE 暗房工作台
 * - 4 个真实高能游戏切片即时切换
 * - 实时双语 ASS 字幕覆层（译上原下 / 原上译下 / 仅译文）
 * - 高 DPI Canvas 96 柱精密音频波形 + 琥珀高能区 + 冰青播放头拖拽 scrubbing
 * - 真实可点击 & 键盘联动的 J / K / L / I / O 非编走带控制
 * - 动态 SMPTE 时间码 (60fps) + 双声道 VU 电平表
 * - 遵循 WCAG 2.2.2：提供可见播放/暂停按钮，文案切换时不叠加 aria-pressed
 */

import { WORKBENCH_CLIPS, type SubtitleCue, type WorkbenchClip } from '../data/workbenchDemo';

type SubtitleMode = 'zh-top' | 'src-top' | 'zh-only';
type ShuttleAction = 'back' | 'toggle' | 'forward' | 'in' | 'out';

function formatSmpte(totalSeconds: number, fps = 60): string {
  const safe = Math.max(0, totalSeconds);
  const hours = Math.floor(safe / 3600);
  const mins = Math.floor((safe % 3600) / 60);
  const secs = Math.floor(safe % 60);
  const frames = Math.floor((safe - Math.floor(safe)) * fps) % fps;
  const pad = (n: number) => String(n).padStart(2, '0');
  return `${pad(hours)}:${pad(mins)}:${pad(secs)}:${pad(frames)}`;
}

function findActiveCue(clip: WorkbenchClip, progress: number): SubtitleCue {
  for (const cue of clip.cues) {
    if (progress >= cue.start && progress < cue.end) {
      return cue;
    }
  }
  return clip.cues[clip.cues.length - 1] ?? clip.cues[0]!;
}

export function initNleWorkbench(announce: (msg: string) => void): void {
  const root = document.getElementById('nle-workbench');
  if (!root) return;

  const smpteEl = root.querySelector<HTMLElement>('[data-nle-smpte]');
  const trackLabelEl = root.querySelector<HTMLElement>('[data-nle-track-label]');
  const sceneSpecEl = root.querySelector<HTMLElement>('[data-nle-scene-spec]');
  const ragContainer = root.querySelector<HTMLElement>('[data-nle-rag]');
  const ragCompactEl = root.querySelector<HTMLElement>('[data-nle-rag-compact]');
  const subBoxEl = root.querySelector<HTMLElement>('[data-nle-sub-box]');
  const subZhEl = root.querySelector<HTMLElement>('[data-nle-sub-zh]');
  const subSrcEl = root.querySelector<HTMLElement>('[data-nle-sub-src]');
  const vuLeftEl = root.querySelector<HTMLElement>('[data-nle-vu-left]');
  const vuRightEl = root.querySelector<HTMLElement>('[data-nle-vu-right]');
  const vuPeakEl = root.querySelector<HTMLElement>('[data-nle-vu-peak]');
  const playBtn = root.querySelector<HTMLButtonElement>('[data-nle-play-toggle]');
  const playLabelEl = root.querySelector<HTMLElement>('[data-nle-play-label]');
  const playIconEl = root.querySelector<SVGSVGElement>('[data-nle-icon-play]');
  const pauseIconEl = root.querySelector<SVGSVGElement>('[data-nle-icon-pause]');
  const waveformTrack = root.querySelector<HTMLElement>('[data-nle-waveform-track]');
  const canvas = root.querySelector<HTMLCanvasElement>('[data-nle-canvas]');
  const playheadEl = root.querySelector<HTMLElement>('[data-nle-playhead]');
  const scrubRange = root.querySelector<HTMLInputElement>('[data-nle-scrub]');
  const rulerEl = root.querySelector<HTMLElement>('[data-nle-ruler]');
  const clipButtons = Array.from(root.querySelectorAll<HTMLButtonElement>('[data-nle-clip]'));
  const modeButtons = Array.from(root.querySelectorAll<HTMLButtonElement>('[data-nle-sub-mode]'));
  const shuttleButtons = Array.from(root.querySelectorAll<HTMLButtonElement>('[data-nle-shuttle]'));

  if (!canvas || !waveformTrack || !scrubRange || !playBtn) return;
  const ctx = canvas.getContext('2d');
  if (!ctx) return;

  let activeClipIndex = 0;
  let progress = 0.44;
  const prefersReducedMotion = window.matchMedia('(prefers-reduced-motion: reduce)').matches;
  let isPlaying = !prefersReducedMotion;
  let isDragging = false;
  let rafId = 0;
  let lastTs = 0;

  const getClip = (): WorkbenchClip => WORKBENCH_CLIPS[activeClipIndex] ?? WORKBENCH_CLIPS[0]!;

  const updatePlayButtonUI = (): void => {
    if (playLabelEl) {
      playLabelEl.textContent = isPlaying ? '暂停' : '播放';
    }
    if (playIconEl && pauseIconEl) {
      playIconEl.classList.toggle('is-hidden', isPlaying);
      pauseIconEl.classList.toggle('is-hidden', !isPlaying);
    }
    playBtn.setAttribute('aria-label', isPlaying ? '暂停时间线模拟播放' : '播放时间线模拟预览');
  };

  const renderRuler = (clip: WorkbenchClip): void => {
    if (!rulerEl) return;
    const marks = 6;
    const labels: string[] = [];
    for (let i = 0; i <= marks; i++) {
      const sec = clip.baseSeconds + (clip.durationSec * i) / marks;
      const h = Math.floor(sec / 3600);
      const m = Math.floor((sec % 3600) / 60);
      const s = Math.floor(sec % 60);
      const pad = (n: number) => String(n).padStart(2, '0');
      labels.push(`${pad(h)}:${pad(m)}:${pad(s)}`);
    }
    rulerEl.replaceChildren(
      ...labels.map((text) => {
        const span = document.createElement('span');
        span.textContent = text;
        return span;
      }),
    );
  };

  const renderRagTags = (clip: WorkbenchClip): void => {
    if (ragContainer) {
      const headerTag = document.createElement('span');
      headerTag.className = 'nle-hud-tag nle-hud-tag--accent';
      headerTag.textContent = 'RAG 命中';

      const hitTags = clip.ragHits.map((hit) => {
        const span = document.createElement('span');
        span.className = 'nle-hud-tag';
        span.textContent = hit;
        return span;
      });
      ragContainer.replaceChildren(headerTag, ...hitTags);
    }
    if (ragCompactEl) {
      ragCompactEl.textContent = `RAG · ${clip.ragHits.length} 词条命中`;
    }
  };

  const drawWaveform = (): void => {
    const rect = waveformTrack.getBoundingClientRect();
    const width = Math.max(1, Math.floor(rect.width));
    const height = Math.max(1, Math.floor(rect.height));
    const dpr = Math.min(window.devicePixelRatio || 1, 2);

    if (canvas.width !== Math.floor(width * dpr) || canvas.height !== Math.floor(height * dpr)) {
      canvas.width = Math.floor(width * dpr);
      canvas.height = Math.floor(height * dpr);
    }

    ctx.save();
    ctx.scale(dpr, dpr);
    ctx.clearRect(0, 0, width, height);

    const clip = getClip();
    const [peakStart, peakEnd] = clip.peakZone;

    // 1. 琥珀色高能检测区间底带 + 顶边细标线
    const peakX = Math.floor(peakStart * width);
    const peakW = Math.max(2, Math.floor((peakEnd - peakStart) * width));
    ctx.fillStyle = 'rgba(217, 155, 56, 0.11)';
    ctx.fillRect(peakX, 0, peakW, height);
    ctx.fillStyle = '#D99B38';
    ctx.fillRect(peakX, 0, peakW, 2);

    // 2. 轨道中心基准 0dB 细线
    const midY = Math.floor(height / 2) + 0.5;
    ctx.strokeStyle = 'rgba(255, 255, 255, 0.08)';
    ctx.lineWidth = 1;
    ctx.beginPath();
    ctx.moveTo(0, midY);
    ctx.lineTo(width, midY);
    ctx.stroke();

    // 3. 96 柱高密度音频波形
    const bars = clip.waveform;
    const count = bars.length;
    const step = width / count;
    const barW = Math.max(1.5, Math.min(4, step * 0.58));
    const maxAmp = height * 0.42;

    for (let i = 0; i < count; i++) {
      const ratio = i / (count - 1);
      const amp = (bars[i] ?? 0.2) * maxAmp;
      const x = Math.round(i * step + (step - barW) / 2);
      const inPeak = ratio >= peakStart && ratio <= peakEnd;
      const passed = ratio <= progress;

      if (inPeak) {
        ctx.fillStyle = passed ? '#E5AB4B' : 'rgba(217, 155, 56, 0.52)';
      } else if (passed) {
        ctx.fillStyle = 'rgba(73, 188, 236, 0.78)';
      } else {
        ctx.fillStyle = '#333B4B';
      }

      const barHeight = Math.max(4, Math.round(amp * 2));
      const y = Math.round(height / 2 - barHeight / 2);
      ctx.fillRect(x, y, barW, barHeight);
    }

    ctx.restore();
  };

  const renderFrameState = (): void => {
    const clip = getClip();
    const currentSeconds = clip.baseSeconds + clip.durationSec * progress;
    const cue = findActiveCue(clip, progress);

    if (smpteEl) {
      smpteEl.textContent = formatSmpte(currentSeconds);
    }
    if (subZhEl && subZhEl.textContent !== cue.translated) {
      subZhEl.textContent = cue.translated;
      subZhEl.setAttribute('lang', clip.targetLang);
    }
    if (subSrcEl && subSrcEl.textContent !== cue.source) {
      subSrcEl.textContent = cue.source;
      subSrcEl.setAttribute('lang', clip.sourceLang);
    }

    const microWave = isPlaying ? Math.sin(performance.now() * 0.012) * 0.045 : 0;
    const leftVal = Math.max(0.15, Math.min(0.98, cue.vuLeft + microWave));
    const rightVal = Math.max(0.15, Math.min(0.98, cue.vuRight - microWave * 0.8));

    if (vuLeftEl) vuLeftEl.style.transform = `scaleX(${leftVal.toFixed(3)})`;
    if (vuRightEl) vuRightEl.style.transform = `scaleX(${rightVal.toFixed(3)})`;
    if (vuPeakEl) vuPeakEl.textContent = clip.peakDb;

    const pct = `${(progress * 100).toFixed(2)}%`;
    if (playheadEl) {
      playheadEl.style.setProperty('--playhead-pct', pct);
    }
    scrubRange.value = String(Math.round(progress * 1000));
    scrubRange.setAttribute('aria-valuetext', `${formatSmpte(currentSeconds)}，${cue.translated}`);

    drawWaveform();
  };

  const selectClip = (index: number, shouldAnnounce = false): void => {
    if (index < 0 || index >= WORKBENCH_CLIPS.length) return;
    activeClipIndex = index;
    const clip = getClip();
    progress = Math.min(0.85, clip.peakZone[0] + 0.08);

    clipButtons.forEach((btn, i) => {
      const active = i === index;
      btn.setAttribute('aria-current', active ? 'true' : 'false');
    });

    if (trackLabelEl) trackLabelEl.textContent = clip.trackLabel;
    if (sceneSpecEl) sceneSpecEl.textContent = clip.sceneLabel;

    renderRuler(clip);
    renderRagTags(clip);
    renderFrameState();

    if (shouldAnnounce) {
      announce(`已切换至高光切片 ${clip.rank}：${clip.title}`);
    }
  };

  const setSubtitleMode = (mode: SubtitleMode): void => {
    if (subBoxEl) {
      subBoxEl.dataset.mode = mode;
    }
    modeButtons.forEach((btn) => {
      const isActive = btn.dataset.nleSubMode === mode;
      btn.setAttribute('aria-pressed', String(isActive));
    });
  };

  const togglePlayback = (): void => {
    isPlaying = !isPlaying;
    lastTs = performance.now();
    updatePlayButtonUI();
    renderFrameState();
    announce(isPlaying ? '已开始模拟播放时间线' : '已暂停模拟播放时间线');
  };

  const flashShuttleKey = (action: ShuttleAction): void => {
    const btn = shuttleButtons.find((item) => item.dataset.nleShuttle === action);
    if (!btn) return;
    btn.setAttribute('data-active', '');
    window.setTimeout(() => btn.removeAttribute('data-active'), 220);
  };

  const triggerShuttle = (action: ShuttleAction): void => {
    const clip = getClip();
    flashShuttleKey(action);
    switch (action) {
      case 'back':
        progress = Math.max(0, progress - 0.12);
        renderFrameState();
        break;
      case 'toggle':
        togglePlayback();
        break;
      case 'forward':
        progress = Math.min(0.99, progress + 0.12);
        renderFrameState();
        break;
      case 'in':
        progress = clip.peakZone[0];
        renderFrameState();
        announce('已定位至高能人声入点');
        break;
      case 'out':
        progress = clip.peakZone[1];
        renderFrameState();
        announce('已定位至高能人声出点');
        break;
    }
  };

  // 切片列表切换
  clipButtons.forEach((btn, idx) => {
    btn.addEventListener('click', () => selectClip(idx, true));
  });

  // 字幕显示模式切换
  modeButtons.forEach((btn) => {
    btn.addEventListener('click', () => {
      const mode = btn.dataset.nleSubMode as SubtitleMode | undefined;
      if (mode) setSubtitleMode(mode);
    });
  });

  // 播放 / 暂停开关（严格遵循 WCAG 2.2.2，不设置 aria-pressed）
  playBtn.addEventListener('click', togglePlayback);

  // J / K / L / I / O 非编走带控制按钮点击与聚焦内快捷键支持
  shuttleButtons.forEach((btn) => {
    btn.addEventListener('click', () => {
      const action = btn.dataset.nleShuttle as ShuttleAction | undefined;
      if (action) triggerShuttle(action);
    });
  });

  root.addEventListener('keydown', (event) => {
    if (event.altKey || event.ctrlKey || event.metaKey) return;
    const key = event.key.toLowerCase();
    const map: Record<string, ShuttleAction> = {
      j: 'back',
      k: 'toggle',
      l: 'forward',
      i: 'in',
      o: 'out',
    };
    const action = map[key];
    if (action) {
      event.preventDefault();
      triggerShuttle(action);
    }
  });

  // 指针直接点击/拖拽波形时间线
  const updateFromPointer = (clientX: number): void => {
    const rect = waveformTrack.getBoundingClientRect();
    if (rect.width <= 0) return;
    progress = Math.max(0, Math.min(1, (clientX - rect.left) / rect.width));
    renderFrameState();
  };

  waveformTrack.addEventListener('pointerdown', (event) => {
    if (event.button !== 0 && event.pointerType === 'mouse') return;
    isDragging = true;
    waveformTrack.setPointerCapture(event.pointerId);
    updateFromPointer(event.clientX);
  });

  waveformTrack.addEventListener('pointermove', (event) => {
    if (!isDragging) return;
    updateFromPointer(event.clientX);
  });

  const endDrag = (): void => {
    isDragging = false;
  };
  waveformTrack.addEventListener('pointerup', endDrag);
  waveformTrack.addEventListener('pointercancel', endDrag);

  // 原生 range 滑块支持键盘方向键与辅助技术精准定位
  scrubRange.addEventListener('input', () => {
    progress = Math.max(0, Math.min(1, Number(scrubRange.value) / 1000));
    renderFrameState();
  });

  // 容器尺寸变化时重绘高清 Canvas
  const resizeObserver = new ResizeObserver(() => {
    drawWaveform();
  });
  resizeObserver.observe(waveformTrack);

  // 动画主循环（节流至每帧平滑推进；页面不可见时自动跳过省电）
  const tick = (ts: number): void => {
    if (!lastTs) lastTs = ts;
    const dt = Math.min(0.1, (ts - lastTs) / 1000);
    lastTs = ts;

    if (isPlaying && !isDragging && !document.hidden) {
      const clip = getClip();
      progress += (dt * 1.8) / clip.durationSec;
      if (progress >= 1) {
        progress = 0;
      }
      renderFrameState();
    }

    rafId = requestAnimationFrame(tick);
  };

  document.addEventListener('visibilitychange', () => {
    if (!document.hidden) {
      lastTs = performance.now();
    }
  });

  // 系统减动效偏好实时监听
  window.matchMedia('(prefers-reduced-motion: reduce)').addEventListener('change', (e) => {
    if (e.matches && isPlaying) {
      isPlaying = false;
      updatePlayButtonUI();
      renderFrameState();
    }
  });

  // 初始化状态
  updatePlayButtonUI();
  setSubtitleMode('zh-top');
  selectClip(0, false);
  rafId = requestAnimationFrame(tick);

  window.addEventListener(
    'pagehide',
    () => {
      cancelAnimationFrame(rafId);
      resizeObserver.disconnect();
    },
    { once: true },
  );
}
