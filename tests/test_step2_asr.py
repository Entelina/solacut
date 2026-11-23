"""Step 2（src/step2_asr.py）纯本地单元测试。

覆盖候选窗口挑选/扩展/合并、WAV 精准分帧切片、高音量与字幕重叠匹配以及 TXT 导出，
全部测试不依赖网络或外部模型权重下载。
"""

from __future__ import annotations

import json
import math
import struct
import sys
import tempfile
import unittest
import wave
from pathlib import Path
from unittest.mock import patch

import numpy as np

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.step2_asr import (  # noqa: E402
    align_segments_with_peaks,
    export_readable_transcript,
    read_wav_segment_float32,
    select_candidate_windows,
    split_long_segment_by_words,
    transcribe_with_audio_peaks,
)


def _create_sine_wav(wav_path: Path, segments: list[tuple[float, float]], sample_rate: int = 16000) -> None:
    """按 (时长秒数, 振幅) 生成 16kHz 单声道 16-bit PCM WAV 测试文件。"""
    with wave.open(str(wav_path), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)

        buf = bytearray()
        idx = 0
        for duration_sec, amp in segments:
            total = int(round(duration_sec * sample_rate))
            for _ in range(total):
                val = int(round(32767.0 * amp * math.sin(2.0 * math.pi * 440.0 * idx / sample_rate)))
                val = max(-32768, min(32767, val))
                buf.extend(struct.pack("<h", val))
                idx += 1
        wf.writeframes(bytes(buf))


class TestStep2Asr(unittest.TestCase):
    """Step 2 核心纯算法与文件工具单元测试。"""

    def test_select_candidate_windows_top_k_pad_and_merge(self) -> None:
        """测试 1：验证 Top K 排序过滤、前后扩展边界裁剪与相近窗口合并。"""
        peaks = [
            {"start_sec": 5.0, "end_sec": 8.0, "peak_rms": 0.15},     # 第 3 强（会被 Top 3 选中，左边界裁剪到 0）
            {"start_sec": 22.0, "end_sec": 25.0, "peak_rms": 0.30},   # 第 1 强（扩展后 7~40 与 0~23 重叠应合并！）
            {"start_sec": 60.0, "end_sec": 62.0, "peak_rms": 0.05},   # 最弱（Top 3 不选）
            {"start_sec": 95.0, "end_sec": 98.0, "peak_rms": 0.25},   # 第 2 强（扩展后 80~100 右边界裁剪到 100）
        ]

        windows = select_candidate_windows(
            peak_segments=peaks,
            total_duration_sec=100.0,
            top_k_peaks=3,
            context_pad_sec=15.0,
            merge_gap_sec=3.0,
        )

        # 前两个窗口（0~23 和 7~40）重叠合并为 0~40；第 4 个变为 80~100
        self.assertEqual(len(windows), 2)

        w1, w2 = windows[0], windows[1]
        self.assertEqual(w1["window_id"], 1)
        self.assertEqual(w1["start_sec"], 0.0)
        self.assertEqual(w1["end_sec"], 40.0)
        self.assertEqual(w1["max_peak_rms"], 0.3)
        self.assertEqual(w1["peak_count"], 2)

        self.assertEqual(w2["window_id"], 2)
        self.assertEqual(w2["start_sec"], 80.0)
        self.assertEqual(w2["end_sec"], 100.0)
        self.assertEqual(w2["max_peak_rms"], 0.25)
        self.assertEqual(w2["peak_count"], 1)

    def test_read_wav_segment_float32_exact_slice(self) -> None:
        """测试 2：验证直接按秒数从 WAV 精准提取对应切片且振幅归一化正确。"""
        with tempfile.TemporaryDirectory() as tmp_dir:
            wav_path = Path(tmp_dir) / "sample.wav"
            # 0~3s 几乎静音(0.01)，3~5s 强正弦波(0.80)，5~8s 几乎静音(0.01)
            _create_sine_wav(wav_path, [(3.0, 0.01), (2.0, 0.80), (3.0, 0.01)], sample_rate=16000)

            mid_slice = read_wav_segment_float32(wav_path, 3.0, 5.0)
            self.assertEqual(mid_slice.dtype, np.float32)
            self.assertEqual(mid_slice.size, 2 * 16000)
            # 振幅 0.80 的正弦波 RMS 约为 0.8 / sqrt(2) ≈ 0.5657
            mid_rms = float(np.sqrt(np.mean(mid_slice * mid_slice)))
            self.assertAlmostEqual(mid_rms, 0.80 / math.sqrt(2.0), places=2)

            quiet_slice = read_wav_segment_float32(wav_path, 5.5, 7.0)
            self.assertEqual(quiet_slice.size, int(1.5 * 16000))
            self.assertLess(float(np.max(np.abs(quiet_slice))), 0.02)

    def test_align_segments_with_peaks(self) -> None:
        """测试 3：验证字幕与高音量区间重叠匹配及多区间取最高 RMS。"""
        transcript = [
            {"start_sec": 10.0, "end_sec": 14.0, "text": "Normal chatting"},
            {"start_sec": 20.0, "end_sec": 26.0, "text": "Oh my god!!"},
            {"start_sec": 30.0, "end_sec": 35.0, "text": "No overlap"},
        ]
        peaks = [
            {"start_sec": 19.0, "end_sec": 22.0, "peak_rms": 0.14},
            {"start_sec": 24.0, "end_sec": 27.0, "peak_rms": 0.2717},
        ]

        aligned = align_segments_with_peaks(transcript, peaks)
        self.assertEqual(len(aligned), 3)

        self.assertFalse(aligned[0]["is_high_volume"])
        self.assertEqual(aligned[0]["peak_rms"], 0.0)

        self.assertTrue(aligned[1]["is_high_volume"])
        self.assertEqual(aligned[1]["peak_rms"], 0.2717)

        self.assertFalse(aligned[2]["is_high_volume"])
        self.assertEqual(aligned[2]["peak_rms"], 0.0)

    def test_export_readable_transcript(self) -> None:
        """测试 4：验证导出 TXT 字幕稿包含候选窗口头与 [HIGH_VOL] 高亮标记。"""
        with tempfile.TemporaryDirectory() as tmp_dir:
            txt_path = Path(tmp_dir) / "demo_transcript.txt"
            fake_result = {
                "video_path": "input/demo.mp4",
                "mode": "peaks",
                "language": "en",
                "segment_count": 2,
                "high_volume_count": 1,
                "candidate_windows": [
                    {
                        "window_id": 1,
                        "start_str": "01:40:06",
                        "end_str": "01:40:44",
                        "max_peak_rms": 0.2717,
                    }
                ],
                "segments": [
                    {
                        "window_id": 1,
                        "start_str": "01:40:08",
                        "end_str": "01:40:12",
                        "is_high_volume": False,
                        "peak_rms": 0.0,
                        "text": "Let's try this boss.",
                    },
                    {
                        "window_id": 1,
                        "start_str": "01:40:21",
                        "end_str": "01:40:25",
                        "is_high_volume": True,
                        "peak_rms": 0.2717,
                        "text": "WHAT WAS THAT?!",
                    },
                ],
            }

            out = export_readable_transcript(fake_result, txt_path)
            self.assertTrue(out.is_file())
            content = out.read_text(encoding="utf-8")
            self.assertIn("=== 候选窗口 #1 [01:40:06 - 01:40:44] (最高RMS: 0.2717) ===", content)
            self.assertIn("[01:40:08 - 01:40:12] Let's try this boss.", content)
            self.assertIn("[01:40:21 - 01:40:25] [HIGH_VOL rms=0.2717] WHAT WAS THAT?!", content)

    def test_split_long_segment_by_words(self) -> None:
        """测试 5：验证超长 ASR 字幕可依据词级时间戳或标点平缓拆成 2~4 秒紧凑短句。"""
        # 1. 有词级时间戳：10 秒长句含 2 个句号，应拆成短句且严格落在 [10.0, 20.0]
        words = [
            {"word": " Oh", "start": 10.0, "end": 10.6},
            {"word": " my", "start": 10.6, "end": 11.2},
            {"word": " god,", "start": 11.2, "end": 12.0},
            {"word": " Where", "start": 12.1, "end": 12.7},
            {"word": " Winds", "start": 12.7, "end": 13.3},
            {"word": " Meet", "start": 13.3, "end": 13.8},
            {"word": " is", "start": 13.8, "end": 14.2},
            {"word": " crazy!", "start": 14.2, "end": 15.0},
            {"word": " I", "start": 15.2, "end": 15.6},
            {"word": " played", "start": 15.6, "end": 16.4},
            {"word": " until", "start": 16.4, "end": 17.5},
            {"word": " nine", "start": 17.5, "end": 18.6},
            {"word": " in", "start": 18.6, "end": 19.1},
            {"word": " the", "start": 19.1, "end": 19.4},
            {"word": " morning.", "start": 19.4, "end": 20.0},
        ]
        parts_words = split_long_segment_by_words(
            text="Oh my god, Where Winds Meet is crazy! I played until nine in the morning.",
            start_sec=10.0,
            end_sec=20.0,
            words=words,
            max_duration_sec=4.5,
            max_chars=40,
        )
        self.assertGreaterEqual(len(parts_words), 2)
        for p in parts_words:
            self.assertGreaterEqual(p["start_sec"], 10.0)
            self.assertLessEqual(p["end_sec"], 20.0)
            self.assertLessEqual(p["end_sec"] - p["start_sec"], 5.5)

        # 2. 无词级时间戳：14 秒长句也能按标点/词边界拆分且平滑分配时间
        parts_no_words = split_long_segment_by_words(
            text="First clause is very long indeed, second clause keeps going on and on! Third final part here.",
            start_sec=0.0,
            end_sec=14.0,
            words=None,
            max_duration_sec=4.5,
            max_chars=38,
        )
        self.assertGreaterEqual(len(parts_no_words), 3)
        self.assertAlmostEqual(parts_no_words[0]["start_sec"], 0.0, places=2)
        self.assertAlmostEqual(parts_no_words[-1]["end_sec"], 14.0, places=2)

    def test_no_dangling_preposition_or_tiny_fragment_splits(self) -> None:
        """测试 6：验证词级与无词级切分均不在 of/the/and 等悬垂虚词后硬断句，且消灭超短碎词。"""
        # 1. 词级时间戳测试：模拟真实语流 "They even stream that game most of the time, and all I play is Where Winds Meet."
        tokens = [
            "They", "even", "stream", "that", "game", "most", "of", "the", "time,",
            "and", "all", "I", "play", "is", "Where", "Winds", "Meet.",
        ]
        words = []
        t = 0.0
        for tok in tokens:
            dur = 0.45
            words.append({"word": f" {tok}", "start": round(t, 2), "end": round(t + dur, 2)})
            t += dur

        parts_words = split_long_segment_by_words(
            text=" ".join(tokens),
            start_sec=0.0,
            end_sec=round(t, 2),
            words=words,
            max_duration_sec=3.5,
            max_chars=36,
        )
        self.assertGreaterEqual(len(parts_words), 2)
        for part in parts_words:
            last_word = part["text"].split()[-1].strip(",.!?").lower()
            self.assertNotIn(
                last_word,
                {"of", "the", "a", "an", "and", "to", "in", "is"},
                msg=f"不应在悬垂虚词处切碎：{part['text']}",
            )
            self.assertGreaterEqual(part["end_sec"] - part["start_sec"], 0.9)

        # 2. 无词级时间戳测试：不应出现 "part of" / "the time" 之类在虚词后切断的碎片
        parts_text = split_long_segment_by_words(
            text="Most people play games part of the time and I need parental controls for it.",
            start_sec=10.0,
            end_sec=20.0,
            words=None,
            max_duration_sec=4.0,
            max_chars=34,
        )
        self.assertGreaterEqual(len(parts_text), 2)
        for part in parts_text:
            last_word = part["text"].split()[-1].strip(",.!?").lower()
            self.assertNotIn(
                last_word,
                {"of", "the", "a", "and", "for"},
                msg=f"无词级断句不应以虚词结尾：{part['text']}",
            )
            self.assertGreaterEqual(part["end_sec"] - part["start_sec"], 0.9)

    def test_transcribe_with_audio_peaks_default_deepseek_provider(self) -> None:
        """测试 7：验证 Step 2 默认走 DeepSeek 云端 ASR（通过 Mock HTTP 离线端到端跑通）。"""
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp = Path(tmp_dir)
            wav_path = tmp / "vod_16k.wav"
            peaks_json = tmp / "vod_audio_peaks.json"
            _create_sine_wav(wav_path, [(2.0, 0.02), (3.0, 0.40), (2.0, 0.02)], sample_rate=16000)
            peaks_json.write_text(
                json.dumps(
                    {
                        "video_path": "E:/vods/vod.mp4",
                        "wav_path": str(wav_path),
                        "total_duration_sec": 7.0,
                        "peak_segments": [{"start_sec": 2.0, "end_sec": 5.0, "peak_rms": 0.28}],
                    }
                ),
                encoding="utf-8",
            )

            class _Resp:
                status_code = 200
                text = '{"text": "We won this fight!", "duration_ms": 2500}'

                def json(self):
                    return {"request_id": "req1", "text": "We won this fight!", "duration_ms": 2500}

            with patch("src.asr_providers.detect_speech_chunks", return_value=[(2.0, 4.5)]):
                res = transcribe_with_audio_peaks(
                    peaks_json_path=peaks_json,
                    output_dir=tmp,
                    asr_provider="deepseek",
                    asr_api_key="sk-mock",
                    top_k_peaks=5,
                    asr_caller=lambda *a, **kw: _Resp(),
                )

            self.assertEqual(res["asr_provider"], "deepseek")
            self.assertEqual(res["top_k_peaks"], 5)
            self.assertTrue(res["normalize_audio"])
            self.assertEqual(res["segment_count"], 1)
            self.assertEqual(res["segments"][0]["text"], "We won this fight!")
            self.assertTrue(res["segments"][0]["is_high_volume"])
            self.assertAlmostEqual(res["billed_audio_sec"], 2.5, places=1)
            self.assertTrue(Path(res["transcript_json_path"]).is_file())
            self.assertTrue(Path(res["transcript_txt_path"]).is_file())


if __name__ == "__main__":
    unittest.main()
