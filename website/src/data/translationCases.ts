/**
 * 六语种 × 60 万语料：普通机翻 vs Solacut 母语意译 真实对比数据
 * 每语种与 index.html 静态渲染面板严格保持单一事实同步（用于静态 SSR 首屏与 beforematch 页内搜索）
 */

export interface TranslationCase {
  id: string;
  tabLabel: string;
  shortCode: string;
  directionLabel: string;
  sourceLangAttr: string;
  targetLangAttr: string;
  sourceQuote: string;
  contextTag: string;
  machineTranslation: string;
  machinePitfall: string;
  solacutTranslation: string;
  solacutInsight: string;
  corpusScale: string;
  ragLatencyMs: string;
  glossaryHits: string[];
  qaStatus: string;
}

export const TRANSLATION_CASES: readonly TranslationCase[] = [
  {
    id: 'en-zh',
    tabLabel: '英 → 中',
    shortCode: 'EN→ZH',
    directionLabel: '英语直播原声 → 中文母语字幕',
    sourceLangAttr: 'en',
    targetLangAttr: 'zh-CN',
    sourceQuote: '“God forbid I whiff one parry and chat instantly goes feral.”',
    contextTag: 'Twitch / 动作游戏高难 Boss 切片',
    machineTranslation: '上帝禁止我打空一次格挡然后聊天立刻变野蛮。',
    machinePitfall: '硬译习语“God forbid”与直播高频词“chat / goes feral”，喜剧反差感彻底崩塌，观众出戏。',
    solacutTranslation: '好家伙我就漏了一次弹反，弹幕直接疯了。',
    solacutInsight: '精准捕捉反讽句式，同时命中游戏黑话“whiff / parry”与直播语境“chat → 弹幕”，一步到位达到熟肉字幕组水准。',
    corpusScale: '100,000 条 FTS5 索引',
    ragLatencyMs: '6.4 ms',
    glossaryHits: ['whiff → 漏掉', 'parry → 弹反', 'goes feral → 直接疯了'],
    qaStatus: '0 漏翻 · 未译英文残留 0 处',
  },
  {
    id: 'ja-zh',
    tabLabel: '日 → 中',
    shortCode: 'JA→ZH',
    directionLabel: '日语直播原声 → 中文母语字幕',
    sourceLangAttr: 'ja',
    targetLangAttr: 'zh-CN',
    sourceQuote: '「草生える、今の完全にプレミじゃん、一回落ち着けって」',
    contextTag: 'VTuber / 日服排位破防名场面',
    machineTranslation: '长草了，刚才完全是高级失误吧，冷静一次。',
    machinePitfall: '把弹幕词“草生える”译成植物长草，把日语电竞缩写“プレミ (プレイミス)”误判成“高级”。',
    solacutTranslation: '笑死，刚刚那波纯纯下饭操作，兄弟先稳一手。',
    solacutInsight: 'FTS5 Trigram 无空格分词精准命中日语电竞缩略词“プレミ”与口语收尾，贴合中文玩家语感。',
    corpusScale: '100,000 条 FTS5 Trigram',
    ragLatencyMs: '9.8 ms',
    glossaryHits: ['草生える → 笑死', 'プレミ → 下饭操作', '一回落ち着け → 先稳一手'],
    qaStatus: '0 漏翻 · 假名残留 0 处',
  },
  {
    id: 'ko-zh',
    tabLabel: '韩 → 中',
    shortCode: 'KO→ZH',
    directionLabel: '韩语直播原声 → 中文母语字幕',
    sourceLangAttr: 'ko',
    targetLangAttr: 'zh-CN',
    sourceQuote: '“아니 이건 진짜 억까 아니냐고, 궁 빠진 거 보고 들어오네”',
    contextTag: '韩服高端局 / MOBA 团战集锦',
    machineTranslation: '不是这真是勉强黑吗，看着大招掉了进来啊。',
    machinePitfall: '完全无法理解韩网新造词“억까”与技能冷却表达“궁 빠진 거”，译文生硬难懂。',
    solacutTranslation: '不是吧这纯属针对硬演我，看我没大直接硬开啊。',
    solacutInsight: 'Trigram 索引召回韩服电竞术语卡，完整保留“没大招被对面盯上强开”的实战张力。',
    corpusScale: '100,000 条 FTS5 Trigram',
    ragLatencyMs: '11.2 ms',
    glossaryHits: ['억까 → 针对硬演', '궁 빠진 거 → 没大', '들어오네 → 直接硬开'],
    qaStatus: '0 漏翻 · 谚文残留 0 处',
  },
  {
    id: 'ru-zh',
    tabLabel: '俄 → 中',
    shortCode: 'RU→ZH',
    directionLabel: '俄语直播原声 → 中文母语字幕',
    sourceLangAttr: 'ru',
    targetLangAttr: 'zh-CN',
    sourceQuote: '“Жесть, он меня реально ваншотнул через смок, найс тайминг”',
    contextTag: 'CIS 赛区 / CS2 战术 FPS 残局',
    machineTranslation: '铁皮，他真的通过烟雾一枪我了，好时机。',
    machinePitfall: '“Жесть”按词典直译为“铁皮”，俄语西里尔化的英文借词“ваншотнул / смок / тайминг”全线翻车。',
    solacutTranslation: '离大谱，隔着烟都能一枪把我秒了，这卡点绝了。',
    solacutInsight: '准确识破俄区 FPS 借词变位与反讽感叹语气，输出地道 CS 观赛中文表达。',
    corpusScale: '100,000 条 FTS5 索引',
    ragLatencyMs: '8.1 ms',
    glossaryHits: ['Жесть → 离大谱', 'ваншотнул → 一枪秒', 'тайминг → 卡点'],
    qaStatus: '0 漏翻 · 西里尔残留 0 处',
  },
  {
    id: 'es-zh',
    tabLabel: '西 → 中',
    shortCode: 'ES→ZH',
    directionLabel: '西语直播原声 → 中文母语字幕',
    sourceLangAttr: 'es',
    targetLangAttr: 'zh-CN',
    sourceQuote: '“No manches, me comí tremendo bait en mid, qué troleada”',
    contextTag: '拉美/西语区主播 / 中路视野博弈',
    machineTranslation: '别弄脏，我在中路吃了巨大的诱饵，多么巨魔。',
    machinePitfall: '拉美高频叹词“No manches”被按字面直译为“别弄脏”，“troleada”译成“巨魔”，毫无笑点。',
    solacutTranslation: '离谱，中路直接吃了对面一个大钩子，纯纯被演麻了。',
    solacutInsight: '反直译红线金库命中西语口语与 MOBA 混用黑话“bait / mid / troleada”，流畅自然。',
    corpusScale: '100,000 条 FTS5 索引',
    ragLatencyMs: '7.3 ms',
    glossaryHits: ['No manches → 离谱', 'me comí bait → 吃钩', 'troleada → 被演麻了'],
    qaStatus: '0 漏翻 · 西语残词 0 处',
  },
  {
    id: 'zh-en',
    tabLabel: '中 → 英出海',
    shortCode: 'ZH→EN',
    directionLabel: '中文高光原声 → 母语级出海英文字幕',
    sourceLangAttr: 'zh-CN',
    targetLangAttr: 'en',
    sourceQuote: '“这波弹反空了直接破防，对面打野纯在演我”',
    contextTag: '国服主播 / YouTube Shorts & TikTok 出海',
    machineTranslation: 'This wave parry empty directly break defense, opposite jungle purely acting me.',
    machinePitfall: '逐词硬翻导致严重 Chinglish，海外观众看不懂“break defense / acting me”，完播率暴跌。',
    solacutTranslation: 'That missed parry totally tilted me — their jungler is straight-up trolling.',
    solacutInsight: '直出欧美游戏圈原生高频表达：“破防 → tilted”、“纯在演我 → straight-up trolling”，出海即母语。',
    corpusScale: '100,000 条 FTS5 Trigram',
    ragLatencyMs: '10.6 ms',
    glossaryHits: ['破防 → tilted', '打野 → jungler', '纯在演我 → straight-up trolling'],
    qaStatus: '0 漏翻 · 中文字符残留 0 处',
  },
];
