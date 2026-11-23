"""本地毫秒级多语种「专家语料检索与术语记忆注入引擎」（6 语种 TM + Anti-Literal + FTS5 RAG）。

核心职责：
1. 维护并检索六大语种对（en_zh / zh_en / ja_zh / ko_zh / ru_zh / es_zh）的四层翻译记忆资产：
   - 第 1 层（最高优先级）：公会/主播专属词库 `data/corpus/user_glossary.json`
   - 第 2 层：多语种游戏/直播/电竞专词与 ASR 同音误听自动纠偏库 `<lang_pair>/curated_gaming_glossary.json`
   - 第 3 层：多语种口语习语/俚语/热梗反直译红线专家金库 `<lang_pair>/curated_idioms_slang.json`
   - 第 4 层：60 万级开源精洗人类专家平行语料 SQLite FTS5 BM25 动态 Few-Shot 检索 `<lang_pair>/corpus_index.db`
2. 纯标准库 `sqlite3` + FTS5（CJK 无空格语种使用原生 `trigram` 分词，拉丁/西里尔使用 `unicode61`/`porter`），
   零重型向量库依赖，单次检索耗时 < 10ms；
3. 自动检测台词源语种并为 Step 3 粗筛与双语精修动态组装《母语级专家翻译参考卡》。
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import threading
from pathlib import Path
from typing import Any

import sys

def _resolve_default_corpus_root() -> Path:
    """兼容源码运行与 PyInstaller 便携包运行时的默认语料库根目录。"""
    if getattr(sys, "frozen", False):
        exe_dir = Path(sys.executable).resolve().parent
        if (exe_dir / "data" / "corpus").is_dir():
            return exe_dir / "data" / "corpus"
        meipass = getattr(sys, "_MEIPASS", None)
        if meipass and (Path(meipass) / "data" / "corpus").is_dir():
            return Path(meipass) / "data" / "corpus"
        return exe_dir / "data" / "corpus"
    return Path(__file__).resolve().parent.parent / "data" / "corpus"


PROJECT_ROOT = (
    Path(sys.executable).resolve().parent
    if getattr(sys, "frozen", False)
    else Path(__file__).resolve().parent.parent
)
DEFAULT_CORPUS_ROOT = _resolve_default_corpus_root()

SUPPORTED_LANG_PAIRS: tuple[str, ...] = ("en_zh", "zh_en", "ja_zh", "ko_zh", "ru_zh", "es_zh")

_WORD_TOKEN_RE = re.compile(r"[A-Za-z][A-Za-z0-9'-]{1,24}")
_ES_WORD_TOKEN_RE = re.compile(r"[A-Za-zÁÉÍÓÚÜÑáéíóúüñ][A-Za-zÁÉÍÓÚÜÑáéíóúüñ0-9'-]{1,24}")
_RU_WORD_TOKEN_RE = re.compile(r"[\u0400-\u04ff]{3,24}")
_CJK_SPAN_RE = re.compile(r"[\u3040-\u30ff\u4e00-\u9fff\uac00-\ud7af]{3,}")
_CJK_CHAR_RE = re.compile(r"[\u4e00-\u9fff]")
_JP_KANA_RE = re.compile(r"[\u3040-\u30ff]")
_KR_HANGUL_RE = re.compile(r"[\uac00-\ud7af]")
_CYRILLIC_CHAR_RE = re.compile(r"[\u0400-\u04ff]")
_ES_DISTINCT_CHAR_RE = re.compile(r"[¿¡ñÑáéíóúÁÉÍÓÚüÜ]")
_ES_HINT_WORDS_RE = re.compile(
    r"\b(?:qué|como|cómo|pero|para|porque|está|estoy|están|vamos|tío|wey|locura|bueno|vale|"
    r"hola|gracias|mucho|todo|todos|esto|eso|aquí|ahora|muy|más|sin|sobre|cuando|donde|"
    r"quiero|puedo|tengo|joder|hostia|manches|chavales|gente|juego|mierda)\b",
    re.IGNORECASE,
)
_EN_HINT_WORDS_RE = re.compile(
    r"\b(?:the|and|that|this|with|you|your|have|from|they|what|when|where|just|like|really|"
    r"about|going|gonna|don't|can't|i'm|it's|that's|bro|dude|chat|game|play|playing)\b",
    re.IGNORECASE,
)

_EN_STOPWORDS = frozenset(
    {
        "the", "and", "for", "that", "this", "with", "you", "your", "are", "was", "were",
        "have", "has", "had", "not", "but", "what", "when", "where", "who", "why", "how",
        "from", "they", "them", "their", "there", "here", "just", "like", "yeah", "well",
        "about", "into", "out", "over", "then", "than", "also", "very", "much", "some",
        "more", "most", "really", "actually", "literally", "going", "gonna", "want", "know",
        "think", "mean", "said", "says", "saying", "don't", "didn't", "doesn't", "can't",
        "won't", "i'm", "i've", "i'd", "i'll", "it's", "that's", "you're", "we're", "they're",
        "she's", "he's", "isn't", "aren't", "wasn't", "ain't", "because", "cause", "would",
        "could", "should", "will", "been", "being", "doing", "does", "did", "all", "one",
        "two", "got", "get", "getting", "let", "see", "look", "come", "make", "take", "give",
        "only", "even", "still", "back", "now", "way", "thing", "things", "something", "anything",
        "everything", "nothing", "someone", "anyone", "everyone", "people", "guy", "guys", "bro",
        "dude", "man", "god", "fuck", "fucking", "shit", "oh", "uh", "um", "ah", "okay", "ok",
    }
)

_RU_STOPWORDS = frozenset(
    {
        "что", "это", "как", "так", "вот", "для", "или", "они", "она", "оно", "его", "её",
        "нас", "вас", "вам", "нам", "тут", "там", "где", "уже", "ещё", "еще", "все", "всё",
        "был", "была", "было", "были", "быть", "нет", "про", "при", "под", "над", "без",
        "чтобы", "если", "когда", "только", "просто", "очень", "меня", "тебя", "себя",
        "тебе", "мне", "этот", "эта", "эти", "того", "этого", "может", "надо", "будет",
    }
)

_ES_STOPWORDS = frozenset(
    {
        "que", "qué", "los", "las", "una", "uno", "unos", "unas", "por", "para", "con", "sin",
        "como", "cómo", "pero", "más", "mas", "muy", "está", "esta", "están", "son", "fue",
        "era", "hay", "esto", "eso", "ese", "esa", "aqui", "aquí", "ahora", "todo", "todos",
        "toda", "todas", "bien", "mal", "del", "nos", "les", "mis", "tus", "sus", "este",
        "porque", "cuando", "donde", "también", "solo", "siempre", "nunca", "nada", "algo",
        "tengo", "estoy", "eres", "somos", "tienen", "hacer", "puede", "puedo", "quiero",
    }
)

# CJK 无区分度纯助词/高频虚词三元组黑名单（避免 trigram 检索被纯虚词淹没）
_CJK_TRIGRAM_STOP = frozenset(
    {
        "ている", "ていた", "ていて", "ました", "ません", "ですが", "ですね", "ですよ", "という",
        "のこと", "ように", "ている", "してる", "してた", "んです", "だから", "けれど", "しかし",
        "입니다", "습니다", "하세요", "했어요", "하는데", "그런데", "그리고", "하지만", "있어요",
        "的就是", "的时候", "什么样", "是因为", "不知道", "我觉得", "是不是", "有没有", "怎么会",
    }
)

_LANG_PAIR_LABELS: dict[str, dict[str, str]] = {
    "en_zh": {
        "idiom_title": "英语口语习语反直译专家红线",
        "exemplar_title": "人类专家口语双语译文风格示范（英→中，参考简洁、去机翻腔的自然中文语感）",
    },
    "zh_en": {
        "idiom_title": "中文热梗与口语去 Chinglish 反直译专家红线",
        "exemplar_title": "人类专家口语双语译文风格示范（中→母语英语，参考地道、零中式翻译腔的英语表达）",
    },
    "ja_zh": {
        "idiom_title": "日语口语、V圈与二次元黑话反直译专家红线",
        "exemplar_title": "人类专家口语双语译文风格示范（日→中，参考鲜活、去直译腔的自然中文语感）",
    },
    "ko_zh": {
        "idiom_title": "韩语口语、半语敬语与电竞热梗反直译专家红线",
        "exemplar_title": "人类专家口语双语译文风格示范（韩→中，参考有网感、去直译腔的自然中文语感）",
    },
    "ru_zh": {
        "idiom_title": "俄语电竞口语与毛式感叹反直译专家红线",
        "exemplar_title": "人类专家口语双语译文风格示范（俄→中，参考干脆利落、去翻译腔的自然中文语感）",
    },
    "es_zh": {
        "idiom_title": "西语/拉美直播口语反直译专家红线",
        "exemplar_title": "人类专家口语双语译文风格示范（西→中，参考热情地道、去直译腔的自然中文语感）",
    },
}


def detect_source_language(text: str | None) -> str:
    """轻量零依赖高精度源语种自动识别（支持 en / zh / ja / ko / ru / es）。"""
    sample = (text or "").strip()
    if not sample:
        return "en"

    jp_kana = len(_JP_KANA_RE.findall(sample))
    kr_hangul = len(_KR_HANGUL_RE.findall(sample))
    cyrillic = len(_CYRILLIC_CHAR_RE.findall(sample))
    cjk_chars = len(_CJK_CHAR_RE.findall(sample))
    latin_words = _ES_WORD_TOKEN_RE.findall(sample)

    # 1. 日语：只要出现一定数量平/片假名，直接高置信判定日语（即使混有大量汉字）
    if jp_kana >= 3 and jp_kana >= kr_hangul and jp_kana >= cyrillic:
        return "ja"
    # 2. 韩语：谚文独有特征
    if kr_hangul >= 3 and kr_hangul >= jp_kana and kr_hangul >= cyrillic:
        return "ko"
    # 3. 俄语：西里尔字母独有特征
    if cyrillic >= 4 and cyrillic >= len(latin_words):
        return "ru"
    # 4. 简体/繁体中文：无假名/谚文，且汉字数显著多于英文单词
    if cjk_chars >= 4 and jp_kana < 2 and kr_hangul < 2:
        if cjk_chars >= len(latin_words) * 1.2:
            return "zh"
    # 5. 西班牙语 vs 英语：检查西语特有重音/倒置标点及功能词密度
    if latin_words:
        es_chars = len(_ES_DISTINCT_CHAR_RE.findall(sample))
        es_words = len(_ES_HINT_WORDS_RE.findall(sample))
        en_words = len(_EN_HINT_WORDS_RE.findall(sample))
        if (es_chars >= 2 and es_words >= 1) or (es_words >= 3 and es_words > en_words * 1.4):
            return "es"

    return "en"


def _normalize_lang_pair(source_language: str | None, text: str | None = None) -> str:
    """将 ASR 识别的源语种（或已有 lang_pair）映射到本地语料库的语种对目录名。"""
    raw = (source_language or "").strip().lower()
    if not raw or raw in {"auto", "unknown", "none"}:
        raw = detect_source_language(text) if text else "en"

    if raw in SUPPORTED_LANG_PAIRS:
        return raw
    if raw.startswith("ja") or raw in {"jp", "jpn", "japanese"}:
        return "ja_zh"
    if raw.startswith("ko") or raw in {"kr", "kor", "korean"}:
        return "ko_zh"
    if raw.startswith("ru") or raw in {"rus", "russian"}:
        return "ru_zh"
    if raw.startswith("es") or raw in {"spa", "spanish"}:
        return "es_zh"
    if raw.startswith("zh") or raw in {"cmn", "zho", "cn", "chinese"}:
        return "zh_en"
    return "en_zh"


def _is_word_boundary_char(ch: str) -> bool:
    """判断字符是否属于需要词边界保护的字母/数字字符（拉丁/扩展拉丁/西里尔/数字，排除 CJK）。"""
    code = ord(ch)
    return (
        ("A" <= ch <= "Z")
        or ("a" <= ch <= "z")
        or ("0" <= ch <= "9")
        or (0x00C0 <= code <= 0x024F)
        or (0x0400 <= code <= 0x04FF)
    )


def _compile_phrase_pattern(phrase: str) -> re.Pattern[str] | None:
    """为术语或习语短语编译带多语种安全词边界的忽略大小写正则，避免子串误命中。"""
    cleaned = (phrase or "").strip()
    if not cleaned:
        return None
    escaped = re.escape(cleaned).replace(r"\ ", r"\s+")
    boundary_class = r"[A-Za-z0-9\u00c0-\u024f\u0400-\u04ff]"
    prefix = f"(?<!{boundary_class})" if _is_word_boundary_char(cleaned[0]) else ""
    suffix = f"(?!{boundary_class})" if _is_word_boundary_char(cleaned[-1]) else ""
    return re.compile(f"{prefix}{escaped}{suffix}", re.IGNORECASE)


def _user_glossary_path(corpus_root: Path = DEFAULT_CORPUS_ROOT) -> Path:
    return corpus_root / "user_glossary.json"


def load_user_glossary(corpus_root: Path = DEFAULT_CORPUS_ROOT) -> list[dict[str, Any]]:
    """读取公会与主播专属自定义术语/纠错记忆库。"""
    path = _user_glossary_path(corpus_root)
    if not path.is_file():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        terms = data.get("terms", []) if isinstance(data, dict) else data
        if not isinstance(terms, list):
            return []
        cleaned: list[dict[str, Any]] = []
        for row in terms:
            if not isinstance(row, dict):
                continue
            en = str(row.get("en") or row.get("src") or "").strip()
            zh = str(row.get("zh") or row.get("tgt") or "").strip()
            if not en or not zh:
                continue
            raw_aliases = row.get("aliases") or []
            if isinstance(raw_aliases, str):
                aliases = [a.strip() for a in re.split(r"[,，;；]+", raw_aliases) if a.strip()]
            elif isinstance(raw_aliases, list):
                aliases = [str(a).strip() for a in raw_aliases if str(a).strip()]
            else:
                aliases = []
            cleaned.append(
                {
                    "en": en,
                    "zh": zh,
                    "aliases": aliases,
                    "note": str(row.get("note") or "").strip(),
                }
            )
        return cleaned
    except Exception:
        return []


def save_user_glossary(
    terms: list[dict[str, Any]],
    corpus_root: Path = DEFAULT_CORPUS_ROOT,
) -> list[dict[str, Any]]:
    """标准化并持久化保存公会/主播专属词库。"""
    sanitized: list[dict[str, Any]] = []
    seen: set[str] = set()
    for row in terms:
        if not isinstance(row, dict):
            continue
        en = str(row.get("en") or row.get("src") or "").strip()
        zh = str(row.get("zh") or row.get("tgt") or "").strip()
        if not en or not zh:
            continue
        key = en.lower()
        if key in seen:
            continue
        seen.add(key)
        raw_aliases = row.get("aliases") or []
        if isinstance(raw_aliases, str):
            aliases = [a.strip() for a in re.split(r"[,，;；]+", raw_aliases) if a.strip()]
        elif isinstance(raw_aliases, list):
            aliases = [str(a).strip() for a in raw_aliases if str(a).strip()]
        else:
            aliases = []
        sanitized.append(
            {
                "en": en,
                "zh": zh,
                "aliases": aliases,
                "note": str(row.get("note") or "").strip(),
            }
        )

    path = _user_glossary_path(corpus_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "meta": {
            "name": "公会与主播专属自定义术语与ASR纠错库",
            "priority": "highest",
        },
        "terms": sanitized,
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    DEFAULT_CORPUS_RAG.invalidate_cache()
    return sanitized


class CorpusRAG:
    """线程安全的六语种本地专家语料、术语与习语检索引擎（带内存编译与索引元数据缓存）。"""

    def __init__(self, corpus_root: Path = DEFAULT_CORPUS_ROOT) -> None:
        self.corpus_root = Path(corpus_root)
        self._lock = threading.Lock()
        self._compiled_cache: dict[str, dict[str, Any]] = {}
        self._db_meta_cache: dict[str, dict[str, Any]] = {}

    def invalidate_cache(self) -> None:
        with self._lock:
            self._compiled_cache.clear()
            self._db_meta_cache.clear()

    def _load_compiled_lexicons(self, lang_pair: str) -> dict[str, Any]:
        with self._lock:
            cached = self._compiled_cache.get(lang_pair)
            if cached is not None:
                return cached

        lang_dir = self.corpus_root / lang_pair
        compiled: dict[str, Any] = {
            "mishearings": [],
            "terms": [],
            "idioms": [],
        }

        # 1. 加载游戏/直播专词与 ASR 误听表（统一兼容 src/tgt 与旧 en/zh 字段）
        glossary_file = lang_dir / "curated_gaming_glossary.json"
        if glossary_file.is_file():
            try:
                raw = json.loads(glossary_file.read_text(encoding="utf-8"))
                for item in raw.get("asr_mishearings", []):
                    pats = [
                        (str(h).strip(), _compile_phrase_pattern(str(h)))
                        for h in (item.get("heard") or [])
                        if str(h).strip()
                    ]
                    valid_pats = [(h, p) for h, p in pats if p is not None]
                    if valid_pats:
                        compiled["mishearings"].append((item, valid_pats))

                for item in raw.get("terms", []):
                    primary = str(item.get("src") or item.get("en") or "").strip()
                    phrases = [primary, *(item.get("aliases") or [])]
                    pats = [
                        _compile_phrase_pattern(str(ph))
                        for ph in phrases
                        if str(ph).strip()
                    ]
                    valid_pats = [p for p in pats if p is not None]
                    if valid_pats:
                        compiled["terms"].append((item, valid_pats))
            except Exception:
                pass

        # 2. 加载习语俚语反直译专家库
        idioms_file = lang_dir / "curated_idioms_slang.json"
        if idioms_file.is_file():
            try:
                raw = json.loads(idioms_file.read_text(encoding="utf-8"))
                for item in raw.get("idioms", []):
                    phrases = list(item.get("patterns") or [])
                    if item.get("expr"):
                        phrases.append(item["expr"])
                    pats = [
                        _compile_phrase_pattern(str(ph))
                        for ph in phrases
                        if str(ph).strip()
                    ]
                    valid_pats = [p for p in pats if p is not None]
                    if valid_pats:
                        compiled["idioms"].append((item, valid_pats))
            except Exception:
                pass

        with self._lock:
            self._compiled_cache[lang_pair] = compiled
        return compiled

    def _get_db_meta(self, db_path: Path, lang_pair: str) -> dict[str, Any]:
        """读取并缓存指定 FTS5 数据库的 tokenizer 与索引列模式，兼容旧版数据库。"""
        cache_key = str(db_path)
        with self._lock:
            cached = self._db_meta_cache.get(cache_key)
            if cached is not None:
                return cached

        meta: dict[str, Any] = {
            "tokenizer": "trigram" if lang_pair in {"ja_zh", "ko_zh", "zh_en"} else "unicode61",
            "has_meta_table": False,
        }
        conn: sqlite3.Connection | None = None
        try:
            conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=3.0)
            row = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='corpus_meta'"
            ).fetchone()
            if row:
                meta["has_meta_table"] = True
                for k, v in conn.execute("SELECT key, value FROM corpus_meta").fetchall():
                    meta[str(k)] = str(v)
            else:
                # 兼容旧版数据库：通过 sqlite_master 的建表 SQL 识别是否使用 trigram
                fts_sql_row = conn.execute(
                    "SELECT sql FROM sqlite_master WHERE type='table' AND name='pairs_fts'"
                ).fetchone()
                fts_sql = str(fts_sql_row[0] or "").lower() if fts_sql_row else ""
                meta["tokenizer"] = "trigram" if "trigram" in fts_sql else "unicode61"
        except Exception:
            pass
        finally:
            if conn is not None:
                conn.close()

        with self._lock:
            self._db_meta_cache[cache_key] = meta
        return meta

    def _extract_query_terms(
        self,
        text: str,
        lang_pair: str,
        tokenizer: str = "unicode61",
        limit: int = 16,
    ) -> list[str]:
        """按源语种与 FTS5 分词器提取最具检索区分度的关键词或 CJK 特征三元组。"""
        raw_text = text or ""

        if "trigram" in tokenizer or lang_pair in {"ja_zh", "ko_zh", "zh_en"}:
            # 对无空格中日韩文：切出连续文本块并用步长 1 滑窗提取 3 字特征片段（跳过高频纯助词）
            spans = _CJK_SPAN_RE.findall(raw_text)
            freq: dict[str, int] = {}
            for sp in spans:
                sp_len = len(sp)
                if sp_len <= 4:
                    if sp not in _CJK_TRIGRAM_STOP:
                        freq[sp] = freq.get(sp, 0) + 2
                    continue
                for i in range(0, sp_len - 2):
                    gram = sp[i : i + 3]
                    if gram in _CJK_TRIGRAM_STOP:
                        continue
                    freq[gram] = freq.get(gram, 0) + 1
            ranked = sorted(freq.items(), key=lambda kv: (-kv[1], -len(kv[0]), kv[0]))
            return [k for k, _ in ranked[: max(limit, 24)]]

        if lang_pair == "ru_zh":
            tokens = _RU_WORD_TOKEN_RE.findall(raw_text)
            freq_ru: dict[str, int] = {}
            for tok in tokens:
                low = tok.lower()
                if len(low) < 3 or low in _RU_STOPWORDS:
                    continue
                freq_ru[low] = freq_ru.get(low, 0) + 1
            ranked_ru = sorted(freq_ru.items(), key=lambda kv: (-kv[1], -len(kv[0]), kv[0]))
            return [k for k, _ in ranked_ru[:limit]]

        if lang_pair == "es_zh":
            tokens = _ES_WORD_TOKEN_RE.findall(raw_text)
            freq_es: dict[str, int] = {}
            for tok in tokens:
                low = tok.lower().strip("'-")
                safe = re.sub(r"[^a-záéíóúüñ0-9]", "", low)
                if len(safe) < 3 or safe in _ES_STOPWORDS:
                    continue
                freq_es[safe] = freq_es.get(safe, 0) + 1
            ranked_es = sorted(freq_es.items(), key=lambda kv: (-kv[1], -len(kv[0]), kv[0]))
            return [k for k, _ in ranked_es[:limit]]

        # 默认 en_zh
        tokens = _WORD_TOKEN_RE.findall(raw_text)
        freq_en: dict[str, int] = {}
        for tok in tokens:
            low = tok.lower().strip("'-")
            if len(low) < 3 or low in _EN_STOPWORDS:
                continue
            safe = re.sub(r"[^a-z0-9]", "", low)
            if len(safe) >= 3 and safe not in _EN_STOPWORDS:
                freq_en[safe] = freq_en.get(safe, 0) + 1
        ranked_en = sorted(freq_en.items(), key=lambda kv: (-kv[1], -len(kv[0]), kv[0]))
        return [k for k, _ in ranked_en[:limit]]

    def search_parallel_exemplars(
        self,
        query_text: str,
        lang_pair: str = "en_zh",
        top_k: int = 6,
        commercial_only: bool = False,
    ) -> list[dict[str, Any]]:
        """通过本地 SQLite FTS5 BM25 毫秒级检索最贴合当前语气的开源人类专家平行句对。"""
        if top_k <= 0:
            return []
        db_path = self.corpus_root / lang_pair / "corpus_index.db"
        if not db_path.is_file():
            return []

        db_meta = self._get_db_meta(db_path, lang_pair=lang_pair)
        tokenizer = str(db_meta.get("tokenizer") or "unicode61")
        keywords = self._extract_query_terms(
            query_text,
            lang_pair=lang_pair,
            tokenizer=tokenizer,
            limit=14,
        )
        if not keywords:
            return []

        # 严格限定只在源语言列 src 检索，防止误命中译文列并显著提升 BM25 相关性
        safe_clauses = []
        for kw in keywords:
            clean_kw = kw.replace('"', "").strip()
            if "trigram" in tokenizer and len(clean_kw) < 3:
                continue
            if clean_kw:
                safe_clauses.append(f'"{clean_kw}"')
        if not safe_clauses:
            return []

        fts_expr = "src : (" + " OR ".join(safe_clauses) + ")"
        fetch_limit = max(24, top_k * 5)

        sql = """
            SELECT p.src, p.tgt, p.source, p.domain, p.license, p.commercial_safe, p.quality_score,
                   bm25(pairs_fts) AS bm25_rank
            FROM pairs_fts
            JOIN pairs p ON p.id = pairs_fts.rowid
            WHERE pairs_fts MATCH ?
        """
        params: list[Any] = [fts_expr]
        if commercial_only:
            sql += " AND p.commercial_safe = 1"
        sql += " ORDER BY (bm25_rank - p.quality_score * 0.65) ASC LIMIT ?"
        params.append(fetch_limit)

        rows: list[tuple[Any, ...]] = []
        conn: sqlite3.Connection | None = None
        try:
            conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=5.0)
            rows = conn.execute(sql, params).fetchall()

            # 对无空格 CJK 语种：若 3-gram 精准命中不足 top_k（常见于网络新梗短句），用高价值 2 字实词补查
            if len(rows) < top_k and ("trigram" in tokenizer or lang_pair in {"ja_zh", "ko_zh", "zh_en"}):
                bigrams: list[str] = []
                seen_bg: set[str] = set()
                for sp in _CJK_SPAN_RE.findall(query_text or ""):
                    for i in range(0, len(sp) - 1):
                        bg = sp[i : i + 2]
                        if bg in seen_bg or bg in {"这个", "那个", "什么", "怎么", "就是", "不是", "可以", "因为", "所以", "如果", "已经", "真的"}:
                            continue
                        seen_bg.add(bg)
                        bigrams.append(bg)
                        if len(bigrams) >= 8:
                            break
                    if len(bigrams) >= 8:
                        break
                if bigrams:
                    like_clauses = " OR ".join("src LIKE ?" for _ in bigrams)
                    fb_sql = f"""
                        SELECT src, tgt, source, domain, license, commercial_safe, quality_score, 0.0 AS bm25_rank
                        FROM pairs
                        WHERE ({like_clauses})
                    """
                    fb_params: list[Any] = [f"%{bg}%" for bg in bigrams]
                    if commercial_only:
                        fb_sql += " AND commercial_safe = 1"
                    fb_sql += " ORDER BY quality_score DESC LIMIT ?"
                    fb_params.append(fetch_limit)
                    rows.extend(conn.execute(fb_sql, fb_params).fetchall())
        except Exception:
            return []
        finally:
            if conn is not None:
                conn.close()

        # 多样性精选：避免相近句首或重复模板占满 Few-Shot 槽位
        selected: list[dict[str, Any]] = []
        seen_prefixes: set[str] = set()
        prefix_len = 12 if lang_pair in {"ja_zh", "ko_zh", "zh_en"} else 24
        for src, tgt, source, domain, lic, comm, q_score, bm25_rank in rows:
            prefix = src[:prefix_len].lower()
            if prefix in seen_prefixes:
                continue
            seen_prefixes.add(prefix)
            selected.append(
                {
                    "src": src,
                    "tgt": tgt,
                    "source": source,
                    "domain": domain,
                    "license": lic,
                    "commercial_safe": bool(comm),
                    "quality_score": float(q_score),
                }
            )
            if len(selected) >= top_k:
                break
        return selected

    def retrieve(
        self,
        text: str,
        source_language: str | None = None,
        max_terms: int = 10,
        max_idioms: int = 8,
        max_exemplars: int = 6,
        commercial_only: bool | None = None,
    ) -> dict[str, Any]:
        """一次性检索当前台词触发的全部专家知识（专属词库 + 误听纠偏 + 游戏术语 + 习语反直译 + 平行范例）。"""
        combined_text = (text or "").strip()
        lang_pair = _normalize_lang_pair(source_language, text=combined_text)
        if commercial_only is None:
            commercial_only = os.getenv("CORPUS_COMMERCIAL_ONLY", "").strip().lower() in {
                "1", "true", "yes", "on"
            }

        if not combined_text:
            return {
                "lang_pair": lang_pair,
                "user_hits": [],
                "mishearing_hits": [],
                "term_hits": [],
                "idiom_hits": [],
                "parallel_exemplars": [],
            }

        # 1. 最高优先级：公会与主播专属自定义词库（实时读取确保保存即生效）
        user_hits: list[dict[str, Any]] = []
        for entry in load_user_glossary(self.corpus_root):
            phrases = [entry["en"], *entry.get("aliases", [])]
            matched_phrase = ""
            for ph in phrases:
                pat = _compile_phrase_pattern(ph)
                if pat and pat.search(combined_text):
                    matched_phrase = ph
                    break
            if matched_phrase:
                user_hits.append(
                    {
                        "matched": matched_phrase,
                        "src": entry["en"],
                        "tgt": entry["zh"],
                        "en": entry["en"],
                        "zh": entry["zh"],
                        "note": entry.get("note", ""),
                    }
                )

        # 2. 内置专家术语、ASR 误听纠偏与习语反直译检索
        lex = self._load_compiled_lexicons(lang_pair)

        mishearing_hits: list[dict[str, Any]] = []
        for item, pats in lex["mishearings"]:
            for heard_str, pat in pats:
                if pat.search(combined_text):
                    corr = str(item.get("correct_src") or item.get("correct_en") or "").strip()
                    tgt_val = str(item.get("tgt") or item.get("zh") or "").strip()
                    mishearing_hits.append(
                        {
                            "heard": heard_str,
                            "correct_src": corr,
                            "correct_en": corr,
                            "tgt": tgt_val,
                            "zh": tgt_val,
                            "note": item.get("note", ""),
                        }
                    )
                    break

        term_hits: list[dict[str, Any]] = []
        seen_terms: set[str] = {u["en"].lower() for u in user_hits}
        for item, pats in lex["terms"]:
            src_val = str(item.get("src") or item.get("en") or "").strip()
            tgt_val = str(item.get("tgt") or item.get("zh") or "").strip()
            key = src_val.lower()
            if not src_val or key in seen_terms:
                continue
            if any(p.search(combined_text) for p in pats):
                seen_terms.add(key)
                term_hits.append(
                    {
                        "src": src_val,
                        "tgt": tgt_val,
                        "en": src_val,
                        "zh": tgt_val,
                        "category": item.get("category", ""),
                    }
                )
                if len(term_hits) >= max_terms:
                    break

        idiom_hits: list[dict[str, Any]] = []
        for item, pats in lex["idioms"]:
            if any(p.search(combined_text) for p in pats):
                expert_val = str(item.get("expert_tgt") or item.get("expert_zh") or "").strip()
                idiom_hits.append(
                    {
                        "expr": item["expr"],
                        "avoid_literal": item.get("avoid_literal", ""),
                        "expert_tgt": expert_val,
                        "expert_zh": expert_val,
                        "tone": item.get("tone", ""),
                    }
                )
                if len(idiom_hits) >= max_idioms:
                    break

        # 3. SQLite FTS5 BM25 动态检索最贴合当前语气的开源人类专家平行译文范例
        parallel_exemplars = self.search_parallel_exemplars(
            query_text=combined_text,
            lang_pair=lang_pair,
            top_k=max_exemplars,
            commercial_only=bool(commercial_only),
        )

        return {
            "lang_pair": lang_pair,
            "user_hits": user_hits,
            "mishearing_hits": mishearing_hits,
            "term_hits": term_hits,
            "idiom_hits": idiom_hits,
            "parallel_exemplars": parallel_exemplars,
        }


DEFAULT_CORPUS_RAG = CorpusRAG()


def retrieve_translation_references(
    text: str,
    source_language: str | None = None,
    max_terms: int = 10,
    max_idioms: int = 8,
    max_exemplars: int = 6,
    commercial_only: bool | None = None,
    corpus_root: Path | None = None,
) -> dict[str, Any]:
    """便捷检索函数：默认使用全局缓存单例，也可指定临时 corpus_root 便于测试隔离。"""
    rag = CorpusRAG(corpus_root) if corpus_root is not None else DEFAULT_CORPUS_RAG
    return rag.retrieve(
        text=text,
        source_language=source_language,
        max_terms=max_terms,
        max_idioms=max_idioms,
        max_exemplars=max_exemplars,
        commercial_only=commercial_only,
    )


def format_rag_reference_block(refs: dict[str, Any]) -> str:
    """将检索到的多语种专家记忆渲染为精炼紧凑的 Prompt 参考区块（无命中时返回空串）。"""
    if not refs:
        return ""
    lang_pair = str(refs.get("lang_pair") or "en_zh")
    labels = _LANG_PAIR_LABELS.get(lang_pair, _LANG_PAIR_LABELS["en_zh"])
    lines: list[str] = []

    user_hits = refs.get("user_hits") or []
    if user_hits:
        lines.append("- 【公会/主播专属词库（最高优先级，务必采用）】：")
        for u in user_hits:
            src_u = u.get("src") or u.get("en") or ""
            tgt_u = u.get("tgt") or u.get("zh") or ""
            note_s = f"（{u['note']}）" if u.get("note") else ""
            if u["matched"].lower() != src_u.lower():
                lines.append(f'  * 听到 "{u["matched"]}" -> 修正为 "{src_u}" -> 译为：{tgt_u}{note_s}')
            else:
                lines.append(f"  * {src_u} -> {tgt_u}{note_s}")

    mishearings = refs.get("mishearing_hits") or []
    if mishearings:
        lines.append("- 【ASR 常见同音误听核查纠偏】：")
        for m in mishearings:
            corr = m.get("correct_src") or m.get("correct_en") or ""
            tgt_m = m.get("tgt") or m.get("zh") or ""
            note_s = f"（{m['note']}）" if m.get("note") else ""
            lines.append(
                f'  * 原稿出现 "{m["heard"]}" 时请结合上下文核查是否实为 "{corr}" -> 译作：{tgt_m}{note_s}'
            )

    terms = refs.get("term_hits") or []
    if terms:
        term_str = "；".join(
            f"{t.get('src') or t.get('en')}={t.get('tgt') or t.get('zh')}" for t in terms
        )
        lines.append(f"- 【游戏与直播专业术语参考】：{term_str}")

    idioms = refs.get("idiom_hits") or []
    if idioms:
        lines.append(f"- 【{labels['idiom_title']}】：")
        for d in idioms:
            avoid = f"（严禁直译：{d['avoid_literal']}）" if d.get("avoid_literal") else ""
            tone = f" [{d['tone']}]" if d.get("tone") else ""
            exp = d.get("expert_tgt") or d.get("expert_zh") or ""
            lines.append(f'  * "{d["expr"]}"{tone}{avoid} -> 推荐意译：{exp}')

    exemplars = refs.get("parallel_exemplars") or []
    if exemplars:
        lines.append(f"- 【{labels['exemplar_title']}】：")
        for idx, ex in enumerate(exemplars, start=1):
            lines.append(f"  {idx}. {ex['src']}  ==>  {ex['tgt']}")

    if not lines:
        return ""
    return f"【本地多语种专家语料库与术语记忆参考（语种方向：{lang_pair}）】\n" + "\n".join(lines)


def get_corpus_stats(corpus_root: Path = DEFAULT_CORPUS_ROOT) -> dict[str, Any]:
    """统计全语种已收录的开源平行语料规模、专家词库与用户专属词条数。"""
    root = Path(corpus_root)
    manifest_path = root / "corpus_manifest.json"
    lang_pairs: dict[str, Any] = {}
    if manifest_path.is_file():
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            lang_pairs = dict(manifest.get("lang_pairs", {}))
        except Exception:
            lang_pairs = {}

    total_terms_cnt = 0
    total_mishearing_cnt = 0
    total_idioms_cnt = 0
    lexicon_by_lang: dict[str, dict[str, Any]] = {}

    # 汇总所有语种目录下的专家术语、ASR误听与反直译红线
    scan_dirs = list(SUPPORTED_LANG_PAIRS) + [
        lp for lp in lang_pairs.keys() if lp not in SUPPORTED_LANG_PAIRS
    ]
    for lp in scan_dirs:
        lp_dir = root / lp
        g_cnt = 0
        m_cnt = 0
        i_cnt = 0
        if (lp_dir / "curated_gaming_glossary.json").is_file():
            try:
                g = json.loads((lp_dir / "curated_gaming_glossary.json").read_text(encoding="utf-8"))
                g_cnt = len(g.get("terms", []))
                m_cnt = len(g.get("asr_mishearings", []))
            except Exception:
                pass
        if (lp_dir / "curated_idioms_slang.json").is_file():
            try:
                i = json.loads((lp_dir / "curated_idioms_slang.json").read_text(encoding="utf-8"))
                i_cnt = len(i.get("idioms", []))
            except Exception:
                pass
        has_idx = (lp_dir / "corpus_index.db").is_file()
        if g_cnt or m_cnt or i_cnt or has_idx or lp in lang_pairs:
            total_terms_cnt += g_cnt
            total_mishearing_cnt += m_cnt
            total_idioms_cnt += i_cnt
            lexicon_by_lang[lp] = {
                "gaming_terms_count": g_cnt,
                "asr_mishearings_count": m_cnt,
                "idioms_anti_literal_count": i_cnt,
                "has_index": has_idx,
            }
            if lp in lang_pairs and isinstance(lang_pairs[lp], dict):
                lang_pairs[lp] = {**lang_pairs[lp], **lexicon_by_lang[lp]}

    user_terms = load_user_glossary(root)
    total_parallel = sum(int(v.get("total_pairs", 0)) for v in lang_pairs.values() if isinstance(v, dict))
    total_commercial = sum(
        int(v.get("commercial_safe_pairs", 0)) for v in lang_pairs.values() if isinstance(v, dict)
    )

    return {
        "total_parallel_pairs": total_parallel,
        "commercial_safe_pairs": total_commercial,
        "gaming_terms_count": total_terms_cnt,
        "asr_mishearings_count": total_mishearing_cnt,
        "idioms_anti_literal_count": total_idioms_cnt,
        "user_glossary_count": len(user_terms),
        "supported_lang_pairs": list(SUPPORTED_LANG_PAIRS),
        "lang_pairs": lang_pairs,
        "lexicon_by_lang": lexicon_by_lang,
        "has_en_zh_index": (root / "en_zh" / "corpus_index.db").is_file(),
    }
