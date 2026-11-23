"""三层配置中心（src/config.py）、独立 ASR 引擎（src/asr_providers.py）与高可用 LLM 调度层（src/llm_client.py）单元测试。

全部测试纯本地离线 Mock，不消耗真实 API 额度、不触碰项目真实 .env 文件。
"""

from __future__ import annotations

import math
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import numpy as np

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.asr_providers import (  # noqa: E402
    detect_speech_chunks,
    encode_float32_to_wav_bytes,
    normalize_audio_for_asr,
    transcribe_window_with_deepseek_asr,
    transcribe_window_with_openai_compatible_asr,
)
from src.config import (  # noqa: E402
    read_workbench_config,
    resolve_asr_config,
    resolve_llm_role_config,
    write_env_settings,
)
from src.llm_client import LLMClient, TokenUsageTracker  # noqa: E402


class _FakeHttpResp:
    """模拟 httpx.Response 的轻量对象。"""

    def __init__(self, status_code: int, payload: dict | None = None, text: str = "") -> None:
        self.status_code = status_code
        self._payload = payload if payload is not None else {}
        self.text = text or ("{}" if payload is not None else "")

    def json(self) -> dict:
        return self._payload


class _FakeApiError(Exception):
    """模拟携带 status_code 的 OpenAI 异常。"""

    def __init__(self, message: str, status_code: int) -> None:
        super().__init__(message)
        self.status_code = status_code


class TestConfigAndProviders(unittest.TestCase):
    """三层路由配置、音频归一化+VAD、云端 ASR 与高可用 LLM 客户端测试套件。"""

    def test_three_tier_config_fallback_and_overrides(self) -> None:
        """测试 1：验证单 Key 全家桶默认回退（DeepSeek ASR + deepseek-flash）与三层独立覆写。"""
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp = Path(tmp_dir)
            env_file = tmp / ".env"
            cfg_file = tmp / "wb.json"

            # 先写入夹杂非法代码行的脏 .env，模拟误贴代码后通过工作台自动净化
            env_file.write_text(
                "client = OpenAI(\nOPENAI_API_KEY=sk-primary-1234567890\n)\n",
                encoding="utf-8",
            )

            clean_env = {
                k: ""
                for k in (
                    "OPENAI_API_KEY",
                    "DEEPSEEK_API_KEY",
                    "OPENAI_BASE_URL",
                    "LLM_MODEL",
                    "ASR_PROVIDER",
                    "ASR_API_KEY",
                    "ASR_BASE_URL",
                    "ASR_MODEL",
                    "SCREEN_LLM_API_KEY",
                    "SCREEN_LLM_MODEL",
                    "POLISH_LLM_API_KEY",
                    "POLISH_LLM_MODEL",
                )
            }
            # 移除进程环境变量干扰，确保纯粹从临时 env_file 解析
            with patch.dict(os.environ, {}, clear=False):
                for k in clean_env:
                    os.environ.pop(k, None)

                # 1. 极简单 Key 模式：自动默认 deepseek ASR + deepseek-flash
                asr_cfg = resolve_asr_config(require_api_key=True, env_path=env_file)
                self.assertEqual(asr_cfg["provider"], "deepseek")
                self.assertEqual(asr_cfg["api_key"], "sk-primary-1234567890")
                self.assertIn("/v1/tools/asr", asr_cfg["base_url"])

                screen_cfg = resolve_llm_role_config("screen", env_path=env_file)
                polish_cfg = resolve_llm_role_config("polish", env_path=env_file)
                self.assertEqual(screen_cfg["model"], "deepseek-flash")
                self.assertEqual(polish_cfg["model"], "deepseek-flash")
                self.assertEqual(polish_cfg["api_key"], "sk-primary-1234567890")

                # 2. 写入三层独立覆盖，并验证掩码 Key 不会覆盖真实主密钥，同时脏代码行被自动清理
                write_env_settings(
                    env_file,
                    {
                        "OPENAI_API_KEY": "sk-p********7890",  # 含星号掩码应保留原值
                        "ASR_PROVIDER": "openai_compatible",
                        "ASR_API_KEY": "sk-groq-asr-999999",
                        "ASR_MODEL": "whisper-large-v3-turbo",
                        "SCREEN_LLM_MODEL": "deepseek-flash",
                        "POLISH_LLM_MODEL": "deepseek-v4-pro",
                        "POLISH_LLM_API_KEY": "sk-polish-888888",
                    },
                )
                saved_text = env_file.read_text(encoding="utf-8")
                self.assertNotIn("client = OpenAI", saved_text)

                wb = read_workbench_config(config_path=cfg_file, env_path=env_file)
                self.assertTrue(wb["has_api_key"])
                self.assertNotIn("1234567890", wb["api_key_masked"])
                self.assertEqual(wb["asr_provider"], "openai_compatible")
                self.assertEqual(wb["polish_effective_model"], "deepseek-v4-pro")
                self.assertIn("****", wb["asr_api_key_masked"])
                self.assertIn("****", wb["polish_api_key_masked"])

                # 清理写回 os.environ 的临时测试键
                for k in list(os.environ.keys()):
                    if k.startswith(("ASR_", "SCREEN_LLM_", "POLISH_LLM_")) or k in {
                        "OPENAI_API_KEY",
                        "OPENAI_BASE_URL",
                        "LLM_MODEL",
                    }:
                        os.environ.pop(k, None)

    def test_normalize_audio_boosts_quiet_speech_without_noise_blowup(self) -> None:
        """测试 2：验证温和局部 AGC 可明显抬升轻声吐槽，但不放大纯静音底噪也不破音削波。"""
        sr = 16000
        t_block = np.arange(sr, dtype=np.float32) / float(sr)
        # 第 1 秒：极小底噪 0.0005；第 2 秒：主播轻声说话 0.015；第 3 秒：主播大声尖叫 0.90
        noise = 0.0005 * np.sin(2.0 * math.pi * 500.0 * t_block)
        whisper_voice = 0.015 * np.sin(2.0 * math.pi * 600.0 * t_block)
        shout = 0.90 * np.sin(2.0 * math.pi * 700.0 * t_block)
        audio = np.concatenate([noise, whisper_voice, shout]).astype(np.float32)

        norm = normalize_audio_for_asr(audio, sample_rate=sr)
        self.assertEqual(norm.size, audio.size)

        noise_rms_in = float(np.sqrt(np.mean(noise ** 2)))
        noise_rms_out = float(np.sqrt(np.mean(norm[:sr] ** 2)))
        quiet_rms_in = float(np.sqrt(np.mean(whisper_voice ** 2)))
        quiet_rms_out = float(np.sqrt(np.mean(norm[sr : 2 * sr] ** 2)))

        # 轻声说话被明显放大（> 3.5 倍）
        self.assertGreater(quiet_rms_out, quiet_rms_in * 3.5)
        # 静音底噪不被放大到干扰级别
        self.assertLess(noise_rms_out, noise_rms_in * 2.5)
        # 尖叫块被软限幅保护在 [-1.0, 1.0]
        self.assertLessEqual(float(np.max(np.abs(norm))), 1.0)

    def test_detect_speech_chunks_vad_and_max_duration_split(self) -> None:
        """测试 3：验证能量降级 VAD 与硬时长裁剪逻辑，保证永远 <= 120s 适配 DeepSeek ASR。"""
        sr = 16000
        # 构造 16 秒音频：0~1s 静音 | 1~15s 连续 600Hz 有声 | 15~16s 静音
        t14 = np.arange(14 * sr, dtype=np.float32) / float(sr)
        audio = np.concatenate(
            [
                np.zeros(sr, dtype=np.float32),
                0.25 * np.sin(2.0 * math.pi * 600.0 * t14),
                np.zeros(sr, dtype=np.float32),
            ]
        )

        # 设置 max_speech_sec=6.0，14 秒连续语音必须被自动安全切分成 >= 3 块且每块 <= 6.5s
        chunks = detect_speech_chunks(
            audio,
            sample_rate=sr,
            max_speech_sec=6.0,
            force_energy_fallback=True,
        )
        self.assertGreaterEqual(len(chunks), 3)
        for s_t, e_t in chunks:
            self.assertLessEqual(e_t - s_t, 6.5)

        wav_bytes = encode_float32_to_wav_bytes(audio[:sr], sample_rate=sr)
        self.assertEqual(wav_bytes[:4], b"RIFF")
        self.assertGreater(len(wav_bytes), 44)

    def test_deepseek_asr_provider_retry_concurrency_and_order(self) -> None:
        """测试 4：验证 DeepSeek ASR 请求格式、429 指数退避重试、空文本过滤与并发有序组装。"""
        sr = 16000
        t2 = np.arange(2 * sr, dtype=np.float32) / float(sr)
        # 构造两段清晰分离的语音（中间间隔 2 秒静音）
        speech1 = 0.25 * np.sin(2.0 * math.pi * 550.0 * t2)
        silence = np.zeros(2 * sr, dtype=np.float32)
        speech2 = 0.30 * np.sin(2.0 * math.pi * 650.0 * t2)
        audio = np.concatenate([silence[: sr // 2], speech1, silence, speech2, silence[: sr // 2]])

        call_attempts = {"count": 0}
        seen_auth: list[str] = []

        def _fake_post(url: str, headers: dict, files: dict, data=None, timeout=60.0):
            seen_auth.append(headers.get("Authorization", ""))
            fname, wav_bytes, mime = files["file"]
            self.assertEqual(mime, "audio/wav")
            self.assertEqual(wav_bytes[:4], b"RIFF")
            call_attempts["count"] += 1
            # 第 1 次模拟触发 429 限流，验证自动重试成功
            if call_attempts["count"] == 1:
                return _FakeHttpResp(429, text="rate limited")
            if fname.startswith("seg_0"):
                return _FakeHttpResp(200, {"request_id": "r1", "text": "We are in this together!", "duration_ms": 2000})
            return _FakeHttpResp(200, {"request_id": "r2", "text": "It sucked me in so bad!", "duration_ms": 2000})

        with patch(
            "src.asr_providers.detect_speech_chunks",
            return_value=[(0.5, 2.5), (4.5, 6.5)],
        ):
            res = transcribe_window_with_deepseek_asr(
                audio=audio,
                sample_rate=sr,
                api_key="sk-test-ds-asr",
                base_url="https://api.deepseek.com/v1/tools/asr",
                concurrency=2,
                http_post=_fake_post,
                retry_base_delay=0.0,
            )

        self.assertGreaterEqual(call_attempts["count"], 3)
        self.assertTrue(all(a == "Bearer sk-test-ds-asr" for a in seen_auth))
        self.assertEqual(len(res["segments"]), 2)
        self.assertEqual(res["segments"][0]["text"], "We are in this together!")
        self.assertEqual(res["segments"][1]["text"], "It sucked me in so bad!")
        self.assertLess(res["segments"][0]["start_sec"], res["segments"][1]["start_sec"])
        self.assertAlmostEqual(res["billed_audio_sec"], 4.0, places=1)

    def test_openai_compatible_asr_provider_400_fallback(self) -> None:
        """测试 5：验证 OpenAI 兼容 ASR 支持 verbose_json 并在遇到 400 时自动降级普通 json。"""
        sr = 16000
        audio = 0.2 * np.sin(2.0 * math.pi * 500.0 * np.arange(2 * sr, dtype=np.float32) / sr)
        seen_formats: list[str] = []

        def _fake_post(url: str, headers: dict, files: dict, data=None, timeout=60.0):
            fmt = (data or {}).get("response_format", "")
            seen_formats.append(fmt)
            if fmt == "verbose_json":
                return _FakeHttpResp(400, text="verbose_json not supported")
            return _FakeHttpResp(200, {"text": "Fallback plain json works!"})

        with patch("src.asr_providers.detect_speech_chunks", return_value=[(0.2, 1.8)]):
            res = transcribe_window_with_openai_compatible_asr(
                audio=audio,
                sample_rate=sr,
                api_key="sk-compat",
                base_url="https://example.com/v1/audio/transcriptions",
                model="whisper-large-v3-turbo",
                hotwords="Shylily, KEKW",
                http_post=_fake_post,
                retry_base_delay=0.0,
            )

        self.assertEqual(seen_formats, ["verbose_json", "json"])
        self.assertEqual(len(res["segments"]), 1)
        self.assertEqual(res["segments"][0]["text"], "Fallback plain json works!")

    def test_llm_client_429_retry_reasoning_400_and_usage_tracker(self) -> None:
        """测试 6：验证 LLMClient 遇到不支持 temperature 报错自动剥离、429 自动退避重试及 Token 统计。"""
        seen_kwargs: list[dict] = []
        state = {"step": 0}

        class _FakeCompletions:
            def create(self, **kwargs):
                seen_kwargs.append(dict(kwargs))
                state["step"] += 1
                if state["step"] == 1:
                    raise _FakeApiError("Unsupported parameter: temperature is not supported for reasoning model", 400)
                if state["step"] == 2:
                    raise _FakeApiError("Rate limit exceeded", 429)
                return SimpleNamespace(
                    choices=[SimpleNamespace(message=SimpleNamespace(content='{"ok": true}'))],
                    usage=SimpleNamespace(prompt_tokens=1200, completion_tokens=300, total_tokens=1500),
                )

        fake_client = SimpleNamespace(chat=SimpleNamespace(completions=_FakeCompletions()))
        client = LLMClient(client_factory=lambda **kw: fake_client)
        tracker = TokenUsageTracker()

        reply = client.complete_json(
            system_prompt="sys",
            user_prompt="usr",
            api_key="sk-test",
            base_url="https://api.deepseek.com/v1",
            model="deepseek-flash",
            reasoning_effort="low",
            role="screen",
            usage_tracker=tracker,
            retry_base_delay=0.0,
        )

        self.assertEqual(reply, '{"ok": true}')
        self.assertEqual(len(seen_kwargs), 3)
        self.assertIn("temperature", seen_kwargs[0])
        self.assertNotIn("temperature", seen_kwargs[1])
        self.assertEqual(seen_kwargs[0]["reasoning_effort"], "low")

        summary = tracker.summary()
        self.assertEqual(summary["request_count"], 1)
        self.assertEqual(summary["total_tokens"], 1500)
        self.assertEqual(summary["by_role"]["screen"]["model"], "deepseek-flash")

    def test_llm_client_deepseek_flash_thinking_toggle_and_length_truncation_recovery(self) -> None:
        """测试 7：验证 deepseek-flash 思考开关预算与 finish_reason=length 截断自动提额恢复。"""
        seen_kwargs: list[dict] = []

        class _FakeCompletions:
            def __init__(self, responses):
                self._responses = list(responses)
                self._idx = 0

            def create(self, **kwargs):
                seen_kwargs.append(dict(kwargs))
                resp = self._responses[self._idx]
                self._idx += 1
                return resp

        # 场景 A：deepseek-flash 默认留空 reasoning_effort -> 必须显式传 thinking.disabled 防止默认 high 吃爆 8192
        fake_a = SimpleNamespace(
            chat=SimpleNamespace(
                completions=_FakeCompletions(
                    [
                        SimpleNamespace(
                            choices=[
                                SimpleNamespace(
                                    finish_reason="stop",
                                    message=SimpleNamespace(content='{"highlights": []}'),
                                )
                            ],
                            usage=SimpleNamespace(prompt_tokens=100, completion_tokens=50, total_tokens=150),
                        )
                    ]
                )
            )
        )
        client_a = LLMClient(client_factory=lambda **kw: fake_a)
        out_a = client_a.complete_json(
            system_prompt="sys",
            user_prompt="usr",
            api_key="sk-test",
            base_url="https://api.deepseek.com/v1",
            model="deepseek-flash",
            reasoning_effort="",
            retry_base_delay=0.0,
        )
        self.assertEqual(out_a, '{"highlights": []}')
        self.assertEqual(seen_kwargs[-1].get("extra_body"), {"thinking": {"type": "disabled"}})
        self.assertNotIn("reasoning_effort", seen_kwargs[-1])

        # 场景 B：deepseek-flash 开启 low 思考先遇 finish_reason=length 截断，自动提额到 65536 后恢复成功
        seen_kwargs.clear()
        fake_b = SimpleNamespace(
            chat=SimpleNamespace(
                completions=_FakeCompletions(
                    [
                        SimpleNamespace(
                            choices=[
                                SimpleNamespace(
                                    finish_reason="length",
                                    message=SimpleNamespace(content='{"highlights": [{"title_en": "Begs for Par'),
                                )
                            ],
                            usage=SimpleNamespace(prompt_tokens=1000, completion_tokens=32768, total_tokens=33768),
                        ),
                        SimpleNamespace(
                            choices=[
                                SimpleNamespace(
                                    finish_reason="stop",
                                    message=SimpleNamespace(content='{"highlights": [{"start_sec": 10.0}]}'),
                                )
                            ],
                            usage=SimpleNamespace(prompt_tokens=1000, completion_tokens=400, total_tokens=1400),
                        ),
                    ]
                )
            )
        )
        client_b = LLMClient(client_factory=lambda **kw: fake_b)
        out_b = client_b.complete_json(
            system_prompt="sys",
            user_prompt="usr",
            api_key="sk-test",
            base_url="https://api.deepseek.com/v1",
            model="deepseek-flash",
            reasoning_effort="low",
            retry_base_delay=0.0,
        )
        self.assertEqual(out_b, '{"highlights": [{"start_sec": 10.0}]}')
        self.assertEqual(len(seen_kwargs), 2)
        self.assertEqual(seen_kwargs[0]["max_tokens"], 32768)
        self.assertEqual(seen_kwargs[0]["extra_body"], {"thinking": {"type": "enabled"}})
        self.assertEqual(seen_kwargs[1]["max_tokens"], 65536)

        # 场景 C：持续触发 finish_reason=length 耗尽重试时，必须明确报长度截断错误而非静默放行半截 JSON
        fake_c = SimpleNamespace(
            chat=SimpleNamespace(
                completions=_FakeCompletions(
                    [
                        SimpleNamespace(
                            choices=[
                                SimpleNamespace(
                                    finish_reason="length",
                                    message=SimpleNamespace(content='{"partial": 1'),
                                )
                            ],
                            usage=SimpleNamespace(prompt_tokens=100, completion_tokens=65536, total_tokens=65636),
                        )
                        for _ in range(4)
                    ]
                )
            )
        )
        client_c = LLMClient(client_factory=lambda **kw: fake_c)
        with self.assertRaises(RuntimeError) as ctx:
            client_c.complete_json(
                system_prompt="sys",
                user_prompt="usr",
                api_key="sk-test",
                base_url="https://api.deepseek.com/v1",
                model="deepseek-flash",
                reasoning_effort="high",
                retry_base_delay=0.0,
            )
        self.assertIn("finish_reason=length", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
