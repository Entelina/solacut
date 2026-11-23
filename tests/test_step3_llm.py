"""Step 3（src/step3_llm.py）离线单元测试。

全部测试使用模拟 LLM 回调函数，不消耗真实 API 额度、不依赖外部网络，
重点验证时长自适应策略、长字幕分块、JSON 容错解析、重叠去重以及审片 Markdown 导出。
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.step3_llm import (  # noqa: E402
    _extract_json_payload,
    build_transcript_chunks,
    compute_adaptive_selection_plan,
    enrich_highlights_with_bilingual_subtitles,
    parse_and_validate_highlights,
    select_highlights_with_llm,
    translate_and_polish_highlight_subtitles,
)


class TestStep3Llm(unittest.TestCase):
    """Step 3 自适应策略、解析与端到端审片清单测试套件。"""

    def test_compute_adaptive_selection_plan(self) -> None:
        """测试 1：验证不同回放时长（15 分钟、90 分钟、4.6 小时）自适应档位与手动覆盖。"""
        # 15 分钟短回放
        short_plan = compute_adaptive_selection_plan(total_duration_sec=900)
        self.assertEqual(short_plan["target_count"], 4)
        self.assertEqual(short_plan["min_clip_sec"], 15)
        self.assertEqual(short_plan["max_clip_sec"], 45)
        self.assertTrue(short_plan["is_auto_count"])
        self.assertTrue(short_plan["is_auto_clip_range"])

        # 90 分钟常规直播
        medium_plan = compute_adaptive_selection_plan(total_duration_sec=5400)
        self.assertEqual(medium_plan["target_count"], 8)
        self.assertEqual(medium_plan["min_clip_sec"], 20)
        self.assertEqual(medium_plan["max_clip_sec"], 60)

        # 4.6 小时超长直播（16631s -> round(4.62*2.5)=12，且受限于 14 个窗口时保持 12）
        long_plan = compute_adaptive_selection_plan(
            total_duration_sec=16631.69,
            candidate_window_count=14,
        )
        self.assertEqual(long_plan["target_count"], 12)
        self.assertEqual(long_plan["min_clip_sec"], 25)
        self.assertEqual(long_plan["max_clip_sec"], 90)

        # 超长直播但候选窗口很少（例如只取了 5 个窗口），自动缩减到 5 条避免硬凑
        capped_plan = compute_adaptive_selection_plan(
            total_duration_sec=16631.69,
            candidate_window_count=5,
        )
        self.assertEqual(capped_plan["target_count"], 5)

        # 用户显式手动覆盖条数与时长范围
        manual_plan = compute_adaptive_selection_plan(
            total_duration_sec=16631.69,
            candidate_window_count=5,
            top_k=10,
            min_clip_sec=12,
            max_clip_sec=35,
        )
        self.assertEqual(manual_plan["target_count"], 10)
        self.assertEqual(manual_plan["min_clip_sec"], 12)
        self.assertEqual(manual_plan["max_clip_sec"], 35)
        self.assertFalse(manual_plan["is_auto_count"])
        self.assertFalse(manual_plan["is_auto_clip_range"])

    def test_build_transcript_chunks_splits_large_input(self) -> None:
        """测试 2：验证长字幕稿超过字符上限时能按窗口/行拆分，且不丢失任何字幕内容。"""
        windows = [
            {
                "window_id": 1,
                "start_sec": 10.0,
                "end_sec": 40.0,
                "start_str": "00:10",
                "end_str": "00:40",
                "max_peak_rms": 0.25,
            },
            {
                "window_id": 2,
                "start_sec": 100.0,
                "end_sec": 130.0,
                "start_str": "01:40",
                "end_str": "02:10",
                "max_peak_rms": 0.28,
            },
        ]
        segments = [
            {
                "window_id": 1,
                "start_sec": 12.0,
                "end_sec": 18.0,
                "start_str": "00:12",
                "end_str": "00:18",
                "is_high_volume": True,
                "peak_rms": 0.25,
                "text": "First window line one " + ("A" * 80),
            },
            {
                "window_id": 1,
                "start_sec": 19.0,
                "end_sec": 25.0,
                "start_str": "00:19",
                "end_str": "00:25",
                "is_high_volume": False,
                "peak_rms": 0.0,
                "text": "First window line two " + ("B" * 80),
            },
            {
                "window_id": 2,
                "start_sec": 105.0,
                "end_sec": 115.0,
                "start_str": "01:45",
                "end_str": "01:55",
                "is_high_volume": True,
                "peak_rms": 0.28,
                "text": "Second window line one " + ("C" * 80),
            },
        ]

        chunks = build_transcript_chunks(
            segments=segments,
            candidate_windows=windows,
            max_chars_per_chunk=360,
        )
        self.assertGreaterEqual(len(chunks), 2)
        joined = "\n".join(chunks)
        self.assertIn("First window line one", joined)
        self.assertIn("First window line two", joined)
        self.assertIn("Second window line one", joined)

    def test_parse_and_validate_highlights_handles_fences_and_overlap(self) -> None:
        """测试 3：验证 ```json 围栏剥离、非法项过滤、边界裁剪、高重叠去重与 selected 开关。"""
        fenced_reply = (
            "好的，以下是为您筛选的结果：\n"
            "```json\n"
            "{\n"
            '  "highlights": [\n'
            '    {"start_sec": 100.0, "end_sec": 140.0, "score": 9.3, "title_zh": "高分主选", "title_en": "Top Pick", "summary_zh": "高能反杀", "reason": "声音爆炸", "tags": ["高光", "反杀"]},\n'
            '    {"start_sec": 105.0, "end_sec": 138.0, "score": 7.8, "title_zh": "重叠低分项应被去重", "summary_zh": "重复片段"},\n'
            '    {"start_sec": 190.0, "end_sec": 250.0, "score": 8.6, "title_zh": "尾部越界需裁剪", "summary_zh": "最后收尾"},\n'
            '    {"start_sec": 50.0, "end_sec": 50.5, "score": 9.9, "title_zh": "太短无效"}\n'
            "  ]\n"
            "}\n"
            "```"
        )
        payload = _extract_json_payload(fenced_reply)
        items = parse_and_validate_highlights(
            raw_items=payload["highlights"],
            total_duration_sec=210.0,
            target_count=5,
        )

        # 太短(0.5s)的被丢弃，105~138 与 100~140 高度重叠被去重，剩下 2 条
        self.assertEqual(len(items), 2)

        first, second = items[0], items[1]
        self.assertEqual(first["rank"], 1)
        self.assertTrue(first["selected"])
        self.assertEqual(first["title_zh"], "高分主选")
        self.assertEqual(first["duration_sec"], 40.0)

        self.assertEqual(second["rank"], 2)
        self.assertTrue(second["selected"])
        self.assertEqual(second["start_sec"], 190.0)
        # 250 被安全裁剪到视频总时长 210.0
        self.assertEqual(second["end_sec"], 210.0)
        self.assertEqual(second["duration_sec"], 20.0)

    def test_select_highlights_end_to_end_with_mock_caller(self) -> None:
        """测试 4：用模拟 LLM 回调跑通 Step 3 全流程，验证 JSON 和带原台词的审片 Markdown。"""
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp = Path(tmp_dir)
            transcript_json = tmp / "demo_stream_transcript.json"
            out_dir = tmp / "custom_out"

            transcript_payload = {
                "video_path": "E:/videos/demo_stream.mp4",
                "total_duration_sec": 3600.0,
                "candidate_windows": [
                    {
                        "window_id": 1,
                        "start_sec": 600.0,
                        "end_sec": 645.0,
                        "start_str": "10:00",
                        "end_str": "10:45",
                        "max_peak_rms": 0.26,
                    }
                ],
                "segments": [
                    {
                        "window_id": 1,
                        "start_sec": 605.0,
                        "end_sec": 608.5,
                        "start_str": "10:05",
                        "end_str": "10:08",
                        "is_high_volume": False,
                        "peak_rms": 0.0,
                        "text": "Wait, let me check my storage folder.",
                    },
                    {
                        "window_id": 1,
                        "start_sec": 615.0,
                        "end_sec": 619.0,
                        "start_str": "10:15",
                        "end_str": "10:19",
                        "is_high_volume": True,
                        "peak_rms": 0.26,
                        "text": "Why is my homework folder 40 gigs?!",
                    },
                ],
            }
            transcript_json.write_text(
                json.dumps(transcript_payload, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )

            call_count = 0

            def _fake_llm(system_prompt: str, user_prompt: str) -> str:
                nonlocal call_count
                call_count += 1
                self.assertIn("Why is my homework folder 40 gigs?!", user_prompt)
                if call_count == 1:
                    return json.dumps(
                        {
                            "highlights": [
                                {
                                    "start_sec": 604.0,
                                    "end_sec": 628.0,
                                    "score": 9.4,
                                    "title_zh": "清硬盘惊现40G学习资料！主播当场破防",
                                    "title_en": "Why Is My Homework Folder 40GB?!",
                                    "summary_zh": "主播清理电脑硬盘时突然发现所谓学习资料文件夹高达 40GB，瞬间惊呼破防。",
                                    "reason": "强反差喜剧效果，伴随 0.26 峰值高音量反应。",
                                    "tags": ["整活名场面", "清硬盘"],
                                }
                            ]
                        },
                        ensure_ascii=False,
                    )
                return json.dumps(
                    {
                        "clips": [
                            {
                                "rank": 1,
                                "subtitles": [
                                    {
                                        "start_sec": 605.0,
                                        "end_sec": 608.5,
                                        "text_en": "Wait, let me check my storage folder.",
                                        "text_zh": "等等，我先看一眼我的硬盘文件夹。",
                                    },
                                    {
                                        "start_sec": 615.0,
                                        "end_sec": 619.0,
                                        "text_en": "Why is my homework folder 40GB?!",
                                        "text_zh": "为什么我的“学习资料”文件夹有40个G？！",
                                    },
                                ],
                            }
                        ]
                    },
                    ensure_ascii=False,
                )

            result = select_highlights_with_llm(
                transcript_json_path=transcript_json,
                output_dir=out_dir,
                bilingual_subtitles=True,
                llm_caller=_fake_llm,
            )

            json_out = Path(result["highlights_json_path"])
            md_out = Path(result["review_md_path"])
            self.assertTrue(json_out.is_file())
            self.assertTrue(md_out.is_file())
            self.assertEqual(json_out.name, "demo_stream_highlights.json")
            self.assertEqual(md_out.name, "demo_stream_review.md")
            self.assertEqual(result["highlight_count"], 1)
            self.assertTrue(result["bilingual_subtitles"])
            self.assertTrue(result["highlights"][0]["selected"])
            self.assertEqual(len(result["highlights"][0]["subtitles"]), 2)
            self.assertEqual(
                result["highlights"][0]["subtitles"][1]["text_zh"],
                "为什么我的“学习资料”文件夹有40个G？！",
            )

            md_text = md_out.read_text(encoding="utf-8")
            self.assertIn("清硬盘惊现40G学习资料！主播当场破防", md_text)
            self.assertIn("为什么我的“学习资料”文件夹有40个G？！", md_text)
            self.assertIn("Why is my homework folder 40GB?!", md_text)

    def test_enrich_existing_highlights_with_bilingual_subtitles(self) -> None:
        """测试 6：验证 --translate-only 模式可为已有 highlights.json 补齐双语字幕并保留勾选状态。"""
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp = Path(tmp_dir)
            tr_json = tmp / "vod_transcript.json"
            hl_json = tmp / "vod_highlights.json"

            tr_json.write_text(
                json.dumps(
                    {
                        "language": "en",
                        "segments": [
                            {
                                "start_sec": 10.0,
                                "end_sec": 22.0,
                                "text": "Hold on wear ends meet sucked me in yesterday, oh god I played till 9 am.",
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            hl_json.write_text(
                json.dumps(
                    {
                        "video_path": "vod.mp4",
                        "transcript_json_path": str(tr_json),
                        "total_duration_sec": 120.0,
                        "total_duration_str": "02:00",
                        "selection_plan": {
                            "duration_tier": "< 30分钟",
                            "target_count": 4,
                            "min_clip_sec": 15,
                            "max_clip_sec": 45,
                        },
                        "model": "deepseek-chat",
                        "highlight_count": 1,
                        "elapsed_sec": 1.0,
                        "highlights": [
                            {
                                "rank": 1,
                                "selected": False,  # 人工已取消勾选，补翻后必须原样保留
                                "start_sec": 9.0,
                                "end_sec": 23.0,
                                "duration_sec": 14.0,
                                "start_str": "00:09",
                                "end_str": "00:23",
                                "score": 9.0,
                                "title_zh": "沉迷燕云十六声到早九点",
                                "title_en": "Addicted to Where Winds Meet",
                                "summary_zh": "主播沉迷玩《燕云十六声》一直打到早上九点。",
                                "reason": "热梗吐槽",
                                "tags": ["燕云十六声"],
                            }
                        ],
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

            def _translate_mock(sys_p: str, usr_p: str) -> str:
                # 确认传入剧情梗概语境和原始台词时间轴
                self.assertIn("燕云十六声", usr_p)
                self.assertIn("[10.00 - 22.00]", usr_p)
                return json.dumps(
                    {
                        "clips": [
                            {
                                "rank": 1,
                                "subtitles": [
                                    {
                                        "start_sec": 10.0,
                                        "end_sec": 15.8,
                                        "text_en": "Hold on, Where Winds Meet sucked me in so bad yesterday!",
                                        "text_zh": "昨天玩《燕云十六声》直接玩上头了，",
                                    },
                                    {
                                        "start_sec": 15.8,
                                        "end_sec": 22.0,
                                        "text_en": "oh god I played until 9 AM.",
                                        "text_zh": "天哪我一口气打到了早上九点。",
                                    },
                                ],
                            }
                        ]
                    },
                    ensure_ascii=False,
                )

            updated = enrich_highlights_with_bilingual_subtitles(
                input_json_path=hl_json,
                llm_caller=_translate_mock,
            )
            self.assertTrue(updated["bilingual_subtitles"])
            hl_item = updated["highlights"][0]
            self.assertFalse(hl_item["selected"])
            self.assertGreaterEqual(len(hl_item["subtitles"]), 2)
            self.assertEqual(hl_item["subtitles"][0]["text_zh"], "昨天玩《燕云十六声》直接玩上头了，")

    def test_resegment_subtitles_monotonic_and_coverage_fallback(self) -> None:
        """测试 7：验证意群重组后的越界裁剪、重叠顺延修正，以及覆盖率不足时的安全兜底。"""
        highlights = [
            {
                "rank": 1,
                "start_sec": 10.0,
                "end_sec": 30.0,
                "title_zh": "测试高光",
                "summary_zh": "测试重组修正与覆盖率回退",
            }
        ]
        segments = [
            {"start_sec": 10.0, "end_sec": 19.0, "text": "First half talking about this awesome game."},
            {"start_sec": 19.5, "end_sec": 29.5, "text": "Second half never skip ending reaction here!"},
        ]

        # 场景 A：包含前越界(8.0)、中段重叠(14.0 < 15.0)与后越界(32.0)
        def _overlap_caller(sys_p: str, usr_p: str) -> str:
            return json.dumps(
                {
                    "clips": [
                        {
                            "rank": 1,
                            "subtitles": [
                                {"start_sec": 8.0, "end_sec": 15.0, "text_en": "First part.", "text_zh": "第一段话"},
                                {"start_sec": 14.0, "end_sec": 22.0, "text_en": "Middle part.", "text_zh": "中间连接"},
                                {"start_sec": 22.0, "end_sec": 32.0, "text_en": "Ending part!", "text_zh": "结尾反应！"},
                            ],
                        }
                    ]
                },
                ensure_ascii=False,
            )

        res_fixed = translate_and_polish_highlight_subtitles(
            highlights=highlights,
            transcript_segments=segments,
            llm_caller=_overlap_caller,
            source_language="en",
        )
        subs = res_fixed[0]["subtitles"]
        self.assertEqual(len(subs), 3)
        self.assertEqual(subs[0]["start_sec"], 10.0)
        self.assertEqual(subs[2]["end_sec"], 30.0)
        self.assertGreaterEqual(subs[1]["start_sec"], subs[0]["end_sec"])
        self.assertGreaterEqual(subs[2]["start_sec"], subs[1]["end_sec"])

        # 场景 B：大模型偷懒只返回前 2 秒（覆盖率 2/19 < 40%），应回退保留全部原句不丢失后半段
        def _lazy_caller(sys_p: str, usr_p: str) -> str:
            return json.dumps(
                {
                    "clips": [
                        {
                            "rank": 1,
                            "subtitles": [
                                {"start_sec": 10.0, "end_sec": 12.0, "text_en": "First half only.", "text_zh": "只翻开头"},
                            ],
                        }
                    ]
                },
                ensure_ascii=False,
            )

        res_fallback = translate_and_polish_highlight_subtitles(
            highlights=highlights,
            transcript_segments=segments,
            llm_caller=_lazy_caller,
            source_language="en",
        )
        self.assertEqual(res_fallback[0]["translation_status"], "fallback")
        fallback_subs = res_fallback[0]["subtitles"]
        joined_orig = " ".join(s["text_orig"] for s in fallback_subs)
        self.assertIn("never skip ending", joined_orig)

    def test_dual_role_routing_hash_rank_and_hotwords(self) -> None:
        """测试 8：验证粗筛与精修双角色独立路由、'#1' 排名字符串容错与热词动态注入。"""
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp = Path(tmp_dir)
            tr_json = tmp / "dual_transcript.json"
            tr_json.write_text(
                json.dumps(
                    {
                        "video_path": "dual.mp4",
                        "language": "en",
                        "total_duration_sec": 300.0,
                        "hotwords": "Shylily, Valorant",
                        "segments": [
                            {"start_sec": 20.0, "end_sec": 25.0, "text": "We just clutched that round!"}
                        ],
                    }
                ),
                encoding="utf-8",
            )

            seen = {"screen": 0, "polish": 0, "polish_prompt": ""}

            def _screen_caller(sys_p: str, usr_p: str) -> str:
                seen["screen"] += 1
                return json.dumps(
                    {
                        "highlights": [
                            {
                                "start_sec": 19.0,
                                "end_sec": 26.0,
                                "score": 9.1,
                                "title_zh": "残局反杀",
                                "title_en": "Clutch Round",
                                "summary_zh": "主播残局极限反杀拿下本回合。",
                                "reason": "操作高燃",
                                "tags": ["反杀"],
                            }
                        ]
                    },
                    ensure_ascii=False,
                )

            def _polish_caller(sys_p: str, usr_p: str) -> str:
                seen["polish"] += 1
                seen["polish_prompt"] = usr_p
                # 故意返回 "#1" 字符串排名验证正则容错
                return json.dumps(
                    {
                        "clips": [
                            {
                                "rank": "#1",
                                "subtitles": [
                                    {
                                        "start_sec": 20.0,
                                        "end_sec": 25.0,
                                        "text_en": "We just clutched that round!",
                                        "text_zh": "这波残局直接被咱们拿下了！",
                                    }
                                ],
                            }
                        ]
                    },
                    ensure_ascii=False,
                )

            res = select_highlights_with_llm(
                transcript_json_path=tr_json,
                output_dir=tmp,
                bilingual_subtitles=True,
                llm_caller=_screen_caller,
                polish_llm_caller=_polish_caller,
            )
            self.assertEqual(seen["screen"], 1)
            self.assertEqual(seen["polish"], 1)
            self.assertIn("Shylily, Valorant", seen["polish_prompt"])
            self.assertEqual(res["translated_ok_count"], 1)
            self.assertEqual(res["translation_fallback_count"], 0)
            self.assertEqual(res["highlights"][0]["translation_status"], "ok")
            self.assertEqual(res["highlights"][0]["subtitles"][0]["text_zh"], "这波残局直接被咱们拿下了！")

    def test_missing_api_key_raises_friendly_error(self) -> None:
        """测试 5：未配置 API Key 时应立刻报错并给出清晰的中文 .env 配置提示。"""
        with tempfile.TemporaryDirectory() as tmp_dir:
            transcript_json = Path(tmp_dir) / "no_key_transcript.json"
            transcript_json.write_text(
                json.dumps(
                    {
                        "video_path": "demo.mp4",
                        "total_duration_sec": 120.0,
                        "segments": [],
                    }
                ),
                encoding="utf-8",
            )

            with patch.dict(
                os.environ,
                {"OPENAI_API_KEY": "", "DEEPSEEK_API_KEY": "", "SCREEN_LLM_API_KEY": ""},
                clear=False,
            ):
                with self.assertRaises(RuntimeError) as ctx:
                    select_highlights_with_llm(
                        transcript_json_path=transcript_json,
                        api_key="",
                    )
                self.assertIn("OPENAI_API_KEY", str(ctx.exception))
                self.assertIn(".env", str(ctx.exception))

    def test_extract_json_trailing_comma_and_screen_retry_on_malformed_json(self) -> None:
        """测试 9：验证尾逗号容错、截断诊断提示以及粗筛偶发一次畸形 JSON 自动重试恢复。"""
        # 1. 尾逗号容错
        parsed = _extract_json_payload('{"highlights": [{"start_sec": 1.0, "end_sec": 5.0,},]}')
        self.assertEqual(len(parsed["highlights"]), 1)

        # 2. 截断报错提示清晰可见
        with self.assertRaises(ValueError) as ctx:
            _extract_json_payload('{"highlights": [{"title_en": "Begs for Par')
        self.assertIn("疑似输出被长度上限截断", str(ctx.exception))

        # 3. 粗筛首回返回截断 JSON，第二次返回合法 JSON -> 自动重试成功
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp = Path(tmp_dir)
            tr_json = tmp / "retry_transcript.json"
            tr_json.write_text(
                json.dumps(
                    {
                        "video_path": "retry.mp4",
                        "total_duration_sec": 120.0,
                        "segments": [{"start_sec": 10.0, "end_sec": 20.0, "text": "Huge play!"}],
                    }
                ),
                encoding="utf-8",
            )
            attempts = {"n": 0}

            def _flaky_screen(sys_p: str, usr_p: str) -> str:
                attempts["n"] += 1
                if attempts["n"] == 1:
                    return '{"highlights": [{"start_sec": 10.0, "title_en": "Truncated'
                return json.dumps(
                    {
                        "highlights": [
                            {
                                "start_sec": 10.0,
                                "end_sec": 20.0,
                                "score": 9.0,
                                "title_zh": "自动重试成功",
                                "title_en": "Retry Succeeded",
                                "summary_zh": "偶发截断后自动重跑恢复正常。",
                                "reason": "高燃操作",
                                "tags": ["高光"],
                            }
                        ]
                    },
                    ensure_ascii=False,
                )

            res = select_highlights_with_llm(
                transcript_json_path=tr_json,
                output_dir=tmp,
                bilingual_subtitles=False,
                llm_caller=_flaky_screen,
            )
            self.assertEqual(attempts["n"], 2)
            self.assertEqual(res["highlight_count"], 1)
            self.assertEqual(res["highlights"][0]["title_zh"], "自动重试成功")

    def test_commercial_subtitle_quality_gate_and_single_rank_retranslate(self) -> None:
        """测试 10：验证反懒人质检自动打回重写、微碎口癖合并，以及按 ranks 定点单条重翻 + extra_context 注入。"""
        # 1. 构造 10 行含口吃/残句的原始台词，首轮故意模拟偷懒 1:1 照抄，验证自动打回重写
        highlights = [
            {
                "rank": 1,
                "start_sec": 100.0,
                "end_sec": 122.0,
                "title_zh": "沉迷新游到天亮",
                "summary_zh": "主播沉迷新游玩到早上九点。",
            }
        ]
        raw_segs = [
            {"start_sec": 100.0 + i * 2.0, "end_sec": 102.0 + i * 2.0, "text": f"Line {i} like, uh,"}
            for i in range(10)
        ]

        seen_prompts: list[str] = []

        def _quality_gate_caller(sys_p: str, usr_p: str) -> str:
            seen_prompts.append(usr_p)
            if len(seen_prompts) == 1:
                # 第 1 轮偷懒 1:1 照抄，包含多个残句口吃
                lazy_subs = [
                    {
                        "start_sec": 100.0 + i * 2.0,
                        "end_sec": 100.7 + i * 2.0,
                        "text_en": "I need like,",
                        "text_zh": "我需要像，" if i % 2 == 0 else "像其他，呃，",
                    }
                    for i in range(10)
                ]
                return json.dumps({"clips": [{"rank": 1, "subtitles": lazy_subs}]}, ensure_ascii=False)
            # 第 2 轮收到质检打回后合并成完整自然意群 + 故意留一个 0.5s 微碎口癖测试自动吸收
            return json.dumps(
                {
                    "clips": [
                        {
                            "rank": 1,
                            "subtitles": [
                                {
                                    "start_sec": 100.0,
                                    "end_sec": 107.0,
                                    "text_en": "This game sucked me in so bad yesterday!",
                                    "text_zh": "这游戏昨天直接让我玩上头了！",
                                },
                                {
                                    "start_sec": 107.1,
                                    "end_sec": 107.6,
                                    "text_en": "I was like,",
                                    "text_zh": "我就想",
                                },
                                {
                                    "start_sec": 107.7,
                                    "end_sec": 115.0,
                                    "text_en": "I played until 9 AM and need parental controls.",
                                    "text_zh": "一路打到早上九点，真该给我装个家长控制",
                                },
                                {
                                    "start_sec": 115.0,
                                    "end_sec": 122.0,
                                    "text_en": "That out of context is wild.",
                                    "text_zh": "这话断章取义听起来太离谱了",
                                },
                            ],
                        }
                    ]
                },
                ensure_ascii=False,
            )

        polished = translate_and_polish_highlight_subtitles(
            highlights=highlights,
            transcript_segments=raw_segs,
            llm_caller=_quality_gate_caller,
            source_language="en",
            concurrency=1,
        )
        self.assertEqual(len(seen_prompts), 2)
        self.assertIn("质检打回整改要求", seen_prompts[1])
        self.assertEqual(polished[0]["translation_status"], "ok")
        # 0.5s 的“我就想”微碎句应被自动吸收，最终剩 3 条自然长句
        self.assertEqual(len(polished[0]["subtitles"]), 3)

        # 2. 验证 enrich_highlights_with_bilingual_subtitles 定点 ranks=[2] 单条重翻 + extra_context
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp = Path(tmp_dir)
            tr_json = tmp / "vod_transcript.json"
            hl_json = tmp / "vod_highlights.json"
            tr_json.write_text(
                json.dumps(
                    {
                        "language": "en",
                        "segments": [
                            {"start_sec": 10.0, "end_sec": 15.0, "text": "Clip one line."},
                            {"start_sec": 50.0, "end_sec": 56.0, "text": "Warrens meat is so fun!"},
                        ],
                    }
                ),
                encoding="utf-8",
            )
            hl_json.write_text(
                json.dumps(
                    {
                        "video_path": "vod.mp4",
                        "transcript_json_path": str(tr_json),
                        "total_duration_sec": 100.0,
                        "total_duration_str": "01:40",
                        "selection_plan": {
                            "duration_tier": "< 30分钟",
                            "target_count": 4,
                            "min_clip_sec": 15,
                            "max_clip_sec": 45,
                        },
                        "model": "deepseek-flash",
                        "highlight_count": 2,
                        "elapsed_sec": 1.0,
                        "highlights": [
                            {
                                "rank": 1,
                                "selected": True,
                                "start_sec": 10.0,
                                "end_sec": 15.0,
                                "duration_sec": 5.0,
                                "start_str": "00:10",
                                "end_str": "00:15",
                                "score": 9.0,
                                "title_zh": "保留不动的片段1",
                                "title_en": "Keep One",
                                "summary_zh": "保留不动",
                                "reason": "",
                                "tags": [],
                                "translation_status": "ok",
                                "subtitles": [
                                    {
                                        "start_sec": 10.0,
                                        "end_sec": 15.0,
                                        "text_orig": "Clip one line.",
                                        "text_zh": "人工已精修好的字幕1",
                                        "text_en": "Clip one line.",
                                    }
                                ],
                            },
                            {
                                "rank": 2,
                                "selected": False,
                                "start_sec": 50.0,
                                "end_sec": 56.0,
                                "duration_sec": 6.0,
                                "start_str": "00:50",
                                "end_str": "00:56",
                                "score": 8.5,
                                "title_zh": "待重翻片段2",
                                "title_en": "Redo Two",
                                "summary_zh": "聊燕云十六声",
                                "reason": "",
                                "tags": [],
                                "translation_status": "fallback",
                                "subtitles": [],
                            },
                        ],
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

            called_prompts: list[str] = []

            def _single_redo(sys_p: str, usr_p: str) -> str:
                called_prompts.append(usr_p)
                return json.dumps(
                    {
                        "clips": [
                            {
                                "rank": 2,
                                "subtitles": [
                                    {
                                        "start_sec": 50.0,
                                        "end_sec": 56.0,
                                        "text_en": "Where Winds Meet is so fun!",
                                        "text_zh": "《燕云十六声》太好玩了！",
                                    }
                                ],
                            }
                        ]
                    },
                    ensure_ascii=False,
                )

            res = enrich_highlights_with_bilingual_subtitles(
                input_json_path=hl_json,
                hotwords="Where Winds Meet",
                extra_context="本段在聊国产武侠游戏《燕云十六声》",
                ranks=[2],
                llm_caller=_single_redo,
            )
            self.assertEqual(len(called_prompts), 1)
            self.assertIn("燕云十六声", called_prompts[0])
            # 片段 1 原封不动保留
            self.assertEqual(res["highlights"][0]["subtitles"][0]["text_zh"], "人工已精修好的字幕1")
            # 片段 2 成功重翻并保留原 selected=False 状态
            self.assertFalse(res["highlights"][1]["selected"])
            self.assertEqual(res["highlights"][1]["translation_status"], "ok")
            self.assertEqual(res["highlights"][1]["subtitles"][0]["text_zh"], "《燕云十六声》太好玩了！")

    def test_zero_omission_surgical_repair_and_multilingual_untranslated_detection(self) -> None:
        """测试 11：验证多语种精准漏翻判定（含日语假名残留/中译英汉字残留）与单句/时间黑洞手术刀级定点补翻闭环。"""
        from src.step3_llm import _is_subtitle_untranslated

        # 1. 多语种漏翻判定精度验证
        # 日语含汉字但残留平假名 -> 必须被抓出漏翻（旧版仅查 _CJK_RE 会漏网！）
        self.assertTrue(
            _is_subtitle_untranslated(
                {
                    "text_orig": "マジでやばい、時間がないよ！",
                    "text_en": "マジでやばい、時間がないよ！",
                    "text_zh": "マジでやばい、時間がないよ！",
                },
                source_language="ja",
            )
        )
        # 日语正确译成简体中文 -> 不误报
        self.assertFalse(
            _is_subtitle_untranslated(
                {
                    "text_orig": "マジでやばい、時間がないよ！",
                    "text_en": "マジでやばい、時間がないよ！",
                    "text_zh": "卧槽完蛋了，时间不够了！",
                },
                source_language="ja",
            )
        )
        # 韩语残留谚文 -> 抓出
        self.assertTrue(
            _is_subtitle_untranslated(
                {"text_orig": "아니 진짜 미쳤어!", "text_en": "아니 진짜 미쳤어!", "text_zh": "진짜 牛逼！"},
                source_language="ko",
            )
        )
        # 俄语残留西里尔 -> 抓出
        self.assertTrue(
            _is_subtitle_untranslated(
                {"text_orig": "Жесть, как он попал?", "text_en": "Жесть, как он попал?", "text_zh": "Жесть 怎么打中的？"},
                source_language="ru",
            )
        )
        # 中文出海（zh_en）text_en 残留中文 -> 抓出
        self.assertTrue(
            _is_subtitle_untranslated(
                {"text_orig": "这波直接破防了", "text_zh": "这波直接破防了", "text_en": "这波直接破防了"},
                source_language="zh",
            )
        )

        # 2. 端到端定点手术补翻：模拟大模型漏翻 1 条英文 + 跳过中间 1 条 1.6s 实质原声台词
        highlights = [
            {
                "rank": 1,
                "start_sec": 100.0,
                "end_sec": 112.0,
                "title_zh": "绝境吐槽",
                "summary_zh": "主播连续吐槽游戏判定并发现删错存档。",
            }
        ]
        segments = [
            {"start_sec": 100.0, "end_sec": 103.0, "text": "Wait a second, this boss is insane!"},
            {"start_sec": 103.2, "end_sec": 105.0, "text": "Why did you delete the save file?"},
            {"start_sec": 105.5, "end_sec": 108.5, "text": "We literally spent four hours grinding that!"},
            {"start_sec": 108.8, "end_sec": 111.8, "text": "I cannot believe this actually happened."},
        ]
        calls: list[str] = []

        def _mock_caller_with_one_omission(sys_p: str, usr_p: str) -> str:
            calls.append(usr_p)
            if "待定点补翻台词" in usr_p:
                # 定点补翻调用：补齐 1 条未译中文 + 1 条被跳过的原声时间黑洞
                return json.dumps(
                    {
                        "repairs": [
                            {
                                "id": 1,
                                "text_en": "I cannot believe this actually happened.",
                                "text_zh": "我真不敢相信这事居然真的发生了！",
                            },
                            {
                                "id": 2,
                                "text_en": "Why did you delete the save file?",
                                "text_zh": "你怎么把存档文件给删了啊！？",
                            },
                        ]
                    },
                    ensure_ascii=False,
                )
            # 初版故意漏跳过 103.2-105.0（时间黑洞），且最后一句 text_zh 漏翻直接抄英文
            return json.dumps(
                {
                    "clips": [
                        {
                            "rank": 1,
                            "subtitles": [
                                {
                                    "start_sec": 100.0,
                                    "end_sec": 103.0,
                                    "text_en": "Wait a second, this boss is insane!",
                                    "text_zh": "等等，这Boss也太变态了吧！",
                                },
                                {
                                    "start_sec": 105.5,
                                    "end_sec": 108.5,
                                    "text_en": "We literally spent four hours grinding that!",
                                    "text_zh": "咱们整整爆肝刷了四个小时啊！",
                                },
                                {
                                    "start_sec": 108.8,
                                    "end_sec": 111.8,
                                    "text_en": "I cannot believe this actually happened.",
                                    "text_zh": "I cannot believe this actually happened.",
                                },
                            ],
                        }
                    ]
                },
                ensure_ascii=False,
            )

        res = translate_and_polish_highlight_subtitles(
            highlights=highlights,
            transcript_segments=segments,
            llm_caller=_mock_caller_with_one_omission,
            source_language="en",
            concurrency=1,
        )
        # 1/3 未译 (>=15%) 先触发 1 次整段质检打回重写，重写后仍残存漏句与黑洞 -> 自动启动手术刀级定点补翻（共 3 次调用）
        self.assertEqual(len(calls), 3, "应先触发整段质检重写，再由手术刀级定点补翻彻底清零漏句")
        self.assertTrue(any("质检打回整改要求" in p for p in calls))
        self.assertTrue(any("待定点补翻台词" in p for p in calls))
        self.assertEqual(res[0]["translation_status"], "ok")
        subs = res[0]["subtitles"]
        self.assertEqual(len(subs), 4, "被跳过的原声时间黑洞必须被精准插回补齐")
        # 校验单调递增且全部 4 句 0 漏翻
        for i, row in enumerate(subs):
            self.assertFalse(_is_subtitle_untranslated(row, source_language="en"), f"第 {i+1} 句仍有漏翻: {row}")
            if i > 0:
                self.assertGreaterEqual(row["start_sec"], subs[i - 1]["end_sec"])
        self.assertEqual(subs[1]["text_zh"], "你怎么把存档文件给删了啊！？")
        self.assertEqual(subs[3]["text_zh"], "我真不敢相信这事居然真的发生了！")


if __name__ == "__main__":
    unittest.main()
