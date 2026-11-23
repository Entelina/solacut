/**
 * 微缩 Solacut NLE 暗房工作台交互演示数据
 * 4 个真实高能游戏切片，支持即时切换、播放头拖拽 scrubbing、SMPTE 时间码联动与 ASS 双语字幕覆层
 */

export interface SubtitleCue {
  /** 在当前切片内的起始进度 (0..1) */
  start: number;
  /** 在当前切片内的结束进度 (0..1) */
  end: number;
  /** 原文台词 */
  source: string;
  /** 母语级译文 */
  translated: string;
  /** 当前瞬时左/右声道电平峰值 (0..1) */
  vuLeft: number;
  vuRight: number;
}

export interface WorkbenchClip {
  id: string;
  rank: string;
  title: string;
  langPair: string;
  sourceLang: string;
  targetLang: string;
  /** 基准录屏起始时间（秒），用于计算真实 SMPTE HH:MM:SS:FF */
  baseSeconds: number;
  /** 切片总时长（秒） */
  durationSec: number;
  /** 展示用区间字符串 */
  timeRange: string;
  /** 音频带通峰值 dBFS */
  peakDb: string;
  /** 轨道与滤波标识 */
  trackLabel: string;
  /** 场景与视频规格标签 */
  sceneLabel: string;
  /** RAG 命中专家词条 */
  ragHits: string[];
  /** 高能峰值区间 [startRatio, endRatio]，在波形轨道上用低饱和琥珀色标定 */
  peakZone: [number, number];
  /** 96 根高密度波形采样振幅 (0.08..1.0) */
  waveform: number[];
  /** 按进度分段的实时双语 ASS 字幕轨 */
  cues: SubtitleCue[];
}

/** 生成确定性、具真实人声包络特征的 96 柱音频波形 */
function buildWaveform(seed: number, peakStart: number, peakEnd: number): number[] {
  const bars = 96;
  const values: number[] = [];
  for (let i = 0; i < bars; i++) {
    const t = i / (bars - 1);
    const inPeak = t >= peakStart && t <= peakEnd;
    const harmonic =
      Math.sin((i + seed * 7) * 0.63) * 0.28 +
      Math.cos((i * 1.41 + seed * 3)) * 0.22 +
      Math.sin((i * 2.87 + seed * 11)) * 0.14;
    const raw = Math.abs(harmonic);
    const base = inPeak ? 0.56 + raw * 0.44 : 0.14 + raw * 0.42;
    // 边缘呼吸停顿衰减
    const edgeFade = t < 0.06 || t > 0.94 ? 0.58 : 1;
    // 模拟真实语流短停顿
    const breathDip = (i + seed * 5) % 19 === 0 && !inPeak ? 0.35 : 1;
    const clamped = Math.max(0.08, Math.min(1, Number((base * edgeFade * breathDip).toFixed(3))));
    values.push(clamped);
  }
  return values;
}

export const WORKBENCH_CLIPS: readonly WorkbenchClip[] = [
  {
    id: 'clip-01',
    rank: '01',
    title: '残血漏弹反 · 弹幕瞬间破防',
    langPair: 'EN → ZH',
    sourceLang: 'en',
    targetLang: 'zh-CN',
    baseSeconds: 4458, // 01:14:18
    durationSec: 34,
    timeRange: '01:14:18 — 01:14:52',
    peakDb: '-1.8 dB',
    trackLabel: 'OBS Track 2 · 纯人声 200-3400Hz',
    sceneLabel: '1080p · 60.00 fps · REC.709',
    ragHits: ['whiff → 漏掉/打空', 'parry → 弹反', 'goes feral → 直接疯了'],
    peakZone: [0.32, 0.72],
    waveform: buildWaveform(1, 0.32, 0.72),
    cues: [
      {
        start: 0,
        end: 0.32,
        source: 'Hold on, one more deflect and this phase is completely done—',
        translated: '稳住稳住，再弹反一刀这个阶段就直接过了——',
        vuLeft: 0.56,
        vuRight: 0.52,
      },
      {
        start: 0.32,
        end: 0.72,
        source: 'God forbid I whiff one parry and chat instantly goes feral.',
        translated: '好家伙我就漏了一次弹反，弹幕直接疯了。',
        vuLeft: 0.92,
        vuRight: 0.88,
      },
      {
        start: 0.72,
        end: 1.01,
        source: 'No shot you guys clipped that frame-one, chill out!',
        translated: '不是吧你们这都能第一帧切片，先冷静点啊！',
        vuLeft: 0.74,
        vuRight: 0.71,
      },
    ],
  },
  {
    id: 'clip-02',
    rank: '02',
    title: '决胜局下饭失误 · 日语暴走',
    langPair: 'JA → ZH',
    sourceLang: 'ja',
    targetLang: 'zh-CN',
    baseSeconds: 7691, // 02:08:11
    durationSec: 33,
    timeRange: '02:08:11 — 02:08:44',
    peakDb: '-2.3 dB',
    trackLabel: 'OBS Track 2 · 纯人声 200-3400Hz',
    sceneLabel: '1080p · 60.00 fps · REC.709',
    ragHits: ['草生える → 笑死', 'プレミ → 下饭操作', '一回落ち着け → 先稳一手'],
    peakZone: [0.26, 0.68],
    waveform: buildWaveform(4, 0.26, 0.68),
    cues: [
      {
        start: 0,
        end: 0.28,
        source: '待って待って、今ウルト押したはずなのに出てないんだけど！',
        translated: '等等等等，我刚才明明按大招了怎么没放出来啊！',
        vuLeft: 0.62,
        vuRight: 0.6,
      },
      {
        start: 0.28,
        end: 0.7,
        source: '草生える、今の完全にプレミじゃん、一回落ち着けって。',
        translated: '笑死，刚刚那波纯纯下饭操作，兄弟先稳一手。',
        vuLeft: 0.89,
        vuRight: 0.85,
      },
      {
        start: 0.7,
        end: 1.01,
        source: 'いやマジで今の見なかったことにして、次で取り返すから。',
        translated: '真当刚才那波没看见行不行，下把一定打回来。',
        vuLeft: 0.68,
        vuRight: 0.66,
      },
    ],
  },
  {
    id: 'clip-03',
    rank: '03',
    title: '隔烟盲穿一枪秒 · 俄语卡点',
    langPair: 'RU → ZH',
    sourceLang: 'ru',
    targetLang: 'zh-CN',
    baseSeconds: 2765, // 00:46:05
    durationSec: 34,
    timeRange: '00:46:05 — 00:46:39',
    peakDb: '-1.4 dB',
    trackLabel: 'OBS Track 1 · 混合轨带通滤波',
    sceneLabel: '1440p · 60.00 fps · REC.709',
    ragHits: ['Жесть → 离大谱', 'ваншотнул → 一枪秒', 'тайминг → 卡点'],
    peakZone: [0.34, 0.76],
    waveform: buildWaveform(9, 0.34, 0.76),
    cues: [
      {
        start: 0,
        end: 0.34,
        source: 'Смок лежит плотно, я вообще не свечусь, тихо держу угол.',
        translated: '烟封得死死的，我根本没露身位，先静步架住这个角。',
        vuLeft: 0.48,
        vuRight: 0.45,
      },
      {
        start: 0.34,
        end: 0.76,
        source: 'Жесть, он меня реально ваншотнул через смок, найс тайминг!',
        translated: '离大谱，隔着烟都能一枪把我秒了，这卡点绝了！',
        vuLeft: 0.96,
        vuRight: 0.93,
      },
      {
        start: 0.76,
        end: 1.01,
        source: 'Ладно, эко раунд, дропните пистолет — ещё камбэкнем.',
        translated: '行吧纯经济局，给我发把副手——这把还能翻回来。',
        vuLeft: 0.64,
        vuRight: 0.62,
      },
    ],
  },
  {
    id: 'clip-04',
    rank: '04',
    title: '中路吃钩被演麻 · 西语翻盘',
    langPair: 'ES → ZH',
    sourceLang: 'es',
    targetLang: 'zh-CN',
    baseSeconds: 6750, // 01:52:30
    durationSec: 38,
    timeRange: '01:52:30 — 01:53:08',
    peakDb: '-2.1 dB',
    trackLabel: 'OBS Track 2 · 纯人声 200-3400Hz',
    sceneLabel: '1080p · 60.00 fps · REC.709',
    ragHits: ['No manches → 离谱', 'me comí bait → 吃了大钩子', 'troleada → 被演麻了'],
    peakZone: [0.28, 0.66],
    waveform: buildWaveform(13, 0.28, 0.66),
    cues: [
      {
        start: 0,
        end: 0.28,
        source: 'Ojo que el jungla no sale en mapa, no empujen demasiado.',
        translated: '注意对面打野没露头，兵线别推太深了。',
        vuLeft: 0.54,
        vuRight: 0.53,
      },
      {
        start: 0.28,
        end: 0.68,
        source: 'No manches, me comí tremendo bait en mid, qué troleada.',
        translated: '离谱，中路直接吃了对面一个大钩子，纯纯被演麻了。',
        vuLeft: 0.9,
        vuRight: 0.87,
      },
      {
        start: 0.68,
        end: 1.01,
        source: 'Pero mira el TP de top, se viene remontada épica, ¡vamos!',
        translated: '快看上单这波传送，史诗级翻盘要来了，直接冲！',
        vuLeft: 0.78,
        vuRight: 0.76,
      },
    ],
  },
];
