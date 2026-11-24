"""Step 4（src/step4_cut.py）自动化端到端测试。

全部测试使用合成测试视频在临时目录运行，验证：
1. SRT 时间码与专业剪辑时间码格式（含进位边界）
2. 切片独立 SRT 时间归零与边界裁剪
3. lossless 模式严格尊重 `selected` 人工审片开关，并生成 CSV/EDL 标记
4. vertical 模式真实通过 FFmpeg 输出指定竖屏分辨率并成功烧录中文/英文 SRT 字幕
"""

from __future__ import annotations

import csv
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.step1_audio import _normalize_media_cmd, run_media_subprocess  # noqa: E402
from src.step4_cut import (  # noqa: E402
    cut_single_clip,
    format_srt_timestamp,
    format_timecode_fps,
    generate_clip_ass,
    generate_clip_srt,
    render_highlight_clips,
)


def _create_dummy_mp4(mp4_path: Path, width: int = 640, height: int = 360, duration_sec: float = 12.0) -> None:
    """使用 ffmpeg 生成一个带测试画面与正弦波音轨的真实 MP4 视频。"""
    ffmpeg_bin = shutil.which("ffmpeg")
    if not ffmpeg_bin:
        raise RuntimeError("测试环境缺少 ffmpeg")

    cmd = [
        ffmpeg_bin,
        "-y",
        "-f",
        "lavfi",
        "-i",
        f"testsrc=size={width}x{height}:rate=30:duration={duration_sec}",
        "-f",
        "lavfi",
        "-i",
        f"sine=frequency=440:sample_rate=16000:duration={duration_sec}",
        "-c:v",
        "libx264",
        "-pix_fmt",
        "yuv420p",
        "-c:a",
        "aac",
        "-shortest",
        str(mp4_path),
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if proc.returncode != 0:
        raise RuntimeError(f"测试视频生成失败：\n{proc.stderr}")


def _probe_video_streams(mp4_path: Path) -> dict[str, str]:
    """调用 ffprobe 获取输出 MP4 的宽高与流类型。"""
    ffprobe_bin = shutil.which("ffprobe")
    if not ffprobe_bin:
        raise RuntimeError("测试环境缺少 ffprobe")

    cmd = [
        ffprobe_bin,
        "-v",
        "error",
        "-show_entries",
        "stream=codec_type,width,height",
        "-of",
        "json",
        str(mp4_path),
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if proc.returncode != 0:
        raise RuntimeError(f"ffprobe 校验失败：{proc.stderr}")

    info = json.loads(proc.stdout)
    streams = info.get("streams", [])
    video_streams = [s for s in streams if s.get("codec_type") == "video"]
    audio_streams = [s for s in streams if s.get("codec_type") == "audio"]
    return {
        "has_video": str(bool(video_streams)),
        "has_audio": str(bool(audio_streams)),
        "width": str(video_streams[0].get("width", 0)) if video_streams else "0",
        "height": str(video_streams[0].get("height", 0)) if video_streams else "0",
    }


class TestStep4Cut(unittest.TestCase):
    """Step 4 切片与时间线标记导出测试套件。"""

    def test_format_srt_and_timecode(self) -> None:
        """测试 1：验证 SRT 时间码与帧率时间码在进位边界处无溢出 Bug。"""
        self.assertEqual(format_srt_timestamp(0.0), "00:00:00,000")
        self.assertEqual(format_srt_timestamp(59.9999), "00:01:00,000")
        self.assertEqual(format_srt_timestamp(3661.25), "01:01:01,250")

        self.assertEqual(format_timecode_fps(0.0, fps=30.0), "00:00:00:00")
        self.assertEqual(format_timecode_fps(1.5, fps=30.0), "00:00:01:15")
        self.assertEqual(format_timecode_fps(60.0, fps=30.0), "00:01:00:00")

    def test_generate_clip_srt_shifts_and_clips_timestamps(self) -> None:
        """测试 2：验证生成单个切片 SRT 时能正确归零并裁剪跨界字幕。"""
        with tempfile.TemporaryDirectory() as tmp_dir:
            srt_path = Path(tmp_dir) / "clip.srt"
            segments = [
                {"start_sec": 1.0, "end_sec": 4.0, "text": "Unrelated before"},
                {"start_sec": 8.0, "end_sec": 12.0, "text": "Crosses start boundary"},
                {"start_sec": 13.0, "end_sec": 18.0, "text": "Crosses end boundary"},
                {"start_sec": 20.0, "end_sec": 24.0, "text": "Unrelated after"},
            ]

            count = generate_clip_srt(
                transcript_segments=segments,
                clip_start_sec=10.0,
                clip_end_sec=16.0,
                srt_path=srt_path,
            )
            self.assertEqual(count, 2)
            content = srt_path.read_text(encoding="utf-8")
            self.assertNotIn("Unrelated", content)
            # 8~12s 切在 10~16s 里应归零为 00:00:00,000 --> 00:00:02,000
            self.assertIn("00:00:00,000 --> 00:00:02,000\nCrosses start boundary", content)
            # 13~18s 切在 10~16s 里应裁剪到 00:00:03,000 --> 00:00:06,000
            self.assertIn("00:00:03,000 --> 00:00:06,000\nCrosses end boundary", content)

    def test_render_lossless_respects_selected_flag_and_exports_markers(self) -> None:
        """测试 3：验证无损导出严格遵循 selected 开关，并产出 CSV / EDL 标记文件。"""
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp = Path(tmp_dir)
            src_mp4 = tmp / "stream_demo.mp4"
            transcript_json = tmp / "stream_demo_transcript.json"
            highlights_json = tmp / "stream_demo_highlights.json"
            out_dir = tmp / "clips_out"

            _create_dummy_mp4(src_mp4, width=640, height=360, duration_sec=12.0)

            transcript_json.write_text(
                json.dumps(
                    {
                        "segments": [
                            {"start_sec": 1.5, "end_sec": 4.5, "text": "First highlight line"},
                            {"start_sec": 7.0, "end_sec": 10.0, "text": "Skipped highlight line"},
                        ]
                    }
                ),
                encoding="utf-8",
            )
            highlights_json.write_text(
                json.dumps(
                    {
                        "video_path": str(src_mp4),
                        "transcript_json_path": str(transcript_json),
                        "highlights": [
                            {
                                "rank": 1,
                                "selected": True,
                                "start_sec": 1.0,
                                "end_sec": 5.0,
                                "score": 9.3,
                                "title_zh": "保留：高能时刻",
                                "summary_zh": "测试保留片段",
                            },
                            {
                                "rank": 2,
                                "selected": False,
                                "start_sec": 6.5,
                                "end_sec": 10.5,
                                "score": 7.1,
                                "title_zh": "剔除：人工取消勾选",
                                "summary_zh": "不应导出该片段",
                            },
                        ],
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

            result = render_highlight_clips(
                highlights_json_path=highlights_json,
                output_dir=out_dir,
                mode="lossless",
            )

            # 仅导出 1 条（rank=2 因为 selected=False 被跳过）
            self.assertEqual(result["exported_count"], 1)
            clip = result["clips"][0]
            self.assertEqual(clip["rank"], 1)
            self.assertEqual(clip["encoder"], "copy")
            self.assertTrue(Path(clip["mp4_path"]).is_file())
            self.assertTrue(Path(clip["srt_path"]).is_file())
            self.assertEqual(clip["srt_line_count"], 1)

            # 校验 CSV 与 EDL 文件
            csv_path = Path(result["markers_csv_path"])
            edl_path = Path(result["markers_edl_path"])
            self.assertTrue(csv_path.is_file())
            self.assertTrue(edl_path.is_file())

            with csv_path.open("r", encoding="utf-8-sig", newline="") as f:
                rows = list(csv.reader(f))
            self.assertEqual(len(rows), 2)  # 1 表头 + 1 条保留高光
            self.assertEqual(rows[1][4], "Red")
            self.assertIn("保留：高能时刻", rows[1][5])

            edl_text = edl_path.read_text(encoding="utf-8")
            self.assertIn("FROM CLIP NAME: stream_demo.mp4", edl_text)
            self.assertNotIn("剔除：人工取消勾选", edl_text)

    def test_render_vertical_with_burned_subtitles(self) -> None:
        """测试 4：验证 vertical 模式真实生成指定竖屏分辨率并成功烧录中英文字幕。"""
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp = Path(tmp_dir)
            src_mp4 = tmp / "横屏测试_源片.mp4"
            transcript_json = tmp / "demo_transcript.json"
            highlights_json = tmp / "demo_highlights.json"
            out_dir = tmp / "竖屏输出目录"

            _create_dummy_mp4(src_mp4, width=640, height=360, duration_sec=8.0)

            transcript_json.write_text(
                json.dumps(
                    {
                        "segments": [
                            {"start_sec": 1.5, "end_sec": 4.5, "text": "中文测试字幕 Test Sub!"}
                        ]
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            highlights_json.write_text(
                json.dumps(
                    {
                        "video_path": str(src_mp4),
                        "transcript_json_path": str(transcript_json),
                        "highlights": [
                            {
                                "rank": 1,
                                "selected": True,
                                "start_sec": 1.0,
                                "end_sec": 5.0,
                                "score": 9.1,
                                "title_zh": "中文标题:测试冒号?",
                                "summary_zh": "测试竖屏烧录",
                            }
                        ],
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

            result = render_highlight_clips(
                highlights_json_path=highlights_json,
                output_dir=out_dir,
                mode="vertical",
                burn_subtitles=True,
                encoder="auto",
                width=360,
                height=640,
            )

            self.assertEqual(result["exported_count"], 1)
            clip = result["clips"][0]
            self.assertTrue(clip["burned_subtitles"])
            out_mp4 = Path(clip["mp4_path"])
            self.assertTrue(out_mp4.is_file())

            # 确认临时字幕文件已经清理干净
            leftover_tmps = list(out_dir.glob("_burn_tmp_*.*")) + list(out_dir.glob("_render_style_*"))
            self.assertEqual(leftover_tmps, [])

            # ffprobe 验证真实输出为 360x640 竖屏且包含正常音视频流
            stream_info = _probe_video_streams(out_mp4)
            self.assertEqual(stream_info["has_video"], "True")
            self.assertEqual(stream_info["has_audio"], "True")
            self.assertEqual(stream_info["width"], "360")
            self.assertEqual(stream_info["height"], "640")

    def test_generate_bilingual_ass_and_srt(self) -> None:
        """测试 5：验证双语 ASS 分层样式、顶部标题条及双语 SRT 换行导出。"""
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp = Path(tmp_dir)
            ass_path = tmp / "styled.ass"
            srt_path = tmp / "bilingual.srt"

            bilingual_subs = [
                {
                    "start_sec": 100.0,
                    "end_sec": 103.5,
                    "text_orig": "What is wrong with you?",
                    "text_zh": "你们到底有什么毛病啊？",
                    "text_en": "What is wrong with you?",
                }
            ]

            ass_count = generate_clip_ass(
                transcript_segments=[],
                clip_start_sec=98.0,
                clip_end_sec=106.0,
                ass_path=ass_path,
                width=1080,
                height=1920,
                subtitle_lang="bilingual",
                title_banner="主播硬盘藏成人文件夹被抓包！嘴硬表示女孩有爱好",
                mode="vertical",
                highlight_subtitles=bilingual_subs,
            )
            self.assertEqual(ass_count, 1)
            ass_text = ass_path.read_text(encoding="utf-8")
            self.assertIn("PlayResX: 1080", ass_text)
            self.assertIn("PlayResY: 1920", ass_text)
            self.assertIn("Style: Title,", ass_text)
            self.assertIn("Style: ZH,", ass_text)
            self.assertIn("Style: EN,", ass_text)
            self.assertIn("你们到底有什么毛病啊？", ass_text)
            self.assertIn("What is wrong with you?", ass_text)
            self.assertIn(r"\N", ass_text)

            srt_count = generate_clip_srt(
                transcript_segments=[],
                clip_start_sec=98.0,
                clip_end_sec=106.0,
                srt_path=srt_path,
                subtitle_lang="bilingual",
                highlight_subtitles=bilingual_subs,
            )
            self.assertEqual(srt_count, 1)
            srt_text = srt_path.read_text(encoding="utf-8")
            self.assertIn("00:00:02,000 --> 00:00:05,500", srt_text)
            self.assertIn("你们到底有什么毛病啊？\nWhat is wrong with you?", srt_text)

    def test_multilingual_subtitle_pairs_and_release_security_audit(self) -> None:
        """测试 6：验证日→中/韩→中/中→英多语种字幕搭配（含倒序双语与原生零缺字字体）及发布零泄露扫描门禁。"""
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp = Path(tmp_dir)
            # 1. 日→中双语 ASS：副行自动匹配 Meiryo 防日文缺字，主行 Microsoft YaHei
            ja_subs = [
                {
                    "start_sec": 10.0,
                    "end_sec": 13.0,
                    "text_src": "マジでやばいじゃんこれ草生える！",
                    "text_tgt": "卧槽这也太离谱了吧笑死我了！",
                    "text_en": "マジでやばいじゃんこれ草生える！",
                    "text_zh": "卧槽这也太离谱了吧笑死我了！",
                }
            ]
            ja_ass = tmp / "ja_zh.ass"
            generate_clip_ass(
                [],
                9.0,
                14.0,
                ja_ass,
                subtitle_lang="bilingual",
                highlight_subtitles=ja_subs,
                source_language="ja",
                target_language="zh",
            )
            ja_ass_txt = ja_ass.read_text(encoding="utf-8")
            self.assertIn("Style: ZH,Microsoft YaHei,", ja_ass_txt)
            self.assertIn("Style: EN,Yu Gothic UI,", ja_ass_txt)

            # 2. 韩→中倒序双语（韩上/中下）SRT 与 ASS（主行自动 Malgun Gothic）
            ko_subs = [
                {
                    "start_sec": 10.0,
                    "end_sec": 13.0,
                    "text_src": "아니 진짜 미쳤어 폼 미쳤다!",
                    "text_tgt": "不是哥们牛逼炸了，状态无敌！",
                }
            ]
            ko_ass = tmp / "ko_rev.ass"
            ko_srt = tmp / "ko_rev.srt"
            generate_clip_ass(
                [],
                9.0,
                14.0,
                ko_ass,
                subtitle_lang="bilingual_rev",
                highlight_subtitles=ko_subs,
                source_language="ko",
                target_language="zh",
            )
            generate_clip_srt(
                [],
                9.0,
                14.0,
                ko_srt,
                subtitle_lang="bilingual_rev",
                highlight_subtitles=ko_subs,
                source_language="ko",
                target_language="zh",
            )
            self.assertIn("Style: ZH,Malgun Gothic,", ko_ass.read_text(encoding="utf-8"))
            self.assertIn("아니 진짜 미쳤어 폼 미쳤다!\n不是哥们牛逼炸了，状态无敌！", ko_srt.read_text(encoding="utf-8"))

            # 3. 中→英出海仅译文（target）与仅原文（source）
            zh_en_subs = [
                {
                    "start_sec": 1.0,
                    "end_sec": 4.0,
                    "text_src": "这波弹反直接破防了！",
                    "text_tgt": "That missed parry totally tilted me!",
                    "text_zh": "这波弹反直接破防了！",
                    "text_en": "That missed parry totally tilted me!",
                }
            ]
            tgt_srt = tmp / "tgt.srt"
            src_srt = tmp / "src.srt"
            generate_clip_srt([], 0.0, 5.0, tgt_srt, subtitle_lang="target", highlight_subtitles=zh_en_subs, source_language="zh", target_language="en")
            generate_clip_srt([], 0.0, 5.0, src_srt, subtitle_lang="source", highlight_subtitles=zh_en_subs, source_language="zh", target_language="en")
            self.assertIn("That missed parry totally tilted me!", tgt_srt.read_text(encoding="utf-8"))
            self.assertNotIn("破防", tgt_srt.read_text(encoding="utf-8"))
            self.assertIn("这波弹反直接破防了！", src_srt.read_text(encoding="utf-8"))

            # 4. 验证安全发布审计器精准拦截密钥、.env 与非商用语料
            from scripts.build_release import run_security_audit

            clean_dir = tmp / "clean_rel"
            clean_dir.mkdir()
            (clean_dir / "README.md").write_text("# Solacut\n", encoding="utf-8")
            self.assertEqual(run_security_audit(clean_dir), [])

            dirty_dir = tmp / "dirty_rel"
            (dirty_dir / "data" / "corpus").mkdir(parents=True)
            (dirty_dir / ".env").write_text("OPENAI_API_KEY=sk-1234567890abcdef123456\n", encoding="utf-8")
            (dirty_dir / "leak.txt").write_text("token=sk-testsecretkey999999999999\n", encoding="utf-8")
            (dirty_dir / "data" / "corpus" / "corpus_manifest.json").write_text(
                json.dumps({"lang_pairs": {"en_zh": {"sources": [{"source_id": "ted2020_en_zh", "commercial_safe": False, "license": "CC BY-NC-ND"}]}}}),
                encoding="utf-8",
            )
            violations = run_security_audit(dirty_dir)
            self.assertGreaterEqual(len(violations), 3)

    def test_media_subprocess_hides_window_and_injects_safe_flags(self) -> None:
        """测试 7：验证 Windows 下 FFmpeg/FFprobe 均静默无黑窗、禁 stdin 且自动注入 -hide_banner -nostdin。"""
        ff_cmd = _normalize_media_cmd(["C:/bin/ffmpeg.exe", "-y", "-i", "a.mp4", "b.mp4"])
        self.assertEqual(ff_cmd[1:3], ["-hide_banner", "-nostdin"])
        # 幂等去重
        self.assertEqual(_normalize_media_cmd(ff_cmd).count("-nostdin"), 1)

        fp_cmd = _normalize_media_cmd(["ffprobe.exe", "-v", "error", "a.mp4"])
        self.assertEqual(fp_cmd[1], "-hide_banner")
        self.assertNotIn("-nostdin", fp_cmd)

        captured_kwargs: dict[str, object] = {}

        class _FakePopen:
            def __init__(self, args: list[str], **kwargs: object) -> None:
                captured_kwargs["args"] = args
                captured_kwargs.update(kwargs)
                self.returncode = 0

            def communicate(self, timeout: float | None = None) -> tuple[str, str]:
                return "ok", ""

            def poll(self) -> int:
                return 0

        with mock.patch("src.step1_audio.subprocess.Popen", _FakePopen):
            res = run_media_subprocess(["ffmpeg.exe", "-version"])
        self.assertEqual(res.returncode, 0)
        self.assertEqual(captured_kwargs["stdin"], subprocess.DEVNULL)
        if sys.platform == "win32":
            self.assertEqual(captured_kwargs["creationflags"], subprocess.CREATE_NO_WINDOW)

    def test_lossless_signal15_retry_and_mkv_pcm_aac_fallback(self) -> None:
        """测试 8：验证遇 signal 15/STSC 瞬时中断自动重试恢复，且 OBS MKV(PCM音轨) 自动降级 copy+aac。"""
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp = Path(tmp_dir)
            src_mp4 = tmp / "sample.mp4"
            _create_dummy_mp4(src_mp4, width=320, height=240, duration_sec=6.0)

            # 8A. 模拟首次触发用户原报错 signal 15 + STSC，第二次正常走真实 FFmpeg 恢复成功
            real_run_ffmpeg = __import__("src.step4_cut", fromlist=["_run_ffmpeg"])._run_ffmpeg
            call_count = {"n": 0}

            def _flaky_ffmpeg(cmd: list[str], cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
                call_count["n"] += 1
                if call_count["n"] == 1:
                    return subprocess.CompletedProcess(
                        args=cmd,
                        returncode=255,
                        stdout="",
                        stderr=(
                            "[in#0] STSC entry 210 is invalid (first=242 count=0 id=0)\n"
                            "[in#0] reached eof, corrupted STSC atom\n"
                            "Error opening input files: End of file\n"
                            "Exiting normally, received signal 15.\n"
                        ),
                    )
                return real_run_ffmpeg(cmd, cwd=cwd)

            out_retry = tmp / "retry_ok.mp4"
            with mock.patch("src.step4_cut._run_ffmpeg", side_effect=_flaky_ffmpeg):
                info = cut_single_clip(src_mp4, 1.0, 4.0, out_retry, mode="lossless")
            self.assertGreaterEqual(call_count["n"], 2)
            self.assertEqual(info["encoder"], "copy")
            self.assertTrue(out_retry.is_file() and out_retry.stat().st_size > 0)
            self.assertEqual(list(tmp.glob(".part_*.mp4")), [])

            # 8B. 真实含 MP4 不支持音轨的 MKV（如 wmav2 / 老版 FFmpeg 下的 PCM），lossless 自动保留原画并转 AAC 192k
            ffmpeg_bin = shutil.which("ffmpeg")
            assert ffmpeg_bin is not None
            obs_mkv = tmp / "obs_incompat_audio.mkv"
            mk_proc = run_media_subprocess(
                [
                    ffmpeg_bin,
                    "-y",
                    "-f",
                    "lavfi",
                    "-i",
                    "testsrc=size=320x240:rate=30:duration=5",
                    "-f",
                    "lavfi",
                    "-i",
                    "sine=frequency=440:sample_rate=44100:duration=5",
                    "-c:v",
                    "libx264",
                    "-pix_fmt",
                    "yuv420p",
                    "-c:a",
                    "wmav2",
                    "-shortest",
                    str(obs_mkv),
                ]
            )
            self.assertEqual(mk_proc.returncode, 0, mk_proc.stderr)
            out_from_mkv = tmp / "from_obs_mkv.mp4"
            mkv_info = cut_single_clip(obs_mkv, 0.5, 4.0, out_from_mkv, mode="lossless")
            self.assertEqual(mkv_info["encoder"], "copy+aac")
            self.assertTrue(out_from_mkv.is_file() and out_from_mkv.stat().st_size > 0)
            st = _probe_video_streams(out_from_mkv)
            self.assertEqual(st["has_video"], "True")
            self.assertEqual(st["has_audio"], "True")

    def test_landscape_odd_size_and_ranks_override_and_orphan_cleanup(self) -> None:
        """测试 9：验证奇数尺寸横屏重编码不崩、单条导出未勾选 rank 不被吞掉、进度回调与失败清理孤儿 SRT。"""
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp = Path(tmp_dir)
            ffmpeg_bin = shutil.which("ffmpeg")
            assert ffmpeg_bin is not None

            # 生成奇数宽高 321x241 的 MKV（窗口录屏常见坑）
            odd_mkv = tmp / "odd_capture.mkv"
            mk_odd = run_media_subprocess(
                [
                    ffmpeg_bin,
                    "-y",
                    "-f",
                    "lavfi",
                    "-i",
                    "testsrc=size=321x241:rate=30:duration=5",
                    "-f",
                    "lavfi",
                    "-i",
                    "sine=frequency=440:sample_rate=16000:duration=5",
                    "-c:v",
                    "libx264rgb",
                    "-c:a",
                    "aac",
                    "-shortest",
                    str(odd_mkv),
                ]
            )
            self.assertEqual(mk_odd.returncode, 0, mk_odd.stderr)

            odd_out = tmp / "odd_landscape.mp4"
            odd_info = cut_single_clip(odd_mkv, 0.5, 3.5, odd_out, mode="landscape", encoder="x264")
            self.assertEqual(odd_info["encoder"], "libx264")
            st = _probe_video_streams(odd_out)
            self.assertEqual(int(st["width"]) % 2, 0)
            self.assertEqual(int(st["height"]) % 2, 0)

            # 单条导出未勾选 rank=2 + 逐条进度回调
            src_mp4 = tmp / "demo.mp4"
            _create_dummy_mp4(src_mp4, width=320, height=240, duration_sec=8.0)
            hl_json = tmp / "demo_highlights.json"
            hl_json.write_text(
                json.dumps(
                    {
                        "video_path": str(src_mp4),
                        "highlights": [
                            {"rank": 1, "selected": True, "start_sec": 0.5, "end_sec": 3.0, "score": 9.0, "title_zh": "第一条"},
                            {"rank": 2, "selected": False, "start_sec": 3.5, "end_sec": 6.5, "score": 8.5, "title_zh": "未勾选单导"},
                        ],
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            progress_events: list[tuple[str, int, str]] = []
            out_dir = tmp / "out_single"
            res = render_highlight_clips(
                highlights_json_path=hl_json,
                output_dir=out_dir,
                mode="lossless",
                ranks=[2],
                on_progress=lambda stage, pct, msg: progress_events.append((stage, pct, msg)),
            )
            self.assertEqual(res["exported_count"], 1)
            self.assertEqual(res["clips"][0]["rank"], 2)
            self.assertGreaterEqual(len(progress_events), 2)

            # 失败时自动清理本次新建的孤儿 SRT
            fail_dir = tmp / "out_fail"
            with mock.patch("src.step4_cut.cut_single_clip", side_effect=RuntimeError("mock cut boom")):
                with self.assertRaises(RuntimeError):
                    render_highlight_clips(highlights_json_path=hl_json, output_dir=fail_dir, mode="lossless")
            self.assertEqual(list(fail_dir.glob("*.srt")), [])


if __name__ == "__main__":
    unittest.main()
