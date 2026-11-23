"""独立语音识别（ASR）引擎抽象层与音频防漏词预处理模块。

本模块将语音识别彻底与文本大模型解耦，提供：
1. 内存级温和动态响度归一化（`normalize_audio_for_asr`）：把主播轻声吐槽/气声温柔托起，
   彻底根治“声音小一点就不识别”，同时绝不污染磁盘上 Step 1 原始音频。
2. 高灵敏度语音活动检测切段（`detect_speech_chunks`）：优先复用内置 Silero VAD，
   采用低门限（0.30）+ 450ms 静音切句 + 12s 上限，既捞回小声又彻底消灭 14 秒僵尸长字幕，
   并天然破解 DeepSeek `/v1/tools/asr` 无逐句时间戳与 120s 时长限制。
3. 三大独立 ASR 引擎统一封装（均内置 429/5xx 指数退避重试与并发调度）：
   - `deepseek`（默认）：DeepSeek 官方原生语音识别 API (`POST /v1/tools/asr`)
   - `openai_compatible`：通用 OpenAI `/audio/transcriptions` 接口（Groq / OpenAI / 硅基流动等）
   - `local`：本地 RTX CUDA / CPU 增强版 faster-whisper（默认 `large-v3-turbo` + 热词 + 调优 VAD）
"""

from __future__ import annotations

import io
import random
import time
import wave
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Callable

import httpx
import numpy as np


def normalize_audio_for_asr(
    audio: np.ndarray,
    sample_rate: int = 16000,
    target_rms: float = 0.09,
    max_gain: float = 6.0,
    noise_floor_rms: float = 0.004,
) -> np.ndarray:
    """对送入 ASR/VAD 的内存音频执行温和局部响度归一化（AGC + 软限幅）。

    为什么需要这一步？
    游戏主播高声尖叫可达 0 dBFS，而低声吐槽或气声补刀只有 -38 dBFS（RMS ~0.01），
    原始裸音频直接喂给 VAD 会把轻声说话误当静音一刀切掉。
    这里只对高于底噪门限（>0.004）的有声块做平滑增益，纯静音底噪绝不放大，并用 tanh 防止尖叫破音。
    """
    if audio.size == 0:
        return audio.astype(np.float32, copy=False)

    x = audio.astype(np.float32, copy=True)
    # 去除直流偏置
    x -= float(np.mean(x))

    frame_len = max(1, int(round(sample_rate * 0.5)))
    pad_len = (-x.size) % frame_len
    padded = np.pad(x, (0, pad_len), mode="constant") if pad_len else x
    blocks = padded.reshape(-1, frame_len)

    block_rms = np.sqrt(np.mean(blocks * blocks, axis=1) + 1e-12)
    gains = np.where(
        block_rms > noise_floor_rms,
        np.clip(target_rms / block_rms, 1.0, max_gain),
        1.0,
    ).astype(np.float32)

    # 三点平滑过渡，防止相邻 0.5 秒块增益突变产生爆音
    if gains.size >= 3:
        gains = np.convolve(
            np.pad(gains, (1, 1), mode="edge"),
            np.array([0.25, 0.5, 0.25], dtype=np.float32),
            mode="valid",
        ).astype(np.float32)

    sample_gains = np.repeat(gains, frame_len)[: x.size]
    boosted = x * sample_gains
    # 柔和软限幅，保留清晰度同时防止超过 [-1.0, 1.0] 削波失真
    soft_clipped = np.tanh(boosted * 1.15) / 1.15
    return soft_clipped.astype(np.float32, copy=False)


def _fallback_energy_vad(
    audio: np.ndarray,
    sample_rate: int = 16000,
    min_speech_ms: int = 150,
    min_silence_ms: int = 450,
    speech_pad_ms: int = 220,
) -> list[tuple[float, float]]:
    """无 Silero VAD 时的纯 NumPy 能量包络降级语音切段器。"""
    if audio.size == 0:
        return []
    frame_sec = 0.05
    frame_len = max(1, int(round(sample_rate * frame_sec)))
    pad = (-audio.size) % frame_len
    padded = np.pad(audio, (0, pad), mode="constant") if pad else audio
    frames = padded.reshape(-1, frame_len)
    rms = np.sqrt(np.mean(frames * frames, axis=1) + 1e-12)

    # 动态门限：防止整段连续说话（90% 以上都在发声）时被高分位数反杀误判为静音
    noise_floor = float(np.percentile(rms, 10))
    peak_level = float(np.percentile(rms, 90))
    if peak_level < 0.010:
        return []
    if noise_floor >= peak_level * 0.65:
        thr = 0.015
    else:
        thr = max(0.012, min(peak_level * 0.35, noise_floor * 2.2))
    active = rms >= thr
    if not np.any(active):
        return []

    total_dur = audio.size / float(sample_rate)
    min_sil_frames = max(1, int(round((min_silence_ms / 1000.0) / frame_sec)))
    min_sp_sec = min_speech_ms / 1000.0
    pad_sec = speech_pad_ms / 1000.0

    # 先按连续有声与短静音桥接成粗区间
    raw_spans: list[list[int]] = []
    idx = 0
    n = active.size
    while idx < n:
        if not active[idx]:
            idx += 1
            continue
        start_f = idx
        while idx < n and active[idx]:
            idx += 1
        end_f = idx
        if raw_spans and (start_f - raw_spans[-1][1]) <= min_sil_frames:
            raw_spans[-1][1] = end_f
        else:
            raw_spans.append([start_f, end_f])

    out: list[tuple[float, float]] = []
    for s_f, e_f in raw_spans:
        s_t = max(0.0, s_f * frame_sec - pad_sec)
        e_t = min(total_dur, e_f * frame_sec + pad_sec)
        if e_t - s_t < min_sp_sec:
            continue
        if out and s_t <= out[-1][1]:
            out[-1] = (out[-1][0], max(out[-1][1], e_t))
        else:
            out.append((round(s_t, 3), round(e_t, 3)))
    return out


def _split_oversized_chunk(
    start_sec: float,
    end_sec: float,
    audio: np.ndarray,
    sample_rate: int,
    hard_max_sec: float,
) -> list[tuple[float, float]]:
    """若单个语音段超过上限，在中间 35%~65% 区域寻找局部能量最低点递归一分为二。"""
    dur = end_sec - start_sec
    if dur <= hard_max_sec or dur < 2.0:
        return [(round(start_sec, 3), round(end_sec, 3))]

    s_idx = max(0, int(round(start_sec * sample_rate)))
    e_idx = min(audio.size, int(round(end_sec * sample_rate)))
    seg = audio[s_idx:e_idx]
    win = max(1, int(round(sample_rate * 0.1)))  # 100ms 能量窗
    if seg.size < win * 4:
        mid = (start_sec + end_sec) / 2.0
        return _split_oversized_chunk(start_sec, mid, audio, sample_rate, hard_max_sec) + (
            _split_oversized_chunk(mid, end_sec, audio, sample_rate, hard_max_sec)
        )

    num_wins = seg.size // win
    energies = np.mean(seg[: num_wins * win].reshape(num_wins, win) ** 2, axis=1)
    lo = max(1, int(num_wins * 0.35))
    hi = min(num_wins - 1, int(num_wins * 0.65))
    if hi <= lo:
        best_w = num_wins // 2
    else:
        best_w = lo + int(np.argmin(energies[lo:hi]))
    split_sec = start_sec + (best_w * win) / float(sample_rate)

    return _split_oversized_chunk(start_sec, split_sec, audio, sample_rate, hard_max_sec) + (
        _split_oversized_chunk(split_sec, end_sec, audio, sample_rate, hard_max_sec)
    )


def detect_speech_chunks(
    audio: np.ndarray,
    sample_rate: int = 16000,
    vad_threshold: float = 0.30,
    neg_threshold: float | None = None,
    min_speech_ms: int = 150,
    min_silence_ms: int = 450,
    max_speech_sec: float = 12.0,
    speech_pad_ms: int = 220,
    force_energy_fallback: bool = False,
) -> list[tuple[float, float]]:
    """使用高灵敏度 Silero VAD（降级能量 VAD）切出带精确起止秒数的自然说话语段。

    设计优势：
    - 门限默认降至 0.30（配合响度归一化）彻底拯救主播轻声句；
    - 450ms 自然停顿即断句，消灭 14 秒僵尸字幕；
    - 强制不超过 `max_speech_sec`（且硬顶 <= 110s），100% 符合 DeepSeek ASR ≤120s/8MiB 规范。
    """
    if audio.size == 0:
        return []

    total_dur = audio.size / float(sample_rate)
    hard_max = min(max(2.0, float(max_speech_sec)), 110.0)
    resolved_neg = (
        float(neg_threshold)
        if neg_threshold is not None
        else max(0.10, round(float(vad_threshold) - 0.12, 2))
    )

    chunks: list[tuple[float, float]] = []
    used_silero = False

    if not force_energy_fallback and sample_rate == 16000:
        try:
            from faster_whisper.vad import VadOptions, get_speech_timestamps

            opts = VadOptions(
                threshold=float(vad_threshold),
                neg_threshold=resolved_neg,
                min_speech_duration_ms=int(min_speech_ms),
                max_speech_duration_s=float(hard_max),
                min_silence_duration_ms=int(min_silence_ms),
                speech_pad_ms=int(speech_pad_ms),
            )
            raw_ts = get_speech_timestamps(
                audio.astype(np.float32, copy=False),
                vad_options=opts,
                sampling_rate=sample_rate,
            )
            used_silero = True
            for item in raw_ts:
                s_t = max(0.0, float(item["start"]) / float(sample_rate))
                e_t = min(total_dur, float(item["end"]) / float(sample_rate))
                if e_t > s_t:
                    chunks.append((round(s_t, 3), round(e_t, 3)))
        except Exception:
            used_silero = False

    if not used_silero:
        chunks = _fallback_energy_vad(
            audio=audio,
            sample_rate=sample_rate,
            min_speech_ms=min_speech_ms,
            min_silence_ms=min_silence_ms,
            speech_pad_ms=speech_pad_ms,
        )

    # 二次硬顶保护：无论哪路 VAD 产出，绝不出现超过硬上限的超长段
    bounded: list[tuple[float, float]] = []
    for s_t, e_t in chunks:
        if e_t - s_t > hard_max + 0.5:
            bounded.extend(_split_oversized_chunk(s_t, e_t, audio, sample_rate, hard_max))
        else:
            bounded.append((round(s_t, 3), round(e_t, 3)))

    return bounded


def encode_float32_to_wav_bytes(audio: np.ndarray, sample_rate: int = 16000) -> bytes:
    """将归一化 float32 音频数组在内存中直接封装为 16-bit 单声道 PCM WAV 字节流。"""
    clipped = np.clip(audio, -1.0, 1.0)
    pcm16 = np.round(clipped * 32767.0).astype("<i2")

    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(int(sample_rate))
        wf.writeframes(pcm16.tobytes())
    return buf.getvalue()


def _post_multipart_with_retry(
    url: str,
    headers: dict[str, str],
    files: dict[str, Any],
    data: dict[str, Any] | None = None,
    timeout: float = 60.0,
    max_retries: int = 4,
    retry_base_delay: float = 1.0,
    http_post: Callable[..., Any] | None = None,
) -> tuple[int, dict[str, Any], str]:
    """发送带 429/5xx/超时指数退避重试的 multipart HTTP POST 请求。"""
    last_error: Exception | None = None
    retryable_status = {408, 429, 500, 502, 503, 504}

    for attempt in range(max_retries + 1):
        try:
            if http_post is not None:
                resp = http_post(
                    url,
                    headers=headers,
                    files=files,
                    data=data,
                    timeout=timeout,
                )
            else:
                with httpx.Client(timeout=timeout) as client:
                    resp = client.post(url, headers=headers, files=files, data=data)

            status_code = int(resp.status_code)
            raw_text = resp.text or ""
            try:
                payload = resp.json() if raw_text.strip() else {}
            except Exception:
                payload = {}

            if status_code in {401, 403}:
                raise RuntimeError(
                    f"云端语音识别接口鉴权失败（HTTP {status_code}），请检查 ASR_API_KEY / OPENAI_API_KEY 是否正确。"
                )

            if status_code in retryable_status:
                if attempt < max_retries:
                    delay = retry_base_delay * (2 ** attempt) + random.uniform(0.0, retry_base_delay * 0.25)
                    if delay > 0:
                        time.sleep(delay)
                    continue
                raise RuntimeError(
                    f"云端语音识别接口持续返回 HTTP {status_code}（已重试 {max_retries} 次）：{raw_text[:200]}"
                )

            return status_code, payload if isinstance(payload, dict) else {}, raw_text
        except (httpx.TimeoutException, httpx.TransportError, OSError) as exc:
            last_error = exc
            if attempt < max_retries:
                delay = retry_base_delay * (2 ** attempt)
                if delay > 0:
                    time.sleep(delay)
                continue
            raise RuntimeError(
                f"云端语音识别网络请求失败（已重试 {max_retries} 次）：{exc}"
            ) from exc

    raise RuntimeError(f"云端语音识别请求失败：{last_error}")


def transcribe_window_with_deepseek_asr(
    audio: np.ndarray,
    sample_rate: int = 16000,
    api_key: str = "",
    base_url: str = "https://api.deepseek.com/v1/tools/asr",
    normalize_audio: bool = True,
    vad_threshold: float = 0.30,
    concurrency: int = 4,
    timeout: float = 60.0,
    http_post: Callable[..., Any] | None = None,
    retry_base_delay: float = 1.0,
) -> dict[str, Any]:
    """使用 DeepSeek 官方语音识别 API（`/v1/tools/asr`）转写音频窗口并赋予精准时间轴。

    核心技术路线：
    1. 内存级温和响度归一化 -> 捞回主播小声吐槽，解决“声音小不识别”；
    2. 内置高灵敏度 Silero VAD 切出 1~12 秒自然语段 -> 精准拿到每句起止时间戳，
       同时满足官方单次 <=120s / <=8MiB 限制并省去纯静音段的计费；
    3. 多线程并发提交 + 429/5xx 指数退避重试，极速完成整段转写。
    """
    if audio.size == 0:
        return {"segments": [], "billed_audio_sec": 0.0, "request_count": 0, "detected_language": None}
    if not api_key and http_post is None:
        raise RuntimeError("调用 DeepSeek 云端 ASR 需要有效的 API Key。")

    proc_audio = normalize_audio_for_asr(audio, sample_rate) if normalize_audio else audio.astype(np.float32, copy=False)
    speech_chunks = detect_speech_chunks(
        proc_audio,
        sample_rate=sample_rate,
        vad_threshold=vad_threshold,
        max_speech_sec=12.0,
    )
    if not speech_chunks:
        return {"segments": [], "billed_audio_sec": 0.0, "request_count": 0, "detected_language": None}

    headers = {"Authorization": f"Bearer {api_key}"}

    def _call_one(idx: int, s_sec: float, e_sec: float) -> tuple[int, dict[str, Any] | None, float]:
        s_idx = max(0, int(round(s_sec * sample_rate)))
        e_idx = min(proc_audio.size, int(round(e_sec * sample_rate)))
        clip = proc_audio[s_idx:e_idx]
        if clip.size < int(sample_rate * 0.1):
            return idx, None, 0.0

        wav_bytes = encode_float32_to_wav_bytes(clip, sample_rate=sample_rate)
        status, payload, raw_text = _post_multipart_with_retry(
            url=base_url,
            headers=headers,
            files={"file": (f"seg_{idx}.wav", wav_bytes, "audio/wav")},
            timeout=timeout,
            retry_base_delay=retry_base_delay,
            http_post=http_post,
        )
        if status >= 400:
            raise RuntimeError(f"DeepSeek ASR 接口报错（HTTP {status}）：{raw_text[:240]}")

        text = str(payload.get("text") or "").strip()
        dur_ms = payload.get("duration_ms")
        billed = (float(dur_ms) / 1000.0) if isinstance(dur_ms, (int, float)) and dur_ms > 0 else (e_sec - s_sec)
        if not text:
            return idx, None, billed

        return (
            idx,
            {
                "start_sec": round(s_sec, 2),
                "end_sec": round(e_sec, 2),
                "text": text,
                "words": None,
            },
            billed,
        )

    workers = max(1, min(int(concurrency), len(speech_chunks)))
    ordered_results: list[tuple[int, dict[str, Any]]] = []
    total_billed = 0.0

    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [
            pool.submit(_call_one, idx, s_sec, e_sec)
            for idx, (s_sec, e_sec) in enumerate(speech_chunks)
        ]
        for fut in as_completed(futures):
            idx, seg_obj, billed = fut.result()
            total_billed += billed
            if seg_obj is not None:
                ordered_results.append((idx, seg_obj))

    ordered_results.sort(key=lambda item: item[0])
    return {
        "segments": [item[1] for item in ordered_results],
        "billed_audio_sec": round(total_billed, 2),
        "request_count": len(speech_chunks),
        "detected_language": None,
    }


def _group_chunks_for_batch_asr(
    chunks: list[tuple[float, float]],
    max_group_span_sec: float = 45.0,
    max_gap_sec: float = 2.0,
) -> list[tuple[float, float]]:
    """将相邻语音块适度聚合成 <= 45 秒的批次，减少 OpenAI 兼容接口的请求次数。"""
    if not chunks:
        return []
    grouped: list[list[float]] = [[chunks[0][0], chunks[0][1]]]
    for s_t, e_t in chunks[1:]:
        prev_s, prev_e = grouped[-1]
        if (s_t - prev_e) <= max_gap_sec and (e_t - prev_s) <= max_group_span_sec:
            grouped[-1][1] = max(prev_e, e_t)
        else:
            grouped.append([s_t, e_t])
    return [(round(s, 3), round(e, 3)) for s, e in grouped]


def transcribe_window_with_openai_compatible_asr(
    audio: np.ndarray,
    sample_rate: int = 16000,
    api_key: str = "",
    base_url: str = "https://api.openai.com/v1/audio/transcriptions",
    model: str = "whisper-large-v3-turbo",
    language: str | None = None,
    hotwords: str = "",
    normalize_audio: bool = True,
    vad_threshold: float = 0.30,
    concurrency: int = 4,
    timeout: float = 90.0,
    http_post: Callable[..., Any] | None = None,
    retry_base_delay: float = 1.0,
) -> dict[str, Any]:
    """使用通用 OpenAI 兼容 `/audio/transcriptions` 接口转写（支持 Groq/OpenAI/硅基流动等）。"""
    if audio.size == 0:
        return {"segments": [], "billed_audio_sec": 0.0, "request_count": 0, "detected_language": language}
    if not api_key and http_post is None:
        raise RuntimeError("调用 OpenAI 兼容云端 ASR 需要有效的 API Key。")

    proc_audio = normalize_audio_for_asr(audio, sample_rate) if normalize_audio else audio.astype(np.float32, copy=False)
    speech_chunks = detect_speech_chunks(
        proc_audio,
        sample_rate=sample_rate,
        vad_threshold=vad_threshold,
        max_speech_sec=20.0,
    )
    if not speech_chunks:
        return {"segments": [], "billed_audio_sec": 0.0, "request_count": 0, "detected_language": language}

    batched_chunks = _group_chunks_for_batch_asr(speech_chunks, max_group_span_sec=45.0)
    headers = {"Authorization": f"Bearer {api_key}"}

    def _call_one(idx: int, s_sec: float, e_sec: float) -> tuple[int, list[dict[str, Any]], float, str | None]:
        s_idx = max(0, int(round(s_sec * sample_rate)))
        e_idx = min(proc_audio.size, int(round(e_sec * sample_rate)))
        clip = proc_audio[s_idx:e_idx]
        if clip.size < int(sample_rate * 0.1):
            return idx, [], 0.0, None

        wav_bytes = encode_float32_to_wav_bytes(clip, sample_rate=sample_rate)
        form_data: dict[str, Any] = {
            "model": model,
            "response_format": "verbose_json",
            "timestamp_granularities[]": "word",
        }
        if language:
            form_data["language"] = language
        if hotwords.strip():
            form_data["prompt"] = f"Livestream transcript. Key terms: {hotwords.strip()}."

        status, payload, raw_text = _post_multipart_with_retry(
            url=base_url,
            headers=headers,
            files={"file": (f"batch_{idx}.wav", wav_bytes, "audio/wav")},
            data=form_data,
            timeout=timeout,
            retry_base_delay=retry_base_delay,
            http_post=http_post,
        )

        # 部分兼容服务商不支持 verbose_json 或 timestamp_granularities，自动降级为标准 json
        if status == 400:
            fallback_data: dict[str, Any] = {"model": model, "response_format": "json"}
            if language:
                fallback_data["language"] = language
            if hotwords.strip():
                fallback_data["prompt"] = form_data["prompt"]
            status, payload, raw_text = _post_multipart_with_retry(
                url=base_url,
                headers=headers,
                files={"file": (f"batch_{idx}.wav", wav_bytes, "audio/wav")},
                data=fallback_data,
                timeout=timeout,
                retry_base_delay=retry_base_delay,
                http_post=http_post,
            )

        if status >= 400:
            raise RuntimeError(f"云端 ASR 接口报错（HTTP {status}）：{raw_text[:240]}")

        billed = e_sec - s_sec
        det_lang = str(payload.get("language") or "").strip() or None
        raw_segs = payload.get("segments")
        raw_words = payload.get("words") if isinstance(payload.get("words"), list) else None

        parsed_segs: list[dict[str, Any]] = []
        if isinstance(raw_segs, list) and raw_segs:
            for seg in raw_segs:
                if not isinstance(seg, dict):
                    continue
                txt = str(seg.get("text") or "").strip()
                if not txt:
                    continue
                seg_s = round(s_sec + max(0.0, float(seg.get("start", 0.0))), 2)
                seg_e = round(min(e_sec, s_sec + max(0.0, float(seg.get("end", billed)))), 2)
                if seg_e < seg_s:
                    seg_e = seg_s
                seg_words = None
                if isinstance(seg.get("words"), list):
                    seg_words = [
                        {
                            "word": str(w.get("word", "")),
                            "start": round(s_sec + float(w.get("start", 0.0)), 2),
                            "end": round(s_sec + float(w.get("end", 0.0)), 2),
                        }
                        for w in seg["words"]
                        if isinstance(w, dict) and str(w.get("word", "")).strip()
                    ]
                parsed_segs.append(
                    {"start_sec": seg_s, "end_sec": seg_e, "text": txt, "words": seg_words}
                )
        else:
            full_text = str(payload.get("text") or "").strip()
            if full_text:
                shifted_words = None
                if raw_words:
                    shifted_words = [
                        {
                            "word": str(w.get("word", "")),
                            "start": round(s_sec + float(w.get("start", 0.0)), 2),
                            "end": round(s_sec + float(w.get("end", 0.0)), 2),
                        }
                        for w in raw_words
                        if isinstance(w, dict) and str(w.get("word", "")).strip()
                    ]
                parsed_segs.append(
                    {
                        "start_sec": round(s_sec, 2),
                        "end_sec": round(e_sec, 2),
                        "text": full_text,
                        "words": shifted_words,
                    }
                )

        return idx, parsed_segs, billed, det_lang

    workers = max(1, min(int(concurrency), len(batched_chunks)))
    ordered_chunks: list[tuple[int, list[dict[str, Any]]]] = []
    total_billed = 0.0
    first_lang: str | None = language

    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [
            pool.submit(_call_one, idx, s_sec, e_sec)
            for idx, (s_sec, e_sec) in enumerate(batched_chunks)
        ]
        for fut in as_completed(futures):
            idx, segs, billed, det_lang = fut.result()
            total_billed += billed
            if first_lang is None and det_lang:
                first_lang = det_lang
            ordered_chunks.append((idx, segs))

    ordered_chunks.sort(key=lambda item: item[0])
    all_segments: list[dict[str, Any]] = []
    for _, segs in ordered_chunks:
        all_segments.extend(segs)

    return {
        "segments": all_segments,
        "billed_audio_sec": round(total_billed, 2),
        "request_count": len(batched_chunks),
        "detected_language": first_lang,
    }
