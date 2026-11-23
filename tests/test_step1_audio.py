"""Step 1（src/step1_audio.py）端到端自动化测试。

测试内容全部在临时目录中完成，不会污染项目的 input/ 与 output/ 目录。
"""

from __future__ import annotations

import json
import math
import struct
import subprocess
import sys
import tempfile
import unittest
import wave
from pathlib import Path

# 确保无论从哪个目录运行测试，都能正确导入 src 包
PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.step1_audio import (  # noqa: E402
    analyze_video_audio,
    check_ffmpeg_available,
    compute_per_second_rms,
    diagnose_stream_energy,
    probe_audio_streams,
)


def _write_test_wav(
    wav_path: Path,
    segments: list[tuple[float, float]],
    sample_rate: int = 16000,
    freq_hz: float = 440.0,
) -> None:
    """按分段 (时长秒数, 振幅 0~1) 生成 16-bit 单声道 WAV 文件。"""
    with wave.open(str(wav_path), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)

        frames = bytearray()
        sample_idx = 0
        for duration_sec, amplitude in segments:
            num_samples = int(round(duration_sec * sample_rate))
            for _ in range(num_samples):
                if amplitude <= 0.0:
                    val = 0
                else:
                    t = sample_idx / float(sample_rate)
                    val = int(round(32767.0 * amplitude * math.sin(2.0 * math.pi * freq_hz * t)))
                    val = max(-32768, min(32767, val))
                frames.extend(struct.pack("<h", val))
                sample_idx += 1

        wf.writeframes(bytes(frames))


def _mux_black_mp4(wav_path: Path, mp4_path: Path, duration_sec: float) -> None:
    """使用 ffmpeg 将测试 WAV 与纯黑画面合成为真实 MP4 视频。"""
    ffmpeg_bin = check_ffmpeg_available()
    cmd = [
        ffmpeg_bin,
        "-y",
        "-f",
        "lavfi",
        "-i",
        f"color=c=black:s=320x240:r=10:d={duration_sec}",
        "-i",
        str(wav_path),
        "-c:v",
        "libx264",
        "-pix_fmt",
        "yuv420p",
        "-c:a",
        "aac",
        "-shortest",
        str(mp4_path),
    ]
    proc = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if proc.returncode != 0:
        raise RuntimeError(f"测试视频合成失败：\n{proc.stderr}")


class TestStep1Audio(unittest.TestCase):
    """Step 1 音频提取与高音量检测测试套件。"""

    def test_detect_two_high_volume_segments_end_to_end(self) -> None:
        """测试 1：合成包含 2 段高音量的真实 MP4，验证准确识别起止时间与输出文件。"""
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp = Path(tmp_dir)
            raw_wav = tmp / "raw.wav"
            demo_mp4 = tmp / "demo_match.mp4"
            out_dir = tmp / "out"

            # 构造 16 秒音轨：
            # 0~4s 低音 0.03 | 4~7s 高音 0.70 | 7~11s 低音 0.03 | 11~13s 高音 0.85 | 13~16s 低音 0.03
            _write_test_wav(
                raw_wav,
                segments=[
                    (4.0, 0.03),
                    (3.0, 0.70),
                    (4.0, 0.03),
                    (2.0, 0.85),
                    (3.0, 0.03),
                ],
            )
            _mux_black_mp4(raw_wav, demo_mp4, duration_sec=16.0)

            # 5s/16s = 31.25% 为高音量，设置 top_percent=28.0 确保阈值稳定落在高低音之间
            result = analyze_video_audio(
                video_path=demo_mp4,
                output_dir=out_dir,
                window_sec=1.0,
                top_percent=28.0,
                min_rms=0.01,
                merge_gap_sec=2.0,
                pad_sec=1.0,
            )

            wav_out = Path(result["wav_path"])
            json_out = Path(result["json_path"])
            self.assertTrue(wav_out.is_file(), "未生成提取后的 16k WAV 文件")
            self.assertTrue(json_out.is_file(), "未生成 JSON 报告文件")

            # 校验 WAV 参数确认为 16kHz 单声道 16-bit PCM
            with wave.open(str(wav_out), "rb") as wf:
                self.assertEqual(wf.getframerate(), 16000)
                self.assertEqual(wf.getnchannels(), 1)
                self.assertEqual(wf.getsampwidth(), 2)

            # 校验磁盘 JSON 可正常反序列化且内容一致
            loaded = json.loads(json_out.read_text(encoding="utf-8"))
            self.assertEqual(loaded["segment_count"], 2)
            segments = loaded["peak_segments"]
            self.assertEqual(len(segments), 2)

            # 第 1 段高音在 4~7 秒，允许 pad_sec=1.0 前后缓冲，必须完整覆盖 4.0~7.0
            seg1 = segments[0]
            self.assertLessEqual(seg1["start_sec"], 4.0)
            self.assertGreaterEqual(seg1["end_sec"], 7.0)
            self.assertGreaterEqual(seg1["start_sec"], 2.5)
            self.assertLessEqual(seg1["end_sec"], 8.5)

            # 第 2 段高音在 11~13 秒，允许 pad_sec=1.0 前后缓冲，必须完整覆盖 11.0~13.0
            seg2 = segments[1]
            self.assertLessEqual(seg2["start_sec"], 11.0)
            self.assertGreaterEqual(seg2["end_sec"], 13.0)
            self.assertGreaterEqual(seg2["start_sec"], 9.5)
            self.assertLessEqual(seg2["end_sec"], 14.5)

    def test_silent_video_returns_no_peaks(self) -> None:
        """测试 2：纯静音 MP4 视频应被 min_rms 底线拦截，不误报高光。"""
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp = Path(tmp_dir)
            silent_wav = tmp / "silent.wav"
            silent_mp4 = tmp / "silent.mp4"
            out_dir = tmp / "out"

            _write_test_wav(silent_wav, segments=[(6.0, 0.0)])
            _mux_black_mp4(silent_wav, silent_mp4, duration_sec=6.0)

            result = analyze_video_audio(
                video_path=silent_mp4,
                output_dir=out_dir,
                min_rms=0.01,
            )
            self.assertEqual(result["segment_count"], 0)
            self.assertEqual(result["peak_segments"], [])

    def test_cli_execution(self) -> None:
        """测试 3：直接通过命令行运行 src/step1_audio.py，验证退出码与终端输出。"""
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp = Path(tmp_dir)
            raw_wav = tmp / "cli_raw.wav"
            cli_mp4 = tmp / "cli_demo.mp4"
            out_dir = tmp / "cli_out"

            _write_test_wav(
                raw_wav,
                segments=[
                    (4.0, 0.03),
                    (3.0, 0.70),
                    (4.0, 0.03),
                    (2.0, 0.85),
                    (3.0, 0.03),
                ],
            )
            _mux_black_mp4(raw_wav, cli_mp4, duration_sec=16.0)

            script_path = PROJECT_ROOT / "src" / "step1_audio.py"
            proc = subprocess.run(
                [
                    sys.executable,
                    str(script_path),
                    str(cli_mp4),
                    "--output-dir",
                    str(out_dir),
                    "--top-percent",
                    "28",
                ],
                cwd=str(PROJECT_ROOT),
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
            )
            self.assertEqual(proc.returncode, 0, msg=f"CLI 报错：{proc.stderr}")
            self.assertIn("Step 1 音频高音量分析完成", proc.stdout)
            self.assertIn("命中高能段 : 2 个", proc.stdout)
            self.assertTrue((out_dir / "cli_demo_16k.wav").is_file())
            self.assertTrue((out_dir / "cli_demo_audio_peaks.json").is_file())

    def test_diagnose_stream_energy_grades_and_recommendations(self) -> None:
        """测试 4：验证 S/B/WARN 直播评级、分小时热度分布与按时长智能推荐参数。"""
        # 1. S 级 4.5 小时直播，第 0~1h 强峰最集中
        s_peaks = [
            {"start_sec": 300.0 + i * 200.0, "peak_rms": 0.24} for i in range(12)
        ] + [
            {"start_sec": 4500.0, "peak_rms": 0.21},
            {"start_sec": 8500.0, "peak_rms": 0.19},
        ]
        diag_s = diagnose_stream_energy(
            peak_segments=s_peaks,
            total_duration_sec=16200.0,
            rms_threshold=0.06,
        )
        self.assertEqual(diag_s["grade"], "S")
        self.assertEqual(diag_s["golden_bucket_label"], "0-1h")
        self.assertEqual(len(diag_s["hourly_heatmap"]), 5)
        self.assertEqual(diag_s["recommendation"]["asr_mode"], "peaks")
        self.assertEqual(diag_s["recommendation"]["top_k_peaks"], 25)

        # 2. B 级平淡/限幅 1 小时直播（爆发比 1.8x -> 自动建议 full 全量转写）
        b_peaks = [{"start_sec": 600.0, "peak_rms": 0.09}]
        diag_b = diagnose_stream_energy(
            peak_segments=b_peaks,
            total_duration_sec=3600.0,
            rms_threshold=0.05,
        )
        self.assertEqual(diag_b["grade"], "B")
        self.assertEqual(diag_b["recommendation"]["asr_mode"], "full")

        # 3. WARN 静音/底噪过低视频
        diag_warn = diagnose_stream_energy(
            peak_segments=[],
            total_duration_sec=1200.0,
            rms_threshold=0.005,
        )
        self.assertEqual(diag_warn["grade"], "WARN")
        self.assertEqual(diag_warn["recommendation"]["asr_mode"], "full")

    def test_probe_and_select_mic_audio_stream(self) -> None:
        """测试 5：合成 OBS 双音轨 MP4（轨0桌面立体声 + 轨1人声单声道），验证自动识别与指定选轨提取。"""
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp = Path(tmp_dir)
            desktop_wav = tmp / "desktop.wav"
            mic_wav = tmp / "mic.wav"
            multi_mp4 = tmp / "obs_multitrack.mp4"
            out_dir = tmp / "out"

            # 轨 0（游戏桌面声）：全片平稳极低音量
            _write_test_wav(desktop_wav, segments=[(9.0, 0.04)])
            # 轨 1（纯人声麦轨）：0~3s 静音底噪，3~6s 高音量爆发，6~9s 静音底噪
            _write_test_wav(mic_wav, segments=[(3.0, 0.02), (3.0, 0.80), (3.0, 0.02)])

            ffmpeg_bin = check_ffmpeg_available()
            cmd = [
                ffmpeg_bin,
                "-y",
                "-f",
                "lavfi",
                "-i",
                "color=c=black:s=320x240:r=10:d=9.0",
                "-i",
                str(desktop_wav),
                "-i",
                str(mic_wav),
                "-map",
                "0:v:0",
                "-map",
                "1:a:0",
                "-map",
                "2:a:0",
                "-c:v",
                "libx264",
                "-pix_fmt",
                "yuv420p",
                "-c:a:0",
                "aac",
                "-ac:a:0",
                "2",
                "-c:a:1",
                "aac",
                "-ac:a:1",
                "1",
                "-metadata:s:a:0",
                "title=Desktop Audio",
                "-metadata:s:a:0",
                "handler_name=Desktop Audio",
                "-metadata:s:a:1",
                "title=Mic/Aux",
                "-metadata:s:a:1",
                "handler_name=Mic/Aux",
                "-shortest",
                str(multi_mp4),
            ]
            proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
            self.assertEqual(proc.returncode, 0, msg=f"合成双音轨 MP4 失败：\n{proc.stderr}")

            streams = probe_audio_streams(multi_mp4)
            self.assertEqual(len(streams), 2)
            self.assertEqual(streams[0]["audio_index"], 0)
            self.assertEqual(streams[1]["audio_index"], 1)
            self.assertFalse(streams[0]["is_likely_mic"])
            self.assertTrue(streams[1]["is_likely_mic"])

            # 选中纯人声麦轨（audio_stream=1）分析，必须精准捕获 3~6s 的高音量区间
            result = analyze_video_audio(
                video_path=multi_mp4,
                output_dir=out_dir,
                top_percent=28.0,
                min_rms=0.05,
                merge_gap_sec=1.0,
                pad_sec=0.5,
                audio_stream=1,
            )
            self.assertEqual(result["audio_stream"], 1)
            self.assertEqual(result["segment_count"], 1)
            seg = result["peak_segments"][0]
            self.assertLessEqual(seg["start_sec"], 3.0)
            self.assertGreaterEqual(seg["end_sec"], 6.0)

            # 越界音轨索引必须抛出清晰异常
            with self.assertRaises(ValueError):
                analyze_video_audio(video_path=multi_mp4, output_dir=out_dir, audio_stream=3)

    def test_vocal_bandpass_filters_low_frequency_explosion(self) -> None:
        """测试 6：验证 300~3400Hz 带通滤波可彻底屏蔽 60Hz 低频爆炸干扰，只捕获人声频段高能。"""
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp = Path(tmp_dir)
            mix_wav = tmp / "boom_and_voice.wav"

            # 合成 9 秒 WAV：
            # 0~3s: 60Hz 超强游戏低频爆炸 (振幅 0.90) -> 非人声频段，应被带通完全滤除
            # 3~6s: 800Hz 人声核心频率 (振幅 0.45) -> 在 300~3400Hz 内，应被准确保留
            # 6~9s: 800Hz 极小背景声 (振幅 0.02)
            sample_rate = 16000
            with wave.open(str(mix_wav), "wb") as wf:
                wf.setnchannels(1)
                wf.setsampwidth(2)
                wf.setframerate(sample_rate)
                frames = bytearray()
                specs = [(3.0, 0.90, 60.0), (3.0, 0.45, 800.0), (3.0, 0.02, 800.0)]
                idx = 0
                for dur, amp, hz in specs:
                    for _ in range(int(round(dur * sample_rate))):
                        t = idx / float(sample_rate)
                        val = int(round(32767.0 * amp * math.sin(2.0 * math.pi * hz * t)))
                        val = max(-32768, min(32767, val))
                        frames.extend(struct.pack("<h", val))
                        idx += 1
                wf.writeframes(bytes(frames))

            # 1. 关闭人声带通时：前 3 秒 60Hz 巨响的 RMS 远高于第 3~6 秒人声
            rms_full, _, _ = compute_per_second_rms(mix_wav, vocal_bandpass=False)
            self.assertEqual(rms_full.size, 9)
            self.assertGreater(float(rms_full[0]), 0.55)
            self.assertGreater(float(rms_full[0]), float(rms_full[3]))

            # 2. 开启 300~3400Hz 人声带通后：前 3 秒 60Hz 爆炸被滤除到接近 0，第 3~6 秒 800Hz 人声占主导
            rms_vocal, _, _ = compute_per_second_rms(mix_wav, vocal_bandpass=True)
            self.assertEqual(rms_vocal.size, 9)
            self.assertLess(float(rms_vocal[0]), 0.02)
            self.assertLess(float(rms_vocal[1]), 0.02)
            self.assertGreater(float(rms_vocal[3]), 0.25)
            self.assertAlmostEqual(float(rms_vocal[4]), 0.45 / math.sqrt(2.0), places=2)


if __name__ == "__main__":
    unittest.main()
