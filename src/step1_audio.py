"""Step 1：音频提取与高音量（情绪峰值）区间检测。

本模块负责整个高光切片流程的第一步：
1. 调用系统里的 FFmpeg，从任意格式的游戏录屏中抽取出 16kHz、单声道、16-bit PCM 的 WAV 音频文件
   （这个格式同时也是 Step 2 的 faster-whisper 最喜欢的标准输入，一次提取两步复用）。
2. 用标准库 wave 配合 numpy 按时间窗口（默认每 1 秒）流式读取音频，计算每个窗口的
   均方根能量（RMS，用来客观衡量人声/游戏音量的响度），避免几小时的大视频一次性读进内存卡死。
3. 根据百分比动态阈值与最低音量底线找出高音量片段，自动合并相近片段并做前后缓冲扩展，
   最后导出 JSON 报告和终端摘要，方便后续步骤使用。

命令行运行示例：
    python src/step1_audio.py input/你的游戏录屏.mp4
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import shutil
import subprocess
import sys
import threading
import uuid
import wave
from pathlib import Path
from typing import Any

import numpy as np

# 跟踪当前活跃的 FFmpeg / FFprobe 子进程，便于桌面端关窗时优雅清理防止僵尸进程占用文件
_ACTIVE_MEDIA_PROCS: set[subprocess.Popen[str]] = set()
_ACTIVE_PROCS_LOCK = threading.Lock()


def _normalize_media_cmd(cmd: list[str]) -> list[str]:
    """为 ffmpeg/ffprobe 自动注入 `-hide_banner` 与 `-nostdin`，消除交互阻塞并精简错误日志。"""
    if not cmd:
        return cmd
    exe_name = Path(cmd[0]).name.lower()
    args = list(cmd[1:])
    prefix: list[str] = []
    if exe_name in {"ffmpeg", "ffmpeg.exe"}:
        if "-hide_banner" not in args:
            prefix.append("-hide_banner")
        if "-nostdin" not in args:
            prefix.append("-nostdin")
    elif exe_name in {"ffprobe", "ffprobe.exe"}:
        if "-hide_banner" not in args:
            prefix.append("-hide_banner")
    return [cmd[0], *prefix, *args]


def run_media_subprocess(
    cmd: list[str],
    *,
    cwd: Path | None = None,
    timeout: float | None = None,
) -> subprocess.CompletedProcess[str]:
    """安全静默执行 FFmpeg/FFprobe 子进程（Windows 自动隐藏控制台窗口并禁用 stdin）。

    解决 Windows 无控制台 GUI（pythonw / PyInstaller --windowed）下直接调用 `subprocess.run`
    弹出黑窗并触发 `CTRL_CLOSE_EVENT` -> `SIGTERM (signal 15)` 截断媒体读取的核心问题。
    """
    final_cmd = _normalize_media_cmd(cmd)
    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if sys.platform == "win32" else 0
    proc = subprocess.Popen(
        final_cmd,
        cwd=str(cwd) if cwd is not None else None,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        creationflags=creationflags,
    )
    with _ACTIVE_PROCS_LOCK:
        _ACTIVE_MEDIA_PROCS.add(proc)
    try:
        stdout, stderr = proc.communicate(timeout=timeout)
        return subprocess.CompletedProcess(
            args=final_cmd,
            returncode=proc.returncode,
            stdout=stdout or "",
            stderr=stderr or "",
        )
    except subprocess.TimeoutExpired:
        try:
            proc.kill()
        except Exception:
            pass
        try:
            proc.communicate(timeout=3)
        except Exception:
            pass
        raise
    finally:
        with _ACTIVE_PROCS_LOCK:
            _ACTIVE_MEDIA_PROCS.discard(proc)


def terminate_active_media_processes() -> int:
    """优雅终止全部正在运行中的 FFmpeg/FFprobe 子进程（桌面窗口退出清理专用）。"""
    with _ACTIVE_PROCS_LOCK:
        procs = list(_ACTIVE_MEDIA_PROCS)
    killed = 0
    for proc in procs:
        if proc.poll() is not None:
            continue
        try:
            proc.terminate()
            killed += 1
        except Exception:
            pass
    for proc in procs:
        if proc.poll() is not None:
            continue
        try:
            proc.wait(timeout=1.5)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass
    return killed


def format_timestamp(seconds: float) -> str:
    """将秒数格式化为可读的时间字符串（MM:SS 或 HH:MM:SS）。

    参数：
        seconds: 视频中的时间点（单位：秒）。
    返回：
        例如 65.0 -> "01:05"，3665.0 -> "01:01:05"。
    """
    # 防止负值输入，并四舍五入到整秒，方便终端展示
    safe_seconds = max(0, int(round(seconds)))
    hours, remainder = divmod(safe_seconds, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours > 0:
        return f"{hours:02d}:{minutes:02d}:{secs:02d}"
    return f"{minutes:02d}:{secs:02d}"


def _resolve_bundled_binary(binary_name: str) -> str | None:
    """优先查找应用便携包内置的 `bin/ffmpeg.exe` 或 `bin/ffprobe.exe`（支持 PyInstaller 与源码目录）。"""
    exe_names = (f"{binary_name}.exe", binary_name) if sys.platform.startswith("win") else (binary_name,)
    candidate_roots: list[Path] = []

    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        candidate_roots.append(Path(meipass))
    if getattr(sys, "frozen", False):
        candidate_roots.append(Path(sys.executable).resolve().parent)
    candidate_roots.append(Path(__file__).resolve().parent.parent)

    for root in candidate_roots:
        for sub in ("bin", "ffmpeg", "."):
            for name in exe_names:
                p = root / sub / name if sub != "." else root / name
                if p.is_file():
                    return str(p.resolve())
    return None


def check_ffmpeg_available() -> str:
    """查找可用的 FFmpeg 可执行文件（优先便携包内置 `bin/ffmpeg.exe`，其次系统 PATH）。"""
    bundled = _resolve_bundled_binary("ffmpeg")
    if bundled:
        return bundled
    ffmpeg_path = shutil.which("ffmpeg")
    if not ffmpeg_path:
        raise RuntimeError(
            "未找到 ffmpeg（既未在应用 bin/ 目录找到内置版，也未在系统 PATH 中找到）。\n"
            "请确认使用完整便携包或已安装 FFmpeg，并在终端运行 `ffmpeg -version` 验证后再试。"
        )
    return ffmpeg_path


def check_ffprobe_available() -> str:
    """查找可用的 FFprobe 可执行文件（优先便携包内置 `bin/ffprobe.exe`，其次系统 PATH）。"""
    bundled = _resolve_bundled_binary("ffprobe")
    if bundled:
        return bundled
    ffprobe_path = shutil.which("ffprobe")
    if not ffprobe_path:
        raise RuntimeError(
            "未找到 ffprobe（既未在应用 bin/ 目录找到内置版，也未在系统 PATH 中找到）。\n"
            "请确认完整安装 FFmpeg 套件（包含 ffprobe），并在终端运行 `ffprobe -version` 验证。"
        )
    return ffprobe_path


_MIC_STREAM_KEYWORDS = (
    "mic",
    "voice",
    "vocal",
    "commentary",
    "host",
    "caster",
    "人声",
    "麦克",
    "麦轨",
    "主播",
    "解说",
)
_NON_MIC_STREAM_KEYWORDS = (
    "desktop",
    "game",
    "bgm",
    "music",
    "system",
    "mix",
    "all",
    "游戏",
    "桌面",
    "背景",
)
_GENERIC_HANDLER_NAMES = {"soundhandler", "audiohandler", "core media audio"}


def probe_audio_streams(video_path: str | Path) -> list[dict[str, Any]]:
    """探测视频文件中的全部音频轨道信息（支持 OBS 多音轨人声识别）。

    返回每条音轨的索引（audio_index，对应 ffmpeg `-map 0:a:<index>`）、编码格式、
    声道数、采样率、轨道名称，以及基于名称/声道特征的 `is_likely_mic` 推荐标记。
    """
    src_video = Path(video_path)
    if not src_video.is_file():
        raise FileNotFoundError(f"找不到输入视频文件：{src_video}")

    ffprobe_bin = check_ffprobe_available()
    cmd = [
        ffprobe_bin,
        "-v",
        "error",
        "-select_streams",
        "a",
        "-show_entries",
        "stream=index,codec_name,channels,channel_layout,sample_rate:stream_tags=title,language,handler_name",
        "-of",
        "json",
        str(src_video.resolve()),
    ]
    proc = run_media_subprocess(cmd, timeout=30)
    if proc.returncode != 0:
        stderr_lines = [line for line in proc.stderr.splitlines() if line.strip()]
        err_summary = "\n".join(stderr_lines[-8:]) if stderr_lines else "未知错误"
        raise RuntimeError(
            f"FFprobe 探测音轨失败（退出码 {proc.returncode}）。\n错误详情：\n{err_summary}"
        )

    try:
        payload = json.loads(proc.stdout or "{}")
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"FFprobe 输出解析失败：{exc}") from exc

    raw_streams = payload.get("streams") or []
    streams: list[dict[str, Any]] = []
    title_matched_any = False

    for audio_idx, raw in enumerate(raw_streams):
        tags = raw.get("tags") if isinstance(raw.get("tags"), dict) else {}
        raw_title = str(tags.get("title") or "").strip()
        if not raw_title:
            handler = str(tags.get("handler_name") or "").strip()
            if handler and handler.lower() not in _GENERIC_HANDLER_NAMES:
                raw_title = handler

        raw_lang = str(tags.get("language") or "").strip()
        language = "" if raw_lang.lower() == "und" else raw_lang

        try:
            channels = int(raw.get("channels") or 0)
        except (TypeError, ValueError):
            channels = 0
        try:
            sample_rate = int(raw.get("sample_rate") or 0)
        except (TypeError, ValueError):
            sample_rate = 0

        title_lower = raw_title.lower()
        has_mic_kw = any(kw in title_lower for kw in _MIC_STREAM_KEYWORDS)
        has_non_mic_kw = any(kw in title_lower for kw in _NON_MIC_STREAM_KEYWORDS)
        is_mic = bool(has_mic_kw and not has_non_mic_kw)
        if is_mic:
            title_matched_any = True

        streams.append(
            {
                "audio_index": audio_idx,
                "stream_index": int(raw.get("index", audio_idx)),
                "codec_name": str(raw.get("codec_name") or "unknown"),
                "channels": channels,
                "channel_layout": str(raw.get("channel_layout") or ""),
                "sample_rate": sample_rate,
                "title": raw_title,
                "language": language,
                "is_likely_mic": is_mic,
                "label": "",
            }
        )

    # 若多音轨没有任何轨道通过标题关键词命中，但恰好只有 1 条单声道（OBS 典型配置：
    # 轨 0 立体声桌面混音 + 轨 1 单声道麦克风），自动推荐该单声道作为纯人声麦轨
    if len(streams) >= 2 and not title_matched_any:
        mono_candidates = [s for s in streams if s["channels"] == 1]
        if len(mono_candidates) == 1:
            mono_candidates[0]["is_likely_mic"] = True

    for item in streams:
        display_title = item["title"] or "默认音轨"
        tech_parts = [item["codec_name"]]
        if item["channels"] > 0:
            tech_parts.append(f"{item['channels']}ch")
        if item["sample_rate"] > 0:
            tech_parts.append(f"{item['sample_rate']}Hz")
        if item["language"]:
            tech_parts.append(item["language"])
        rec_suffix = " [推荐·疑似人声麦轨]" if item["is_likely_mic"] else ""
        item["label"] = f"音轨 {item['audio_index']} · {display_title} ({' · '.join(tech_parts)}){rec_suffix}"

    return streams


def extract_audio_to_wav(
    video_path: str | Path,
    wav_path: str | Path,
    sample_rate: int = 16000,
    audio_stream: int = 0,
) -> Path:
    """调用 FFmpeg 从指定音轨提取 16kHz、单声道、16-bit PCM 的标准全频段 WAV 音频。

    注意：这里故意不做 300~3400Hz 窄带裁剪，因为 Step 2 的 Whisper 需要保留高频清辅音
    （如 s/f/th/sh）才能获得最高英文/多语种识别准确率；人声带通滤波仅在计算 RMS 峰值时进行。

    参数：
        video_path: 原始游戏录屏视频路径。
        wav_path: 输出的 WAV 音频文件路径。
        sample_rate: 目标采样率（Hz），默认 16000（Whisper 模型标准采样率）。
        audio_stream: 选取视频的第几条音轨（0 起算，对应 `-map 0:a:<index>`）。
    返回：
        生成的 WAV 文件 Path 对象。
    """
    src_video = Path(video_path)
    dst_wav = Path(wav_path)

    if not src_video.is_file():
        raise FileNotFoundError(f"找不到输入视频文件：{src_video}")
    if sample_rate <= 0:
        raise ValueError(f"sample_rate 必须为正整数，当前传入：{sample_rate}")
    if audio_stream < 0:
        raise ValueError(f"audio_stream 必须为大于等于 0 的整数，当前传入：{audio_stream}")

    # 确保 ffmpeg 可用，并自动创建输出目录（例如 output/）
    ffmpeg_bin = check_ffmpeg_available()
    dst_wav.parent.mkdir(parents=True, exist_ok=True)

    # 写入同目录临时 .wav 文件后再原子替换，避免中途失败遗留半截 WAV 骗过缓存检查
    tmp_wav = dst_wav.parent / f".{dst_wav.stem}.part_{uuid.uuid4().hex[:8]}.wav"

    # 构造 FFmpeg 命令参数说明：
    # -y            : 如果目标 WAV 已存在则直接覆盖，不卡在终端询问
    # -i            : 输入视频路径
    # -map 0:a:<n>  : 精准选取指定音轨（适配 OBS 多音轨：例如纯人声麦轨）
    # -vn           : 丢弃视频画面，只处理音频（提取速度极快）
    # -ac 1         : 混音转为 1 个单声道（Mono）
    # -ar 16000     : 重采样为 16000 Hz
    # -c:a pcm_s16le: 编码为标准 16-bit 有符号小端 PCM（Python wave 库可直接读取）
    cmd = [
        ffmpeg_bin,
        "-y",
        "-i",
        str(src_video.resolve()),
        "-map",
        f"0:a:{int(audio_stream)}",
        "-vn",
        "-ac",
        "1",
        "-ar",
        str(sample_rate),
        "-c:a",
        "pcm_s16le",
        str(tmp_wav),
    ]

    try:
        # 使用统一静默子进程封装（Windows 自动无黑窗 + -nostdin + -hide_banner）
        process = run_media_subprocess(cmd)

        if process.returncode != 0:
            # 只截取最后 12 行错误日志，避免刷屏，同时保留足够排查线索
            stderr_lines = [line for line in process.stderr.splitlines() if line.strip()]
            err_summary = "\n".join(stderr_lines[-12:]) if stderr_lines else "未知错误"
            raise RuntimeError(
                f"FFmpeg 提取第 {audio_stream} 号音轨失败（退出码 {process.returncode}）。\n"
                f"错误详情：\n{err_summary}"
            )

        if not tmp_wav.is_file() or tmp_wav.stat().st_size <= 44:
            # WAV 文件头固定占 44 字节，如果小于等于 44 字节说明没有实际音频数据
            raise RuntimeError(
                f"生成的 WAV 文件为空或无效（{dst_wav}），请检查原视频第 {audio_stream} 号音轨是否包含有效音频。"
            )

        os.replace(tmp_wav, dst_wav)
    finally:
        if tmp_wav.exists():
            tmp_wav.unlink(missing_ok=True)

    return dst_wav


def _build_bandpass_fft_plan(
    num_samples: int,
    sample_rate: int,
    low_hz: float,
    high_hz: float,
) -> tuple[np.ndarray, np.ndarray]:
    """预计算指定样本长度下用于 Parseval 频域带通 RMS 的频率掩码与单边谱权重。"""
    freqs = np.fft.rfftfreq(num_samples, d=1.0 / float(sample_rate))
    band_mask = (freqs >= low_hz) & (freqs <= high_hz)

    # 实信号 rfft 单边功率谱能量权重：DC(0) 权 1，偶数长度奈奎斯特频点权 1，其余正负对称频点权 2
    weights = np.full(freqs.size, 2.0, dtype=np.float64)
    if weights.size > 0:
        weights[0] = 1.0
        if num_samples % 2 == 0 and weights.size > 1:
            weights[-1] = 1.0
    return band_mask, weights


def compute_per_second_rms(
    wav_path: str | Path,
    window_sec: float = 1.0,
    vocal_bandpass: bool = True,
    low_hz: float = 300.0,
    high_hz: float = 3400.0,
) -> tuple[np.ndarray, int, float]:
    """流式读取 WAV 音频并计算每个时间窗口的均方根音量（RMS）。

    当 `vocal_bandpass=True`（默认）时，通过纯 NumPy FFT（Parseval 定理）只统计
    300Hz~3400Hz 核心人声频段的均方能量，从而过滤掉 150Hz 以下游戏低频爆炸/枪炮
    与超高频 BGM 刺耳杂音，避免假高潮抢占高光峰值名额，且完全不改动原始 WAV 文件。

    参数：
        wav_path: 16-bit PCM 格式的 WAV 文件路径。
        window_sec: 每个统计窗口的长度（秒），默认 1.0 秒。
        vocal_bandpass: 是否开启 300~3400Hz 人声核心频段带通滤波测算（默认 True）。
        low_hz: 带通下限频率（Hz，默认 300.0）。
        high_hz: 带通上限频率（Hz，默认 3400.0）。
    返回：
        (rms_values, sample_rate, total_duration_sec)
        - rms_values: 一维 numpy 数组，每个元素是对应窗口的归一化 RMS（范围 0.0 ~ 1.0）
        - sample_rate: 音频实际采样率
        - total_duration_sec: 音频总时长（秒）
    """
    src_wav = Path(wav_path)
    if not src_wav.is_file():
        raise FileNotFoundError(f"找不到音频文件：{src_wav}")
    if window_sec <= 0:
        raise ValueError(f"window_sec 必须大于 0，当前传入：{window_sec}")
    if vocal_bandpass and (low_hz < 0 or high_hz <= low_hz):
        raise ValueError(f"频段带通参数非法：必须满足 0 <= low_hz < high_hz，当前为 [{low_hz}, {high_hz}]")

    rms_list: list[float] = []

    with wave.open(str(src_wav), "rb") as wf:
        sample_rate = wf.getframerate()
        channels = wf.getnchannels()
        sample_width = wf.getsampwidth()
        total_frames = wf.getnframes()

        # 16-bit PCM 每个采样点占 2 个字节；若不是 2 字节则提前提示
        if sample_width != 2:
            raise ValueError(
                f"仅支持 16-bit PCM WAV（sampwidth=2），当前文件 sampwidth={sample_width}"
            )
        if sample_rate <= 0:
            raise ValueError(f"WAV 文件采样率异常：{sample_rate}")

        total_duration_sec = total_frames / float(sample_rate)
        if total_frames == 0:
            return np.array([], dtype=np.float64), sample_rate, 0.0

        # 每个时间窗口包含的采样帧数（例如 16000Hz * 1.0s = 16000 帧）
        frames_per_window = max(1, int(round(sample_rate * window_sec)))
        # 视频末尾如果剩下不到 10% 窗口长度的零头采样，就不单独作为一个窗口，避免边缘误差
        min_tail_frames = max(1, int(0.1 * frames_per_window))

        cached_n = -1
        cached_mask: np.ndarray | None = None
        cached_weights: np.ndarray | None = None

        while True:
            raw_bytes = wf.readframes(frames_per_window)
            if not raw_bytes:
                break

            actual_frames = len(raw_bytes) // (sample_width * channels)
            # 只有末尾残片极短且前面已经有统计块时才跳过；如果是整段都很短的视频则保留唯一一块
            if actual_frames < frames_per_window and actual_frames < min_tail_frames and rms_list:
                break

            # 将原始二进制字节解析为 int16 整数数组（范围 -32768 ~ 32767），
            # 再除以 32768.0 归一化到 [-1.0, 1.0]，避免平方求和时整数溢出
            samples = np.frombuffer(raw_bytes, dtype=np.int16).astype(np.float64) / 32768.0
            if samples.size == 0:
                break

            # 容错处理：如果外部直接传了双声道 WAV 进来，先取各声道平均变成单声道
            if channels > 1:
                samples = samples.reshape(-1, channels).mean(axis=1)

            if vocal_bandpass:
                n = samples.size
                if n != cached_n:
                    cached_mask, cached_weights = _build_bandpass_fft_plan(
                        n, sample_rate, low_hz, high_hz
                    )
                    cached_n = n
                assert cached_mask is not None and cached_weights is not None
                # 先去直流偏置，再用 Parseval 定理累加 [low_hz, high_hz] 频带能量
                centered = samples - float(np.mean(samples))
                spec = np.fft.rfft(centered)
                power = (spec.real * spec.real + spec.imag * spec.imag) * cached_weights
                band_energy = float(np.sum(power[cached_mask]))
                band_ms = band_energy / (float(n) * float(n))
                rms_value = float(np.sqrt(max(0.0, band_ms)))
            else:
                # 全频段时域 RMS 公式：sqrt(mean(x^2))
                rms_value = float(np.sqrt(np.mean(samples * samples)))

            rms_list.append(rms_value)

    return np.array(rms_list, dtype=np.float64), sample_rate, total_duration_sec


def detect_high_volume_segments(
    rms_values: np.ndarray,
    window_sec: float = 1.0,
    top_percent: float = 15.0,
    min_rms: float = 0.01,
    merge_gap_sec: float = 2.0,
    pad_sec: float = 1.0,
    total_duration: float | None = None,
) -> tuple[float, list[dict[str, Any]]]:
    """根据音量 RMS 序列，找出并合并高音量（情绪高能）时间段。

    算法思路：
    1. 计算动态阈值：取全部窗口音量前 top_percent% 的分位数，并与硬底线 min_rms 取较大值。
       （这样就算整场录像几乎静音，也不会把 0.0001 的底噪误判成高光）
    2. 找出所有音量 >= 阈值 的时间窗口。
    3. 把连续或间隔很近（<= merge_gap_sec 秒）的高能窗口合并成同一个片段，防止一句笑声被切碎。
    4. 为每个片段前后各扩展 pad_sec 秒缓冲，让剪出来的台词有完整开头和结尾；若扩展后重叠再合并一次。

    参数：
        rms_values: compute_per_second_rms 返回的一维 RMS 数组。
        window_sec: 每个元素的时长（秒）。
        top_percent: 取响度排名前多少百分比的窗口（默认 15.0，即前 15%）。
        min_rms: 最低 RMS 门槛（默认 0.01），低于此值的纯静音/小底噪绝不算高光。
        merge_gap_sec: 相邻高光间隔小于等于该秒数时自动合并（默认 2.0 秒）。
        pad_sec: 片段前后额外保留的缓冲秒数（默认 1.0 秒）。
        total_duration: 视频总时长（秒）；若为 None 则由窗口数估算。
    返回：
        (实际使用的 RMS 阈值, 高音量片段字典列表)
    """
    if window_sec <= 0:
        raise ValueError(f"window_sec 必须大于 0，当前传入：{window_sec}")
    if not (0 < top_percent <= 100):
        raise ValueError(f"top_percent 必须在 (0, 100] 范围内，当前传入：{top_percent}")
    if merge_gap_sec < 0 or pad_sec < 0:
        raise ValueError("merge_gap_sec 和 pad_sec 不能为负数")

    if rms_values.size == 0:
        return 0.0, []

    duration = float(rms_values.size * window_sec) if total_duration is None else float(total_duration)
    if duration <= 0:
        return 0.0, []

    # 1. 计算动态阈值：例如 top_percent=15 表示取第 85 百分位数（即最大的 15%）
    percentile_threshold = float(np.percentile(rms_values, 100.0 - top_percent))
    threshold = max(percentile_threshold, min_rms)

    # 2. 找到所有达到阈值的高音量窗口下标
    hit_indices = np.where(rms_values >= threshold)[0]
    if hit_indices.size == 0:
        return round(threshold, 4), []

    # 3. 先把命中的单个窗口转换成时间区间，并按 merge_gap_sec 合并相近区间
    merged_raw: list[list[float]] = []
    for idx in hit_indices:
        win_start = float(idx) * window_sec
        win_end = min(duration, float(idx + 1) * window_sec)
        if win_end <= win_start:
            continue

        if not merged_raw:
            merged_raw.append([win_start, win_end])
        else:
            prev_end = merged_raw[-1][1]
            if win_start - prev_end <= merge_gap_sec:
                # 间隔足够短，延伸上一个区间的结束时间
                merged_raw[-1][1] = max(prev_end, win_end)
            else:
                merged_raw.append([win_start, win_end])

    # 4. 前后各扩展 pad_sec 秒，并限制在 [0, duration] 合法区间内；扩展后若重叠再合并一次
    padded_merged: list[list[float]] = []
    for raw_start, raw_end in merged_raw:
        ext_start = max(0.0, raw_start - pad_sec)
        ext_end = min(duration, raw_end + pad_sec)
        if not padded_merged:
            padded_merged.append([ext_start, ext_end])
        elif ext_start <= padded_merged[-1][1]:
            padded_merged[-1][1] = max(padded_merged[-1][1], ext_end)
        else:
            padded_merged.append([ext_start, ext_end])

    # 5. 计算每个最终区间的峰值音量、平均音量与可读时间戳，组装最终输出列表
    segments: list[dict[str, Any]] = []
    for seg_start, seg_end in padded_merged:
        idx_start = max(0, int(math.floor(seg_start / window_sec)))
        idx_end = min(int(rms_values.size), int(math.ceil(seg_end / window_sec)))
        if idx_end <= idx_start:
            idx_end = min(int(rms_values.size), idx_start + 1)

        seg_slice = rms_values[idx_start:idx_end]
        start_rounded = round(seg_start, 2)
        end_rounded = round(seg_end, 2)

        segments.append(
            {
                "start_sec": start_rounded,
                "end_sec": end_rounded,
                "duration_sec": round(end_rounded - start_rounded, 2),
                "start_str": format_timestamp(seg_start),
                "end_str": format_timestamp(seg_end),
                "peak_rms": round(float(np.max(seg_slice)), 4),
                "mean_rms": round(float(np.mean(seg_slice)), 4),
            }
        )

    return round(threshold, 4), segments


def detect_dead_air_segments(
    rms_curve: np.ndarray,
    hop_sec: float,
    total_duration_sec: float,
    min_dead_air_sec: float = 15.0,
) -> list[dict[str, Any]]:
    """检测长时间挂机/黑屏加载/离席导致的死寂长空白段（Dead Air Map）。

    设计红线：
    - 严格尊重剪辑师社区底线，绝不将 0.5~5 秒的正常说话停顿或喜剧气口标成空白；
    - 仅标记持续时长 `>= min_dead_air_sec`（默认 15.0 秒）且能量显著低于语音活跃底噪的连续死寂段，
      供前端时间线灰显并提供「审片自动跳过长空白」辅助开关。

    参数：
        rms_curve: 一维 RMS 能量序列（归一化 0.0 ~ 1.0）。
        hop_sec: 相邻 RMS 采样点的步长（秒，通常等于 `window_sec`）。
        total_duration_sec: 音视频总时长（秒）。
        min_dead_air_sec: 判定为死寂长空白段的最小连续秒数（默认 15.0）。
    返回：
        死寂段列表，每条包含 `start_sec`, `end_sec`, `duration_sec`, `start_str`, `end_str`。
    """
    try:
        hop_val = float(hop_sec)
        min_dead_val = float(min_dead_air_sec)
        dur_val = 0.0 if total_duration_sec is None else float(total_duration_sec)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"死寂段检测参数必须为有限数字：{exc}") from exc

    if not math.isfinite(hop_val) or hop_val <= 0.0:
        raise ValueError(f"hop_sec 必须为有限正数，当前传入：{hop_sec}")
    if not math.isfinite(min_dead_val) or min_dead_val <= 0.0:
        raise ValueError(f"min_dead_air_sec 必须为有限正数，当前传入：{min_dead_air_sec}")
    if not math.isfinite(dur_val) or dur_val < 0.0:
        raise ValueError(f"total_duration_sec 必须为有限非负数，当前传入：{total_duration_sec}")

    arr = np.asarray(rms_curve, dtype=np.float64).ravel()
    if arr.size == 0 or dur_val <= 0.0 or dur_val < min_dead_val:
        return []

    arr = np.where(np.isfinite(arr), np.maximum(arr, 0.0), 0.0)

    # 自适应死寂阈值：基于活跃语音中位响度设定相对低阈并钳制安全上限，不误伤轻声或低增益主播
    max_val = float(np.max(arr))
    active_vals = arr[arr >= 0.01]
    if active_vals.size > 0:
        speech_median = float(np.median(active_vals))
        dead_threshold = min(0.012, max(0.003, speech_median * 0.12))
    elif max_val >= 0.005:
        active_p75 = float(np.percentile(arr, 75))
        dead_threshold = min(0.006, max(0.0015, active_p75 * 0.20))
    elif max_val > 1e-4:
        # 低增益轻声主播：走相对底噪比例（p50 * 0.25），绝不硬编码 0.005 误杀全片
        p50 = float(np.percentile(arr, 50))
        if p50 > 1e-4:
            ref_level = p50
        else:
            nonzero_vals = arr[arr > 1e-4]
            ref_level = float(np.median(nonzero_vals)) if nonzero_vals.size > 0 else max_val
        dead_threshold = max(1e-5, min(max_val * 0.45, ref_level * 0.25))
    else:
        # 全片真实纯静音（能量 <= 1e-4）
        dead_threshold = 1e-4

    is_dead = arr <= dead_threshold
    segments: list[dict[str, Any]] = []
    run_start: int | None = None

    def _flush_run(start_idx: int, end_idx: int) -> None:
        seg_start = float(start_idx) * hop_val
        seg_end = min(dur_val, float(end_idx + 1) * hop_val)
        if seg_end <= seg_start:
            return
        start_rounded = round(seg_start, 2)
        end_rounded = round(seg_end, 2)
        dur_rounded = round(max(0.0, end_rounded - start_rounded), 2)
        if dur_rounded + 1e-6 >= min_dead_val:
            segments.append(
                {
                    "start_sec": start_rounded,
                    "end_sec": end_rounded,
                    "duration_sec": dur_rounded,
                    "start_str": format_timestamp(seg_start),
                    "end_str": format_timestamp(seg_end),
                }
            )

    for idx, quiet in enumerate(is_dead):
        if quiet:
            if run_start is None:
                run_start = idx
        elif run_start is not None:
            _flush_run(run_start, idx - 1)
            run_start = None

    if run_start is not None:
        _flush_run(run_start, int(arr.size) - 1)

    return segments


def diagnose_stream_energy(
    peak_segments: list[dict[str, Any]],
    total_duration_sec: float,
    rms_threshold: float,
) -> dict[str, Any]:
    """根据 Step 1 音频峰值分布，快速诊断本场直播状态评级、黄金时段分布并给出推荐参数。

    用于大公会操作员在 30 秒内判定：本场是值得重点精剪的“神回场”，还是平淡摸鱼/忘开麦的废片，
    同时自动根据直播总时长计算出最省时省钱的 Step 2 推荐配置。
    """
    duration = max(1.0, float(total_duration_sec or 0.0))
    thr = float(rms_threshold or 0.0)
    hours = duration / 3600.0

    # 1. 根据总时长划分基础推荐参数
    if duration < 1800.0:
        rec_mode = "full"
        base_k = 10
        deep_k = 15
        dur_hint = "30分钟以内短录像，推荐全量转写以免漏掉轻声金句"
    elif duration <= 5400.0:
        rec_mode = "peaks"
        base_k = 15
        deep_k = 24
        dur_hint = "0.5~1.5小时标准场，推荐抓 Top 15 峰值上下文"
    elif duration <= 10800.0:
        rec_mode = "peaks"
        base_k = 24
        deep_k = 36
        dur_hint = "1.5~3小时常规场，推荐抓 Top 24 峰值上下文"
    elif duration <= 18000.0:
        rec_mode = "peaks"
        base_k = 25
        deep_k = 40
        dur_hint = "3~5小时长直播，日更抓 Top 25 即可，深挖可调到 40"
    else:
        rec_mode = "peaks"
        base_k = min(60, max(30, round(hours * 8)))
        deep_k = min(80, max(45, round(hours * 11)))
        dur_hint = f"{hours:.1f}小时超长马拉松场，按每小时约 8 个锚点提取"

    if not peak_segments or thr <= 0.0:
        bucket_size = 600.0 if duration <= 3600.0 else 3600.0
        num_buckets = max(1, int(math.ceil(duration / bucket_size)))
        empty_buckets = [
            {
                "label": f"{i * 10}-{(i + 1) * 10}m" if bucket_size == 600.0 else f"{i}-{i + 1}h",
                "start_sec": round(i * bucket_size, 1),
                "end_sec": round(min(duration, (i + 1) * bucket_size), 1),
                "total_peaks": 0,
                "strong_peaks": 0,
                "max_rms": 0.0,
                "heat_pct": 0,
            }
            for i in range(num_buckets)
        ]
        return {
            "grade": "WARN",
            "grade_label": "异常·静音或无有效动态",
            "advice": "未检测到有效高音量波动或音轨近乎静音，疑似忘开麦或黑屏挂机，请先试听原片再决定是否跑 AI。",
            "burst_ratio": 0.0,
            "max_burst_ratio": 0.0,
            "max_peak_rms": 0.0,
            "top10_min_rms": 0.0,
            "strong_threshold": 0.0,
            "strong_peak_count": 0,
            "strong_peaks_per_hour": 0.0,
            "golden_bucket_label": empty_buckets[0]["label"],
            "golden_start_sec": 0.0,
            "hourly_heatmap": empty_buckets,
            "recommendation": {
                "asr_mode": rec_mode,
                "top_k_peaks": int(base_k),
                "deep_top_k_peaks": int(deep_k),
                "top_percent": 15.0,
                "reason": dur_hint,
            },
        }

    # 2. 计算核心情绪爆发比（Top 10 最低峰值 / 日常 15% 阈值）与强峰密度
    sorted_peaks = sorted((float(s.get("peak_rms", 0.0)) for s in peak_segments), reverse=True)
    max_peak_rms = sorted_peaks[0]
    top10_min_rms = sorted_peaks[:10][-1]
    burst_ratio = round(top10_min_rms / thr, 2)
    max_burst_ratio = round(max_peak_rms / thr, 2)

    strong_threshold = max(thr * 2.7, 0.08)
    strong_segs = [s for s in peak_segments if float(s.get("peak_rms", 0.0)) >= strong_threshold]
    strong_peak_count = len(strong_segs)
    strong_peaks_per_hour = round(strong_peak_count / max(hours, 0.1), 1)

    # 3. 判定直播状态等级
    if thr < 0.015 or burst_ratio < 1.6:
        grade = "WARN"
        grade_label = "异常·静音或动态过平"
        advice = "音量均值偏低或起伏极弱（疑似未录进人声或整场平淡挂机），建议先点击预览抽听几处再跑大模型。"
    elif burst_ratio >= 3.2 or (burst_ratio >= 2.8 and strong_peaks_per_hour >= 8.0):
        grade = "S"
        grade_label = "S级·神回爆点场"
        advice = "情绪爆发极具张力且强高光密集！闭眼重点精剪，可适当拉大提取高峰多产几条短视频。"
    elif burst_ratio >= 2.4:
        grade = "A"
        grade_label = "A级·常规优质场"
        advice = "音频情绪起伏健康，高光分布正常，按右侧推荐峰值运行即可稳定出片。"
    else:
        grade = "B"
        grade_label = "B级·平淡或压限场"
        advice = "爆发力偏柔和，可能是聊天局或 OBS 开启了强力防炸麦限幅；建议调高提取高峰或直接全量转写。"

    # 若为 B 级平淡/限幅场，自动将推荐模式向深挖/全量倾斜以免漏掉口播梗
    if grade == "B":
        if duration < 7200.0:
            rec_mode = "full"
            dur_hint = "本场动态偏平且在2小时内，已切换推荐全量转写以防漏掉轻声金句"
        else:
            base_k = min(60, round(base_k * 1.4))
            deep_k = min(80, round(deep_k * 1.4))
            dur_hint = "本场动态偏平，已将推荐高峰数自动上浮 40% 以扩大捞梗范围"

    # 4. 计算分段/分小时热度柱状图
    bucket_size = 600.0 if duration <= 3600.0 else 3600.0
    num_buckets = max(1, int(math.ceil(duration / bucket_size)))
    buckets: list[dict[str, Any]] = [
        {
            "label": f"{i * 10}-{(i + 1) * 10}m" if bucket_size == 600.0 else f"{i}-{i + 1}h",
            "start_sec": round(i * bucket_size, 1),
            "end_sec": round(min(duration, (i + 1) * bucket_size), 1),
            "total_peaks": 0,
            "strong_peaks": 0,
            "max_rms": 0.0,
            "heat_pct": 0,
        }
        for i in range(num_buckets)
    ]

    for seg in peak_segments:
        s_start = float(seg.get("start_sec", 0.0))
        p_rms = float(seg.get("peak_rms", 0.0))
        b_idx = min(num_buckets - 1, max(0, int(s_start // bucket_size)))
        b = buckets[b_idx]
        b["total_peaks"] += 1
        if p_rms >= strong_threshold:
            b["strong_peaks"] += 1
        if p_rms > b["max_rms"]:
            b["max_rms"] = round(p_rms, 4)

    max_strong = max(b["strong_peaks"] for b in buckets)
    max_total = max(b["total_peaks"] for b in buckets)
    for b in buckets:
        if max_strong > 0:
            b["heat_pct"] = round((b["strong_peaks"] / max_strong) * 100)
        elif max_total > 0:
            b["heat_pct"] = round((b["total_peaks"] / max_total) * 100)

    golden = max(buckets, key=lambda b: (b["strong_peaks"], b["max_rms"], b["total_peaks"]))

    return {
        "grade": grade,
        "grade_label": grade_label,
        "advice": advice,
        "burst_ratio": burst_ratio,
        "max_burst_ratio": max_burst_ratio,
        "max_peak_rms": round(max_peak_rms, 4),
        "top10_min_rms": round(top10_min_rms, 4),
        "strong_threshold": round(strong_threshold, 4),
        "strong_peak_count": strong_peak_count,
        "strong_peaks_per_hour": strong_peaks_per_hour,
        "golden_bucket_label": golden["label"],
        "golden_start_sec": golden["start_sec"],
        "hourly_heatmap": buckets,
        "recommendation": {
            "asr_mode": rec_mode,
            "top_k_peaks": int(base_k),
            "deep_top_k_peaks": int(deep_k),
            "top_percent": 15.0,
            "reason": dur_hint,
        },
    }


def analyze_video_audio(
    video_path: str | Path,
    output_dir: str | Path = "output",
    window_sec: float = 1.0,
    top_percent: float = 15.0,
    min_rms: float = 0.01,
    merge_gap_sec: float = 2.0,
    pad_sec: float = 1.0,
    audio_stream: int = 0,
    vocal_bandpass: bool = True,
    bandpass_low_hz: float = 300.0,
    bandpass_high_hz: float = 3400.0,
    min_dead_air_sec: float = 15.0,
) -> dict[str, Any]:
    """一键执行 Step 1 完整流程：探测多音轨 -> 提取 WAV -> 带通计算 RMS -> 高能与死寂段检测 -> 输出 JSON。

    参数：
        video_path: 源视频文件路径。
        output_dir: WAV 与 JSON 报告的保存目录（默认 "output"）。
        window_sec: 统计窗口秒数（默认 1.0）。
        top_percent: 高能音量百分比阈值（默认 15.0）。
        min_rms: 静音过滤最低 RMS 门槛（默认 0.01）。
        merge_gap_sec: 合并相邻高光的最大间隔秒数（默认 2.0）。
        pad_sec: 高光片段前后扩展缓冲秒数（默认 1.0）。
        audio_stream: 选取视频的第几条音轨（0 起算，默认 0）。
        vocal_bandpass: 是否开启 300~3400Hz 核心人声频段带通测算 RMS（默认 True）。
        bandpass_low_hz: 人声带通下限频率（默认 300.0 Hz）。
        bandpass_high_hz: 人声带通上限频率（默认 3400.0 Hz）。
        min_dead_air_sec: 死寂长空白段最小连续秒数（默认 15.0，绝不标记短停顿）。
    返回：
        包含元信息、音轨列表、阈值、peak_segments 与 dead_air_segments 的完整字典。
    """
    src_video = Path(video_path)
    if not src_video.is_file():
        raise FileNotFoundError(f"找不到输入视频文件：{src_video}")
    if audio_stream < 0:
        raise ValueError(f"audio_stream 必须为大于等于 0 的整数，当前传入：{audio_stream}")

    # 预先探测视频内的多音轨列表（OBS 多音轨视频可用于精确选人声轨与合法性校验）
    audio_streams: list[dict[str, Any]] = []
    try:
        audio_streams = probe_audio_streams(src_video)
    except (RuntimeError, OSError):
        audio_streams = []

    if audio_streams and audio_stream >= len(audio_streams):
        raise ValueError(
            f"音轨索引 audio_stream={audio_stream} 超出范围：该视频共包含 {len(audio_streams)} 条音轨"
            f"（合法范围为 0 ~ {len(audio_streams) - 1}）。"
        )

    out_dir = Path(output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    stem = src_video.stem
    wav_path = out_dir / f"{stem}_16k.wav"
    json_path = out_dir / f"{stem}_audio_peaks.json"

    # 第一步：用 FFmpeg 从指定音轨抽取出 16kHz 单声道全频段 WAV（保留清辅音给 Whisper）
    extract_audio_to_wav(
        src_video,
        wav_path,
        sample_rate=16000,
        audio_stream=audio_stream,
    )

    # 第二步：流式计算每个窗口的人声频段（或全频段）RMS 能量
    rms_values, sample_rate, total_duration_sec = compute_per_second_rms(
        wav_path,
        window_sec=window_sec,
        vocal_bandpass=vocal_bandpass,
        low_hz=bandpass_low_hz,
        high_hz=bandpass_high_hz,
    )

    # 第三步：识别并合并高音量时间段
    threshold, segments = detect_high_volume_segments(
        rms_values=rms_values,
        window_sec=window_sec,
        top_percent=top_percent,
        min_rms=min_rms,
        merge_gap_sec=merge_gap_sec,
        pad_sec=pad_sec,
        total_duration=total_duration_sec,
    )

    # 第三步半：检测长时间死寂空白段（Dead Air Map，绝不误标正常说话停顿）
    dead_air_segments = detect_dead_air_segments(
        rms_curve=rms_values,
        hop_sec=window_sec,
        total_duration_sec=total_duration_sec,
        min_dead_air_sec=min_dead_air_sec,
    )
    dead_air_total_sec = round(sum(float(seg["duration_sec"]) for seg in dead_air_segments), 2)

    # 第四步：生成直播状态诊断评级与推荐参数
    diagnostics = diagnose_stream_energy(
        peak_segments=segments,
        total_duration_sec=total_duration_sec,
        rms_threshold=threshold,
    )

    src_stat = src_video.stat()
    result: dict[str, Any] = {
        "video_path": str(src_video.resolve()),
        "video_size": int(src_stat.st_size),
        "video_mtime_ns": int(src_stat.st_mtime_ns),
        "wav_path": str(wav_path.resolve()),
        "json_path": str(json_path.resolve()),
        "sample_rate": sample_rate,
        "audio_stream": int(audio_stream),
        "vocal_bandpass": bool(vocal_bandpass),
        "bandpass_hz": [float(bandpass_low_hz), float(bandpass_high_hz)] if vocal_bandpass else None,
        "audio_streams": audio_streams,
        "total_duration_sec": round(total_duration_sec, 2),
        "total_duration_str": format_timestamp(total_duration_sec),
        "window_sec": window_sec,
        "top_percent": top_percent,
        "min_rms": min_rms,
        "rms_threshold": threshold,
        "segment_count": len(segments),
        "dead_air_min_sec": float(min_dead_air_sec),
        "dead_air_count": len(dead_air_segments),
        "dead_air_total_sec": dead_air_total_sec,
        "dead_air_segments": dead_air_segments,
        "diagnostics": diagnostics,
        "peak_segments": segments,
    }

    # 将分析报告原子写成格式化中文 JSON（禁用 NaN/Infinity），供人眼检查和 Step 2 直接读取
    tmp_json = json_path.with_name(f".{json_path.name}.{uuid.uuid4().hex[:8]}.tmp")
    try:
        tmp_json.write_text(
            json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False),
            encoding="utf-8",
        )
        os.replace(tmp_json, json_path)
    finally:
        if tmp_json.exists():
            tmp_json.unlink(missing_ok=True)

    return result


# 兼容别名：方便外部模块以 detect_audio_peaks() 调用 Step 1 音频高能与死寂段联合分析
detect_audio_peaks = analyze_video_audio


_SMPTE_TC_RE = re.compile(r"^\s*(\d{1,2}):(\d{2}):(\d{2})([:;])(\d{2,3})\s*$")


def _parse_ffprobe_rate(rate_str: str | None) -> float:
    """安全解析 ffprobe 的帧率字符串（如 '60000/1001'、'30/1'），异常返回 0.0。"""
    text = str(rate_str or "").strip()
    if not text or text in {"0/0", "N/A"}:
        return 0.0
    if "/" in text:
        num_s, _, den_s = text.partition("/")
        try:
            num, den = float(num_s), float(den_s)
            if not (math.isfinite(num) and math.isfinite(den)) or den <= 0.0:
                return 0.0
            val = num / den
            return val if math.isfinite(val) else 0.0
        except ValueError:
            return 0.0
    try:
        val = float(text)
        return val if math.isfinite(val) else 0.0
    except ValueError:
        return 0.0


def _parse_smpte_timecode_to_sec(tc_str: str | None, fps: float) -> tuple[str, float]:
    """解析 SMPTE 起始时间码字符串（HH:MM:SS:FF / HH:MM:SS;FF）并返回 (规范时间码, 起始秒数)。"""
    if not tc_str:
        return "", 0.0
    match = _SMPTE_TC_RE.match(str(tc_str))
    if not match:
        return "", 0.0

    hh = int(match.group(1))
    mm = int(match.group(2))
    ss = int(match.group(3))
    sep = match.group(4)
    ff = int(match.group(5))
    if mm >= 60 or ss >= 60:
        return "", 0.0

    safe_fps = fps if (math.isfinite(fps) and 1.0 <= fps <= 240.0) else 30.0
    tb = max(1, int(round(safe_fps)))
    if ff >= max(tb, 1):
        ff = min(ff, max(tb - 1, 0))

    total_frames = (hh * 3600 + mm * 60 + ss) * tb + ff
    is_drop = sep == ";" and tb in {30, 60}
    if is_drop:
        drop_per_min = 2 if tb == 30 else 4
        total_mins = hh * 60 + mm
        total_frames = max(0, total_frames - drop_per_min * (total_mins - total_mins // 10))

    # 对 NTSC 下拉帧率采用精确有理帧率换算秒数，消除 1000/1001 漂移
    exact_fps = safe_fps
    for cand_num, cand_den in ((24000, 1001), (30000, 1001), (60000, 1001), (120000, 1001)):
        cand = cand_num / float(cand_den)
        if abs(safe_fps - cand) <= 0.05:
            exact_fps = cand
            break

    sec = round(max(0.0, float(total_frames) / exact_fps), 6)
    norm_tc = f"{hh:02d}:{mm:02d}:{ss:02d}{sep}{ff:02d}"
    return norm_tc, sec


def _extract_timecode_tag(tags: Any) -> str:
    """从 ffprobe tags 字典中大小写不敏感地提取 timecode 字符串。"""
    if not isinstance(tags, dict):
        return ""
    for k, v in tags.items():
        if str(k).strip().lower() == "timecode" and v is not None:
            text = str(v).strip()
            if text:
                return text
    return ""


def probe_media_info(video_path: str | Path) -> dict[str, Any]:
    """全仓权威单次 ffprobe 媒体元数据探测（分辨率、真实 FPS、音轨参数、时长、码率与起始时间码）。"""
    src_video = Path(video_path)
    if not src_video.is_file():
        raise FileNotFoundError(f"找不到输入视频文件：{src_video}")

    ffprobe_bin = check_ffprobe_available()
    cmd = [
        ffprobe_bin,
        "-v",
        "error",
        "-show_format",
        "-show_streams",
        "-of",
        "json",
        str(src_video.resolve()),
    ]
    proc = run_media_subprocess(cmd, timeout=30)
    if proc.returncode != 0:
        stderr_lines = [line for line in proc.stderr.splitlines() if line.strip()]
        err_summary = "\n".join(stderr_lines[-6:]) if stderr_lines else "未知错误"
        raise RuntimeError(f"FFprobe 探测媒体元数据失败：{err_summary}")

    try:
        payload = json.loads(proc.stdout or "{}")
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"FFprobe 媒体元数据 JSON 解析失败：{exc}") from exc
    if not isinstance(payload, dict):
        raise RuntimeError("FFprobe 媒体元数据输出结构异常")

    streams = payload.get("streams") if isinstance(payload.get("streams"), list) else []
    fmt = payload.get("format") if isinstance(payload.get("format"), dict) else {}

    video_stream = next((s for s in streams if isinstance(s, dict) and s.get("codec_type") == "video"), None)
    audio_streams = [s for s in streams if isinstance(s, dict) and s.get("codec_type") == "audio"]
    data_streams = [s for s in streams if isinstance(s, dict) and s.get("codec_type") == "data"]

    width = int(video_stream.get("width") or 0) if video_stream else 0
    height = int(video_stream.get("height") or 0) if video_stream else 0
    video_codec = str(video_stream.get("codec_name") or "") if video_stream else ""

    # 优先使用真实平均帧率 avg_frame_rate，避免 VFR/高 timebase 视频被 r_frame_rate 误报，缺失时回退 r_frame_rate
    fps = 0.0
    if video_stream:
        avg_fps = _parse_ffprobe_rate(video_stream.get("avg_frame_rate"))
        r_fps = _parse_ffprobe_rate(video_stream.get("r_frame_rate"))
        if 0.5 < avg_fps <= 240.0:
            fps = avg_fps
        elif 0.5 < r_fps <= 240.0:
            fps = r_fps
    if fps <= 0.5 or fps > 240.0:
        fps = 30.0

    try:
        raw_dur = float(fmt.get("duration") or (video_stream.get("duration") if video_stream else 0.0) or 0.0)
        duration_sec = raw_dur if (math.isfinite(raw_dur) and raw_dur > 0.0) else 0.0
    except (TypeError, ValueError):
        duration_sec = 0.0

    try:
        raw_br = float(fmt.get("bit_rate") or 0.0)
        bit_rate = raw_br if (math.isfinite(raw_br) and raw_br > 0.0) else 0.0
    except (TypeError, ValueError):
        bit_rate = 0.0

    has_audio = bool(audio_streams)
    audio_channels = 0
    audio_rate = 48000
    if audio_streams:
        a0 = audio_streams[0]
        try:
            ch_val = int(a0.get("channels") or 0)
        except (TypeError, ValueError):
            ch_val = 0
        try:
            sr_val = int(a0.get("sample_rate") or 0)
        except (TypeError, ValueError):
            sr_val = 0
        audio_channels = max(1, min(8, ch_val)) if ch_val > 0 else 2
        audio_rate = sr_val if sr_val >= 8000 else 48000

    # 提取真实源片起始时间码（优先 tmcd 数据轨 -> 视频流 tags -> 容器 format tags）
    raw_tc = ""
    for ds in data_streams:
        raw_tc = _extract_timecode_tag(ds.get("tags"))
        if raw_tc:
            break
    if not raw_tc and video_stream:
        raw_tc = _extract_timecode_tag(video_stream.get("tags"))
    if not raw_tc:
        raw_tc = _extract_timecode_tag(fmt.get("tags"))
    start_timecode, start_timecode_sec = _parse_smpte_timecode_to_sec(raw_tc, fps)

    return {
        "path": str(src_video.resolve()),
        "width": width,
        "height": height,
        "fps": round(fps, 3),
        "video_codec": video_codec,
        "duration_sec": round(duration_sec, 3),
        "duration_str": format_timestamp(duration_sec) if duration_sec > 0 else "-",
        "bitrate_kbps": int(round(bit_rate / 1000.0)) if bit_rate > 0 else 0,
        "audio_stream_count": len(audio_streams),
        "has_video": bool(video_stream is not None),
        "has_audio": has_audio,
        "audio_channels": audio_channels,
        "audio_rate": audio_rate,
        "start_timecode": start_timecode,
        "start_timecode_sec": start_timecode_sec,
    }


def sample_wav_waveform(
    wav_path: str | Path,
    start_sec: float = 0.0,
    end_sec: float | None = None,
    points: int = 1200,
) -> dict[str, Any]:
    """对 16-bit PCM WAV 按任意时间窗口进行毫秒级分桶采样，返回 (rms, peak) 波形数组。

    - 短区间（<= 100s）：单次 seek + 向量化 reshape 精确计算，耗时 < 5ms；
    - 超长全片（如 4~8 小时 500MB WAV）：分桶跳跃采样，绝不爆内存且 ~30ms 内完成。
    """
    src_wav = Path(wav_path)
    if not src_wav.is_file():
        raise FileNotFoundError(f"找不到音频 WAV 文件：{src_wav}")

    target_points = max(16, min(4000, int(points)))

    with wave.open(str(src_wav), "rb") as wf:
        sr = wf.getframerate()
        channels = wf.getnchannels()
        sampwidth = wf.getsampwidth()
        total_frames = wf.getnframes()

        if sampwidth != 2 or sr <= 0 or total_frames <= 0:
            return {
                "source": "wav",
                "start_sec": 0.0,
                "end_sec": 0.0,
                "total_duration_sec": 0.0,
                "points": 0,
                "rms": [],
                "peak": [],
            }

        total_dur = total_frames / float(sr)
        s_sec = max(0.0, min(total_dur, float(start_sec)))
        e_sec = total_dur if end_sec is None or end_sec <= 0 else max(s_sec, min(total_dur, float(end_sec)))

        start_frame = max(0, min(total_frames - 1, int(round(s_sec * sr))))
        end_frame = max(start_frame + 1, min(total_frames, int(round(e_sec * sr))))
        span_frames = end_frame - start_frame
        actual_points = min(target_points, span_frames)

        rms_out: list[float] = []
        peak_out: list[float] = []

        if span_frames <= 1_600_000:
            wf.setpos(start_frame)
            raw = wf.readframes(span_frames)
            arr = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
            if channels > 1 and arr.size >= channels:
                arr = arr.reshape(-1, channels).mean(axis=1)
            usable = (arr.size // actual_points) * actual_points
            if usable >= actual_points:
                buckets = arr[:usable].reshape(actual_points, -1)
                rms_arr = np.sqrt(np.mean(buckets * buckets, axis=1))
                peak_arr = np.max(np.abs(buckets), axis=1)
                rms_out = [round(float(v), 4) for v in rms_arr]
                peak_out = [round(float(v), 4) for v in peak_arr]
        else:
            # 超长区间跳跃抽样：每桶采样最多 2048 帧兼顾峰值保真与极速响应
            step = span_frames / float(actual_points)
            max_chunk = min(2048, max(64, int(step)))
            for i in range(actual_points):
                pos = start_frame + int(i * step)
                read_n = min(max_chunk, end_frame - pos)
                if read_n <= 0:
                    rms_out.append(0.0)
                    peak_out.append(0.0)
                    continue
                wf.setpos(pos)
                raw = wf.readframes(read_n)
                if not raw:
                    rms_out.append(0.0)
                    peak_out.append(0.0)
                    continue
                arr = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
                if channels > 1 and arr.size >= channels:
                    arr = arr.reshape(-1, channels).mean(axis=1)
                rms_out.append(round(float(np.sqrt(np.mean(arr * arr))), 4))
                peak_out.append(round(float(np.max(np.abs(arr))), 4))

    return {
        "source": "wav",
        "start_sec": round(s_sec, 3),
        "end_sec": round(e_sec, 3),
        "total_duration_sec": round(total_dur, 3),
        "points": len(rms_out),
        "rms": rms_out,
        "peak": peak_out,
    }


def extract_video_thumbnail(
    video_path: str | Path,
    cache_dir: str | Path,
    time_sec: float = 10.0,
    width: int = 320,
) -> Path:
    """用 FFmpeg 极速抽取视频单帧缩略图并持久化缓存至本地 JPEG。"""
    src_video = Path(video_path)
    if not src_video.is_file():
        raise FileNotFoundError(f"找不到输入视频文件：{src_video}")

    stat = src_video.stat()
    safe_time = max(0.0, round(float(time_sec), 2))
    safe_w = max(80, min(960, int(width)))
    key_raw = f"{src_video.resolve()}|{stat.st_mtime_ns}|{stat.st_size}|{safe_time}|{safe_w}"
    cache_name = f"{hashlib.sha1(key_raw.encode('utf-8')).hexdigest()[:16]}.jpg"

    out_dir = Path(cache_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    out_jpg = out_dir / cache_name
    if out_jpg.is_file() and out_jpg.stat().st_size > 200:
        return out_jpg

    ffmpeg_bin = check_ffmpeg_available()

    def _run_extract(seek_sec: float) -> bool:
        tmp_jpg = out_dir / f".{out_jpg.stem}.part_{uuid.uuid4().hex[:8]}.jpg"
        cmd = [
            ffmpeg_bin,
            "-y",
            "-ss",
            f"{seek_sec:.2f}",
            "-i",
            str(src_video.resolve()),
            "-frames:v",
            "1",
            "-update",
            "1",
            "-vf",
            f"scale={safe_w}:-2",
            "-q:v",
            "4",
            str(tmp_jpg),
        ]
        try:
            proc = run_media_subprocess(cmd, timeout=20)
            if proc.returncode == 0 and tmp_jpg.is_file() and tmp_jpg.stat().st_size > 0:
                os.replace(tmp_jpg, out_jpg)
                return True
            return False
        except Exception:
            return False
        finally:
            if tmp_jpg.exists():
                tmp_jpg.unlink(missing_ok=True)

    if not _run_extract(safe_time):
        if safe_time > 0.0 and _run_extract(0.0):
            return out_jpg
        raise RuntimeError(f"FFmpeg 抽取视频缩略图失败：{src_video.name}")

    return out_jpg


def _print_summary(result: dict[str, Any]) -> None:
    """在终端打印易读的中文音频分析结果摘要。"""
    print("=" * 64)
    print("Step 1 音频高音量分析完成")
    print("=" * 64)
    print(f"源视频文件 : {result['video_path']}")
    print(f"视频总时长 : {result['total_duration_str']} ({result['total_duration_sec']} 秒)")
    stream_idx = result.get("audio_stream", 0)
    streams = result.get("audio_streams") or []
    stream_desc = f"音轨 #{stream_idx}"
    if 0 <= stream_idx < len(streams):
        stream_desc = streams[stream_idx].get("label") or stream_desc
    bp_hz = result.get("bandpass_hz")
    bp_desc = (
        f"开启人声带通 ({int(bp_hz[0])}~{int(bp_hz[1])}Hz)"
        if result.get("vocal_bandpass") and bp_hz
        else "全频段（未开启人声带通）"
    )
    print(f"选用音轨   : {stream_desc}")
    print(f"频段滤波   : {bp_desc}")
    print(
        f"判定阈值   : RMS >= {result['rms_threshold']} "
        f"(前 {result['top_percent']}%, 最低底线 {result['min_rms']})"
    )
    print(f"命中高能段 : {result['segment_count']} 个")
    dead_air_segs = result.get("dead_air_segments") or []
    if dead_air_segs:
        print(
            f"死寂长空白 : {len(dead_air_segs)} 段（累计 {result.get('dead_air_total_sec', 0.0)} 秒，审片时可一键跳过）"
        )
    print("-" * 64)

    segments = result["peak_segments"]
    if not segments:
        print("未检测到明显高音量片段（可能是纯静音视频，或可尝试调低 --min-rms / 调高 --top-percent）。")
    else:
        print(f"{'序号':<4} {'时间段':<15} {'起止(秒)':<17} {'时长':<8} {'峰值RMS':<9} {'均值RMS'}")
        for idx, seg in enumerate(segments, start=1):
            time_range = f"{seg['start_str']} - {seg['end_str']}"
            sec_range = f"{seg['start_sec']:.1f}s - {seg['end_sec']:.1f}s"
            print(
                f"{idx:<4} {time_range:<15} {sec_range:<17} "
                f"{seg['duration_sec']:<6.1f}s  {seg['peak_rms']:<9.4f} {seg['mean_rms']:.4f}"
            )

    print("-" * 64)
    diag = result.get("diagnostics")
    if diag:
        rec = diag["recommendation"]
        print(
            f"直播评级   : {diag['grade_label']} | 爆发比 {diag['burst_ratio']}x | "
            f"强峰 {diag['strong_peaks_per_hour']}/h | 黄金段 [{diag['golden_bucket_label']}]"
        )
        print(
            f"推荐 Step2 : 模式={rec['asr_mode']} | 提取高峰={rec['top_k_peaks']} "
            f"（深挖 {rec['deep_top_k_peaks']}）"
        )
        print("-" * 64)
    print(f"16k WAV 音频 : {result['wav_path']}")
    print(f"JSON 报告    : {result['json_path']}")
    print("=" * 64)


def build_arg_parser() -> argparse.ArgumentParser:
    """构建命令行参数解析器。"""
    parser = argparse.ArgumentParser(
        description="Step 1：从游戏录屏中提取 16kHz 单声道 WAV，并检测高音量高光时间段（支持 OBS 多音轨与人声频段带通）。"
    )
    parser.add_argument(
        "video_path",
        help="输入游戏录屏视频文件路径（如 input/demo.mp4）",
    )
    parser.add_argument(
        "--output-dir",
        default="output",
        help="输出 WAV 与 JSON 报告的目录（默认：output）",
    )
    parser.add_argument(
        "--window-sec",
        type=float,
        default=1.0,
        help="统计音量的时间窗口长度（秒，默认：1.0）",
    )
    parser.add_argument(
        "--top-percent",
        type=float,
        default=15.0,
        help="选取响度排名前多少百分比的窗口作为候选高光（默认：15.0）",
    )
    parser.add_argument(
        "--min-rms",
        type=float,
        default=0.01,
        help="过滤纯静音/底噪的最低 RMS 门槛（默认：0.01）",
    )
    parser.add_argument(
        "--merge-gap",
        type=float,
        default=2.0,
        help="相邻高音量区间间隔小于等于多少秒时自动合并（默认：2.0）",
    )
    parser.add_argument(
        "--pad",
        type=float,
        default=1.0,
        help="每个高音量片段前后额外保留的缓冲秒数（默认：1.0）",
    )
    parser.add_argument(
        "--audio-stream",
        type=int,
        default=0,
        help="指定提取视频中的第几条音轨（0 起算，适合 OBS 多音轨单独选人声麦轨，默认：0）",
    )
    parser.add_argument(
        "--no-vocal-bandpass",
        action="store_true",
        help="关闭 300~3400Hz 人声带通滤波（改用全频段音量测算峰值）",
    )
    parser.add_argument(
        "--list-streams",
        action="store_true",
        help="仅探测并打印视频包含的全部音频轨道信息后退出",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    """命令行程序主入口。"""
    # 尽量保证 Windows 终端输出中文时不触发编码异常
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")

    parser = build_arg_parser()
    args = parser.parse_args(argv)

    try:
        if args.list_streams:
            streams = probe_audio_streams(args.video_path)
            print("=" * 64)
            print(f"视频音轨列表（共 {len(streams)} 条）：{Path(args.video_path).resolve()}")
            print("=" * 64)
            if not streams:
                print("未检测到任何有效音频轨道。")
            else:
                for st in streams:
                    print(f"  {st['label']}")
            print("=" * 64)
            return 0

        result = analyze_video_audio(
            video_path=args.video_path,
            output_dir=args.output_dir,
            window_sec=args.window_sec,
            top_percent=args.top_percent,
            min_rms=args.min_rms,
            merge_gap_sec=args.merge_gap,
            pad_sec=args.pad,
            audio_stream=args.audio_stream,
            vocal_bandpass=not args.no_vocal_bandpass,
        )
    except (FileNotFoundError, ValueError, RuntimeError) as exc:
        print(f"[错误] {exc}", file=sys.stderr)
        return 1

    _print_summary(result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
