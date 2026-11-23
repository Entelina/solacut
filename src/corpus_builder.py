"""开源多语种顶级高质量双语语料采集、严苛母语清洗与 SQLite FTS5 索引构建器。

六大核心语种对（拉齐至 10 万条/语种对同规模黄金精洗语料，总规模达 60 万级）：
1. `en_zh`（英语 → 简体中文 · 旗舰基准语种）：Twitch/YouTube 欧美游戏主播、VTuber 与电竞切片
2. `zh_en`（简体中文 → 地道母语英语 · 国游出海）：《黑神话》《燕云十六声》《鸣潮》及中文主播出海双语切片
3. `ja_zh`（日语 → 简体中文 · 必备语种）：Hololive / 彩虹社 / VSPO! / 日系动作二游直播切片
4. `ko_zh`（韩语 → 简体中文 · 必备语种）：LCK / T1 / 韩服排位 / SOOP / CHZZK 主播切片
5. `ru_zh`（俄语 → 简体中文 · 国内爆款增补）：CS2 / Dota2 / 逃离塔科夫 / 战争雷霆俄区整活切片
6. `es_zh`（西班牙语 → 简体中文 · 全球蓝海增补）：Twitch 第二大语种、西语/拉美主播与电竞足坛切片

权威多源融合（商用无限制许可 + 影视口语字幕 + 真人演讲字幕 + 人类专家基准）：
- OPUS Tatoeba (v2026-07-08, CC BY 2.0 FR · 商用友好)：母语者人工精译日常自然口语
- Microsoft NTREX-128 (CC BY-SA 4.0 · 商用友好)：微软官方专业人类译员逐句精校基准
- OPUS QED (v2.0a, CC BY-SA 4.0 · 商用友好)：开源精校视频演讲与教学双语字幕
- OPUS WikiMatrix (v1, CC BY-SA 4.0 · 商用友好)：多领域百科精筛高对齐短句
- OPUS News-Commentary (v16 · 公开商用研究)：高质量长难句与新闻评论专家译文
- OPUS TED2020 (v1, CC BY-NC-ND 4.0 · 研究/选配)：真人字幕组精译 TED 演讲口语字幕
- OPUS OpenSubtitles (v2018/v2024 · 研究/选配)：千万级影视剧真实口语对白字幕（流式低内存堆选）

核心工程特性：
- 恒定低内存流式优先堆（`heapq`）管道，处理千万行 OpenSubtitles 内存恒定 < 120MB；
- 多语种专属清洗与母语口语亲和度评分，严防繁体混入、错行污染、广告水印与翻译腔；
- SQLite FTS5 按语种自动匹配最优分词器：
  * `ja_zh` / `ko_zh` / `zh_en`：原生 `tokenize='trigram'`，根治无空格 CJK 检索失效；
  * `en_zh`：`tokenize='porter unicode61 remove_diacritics 2'`；
  * `ru_zh` / `es_zh`：`tokenize='unicode61 remove_diacritics 2'`。

命令行运行示例：
    python src/corpus_builder.py --lang-pair en_zh
    python src/corpus_builder.py --lang-pair all
    python src/corpus_builder.py --lang-pair all --commercial-only
"""

from __future__ import annotations

import argparse
import heapq
import html
import io
import json
import re
import sqlite3
import sys
import time
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator

import httpx

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CORPUS_ROOT = PROJECT_ROOT / "data" / "corpus"

ALL_LANG_PAIRS: tuple[str, ...] = ("en_zh", "zh_en", "ja_zh", "ko_zh", "ru_zh", "es_zh")
TARGET_PAIRS_PER_LANG = 100_000

_CJK_RE = re.compile(r"[\u4e00-\u9fff]")
_JP_KANA_RE = re.compile(r"[\u3040-\u30ff]")
_JP_ALL_RE = re.compile(r"[\u3040-\u30ff\u4e00-\u9fff]")
_KR_HANGUL_RE = re.compile(r"[\uac00-\ud7af]")
_CYRILLIC_RE = re.compile(r"[\u0400-\u04ff]")
_LATIN_WORD_RE = re.compile(r"[A-Za-zÁÉÍÓÚÜÑáéíóúüñ]+(?:'[A-Za-z]+)?")
_EN_WORD_RE = re.compile(r"[A-Za-z]+(?:'[A-Za-z]+)?")
_RU_WORD_RE = re.compile(r"[\u0400-\u04ff]+(?:-[\u0400-\u04ff]+)?")
_DIGIT_RE = re.compile(r"\d")
_HTML_TAG_RE = re.compile(r"<[^>]+>")
_STAGE_NOTE_RE = re.compile(
    r"(?:\[[^\]]{1,40}\]|\((?:music|applause|laughter|cheers|sighs|inaudible|drum|sound|video|audio|音乐|掌声|笑声|拍手|笑い|박수|웃음)[^)]{0,40}\))",
    re.IGNORECASE,
)
_TEXTBOOK_NAME_RE = re.compile(r"\b(?:Tom|Mary|Sami|Layla|Boston)\b")
_WATERMARK_RE = re.compile(
    r"(?:https?://|www\.|\.com\b|\.org\b|\.net\b|subtitles?\s+by|translated\s+by|synced\s+by|"
    r"opensubtitles|subscene|addic7ed|字幕组|翻译组|校对|时间轴|人人影视|射手网|听译|压制|字幕制作|"
    r"자막\s*제작|싱크\s*수정|翻訳\s*[:：]|субтитры)",
    re.IGNORECASE,
)
# 过滤开源平行范例中的露骨色情/极端仇恨词，避免 Few-Shot 污染风格或触发云端 API 内容风控
_EXPLICIT_FILTER_RE = re.compile(
    r"(?:\b(?:dick|cock|pussy|cunt|blowjob|porn|rape|nigger|faggot|whore|slut)\b|"
    r"鸡巴|屄|操你妈|草泥马|强奸|ちんこ|まんこ|レイプ|씨발|병신|강간|хуй|пизда|ебать|блядь|puta|mierda|coño)",
    re.IGNORECASE,
)

# 各语种口语/对话自然度加分特征
_COLLOQUIAL_EN_RE = re.compile(
    r"\b(?:i'm|i've|i'd|i'll|don't|doesn't|didn't|can't|couldn't|won't|wouldn't|shouldn't|"
    r"isn't|aren't|wasn't|weren't|haven't|hasn't|it's|that's|what's|there's|you're|we're|they're|"
    r"gonna|wanna|gotta|kinda|yeah|hey|oh|wow|wait|actually|literally|honestly|seriously|come on|no way)\b",
    re.IGNORECASE,
)
_COLLOQUIAL_ZH_RE = re.compile(
    r"(?:吗|呢|吧|啊|嘛|呀|哦|哈|其实|根本|简直|压根|真的|居然|竟然|赶紧|别|咱们|啥|咋|怎么|干嘛|太.*了|？|！)"
)
_COLLOQUIAL_JA_RE = re.compile(
    r"(?:だよ|だね|じゃん|マジ|やばい|ヤバい|めっちゃ|って|かな|よね|でしょ|だろ|ちょっと|本当|ほんと|すごい|凄い|無理|やめて|待って|？|！)"
)
_COLLOQUIAL_KO_RE = re.compile(
    r"(?:요[\.\?!]?$|죠[\.\?!]?$|야[\.\?!]?$|냐[\.\?!]?$|진짜|완전|정말|아니|대박|미쳤|잠깐|제발|근데|그냥|너무|뭐야|왜|어떻게|？|！|\?|!)"
)
_COLLOQUIAL_RU_RE = re.compile(
    r"(?:\b(?:ну|вот|давай|ладно|блин|чёрт|черт|слушай|подожди|серьёзно|серьезно|реально|вообще|просто|короче|круто|жесть|ого|эй|правда)\b|\?|!)",
    re.IGNORECASE,
)
_COLLOQUIAL_ES_RE = re.compile(
    r"(?:\b(?:oye|mira|bueno|vale|claro|venga|espera|en serio|de verdad|qué|cómo|por qué|dónde|quién|vamos|joder|hostia|tío|wey|órale|genial|locura|increíble|nada|nunca|siempre|ahora|aquí|esto|eso|eres|estoy|estás|tengo|quiero|puedo)\b|[¿¡ñáéíóú])",
    re.IGNORECASE,
)
# 用于辨识西语真实性（防止 WikiMatrix 等混入纯英文句）
_ES_MARKER_RE = re.compile(
    r"(?:[¿¡ñáéíóúü]|\b(?:el|la|los|las|un|una|que|de|en|por|para|con|como|pero|más|muy|está|están|son|fue|era|hay|yo|tú|él|ella|nosotros|ellos|esto|eso|mi|tu|su|nos|les|del|al|sin|sobre|todo|todos|bien|aquí|ahora|cuando|donde|porque|también|solo|siempre|nunca|nada|algo|hacer|tener|decir|poder|ver|ir|saber|querer)\b)",
    re.IGNORECASE,
)

# 高频繁体独有字集合（严格净化简中语料，防止混入繁体字影响商业出品一致性）
_TRADITIONAL_ONLY_CHARS = frozenset(
    "這們來說時會個學為與對過點裡後發動實現開關機問題樣經還麼嗎頭體當處義無應區國圖際讓給誰請謝進選連實線號網電"
    "買賣錢車門問間聞聽視見覺觀認記語話讀變該調談論講議證識護翻譯釋錄錯長隊難離雲零響頁頂項順須預領題額顏風飛食飲"
    "馬駕驗高髮鬧魚鳥鳴鹽麥黃齊龍龜氣壞壓廣廳廢廠異張彎歸彈錄徹愛憂憶懷戀懸戲戰戶拋掃揚換損搖擊擔據擴擺擾攝"
    "攤敵數整斷於舊昇晝書望朝殺雜權條東極構槍樂樓標樞樹檢業榮橋檔櫃歡步歲漢湯溫準溝滅演滿潔潛澤濃"
    "濟濤濫濱濕煩熱燈燒營燦爛爭爺爾牆片版牘獎獨獄獵獸獻現球理產畫疇疊療監蓋盡盤盧眾睜瞞矚矯礦碼磚磨礎"
    "禮禱禦萬葉著蔥藍藏藝藥蘇蘭虛虧蟲補裝製複褲襲親覽觸解計訂訊討訓託訪設許訴診評詞詢試詩詳誇誌"
    "誓誕誘誠誤課誼謀諜謊諧諭諮諾謹警譽讚貝貞負財貢貧貨販貪貫責貯貴貶貸費貼貿賀資"
    "賈賄賃賊賑賓賜賞賠賢賦質賬賭賴賺賽贈贊贏趙趕趨躍跡踐踴踏蹤轉輪輕載輔輩輝輯輸轄輿辭辦農運達違遙遞遠"
    "適遭遮遲遷遺遼邁邊邏鄰鄧鄭郵鄉醫醬釀釐針釣鈴鐵銀銅銘銷鋒鋤錯錫錦館鋼鍵鍾鎮鏡閉閏閒閱閣閥闆闊"
    "階際陸陳險隨隱隸隻雙雞霧露霸靈靜韋頓頒頑頗頻顆願類顧顯飄飢飯飽飾餅養餐"
    "餘首香駐駕駛駝駭騎騙騷驅驕驚驟骨鬱鬼魂魔魯鮮鯨鴉鴨鴻鴿鵝鷹麵麻黨齒齡龐"
    "襪褲襯衫裏臺灣華麗衛歷曆廠廳壓壞廣藝體國樂"
)

# 语种元数据（供 SOURCES.md 与清单展示）
LANG_PAIR_META: dict[str, dict[str, str]] = {
    "en_zh": {"label": "英语 → 简体中文", "tokenizer": "porter unicode61 remove_diacritics 2"},
    "zh_en": {"label": "简体中文 → 母语英语", "tokenizer": "trigram"},
    "ja_zh": {"label": "日语 → 简体中文", "tokenizer": "trigram"},
    "ko_zh": {"label": "韩语 → 简体中文", "tokenizer": "trigram"},
    "ru_zh": {"label": "俄语 → 简体中文", "tokenizer": "unicode61 remove_diacritics 2"},
    "es_zh": {"label": "西班牙语 → 简体中文", "tokenizer": "unicode61 remove_diacritics 2"},
}


@dataclass(frozen=True)
class CorpusSourceSpec:
    """单个开源语料源的元数据与流式采集规格。"""

    source_id: str
    name: str
    lang_pair: str
    kind: str  # 'opus_zip' | 'parallel_txt'
    domain: str  # 'spoken' | 'subtitle' | 'expert_ref' | 'encyclopedia'
    license_name: str
    commercial_safe: bool
    homepage: str
    urls: tuple[str, ...]
    src_ext: str
    tgt_ext: str
    max_pairs: int
    default_enabled: bool = True
    description: str = ""
    cache_source_id: str = ""  # 复用其他源的本地缓存文件（例如 zh_en 零重复下载复用 en_zh 缓存）
    reverse_columns: bool = False  # True 时交换源/目标列（用于 zh_en 反向清洗构建）
    max_scan_lines: int = 0  # 0 表示全量扫描；对超大影视包可设扫描上限加速精选


CORPUS_SOURCES: list[CorpusSourceSpec] = [
    # =========================================================================
    # 1. en_zh（英语 → 简体中文 · 旗舰基准，拉齐 100,000 条）
    # =========================================================================
    CorpusSourceSpec(
        source_id="ntrex128_en_zh",
        name="Microsoft NTREX-128 (EN-ZH)",
        lang_pair="en_zh",
        kind="parallel_txt",
        domain="expert_ref",
        license_name="CC BY-SA 4.0",
        commercial_safe=True,
        homepage="https://github.com/MicrosoftTranslator/NTREX",
        urls=(
            "https://raw.githubusercontent.com/MicrosoftTranslator/NTREX/main/NTREX-128/newstest2019-src.eng.txt",
            "https://raw.githubusercontent.com/MicrosoftTranslator/NTREX/main/NTREX-128/newstest2019-ref.zho-CN.txt",
        ),
        src_ext=".eng.txt",
        tgt_ext=".zho-CN.txt",
        max_pairs=2500,
        description="微软官方专业人类译员逐句精校文档级基准译文。",
    ),
    CorpusSourceSpec(
        source_id="tatoeba_en_zh",
        name="OPUS Tatoeba EN-ZH (v2026-07-08)",
        lang_pair="en_zh",
        kind="opus_zip",
        domain="spoken",
        license_name="CC BY 2.0 FR",
        commercial_safe=True,
        homepage="https://tatoeba.org / https://opus.nlpl.eu/Tatoeba.php",
        urls=("https://object.pouta.csc.fi/OPUS-Tatoeba/v2026-07-08/moses/cmn-en.txt.zip",),
        src_ext=".en",
        tgt_ext=".cmn",
        max_pairs=25000,
        description="全球母语者人工精译日常自然口语句对，节奏短促地道。",
    ),
    CorpusSourceSpec(
        source_id="qed_en_zh",
        name="OPUS QED EN-ZH (v2.0a)",
        lang_pair="en_zh",
        kind="opus_zip",
        domain="subtitle",
        license_name="CC BY-SA 4.0",
        commercial_safe=True,
        homepage="https://opus.nlpl.eu/QED.php",
        urls=("https://object.pouta.csc.fi/OPUS-QED/v2.0a/moses/en-zh.txt.zip",),
        src_ext=".en",
        tgt_ext=".zh",
        max_pairs=8000,
        description="开源精校视频演讲与教学双语字幕。",
    ),
    CorpusSourceSpec(
        source_id="wikimatrix_en_zh",
        name="OPUS WikiMatrix EN-ZH (v1)",
        lang_pair="en_zh",
        kind="opus_zip",
        domain="encyclopedia",
        license_name="CC BY-SA 4.0",
        commercial_safe=True,
        homepage="https://opus.nlpl.eu/WikiMatrix.php",
        urls=("https://object.pouta.csc.fi/OPUS-WikiMatrix/v1/moses/en-zh.txt.zip",),
        src_ext=".en",
        tgt_ext=".zh",
        max_pairs=18000,
        max_scan_lines=1_200_000,
        description="精筛多领域百科高对齐短句（补强游戏历史、文化、科技专词与商用无限制语料池）。",
    ),
    CorpusSourceSpec(
        source_id="news_commentary_en_zh",
        name="OPUS News-Commentary EN-ZH (v16)",
        lang_pair="en_zh",
        kind="opus_zip",
        domain="expert_ref",
        license_name="Public Research / News-Commentary",
        commercial_safe=True,
        homepage="https://opus.nlpl.eu/News-Commentary.php",
        urls=("https://object.pouta.csc.fi/OPUS-News-Commentary/v16/moses/en-zh.txt.zip",),
        src_ext=".en",
        tgt_ext=".zh",
        max_pairs=10000,
        description="高质量新闻评论人类专家译文，强化复杂句逻辑意译能力。",
    ),
    CorpusSourceSpec(
        source_id="wmt_news_en_zh",
        name="OPUS WMT-News EN-ZH (v2019)",
        lang_pair="en_zh",
        kind="opus_zip",
        domain="expert_ref",
        license_name="Public Research Benchmark (WMT)",
        commercial_safe=False,
        homepage="https://opus.nlpl.eu/WMT-News.php",
        urls=("https://object.pouta.csc.fi/OPUS-WMT-News/v2019/moses/en-zh.txt.zip",),
        src_ext=".en",
        tgt_ext=".zh",
        max_pairs=5500,
        description="WMT 国际机器翻译大赛人类专家参考译文。",
    ),
    CorpusSourceSpec(
        source_id="ted2020_en_zh",
        name="OPUS TED2020 EN-ZH (v1)",
        lang_pair="en_zh",
        kind="opus_zip",
        domain="subtitle",
        license_name="CC BY-NC-ND 4.0 (TED Talks)",
        commercial_safe=False,
        homepage="https://opus.nlpl.eu/TED2020.php",
        urls=("https://object.pouta.csc.fi/OPUS-TED2020/v1/moses/en-zh_cn.txt.zip",),
        src_ext=".en",
        tgt_ext=".zh_cn",
        max_pairs=24000,
        description="真人字幕组精校 TED 演讲中英口语字幕，极具节奏感与感染力。",
    ),
    CorpusSourceSpec(
        source_id="opensubtitles_en_zh",
        name="OPUS OpenSubtitles EN-ZH (v2018)",
        lang_pair="en_zh",
        kind="opus_zip",
        domain="subtitle",
        license_name="Community Subtitles (OpenSubtitles)",
        commercial_safe=False,
        homepage="https://opus.nlpl.eu/OpenSubtitles.php",
        urls=("https://object.pouta.csc.fi/OPUS-OpenSubtitles/v2018/moses/en-zh_cn.txt.zip",),
        src_ext=".en",
        tgt_ext=".zh_cn",
        max_pairs=42000,
        max_scan_lines=3_000_000,
        description="千万级欧美电影美剧真实口语对话字幕，流式精选最高口语亲和度对白。",
    ),
    # =========================================================================
    # 2. zh_en（简体中文 → 地道母语英语 · 国产游戏/中文主播出海，零重复下载拉齐 100,000 条）
    # =========================================================================
    CorpusSourceSpec(
        source_id="ntrex128_zh_en",
        name="Microsoft NTREX-128 (ZH-EN)",
        lang_pair="zh_en",
        kind="parallel_txt",
        domain="expert_ref",
        license_name="CC BY-SA 4.0",
        commercial_safe=True,
        homepage="https://github.com/MicrosoftTranslator/NTREX",
        urls=(
            "https://raw.githubusercontent.com/MicrosoftTranslator/NTREX/main/NTREX-128/newstest2019-src.eng.txt",
            "https://raw.githubusercontent.com/MicrosoftTranslator/NTREX/main/NTREX-128/newstest2019-ref.zho-CN.txt",
        ),
        src_ext=".eng.txt",
        tgt_ext=".zho-CN.txt",
        max_pairs=2500,
        cache_source_id="ntrex128_en_zh",
        reverse_columns=True,
        description="微软专业译员中→英对齐参考基准。",
    ),
    CorpusSourceSpec(
        source_id="tatoeba_zh_en",
        name="OPUS Tatoeba ZH-EN (v2026-07-08)",
        lang_pair="zh_en",
        kind="opus_zip",
        domain="spoken",
        license_name="CC BY 2.0 FR",
        commercial_safe=True,
        homepage="https://tatoeba.org / https://opus.nlpl.eu/Tatoeba.php",
        urls=("https://object.pouta.csc.fi/OPUS-Tatoeba/v2026-07-08/moses/cmn-en.txt.zip",),
        src_ext=".en",
        tgt_ext=".cmn",
        max_pairs=25000,
        cache_source_id="tatoeba_en_zh",
        reverse_columns=True,
        description="母语级日常中→英自然口语对照。",
    ),
    CorpusSourceSpec(
        source_id="qed_zh_en",
        name="OPUS QED ZH-EN (v2.0a)",
        lang_pair="zh_en",
        kind="opus_zip",
        domain="subtitle",
        license_name="CC BY-SA 4.0",
        commercial_safe=True,
        homepage="https://opus.nlpl.eu/QED.php",
        urls=("https://object.pouta.csc.fi/OPUS-QED/v2.0a/moses/en-zh.txt.zip",),
        src_ext=".en",
        tgt_ext=".zh",
        max_pairs=8000,
        cache_source_id="qed_en_zh",
        reverse_columns=True,
        description="开源精校教学视频中→英字幕。",
    ),
    CorpusSourceSpec(
        source_id="wikimatrix_zh_en",
        name="OPUS WikiMatrix ZH-EN (v1)",
        lang_pair="zh_en",
        kind="opus_zip",
        domain="encyclopedia",
        license_name="CC BY-SA 4.0",
        commercial_safe=True,
        homepage="https://opus.nlpl.eu/WikiMatrix.php",
        urls=("https://object.pouta.csc.fi/OPUS-WikiMatrix/v1/moses/en-zh.txt.zip",),
        src_ext=".en",
        tgt_ext=".zh",
        max_pairs=18000,
        cache_source_id="wikimatrix_en_zh",
        reverse_columns=True,
        max_scan_lines=1_200_000,
        description="多领域中文→地道英文短句。",
    ),
    CorpusSourceSpec(
        source_id="news_commentary_zh_en",
        name="OPUS News-Commentary ZH-EN (v16)",
        lang_pair="zh_en",
        kind="opus_zip",
        domain="expert_ref",
        license_name="Public Research / News-Commentary",
        commercial_safe=True,
        homepage="https://opus.nlpl.eu/News-Commentary.php",
        urls=("https://object.pouta.csc.fi/OPUS-News-Commentary/v16/moses/en-zh.txt.zip",),
        src_ext=".en",
        tgt_ext=".zh",
        max_pairs=10000,
        cache_source_id="news_commentary_en_zh",
        reverse_columns=True,
        description="高质量中→英评论表达范例。",
    ),
    CorpusSourceSpec(
        source_id="wmt_news_zh_en",
        name="OPUS WMT-News ZH-EN (v2019)",
        lang_pair="zh_en",
        kind="opus_zip",
        domain="expert_ref",
        license_name="Public Research Benchmark (WMT)",
        commercial_safe=False,
        homepage="https://opus.nlpl.eu/WMT-News.php",
        urls=("https://object.pouta.csc.fi/OPUS-WMT-News/v2019/moses/en-zh.txt.zip",),
        src_ext=".en",
        tgt_ext=".zh",
        max_pairs=5500,
        cache_source_id="wmt_news_en_zh",
        reverse_columns=True,
        description="WMT 专家中→英基准参考。",
    ),
    CorpusSourceSpec(
        source_id="ted2020_zh_en",
        name="OPUS TED2020 ZH-EN (v1)",
        lang_pair="zh_en",
        kind="opus_zip",
        domain="subtitle",
        license_name="CC BY-NC-ND 4.0 (TED Talks)",
        commercial_safe=False,
        homepage="https://opus.nlpl.eu/TED2020.php",
        urls=("https://object.pouta.csc.fi/OPUS-TED2020/v1/moses/en-zh_cn.txt.zip",),
        src_ext=".en",
        tgt_ext=".zh_cn",
        max_pairs=24000,
        cache_source_id="ted2020_en_zh",
        reverse_columns=True,
        description="TED 演讲中→母语英语地道口语表达。",
    ),
    CorpusSourceSpec(
        source_id="opensubtitles_zh_en",
        name="OPUS OpenSubtitles ZH-EN (v2018)",
        lang_pair="zh_en",
        kind="opus_zip",
        domain="subtitle",
        license_name="Community Subtitles (OpenSubtitles)",
        commercial_safe=False,
        homepage="https://opus.nlpl.eu/OpenSubtitles.php",
        urls=("https://object.pouta.csc.fi/OPUS-OpenSubtitles/v2018/moses/en-zh_cn.txt.zip",),
        src_ext=".en",
        tgt_ext=".zh_cn",
        max_pairs=42000,
        cache_source_id="opensubtitles_en_zh",
        reverse_columns=True,
        max_scan_lines=3_000_000,
        description="影视剧真实对话中→母语级自然英语（彻底消除 Chinglish 翻译腔）。",
    ),
    # =========================================================================
    # 3. ja_zh（日语 → 简体中文 · 必备语种，拉齐 100,000 条）
    # =========================================================================
    CorpusSourceSpec(
        source_id="ntrex128_ja_zh",
        name="Microsoft NTREX-128 (JA-ZH)",
        lang_pair="ja_zh",
        kind="parallel_txt",
        domain="expert_ref",
        license_name="CC BY-SA 4.0",
        commercial_safe=True,
        homepage="https://github.com/MicrosoftTranslator/NTREX",
        urls=(
            "https://raw.githubusercontent.com/MicrosoftTranslator/NTREX/main/NTREX-128/newstest2019-ref.jpn.txt",
            "https://raw.githubusercontent.com/MicrosoftTranslator/NTREX/main/NTREX-128/newstest2019-ref.zho-CN.txt",
        ),
        src_ext=".jpn.txt",
        tgt_ext=".zho-CN.txt",
        max_pairs=2500,
        description="微软多语种人类专家基准对齐的日语→简体中文译文。",
    ),
    CorpusSourceSpec(
        source_id="tatoeba_ja_zh",
        name="OPUS Tatoeba JA-ZH (v2026-07-08)",
        lang_pair="ja_zh",
        kind="opus_zip",
        domain="spoken",
        license_name="CC BY 2.0 FR",
        commercial_safe=True,
        homepage="https://tatoeba.org / https://opus.nlpl.eu/Tatoeba.php",
        urls=("https://object.pouta.csc.fi/OPUS-Tatoeba/v2026-07-08/moses/cmn-ja.txt.zip",),
        src_ext=".ja",
        tgt_ext=".cmn",
        max_pairs=6000,
        description="日语 ↔ 简体中文日常自然口语对照语料。",
    ),
    CorpusSourceSpec(
        source_id="qed_ja_zh",
        name="OPUS QED JA-ZH (v2.0a)",
        lang_pair="ja_zh",
        kind="opus_zip",
        domain="subtitle",
        license_name="CC BY-SA 4.0",
        commercial_safe=True,
        homepage="https://opus.nlpl.eu/QED.php",
        urls=("https://object.pouta.csc.fi/OPUS-QED/v2.0a/moses/ja-zh.txt.zip",),
        src_ext=".ja",
        tgt_ext=".zh",
        max_pairs=8000,
        description="开源精校日语视频讲解双语字幕。",
    ),
    CorpusSourceSpec(
        source_id="wikimatrix_ja_zh",
        name="OPUS WikiMatrix JA-ZH (v1)",
        lang_pair="ja_zh",
        kind="opus_zip",
        domain="encyclopedia",
        license_name="CC BY-SA 4.0",
        commercial_safe=True,
        homepage="https://opus.nlpl.eu/WikiMatrix.php",
        urls=("https://object.pouta.csc.fi/OPUS-WikiMatrix/v1/moses/ja-zh.txt.zip",),
        src_ext=".ja",
        tgt_ext=".zh",
        max_pairs=26000,
        max_scan_lines=1_000_000,
        description="精筛日中多领域（动漫/游戏/文化/生活）高对齐商用友好语料。",
    ),
    CorpusSourceSpec(
        source_id="ted2020_ja_zh",
        name="OPUS TED2020 JA-ZH (v1)",
        lang_pair="ja_zh",
        kind="opus_zip",
        domain="subtitle",
        license_name="CC BY-NC-ND 4.0 (TED Talks)",
        commercial_safe=False,
        homepage="https://opus.nlpl.eu/TED2020.php",
        urls=("https://object.pouta.csc.fi/OPUS-TED2020/v1/moses/ja-zh_cn.txt.zip",),
        src_ext=".ja",
        tgt_ext=".zh_cn",
        max_pairs=35000,
        description="真人字幕组精校 TED 日语→简体中文演讲口语字幕。",
    ),
    CorpusSourceSpec(
        source_id="opensubtitles_ja_zh",
        name="OPUS OpenSubtitles JA-ZH (v2024)",
        lang_pair="ja_zh",
        kind="opus_zip",
        domain="subtitle",
        license_name="Community Subtitles (OpenSubtitles)",
        commercial_safe=False,
        homepage="https://opus.nlpl.eu/OpenSubtitles.php",
        urls=("https://object.pouta.csc.fi/OPUS-OpenSubtitles/v2024/moses/ja-zh_CN.txt.zip",),
        src_ext=".ja",
        tgt_ext=".zh_CN",
        max_pairs=55000,
        max_scan_lines=2_500_000,
        description="海量日剧、日漫与日本电影真实口语字幕，还原日V与日系游戏鲜活语感。",
    ),
    # =========================================================================
    # 4. ko_zh（韩语 → 简体中文 · 必备语种，拉齐 100,000 条）
    # =========================================================================
    CorpusSourceSpec(
        source_id="ntrex128_ko_zh",
        name="Microsoft NTREX-128 (KO-ZH)",
        lang_pair="ko_zh",
        kind="parallel_txt",
        domain="expert_ref",
        license_name="CC BY-SA 4.0",
        commercial_safe=True,
        homepage="https://github.com/MicrosoftTranslator/NTREX",
        urls=(
            "https://raw.githubusercontent.com/MicrosoftTranslator/NTREX/main/NTREX-128/newstest2019-ref.kor.txt",
            "https://raw.githubusercontent.com/MicrosoftTranslator/NTREX/main/NTREX-128/newstest2019-ref.zho-CN.txt",
        ),
        src_ext=".kor.txt",
        tgt_ext=".zho-CN.txt",
        max_pairs=2500,
        description="微软多语种人类专家基准对齐的韩语→简体中文译文。",
    ),
    CorpusSourceSpec(
        source_id="tatoeba_ko_zh",
        name="OPUS Tatoeba KO-ZH (v2026-07-08)",
        lang_pair="ko_zh",
        kind="opus_zip",
        domain="spoken",
        license_name="CC BY 2.0 FR",
        commercial_safe=True,
        homepage="https://tatoeba.org / https://opus.nlpl.eu/Tatoeba.php",
        urls=("https://object.pouta.csc.fi/OPUS-Tatoeba/v2026-07-08/moses/cmn-ko.txt.zip",),
        src_ext=".ko",
        tgt_ext=".cmn",
        max_pairs=2500,
        description="韩语 ↔ 简体中文日常自然口语对照语料。",
    ),
    CorpusSourceSpec(
        source_id="qed_ko_zh",
        name="OPUS QED KO-ZH (v2.0a)",
        lang_pair="ko_zh",
        kind="opus_zip",
        domain="subtitle",
        license_name="CC BY-SA 4.0",
        commercial_safe=True,
        homepage="https://opus.nlpl.eu/QED.php",
        urls=("https://object.pouta.csc.fi/OPUS-QED/v2.0a/moses/ko-zh.txt.zip",),
        src_ext=".ko",
        tgt_ext=".zh",
        max_pairs=8000,
        description="开源精校韩语视频讲解双语字幕。",
    ),
    CorpusSourceSpec(
        source_id="wikimatrix_ko_zh",
        name="OPUS WikiMatrix KO-ZH (v1)",
        lang_pair="ko_zh",
        kind="opus_zip",
        domain="encyclopedia",
        license_name="CC BY-SA 4.0",
        commercial_safe=True,
        homepage="https://opus.nlpl.eu/WikiMatrix.php",
        urls=("https://object.pouta.csc.fi/OPUS-WikiMatrix/v1/moses/ko-zh.txt.zip",),
        src_ext=".ko",
        tgt_ext=".zh",
        max_pairs=20000,
        max_scan_lines=800_000,
        description="精筛韩中多领域高对齐商用友好语料。",
    ),
    CorpusSourceSpec(
        source_id="ted2020_ko_zh",
        name="OPUS TED2020 KO-ZH (v1)",
        lang_pair="ko_zh",
        kind="opus_zip",
        domain="subtitle",
        license_name="CC BY-NC-ND 4.0 (TED Talks)",
        commercial_safe=False,
        homepage="https://opus.nlpl.eu/TED2020.php",
        urls=("https://object.pouta.csc.fi/OPUS-TED2020/v1/moses/ko-zh_cn.txt.zip",),
        src_ext=".ko",
        tgt_ext=".zh_cn",
        max_pairs=32000,
        description="真人字幕组精校 TED 韩语→简体中文演讲口语字幕。",
    ),
    CorpusSourceSpec(
        source_id="opensubtitles_ko_zh",
        name="OPUS OpenSubtitles KO-ZH (v2024)",
        lang_pair="ko_zh",
        kind="opus_zip",
        domain="subtitle",
        license_name="Community Subtitles (OpenSubtitles)",
        commercial_safe=False,
        homepage="https://opus.nlpl.eu/OpenSubtitles.php",
        urls=("https://object.pouta.csc.fi/OPUS-OpenSubtitles/v2024/moses/ko-zh_CN.txt.zip",),
        src_ext=".ko",
        tgt_ext=".zh_CN",
        max_pairs=62000,
        max_scan_lines=3_000_000,
        description="超大规模韩剧、韩综与韩国电影口语字幕，精准还原韩服主播半语/敬语语气。",
    ),
    # =========================================================================
    # 5. ru_zh（俄语 → 简体中文 · 国内 CS2/Dota2/塔科夫爆款增补，拉齐 100,000 条）
    # =========================================================================
    CorpusSourceSpec(
        source_id="ntrex128_ru_zh",
        name="Microsoft NTREX-128 (RU-ZH)",
        lang_pair="ru_zh",
        kind="parallel_txt",
        domain="expert_ref",
        license_name="CC BY-SA 4.0",
        commercial_safe=True,
        homepage="https://github.com/MicrosoftTranslator/NTREX",
        urls=(
            "https://raw.githubusercontent.com/MicrosoftTranslator/NTREX/main/NTREX-128/newstest2019-ref.rus.txt",
            "https://raw.githubusercontent.com/MicrosoftTranslator/NTREX/main/NTREX-128/newstest2019-ref.zho-CN.txt",
        ),
        src_ext=".rus.txt",
        tgt_ext=".zho-CN.txt",
        max_pairs=2500,
        description="微软多语种人类专家基准对齐的俄语→简体中文译文。",
    ),
    CorpusSourceSpec(
        source_id="tatoeba_ru_zh",
        name="OPUS Tatoeba RU-ZH (v2026-07-08)",
        lang_pair="ru_zh",
        kind="opus_zip",
        domain="spoken",
        license_name="CC BY 2.0 FR",
        commercial_safe=True,
        homepage="https://tatoeba.org / https://opus.nlpl.eu/Tatoeba.php",
        urls=("https://object.pouta.csc.fi/OPUS-Tatoeba/v2026-07-08/moses/cmn-ru.txt.zip",),
        src_ext=".ru",
        tgt_ext=".cmn",
        max_pairs=5000,
        description="俄语 ↔ 简体中文日常自然口语对照语料。",
    ),
    CorpusSourceSpec(
        source_id="qed_ru_zh",
        name="OPUS QED RU-ZH (v2.0a)",
        lang_pair="ru_zh",
        kind="opus_zip",
        domain="subtitle",
        license_name="CC BY-SA 4.0",
        commercial_safe=True,
        homepage="https://opus.nlpl.eu/QED.php",
        urls=("https://object.pouta.csc.fi/OPUS-QED/v2.0a/moses/ru-zh.txt.zip",),
        src_ext=".ru",
        tgt_ext=".zh",
        max_pairs=8000,
        description="开源精校俄语视频讲解双语字幕。",
    ),
    CorpusSourceSpec(
        source_id="wikimatrix_ru_zh",
        name="OPUS WikiMatrix RU-ZH (v1)",
        lang_pair="ru_zh",
        kind="opus_zip",
        domain="encyclopedia",
        license_name="CC BY-SA 4.0",
        commercial_safe=True,
        homepage="https://opus.nlpl.eu/WikiMatrix.php",
        urls=("https://object.pouta.csc.fi/OPUS-WikiMatrix/v1/moses/ru-zh.txt.zip",),
        src_ext=".ru",
        tgt_ext=".zh",
        max_pairs=20000,
        max_scan_lines=900_000,
        description="精筛俄中多领域高对齐商用友好语料。",
    ),
    CorpusSourceSpec(
        source_id="news_commentary_ru_zh",
        name="OPUS News-Commentary RU-ZH (v16)",
        lang_pair="ru_zh",
        kind="opus_zip",
        domain="expert_ref",
        license_name="Public Research / News-Commentary",
        commercial_safe=True,
        homepage="https://opus.nlpl.eu/News-Commentary.php",
        urls=("https://object.pouta.csc.fi/OPUS-News-Commentary/v16/moses/ru-zh.txt.zip",),
        src_ext=".ru",
        tgt_ext=".zh",
        max_pairs=12000,
        description="高质量俄中新闻评论专家译文。",
    ),
    CorpusSourceSpec(
        source_id="ted2020_ru_zh",
        name="OPUS TED2020 RU-ZH (v1)",
        lang_pair="ru_zh",
        kind="opus_zip",
        domain="subtitle",
        license_name="CC BY-NC-ND 4.0 (TED Talks)",
        commercial_safe=False,
        homepage="https://opus.nlpl.eu/TED2020.php",
        urls=("https://object.pouta.csc.fi/OPUS-TED2020/v1/moses/ru-zh_cn.txt.zip",),
        src_ext=".ru",
        tgt_ext=".zh_cn",
        max_pairs=28000,
        description="真人字幕组精校 TED 俄语→简体中文演讲字幕。",
    ),
    CorpusSourceSpec(
        source_id="opensubtitles_ru_zh",
        name="OPUS OpenSubtitles RU-ZH (v2018)",
        lang_pair="ru_zh",
        kind="opus_zip",
        domain="subtitle",
        license_name="Community Subtitles (OpenSubtitles)",
        commercial_safe=False,
        homepage="https://opus.nlpl.eu/OpenSubtitles.php",
        urls=("https://object.pouta.csc.fi/OPUS-OpenSubtitles/v2018/moses/ru-zh_cn.txt.zip",),
        src_ext=".ru",
        tgt_ext=".zh_cn",
        max_pairs=52000,
        max_scan_lines=2_500_000,
        description="海量俄语影视真实对话字幕，完美支撑 CS2/Dota2/塔科夫俄区直播切片。",
    ),
    # =========================================================================
    # 6. es_zh（西班牙语 → 简体中文 · Twitch 全球第二大语种蓝海，拉齐 100,000 条）
    # =========================================================================
    CorpusSourceSpec(
        source_id="ntrex128_es_zh",
        name="Microsoft NTREX-128 (ES-ZH)",
        lang_pair="es_zh",
        kind="parallel_txt",
        domain="expert_ref",
        license_name="CC BY-SA 4.0",
        commercial_safe=True,
        homepage="https://github.com/MicrosoftTranslator/NTREX",
        urls=(
            "https://raw.githubusercontent.com/MicrosoftTranslator/NTREX/main/NTREX-128/newstest2019-ref.spa.txt",
            "https://raw.githubusercontent.com/MicrosoftTranslator/NTREX/main/NTREX-128/newstest2019-ref.zho-CN.txt",
        ),
        src_ext=".spa.txt",
        tgt_ext=".zho-CN.txt",
        max_pairs=2500,
        description="微软多语种人类专家基准对齐的西语→简体中文译文。",
    ),
    CorpusSourceSpec(
        source_id="tatoeba_es_zh",
        name="OPUS Tatoeba ES-ZH (v2026-07-08)",
        lang_pair="es_zh",
        kind="opus_zip",
        domain="spoken",
        license_name="CC BY 2.0 FR",
        commercial_safe=True,
        homepage="https://tatoeba.org / https://opus.nlpl.eu/Tatoeba.php",
        urls=("https://object.pouta.csc.fi/OPUS-Tatoeba/v2026-07-08/moses/cmn-es.txt.zip",),
        src_ext=".es",
        tgt_ext=".cmn",
        max_pairs=5000,
        description="西班牙语 ↔ 简体中文日常自然口语对照语料。",
    ),
    CorpusSourceSpec(
        source_id="qed_es_zh",
        name="OPUS QED ES-ZH (v2.0a)",
        lang_pair="es_zh",
        kind="opus_zip",
        domain="subtitle",
        license_name="CC BY-SA 4.0",
        commercial_safe=True,
        homepage="https://opus.nlpl.eu/QED.php",
        urls=("https://object.pouta.csc.fi/OPUS-QED/v2.0a/moses/es-zh.txt.zip",),
        src_ext=".es",
        tgt_ext=".zh",
        max_pairs=8000,
        description="开源精校西语视频讲解双语字幕。",
    ),
    CorpusSourceSpec(
        source_id="wikimatrix_es_zh",
        name="OPUS WikiMatrix ES-ZH (v1)",
        lang_pair="es_zh",
        kind="opus_zip",
        domain="encyclopedia",
        license_name="CC BY-SA 4.0",
        commercial_safe=True,
        homepage="https://opus.nlpl.eu/WikiMatrix.php",
        urls=("https://object.pouta.csc.fi/OPUS-WikiMatrix/v1/moses/es-zh.txt.zip",),
        src_ext=".es",
        tgt_ext=".zh",
        max_pairs=20000,
        max_scan_lines=900_000,
        description="精筛西中多领域高对齐商用友好语料。",
    ),
    CorpusSourceSpec(
        source_id="news_commentary_es_zh",
        name="OPUS News-Commentary ES-ZH (v16)",
        lang_pair="es_zh",
        kind="opus_zip",
        domain="expert_ref",
        license_name="Public Research / News-Commentary",
        commercial_safe=True,
        homepage="https://opus.nlpl.eu/News-Commentary.php",
        urls=("https://object.pouta.csc.fi/OPUS-News-Commentary/v16/moses/es-zh.txt.zip",),
        src_ext=".es",
        tgt_ext=".zh",
        max_pairs=12000,
        description="高质量西中新闻评论专家译文。",
    ),
    CorpusSourceSpec(
        source_id="ted2020_es_zh",
        name="OPUS TED2020 ES-ZH (v1)",
        lang_pair="es_zh",
        kind="opus_zip",
        domain="subtitle",
        license_name="CC BY-NC-ND 4.0 (TED Talks)",
        commercial_safe=False,
        homepage="https://opus.nlpl.eu/TED2020.php",
        urls=("https://object.pouta.csc.fi/OPUS-TED2020/v1/moses/es-zh_cn.txt.zip",),
        src_ext=".es",
        tgt_ext=".zh_cn",
        max_pairs=28000,
        description="真人字幕组精校 TED 西语→简体中文演讲字幕。",
    ),
    CorpusSourceSpec(
        source_id="opensubtitles_es_zh",
        name="OPUS OpenSubtitles ES-ZH (v2018)",
        lang_pair="es_zh",
        kind="opus_zip",
        domain="subtitle",
        license_name="Community Subtitles (OpenSubtitles)",
        commercial_safe=False,
        homepage="https://opus.nlpl.eu/OpenSubtitles.php",
        urls=("https://object.pouta.csc.fi/OPUS-OpenSubtitles/v2018/moses/es-zh_cn.txt.zip",),
        src_ext=".es",
        tgt_ext=".zh_cn",
        max_pairs=52000,
        max_scan_lines=2_500_000,
        description="海量西语/拉美影视真实对话字幕，适配 Twitch 西语区顶流主播切片。",
    ),
]


def _normalize_text(text: str) -> str:
    """清理 HTML 实体、字幕标签、乐符噪音与多余空白。"""
    s = html.unescape(str(text or ""))
    s = _HTML_TAG_RE.sub(" ", s)
    s = _STAGE_NOTE_RE.sub(" ", s)
    s = s.replace("\u3000", " ").replace("\ufeff", "").replace("♪", "").replace("♫", "")
    s = re.sub(r"^\s*[-–—•·*#]+\s*", "", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def _normalize_zh_text(text: str) -> str:
    """规范化简体中文文本（移除汉字之间异常空格，保留英文单词空格）。"""
    s = _normalize_text(text)
    s = re.sub(r"(?<=[\u4e00-\u9fff])\s+(?=[\u4e00-\u9fff])", "", s)
    return s.strip()


def _normalize_ja_text(text: str) -> str:
    """规范化日语文本（移除日文假名/汉字之间异常空格）。"""
    s = _normalize_text(text)
    s = re.sub(r"(?<=[\u3040-\u30ff\u4e00-\u9fff])\s+(?=[\u3040-\u30ff\u4e00-\u9fff])", "", s)
    return s.strip()


def _is_clean_simplified_zh(text: str) -> tuple[bool, int]:
    """校验中文端是否纯粹、无繁体污染且字数合适，返回 (是否通过, 汉字数)。"""
    if _JP_KANA_RE.search(text) or _KR_HANGUL_RE.search(text) or _CYRILLIC_RE.search(text):
        return False, 0
    cjk_chars = _CJK_RE.findall(text)
    cjk_len = len(cjk_chars)
    if cjk_len < 4 or cjk_len > 85:
        return False, 0
    if (cjk_len / max(1, len(text))) < 0.55:
        return False, 0
    if any(ch in _TRADITIONAL_ONLY_CHARS for ch in cjk_chars):
        return False, 0
    return True, cjk_len


def clean_and_score_pair(
    src_text: str,
    tgt_text: str,
    lang_pair: str = "en_zh",
    domain: str = "spoken",
) -> tuple[str, str, float] | None:
    """按语种对执行严苛母语清洗与口语/字幕亲和度打分，不合格返回 None。"""
    if lang_pair == "zh_en":
        src = _normalize_zh_text(src_text)
        tgt = _normalize_text(tgt_text)
    elif lang_pair == "ja_zh":
        src = _normalize_ja_text(src_text)
        tgt = _normalize_zh_text(tgt_text)
    else:
        src = _normalize_text(src_text)
        tgt = _normalize_zh_text(tgt_text)

    if not src or not tgt or src.lower() == tgt.lower():
        return None

    # 1. 通用负面清洗：水印、网址、露骨低俗、过度数字表格
    if _WATERMARK_RE.search(src) or _WATERMARK_RE.search(tgt):
        return None
    if _EXPLICIT_FILTER_RE.search(src) or _EXPLICIT_FILTER_RE.search(tgt):
        return None
    if domain == "encyclopedia":
        # WikiMatrix 严苛去噪：剔除年份/数据堆砌句与超长括号百科词条
        digit_ratio = len(_DIGIT_RE.findall(src)) / max(1, len(src))
        if digit_ratio > 0.18 or "(" in src and len(src) > 95:
            return None

    score = 1.0

    # 2. 按语种对执行专属对齐与母语自然度门控
    if lang_pair == "en_zh":
        ok_zh, cjk_len = _is_clean_simplified_zh(tgt)
        if not ok_zh:
            return None
        words = _EN_WORD_RE.findall(src)
        word_cnt = len(words)
        if word_cnt < 3 or word_cnt > 36 or len(src) > 210:
            return None
        if _CJK_RE.search(src) or _CYRILLIC_RE.search(src):
            return None
        ratio = cjk_len / float(word_cnt)
        if ratio < 0.65 or ratio > 3.60:
            return None
        if 5 <= word_cnt <= 20:
            score += 0.45
        if 1.1 <= ratio <= 2.5:
            score += 0.35
        if _COLLOQUIAL_EN_RE.search(src):
            score += 0.55
        if any(p in src for p in ("?", "!")):
            score += 0.30
        if _COLLOQUIAL_ZH_RE.search(tgt):
            score += 0.30
        if _TEXTBOOK_NAME_RE.search(src):
            score -= 0.55

    elif lang_pair == "zh_en":
        # 简体中文 → 地道母语级英语
        ok_zh, cjk_len = _is_clean_simplified_zh(src)
        if not ok_zh or cjk_len > 65:
            return None
        words = _EN_WORD_RE.findall(tgt)
        word_cnt = len(words)
        if word_cnt < 3 or word_cnt > 36 or len(tgt) > 210:
            return None
        if _CJK_RE.search(tgt) or _CYRILLIC_RE.search(tgt):
            return None
        ratio = cjk_len / float(word_cnt)
        if ratio < 0.65 or ratio > 3.60:
            return None
        if 5 <= word_cnt <= 18:
            score += 0.45
        if 1.1 <= ratio <= 2.5:
            score += 0.35
        # 中译英优先嘉奖英文端极其地道口语化、有缩略词与情绪张力的母语表达
        if _COLLOQUIAL_EN_RE.search(tgt):
            score += 0.65
        if any(p in tgt for p in ("?", "!")):
            score += 0.30
        if _COLLOQUIAL_ZH_RE.search(src):
            score += 0.30
        if _TEXTBOOK_NAME_RE.search(tgt):
            score -= 0.55

    elif lang_pair == "ja_zh":
        ok_zh, cjk_len = _is_clean_simplified_zh(tgt)
        if not ok_zh:
            return None
        # 日语源端必须包含至少 2 个假名（防止中文错行冒充日语）且日文字符占比足够高
        kana_cnt = len(_JP_KANA_RE.findall(src))
        jp_cnt = len(_JP_ALL_RE.findall(src))
        if kana_cnt < 2 or jp_cnt < 5 or len(src) > 100:
            return None
        if (jp_cnt / max(1, len(src))) < 0.58:
            return None
        if _KR_HANGUL_RE.search(src) or _CYRILLIC_RE.search(src):
            return None
        ratio = cjk_len / float(jp_cnt)
        if ratio < 0.32 or ratio > 1.75:
            return None
        if 7 <= jp_cnt <= 34:
            score += 0.45
        if 0.48 <= ratio <= 1.15:
            score += 0.35
        if _COLLOQUIAL_JA_RE.search(src):
            score += 0.55
        if _COLLOQUIAL_ZH_RE.search(tgt):
            score += 0.30

    elif lang_pair == "ko_zh":
        ok_zh, cjk_len = _is_clean_simplified_zh(tgt)
        if not ok_zh:
            return None
        hangul_cnt = len(_KR_HANGUL_RE.findall(src))
        if hangul_cnt < 4 or len(src) > 110:
            return None
        if (hangul_cnt / max(1, len(src))) < 0.48:
            return None
        if _CJK_RE.search(src) or _JP_KANA_RE.search(src) or _CYRILLIC_RE.search(src):
            return None
        ratio = cjk_len / float(hangul_cnt)
        if ratio < 0.38 or ratio > 2.20:
            return None
        if 6 <= hangul_cnt <= 32:
            score += 0.45
        if 0.55 <= ratio <= 1.45:
            score += 0.35
        if _COLLOQUIAL_KO_RE.search(src):
            score += 0.55
        if _COLLOQUIAL_ZH_RE.search(tgt):
            score += 0.30

    elif lang_pair == "ru_zh":
        ok_zh, cjk_len = _is_clean_simplified_zh(tgt)
        if not ok_zh:
            return None
        cyr_cnt = len(_CYRILLIC_RE.findall(src))
        ru_words = _RU_WORD_RE.findall(src)
        word_cnt = len(ru_words)
        if word_cnt < 3 or word_cnt > 32 or len(src) > 210:
            return None
        if (cyr_cnt / max(1, len(src))) < 0.55:
            return None
        if _CJK_RE.search(src) or _JP_KANA_RE.search(src):
            return None
        ratio = cjk_len / float(word_cnt)
        if ratio < 0.75 or ratio > 4.20:
            return None
        if 4 <= word_cnt <= 18:
            score += 0.45
        if 1.2 <= ratio <= 2.8:
            score += 0.35
        if _COLLOQUIAL_RU_RE.search(src):
            score += 0.55
        if _COLLOQUIAL_ZH_RE.search(tgt):
            score += 0.30

    elif lang_pair == "es_zh":
        ok_zh, cjk_len = _is_clean_simplified_zh(tgt)
        if not ok_zh:
            return None
        words = _LATIN_WORD_RE.findall(src)
        word_cnt = len(words)
        if word_cnt < 3 or word_cnt > 36 or len(src) > 210:
            return None
        if _CJK_RE.search(src) or _CYRILLIC_RE.search(src) or _JP_KANA_RE.search(src):
            return None
        # 确认源端确为西语而非误混的英文句
        if not _ES_MARKER_RE.search(src):
            return None
        ratio = cjk_len / float(word_cnt)
        if ratio < 0.60 or ratio > 3.60:
            return None
        if 5 <= word_cnt <= 20:
            score += 0.45
        if 1.0 <= ratio <= 2.4:
            score += 0.35
        if _COLLOQUIAL_ES_RE.search(src):
            score += 0.55
        if any(p in src for p in ("?", "!", "¿", "¡")):
            score += 0.30
        if _COLLOQUIAL_ZH_RE.search(tgt):
            score += 0.30

    else:
        ok_zh, cjk_len = _is_clean_simplified_zh(tgt)
        if not ok_zh or len(src) < 3 or len(src) > 120:
            return None
        ratio = cjk_len / float(len(src))
        if ratio < 0.25 or ratio > 3.0:
            return None

    # 3. 领域权重：真人日常口语 > 影视/演讲字幕 > 人类专家基准 > 百科精筛
    if domain == "spoken":
        score += 0.40
    elif domain == "subtitle":
        score += 0.32
    elif domain == "expert_ref":
        score += 0.24
    elif domain == "encyclopedia":
        score += 0.12

    return src, tgt, round(score, 3)


def _download_with_cache(
    url: str,
    cache_path: Path,
    retries: int = 3,
) -> Path:
    """带本地缓存、断点安全临时文件与自动重试的流式下载器。"""
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    if cache_path.is_file() and cache_path.stat().st_size > 128:
        return cache_path

    tmp_path = cache_path.with_suffix(cache_path.suffix + ".part")
    last_exc: Exception | None = None
    timeout = httpx.Timeout(connect=30.0, read=120.0, write=30.0, pool=30.0)

    for attempt in range(1, retries + 1):
        try:
            with httpx.Client(timeout=timeout, follow_redirects=True) as client:
                with client.stream("GET", url) as resp:
                    resp.raise_for_status()
                    total_bytes = int(resp.headers.get("content-length") or 0)
                    downloaded = 0
                    last_pct = -1
                    with tmp_path.open("wb") as fh:
                        for chunk in resp.iter_bytes(chunk_size=262144):
                            if not chunk:
                                continue
                            fh.write(chunk)
                            downloaded += len(chunk)
                            if total_bytes >= 20 * 1024 * 1024:
                                pct = int(downloaded * 100 / total_bytes)
                                if pct >= last_pct + 25:
                                    last_pct = pct
                                    print(
                                        f"    [下载进度] {cache_path.name}: {pct}% "
                                        f"({downloaded / 1048576:.1f} / {total_bytes / 1048576:.1f} MB)",
                                        flush=True,
                                    )
            tmp_path.replace(cache_path)
            return cache_path
        except Exception as exc:
            last_exc = exc
            if tmp_path.exists():
                try:
                    tmp_path.unlink()
                except OSError:
                    pass
            if attempt < retries:
                wait_sec = attempt * 3
                print(
                    f"    [重试 {attempt}/{retries}] 下载失败 ({exc})，{wait_sec}s 后重试...",
                    file=sys.stderr,
                    flush=True,
                )
                time.sleep(wait_sec)

    raise RuntimeError(f"下载失败（重试 {retries} 次）: {url} -> {last_exc}")


def _resolve_zip_member(names: list[str], target_ext: str) -> str | None:
    """容错定位 OPUS 压缩包中的语言文件（兼容大小写与目录前缀，跳过 README/LICENSE/.ids）。"""
    clean_names = [
        n
        for n in names
        if not n.endswith("/")
        and not n.lower().endswith((".ids", "readme", "license", ".xml", ".md"))
    ]
    exact = next((n for n in clean_names if n.endswith(target_ext)), None)
    if exact:
        return exact
    low_ext = target_ext.lower()
    return next((n for n in clean_names if n.lower().endswith(low_ext)), None)


def _iter_raw_pairs_stream(
    spec: CorpusSourceSpec,
    raw_cache_dir: Path,
) -> Iterator[tuple[str, str]]:
    """流式产出单个语料源的原始双语句对，绝不将千万行一次性读入内存。"""
    cache_prefix = spec.cache_source_id or spec.source_id

    if spec.kind == "opus_zip":
        url = spec.urls[0]
        zip_name = f"{cache_prefix}_{Path(url).name}"
        zip_path = _download_with_cache(url, raw_cache_dir / zip_name)
        with zipfile.ZipFile(zip_path, "r") as zf:
            names = zf.namelist()
            src_member = _resolve_zip_member(names, spec.src_ext)
            tgt_member = _resolve_zip_member(names, spec.tgt_ext)
            if not src_member or not tgt_member:
                raise RuntimeError(
                    f"压缩包 {zip_name} 中未找到匹配后缀 {spec.src_ext}/{spec.tgt_ext}，实际文件：{names}"
                )
            with (
                io.TextIOWrapper(zf.open(src_member), encoding="utf-8", errors="ignore") as f_src,
                io.TextIOWrapper(zf.open(tgt_member), encoding="utf-8", errors="ignore") as f_tgt,
            ):
                for s_line, t_line in zip(f_src, f_tgt):
                    if spec.reverse_columns:
                        yield t_line, s_line
                    else:
                        yield s_line, t_line

    elif spec.kind == "parallel_txt":
        src_url, tgt_url = spec.urls[0], spec.urls[1]
        src_file = _download_with_cache(src_url, raw_cache_dir / f"{cache_prefix}_src.txt")
        tgt_file = _download_with_cache(tgt_url, raw_cache_dir / f"{cache_prefix}_tgt.txt")
        with (
            src_file.open("r", encoding="utf-8", errors="ignore") as f_src,
            tgt_file.open("r", encoding="utf-8", errors="ignore") as f_tgt,
        ):
            for s_line, t_line in zip(f_src, f_tgt):
                if spec.reverse_columns:
                    yield t_line, s_line
                else:
                    yield s_line, t_line
    else:
        raise ValueError(f"不支持的数据源类型：{spec.kind}")


def _load_pairs_from_source(
    spec: CorpusSourceSpec,
    raw_cache_dir: Path,
) -> list[dict[str, Any]]:
    """流式低内存清洗并使用固定容量最小堆精选 Top-K 最高口语分句对。"""
    cap = max(1, int(spec.max_pairs))
    # 最小堆元素：(quality_score, -len(src), seq_id, dedup_key, src, tgt)
    min_heap: list[tuple[float, int, int, str, str, str]] = []
    in_heap_keys: set[str] = set()
    seq = 0
    scanned = 0

    for s_raw, t_raw in _iter_raw_pairs_stream(spec, raw_cache_dir):
        scanned += 1
        if spec.max_scan_lines > 0 and scanned > spec.max_scan_lines:
            break

        res = clean_and_score_pair(s_raw, t_raw, lang_pair=spec.lang_pair, domain=spec.domain)
        if res is None:
            continue
        s_clean, t_clean, q_score = res
        dedup_key = s_clean.lower()
        if dedup_key in in_heap_keys:
            continue

        rank_tuple = (q_score, -len(s_clean))
        if len(min_heap) < cap:
            seq += 1
            in_heap_keys.add(dedup_key)
            heapq.heappush(min_heap, (rank_tuple[0], rank_tuple[1], seq, dedup_key, s_clean, t_clean))
        else:
            top_score, top_neg_len = min_heap[0][0], min_heap[0][1]
            if rank_tuple > (top_score, top_neg_len):
                seq += 1
                popped = heapq.heappushpop(
                    min_heap,
                    (rank_tuple[0], rank_tuple[1], seq, dedup_key, s_clean, t_clean),
                )
                in_heap_keys.discard(popped[3])
                in_heap_keys.add(dedup_key)

    # 按质量降序、短句优先输出
    min_heap.sort(key=lambda item: (-item[0], -item[1], item[2]))
    return [
        {
            "src": item[4],
            "tgt": item[5],
            "lang_pair": spec.lang_pair,
            "source": spec.source_id,
            "source_name": spec.name,
            "domain": spec.domain,
            "license": spec.license_name,
            "commercial_safe": spec.commercial_safe,
            "quality_score": item[0],
        }
        for item in min_heap
    ]


def get_tokenizer_for_lang_pair(lang_pair: str) -> str:
    """按语种对返回最优 SQLite FTS5 分词器（CJK 无空格语种使用原生 trigram）。"""
    meta = LANG_PAIR_META.get(lang_pair)
    if meta:
        return meta["tokenizer"]
    return "porter unicode61 remove_diacritics 2"


def build_fts_index(
    jsonl_path: Path,
    db_path: Path,
    lang_pair: str = "en_zh",
) -> int:
    """将清洗后的平行语料 JSONL 构建为零依赖 SQLite FTS5 BM25 高速检索库。"""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_db = db_path.with_suffix(".db.tmp")
    if tmp_db.exists():
        tmp_db.unlink()

    tokenizer = get_tokenizer_for_lang_pair(lang_pair)
    conn = sqlite3.connect(str(tmp_db))
    try:
        conn.execute("PRAGMA journal_mode = OFF;")
        conn.execute("PRAGMA synchronous = OFF;")
        conn.execute(
            """
            CREATE TABLE corpus_meta (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )
            """
        )
        conn.execute(
            "INSERT INTO corpus_meta (key, value) VALUES (?, ?), (?, ?)",
            ("lang_pair", lang_pair, "tokenizer", tokenizer),
        )
        conn.execute(
            """
            CREATE TABLE pairs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                src TEXT NOT NULL,
                tgt TEXT NOT NULL,
                lang_pair TEXT NOT NULL,
                source TEXT NOT NULL,
                domain TEXT NOT NULL,
                license TEXT NOT NULL,
                commercial_safe INTEGER NOT NULL,
                quality_score REAL NOT NULL
            )
            """
        )
        # 仅索引源语言列 src（tgt 设为 UNINDEXED），可大幅压缩索引体积并杜绝 BM25 误命中译文列
        conn.execute(
            f"""
            CREATE VIRTUAL TABLE pairs_fts USING fts5(
                src,
                tgt UNINDEXED,
                content='pairs',
                content_rowid='id',
                tokenize='{tokenizer}'
            )
            """
        )

        rows: list[tuple[Any, ...]] = []
        count = 0
        with jsonl_path.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                obj = json.loads(line)
                rows.append(
                    (
                        obj["src"],
                        obj["tgt"],
                        obj.get("lang_pair", lang_pair),
                        obj.get("source", ""),
                        obj.get("domain", "spoken"),
                        obj.get("license", ""),
                        1 if obj.get("commercial_safe", False) else 0,
                        float(obj.get("quality_score", 1.0)),
                    )
                )
                count += 1
                if len(rows) >= 10000:
                    conn.executemany(
                        "INSERT INTO pairs (src, tgt, lang_pair, source, domain, license, commercial_safe, quality_score) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                        rows,
                    )
                    rows.clear()
        if rows:
            conn.executemany(
                "INSERT INTO pairs (src, tgt, lang_pair, source, domain, license, commercial_safe, quality_score) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                rows,
            )

        conn.execute("INSERT INTO pairs_fts(pairs_fts) VALUES('rebuild');")
        conn.execute("INSERT INTO pairs_fts(pairs_fts) VALUES('optimize');")
        conn.commit()
    finally:
        conn.close()

    tmp_db.replace(db_path)
    return count


def _write_sources_markdown(
    corpus_root: Path,
    manifest: dict[str, Any],
) -> Path:
    """自动生成多语种版权与来源透明清单 data/corpus/SOURCES.md。"""
    md_path = corpus_root / "SOURCES.md"
    lines: list[str] = [
        "# Solacut 多语种顶级双语语料与母语专家词库清单",
        "",
        "本目录汇总了六大核心语种对（英→中、中→英、日→中、韩→中、俄→中、西→中）的高品质平行语料及原创游戏/直播专家词典，"
        "驱动本地零依赖毫秒级翻译记忆（TM）与动态 Few-Shot 母语意译检索（`src/corpus_rag.py`）。",
        "",
        "## 一、内建原创商业级多语种专家词库（MIT 许可，100% 商用无风险）",
        "",
        "- `en_zh/`：欧美游戏/电竞/Twitch 直播术语 + ASR 误听纠错 + 英语习语反直译红线",
        "- `zh_en/`：国产大作与中文直播黑话出海术语 + 中文热梗去 Chinglish 反直译红线",
        "- `ja_zh/`：日 V / Hololive / 彩虹社 / 日系二游动作术语 + 日语 ASR 误听 + 日语口语反直译红线",
        "- `ko_zh/`：LCK / 韩服电竞 / SOOP·CHZZK 直播术语 + 韩语 ASR 误听 + 韩语半语敬语反直译红线",
        "- `ru_zh/`：CS2 / Dota2 / 塔科夫俄区电竞术语 + 俄语口语与情绪词自然意译红线",
        "- `es_zh/`：Twitch 西语/拉美顶流主播与电竞术语 + 板鸭/拉美口语反直译红线",
        "- `user_glossary.json`：公会与主播专属自定义词库（最高优先级，支持在工作台实时热更新）",
        "",
        "## 二、各语种平行语料规模总览",
        "",
        "| 语种对 | 语言方向 | 总精选句对 | 商用无限制许可 | FTS5 分词器 |",
        "| :---: | :--- | :---: | :---: | :--- |",
    ]
    lang_pairs_map = manifest.get("lang_pairs", {})
    total_all = 0
    total_comm = 0
    for lp in ALL_LANG_PAIRS:
        info = lang_pairs_map.get(lp)
        if not info:
            continue
        cnt = int(info.get("total_pairs", 0))
        comm = int(info.get("commercial_safe_pairs", 0))
        total_all += cnt
        total_comm += comm
        label = LANG_PAIR_META.get(lp, {}).get("label", lp)
        tok = info.get("tokenizer") or get_tokenizer_for_lang_pair(lp)
        lines.append(f"| `{lp}` | {label} | **{cnt:,}** | {comm:,} | `{tok}` |")
    lines.append(f"| **合计** | **全 6 大语种方向** | **{total_all:,}** | **{total_comm:,}** | — |")

    lines.extend(
        [
            "",
            "## 三、开源社区平行语料接入明细与许可协议",
            "",
            "| 语种对 | 数据源 | 领域 | 精选清洗条数 | 许可协议 | 商用友好 | 官方主页 |",
            "| :--- | :--- | :---: | :---: | :--- | :---: | :--- |",
        ]
    )
    for lp, info in sorted(lang_pairs_map.items()):
        for src in info.get("sources", []):
            safe_str = "是" if src.get("commercial_safe") else "仅研究/选配"
            lines.append(
                f"| `{lp}` | **{src['name']}** | `{src['domain']}` | "
                f"{src['kept_pairs']:,} | `{src['license']}` | {safe_str} | {src['homepage']} |"
            )

    lines.extend(
        [
            "",
            "> **商业化合规提示**：若用于对外商业分发且需严格限定仅使用商用无限制开源许可（MIT / CC BY 2.0 / CC BY-SA 4.0），"
            "可在构建时附加 `--commercial-only` 或在环境变量中设置 `CORPUS_COMMERCIAL_ONLY=1`，系统将自动仅检索标注为“商用友好=是”的语料。",
            "",
        ]
    )
    md_path.write_text("\n".join(lines), encoding="utf-8")
    return md_path


def build_corpus_for_lang_pair(
    lang_pair: str = "en_zh",
    corpus_root: Path = DEFAULT_CORPUS_ROOT,
    commercial_only: bool = False,
    include_opensubtitles: bool = True,
    selected_sources: list[str] | None = None,
    target_pairs: int = TARGET_PAIRS_PER_LANG,
) -> dict[str, Any]:
    """拉取、严苛清洗、全局去重、容量拉齐并构建指定语种对的 SQLite FTS5 检索索引。"""
    t0 = time.perf_counter()
    lang_dir = corpus_root / lang_pair
    raw_cache_dir = corpus_root / "raw_cache"
    lang_dir.mkdir(parents=True, exist_ok=True)
    raw_cache_dir.mkdir(parents=True, exist_ok=True)

    candidates = [s for s in CORPUS_SOURCES if s.lang_pair == lang_pair]
    if not candidates:
        raise ValueError(f"暂未配置语种对 `{lang_pair}` 的开源语料源。")

    active_specs: list[CorpusSourceSpec] = []
    for spec in candidates:
        if selected_sources:
            if spec.source_id not in selected_sources:
                continue
        else:
            if not spec.default_enabled:
                continue
            if not include_opensubtitles and spec.source_id.startswith("opensubtitles_"):
                continue
        if commercial_only and not spec.commercial_safe:
            continue
        active_specs.append(spec)

    if not active_specs:
        raise RuntimeError(f"语种对 `{lang_pair}` 在当前筛选条件下没有可用语料源。")

    # 先保底采集所有商用友好源，再补充影视/TED口语源，最后统一拉齐至 target_pairs
    all_records: list[dict[str, Any]] = []
    global_seen: set[str] = set()
    source_meta_by_id: dict[str, CorpusSourceSpec] = {s.source_id: s for s in active_specs}

    for spec in active_specs:
        print(
            f"[语料采集][{lang_pair}] 正在流式拉取并清洗 {spec.name} ({spec.license_name}) ...",
            flush=True,
        )
        try:
            records = _load_pairs_from_source(spec, raw_cache_dir=raw_cache_dir)
        except Exception as exc:
            print(f"[警告][{lang_pair}] 拉取 {spec.name} 失败，已跳过（{exc}）", file=sys.stderr, flush=True)
            continue

        kept_here = 0
        for rec in records:
            key = rec["src"].lower()
            if key in global_seen:
                continue
            global_seen.add(key)
            all_records.append(rec)
            kept_here += 1

        print(f"  -> 流式去重入候选池 {kept_here:,} 条（累计 {len(all_records):,} 条）", flush=True)

    # 容量齐平裁剪：超过 target_pairs 时优先保底全部商用友好句对，再用最高口语分影视/TED句对填满至 target_pairs
    if target_pairs > 0 and len(all_records) > target_pairs:
        comm_recs = [r for r in all_records if r.get("commercial_safe")]
        non_comm_recs = [r for r in all_records if not r.get("commercial_safe")]
        comm_recs.sort(key=lambda r: (-float(r["quality_score"]), len(r["src"])))
        non_comm_recs.sort(key=lambda r: (-float(r["quality_score"]), len(r["src"])))
        if len(comm_recs) >= target_pairs:
            all_records = comm_recs[:target_pairs]
        else:
            remain = target_pairs - len(comm_recs)
            all_records = comm_recs + non_comm_recs[:remain]

    # 按最终入库结果统计各源真实贡献条数
    kept_counter: dict[str, int] = {}
    for rec in all_records:
        sid = str(rec.get("source") or "")
        kept_counter[sid] = kept_counter.get(sid, 0) + 1

    source_stats: list[dict[str, Any]] = []
    for spec in active_specs:
        cnt = kept_counter.get(spec.source_id, 0)
        if cnt <= 0:
            continue
        source_stats.append(
            {
                "source_id": spec.source_id,
                "name": spec.name,
                "domain": spec.domain,
                "license": spec.license_name,
                "commercial_safe": spec.commercial_safe,
                "homepage": spec.homepage,
                "kept_pairs": cnt,
            }
        )

    jsonl_path = lang_dir / "open_parallel_corpus.jsonl"
    with jsonl_path.open("w", encoding="utf-8") as fh:
        for rec in all_records:
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")

    db_path = lang_dir / "corpus_index.db"
    indexed_count = build_fts_index(jsonl_path, db_path, lang_pair=lang_pair)

    manifest_path = corpus_root / "corpus_manifest.json"
    manifest: dict[str, Any] = {"lang_pairs": {}}
    if manifest_path.is_file():
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except Exception:
            manifest = {"lang_pairs": {}}

    commercial_count = sum(1 for r in all_records if r.get("commercial_safe"))
    tokenizer = get_tokenizer_for_lang_pair(lang_pair)
    manifest.setdefault("lang_pairs", {})[lang_pair] = {
        "label": LANG_PAIR_META.get(lang_pair, {}).get("label", lang_pair),
        "total_pairs": indexed_count,
        "commercial_safe_pairs": commercial_count,
        "tokenizer": tokenizer,
        "jsonl_path": str(jsonl_path.relative_to(corpus_root)).replace("\\", "/"),
        "index_db_path": str(db_path.relative_to(corpus_root)).replace("\\", "/"),
        "updated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "sources": source_stats,
    }
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    _write_sources_markdown(corpus_root, manifest)

    elapsed = round(time.perf_counter() - t0, 2)
    print(
        f"[完成][{lang_pair}] 共构建 {indexed_count:,} 条顶级平行语料"
        f"（商用无限制许可 {commercial_count:,} 条，分词器={tokenizer}），耗时 {elapsed}s -> {db_path}",
        flush=True,
    )
    return manifest["lang_pairs"][lang_pair]


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")

    parser = argparse.ArgumentParser(
        description="拉取并清洗六大语种开源顶级双语平行语料，构建零依赖 SQLite FTS5 本地专家检索索引。"
    )
    parser.add_argument(
        "--lang-pair",
        default="en_zh",
        help="要构建的语种对（en_zh / zh_en / ja_zh / ko_zh / ru_zh / es_zh / all）",
    )
    parser.add_argument(
        "--commercial-only",
        action="store_true",
        help="严格仅收录商用无限制开源许可（MIT / CC BY 2.0 / CC BY-SA 4.0）的数据源",
    )
    parser.add_argument(
        "--include-opensubtitles",
        action="store_true",
        help="显式包含 OpenSubtitles 影视大包（默认已开启，保留兼容旧参数）",
    )
    parser.add_argument(
        "--no-opensubtitles",
        action="store_true",
        help="跳过 OpenSubtitles 影视大包（默认开启以拉齐 10 万级口语规模）",
    )
    parser.add_argument(
        "--target-pairs",
        type=int,
        default=TARGET_PAIRS_PER_LANG,
        help=f"单语种目标拉齐条数（默认 {TARGET_PAIRS_PER_LANG:,}）",
    )
    parser.add_argument(
        "--sources",
        default="",
        help="可选：仅拉取指定 source_id（逗号分隔）",
    )
    args = parser.parse_args(argv)

    selected = [s.strip() for s in args.sources.split(",") if s.strip()] or None
    targets = list(ALL_LANG_PAIRS) if args.lang_pair == "all" else [args.lang_pair]

    for lp in targets:
        build_corpus_for_lang_pair(
            lang_pair=lp,
            commercial_only=args.commercial_only,
            include_opensubtitles=not args.no_opensubtitles,
            selected_sources=selected,
            target_pairs=args.target_pairs,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
