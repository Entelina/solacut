"""开源双语语料清洗、SQLite FTS5 索引、毫秒级专家翻译记忆 RAG 与 Web 专属词库单元测试。"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import src.web_app as web_app  # noqa: E402
from src.corpus_builder import build_fts_index, clean_and_score_pair  # noqa: E402
from src.corpus_rag import (  # noqa: E402
    DEFAULT_CORPUS_ROOT,
    SUPPORTED_LANG_PAIRS,
    detect_source_language,
    format_rag_reference_block,
    get_corpus_stats,
    retrieve_translation_references,
    save_user_glossary,
)
from src.step3_llm import _build_bilingual_prompts  # noqa: E402


class TestCorpusRag(unittest.TestCase):
    """验证语料质量清洗门控、FTS5 BM25 检索、术语防误命中与 Web 专属词库闭环。"""

    def test_clean_and_score_pair_quality_gates(self) -> None:
        """测试 1：验证简体净化过滤繁体、去舞台音效与水印、过滤错行以及口语高分排序。"""
        # 1. 正常口语带舞台音效括号 -> 应清洗干净并给高分
        ok_pair = clean_and_score_pair(
            "(Laughter) Wait, I don't think that makes any sense!",
            "等一下，我觉得这根本不合逻辑！",
            lang_pair="en_zh",
            domain="spoken",
        )
        self.assertIsNotNone(ok_pair)
        assert ok_pair is not None
        self.assertNotIn("Laughter", ok_pair[0])
        self.assertGreater(ok_pair[2], 2.0)

        # 2. 繁体中文 -> 必须被简中质检拦截
        trad = clean_and_score_pair(
            "The weather was very bad yesterday.",
            "昨天的天氣壞透了。",
            lang_pair="en_zh",
        )
        self.assertIsNone(trad)

        # 3. 字幕组广告水印 -> 拦截
        wm = clean_and_score_pair(
            "Subtitles by www.example.com",
            "本字幕由某翻译组提供",
            lang_pair="en_zh",
        )
        self.assertIsNone(wm)

        # 4. 严重错行/漏译比例失衡 -> 拦截
        bad_ratio = clean_and_score_pair(
            "This is a very long English sentence talking about many things in great detail for testing.",
            "好的吧。",
            lang_pair="en_zh",
        )
        self.assertIsNone(bad_ratio)

    def test_fts_index_commercial_filter_and_user_glossary_priority(self) -> None:
        """测试 2：验证 FTS5 检索、commercial_only 合规过滤、词边界防误命中与专属词库优先。"""
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            en_dir = root / "en_zh"
            en_dir.mkdir(parents=True)

            # 写入微型平行语料（含商用安全与非商用两条）
            jsonl = en_dir / "open_parallel_corpus.jsonl"
            sample_rows = [
                {
                    "src": "I'm fighting the camera controls in this dungeon boss fight.",
                    "tgt": "打这个副本Boss的时候我一直在跟视角控制较劲。",
                    "lang_pair": "en_zh",
                    "source": "tatoeba_en_zh",
                    "domain": "spoken",
                    "license": "CC BY 2.0 FR",
                    "commercial_safe": True,
                    "quality_score": 2.4,
                },
                {
                    "src": "This boss fight camera angle is completely broken.",
                    "tgt": "这场Boss战的镜头角度彻底崩了。",
                    "lang_pair": "en_zh",
                    "source": "ted2020_en_zh",
                    "domain": "subtitle",
                    "license": "CC BY-NC-ND 4.0",
                    "commercial_safe": False,
                    "quality_score": 2.9,
                },
            ]
            jsonl.write_text(
                "\n".join(json.dumps(r, ensure_ascii=False) for r in sample_rows) + "\n",
                encoding="utf-8",
            )
            indexed = build_fts_index(jsonl, en_dir / "corpus_index.db")
            self.assertEqual(indexed, 2)

            # 写入公会专属词条（含 ASR 误听别名）
            save_user_glossary(
                [
                    {
                        "en": "Where Winds Meet",
                        "aliases": ["warrens meat"],
                        "zh": "《燕云十六声》",
                        "note": "主播常玩的国产武侠",
                    }
                ],
                corpus_root=root,
            )

            # 仅检索商用安全语料 -> 必须过滤掉 commercial_safe=False 的那条
            refs_comm = retrieve_translation_references(
                "Warrens meat boss fight camera angle is crazy!",
                source_language="en",
                commercial_only=True,
                corpus_root=root,
            )
            self.assertEqual(len(refs_comm["user_hits"]), 1)
            self.assertEqual(refs_comm["user_hits"][0]["zh"], "《燕云十六声》")
            self.assertEqual(len(refs_comm["parallel_exemplars"]), 1)
            self.assertTrue(refs_comm["parallel_exemplars"][0]["commercial_safe"])

            block = format_rag_reference_block(refs_comm)
            self.assertIn("公会/主播专属词库", block)
            self.assertIn("《燕云十六声》", block)
            self.assertIn("人类专家口语双语译文风格示范", block)

    def test_builtin_corpus_no_false_positive_and_web_api(self) -> None:
        """测试 3：验证项目内置专家语料库无短词误命中、Step 3 自动注入与 Web 管理接口闭环。"""
        # 验证普通英文句子里的 "of" / "interesting" / "guys" 不会误命中 OnlyFans / int / chat
        refs_clean = retrieve_translation_references(
            "One of the most interesting things you guys showed me today.",
            source_language="en",
            corpus_root=DEFAULT_CORPUS_ROOT,
        )
        hit_terms = {t["en"].lower() for t in refs_clean["term_hits"]}
        self.assertNotIn("onlyfans", hit_terms)
        self.assertNotIn("int", hit_terms)
        self.assertNotIn("chat", hit_terms)

        # 验证真实高光触发习语反直译 + ASR 误听纠偏并注入 Step 3 精修 Prompt
        _, usr_p = _build_bilingual_prompts(
            [
                {
                    "rank": 1,
                    "start_sec": 10.0,
                    "end_sec": 25.0,
                    "title_zh": "沉迷新游",
                    "summary_zh": "聊玩上头了",
                    "raw_lines": [
                        {
                            "start_sec": 10.0,
                            "end_sec": 18.0,
                            "text": "Warrens meat sucked me in so bad out of context!",
                        }
                    ],
                }
            ],
            source_language="en",
        )
        self.assertIn("Where Winds Meet", usr_p)
        self.assertIn("suck me in", usr_p)
        self.assertIn("严禁直译", usr_p)

        # 验证 Web /api/corpus/stats 与 /api/corpus/user-glossary 在隔离目录下读写正常
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_corpus = Path(tmp_dir)
            with patch.object(web_app, "_CORPUS_ROOT", tmp_corpus):
                client = TestClient(
                    web_app.app,
                    headers={"X-Solacut-Token": web_app._SESSION_TOKEN},
                )
                post_res = client.post(
                    "/api/corpus/user-glossary",
                    json={
                        "terms": [
                            {
                                "en": "Shylily",
                                "aliases": ["Shilley", "Shy Lily"],
                                "zh": "Shylily（虎鲸妹）",
                                "note": "英语区VTuber",
                            }
                        ]
                    },
                )
                self.assertEqual(post_res.status_code, 200)
                self.assertEqual(post_res.json()["count"], 1)

                get_res = client.get("/api/corpus/user-glossary")
                self.assertEqual(get_res.status_code, 200)
                self.assertEqual(get_res.json()["terms"][0]["en"], "Shylily")

                stats_res = client.get("/api/corpus/stats")
                self.assertEqual(stats_res.status_code, 200)
                self.assertEqual(stats_res.json()["user_glossary_count"], 1)

    def test_multilingual_detection_cleaning_trigram_and_lexicons(self) -> None:
        """测试 4：验证 6 大语种自动检测、清洗评分、CJK trigram / 西里尔 / 西语 FTS5 检索及全语种专家词库。"""
        # 1. 自动语种识别（覆盖 en / zh / ja / ko / ru / es）
        self.assertEqual(detect_source_language("マジでやばいじゃん、草生える！"), "ja")
        self.assertEqual(detect_source_language("아니 진짜 미쳤어 폼 미쳤다 레전드!"), "ko")
        self.assertEqual(detect_source_language("Жесть, он просто в соло затащил этот клатч!"), "ru")
        self.assertEqual(detect_source_language("¡No manches qué locura tío, la liaste brutal!"), "es")
        self.assertEqual(detect_source_language("这波弹反直接破防了，操作太下饭！"), "zh")
        self.assertEqual(detect_source_language("Bro we are actually cooked, let him cook!"), "en")

        # 2. 多语种清洗评分门控
        self.assertIsNotNone(
            clean_and_score_pair("マジでやばいじゃん、これ勝てるの？", "真的假的这也太离谱了吧，这能赢吗？", lang_pair="ja_zh")
        )
        # 日语源端不能混入纯中文无假名错行
        self.assertIsNone(clean_and_score_pair("今天天气非常好啊", "今天天气非常好啊", lang_pair="ja_zh"))
        self.assertIsNotNone(
            clean_and_score_pair("아니 진짜 미쳤어 이게 되네!", "不是哥们太离谱了这都能成！", lang_pair="ko_zh")
        )
        self.assertIsNotNone(
            clean_and_score_pair("这波残局直接被咱们拿下了！", "We actually clutched that round!", lang_pair="zh_en")
        )
        self.assertIsNotNone(
            clean_and_score_pair("Жесть, ты реально затащил эту катку!", "卧槽你真的单人带飞拿下这局了！", lang_pair="ru_zh")
        )
        self.assertIsNotNone(
            clean_and_score_pair("¡No manches qué locura de jugada!", "不是吧这也太疯狂的神操作了！", lang_pair="es_zh")
        )

        # 3. 隔离构建 ja_zh / ko_zh / zh_en (trigram) 与 ru_zh / es_zh (unicode61) FTS5 并验证精准检索
        samples_by_lp = {
            "ja_zh": (
                "マジでやばいじゃんこれ草生えるわ！",
                "卧槽这也太离谱了吧笑死我了！",
                "マジでやばい台パン不可避",
            ),
            "ko_zh": (
                "아니 진짜 미쳤어 레전드 각이다!",
                "不是哥们牛逼炸了，名场面要来了！",
                "진짜 미쳤어 폼 미쳤다",
            ),
            "zh_en": (
                "这波弹反失误直接让我破防了！",
                "That missed parry totally tilted me!",
                "弹反失误彻底破防了",
            ),
            "ru_zh": (
                "Жесть, красава просто в соло затащил клатч!",
                "卧槽漂亮兄弟，直接单骑救主拿下残局！",
                "Он реально затащил клатч в соло!",
            ),
            "es_zh": (
                "¡Qué locura tío, la rompiste brutal en esta ronda!",
                "太疯了哥们，这回合直接秀翻全场！",
                "Qué locura brutal en esta partida!",
            ),
        }
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            for lp, (src, tgt, query) in samples_by_lp.items():
                lp_dir = root / lp
                lp_dir.mkdir(parents=True)
                jsonl = lp_dir / "open_parallel_corpus.jsonl"
                row = {
                    "src": src,
                    "tgt": tgt,
                    "lang_pair": lp,
                    "source": f"test_{lp}",
                    "domain": "spoken",
                    "license": "CC BY 2.0",
                    "commercial_safe": True,
                    "quality_score": 2.5,
                }
                jsonl.write_text(json.dumps(row, ensure_ascii=False) + "\n", encoding="utf-8")
                cnt = build_fts_index(jsonl, lp_dir / "corpus_index.db", lang_pair=lp)
                self.assertEqual(cnt, 1)
                refs = retrieve_translation_references(query, source_language=lp, corpus_root=root)
                self.assertEqual(refs["lang_pair"], lp)
                self.assertEqual(len(refs["parallel_exemplars"]), 1, f"{lp} FTS5 未命中范例")
                self.assertEqual(refs["parallel_exemplars"][0]["tgt"], tgt)

        # 4. 验证项目内置 6 大语种原创专家词库全部就绪且检索命中
        stats = get_corpus_stats(DEFAULT_CORPUS_ROOT)
        self.assertEqual(set(stats["supported_lang_pairs"]), set(SUPPORTED_LANG_PAIRS))
        self.assertGreaterEqual(stats["gaming_terms_count"], 600)
        self.assertGreaterEqual(stats["asr_mishearings_count"], 140)
        self.assertGreaterEqual(stats["idioms_anti_literal_count"], 600)

        ja_refs = retrieve_translation_references("台パンしてマジでやばい草生える", source_language="ja")
        self.assertTrue(any("台パン" in t["src"] for t in ja_refs["term_hits"]))
        self.assertTrue(any("やばい" in d["expr"] for d in ja_refs["idiom_hits"]))

        ko_refs = retrieve_translation_references("아니 킹받네 페이커 폼 미쳤다 억까", source_language="ko")
        self.assertTrue(len(ko_refs["mishearing_hits"]) >= 1)
        self.assertTrue(any("킹받네" in d["expr"] for d in ko_refs["idiom_hits"]))

        zh_refs = retrieve_translation_references("黑神话弹反失败直接破防，这操作太下饭了", source_language="zh")
        self.assertEqual(zh_refs["lang_pair"], "zh_en")
        self.assertTrue(any("破防" in d["expr"] for d in zh_refs["idiom_hits"]))

        ru_refs = retrieve_translation_references("Жесть он в соло затащил клатч с авика!", source_language="ru")
        self.assertTrue(len(ru_refs["term_hits"]) >= 1)
        self.assertTrue(any("жесть" in d["expr"] for d in ru_refs["idiom_hits"]))

        es_refs = retrieve_translation_references("¡No manches qué locura tío, eres un manco!", source_language="es")
        self.assertTrue(len(es_refs["term_hits"]) >= 1)
        self.assertTrue(any("no manches" in d["expr"] for d in es_refs["idiom_hits"]))


if __name__ == "__main__":
    unittest.main()
