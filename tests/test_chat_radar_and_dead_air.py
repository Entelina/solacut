"""弹幕热力雷达（chat_radar）、死寂长空白段（Dead Air Map）与人工审校工作流综合单测。"""

from __future__ import annotations

import json
import sys
import tempfile
import time
import unittest
from pathlib import Path

import numpy as np

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.chat_radar import (  # noqa: E402
    analyze_chat_spikes,
    extract_hype_terms,
    fuse_audio_and_chat_peaks,
    parse_chat_file,
    save_chat_messages_db,
    search_chat_keywords,
    search_chat_messages_db,
)
from src.pipeline import (  # noqa: E402
    _highlights_cache_matches,
    _peaks_cache_matches,
    build_arg_parser as build_pipeline_arg_parser,
    scan_input_videos,
)
from src.step1_audio import (  # noqa: E402
    analyze_video_audio,
    detect_audio_peaks,
    detect_dead_air_segments,
)
from src.step3_llm import (  # noqa: E402
    compute_review_flags,
    enrich_highlights_with_bilingual_subtitles,
    export_review_markdown,
    select_highlights_with_llm,
)
from tests.test_step1_audio import _mux_black_mp4, _write_test_wav  # noqa: E402


class TestChatRadarAndDeadAir(unittest.TestCase):
    """验证弹幕雷达解析/安全/峰值/融合、死寂长空白检测与 Draft->Reviewed 人工审校闭环。"""

    def test_parse_four_chat_formats_and_filter_invalid(self) -> None:
        """测试 1：验证 Twitch JSON、B站 XML、YouTube JSONL、通用 JSON 四大格式解析与排序过滤。"""
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp = Path(tmp_dir)

            # 1. TwitchDownloader JSON（含乱序、负时间、空白弹幕）
            twitch_path = tmp / "vod_chat.json"
            twitch_payload = {
                "video": {"length": 120.5},
                "comments": [
                    {
                        "content_offset_seconds": 52.4,
                        "message": {"body": "OMEGALUL what a clutch!"},
                        "commenter": {"display_name": "ViewerB"},
                    },
                    {
                        "content_offset_seconds": -3.0,
                        "message": {"body": "pre-stream should be filtered"},
                        "commenter": {"display_name": "Early"},
                    },
                    {
                        "content_offset_seconds": 12.3,
                        "message": {"body": "   "},
                        "commenter": {"display_name": "Blank"},
                    },
                    {
                        "content_offset_seconds": 10.2,
                        "message": {"body": "KEKW let's go"},
                        "commenter": {"display_name": "ViewerA"},
                    },
                ],
            }
            twitch_path.write_text(json.dumps(twitch_payload, ensure_ascii=False), encoding="utf-8")
            res_tw = parse_chat_file(twitch_path)
            self.assertEqual(res_tw["format"], "twitch_json")
            self.assertEqual(res_tw["message_count"], 2)
            self.assertEqual(res_tw["duration_sec"], 120.5)
            self.assertEqual([m["time_sec"] for m in res_tw["messages"]], [10.2, 52.4])
            self.assertEqual(res_tw["messages"][0]["author"], "ViewerA")
            self.assertIn("OMEGALUL", res_tw["messages"][1]["text"])

            # 2. B站 XML 弹幕
            bili_path = tmp / "bilibili_danmaku.xml"
            bili_xml = (
                '<?xml version="1.0" encoding="UTF-8"?>\n'
                "<i>\n"
                '  <d p="123.45,1,25,16777215,1700000100,0,u_hash_2,1002">草 笑死我了 666</d>\n'
                '  <d p="-1.0,1,25,16777215,1700000000,0,u_bad,1000">负时间过滤</d>\n'
                '  <d p="15.5,1,25,16777215,1700000010,0,u_hash_1,1001">   </d>\n'
                '  <d p="20.0,1,25,16777215,1700000020,0,u_hash_3,1003">前方高能 &amp; 名场面</d>\n'
                "</i>\n"
            )
            bili_path.write_text(bili_xml, encoding="utf-8")
            res_bili = parse_chat_file(bili_path)
            self.assertEqual(res_bili["format"], "bilibili_xml")
            self.assertEqual(res_bili["message_count"], 2)
            self.assertEqual(res_bili["duration_sec"], 123.45)
            self.assertEqual(res_bili["messages"][0]["text"], "前方高能 & 名场面")
            self.assertEqual(res_bili["messages"][0]["author"], "u_hash_3")
            self.assertEqual(res_bili["messages"][1]["time_sec"], 123.45)

            # 3. YouTube yt-dlp Live Chat JSONL（含自定义表情 shortcuts 与 SC 醒目留言）
            yt_path = tmp / "stream.live_chat.jsonl"
            yt_lines = [
                json.dumps(
                    {
                        "replayChatItemAction": {
                            "videoOffsetTimeMsec": "-5000",
                            "actions": [
                                {
                                    "addChatItemAction": {
                                        "item": {
                                            "liveChatTextMessageRenderer": {
                                                "authorName": {"simpleText": "Pre"},
                                                "message": {"runs": [{"text": "ignored negative"}]},
                                            }
                                        }
                                    }
                                }
                            ],
                        }
                    }
                ),
                json.dumps(
                    {
                        "replayChatItemAction": {
                            "videoOffsetTimeMsec": "65400",
                            "actions": [
                                {
                                    "addChatItemAction": {
                                        "item": {
                                            "liveChatTextMessageRenderer": {
                                                "authorName": {"simpleText": "JPViewer"},
                                                "message": {
                                                    "runs": [
                                                        {"text": "わろたwww "},
                                                        {"emoji": {"shortcuts": [":POG:"]}},
                                                    ]
                                                },
                                            }
                                        }
                                    }
                                }
                            ],
                        }
                    }
                ),
                json.dumps(
                    {
                        "replayChatItemAction": {
                            "videoOffsetTimeMsec": "30200",
                            "actions": [
                                {
                                    "addChatItemAction": {
                                        "item": {
                                            "liveChatPaidMessageRenderer": {
                                                "authorName": {"simpleText": "SuperChatter"},
                                                "message": {"runs": [{"text": "NO WAY CLUTCH!!"}]},
                                            }
                                        }
                                    }
                                }
                            ],
                        }
                    }
                ),
            ]
            yt_path.write_text("\n".join(yt_lines) + "\n", encoding="utf-8")
            res_yt = parse_chat_file(yt_path)
            self.assertEqual(res_yt["format"], "youtube_live_chat")
            self.assertEqual(res_yt["message_count"], 2)
            self.assertEqual(res_yt["messages"][0]["time_sec"], 30.2)
            self.assertEqual(res_yt["messages"][0]["author"], "SuperChatter")
            self.assertIn("わろたwww", res_yt["messages"][1]["text"])
            self.assertIn(":POG:", res_yt["messages"][1]["text"])

            # 4. 通用精简 JSON 列表（兼容 time_sec / timestamp / offset_sec 与 text / body / message）
            gen_path = tmp / "generic_chat.json"
            gen_payload = [
                {"offset_sec": 88.0, "message": "ㅋㅋㅋ 레전드 미쳤다"},
                {"timestamp": 12.5, "body": "jajaja qué locura"},
                {"time_sec": 45.0, "text": "лол жесть", "author": "Ivan"},
                {"time_sec": -2.0, "text": "invalid"},
            ]
            gen_path.write_text(json.dumps(gen_payload, ensure_ascii=False), encoding="utf-8")
            res_gen = parse_chat_file(gen_path)
            self.assertEqual(res_gen["format"], "generic_json")
            self.assertEqual(res_gen["message_count"], 3)
            self.assertEqual([m["time_sec"] for m in res_gen["messages"]], [12.5, 45.0, 88.0])
            self.assertEqual(res_gen["messages"][1]["author"], "Ivan")

    def test_xml_xxe_billion_laughs_and_file_size_guards(self) -> None:
        """测试 2：安全防御测试——严格拒绝 XXE、Billion Laughs 恶意 XML 及超体积文件。"""
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp = Path(tmp_dir)

            # XXE 攻击样本
            xxe_xml = tmp / "xxe_attack.xml"
            xxe_xml.write_text(
                '<?xml version="1.0"?>\n'
                '<!DOCTYPE foo [ <!ENTITY xxe SYSTEM "file:///etc/passwd"> ]>\n'
                '<i><d p="1.0,1,25,16777215,0,0,a,1">&xxe;</d></i>\n',
                encoding="utf-8",
            )
            with self.assertRaises(ValueError) as ctx_xxe:
                parse_chat_file(xxe_xml)
            self.assertIn("DOCTYPE", str(ctx_xxe.exception))

            # Billion Laughs 实体扩展炸弹样本
            laughs_xml = tmp / "billion_laughs.xml"
            laughs_xml.write_text(
                '<?xml version="1.0"?>\n'
                "<!DOCTYPE lolz [\n"
                '  <!ENTITY lol "lol">\n'
                '  <!ENTITY lol2 "&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;">\n'
                '  <!ENTITY lol3 "&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;">\n'
                "]>\n"
                '<i><d p="10.0,1,25,16777215,0,0,a,1">&lol3;</d></i>\n',
                encoding="utf-8",
            )
            with self.assertRaises(ValueError):
                parse_chat_file(laughs_xml)

            # 文件体积超限防御
            oversized_json = tmp / "big_chat.json"
            oversized_json.write_text(json.dumps([{"time_sec": i, "text": "x" * 20} for i in range(40)]), encoding="utf-8")
            with self.assertRaises(ValueError) as ctx_size:
                parse_chat_file(oversized_json, max_file_mb=0.0001)
            self.assertIn("体积过大", str(ctx_size.exception))

    def test_chat_spikes_multilingual_search_and_audio_chat_fusion(self) -> None:
        """测试 3：验证多语种梗词检测、热力曲线、梗词搜索与音弹双雷达融合。"""
        # 验证多语种高能词识别
        self.assertIn("OMEGALUL", extract_hype_terms("OMEGALUL NO WAY"))
        self.assertIn("草", extract_hype_terms("草 笑死我了 666"))
        self.assertIn("www", extract_hype_terms("えぐいwww わろた"))
        self.assertIn("ㅋㅋ", extract_hype_terms("ㅋㅋㅋ 레전드"))
        self.assertIn("лол", extract_hype_terms("лол ахахах"))
        self.assertIn("jajaja", extract_hype_terms("jajajaja XD"))

        # 构造 180 秒弹幕流：平时每 5 秒 1 条平淡弹幕，在 50~58s（英文爆点）和 120~128s（无声绝杀中文刷屏）制造双高峰
        messages: list[dict[str, object]] = []
        for sec in range(0, 180, 5):
            if 50 <= sec < 60 or 120 <= sec < 130:
                continue
            messages.append({"time_sec": sec + 1.0, "text": "nice stream hello", "author": f"u{sec}"})

        # 爆点 1（50~58s）：英文 OMEGALUL + KEKW 密集刷屏（14 条）
        for i in range(14):
            messages.append(
                {
                    "time_sec": 50.5 + i * 0.5,
                    "text": "OMEGALUL KEKW clip it!!!" if i % 2 == 0 else "LMAOOO no way",
                    "author": f"en_{i}",
                }
            )

        # 爆点 2（120~128s）：冷面无声极限操作，中文 666/草/卧槽 刷屏（16 条）
        for i in range(16):
            messages.append(
                {
                    "time_sec": 120.2 + i * 0.45,
                    "text": "草 666666 卧槽名场面" if i % 2 == 0 else "离谱 绷不住了 ？？？",
                    "author": f"zh_{i}",
                }
            )

        radar = analyze_chat_spikes(
            messages,
            duration_sec=180.0,
            bucket_sec=5.0,
            custom_keywords=["名场面"],
            top_k=10,
        )
        self.assertEqual(len(radar["buckets"]), 36)
        norms = [b["norm"] for b in radar["buckets"]]
        self.assertGreaterEqual(min(norms), 0.0)
        self.assertLessEqual(max(norms), 1.0)
        self.assertAlmostEqual(max(norms), 1.0, places=4)

        peaks = radar["chat_peaks"]
        self.assertGreaterEqual(len(peaks), 2)
        peak_centers = [p["peak_sec"] for p in peaks]
        self.assertTrue(any(48.0 <= c <= 62.0 for c in peak_centers), f"未检出 50~58s 爆点：{peaks}")
        self.assertTrue(any(118.0 <= c <= 132.0 for c in peak_centers), f"未检出 120~128s 爆点：{peaks}")
        for p in peaks:
            for req_key in (
                "rank",
                "start_sec",
                "end_sec",
                "peak_sec",
                "msg_count",
                "msgs_per_sec",
                "z_score",
                "top_terms",
                "sample_messages",
            ):
                self.assertIn(req_key, p)
            self.assertGreater(p["z_score"], 2.5)
            self.assertTrue(p["top_terms"])
            self.assertTrue(p["sample_messages"])

        # 独立梗词检索：同时搜 OMEGALUL 与 草
        search_res = search_chat_keywords(messages, "OMEGALUL, 草", bucket_sec=10.0)
        self.assertEqual(search_res["terms"], ["OMEGALUL", "草"])
        self.assertEqual(search_res["match_count"], 15)  # 7 条 OMEGALUL + 8 条 草
        self.assertTrue(search_res["curve"])
        self.assertTrue(search_res["top_buckets"])

        # 音弹双雷达融合：
        # - 音频 47~54s 与弹幕 50~60s 产生共振 -> audio+chat 且应排第 1
        # - 音频 10~15s 无弹幕 -> audio
        # - 弹幕 120~130s 无音频（冷面绝杀）-> chat 并稳定进入候选池
        audio_peaks = [
            {"start_sec": 10.0, "end_sec": 15.0, "peak_rms": 0.22, "mean_rms": 0.14},
            {"start_sec": 47.0, "end_sec": 54.0, "peak_rms": 0.30, "mean_rms": 0.20},
        ]
        fused = fuse_audio_and_chat_peaks(audio_peaks=audio_peaks, chat_peaks=peaks, top_k=10)
        self.assertGreaterEqual(len(fused), 3)
        self.assertEqual(fused[0]["fusion_source"], "audio+chat")
        self.assertLessEqual(fused[0]["start_sec"], 50.0)
        self.assertGreaterEqual(fused[0]["end_sec"], 55.0)
        sources = {item["fusion_source"] for item in fused}
        self.assertEqual(sources, {"audio+chat", "chat", "audio"})
        chat_only = next(item for item in fused if item["fusion_source"] == "chat")
        self.assertGreaterEqual(chat_only["start_sec"], 115.0)
        self.assertLessEqual(chat_only["end_sec"], 135.0)

        # 完全均匀平淡弹幕绝不误报峰值
        flat_msgs = [{"time_sec": float(i * 5 + 1), "text": "normal message", "author": "u"} for i in range(30)]
        flat_res = analyze_chat_spikes(flat_msgs, duration_sec=150.0, bucket_sec=5.0)
        self.assertEqual(flat_res["chat_peaks"], [])

    def test_detect_dead_air_segments_preserves_short_pauses_and_flags_long_silence(self) -> None:
        """测试 4：验证 Dead Air Map 绝不误标 3 秒正常停顿，同时精准标出 22 秒挂机静音并写入 JSON。"""
        # 构造 100 秒每秒 RMS 曲线：
        # 0~20s 正常说话 | 20~23s 3秒正常停顿(0.0) | 23~50s 说话+轻声吐槽(0.03~0.14)
        # 50~72s 22秒挂机死寂(0.0002) | 72~100s 正常说话
        rms = np.full(100, 0.12, dtype=np.float64)
        rms[20:23] = 0.0
        rms[30:35] = 0.035  # 轻声吐槽绝不可误判为死寂
        rms[50:72] = 0.0002

        dead_segs = detect_dead_air_segments(
            rms_curve=rms,
            hop_sec=1.0,
            total_duration_sec=100.0,
            min_dead_air_sec=15.0,
        )
        self.assertEqual(len(dead_segs), 1)
        seg = dead_segs[0]
        self.assertEqual(seg["start_sec"], 50.0)
        self.assertEqual(seg["end_sec"], 72.0)
        self.assertEqual(seg["duration_sec"], 22.0)
        self.assertEqual(seg["start_str"], "00:50")
        self.assertEqual(seg["end_str"], "01:12")

        # 端到端 MP4 验证：3 秒短停顿忽略 + 18 秒死寂段写入 *_audio_peaks.json（兼容 detect_audio_peaks 别名）
        self.assertIs(detect_audio_peaks, analyze_video_audio)
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp = Path(tmp_dir)
            raw_wav = tmp / "dead_air_raw.wav"
            demo_mp4 = tmp / "dead_air_stream.mp4"
            out_dir = tmp / "out"
            _write_test_wav(
                raw_wav,
                segments=[
                    (4.0, 0.25),
                    (3.0, 0.0),   # 3 秒正常气口停顿 -> 必须忽略
                    (4.0, 0.70),
                    (18.0, 0.0),  # 18 秒长挂机空白 -> 必须标出
                    (4.0, 0.30),
                ],
            )
            _mux_black_mp4(raw_wav, demo_mp4, duration_sec=33.0)

            res = detect_audio_peaks(video_path=demo_mp4, output_dir=out_dir, window_sec=1.0)
            self.assertIn("dead_air_segments", res)
            self.assertIn("dead_air_total_sec", res)
            self.assertEqual(len(res["dead_air_segments"]), 1)
            self.assertGreaterEqual(res["dead_air_total_sec"], 16.0)
            self.assertGreaterEqual(res["dead_air_segments"][0]["start_sec"], 10.0)

            persisted = json.loads(Path(res["json_path"]).read_text(encoding="utf-8"))
            self.assertEqual(persisted["dead_air_segments"], res["dead_air_segments"])
            self.assertEqual(persisted["dead_air_total_sec"], res["dead_air_total_sec"])

    def test_step3_review_status_flags_markdown_and_partial_retranslate(self) -> None:
        """测试 5：验证 Step 3 默认 draft、三类 review_flags、单条重翻保 reviewed 及 pipeline 去 Slop 提示。"""
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp = Path(tmp_dir)
            in_dir = tmp / "input"
            out_dir = tmp / "output"
            in_dir.mkdir()
            out_dir.mkdir()
            fake_video = in_dir / "review_demo.mp4"
            fake_video.write_bytes(b"fake")

            transcript_json = out_dir / "review_demo_transcript.json"
            transcript_payload = {
                "video_path": str(fake_video),
                "total_duration_sec": 1200.0,
                "language": "en",
                "segments": [
                    {
                        "start_sec": 10.0,
                        "end_sec": 15.0,
                        "start_str": "00:10",
                        "end_str": "00:15",
                        "is_high_volume": True,
                        "peak_rms": 0.25,
                        "text": "Let him cook, we're cooked, skill issue!",
                    },
                    {
                        "start_sec": 16.0,
                        "end_sec": 22.0,
                        "start_str": "00:16",
                        "end_str": "00:22",
                        "is_high_volume": False,
                        "peak_rms": 0.08,
                        "text": "That game really sucked me in, out of context this sounds wild.",
                    },
                    {
                        "start_sec": 200.0,
                        "end_sec": 205.0,
                        "start_str": "03:20",
                        "end_str": "03:25",
                        "is_high_volume": True,
                        "peak_rms": 0.30,
                        "text": "Wait hold on, this clip goes on for a long time.",
                    },
                ],
            }
            transcript_json.write_text(json.dumps(transcript_payload, ensure_ascii=False), encoding="utf-8")

            def _screen_caller(sys_p: str, usr_p: str) -> str:
                return json.dumps(
                    {
                        "highlights": [
                            {
                                "start_sec": 10.0,
                                "end_sec": 25.0,
                                "score": 9.1,
                                "title_zh": "高能黑话连击",
                                "title_en": "Idiom Combo",
                                "summary_zh": "主播连续飙出多个直播热梗。",
                                "reason": "梗密度极高",
                                "tags": ["热梗"],
                            },
                            {
                                "start_sec": 198.0,
                                "end_sec": 295.0,  # 97 秒 -> 应命中 long_clip
                                "score": 8.4,
                                "title_zh": "超长剧情回顾",
                                "title_en": "Long Story Clip",
                                "summary_zh": "一段 97 秒的长叙事片段。",
                                "reason": "完整故事",
                                "tags": ["长段"],
                            },
                        ]
                    },
                    ensure_ascii=False,
                )

            def _polish_caller_first_run(sys_p: str, usr_p: str) -> str:
                if "片段 #1" in usr_p:
                    return json.dumps(
                        {
                            "clips": [
                                {
                                    "rank": 1,
                                    "subtitles": [
                                        {
                                            "start_sec": 10.0,
                                            "end_sec": 15.0,
                                            "text_en": "Let him cook, we're cooked, skill issue!",
                                            "text_zh": "让他秀，咱彻底寄了，纯纯菜！",
                                        },
                                        {
                                            "start_sec": 16.0,
                                            "end_sec": 22.0,
                                            "text_en": "That game really sucked me in, out of context this sounds wild.",
                                            "text_zh": "这游戏让我玩上头了，断章取义听起来太离谱了",
                                        },
                                    ],
                                }
                            ]
                        },
                        ensure_ascii=False,
                    )
                # 片段 #2 故意返回损坏 JSON 触发 fallback_translation 兜底提醒
                return "BROKEN NON-JSON OUTPUT"

            res = select_highlights_with_llm(
                transcript_json_path=transcript_json,
                output_dir=out_dir,
                top_k=2,
                bilingual_subtitles=True,
                concurrency=1,
                llm_caller=_screen_caller,
                polish_llm_caller=_polish_caller_first_run,
            )

            self.assertEqual(res["highlight_count"], 2)
            self.assertEqual(res["reviewed_count"], 0)
            self.assertEqual(res["draft_count"], 2)
            hl1, hl2 = res["highlights"]
            self.assertEqual(hl1["review_status"], "draft")
            self.assertEqual(hl2["review_status"], "draft")
            self.assertIn("high_idiom_density", hl1["review_flags"])
            self.assertIn("fallback_translation", hl2["review_flags"])
            self.assertIn("long_clip", hl2["review_flags"])

            md_text_initial = Path(res["review_md_path"]).read_text(encoding="utf-8")
            self.assertIn("[AI初稿待校]", md_text_initial)
            self.assertIn("fallback_translation", md_text_initial)

            # 模拟人工审校：将两条均标记为 "reviewed" 落盘
            hl_path = Path(res["highlights_json_path"])
            persisted = json.loads(hl_path.read_text(encoding="utf-8"))
            persisted["highlights"][0]["review_status"] = "reviewed"
            persisted["highlights"][1]["review_status"] = "reviewed"
            hl_path.write_text(json.dumps(persisted, ensure_ascii=False, indent=2), encoding="utf-8")

            # 仅重翻第 2 条，验证：第 1 条依然保持 "reviewed"；第 2 条回置为 "draft" 且修好后清除 fallback_translation
            def _polish_retranslate_rank2(sys_p: str, usr_p: str) -> str:
                return json.dumps(
                    {
                        "clips": [
                            {
                                "rank": 2,
                                "subtitles": [
                                    {
                                        "start_sec": 200.0,
                                        "end_sec": 205.0,
                                        "text_en": "Wait hold on, this clip goes on for a long time.",
                                        "text_zh": "等等，这段切片铺垫确实有点长啊",
                                    }
                                ],
                            }
                        ]
                    },
                    ensure_ascii=False,
                )

            re_res = enrich_highlights_with_bilingual_subtitles(
                input_json_path=hl_path,
                ranks=[2],
                concurrency=1,
                llm_caller=_polish_retranslate_rank2,
            )
            self.assertEqual(re_res["highlights"][0]["review_status"], "reviewed")
            self.assertEqual(re_res["highlights"][1]["review_status"], "draft")
            self.assertNotIn("fallback_translation", re_res["highlights"][1]["review_flags"])
            self.assertIn("long_clip", re_res["highlights"][1]["review_flags"])
            self.assertEqual(re_res["reviewed_count"], 1)
            self.assertEqual(re_res["draft_count"], 1)

            md_after = Path(re_res["review_md_path"]).read_text(encoding="utf-8")
            self.assertIn("[已人工校对]", md_after)
            self.assertIn("[AI初稿待校]", md_after)

            # 验证 scan_input_videos 透出审校统计，且 pipeline --auto-export 含去 Slop 警示说明
            scanned = scan_input_videos(in_dir, out_dir)
            self.assertEqual(len(scanned), 1)
            self.assertEqual(scanned[0]["reviewed_count"], 1)
            self.assertEqual(scanned[0]["draft_count"], 1)

            parser = build_pipeline_arg_parser()
            all_opts = {opt for a in parser._actions for opt in a.option_strings}
            # 断舍离-02：彻底砍掉 CLI --auto-export 无人值守出片参数，替换为 --export-nle-xml
            self.assertNotIn("--auto-export", all_opts)
            self.assertNotIn("--export-mode", all_opts)
            self.assertIn("--export-nle-xml", all_opts)
            nle_action = next(a for a in parser._actions if "--export-nle-xml" in a.option_strings)
            self.assertIn("NLE", nle_action.help or "")

            # 验证 export_review_markdown 直接渲染与 compute_review_flags 独立调用
            flags_direct = compute_review_flags(
                {"start_sec": 0.0, "end_sec": 100.0, "duration_sec": 100.0, "translation_status": "fallback"}
            )
            self.assertEqual(flags_direct, ["fallback_translation", "long_clip"])
            custom_md = tmp / "custom_review.md"
            export_review_markdown(re_res, custom_md, transcript_segments=transcript_payload["segments"])
            self.assertTrue(custom_md.is_file())

    def test_chat_radar_redteam_xml_oom_utf16_cdata_redos_depth_and_sqlite_fts5(self) -> None:
        """测试 6：红队弹幕引擎专项攻防——嵌套 XML 防 OOM、UTF-16 BOM、CDATA 误报、俄语 ReDoS、深度栈限制与 SQLite FTS5。"""
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp = Path(tmp_dir)

            # 1. 嵌套 <d><d>...</d></d> 防 O(N^2) OOM：只读直接 elem.text，不调用 itertext()
            nested_depth = 300
            parts = ['<?xml version="1.0" encoding="UTF-8"?><i>']
            for i in range(nested_depth):
                parts.append(f'<d p="{i + 1}.0,1,25,16777215,0,0,u{i},{i}">msg_{i}_payload')
            parts.append("</d>" * nested_depth)
            parts.append("</i>")
            nested_xml = tmp / "nested_bomb.xml"
            nested_xml.write_text("".join(parts), encoding="utf-8")

            t0 = time.perf_counter()
            parsed_nested = parse_chat_file(nested_xml)
            self.assertLess(time.perf_counter() - t0, 2.0)
            self.assertEqual(parsed_nested["message_count"], nested_depth)
            # 最外层 <d> 的文本只应是它自己的直接文本 "msg_0_payload"，绝不包含内层 299 条文本拼接！
            self.assertEqual(parsed_nested["messages"][0]["text"], "msg_0_payload")

            # 2. UTF-16 LE BOM 编码 XML 解码 + CDATA 正文含 <!DOCTYPE 不误报
            utf16_xml = tmp / "utf16_danmaku.xml"
            xml_text = (
                '<?xml version="1.0" encoding="UTF-16"?>\n'
                '<i>\n'
                '  <d p="12.5,1,25,16777215,0,0,u1,1"><![CDATA[聊天里提到 <!DOCTYPE html> 不是攻击]]></d>\n'
                '  <d p="25.0,1,25,16777215,0,0,u2,2">UTF-16 中文弹幕 666</d>\n'
                '</i>\n'
            )
            utf16_xml.write_bytes(xml_text.encode("utf-16"))
            parsed_u16 = parse_chat_file(utf16_xml)
            self.assertEqual(parsed_u16["format"], "bilibili_xml")
            self.assertEqual(parsed_u16["message_count"], 2)
            self.assertIn("<!DOCTYPE html>", parsed_u16["messages"][0]["text"])
            self.assertIn("UTF-16 中文弹幕", parsed_u16["messages"][1]["text"])

            # 3. 俄语梗词 ReDoS 攻击串线性时间完成（绝无多项式回溯）
            redos_payload = "ах" * 8000 + "!"
            t_re = time.perf_counter()
            terms = extract_hype_terms(redos_payload)
            self.assertLess(time.perf_counter() - t_re, 0.25, "俄语梗词正则出现慢回溯！")
            self.assertIn("ахах", terms)

            # 4. 超深嵌套 YouTube JSONL：深度 > 16 被迭代栈安全截断，超高嵌套导致 RecursionError 时转 ValueError
            deep_obj: dict[str, object] = {"liveChatTextMessageRenderer": {"message": {"runs": [{"text": "too deep"}]}}}
            for _ in range(25):
                deep_obj = {"wrapper": deep_obj}
            deep_yt = tmp / "deep_yt.jsonl"
            deep_yt.write_text(json.dumps({"replayChatItemAction": {"videoOffsetTimeMsec": "1000", "actions": [deep_obj]}}) + "\n", encoding="utf-8")
            # 深度超 16 被安全跳过而不崩溃（无有效消息报 ValueError 或 0 条）
            res_deep = parse_chat_file(deep_yt)
            self.assertEqual(res_deep["message_count"], 0)

            extreme_json = tmp / "extreme_depth.json"
            extreme_json.write_text("[" * 2000 + "1" + "]" * 2000, encoding="utf-8")
            with self.assertRaises(ValueError):
                parse_chat_file(extreme_json)

            # 5. bucket_samples 严格上限 <= 12 条 + search_chat_keywords 参数上限防御
            dense_msgs = [
                {"time_sec": 10.0 + i * 0.05, "text": f"OMEGALUL 666 msg {i}", "author": f"u{i}"}
                for i in range(40)
            ] + [{"time_sec": float(t * 5 + 1), "text": "calm chat", "author": "c"} for t in range(3, 30)]
            radar = analyze_chat_spikes(dense_msgs, duration_sec=160.0, bucket_sec=5.0)
            for pk in radar["chat_peaks"]:
                self.assertLessEqual(len(pk["sample_messages"]), 12)

            with self.assertRaises(ValueError):
                search_chat_keywords(dense_msgs, "a" * 201)
            with self.assertRaises(ValueError):
                search_chat_keywords(dense_msgs, "OMEGALUL", bucket_sec=float("nan"))
            many_terms = ", ".join(f"w{i}" for i in range(15))
            sr_10 = search_chat_keywords(dense_msgs, many_terms)
            self.assertLessEqual(len(sr_10["terms"]), 10)

            # 6. SQLite FTS5 拆离存储 + 低内存多语种搜索闭环
            db_path = tmp / "stream_chat_messages.db"
            db_msgs = [
                {"time_sec": 12.0, "text": "OMEGALUL INSANE CLUTCH!!", "author": "en1"},
                {"time_sec": 14.5, "text": "OMEGALUL no way", "author": "en2"},
                {"time_sec": 42.0, "text": "草 笑死我了 666 名场面", "author": "zh1"},
                {"time_sec": 45.0, "text": "わろたwww 神プレイ", "author": "jp1"},
            ]
            save_chat_messages_db(db_path, db_msgs)
            self.assertTrue(db_path.is_file())

            res_en = search_chat_messages_db(db_path, "OMEGALUL", bucket_sec=10.0)
            self.assertEqual(res_en["match_count"], 2)
            self.assertEqual([m["author"] for m in res_en["matches"]], ["en1", "en2"])

            res_zh = search_chat_messages_db(db_path, "名场面, 神プレイ", bucket_sec=10.0)
            self.assertEqual(res_zh["match_count"], 2)
            self.assertEqual(len(res_zh["top_buckets"]), 1)

            res_trunc = search_chat_messages_db(db_path, "OMEGALUL", bucket_sec=10.0, max_matches=1)
            self.assertEqual(res_trunc["match_count"], 2)
            self.assertEqual(len(res_trunc["matches"]), 1)
            self.assertTrue(res_trunc["matches_truncated"])

    def test_dead_air_low_gain_ndim_nan_and_step3_concurrency_and_pipeline_cache(self) -> None:
        """测试 7：红队 Dead Air 低增益/NaN/2D 修复、Step 3 并发防覆写审校与失败保旧、Pipeline nle_only 与缓存修复。"""
        # 1. Dead Air：2D 数组自动 ravel；低增益轻声主播（max=0.003）走相对底噪比例绝不误杀全片
        rms_low = np.full(100, 0.0028, dtype=np.float64)
        rms_low[40:60] = 0.0001  # 20 秒真实死寂
        segs_low = detect_dead_air_segments(rms_low.reshape(10, 10), hop_sec=1.0, total_duration_sec=100.0, min_dead_air_sec=15.0)
        self.assertEqual(len(segs_low), 1)
        self.assertEqual(segs_low[0]["start_sec"], 40.0)
        self.assertEqual(segs_low[0]["end_sec"], 60.0)

        # 非有限或非正参数报错
        with self.assertRaises(ValueError):
            detect_dead_air_segments(rms_low, hop_sec=float("nan"), total_duration_sec=100.0)
        with self.assertRaises(ValueError):
            detect_dead_air_segments(rms_low, hop_sec=1.0, total_duration_sec=float("inf"))
        with self.assertRaises(ValueError):
            detect_dead_air_segments(rms_low, hop_sec=1.0, total_duration_sec=100.0, min_dead_air_sec=-5.0)

        # 2. Step 3 并发防覆写审校 + ranks=[] 拦截 + 重翻失败保旧字幕不覆毁
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp = Path(tmp_dir)
            in_dir = tmp / "in"
            out_dir = tmp / "out"
            in_dir.mkdir()
            out_dir.mkdir()
            vid = in_dir / "conc_vod.mp4"
            vid.write_bytes(b"fake")
            tr_path = out_dir / "conc_vod_transcript.json"
            tr_path.write_text(
                json.dumps(
                    {
                        "video_path": str(vid),
                        "total_duration_sec": 120.0,
                        "language": "en",
                        "segments": [
                            {"start_sec": 5.0, "end_sec": 10.0, "text": "Clip one original line"},
                            {"start_sec": 50.0, "end_sec": 55.0, "text": "Clip two original line"},
                        ],
                    }
                ),
                encoding="utf-8",
            )
            hl_path = out_dir / "conc_vod_highlights.json"
            hl_path.write_text(
                json.dumps(
                    {
                        "video_path": str(vid),
                        "transcript_json_path": str(tr_path),
                        "highlights_json_path": str(hl_path),
                        "review_md_path": str(out_dir / "conc_vod_review.md"),
                        "source_language": "en",
                        "target_language": "zh",
                        "total_duration_sec": 120.0,
                        "highlights": [
                            {
                                "rank": 1,
                                "selected": True,
                                "review_status": "draft",
                                "start_sec": 4.0,
                                "end_sec": 12.0,
                                "score": 9.0,
                                "title_zh": "旧标题1",
                                "translation_status": "ok",
                                "subtitles": [
                                    {
                                        "start_sec": 5.0,
                                        "end_sec": 10.0,
                                        "text_src": "Clip one original line",
                                        "text_tgt": "人工精修好的第一条字幕",
                                        "text_en": "Clip one original line",
                                        "text_zh": "人工精修好的第一条字幕",
                                    }
                                ],
                            },
                            {
                                "rank": 2,
                                "selected": True,
                                "review_status": "draft",
                                "start_sec": 48.0,
                                "end_sec": 58.0,
                                "score": 8.5,
                                "title_zh": "旧标题2",
                                "translation_status": "ok",
                                "subtitles": [
                                    {
                                        "start_sec": 50.0,
                                        "end_sec": 55.0,
                                        "text_src": "Clip two original line",
                                        "text_tgt": "原本完好的第二条中文译文",
                                        "text_en": "Clip two original line",
                                        "text_zh": "原本完好的第二条中文译文",
                                    }
                                ],
                            },
                        ],
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

            # 2A. ranks=[] 显式拒绝，绝不退化为全量重置 reviewed
            with self.assertRaises(ValueError):
                enrich_highlights_with_bilingual_subtitles(hl_path, ranks=[])

            # 2B. 并发防覆写：在 LLM 翻译 rank=2 期间，模拟用户在磁盘把 rank=1 审校为 reviewed 并改了标题；
            #     同时让 rank=2 的 LLM 返回损坏 JSON，验证既不抹掉 rank=1 的审校成果，又保住 rank=2 原有 ok 字幕！
            def _concurrent_failing_caller(sys_p: str, usr_p: str) -> str:
                disk_now = json.loads(hl_path.read_text(encoding="utf-8"))
                disk_now["highlights"][0]["review_status"] = "reviewed"
                disk_now["highlights"][0]["title_zh"] = "用户并发审好的标题1"
                hl_path.write_text(json.dumps(disk_now, ensure_ascii=False, indent=2), encoding="utf-8")
                return "BROKEN JSON FOR FALLBACK TEST"

            merged = enrich_highlights_with_bilingual_subtitles(
                hl_path,
                ranks=[2],
                concurrency=1,
                llm_caller=_concurrent_failing_caller,
            )
            disk_final = json.loads(hl_path.read_text(encoding="utf-8"))
            # rank=1 并发审校成果毫发无损！
            self.assertEqual(disk_final["highlights"][0]["review_status"], "reviewed")
            self.assertEqual(disk_final["highlights"][0]["title_zh"], "用户并发审好的标题1")
            # rank=2 翻译失败时保住原有的 ok 字幕不覆毁！
            self.assertEqual(
                merged["highlights"][1]["subtitles"][0]["text_tgt"],
                "原本完好的第二条中文译文",
            )

            # 3. Pipeline 缓存校验 + scan_input_videos 区分 nle_only 与真实 MP4 出片
            self.assertFalse(_peaks_cache_matches({"rms_threshold": 0.1, "top_percent": 10.0, "audio_stream": 0, "vocal_bandpass": True}, 0, True, 10.0))
            self.assertTrue(_peaks_cache_matches({"dead_air_segments": [], "top_percent": 10.0, "audio_stream": 0, "vocal_bandpass": True}, 0, True, 10.0))
            self.assertFalse(
                _highlights_cache_matches(
                    {"model": "manual", "source_language": "en", "target_language": "zh"},
                    "deepseek-chat",
                    "deepseek-chat",
                    10,
                    15,
                    60,
                    True,
                )
            )
            # 中文源片默认目标语推断应为 en（非写死 zh）
            self.assertTrue(
                _highlights_cache_matches(
                    {
                        "model": "deepseek-chat",
                        "source_language": "zh",
                        "target_language": "en",
                        "bilingual_subtitles": True,
                        "selection_plan": {"target_count": 5, "min_clip_sec": 15, "max_clip_sec": 60},
                    },
                    "deepseek-chat",
                    "deepseek-chat",
                    5,
                    15,
                    60,
                    True,
                    target_language=None,
                    source_language="zh",
                )
            )
            self.assertFalse(
                _highlights_cache_matches(
                    {
                        "model": "deepseek-chat",
                        "source_language": "zh",
                        "target_language": "ja",
                        "bilingual_subtitles": True,
                        "selection_plan": {"target_count": 5, "min_clip_sec": 15, "max_clip_sec": 60},
                    },
                    "deepseek-chat",
                    "deepseek-chat",
                    5,
                    15,
                    60,
                    True,
                    target_language=None,
                    source_language="zh",
                )
            )

            # 模拟 nle_only 导出产物（无 MP4，仅有 FCPXML + {stem}_clips_manifest.json）
            clips_dir = out_dir / "conc_vod_clips"
            clips_dir.mkdir()
            fcp_dummy = clips_dir / "conc_vod.fcpxml"
            fcp_dummy.write_text("<fcpxml/>", encoding="utf-8")
            (clips_dir / "conc_vod_clips_manifest.json").write_text(
                json.dumps(
                    {
                        "mode": "nle_only",
                        "exported_count": 2,
                        "exported_clip_count": 0,
                        "nle_item_count": 2,
                        "fcpxml_path": str(fcp_dummy),
                        "premiere_xml_path": "",
                    }
                ),
                encoding="utf-8",
            )
            scanned = scan_input_videos(in_dir, out_dir)
            self.assertEqual(len(scanned), 1)
            self.assertFalse(scanned[0]["has_clips"])
            self.assertTrue(scanned[0]["has_nle_export"])
            self.assertEqual(scanned[0]["stage"], "analyzed")


if __name__ == "__main__":
    unittest.main()
