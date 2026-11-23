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

                # 禁止读取 .env 等非视频文件
                blocked = client.get("/api/video-stream", params={"path": str(tmp / ".env")})
                self.assertEqual(blocked.status_code, 404)

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

            # 1. 验证 Web 接口
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

            with (
                patch.object(web_app, "_CONFIG_PATH", tmp / "cfg.json"),
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


if __name__ == "__main__":
    unittest.main()
