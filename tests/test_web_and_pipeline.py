"""一键流水线（src/pipeline.py）与 Web 工作台接口（src/web_app.py）端到端测试。

全部测试使用临时目录与 FastAPI TestClient 运行，不修改项目真实配置与输出目录。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
import wave
import math
import struct
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import src.pipeline as pipeline_mod  # noqa: E402
import src.web_app as web_app  # noqa: E402
from src.pipeline import run_quick_diagnose, scan_input_videos  # noqa: E402
from src.step1_audio import analyze_video_audio, check_ffmpeg_available  # noqa: E402


class TestWebAndPipeline(unittest.TestCase):
    """测试流水线扫描与图形化工作台 API 闭环。"""

    def test_scan_input_videos_detects_stages(self) -> None:
        """测试 1：验证扫描嵌套目录视频并准确判定 idle / analyzed / exported 阶段。"""
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp = Path(tmp_dir)
            in_dir = tmp / "input"
            out_dir = tmp / "output"
            (in_dir / "vods").mkdir(parents=True)
            out_dir.mkdir(parents=True)

            v1 = in_dir / "vods" / "match_one.mp4"
            v2 = in_dir / "match_two.mkv"
            v1.write_bytes(b"fake-mp4-data")
            v2.write_bytes(b"fake-mkv-data")

            # match_one 已出高光但未出片；match_two 未处理
            (out_dir / "match_one_audio_peaks.json").write_text(
                json.dumps(
                    {
                        "total_duration_sec": 300.0,
                        "total_duration_str": "05:00",
                        "rms_threshold": 0.05,
                        "segment_count": 2,
                        "peak_segments": [
                            {"start_sec": 60.0, "peak_rms": 0.22},
                            {"start_sec": 180.0, "peak_rms": 0.19},
                        ],
                    }
                ),
                encoding="utf-8",
            )
            (out_dir / "match_one_highlights.json").write_text(
                json.dumps(
                    {
                        "total_duration_sec": 300.0,
                        "total_duration_str": "05:00",
                        "bilingual_subtitles": True,
                        "highlights": [{"rank": 1, "selected": True}, {"rank": 2, "selected": False}],
                    }
                ),
                encoding="utf-8",
            )

            items = scan_input_videos(in_dir, out_dir)
            self.assertEqual(len(items), 2)
            by_stem = {item["stem"]: item for item in items}

            self.assertEqual(by_stem["match_one"]["stage"], "analyzed")
            self.assertEqual(by_stem["match_one"]["grade"], "S")
            self.assertIsNotNone(by_stem["match_one"]["diagnostics"])
            self.assertEqual(by_stem["match_one"]["highlight_count"], 2)
            self.assertEqual(by_stem["match_one"]["selected_count"], 1)
            self.assertTrue(by_stem["match_one"]["bilingual_subtitles"])
            self.assertEqual(by_stem["match_two"]["stage"], "idle")
            self.assertIsNone(by_stem["match_two"]["grade"])

    def test_web_app_api_end_to_end(self) -> None:
        """测试 2：验证工作台首页、配置保存、视频列表、审片保存刷新 MD 与 Range 视频流。"""
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp = Path(tmp_dir)
            in_dir = tmp / "in"
            out_dir = tmp / "out"
            in_dir.mkdir()
            out_dir.mkdir()

            video_file = in_dir / "clip_test.mp4"
            video_file.write_bytes(b"0123456789ABCDEF")

            peaks_file = out_dir / "clip_test_audio_peaks.json"
            peaks_file.write_text(
                json.dumps(
                    {
                        "total_duration_sec": 120.0,
                        "total_duration_str": "02:00",
                        "rms_threshold": 0.04,
                        "segment_count": 1,
                        "peak_segments": [{"start_sec": 20.0, "peak_rms": 0.16}],
                    }
                ),
                encoding="utf-8",
            )

            hl_file = out_dir / "clip_test_highlights.json"
            md_file = out_dir / "clip_test_review.md"
            hl_file.write_text(
                json.dumps(
                    {
                        "video_path": str(video_file),
                        "transcript_json_path": "",
                        "highlights_json_path": str(hl_file),
                        "review_md_path": str(md_file),
                        "model": "deepseek-chat",
                        "total_duration_sec": 120.0,
                        "total_duration_str": "02:00",
                        "selection_plan": {
                            "duration_tier": "< 30分钟",
                            "target_count": 4,
                            "min_clip_sec": 15,
                            "max_clip_sec": 45,
                        },
                        "highlight_count": 1,
                        "elapsed_sec": 1.0,
                        "highlights": [
                            {
                                "rank": 1,
                                "selected": True,
                                "start_sec": 10.0,
                                "end_sec": 40.0,
                                "duration_sec": 30.0,
                                "start_str": "00:10",
                                "end_str": "00:40",
                                "score": 9.0,
                                "title_zh": "原标题",
                                "title_en": "Original Title",
                                "summary_zh": "旧梗概",
                                "reason": "测试",
                                "tags": ["高光"],
                            }
                        ],
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

            with (
                patch.object(web_app, "_CONFIG_PATH", tmp / "cfg.json"),
                patch.object(web_app, "_ENV_PATH", tmp / ".env"),
                patch.dict(os.environ, {}, clear=False),
            ):
                client = TestClient(web_app.app)

                # 1. 访问首页、静态 CSS/JS 与品牌应用图标，并自动获得会话 Token 与 SameSite=Strict Cookie
                idx_resp = client.get("/")
                self.assertEqual(idx_resp.status_code, 200)
                self.assertIn("Solacut", idx_resp.text)
                self.assertIn('name="solacut-token"', idx_resp.text)
                self.assertIn("/static/brand/logo.png", idx_resp.text)
                self.assertEqual(idx_resp.cookies.get("solacut_session"), web_app._SESSION_TOKEN)
                self.assertEqual(client.get("/favicon.ico").status_code, 200)
                self.assertEqual(client.get("/static/brand/logo.png").status_code, 200)
                self.assertEqual(client.get("/static/styles.css").status_code, 200)
                self.assertEqual(client.get("/static/app.js").status_code, 200)

                auth_headers = {"X-Solacut-Token": web_app._SESSION_TOKEN}

                # 2. 保存配置并隐藏密钥
                cfg_resp = client.post(
                    "/api/config",
                    headers=auth_headers,
                    json={
                        "input_dir": str(in_dir),
                        "output_dir": str(out_dir),
                        "api_key": "sk-test1234567890abcd",
                        "base_url": "https://api.deepseek.com/v1",
                        "llm_model": "deepseek-chat",
                    },
                )
                self.assertEqual(cfg_resp.status_code, 200)
                cfg_data = cfg_resp.json()
                self.assertTrue(cfg_data["has_api_key"])
                self.assertIn("****", cfg_data["api_key_masked"])
                self.assertNotIn("sk-test1234567890abcd", cfg_data["api_key_masked"])

                # 3. 扫描视频 & 查询实时诊断报告
                vid_resp = client.get("/api/videos", headers=auth_headers)
                self.assertEqual(vid_resp.status_code, 200)
                self.assertEqual(vid_resp.json()["count"], 1)

                diag_resp = client.get("/api/diagnostics", params={"stem": "clip_test"}, headers=auth_headers)
                self.assertEqual(diag_resp.status_code, 200)
                self.assertEqual(diag_resp.json()["diagnostics"]["grade"], "S")

                # 4. 读取并修改保存审片结果，验证同步刷新 review.md
                hl_resp = client.get("/api/highlights", params={"stem": "clip_test"}, headers=auth_headers)
                self.assertEqual(hl_resp.status_code, 200)
                hl_payload = hl_resp.json()
                hl_payload["highlights"][0]["selected"] = False
                hl_payload["highlights"][0]["title_zh"] = "修改后的高光标题"

                save_resp = client.put(
                    "/api/highlights",
                    headers=auth_headers,
                    json={
                        "highlights_json_path": str(hl_file),
                        "highlights": hl_payload["highlights"],
                    },
                )
                self.assertEqual(save_resp.status_code, 200)
                self.assertEqual(save_resp.json()["selected_count"], 0)
                self.assertIn("修改后的高光标题", md_file.read_text(encoding="utf-8"))

                # 5. HTTP 206 Range 流式播放（无需手工带 Header，直接凭首页下发的 SameSite=Strict Cookie 放行）
                stream_resp = client.get(
                    "/api/video-stream",
                    params={"path": str(video_file)},
                    headers={"Range": "bytes=4-9"},
                )
                self.assertEqual(stream_resp.status_code, 206)
                self.assertEqual(stream_resp.content, b"456789")
                self.assertEqual(stream_resp.headers["content-range"], "bytes 4-9/16")

                # 禁止读取工作区外 .env（返 403）及工作区内非视频文件（返 404）
                blocked_env = client.get("/api/video-stream", params={"path": str(tmp / ".env")})
                self.assertEqual(blocked_env.status_code, 403)
                in_note = in_dir / "not_a_video.txt"
                in_note.write_text("plain", encoding="utf-8")
                blocked_ext = client.get("/api/video-stream", params={"path": str(in_note)})
                self.assertEqual(blocked_ext.status_code, 404)

    def test_audio_streams_endpoint_and_pipeline_cache_invalidation(self) -> None:
        """测试 3：验证 Web /api/audio-streams 接口与切换音轨/带通时的缓存失效重算机制。"""
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp = Path(tmp_dir)
            wav0 = tmp / "desk.wav"
            wav1 = tmp / "mic.wav"
            mp4_file = tmp / "dual_obs.mp4"
            out_dir = tmp / "out"
            not_video = tmp / "note.txt"
            not_video.write_text("hello", encoding="utf-8")

            # 生成 2 个 4 秒 16k WAV
            sr = 16000
            for target_wav, amp in ((wav0, 0.05), (wav1, 0.40)):
                with wave.open(str(target_wav), "wb") as wf:
                    wf.setnchannels(1)
                    wf.setsampwidth(2)
                    wf.setframerate(sr)
                    buf = bytearray()
                    for idx in range(4 * sr):
                        val = int(round(32767.0 * amp * math.sin(2.0 * math.pi * 600.0 * idx / sr)))
                        buf.extend(struct.pack("<h", max(-32768, min(32767, val))))
                    wf.writeframes(bytes(buf))

            ffmpeg_bin = check_ffmpeg_available()
            cmd = [
                ffmpeg_bin,
                "-y",
                "-f",
                "lavfi",
                "-i",
                "color=c=black:s=320x240:r=10:d=4.0",
                "-i",
                str(wav0),
                "-i",
                str(wav1),
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
                "-metadata:s:a:1",
                "title=Mic/Aux",
                "-shortest",
                str(mp4_file),
            ]
            proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
            self.assertEqual(proc.returncode, 0, msg=proc.stderr)

            # 1. 验证 Web 接口（先在工作区配置注册临时输入/输出目录）
            cfg_path = tmp / "cfg.json"
            cfg_path.write_text(
                json.dumps({"input_dir": str(tmp), "output_dir": str(out_dir)}),
                encoding="utf-8",
            )
            with patch.object(web_app, "_CONFIG_PATH", cfg_path), patch.object(web_app, "_ENV_PATH", tmp / ".env"):
                client = TestClient(web_app.app, headers={"X-Solacut-Token": web_app._SESSION_TOKEN})
                resp = client.get("/api/audio-streams", params={"path": str(mp4_file)})
                self.assertEqual(resp.status_code, 200)
                payload = resp.json()
                self.assertEqual(payload["count"], 2)
                self.assertEqual(payload["recommended_stream"], 1)
                self.assertTrue(payload["streams"][1]["is_likely_mic"])

                bad_resp = client.get("/api/audio-streams", params={"path": str(not_video)})
                self.assertEqual(bad_resp.status_code, 404)

            # 2. 验证 pipeline 缓存命中与切换音轨/带通自动失效重跑
            with patch.object(pipeline_mod, "analyze_video_audio", wraps=analyze_video_audio) as spy_analyze:
                run_quick_diagnose(mp4_file, output_dir=out_dir, audio_stream=0, vocal_bandpass=True)
                self.assertEqual(spy_analyze.call_count, 1)

                # 同参数应复用缓存
                run_quick_diagnose(mp4_file, output_dir=out_dir, audio_stream=0, vocal_bandpass=True)
                self.assertEqual(spy_analyze.call_count, 1)

                # 关闭带通滤波应自动失效并重跑
                run_quick_diagnose(mp4_file, output_dir=out_dir, audio_stream=0, vocal_bandpass=False)
                self.assertEqual(spy_analyze.call_count, 2)

                # 切换至第 1 轨 + 开启带通应再次重跑并更新 JSON 字段
                res = run_quick_diagnose(mp4_file, output_dir=out_dir, audio_stream=1, vocal_bandpass=True)
                self.assertEqual(spy_analyze.call_count, 3)

            peaks_saved = json.loads(Path(res["peaks_json_path"]).read_text(encoding="utf-8"))
            self.assertEqual(peaks_saved["audio_stream"], 1)
            self.assertTrue(peaks_saved["vocal_bandpass"])

    def test_layered_config_api_and_step2_cache_invalidation(self) -> None:
        """测试 4：验证 Web 三层路由配置读写脱敏，以及 Step 2 修改引擎/高峰数/模型时的缓存失效判定。"""
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp = Path(tmp_dir)
            with (
                patch.object(web_app, "_CONFIG_PATH", tmp / "cfg.json"),
                patch.object(web_app, "_ENV_PATH", tmp / ".env"),
                patch.dict(os.environ, {}, clear=False),
            ):
                client = TestClient(web_app.app, headers={"X-Solacut-Token": web_app._SESSION_TOKEN})
                post_resp = client.post(
                    "/api/config",
                    json={
                        "api_key": "sk-deepseek-main-123456",
                        "base_url": "https://api.deepseek.com/v1",
                        "llm_model": "deepseek-flash",
                        "asr_provider": "deepseek",
                        "asr_hotwords": "Shylily, KEKW",
                        "polish_llm_model": "deepseek-v4-pro",
                        "polish_reasoning_effort": "low",
                    },
                )
                self.assertEqual(post_resp.status_code, 200)
                data = post_resp.json()
                self.assertEqual(data["asr_provider"], "deepseek")
                self.assertEqual(data["screen_llm_model"], "deepseek-flash")
                self.assertEqual(data["polish_effective_model"], "deepseek-v4-pro")
                self.assertEqual(data["polish_reasoning_effort"], "low")
                self.assertEqual(data["asr_hotwords"], "Shylily, KEKW")
                self.assertTrue(data["has_asr_api_key"])
                self.assertNotIn("123456", data["api_key_masked"])

        # 验证 Step 2 缓存签名比对逻辑：改引擎、改高峰数、改模型或关归一化必须判定失效
        cached_tr = {
            "asr_provider": "deepseek",
            "mode": "peaks",
            "top_k_peaks": 20,
            "asr_model": "deepseek-asr",
            "normalize_audio": True,
            "vad_threshold": 0.30,
            "language": "en",
        }
        self.assertTrue(
            pipeline_mod._transcript_cache_matches(
                cached_tr, "deepseek", "peaks", 20, "deepseek-asr", "en", True, 0.30
            )
        )
        # 切换到本地 Whisper 或更改 top_k_peaks 必须立刻判失效
        self.assertFalse(
            pipeline_mod._transcript_cache_matches(
                cached_tr, "local", "peaks", 20, "large-v3-turbo", "en", True, 0.30
            )
        )
        self.assertFalse(
            pipeline_mod._transcript_cache_matches(
                cached_tr, "deepseek", "peaks", 40, "deepseek-asr", "en", True, 0.30
            )
        )
        self.assertFalse(
            pipeline_mod._transcript_cache_matches(
                cached_tr, "deepseek", "peaks", 20, "deepseek-asr", "en", False, 0.30
            )
        )

    def test_nle_media_info_waveform_thumbnail_and_task_cancel_apis(self) -> None:
        """测试 5：验证 Step B 新增的 /api/media-info、/api/waveform、/api/thumbnail 与队列取消/清理接口。"""
        ffmpeg = check_ffmpeg_available()
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp = Path(tmp_dir)
            in_dir = tmp / "in"
            out_dir = tmp / "out"
            in_dir.mkdir()
            out_dir.mkdir()

            # 合成一段 3 秒 320x180 30fps 带正弦音轨的真实测试视频
            video_file = in_dir / "nle_demo.mp4"
            subprocess.run(
                [
                    ffmpeg,
                    "-y",
                    "-f",
                    "lavfi",
                    "-i",
                    "color=c=black:s=320x180:r=30:d=3",
                    "-f",
                    "lavfi",
                    "-i",
                    "sine=frequency=660:duration=3",
                    "-c:v",
                    "libx264",
                    "-pix_fmt",
                    "yuv420p",
                    "-c:a",
                    "aac",
                    str(video_file),
                ],
                check=True,
                capture_output=True,
            )

            # 生成一段 16kHz 16-bit PCM WAV 供 /api/waveform 测试
            wav_file = out_dir / "nle_demo_16k.wav"
            sr = 16000
            with wave.open(str(wav_file), "wb") as wf:
                wf.setnchannels(1)
                wf.setsampwidth(2)
                wf.setframerate(sr)
                frames = bytearray()
                for i in range(sr * 3):
                    amp = 0.6 if sr <= i < sr * 2 else 0.05
                    val = int(max(-32767, min(32767, amp * 32767 * math.sin(2 * math.pi * 440 * i / sr))))
                    frames.extend(struct.pack("<h", val))
                wf.writeframes(bytes(frames))

            peaks_file = out_dir / "nle_demo_audio_peaks.json"
            peaks_file.write_text(
                json.dumps(
                    {
                        "total_duration_sec": 3.0,
                        "total_duration_str": "00:03",
                        "rms_threshold": 0.12,
                        "segment_count": 1,
                        "peak_segments": [{"start_sec": 1.0, "end_sec": 2.0, "peak_rms": 0.42}],
                    }
                ),
                encoding="utf-8",
            )

            cfg_path = tmp / "cfg.json"
            cfg_path.write_text(
                json.dumps({"input_dir": str(in_dir), "output_dir": str(out_dir)}),
                encoding="utf-8",
            )
            with (
                patch.object(web_app, "_CONFIG_PATH", cfg_path),
                patch.object(web_app, "_ENV_PATH", tmp / ".env"),
            ):
                client = TestClient(web_app.app, headers={"X-Solacut-Token": web_app._SESSION_TOKEN})

                # 1. /api/media-info 探测分辨率与真实 FPS
                mi = client.get("/api/media-info", params={"path": str(video_file)})
                self.assertEqual(mi.status_code, 200)
                mi_data = mi.json()
                self.assertEqual(mi_data["width"], 320)
                self.assertEqual(mi_data["height"], 180)
                self.assertAlmostEqual(mi_data["fps"], 30.0, delta=0.5)
                self.assertGreaterEqual(mi_data["duration_sec"], 2.5)
                self.assertEqual(mi_data["audio_stream_count"], 1)

                # 2. /api/waveform WAV 全片总览采样（自动生成 overview 缓存）与局部放大采样
                wf_full = client.get(
                    "/api/waveform",
                    params={"stem": "nle_demo", "output_dir": str(out_dir), "points": 1200},
                )
                self.assertEqual(wf_full.status_code, 200)
                wf_data = wf_full.json()
                self.assertEqual(wf_data["source"], "wav")
                self.assertEqual(wf_data["points"], 1200)
                self.assertTrue((out_dir / "nle_demo_waveform_overview.json").is_file())
                # 中间高能区（1~2s）峰值应明显大于前段低音区（0~1s）
                self.assertGreater(max(wf_data["peak"][450:750]), max(wf_data["peak"][50:250]) * 3)

                wf_zoom = client.get(
                    "/api/waveform",
                    params={"stem": "nle_demo", "output_dir": str(out_dir), "start": 1.0, "end": 2.0, "points": 120},
                )
                self.assertEqual(wf_zoom.status_code, 200)
                self.assertEqual(wf_zoom.json()["points"], 120)

                # 无 WAV 时降级为 peaks_fallback
                wav_file.unlink()
                wf_fb = client.get(
                    "/api/waveform",
                    params={"stem": "nle_demo", "output_dir": str(out_dir), "points": 60},
                )
                self.assertEqual(wf_fb.status_code, 200)
                self.assertEqual(wf_fb.json()["source"], "peaks_fallback")

                # 3. /api/diagnostics 附带吐出 peak_segments
                diag_resp = client.get("/api/diagnostics", params={"stem": "nle_demo", "output_dir": str(out_dir)})
                self.assertEqual(diag_resp.status_code, 200)
                self.assertEqual(len(diag_resp.json()["peak_segments"]), 1)

                # 4. /api/thumbnail 抽取真实 JPEG 缩略图
                thumb_resp = client.get(
                    "/api/thumbnail",
                    params={"path": str(video_file), "time": 1.0, "w": 160, "output_dir": str(out_dir)},
                )
                self.assertEqual(thumb_resp.status_code, 200)
                self.assertEqual(thumb_resp.headers["content-type"], "image/jpeg")
                self.assertTrue(thumb_resp.content.startswith(b"\xff\xd8"))

                # 5. TaskManager 取消排队任务与清理已结束历史
                tm = web_app.TaskManager()
                with tm._lock:
                    tm._tasks["q1"] = {
                        "task_id": "q1",
                        "status": "queued",
                        "stage": "queued",
                        "message": "queued",
                        "logs": [],
                        "updated_at": 0.0,
                    }
                    tm._tasks["d1"] = {
                        "task_id": "d1",
                        "status": "completed",
                        "stage": "done",
                        "message": "done",
                        "logs": [],
                        "updated_at": 0.0,
                    }
                    tm._order = ["q1", "d1"]
                with patch.object(web_app, "_TASK_MANAGER", tm):
                    c_resp = client.delete("/api/tasks/q1")
                    self.assertEqual(c_resp.status_code, 200)
                    self.assertEqual(c_resp.json()["task"]["status"], "cancelled")
                    clr_resp = client.post("/api/tasks/clear")
                    self.assertEqual(clr_resp.status_code, 200)
                    self.assertEqual(clr_resp.json()["removed"], 2)

    def test_local_security_hardening_guards_csrf_dns_rebinding_and_path_boundaries(self) -> None:
        """测试 6：全面验证上线前安全加固（会话令牌、CSRF/DNS Rebinding 拦截、关闭文档、UNC/路径边界、.env 注入与密钥脱敏）。"""
        from src.config import redact_secrets_in_text, write_env_settings

        raw_client = TestClient(web_app.app)
        authed_client = TestClient(web_app.app, headers={"X-Solacut-Token": web_app._SESSION_TOKEN})

        # 1. 生产态已关闭 /docs 与 /openapi.json，/api/health 允许无令牌探活
        self.assertEqual(raw_client.get("/docs").status_code, 404)
        self.assertEqual(raw_client.get("/openapi.json").status_code, 404)
        health = raw_client.get("/api/health")
        self.assertEqual(health.status_code, 200)
        self.assertEqual(health.headers.get("x-content-type-options"), "nosniff")
        self.assertEqual(health.headers.get("x-frame-options"), "DENY")

        # 2. 未携带 X-Solacut-Token 拦截所有敏感业务接口（即使持有 Cookie 也不允许越权修改配置）
        self.assertEqual(raw_client.get("/api/config").status_code, 403)
        self.assertEqual(raw_client.post("/api/config", json={"base_url": "https://evil.example/v1"}).status_code, 403)
        self.assertEqual(raw_client.post("/api/open-folder", json={"path": "."}).status_code, 403)
        # 仅访问首页拿 Cookie 后，普通 API 仍必须携带 X-Solacut-Token
        raw_client.get("/")
        self.assertEqual(raw_client.post("/api/config", json={"base_url": "https://evil.example/v1"}).status_code, 403)

        # 3. 拦截跨站 Origin、Sec-Fetch-Site: cross-site 与 DNS Rebinding Host
        self.assertEqual(
            authed_client.get("/api/config", headers={"Origin": "https://evil.attacker.com"}).status_code,
            403,
        )
        self.assertEqual(
            authed_client.get("/api/config", headers={"Sec-Fetch-Site": "cross-site"}).status_code,
            403,
        )
        self.assertEqual(
            authed_client.get("/api/config", headers={"Host": "attacker.rebind.test:8765"}).status_code,
            403,
        )

        # 4. 高危路径边界防护：拒绝 UNC 路径、拒绝覆写非 *_highlights.json 文件、拒绝非法协议 Base URL
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp = Path(tmp_dir)
            other_json = tmp / "normal_config.json"
            other_json.write_text('{"safe": true}', encoding="utf-8")

            with (
                patch.object(web_app, "_CONFIG_PATH", tmp / "cfg.json"),
                patch.object(web_app, "_ENV_PATH", tmp / ".env"),
            ):
                self.assertEqual(
                    authed_client.post("/api/open-folder", json={"path": r"\\attacker.com\share"}).status_code,
                    400,
                )
                self.assertEqual(
                    authed_client.get("/api/video-stream", params={"path": r"\\attacker.com\share\a.mp4"}).status_code,
                    400,
                )
                self.assertEqual(
                    authed_client.put(
                        "/api/highlights",
                        json={"highlights_json_path": str(other_json), "highlights": []},
                    ).status_code,
                    400,
                )
                self.assertEqual(
                    authed_client.post(
                        "/api/config",
                        json={"base_url": "file:///etc/passwd"},
                    ).status_code,
                    400,
                )

                # .env 写入自动剥离换行符防多行注入
                env_file = tmp / ".env"
                write_env_settings(env_file, {"ASR_HOTWORDS": "Shylily\nEVIL_KEY=pwned\r\nKEKW"})
                env_text = env_file.read_text(encoding="utf-8")
                self.assertNotIn("\nEVIL_KEY=pwned", env_text)

        # 5. 日志密钥脱敏校验
        leaked = "HTTP 401 Authorization: Bearer sk-live1234567890abcdef failed for ghp_1234567890abcdefghij"
        redacted = redact_secrets_in_text(leaked)
        self.assertNotIn("sk-live1234567890abcdef", redacted)
        self.assertNotIn("ghp_1234567890abcdefghij", redacted)
        self.assertIn("REDACTED", redacted)

    def test_v091_redteam_hardening_media_bins_and_demo_save(self) -> None:
        """测试 7：验证红队终审全部 P0/P1 安全加固、媒体池虚拟文件夹持久化 API 与 Demo 保存不报 500。"""
        authed_client = TestClient(web_app.app, headers={"X-Solacut-Token": web_app._SESSION_TOKEN})

        # 1. 红队边缘绕过全部封死：空/畸形 Host、same-site 跨端口、默认端口错配 Origin、混合斜杠 UNC、stem 穿越
        self.assertEqual(authed_client.get("/api/config", headers={"Host": ""}).status_code, 403)
        self.assertEqual(authed_client.get("/api/config", headers={"Sec-Fetch-Site": "same-site"}).status_code, 403)
        self.assertEqual(
            authed_client.get(
                "/api/config",
                headers={"Host": "127.0.0.1:8765", "Origin": "http://localhost"},
            ).status_code,
            403,
        )
        self.assertEqual(
            authed_client.get("/api/videos", params={"input_dir": r"/\attacker.com\share"}).status_code,
            400,
        )
        self.assertEqual(
            authed_client.get("/api/highlights", params={"stem": "../secret"}).status_code,
            400,
        )
        self.assertEqual(
            authed_client.get("/api/waveform", params={"stem": r"C:\evil"}).status_code,
            400,
        )

        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp = Path(tmp_dir)
            outside_target = tmp / "do_not_overwrite.txt"
            outside_target.write_text("SAFE_ORIGINAL", encoding="utf-8")

            sub_dir = tmp / "work"
            sub_dir.mkdir()
            hl_file = sub_dir / "demo_highlights.json"
            # 故意省略 elapsed_sec 且把 review_md_path 篡改为外部敏感文件，验证既不 500 也不覆写外部文件
            hl_file.write_text(
                json.dumps(
                    {
                        "video_path": str(sub_dir / "demo.mp4"),
                        "review_md_path": str(outside_target),
                        "model": "demo-preset",
                        "total_duration_sec": 30.0,
                        "total_duration_str": "00:30",
                        "highlight_count": 1,
                        "selection_plan": {"duration_tier": "示例", "target_count": 1, "min_clip_sec": 8, "max_clip_sec": 15},
                        "highlights": [
                            {
                                "rank": 1,
                                "selected": True,
                                "start_sec": 2.0,
                                "end_sec": 10.0,
                                "score": 9.2,
                                "title_zh": "测试标题",
                                "summary_zh": "测试梗概",
                            }
                        ],
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

            cfg_path = tmp / "cfg.json"
            cfg_path.write_text(
                json.dumps({"input_dir": str(sub_dir), "output_dir": str(sub_dir)}),
                encoding="utf-8",
            )
            with (
                patch.object(web_app, "_CONFIG_PATH", cfg_path),
                patch.object(web_app, "_ENV_PATH", tmp / ".env"),
                patch.object(web_app, "_MEDIA_BINS_PATH", tmp / ".media_bins.json"),
            ):
                # 2. Demo 缺失 elapsed_sec 保存必返 200，且被篡改的外部 review_md_path 丝毫不受影响
                save_res = authed_client.put(
                    "/api/highlights",
                    json={
                        "highlights_json_path": str(hl_file),
                        "highlights": [
                            {
                                "rank": 1,
                                "selected": True,
                                "start_sec": 2.5,
                                "end_sec": 10.5,
                                "score": 9.2,
                                "title_zh": "改过的标题",
                                "summary_zh": "测试梗概",
                            }
                        ],
                    },
                )
                self.assertEqual(save_res.status_code, 200)
                self.assertEqual(outside_target.read_text(encoding="utf-8"), "SAFE_ORIGINAL")
                self.assertTrue((sub_dir / "demo_review.md").is_file())

                # 3. 媒体池虚拟文件夹 API 读写与非法色标/孤儿引用清洗
                put_bins = authed_client.put(
                    "/api/media-bins",
                    json={
                        "folders": [{"id": "bin_1", "name": "本周爆款", "color": "#f59e0b"}],
                        "assignments": {"a.mp4": "bin_1", "b.mp4": "nonexistent_bin"},
                        "tags": {"a.mp4": "red", "b.mp4": "invalid_color"},
                    },
                )
                self.assertEqual(put_bins.status_code, 200)
                bins_data = authed_client.get("/api/media-bins").json()
                self.assertEqual(len(bins_data["folders"]), 1)
                self.assertEqual(bins_data["assignments"], {"a.mp4": "bin_1"})
                self.assertEqual(bins_data["tags"], {"a.mp4": "red"})

    def test_subtitle_modes_srt_and_ass_order_and_legacy_aliases(self) -> None:
        """测试 8：验证 5 种字幕模式（含 zh_only/en_only 兼容别名）在 SRT/ASS 中的行序、内容与 none 无产物。"""
        from src.step4_cut import (
            generate_clip_ass,
            generate_clip_srt,
            normalize_subtitle_lang_mode,
        )

        # 1. 归一化别名映射与非法值拦截
        self.assertEqual(normalize_subtitle_lang_mode("bilingual"), "bilingual")
        self.assertEqual(normalize_subtitle_lang_mode("bilingual_src_top"), "bilingual_src_top")
        self.assertEqual(normalize_subtitle_lang_mode("bilingual_rev"), "bilingual_src_top")
        self.assertEqual(normalize_subtitle_lang_mode("target_only"), "target_only")
        self.assertEqual(normalize_subtitle_lang_mode("zh_only"), "target_only")
        self.assertEqual(normalize_subtitle_lang_mode("source_only"), "source_only")
        self.assertEqual(normalize_subtitle_lang_mode("en_only"), "source_only")
        self.assertEqual(normalize_subtitle_lang_mode("none"), "none")
        with self.assertRaises(ValueError):
            normalize_subtitle_lang_mode("invalid_mode_xyz")

        subs_en_ja = [
            {
                "start_sec": 10.5,
                "end_sec": 13.5,
                "text_src": "Watch this crazy clutch!",
                "text_tgt": "この神クラッチを見ろ！",
            }
        ]
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp = Path(tmp_dir)

            # 2. bilingual：译文（日）上、原文（英）下
            srt_bi = tmp / "bi.srt"
            ass_bi = tmp / "bi.ass"
            self.assertEqual(
                generate_clip_srt([], 10.0, 15.0, srt_bi, "bilingual", subs_en_ja, "en", "ja"),
                1,
            )
            bi_srt_txt = srt_bi.read_text(encoding="utf-8")
            self.assertLess(bi_srt_txt.index("この神クラッチを見ろ！"), bi_srt_txt.index("Watch this crazy clutch!"))

            self.assertEqual(
                generate_clip_ass(
                    [], 10.0, 15.0, ass_bi, 1080, 1920, "bilingual", "神プレイ集", "vertical", subs_en_ja, "en", "ja"
                ),
                1,
            )
            bi_ass_txt = ass_bi.read_text(encoding="utf-8")
            self.assertIn("Style: Title,Yu Gothic UI,", bi_ass_txt)
            self.assertIn("Style: ZH,Yu Gothic UI,", bi_ass_txt)
            self.assertIn("Style: EN,Segoe UI,", bi_ass_txt)
            self.assertIn(",ZH,,0,0,0,,この神クラッチを見ろ！", bi_ass_txt)
            self.assertIn(",EN,,0,0,0,,Watch this crazy clutch!", bi_ass_txt)

            # 3. bilingual_src_top：原文（英）上、译文（日）下，但顶部 Title 仍锁定目标语（日）零缺字字体
            srt_rev = tmp / "rev.srt"
            ass_rev = tmp / "rev.ass"
            self.assertEqual(
                generate_clip_srt([], 10.0, 15.0, srt_rev, "bilingual_src_top", subs_en_ja, "en", "ja"),
                1,
            )
            rev_srt_txt = srt_rev.read_text(encoding="utf-8")
            self.assertLess(rev_srt_txt.index("Watch this crazy clutch!"), rev_srt_txt.index("この神クラッチを見ろ！"))

            self.assertEqual(
                generate_clip_ass(
                    [], 10.0, 15.0, ass_rev, 1080, 1920, "bilingual_src_top", "神プレイ集", "vertical", subs_en_ja, "en", "ja"
                ),
                1,
            )
            rev_ass_txt = ass_rev.read_text(encoding="utf-8")
            self.assertIn("Style: Title,Yu Gothic UI,", rev_ass_txt)
            self.assertIn("Style: ZH,Segoe UI,", rev_ass_txt)
            self.assertIn("Style: EN,Yu Gothic UI,", rev_ass_txt)
            self.assertIn(",ZH,,0,0,0,,Watch this crazy clutch!", rev_ass_txt)
            self.assertIn(",EN,,0,0,0,,この神クラッチを見ろ！", rev_ass_txt)

            # 4. target_only 与历史别名 zh_only：仅输出目标语（此处为日语）
            for mode_name in ("target_only", "zh_only"):
                srt_tgt = tmp / f"{mode_name}.srt"
                ass_tgt = tmp / f"{mode_name}.ass"
                self.assertEqual(
                    generate_clip_srt([], 10.0, 15.0, srt_tgt, mode_name, subs_en_ja, "en", "ja"),
                    1,
                )
                txt = srt_tgt.read_text(encoding="utf-8")
                self.assertIn("この神クラッチを見ろ！", txt)
                self.assertNotIn("Watch this crazy clutch!", txt)

                self.assertEqual(
                    generate_clip_ass([], 10.0, 15.0, ass_tgt, 1080, 1920, mode_name, None, "vertical", subs_en_ja, "en", "ja"),
                    1,
                )
                ass_txt = ass_tgt.read_text(encoding="utf-8")
                self.assertIn("この神クラッチを見ろ！", ass_txt)
                self.assertNotIn("Watch this crazy clutch!", ass_txt)

            # 5. source_only 与历史别名 en_only：仅输出源语（此处为英语）
            for mode_name in ("source_only", "en_only"):
                srt_src = tmp / f"{mode_name}.srt"
                ass_src = tmp / f"{mode_name}.ass"
                self.assertEqual(
                    generate_clip_srt([], 10.0, 15.0, srt_src, mode_name, subs_en_ja, "en", "ja"),
                    1,
                )
                txt = srt_src.read_text(encoding="utf-8")
                self.assertIn("Watch this crazy clutch!", txt)
                self.assertNotIn("この神クラッチを見ろ！", txt)

                self.assertEqual(
                    generate_clip_ass([], 10.0, 15.0, ass_src, 1080, 1920, mode_name, None, "vertical", subs_en_ja, "en", "ja"),
                    1,
                )
                ass_txt = ass_src.read_text(encoding="utf-8")
                self.assertIn("Watch this crazy clutch!", ass_txt)
                self.assertNotIn("この神クラッチを見ろ！", ass_txt)

            # 6. none：不生成 SRT 也不生成 ASS，返回 0
            srt_none = tmp / "none.srt"
            ass_none = tmp / "none.ass"
            self.assertEqual(generate_clip_srt([], 10.0, 15.0, srt_none, "none", subs_en_ja, "en", "ja"), 0)
            self.assertEqual(generate_clip_ass([], 10.0, 15.0, ass_none, 1080, 1920, "none", None, "vertical", subs_en_ja, "en", "ja"), 0)
            self.assertFalse(srt_none.exists())
            self.assertFalse(ass_none.exists())

            # 7. 同语种精修（en -> en）不重复叠两行相同文本
            subs_same = [{"start_sec": 10.5, "end_sec": 13.5, "text_src": "Clean ace!", "text_tgt": "Clean ace!"}]
            srt_same = tmp / "same.srt"
            self.assertEqual(generate_clip_srt([], 10.0, 15.0, srt_same, "bilingual", subs_same, "en", "en"), 1)
            self.assertEqual(srt_same.read_text(encoding="utf-8").count("Clean ace!"), 1)

    def test_six_languages_ass_fonts_and_step3_non_zh_quality_check(self) -> None:
        """测试 9：验证 6 语种 ASS 零缺字字体、title_tgt 优先级及 step3_llm 非中文目标语质检零误杀。"""
        from src.step3_llm import (
            _detect_subtitle_quality_issue,
            _is_subtitle_untranslated,
            _validate_resegmented_subtitles,
            resolve_subtitle_language_pair,
        )
        from src.step4_cut import (
            _resolve_ass_font_for_lang,
            _resolve_highlight_title,
            generate_clip_ass,
        )

        # 1. 6 语种 ASS 零缺字字体映射
        expected_fonts = {
            "zh": "Microsoft YaHei",
            "ja": "Yu Gothic UI",
            "ko": "Malgun Gothic",
            "ru": "Segoe UI",
            "es": "Segoe UI",
            "en": "Segoe UI",
        }
        for lang, expected_font in expected_fonts.items():
            self.assertEqual(_resolve_ass_font_for_lang(lang), expected_font)

        # 验证真实 ASS 文件写入了对应零缺字字体与 title_tgt 优先级
        self.assertEqual(
            _resolve_highlight_title({"title_tgt": "Топ момент", "title_zh": "旧标题", "title_en": "Old"}, "ru", 1),
            "Топ момент",
        )
        self.assertEqual(
            _resolve_highlight_title({"title_zh": "中文旧标题", "title_en": "English Title"}, "en", 1),
            "English Title",
        )

        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp = Path(tmp_dir)
            for lang, expected_font in expected_fonts.items():
                ass_p = tmp / f"font_{lang}.ass"
                subs = [{"start_sec": 1.0, "end_sec": 3.0, "text_src": "Source line", "text_tgt": f"Target {lang}"}]
                generate_clip_ass([], 0.0, 5.0, ass_p, 1080, 1920, "bilingual", f"Banner {lang}", "vertical", subs, "en", lang)
                content = ass_p.read_text(encoding="utf-8")
                self.assertIn(f"Style: Title,{expected_font},", content)
                self.assertIn(f"Style: ZH,{expected_font},", content)

        # 2. 同语种精修允许保留 src == tgt
        src_c, tgt_c, pair_c = resolve_subtitle_language_pair("en", "en", "Let's go!")
        self.assertEqual((src_c, tgt_c, pair_c), ("en", "en", "en_en"))

        # 3. 5 个非中文目标语（ja/ko/ru/es/en）质检绝不因无中文字符误杀，且纯 text_tgt/text_src 可通过校验并补齐兼容字段
        non_zh_cases = [
            ("en", "ja", "No way he actually hit that shot!", "マジであのショット当てたのかよ！"),
            ("en", "ko", "No way he actually hit that shot!", "설마 저 샷을 진짜 맞췄다고?!"),
            ("en", "ru", "No way he actually hit that shot!", "Да ладно, он реально попал этот выстрел!"),
            ("en", "es", "No way he actually hit that shot!", "¡No puede ser que realmente metió ese tiro!"),
            ("zh", "en", "不是吧哥们这枪居然真打中了！", "No way dude, that shot actually landed!"),
        ]
        for src_lang, tgt_lang, src_text, tgt_text in non_zh_cases:
            raw_lines = [{"start_sec": 10.0, "end_sec": 13.0, "text": src_text}]
            # 故意只提供规范字段 text_src / text_tgt，验证 _validate_resegmented_subtitles 不清空并回填兼容字段
            model_subs = [{"start_sec": 10.0, "end_sec": 13.0, "text_src": src_text, "text_tgt": tgt_text}]
            validated = _validate_resegmented_subtitles(
                model_subs, 9.5, 13.5, raw_lines, source_language=src_lang, target_language=tgt_lang
            )
            self.assertIsNotNone(validated, f"{src_lang}->{tgt_lang} should pass validation")
            assert validated is not None
            self.assertEqual(len(validated), 1)
            self.assertEqual(validated[0]["text_tgt"], tgt_text)
            self.assertEqual(validated[0]["text_src"], src_text)
            self.assertFalse(
                _is_subtitle_untranslated(validated[0], source_language=src_lang, target_language=tgt_lang),
                f"{src_lang}->{tgt_lang} falsely marked untranslated",
            )
            self.assertIsNone(
                _detect_subtitle_quality_issue(raw_lines, validated, source_language=src_lang, target_language=tgt_lang),
                f"{src_lang}->{tgt_lang} falsely flagged quality issue",
            )

    def test_i18n_static_asset_and_index_default_english_controls(self) -> None:
        """测试 10：验证 /static/i18n.js 静态可访问、默认英语启动与 6 语种/CC 控件完整挂载。"""
        raw_client = TestClient(web_app.app)

        # 1. /static/i18n.js 静态资产可读且含 6 语种与默认 en
        js_res = raw_client.get("/static/i18n.js")
        self.assertEqual(js_res.status_code, 200)
        js_body = js_res.text
        self.assertIn("SolacutI18n", js_body)
        self.assertIn('STORAGE_KEY = "solacut_ui_lang"', js_body)
        self.assertIn('SUPPORTED_LANGS = ["en", "zh", "ja", "ko", "ru", "es"]', js_body)
        self.assertIn('return "en";', js_body)

        # 2. 首页 GET / 默认英语 <html lang="en">，挂载语言切换器与专业 CC 弹窗菜单，零旧中英写死残留
        idx_res = raw_client.get("/")
        self.assertEqual(idx_res.status_code, 200)
        html = idx_res.text
        self.assertIn('<html lang="en"', html)
        self.assertIn("/static/i18n.js", html)
        self.assertIn('id="btn-ui-lang"', html)
        self.assertIn('id="ui-lang-menu"', html)
        self.assertIn('id="cfg-ui-lang"', html)
        self.assertIn('id="btn-cc-menu"', html)
        self.assertIn('id="cc-menu"', html)
        for mode_val in ('value="bilingual"', 'value="bilingual_src_top"', 'value="target_only"', 'value="source_only"', 'value="none"'):
            self.assertIn(mode_val, html)
        for legacy_cn in ("仅简体中文 (ZH)", "仅英语 (EN)", "英中双语", "中英双语"):
            self.assertNotIn(legacy_cn, html)

    def test_web_api_six_langs_and_five_subtitle_modes_validation(self) -> None:
        """测试 11：验证 Web API 放行 6 语种与 5 种字幕模式（含旧别名）、拦截非法值并规范化保存 title_tgt。"""
        authed_client = TestClient(web_app.app, headers={"X-Solacut-Token": web_app._SESSION_TOKEN})

        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp = Path(tmp_dir)
            video_file = tmp / "sample.mp4"
            video_file.write_bytes(b"fake-video")
            hl_file = tmp / "sample_highlights.json"
            hl_file.write_text(
                json.dumps(
                    {
                        "video_path": str(video_file),
                        "source_language": "en",
                        "target_language": "ja",
                        "model": "test",
                        "total_duration_sec": 30.0,
                        "total_duration_str": "00:30",
                        "highlight_count": 1,
                        "highlights": [
                            {
                                "rank": 1,
                                "selected": True,
                                "start_sec": 1.0,
                                "end_sec": 8.0,
                                "score": 9.0,
                                "title_tgt": "神プレイ",
                                "title_src": "Insane Play",
                                "title_zh": "神プレイ",
                                "title_en": "Insane Play",
                            }
                        ],
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

            submitted_tasks: list[dict[str, object]] = []

            def _fake_submit(task_type: str, title: str, worker: object) -> dict[str, str]:
                submitted_tasks.append({"type": task_type, "title": title})
                return {"task_id": f"fake_{len(submitted_tasks)}", "title": title, "status": "queued"}

            cfg_path = tmp / "cfg.json"
            cfg_path.write_text(
                json.dumps({"input_dir": str(tmp), "output_dir": str(tmp)}),
                encoding="utf-8",
            )
            with (
                patch.object(web_app._TASK_MANAGER, "submit", side_effect=_fake_submit),
                patch.object(web_app, "_CONFIG_PATH", cfg_path),
                patch.object(web_app, "_ENV_PATH", tmp / ".env"),
            ):
                # 1. /api/tasks/export 放行全部 5 规范模式 + subtitle_mode 字段 + 旧别名 zh_only / en_only
                for payload in (
                    {"subtitle_lang": "bilingual"},
                    {"subtitle_lang": "bilingual_src_top"},
                    {"subtitle_lang": "target_only"},
                    {"subtitle_lang": "source_only"},
                    {"subtitle_lang": "none"},
                    {"subtitle_lang": "zh_only"},
                    {"subtitle_lang": "en_only"},
                    {"subtitle_mode": "bilingual_src_top"},
                ):
                    res = authed_client.post(
                        "/api/tasks/export",
                        json={"highlights_json_path": str(hl_file), "output_dir": str(tmp), **payload},
                    )
                    self.assertEqual(res.status_code, 200, f"Export rejected valid payload: {payload} -> {res.text}")

                # 拦截非法字幕模式
                bad_exp = authed_client.post(
                    "/api/tasks/export",
                    json={"highlights_json_path": str(hl_file), "output_dir": str(tmp), "subtitle_lang": "alien_mode"},
                )
                self.assertEqual(bad_exp.status_code, 400)

                # 2. /api/tasks/retranslate 放行全部 6 语种组合（含同语种 en->en）并拒绝非法语种
                for src_l, tgt_l in (("en", "ja"), ("ko", "ru"), ("es", "zh"), ("zh", "en"), ("en", "en")):
                    res = authed_client.post(
                        "/api/tasks/retranslate",
                        json={
                            "highlights_json_path": str(hl_file),
                            "source_language": src_l,
                            "target_language": tgt_l,
                        },
                    )
                    self.assertEqual(res.status_code, 200, f"Retranslate rejected {src_l}->{tgt_l}: {res.text}")

                bad_ret = authed_client.post(
                    "/api/tasks/retranslate",
                    json={"highlights_json_path": str(hl_file), "source_language": "en", "target_language": "klingon"},
                )
                self.assertEqual(bad_ret.status_code, 400)

                # 3. /api/tasks/analyze 放行 auto + 6 语种组合并拒绝非法语种
                res_an = authed_client.post(
                    "/api/tasks/analyze",
                    json={
                        "video_paths": [str(video_file)],
                        "output_dir": str(tmp),
                        "language": "auto",
                        "target_language": "ko",
                    },
                )
                self.assertEqual(res_an.status_code, 200, res_an.text)
                bad_an = authed_client.post(
                    "/api/tasks/analyze",
                    json={
                        "video_paths": [str(video_file)],
                        "output_dir": str(tmp),
                        "language": "xx",
                        "target_language": "ja",
                    },
                )
                self.assertEqual(bad_an.status_code, 400)

                # 4. PUT /api/highlights 持久化并规范化补齐 title_tgt / title_src
                put_res = authed_client.put(
                    "/api/highlights",
                    json={
                        "highlights_json_path": str(hl_file),
                        "highlights": [
                            {
                                "rank": 1,
                                "selected": True,
                                "start_sec": 1.0,
                                "end_sec": 8.0,
                                "score": 9.5,
                                "title_tgt": "最高の瞬間",
                                "title_src": "Best Moment",
                            }
                        ],
                    },
                )
                self.assertEqual(put_res.status_code, 200, put_res.text)
                saved = json.loads(hl_file.read_text(encoding="utf-8"))
                self.assertEqual(saved["highlights"][0]["title_tgt"], "最高の瞬間")
                self.assertEqual(saved["highlights"][0]["title_src"], "Best Moment")

    def test_chapters_transcript_and_manual_clips_api(self) -> None:
        """测试 12：验证 /api/chapters、/api/transcript（全片/搜索/±30s上下文）与零 AI 手动新建片段闭环。"""
        ffmpeg = check_ffmpeg_available()
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp = Path(tmp_dir)
            in_dir = tmp / "in"
            out_dir = tmp / "out"
            in_dir.mkdir()
            out_dir.mkdir()

            # 1. 用 FFmpeg 合成带 2 个 OBS 章节的真实 4 秒 MP4
            meta_file = tmp / "chapters.ffmeta"
            meta_file.write_text(
                ";FFMETADATA1\n"
                "[CHAPTER]\nTIMEBASE=1/1000\nSTART=0\nEND=2000\ntitle=Intro Warmup\n"
                "[CHAPTER]\nTIMEBASE=1/1000\nSTART=2000\nEND=4000\ntitle=Boss Clutch Kill\n",
                encoding="utf-8",
            )
            video_file = in_dir / "obs_vod.mp4"
            subprocess.run(
                [
                    ffmpeg,
                    "-y",
                    "-f",
                    "lavfi",
                    "-i",
                    "color=c=black:s=320x180:r=25:d=4",
                    "-f",
                    "lavfi",
                    "-i",
                    "sine=frequency=440:duration=4",
                    "-i",
                    str(meta_file),
                    "-map_metadata",
                    "2",
                    "-c:v",
                    "libx264",
                    "-pix_fmt",
                    "yuv420p",
                    "-c:a",
                    "aac",
                    str(video_file),
                ],
                check=True,
                capture_output=True,
            )

            not_video = in_dir / "readme.txt"
            not_video.write_text("not a video", encoding="utf-8")

            cfg_path = tmp / "cfg.json"
            cfg_path.write_text(
                json.dumps({"input_dir": str(in_dir), "output_dir": str(out_dir)}),
                encoding="utf-8",
            )
            with (
                patch.object(web_app, "_CONFIG_PATH", cfg_path),
                patch.object(web_app, "_ENV_PATH", tmp / ".env"),
            ):
                client = TestClient(web_app.app, headers={"X-Solacut-Token": web_app._SESSION_TOKEN})

                # 2. /api/chapters 正常提取 + 安全边界拦截
                ch_resp = client.get("/api/chapters", params={"video_path": str(video_file)})
                self.assertEqual(ch_resp.status_code, 200, ch_resp.text)
                ch_data = ch_resp.json()
                self.assertEqual(ch_data["count"], 2)
                self.assertEqual(ch_data["chapters"][1]["title"], "Boss Clutch Kill")

                self.assertEqual(
                    client.get("/api/chapters", params={"video_path": r"\\evil\share\v.mp4"}).status_code,
                    400,
                )
                self.assertEqual(
                    client.get("/api/chapters", params={"video_path": str(not_video)}).status_code,
                    404,
                )

                # 3. /api/transcript 无逐字稿时返回 200 + exists=False（绝不 404）
                tr_missing = client.get("/api/transcript", params={"stem": "obs_vod", "output_dir": str(out_dir)})
                self.assertEqual(tr_missing.status_code, 200)
                self.assertFalse(tr_missing.json()["exists"])
                self.assertEqual(tr_missing.json()["segments"], [])

                # 非法 stem 穿越拦截
                self.assertEqual(
                    client.get("/api/transcript", params={"stem": "../secret", "output_dir": str(out_dir)}).status_code,
                    400,
                )

                # 写入逐字稿并验证全片、关键词检索与 ±30s 上下文过滤
                tr_file = out_dir / "obs_vod_transcript.json"
                tr_file.write_text(
                    json.dumps(
                        {
                            "language": "en",
                            "total_duration_sec": 120.0,
                            "segments": [
                                {"start_sec": 1.0, "end_sec": 3.5, "text": "Warming up in lobby"},
                                {"start_sec": 42.0, "end_sec": 45.0, "text": "Wait he is joking right?"},
                                {"start_sec": 55.0, "end_sec": 58.0, "text": "Insane BOSS clutch kill!"},
                                {"start_sec": 105.0, "end_sec": 110.0, "text": "GG see you tomorrow"},
                            ],
                        },
                        ensure_ascii=False,
                    ),
                    encoding="utf-8",
                )

                tr_all = client.get("/api/transcript", params={"stem": "obs_vod", "output_dir": str(out_dir)}).json()
                self.assertTrue(tr_all["exists"])
                self.assertEqual(tr_all["total_count"], 4)
                self.assertEqual(set(tr_all["segments"][0].keys()), {"id", "start", "end", "text"})

                tr_q = client.get(
                    "/api/transcript",
                    params={"stem": "obs_vod", "output_dir": str(out_dir), "q": "boss clutch"},
                ).json()
                self.assertEqual(tr_q["matched_count"], 1)
                self.assertIn("BOSS", tr_q["segments"][0]["text"])

                tr_ctx = client.get(
                    "/api/transcript",
                    params={"stem": "obs_vod", "output_dir": str(out_dir), "around_sec": 50.0, "window_sec": 30.0},
                ).json()
                self.assertEqual(tr_ctx["matched_count"], 2)
                self.assertEqual([s["start"] for s in tr_ctx["segments"]], [42.0, 55.0])

                # 4. 零 AI 粗筛前置，直接 POST /api/highlights/create-manual 创建高光
                create_resp = client.post(
                    "/api/highlights/create-manual",
                    json={
                        "video_path": str(video_file),
                        "output_dir": str(out_dir),
                        "items": [
                            {
                                "start_sec": 40.0,
                                "end_sec": 60.0,
                                "title_tgt": "Manual Boss Clutch",
                                "source": "transcript",
                            }
                        ],
                    },
                )
                self.assertEqual(create_resp.status_code, 200, create_resp.text)
                created = create_resp.json()
                self.assertEqual(created["added_ranks"], [1])
                self.assertEqual(created["reviewed_count"], 1)
                self.assertEqual(created["highlights"][0]["review_status"], "reviewed")
                # 红队 P0-03/P1-3：手动片段默认 score=None（绝不伪造 8.5），且跨语种自动截取原片字幕绝不将英文错填入 text_tgt/text_zh！
                self.assertIsNone(created["highlights"][0]["score"])
                self.assertGreaterEqual(len(created["highlights"][0]["subtitles"]), 2)
                for sub_row in created["highlights"][0]["subtitles"]:
                    self.assertTrue(sub_row["text_src"])
                    self.assertEqual(sub_row["text_tgt"], "")
                    self.assertEqual(sub_row["text_zh"], "")
                    self.assertEqual(sub_row["text_en"], sub_row["text_src"])
                self.assertTrue((out_dir / "obs_vod_highlights.json").is_file())
                self.assertTrue((out_dir / "obs_vod_review.md").is_file())

    def test_chat_radar_import_heatmap_security_and_dead_air_diagnostics(self) -> None:
        """测试 13：验证 /api/chat/import、/api/chat/heatmap、非法扩展名与 XXE 拦截，以及 /api/diagnostics 透出 dead_air。"""
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp = Path(tmp_dir)
            out_dir = tmp / "out"
            out_dir.mkdir()

            # 准备 audio_peaks.json（含 dead_air_segments 与 peak_segments）
            peaks_file = out_dir / "stream_chat_audio_peaks.json"
            peaks_file.write_text(
                json.dumps(
                    {
                        "total_duration_sec": 180.0,
                        "total_duration_str": "03:00",
                        "rms_threshold": 0.08,
                        "segment_count": 1,
                        "peak_segments": [{"start_sec": 58.0, "end_sec": 64.0, "peak_rms": 0.35, "mean_rms": 0.22}],
                        "dead_air_count": 1,
                        "dead_air_total_sec": 24.5,
                        "dead_air_segments": [{"start_sec": 100.0, "end_sec": 124.5, "duration_sec": 24.5}],
                    }
                ),
                encoding="utf-8",
            )

            # 准备合法 Twitch JSON 弹幕（60s 处高频刷 OMEGALUL）
            comments = []
            for sec in range(0, 180, 10):
                comments.append({"content_offset_seconds": float(sec), "message": {"body": "hi"}, "commenter": {"display_name": "u"}})
            for idx in range(18):
                comments.append(
                    {
                        "content_offset_seconds": 60.0 + idx * 0.2,
                        "message": {"body": "OMEGALUL INSANE CLUTCH"},
                        "commenter": {"display_name": f"viewer{idx}"},
                    }
                )
            twitch_json = tmp / "vod_chat.json"
            twitch_json.write_text(json.dumps({"comments": comments, "video": {"length": 180}}), encoding="utf-8")

            # 非法扩展名与 XXE 恶意 XML
            bad_ext = tmp / "chat.txt"
            bad_ext.write_text("hello", encoding="utf-8")
            xxe_xml = tmp / "evil_xxe.xml"
            xxe_xml.write_text(
                '<?xml version="1.0"?><!DOCTYPE foo [<!ENTITY xxe SYSTEM "file:///C:/Windows/win.ini">]><i><d p="1,1,25">&xxe;</d></i>',
                encoding="utf-8",
            )

            cfg_path = tmp / "cfg.json"
            cfg_path.write_text(
                json.dumps({"input_dir": str(tmp), "output_dir": str(out_dir)}),
                encoding="utf-8",
            )
            with (
                patch.object(web_app, "_CONFIG_PATH", cfg_path),
                patch.object(web_app, "_ENV_PATH", tmp / ".env"),
            ):
                client = TestClient(web_app.app, headers={"X-Solacut-Token": web_app._SESSION_TOKEN})

                # 1. 未导入时 /api/chat/heatmap 返回 exists=False
                empty_hm = client.get("/api/chat/heatmap", params={"stem": "stream_chat", "output_dir": str(out_dir)})
                self.assertEqual(empty_hm.status_code, 200)
                self.assertFalse(empty_hm.json()["exists"])

                # 2. 安全拦截：非法扩展名、XXE 注入、UNC 路径
                self.assertEqual(
                    client.post(
                        "/api/chat/import",
                        json={"stem": "stream_chat", "chat_path": str(bad_ext), "output_dir": str(out_dir)},
                    ).status_code,
                    400,
                )
                xxe_resp = client.post(
                    "/api/chat/import",
                    json={"stem": "stream_chat", "chat_path": str(xxe_xml), "output_dir": str(out_dir)},
                )
                self.assertEqual(xxe_resp.status_code, 400)
                self.assertEqual(
                    client.post(
                        "/api/chat/import",
                        json={"stem": "stream_chat", "chat_path": r"\\evil\share\chat.json", "output_dir": str(out_dir)},
                    ).status_code,
                    400,
                )

                # 3. 合法导入：生成热力曲线、chat_peaks 与 audio+chat 融合峰，且不泄露原始绝对路径
                imp = client.post(
                    "/api/chat/import",
                    json={"stem": "stream_chat", "chat_path": str(twitch_json), "output_dir": str(out_dir)},
                )
                self.assertEqual(imp.status_code, 200, imp.text)
                imp_data = imp.json()
                self.assertEqual(imp_data["format"], "twitch_json")
                self.assertGreaterEqual(len(imp_data["chat_peaks"]), 1)
                self.assertTrue(any(p["fusion_source"] == "audio+chat" for p in imp_data["fusion_peaks"]))
                radar_file = out_dir / "stream_chat_chat_radar.json"
                db_file = out_dir / "stream_chat_chat_messages.db"
                self.assertTrue(radar_file.is_file())
                self.assertTrue(db_file.is_file())
                radar_raw_text = radar_file.read_text(encoding="utf-8")
                self.assertNotIn(str(tmp), radar_raw_text)
                # 红队 P0-5 拆离存储：*_chat_radar.json 彻底不再保存 messages 巨无霸数组
                self.assertNotIn("messages", json.loads(radar_raw_text))

                # 4. 梗词检索 /api/chat/heatmap?q=OMEGALUL
                hm_q = client.get(
                    "/api/chat/heatmap",
                    params={"stem": "stream_chat", "output_dir": str(out_dir), "q": "OMEGALUL"},
                )
                self.assertEqual(hm_q.status_code, 200)
                hm_data = hm_q.json()
                self.assertTrue(hm_data["exists"])
                self.assertEqual(hm_data["search"]["match_count"], 18)

                # 5. /api/diagnostics 原样透出 dead_air_segments / total / count
                diag_res = client.get("/api/diagnostics", params={"stem": "stream_chat", "output_dir": str(out_dir)})
                self.assertEqual(diag_res.status_code, 200)
                diag_data = diag_res.json()
                self.assertEqual(diag_data["dead_air_count"], 1)
                self.assertAlmostEqual(diag_data["dead_air_total_sec"], 24.5, places=1)
                self.assertEqual(len(diag_data["dead_air_segments"]), 1)

    def test_review_status_persistence_nle_only_and_only_reviewed_export_gate(self) -> None:
        """测试 14：验证 review_status 持久化、only_reviewed 导出门禁、nle_only 零转码导出与首页去 Slop 默认配置。"""
        ffmpeg = check_ffmpeg_available()
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp = Path(tmp_dir)
            video_file = tmp / "nle_gate.mp4"
            subprocess.run(
                [
                    ffmpeg,
                    "-y",
                    "-f",
                    "lavfi",
                    "-i",
                    "color=c=black:s=320x180:r=30:d=6",
                    "-f",
                    "lavfi",
                    "-i",
                    "sine=frequency=500:duration=6",
                    "-c:v",
                    "libx264",
                    "-pix_fmt",
                    "yuv420p",
                    "-c:a",
                    "aac",
                    str(video_file),
                ],
                check=True,
                capture_output=True,
            )

            hl_file = tmp / "nle_gate_highlights.json"
            hl_file.write_text(
                json.dumps(
                    {
                        "video_path": str(video_file),
                        "source_language": "en",
                        "target_language": "zh",
                        "model": "test",
                        "total_duration_sec": 6.0,
                        "total_duration_str": "00:06",
                        "highlight_count": 2,
                        "highlights": [
                            {
                                "rank": 1,
                                "selected": True,
                                "review_status": "draft",
                                "start_sec": 0.5,
                                "end_sec": 2.5,
                                "score": 9.1,
                                "title_tgt": "片段一",
                                "title_src": "Clip 1",
                                "subtitles": [{"start_sec": 0.8, "end_sec": 2.0, "text_src": "Nice!", "text_tgt": "漂亮！"}],
                            },
                            {
                                "rank": 2,
                                "selected": True,
                                "review_status": "draft",
                                "start_sec": 3.0,
                                "end_sec": 5.5,
                                "score": 8.4,
                                "title_tgt": "片段二",
                                "title_src": "Clip 2",
                                "subtitles": [],
                            },
                        ],
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

            cfg_path = tmp / "cfg.json"
            cfg_path.write_text(
                json.dumps({"input_dir": str(tmp), "output_dir": str(tmp)}),
                encoding="utf-8",
            )
            with (
                patch.object(web_app, "_CONFIG_PATH", cfg_path),
                patch.object(web_app, "_ENV_PATH", tmp / ".env"),
            ):
                client = TestClient(web_app.app, headers={"X-Solacut-Token": web_app._SESSION_TOKEN})

                # 1. 首页挂载全部新 UI 控件，且 #chk-title-banner 默认未勾选（去 AI Slop）
                idx_html = client.get("/").text
                for marker in (
                    'id="pane-transcript"',
                    'id="context-dialog"',
                    'id="chat-radar-popover"',
                    'id="btn-skip-dead-air"',
                    'id="chk-only-reviewed"',
                    'value="nle_only"',
                ):
                    self.assertIn(marker, idx_html)
                self.assertIn('<input type="checkbox" id="chk-title-banner">', idx_html)

                # 2. 导出非法 mode 拦截；全部 draft 时 only_reviewed=True 返回 400 门禁
                self.assertEqual(
                    client.post(
                        "/api/tasks/export",
                        json={"highlights_json_path": str(hl_file), "mode": "bad_mode"},
                    ).status_code,
                    400,
                )
                gate_block = client.post(
                    "/api/tasks/export",
                    json={"highlights_json_path": str(hl_file), "mode": "nle_only", "only_reviewed": True},
                )
                self.assertEqual(gate_block.status_code, 400)

                # 3. PUT /api/highlights 持久化 review_status 与 review_flags，并清洗非法 status
                hl_data = json.loads(hl_file.read_text(encoding="utf-8"))
                hl_data["highlights"][0]["review_status"] = "reviewed"
                hl_data["highlights"][0]["review_flags"] = ["high_idiom_density", "  "]
                hl_data["highlights"][1]["review_status"] = "invalid_status_xyz"
                put_resp = client.put(
                    "/api/highlights",
                    json={"highlights_json_path": str(hl_file), "highlights": hl_data["highlights"]},
                )
                self.assertEqual(put_resp.status_code, 200, put_resp.text)
                put_json = put_resp.json()
                self.assertEqual(put_json["reviewed_count"], 1)
                self.assertEqual(put_json["draft_count"], 1)
                self.assertEqual(put_json["highlights"][0]["review_status"], "reviewed")
                self.assertEqual(put_json["highlights"][0]["review_flags"], ["high_idiom_density"])
                self.assertEqual(put_json["highlights"][1]["review_status"], "draft")

                # 4. 校验通过门禁后，POST /api/tasks/export 接受 mode=nle_only + only_reviewed=True
                with patch.object(
                    web_app._TASK_MANAGER,
                    "submit",
                    return_value={"task_id": "t_nle", "status": "queued", "title": "nle"},
                ):
                    ok_exp = client.post(
                        "/api/tasks/export",
                        json={"highlights_json_path": str(hl_file), "mode": "nle_only", "only_reviewed": True},
                    )
                    self.assertEqual(ok_exp.status_code, 200, ok_exp.text)

                # 5. 端到端真实执行 pipeline.run_export_pipeline(mode='nle_only', only_reviewed=True)
                nle_out = tmp / "nle_out"
                nle_res = pipeline_mod.run_export_pipeline(
                    highlights_json_path=hl_file,
                    output_dir=nle_out,
                    mode="nle_only",
                    only_reviewed=True,
                )
                self.assertEqual(nle_res["mode"], "nle_only")
                self.assertTrue(nle_res["only_reviewed"])
                self.assertEqual(nle_res["exported_count"], 1)
                self.assertEqual([c["rank"] for c in nle_res["clips"]], [1])
                self.assertTrue(Path(nle_res["fcpxml_path"]).is_file())
                self.assertTrue(Path(nle_res["premiere_xml_path"]).is_file())
                self.assertTrue(Path(nle_res["markers_edl_path"]).is_file())
                self.assertTrue(Path(nle_res["markers_csv_path"]).is_file())
                self.assertEqual(list(nle_out.glob("*.mp4")), [])

    def test_second_round_redteam_web_security_locks_nan_and_build_release(self) -> None:
        """测试 15：第二批红队终极对抗验收——设备保留名、工作区隔离与白名单授权、NaN 防护、rank/ranks 门禁、弹窗锁与发布审计。"""
        import threading
        import time as time_mod
        from scripts import build_release

        ffmpeg = check_ffmpeg_available()
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp = Path(tmp_dir)
            in_dir = tmp / "ws_in"
            out_dir = tmp / "ws_out"
            outside_dir = tmp / "outside_secret"
            in_dir.mkdir()
            out_dir.mkdir()
            outside_dir.mkdir()

            # 工作区内与工作区外各放一个真实短视频与弹幕文件
            ws_video = in_dir / "ws_clip.mp4"
            outside_video = outside_dir / "external_4k.mp4"
            subprocess.run(
                [
                    ffmpeg,
                    "-y",
                    "-f",
                    "lavfi",
                    "-i",
                    "color=c=black:s=160x90:r=10:d=2",
                    "-c:v",
                    "libx264",
                    "-pix_fmt",
                    "yuv420p",
                    str(ws_video),
                ],
                check=True,
                capture_output=True,
            )
            outside_video.write_bytes(ws_video.read_bytes())

            outside_chat = outside_dir / "ext_chat.json"
            outside_chat.write_text(
                json.dumps([{"time_sec": 1.0, "text": "OMEGALUL", "author": "u1"}]),
                encoding="utf-8",
            )
            plain_file = tmp / "not_a_dir.txt"
            plain_file.write_text("file", encoding="utf-8")

            hl_file = out_dir / "ws_clip_highlights.json"
            hl_file.write_text(
                json.dumps(
                    {
                        "video_path": str(ws_video),
                        "source_language": "en",
                        "target_language": "zh",
                        "total_duration_sec": 20.0,
                        "highlights": [
                            {
                                "rank": 1,
                                "selected": True,
                                "review_status": "draft",
                                "start_sec": 1.0,
                                "end_sec": 5.0,
                                "score": 8.0,
                                "title_zh": "草稿一",
                            },
                            {
                                "rank": 2,
                                "selected": True,
                                "review_status": "reviewed",
                                "start_sec": 6.0,
                                "end_sec": 10.0,
                                "score": None,
                                "title_zh": "已审二",
                            },
                        ],
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

            cfg_path = tmp / "cfg.json"
            cfg_path.write_text(
                json.dumps({"input_dir": str(in_dir), "output_dir": str(out_dir)}),
                encoding="utf-8",
            )

            with (
                patch.object(web_app, "_CONFIG_PATH", cfg_path),
                patch.object(web_app, "_ENV_PATH", tmp / ".env"),
                patch.object(web_app, "_USER_AUTHORIZED_FILES", set()),
            ):
                client = TestClient(web_app.app, headers={"X-Solacut-Token": web_app._SESSION_TOKEN})

                # 1. Windows 保留设备名（含扩展名）与设备路径前缀全面拦截（400）
                for bad_stem in ("CON", "nul", "PRN", "COM1", "LPT9", "AUX.json"):
                    self.assertEqual(
                        client.get("/api/highlights", params={"stem": bad_stem}).status_code,
                        400,
                        f"未拦截保留设备名 stem={bad_stem}",
                    )
                for bad_path in (
                    r"\\?\C:\Windows\win.ini",
                    r"\??\C:\Windows\win.ini",
                    str(in_dir / "CON.mp4"),
                    str(in_dir / "NUL.mp4"),
                ):
                    self.assertEqual(
                        client.get("/api/media-info", params={"path": bad_path}).status_code,
                        400,
                        f"未拦截危险路径 {bad_path}",
                    )

                # 2. 写入型 output_dir 越界拦截（403）& POST /api/open-folder 无论存在与否均拦截非工作区目录（403）
                self.assertEqual(
                    client.post(
                        "/api/highlights/create-manual",
                        json={
                            "video_path": str(ws_video),
                            "output_dir": str(outside_dir / "evil_sub"),
                            "items": [{"start_sec": 0.5, "end_sec": 1.5}],
                        },
                    ).status_code,
                    403,
                )
                self.assertFalse((outside_dir / "evil_sub").exists())
                self.assertEqual(
                    client.post("/api/open-folder", json={"path": str(outside_dir)}).status_code,
                    403,
                )

                # 3. 读文件接口未授权工作区外路径拦截（403）；通过服务端内存白名单显式授权后立刻放行（200）
                self.assertEqual(
                    client.get("/api/media-info", params={"path": str(outside_video)}).status_code,
                    403,
                )
                self.assertEqual(
                    client.post(
                        "/api/chat/import",
                        json={"stem": "ws_clip", "chat_path": str(outside_chat), "output_dir": str(out_dir)},
                    ).status_code,
                    403,
                )
                web_app._authorize_user_file(outside_video)
                web_app._authorize_user_file(outside_chat)
                self.assertEqual(
                    client.get("/api/media-info", params={"path": str(outside_video)}).status_code,
                    200,
                )
                self.assertEqual(
                    client.post(
                        "/api/chat/import",
                        json={"stem": "ws_clip", "chat_path": str(outside_chat), "output_dir": str(out_dir)},
                    ).status_code,
                    200,
                )

                # 4. GET /api/thumbnail 强制忽略外部 output_dir，只写入 cfg['output_dir']/.thumb_cache
                evil_thumb_out = outside_dir / "hijack_thumb"
                th_resp = client.get(
                    "/api/thumbnail",
                    params={"path": str(ws_video), "time": 0.5, "w": 120, "output_dir": str(evil_thumb_out)},
                )
                self.assertEqual(th_resp.status_code, 200)
                self.assertFalse(evil_thumb_out.exists())
                self.assertTrue((out_dir / ".thumb_cache").is_dir())

                # 5. NaN / Infinity 与超长搜索词 DoS 拦截
                self.assertIn(
                    client.get("/api/waveform", params={"stem": "ws_clip", "start": "nan"}).status_code,
                    (400, 422),
                )
                self.assertIn(
                    client.get("/api/transcript", params={"stem": "ws_clip", "around_sec": "inf"}).status_code,
                    (400, 422),
                )
                self.assertEqual(
                    client.get("/api/transcript", params={"stem": "ws_clip", "q": "x" * 201}).status_code,
                    422,
                )
                self.assertEqual(
                    client.get("/api/chat/heatmap", params={"stem": "ws_clip", "q": "y" * 201}).status_code,
                    422,
                )
                nan_put = client.put(
                    "/api/highlights",
                    content=json.dumps(
                        {
                            "highlights_json_path": str(hl_file),
                            "highlights": [{"rank": 1, "start_sec": 1.0, "end_sec": 5.0, "score": float("nan")}],
                        }
                    ).encode("utf-8"),
                    headers={"Content-Type": "application/json"},
                )
                self.assertIn(nan_put.status_code, (400, 422))

                # 6. PUT /api/highlights 强校验唯一正整数 rank，且先写 review_md_path 再原子落盘
                self.assertEqual(
                    client.put(
                        "/api/highlights",
                        json={
                            "highlights_json_path": str(hl_file),
                            "highlights": [
                                {"rank": 1, "start_sec": 1.0, "end_sec": 4.0},
                                {"rank": 1, "start_sec": 5.0, "end_sec": 8.0},
                            ],
                        },
                    ).status_code,
                    400,
                )
                self.assertEqual(
                    client.put(
                        "/api/highlights",
                        json={
                            "highlights_json_path": str(hl_file),
                            "highlights": [{"rank": 0, "start_sec": 1.0, "end_sec": 4.0}],
                        },
                    ).status_code,
                    400,
                )
                ok_put = client.put(
                    "/api/highlights",
                    json={
                        "highlights_json_path": str(hl_file),
                        "highlights": [
                            {"rank": 1, "start_sec": 1.0, "end_sec": 4.0, "review_status": "draft", "score": 8.0},
                            {"rank": 2, "start_sec": 6.0, "end_sec": 10.0, "review_status": "reviewed", "score": None},
                        ],
                    },
                )
                self.assertEqual(ok_put.status_code, 200, ok_put.text)
                disk_hl = json.loads(hl_file.read_text(encoding="utf-8"))
                self.assertEqual(disk_hl["review_md_path"], str((out_dir / "ws_clip_review.md").resolve()))
                self.assertIsNone(disk_hl["highlights"][1]["score"])

                # 7. POST /api/tasks/export 与 /api/tasks/retranslate 拒绝 ranks=[]，且 only_reviewed=True 不被 ranks 绕过
                self.assertEqual(
                    client.post(
                        "/api/tasks/export",
                        json={"highlights_json_path": str(hl_file), "mode": "nle_only", "ranks": []},
                    ).status_code,
                    400,
                )
                self.assertEqual(
                    client.post(
                        "/api/tasks/export",
                        json={"highlights_json_path": str(hl_file), "mode": "nle_only", "ranks": [1], "only_reviewed": True},
                    ).status_code,
                    400,
                )
                self.assertEqual(
                    client.post(
                        "/api/tasks/retranslate",
                        json={"highlights_json_path": str(hl_file), "ranks": []},
                    ).status_code,
                    400,
                )

                # 8. POST /api/config 拒绝指向普通文件的目录路径（400）
                self.assertEqual(
                    client.post("/api/config", json={"input_dir": str(plain_file)}).status_code,
                    400,
                )
                self.assertEqual(
                    client.post("/api/config", json={"output_dir": str(plain_file)}).status_code,
                    400,
                )

                # 9. _run_tk_file_dialog 超时线程仍存活时返回 HTTP 408 且绝不提前释放锁（Mock tkinter 避免触碰真实 GUI 线程）
                import types
                from unittest.mock import MagicMock

                blocker = threading.Event()
                test_lock = threading.Lock()
                fake_tk = types.ModuleType("tkinter")
                fake_tk.Tk = MagicMock(return_value=MagicMock())  # type: ignore[attr-defined]
                try:
                    with (
                        patch.dict(sys.modules, {"tkinter": fake_tk}),
                        patch.object(web_app, "_FILE_DIALOG_LOCK", test_lock),
                    ):
                        with self.assertRaises(web_app.HTTPException) as ctx_timeout:
                            web_app._run_tk_file_dialog(lambda _root: blocker.wait(5.0), timeout_sec=0.05)
                        self.assertEqual(ctx_timeout.exception.status_code, 408)
                        # 锁在工作线程退出前必须保持占用，防止重复弹窗竞态
                        self.assertTrue(test_lock.locked())
                        with self.assertRaises(web_app.HTTPException) as ctx_busy:
                            web_app._run_tk_file_dialog(lambda _root: "second", timeout_sec=0.05)
                        self.assertEqual(ctx_busy.exception.status_code, 409)
                finally:
                    blocker.set()
                    time_mod.sleep(0.1)
                    self.assertFalse(test_lock.locked())

            # 10. scripts/build_release.py 审计门禁新增音视频后缀 + hidden-imports + 默认版本号 v0.9.2-beta
            audit_dir = tmp / "release_stage"
            audit_dir.mkdir()
            for leaked_media in ("clip.m4a", "voice.mp3", "stream.ts", "vod.webm", "rec.m4v"):
                (audit_dir / leaked_media).write_bytes(b"media")
            violations = build_release.run_security_audit(audit_dir)
            for leaked_media in ("clip.m4a", "voice.mp3", "stream.ts", "vod.webm", "rec.m4v"):
                self.assertTrue(
                    any(leaked_media in v for v in violations),
                    f"build_release 安全审计未拦截个人媒体后缀 {leaked_media}: {violations}",
                )
            br_src = (PROJECT_ROOT / "scripts" / "build_release.py").read_text(encoding="utf-8")
            self.assertIn('"src.nle_export"', br_src)
            self.assertIn('"src.chat_radar"', br_src)
            self.assertIn('default="v0.9.2-beta"', br_src)


if __name__ == "__main__":
    unittest.main()
