"""Step 2：基于 faster-whisper 的本地语音转字幕与高音量标记融合。

本模块负责整个高光切片流程的第二步：
1. 读取 Step 1 输出的 16kHz WAV 音频与 `<视频名>_audio_peaks.json`。
2. 默认采用极速“高能锚点模式（peaks）”：只挑出全场音量最炸裂的 Top K 个高峰，
   各自向前后扩展上下文窗口（例如前后各 15 秒）并合并重叠片段，再送入本地
   faster-whisper（在 RTX 2060 上自动走 CUDA float16 加速），把 4~5 小时直播的
   转写耗时从几十分钟压缩到几十秒；同时也保留全量转写模式（full）按需使用。
3. 把识别出的每条字幕与 Step 1 的高音量区间对齐，打上 `is_high_volume` 与 `peak_rms` 标记，
   最后导出结构化 JSON 和便于人类预览与 Step 3 喂大模型的 TXT 字幕稿。

命令行运行示例：
    python src/step2_asr.py output/shylily_1_audio_peaks.json
    python src/step2_asr.py output/shylily_1_audio_peaks.json --mode full
"""

from __future__ import annotations

import argparse
import json
import os
import re
import site
import sys
import time
import wave
from pathlib import Path
from typing import Any, Callable

import numpy as np

# 确保既能用 `python src/step2_asr.py` 直接运行，也能用 `from src.step2_asr import ...` 导入
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from src.asr_providers import (  # noqa: E402
    normalize_audio_for_asr,
    transcribe_window_with_deepseek_asr,
    transcribe_window_with_openai_compatible_asr,
)
from src.config import resolve_asr_config  # noqa: E402
from src.step1_audio import format_timestamp  # noqa: E402

# 保存 Windows 下 os.add_dll_directory 返回的句柄，防止被垃圾回收后 DLL 搜索路径失效
_DLL_DIR_HANDLES: list[Any] = []
_REGISTERED_DLL_DIRS: set[str] = set()


def _register_nvidia_dll_dirs() -> list[str]:
    """在 Windows 下把虚拟环境中的 NVIDIA CUDA/cuDNN DLL 目录加入搜索路径。

    为什么需要这个函数？
    很多 Windows 电脑没有单独去官网装几 GB 的 CUDA Toolkit；我们通过 pip/uv 安装了
    `nvidia-cublas-cu12` 和 `nvidia-cudnn-cu12` 后，动态库（.dll）落在 site-packages/nvidia/*/bin 里。
    在导入 ctranslate2 / faster_whisper 前先注册这些目录，就能免配置跑满 RTX 2060 GPU 加速。
    """
    if sys.platform != "win32":
        return []

    candidate_site_dirs: list[Path] = [Path(sys.prefix) / "Lib" / "site-packages"]
    for sp in site.getsitepackages():
        sp_path = Path(sp)
        if sp_path not in candidate_site_dirs:
            candidate_site_dirs.append(sp_path)
    user_sp = site.getusersitepackages()
    if user_sp:
        user_sp_path = Path(user_sp)
        if user_sp_path not in candidate_site_dirs:
            candidate_site_dirs.append(user_sp_path)

    newly_registered: list[str] = []
    current_path = os.environ.get("PATH", "")
    path_parts = current_path.split(os.pathsep) if current_path else []

    for site_dir in candidate_site_dirs:
        nvidia_root = site_dir / "nvidia"
        if not nvidia_root.is_dir():
            continue

        # 遍历 nvidia/* 下的 bin 目录（例如 cublas/bin、cudnn/bin、cuda_nvrtc/bin）
        for bin_dir in sorted(nvidia_root.glob("*/bin")):
            if not bin_dir.is_dir():
                continue
            dir_str = str(bin_dir.resolve())
            if dir_str in _REGISTERED_DLL_DIRS:
                continue

            if hasattr(os, "add_dll_directory"):
                handle = os.add_dll_directory(dir_str)
                _DLL_DIR_HANDLES.append(handle)
            if dir_str not in path_parts:
                path_parts.insert(0, dir_str)
                newly_registered.append(dir_str)

            _REGISTERED_DLL_DIRS.add(dir_str)

    if newly_registered:
        os.environ["PATH"] = os.pathsep.join(path_parts)

    return newly_registered


def select_candidate_windows(
    peak_segments: list[dict[str, Any]],
    total_duration_sec: float,
    top_k_peaks: int = 20,
    context_pad_sec: float = 15.0,
    merge_gap_sec: float = 3.0,
) -> list[dict[str, Any]]:
    """从 Step 1 高音量列表中选出最炸裂的 Top K 锚点，扩展前后上下文并合并重叠窗口。

    算法思路：
    1. 几小时直播往往有上百段高音量，全部转写太慢；这里先按 `peak_rms` 从高到低挑出前 K 名。
    2. 给每个高峰前后各补 `context_pad_sec` 秒（默认 15 秒），既能听到高潮前的铺垫台词，也能听到事后反应。
    3. 按时间排序，如果两个窗口挨得很近（间隔 <= merge_gap_sec）甚至重叠，就合并成同一段连续窗口。

    参数：
        peak_segments: Step 1 JSON 中的 `peak_segments` 列表。
        total_duration_sec: 视频总时长（秒），用于防止扩展时间超出视频末尾。
        top_k_peaks: 选取峰值音量最高的前几个片段（默认 20）。
        context_pad_sec: 每个高能片段前后各扩展的上下文秒数（默认 15.0）。
        merge_gap_sec: 相邻扩展窗口间隔小于等于多少秒时自动合并（默认 3.0）。
    返回：
        按时间先后排列的候选转写窗口列表。
    """
    if top_k_peaks < 1:
        raise ValueError(f"top_k_peaks 必须大于等于 1，当前传入：{top_k_peaks}")
    if context_pad_sec < 0 or merge_gap_sec < 0:
        raise ValueError("context_pad_sec 和 merge_gap_sec 不能为负数")
    if total_duration_sec <= 0 or not peak_segments:
        return []

    # 过滤无效区间并按峰值响度 peak_rms 从高到低排序，取前 top_k_peaks 个
    valid_peaks = [
        seg
        for seg in peak_segments
        if float(seg.get("end_sec", 0.0)) > float(seg.get("start_sec", 0.0))
    ]
    if not valid_peaks:
        return []

    ranked_peaks = sorted(
        valid_peaks,
        key=lambda seg: (-float(seg.get("peak_rms", 0.0)), float(seg.get("start_sec", 0.0))),
    )[:top_k_peaks]

    # 对选出的高能片段做上下文扩展，并裁剪到 [0.0, total_duration_sec] 合法范围内
    expanded: list[tuple[float, float, float]] = []
    for seg in ranked_peaks:
        ext_start = max(0.0, float(seg["start_sec"]) - context_pad_sec)
        ext_end = min(float(total_duration_sec), float(seg["end_sec"]) + context_pad_sec)
        if ext_end > ext_start:
            expanded.append((ext_start, ext_end, float(seg.get("peak_rms", 0.0))))

    if not expanded:
        return []

    # 按时间先后排序，合并重叠或间隔 <= merge_gap_sec 的窗口
    expanded.sort(key=lambda item: (item[0], item[1]))
    merged: list[list[Any]] = []
    for start_sec, end_sec, peak_rms in expanded:
        if not merged:
            merged.append([start_sec, end_sec, peak_rms, 1])
        else:
            prev = merged[-1]
            if start_sec <= prev[1] + merge_gap_sec:
                prev[1] = max(prev[1], end_sec)
                prev[2] = max(prev[2], peak_rms)
                prev[3] += 1
            else:
                merged.append([start_sec, end_sec, peak_rms, 1])

    windows: list[dict[str, Any]] = []
    for idx, (w_start, w_end, max_peak_rms, peak_count) in enumerate(merged, start=1):
        start_rounded = round(float(w_start), 2)
        end_rounded = round(float(w_end), 2)
        windows.append(
            {
                "window_id": idx,
                "start_sec": start_rounded,
                "end_sec": end_rounded,
                "duration_sec": round(end_rounded - start_rounded, 2),
                "start_str": format_timestamp(start_rounded),
                "end_str": format_timestamp(end_rounded),
                "max_peak_rms": round(float(max_peak_rms), 4),
                "peak_count": peak_count,
            }
        )

    return windows


def read_wav_segment_float32(
    wav_path: str | Path,
    start_sec: float,
    end_sec: float,
) -> np.ndarray:
    """用标准库 wave 精准截取 WAV 的指定秒数片段，转为 faster-whisper 所需的 float32 数组。

    优势：不需要额外调 FFmpeg 切小文件，直接通过文件指针 `setpos` 秒级跳到对应位置读取！

    参数：
        wav_path: 16-bit PCM WAV 音频文件路径。
        start_sec: 切片起始时间（秒）。
        end_sec: 切片结束时间（秒）。
    返回：
        一维 np.float32 数组，取值范围归一化到 [-1.0, 1.0]。
    """
    src_wav = Path(wav_path)
    if not src_wav.is_file():
        raise FileNotFoundError(f"找不到音频文件：{src_wav}")
    if end_sec <= start_sec:
        return np.array([], dtype=np.float32)

    with wave.open(str(src_wav), "rb") as wf:
        sample_rate = wf.getframerate()
        channels = wf.getnchannels()
        sample_width = wf.getsampwidth()
        total_frames = wf.getnframes()

        if sample_width != 2:
            raise ValueError(
                f"仅支持 16-bit PCM WAV（sampwidth=2），当前文件 sampwidth={sample_width}"
            )
        if sample_rate <= 0 or total_frames == 0:
            return np.array([], dtype=np.float32)

        start_frame = max(0, min(total_frames, int(round(start_sec * sample_rate))))
        end_frame = max(start_frame, min(total_frames, int(round(end_sec * sample_rate))))
        num_frames = end_frame - start_frame
        if num_frames <= 0:
            return np.array([], dtype=np.float32)

        # 直接定位到 start_frame 位置，无需读取前面的无效音频
        wf.setpos(start_frame)
        raw_bytes = wf.readframes(num_frames)
        if not raw_bytes:
            return np.array([], dtype=np.float32)

    samples = np.frombuffer(raw_bytes, dtype=np.int16).astype(np.float32) / 32768.0
    if channels > 1:
        samples = samples.reshape(-1, channels).mean(axis=1).astype(np.float32)

    return samples


_DANGLING_END_WORDS = frozenset(
    {
        "a",
        "an",
        "the",
        "of",
        "to",
        "in",
        "on",
        "at",
        "for",
        "with",
        "from",
        "by",
        "about",
        "as",
        "into",
        "like",
        "and",
        "but",
        "or",
        "so",
        "because",
        "if",
        "when",
        "while",
        "that",
        "which",
        "who",
        "whose",
        "where",
        "is",
        "are",
        "was",
        "were",
        "be",
        "been",
        "being",
        "am",
        "i",
        "i'm",
        "i'd",
        "i'll",
        "i've",
        "you",
        "we",
        "they",
        "he",
        "she",
        "it",
        "my",
        "your",
        "our",
        "their",
        "his",
        "her",
        "this",
        "these",
        "those",
        "not",
        "just",
        "very",
        "really",
        "grow",
        "make",
        "have",
        "has",
        "had",
        "do",
        "does",
        "did",
        "can",
        "could",
        "would",
        "should",
        "will",
        "uh",
        "um",
        "er",
        "ah",
        "oh",
        "well",
        "kinda",
        "sorta",
    }
)


def _visual_char_len(text: str) -> int:
    """计算文本的近似视觉宽度（中文/全角等 CJK 字符按 2 计，ASCII 按 1 计）。"""
    total = 0
    for ch in text:
        code = ord(ch)
        if 0x2E80 <= code <= 0x9FFF or 0xF900 <= code <= 0xFAFF or 0xFF00 <= code <= 0xFFEF:
            total += 2
        else:
            total += 1
    return total


def _is_dangling_token(token: str) -> bool:
    """判断单个词是否为不宜作为句子/字幕结尾的悬垂虚词或口吃填充词（含逗号结尾如 like, / uh, / I,）。"""
    stripped = (token or "").strip()
    if not stripped or stripped.endswith((".", "!", "?", ";", "。", "！", "？", "；")):
        return False
    core = stripped.strip("\"'()[]{}.,!?;:，。！？；：、").lower()
    return core in _DANGLING_END_WORDS


def _ends_with_dangling_word(text: str) -> bool:
    """判断整段文本末尾是否落在一个未说完的英文悬垂虚词上。"""
    stripped = (text or "").strip()
    if not stripped:
        return False
    if stripped.endswith((".", "!", "?", ";", "。", "！", "？", "；")):
        return False
    parts = stripped.split()
    if not parts:
        return False
    return _is_dangling_token(parts[-1])


def _join_subtitle_texts(left: str, right: str) -> str:
    """智能拼接两段相邻台词（英文之间补空格，中文紧挨）。"""
    l_str = (left or "").rstrip()
    r_str = (right or "").lstrip()
    if not l_str:
        return r_str
    if not r_str:
        return l_str
    last_is_cjk = _visual_char_len(l_str[-1:]) == 2
    first_is_cjk = _visual_char_len(r_str[:1]) == 2
    if last_is_cjk or first_is_cjk or r_str[0] in ",.;!?，。；！？、":
        return f"{l_str}{r_str}"
    return f"{l_str} {r_str}"


def _merge_tiny_segments(
    segments: list[dict[str, Any]],
    max_duration_sec: float,
    max_chars: int,
) -> list[dict[str, Any]]:
    """合并切分后残余的极短/极少词碎片（< 0.9s 或 <= 10 视觉字符），消灭 0.3 秒闪现碎词。"""
    if len(segments) <= 1:
        return segments

    items = [dict(s) for s in segments]
    max_merged_dur = max_duration_sec + 1.0
    max_merged_chars = max_chars + 16

    changed = True
    while changed and len(items) > 1:
        changed = False
        idx = 0
        while idx < len(items):
            cur = items[idx]
            cur_text = str(cur.get("text", "")).strip()
            cur_dur = float(cur["end_sec"]) - float(cur["start_sec"])
            word_cnt = len(cur_text.split())
            is_tiny = (cur_dur < 0.9) or (_visual_char_len(cur_text) <= 10 and word_cnt <= 2)
            if not is_tiny or len(items) <= 1:
                idx += 1
                continue

            merged_done = False
            # 1. 若前一句没说死且间隔很短，优先并入前一句
            if idx > 0:
                prev = items[idx - 1]
                prev_text = str(prev.get("text", "")).strip()
                gap_prev = float(cur["start_sec"]) - float(prev["end_sec"])
                comb_prev = _join_subtitle_texts(prev_text, cur_text)
                comb_dur_prev = float(cur["end_sec"]) - float(prev["start_sec"])
                if (
                    not prev_text.endswith((".", "!", "?", "。", "！", "？"))
                    and gap_prev <= 0.8
                    and comb_dur_prev <= max_merged_dur
                    and _visual_char_len(comb_prev) <= max_merged_chars
                ):
                    prev["end_sec"] = round(max(float(prev["end_sec"]), float(cur["end_sec"])), 2)
                    prev["text"] = comb_prev
                    items.pop(idx)
                    changed = True
                    merged_done = True
                    continue

            # 2. 否则尝试并入后一句（适合句子开头的悬垂虚词碎片）
            if not merged_done and idx + 1 < len(items):
                nxt = items[idx + 1]
                nxt_text = str(nxt.get("text", "")).strip()
                gap_next = float(nxt["start_sec"]) - float(cur["end_sec"])
                comb_next = _join_subtitle_texts(cur_text, nxt_text)
                comb_dur_next = float(nxt["end_sec"]) - float(cur["start_sec"])
                if (
                    not cur_text.endswith((".", "!", "?", "。", "！", "？"))
                    and gap_next <= 0.8
                    and comb_dur_next <= max_merged_dur
                    and _visual_char_len(comb_next) <= max_merged_chars
                ):
                    nxt["start_sec"] = round(min(float(cur["start_sec"]), float(nxt["start_sec"])), 2)
                    nxt["text"] = comb_next
                    items.pop(idx)
                    changed = True
                    continue

            idx += 1

    return items


def _split_text_without_words(text: str, max_chars: int) -> list[str]:
    """在没有词级时间戳时，优先按标点与空格词界将长句拆成紧凑短句，避免悬垂虚词断尾。"""
    # 1. 先按句末/句中常见标点切块，同时保留标点附着在上一句末尾
    raw_clauses = [
        part.strip()
        for part in re.split(r"(?<=[,.;!?，。；！？、])\s*", text.strip())
        if part.strip()
    ]
    if not raw_clauses:
        raw_clauses = [text.strip()]

    # 2. 对依然过长的单个子句，按空格单词或固定视觉宽度切分（带悬垂虚词回溯保护）
    pieces: list[str] = []
    for clause in raw_clauses:
        if _visual_char_len(clause) <= max_chars:
            pieces.append(clause)
            continue

        if " " in clause:
            tokens = clause.split()
            buf: list[str] = []
            for tok in tokens:
                candidate = " ".join(buf + [tok])
                if buf and _visual_char_len(candidate) > max_chars:
                    carry: list[str] = []
                    # 若当前块以冠词/介词/连词等悬垂虚词结尾，将尾部 1~2 个虚词让给下一块
                    while len(buf) >= 3 and len(carry) < 2 and _is_dangling_token(buf[-1]):
                        carry.insert(0, buf.pop())
                    pieces.append(" ".join(buf))
                    buf = carry + [tok]
                else:
                    buf.append(tok)
            if buf:
                pieces.append(" ".join(buf))
        else:
            # 无空格文本（纯中文等）按视觉宽度切分
            cur_chars: list[str] = []
            cur_w = 0
            for ch in clause:
                w = _visual_char_len(ch)
                if cur_chars and cur_w + w > max_chars:
                    pieces.append("".join(cur_chars))
                    cur_chars = [ch]
                    cur_w = w
                else:
                    cur_chars.append(ch)
                    cur_w += w
            if cur_chars:
                pieces.append("".join(cur_chars))

    # 3. 把相邻非常短或以悬垂词结尾的小片段适度合并，避免产生只含一两个词的碎句
    merged: list[str] = []
    for piece in pieces:
        if not merged:
            merged.append(piece)
            continue
        combined = _join_subtitle_texts(merged[-1], piece)
        prev_is_dangling = _ends_with_dangling_word(merged[-1])
        is_short_piece = _visual_char_len(piece) <= max(12, max_chars // 3) or len(piece.split()) <= 2
        if (is_short_piece or prev_is_dangling) and _visual_char_len(combined) <= max_chars + 12:
            merged[-1] = combined
        else:
            merged.append(piece)

    return [m for m in merged if m.strip()]


def split_long_segment_by_words(
    text: str,
    start_sec: float,
    end_sec: float,
    words: list[Any] | None = None,
    max_duration_sec: float = 4.5,
    max_chars: int = 48,
) -> list[dict[str, Any]]:
    """将跨度过长的单条 ASR 字幕拆分为 2~4 秒的紧凑短句，避免文字墙与半空断句碎词。

    优先使用 faster-whisper 输出的词级时间戳（`words`）在标点或时长上限处平滑断句，
    并主动避开英文悬垂虚词（a/the/of/to/and/I 等）后断句与 0.9s 以下超短残片；
    若无词级时间戳，则按标点/词界拆分并按视觉宽度比例平滑分配起止时间。
    """
    clean_text = (text or "").strip()
    if not clean_text:
        return []

    s_start = float(start_sec)
    s_end = max(s_start, float(end_sec))
    duration = s_end - s_start

    # 本身足够短的字幕直接原样保留
    if duration <= max_duration_sec and _visual_char_len(clean_text) <= max_chars:
        return [{"start_sec": round(s_start, 2), "end_sec": round(s_end, 2), "text": clean_text}]

    # 路径 A：优先利用词级时间戳进行精准断句（带悬垂虚词回溯保护）
    normalized_words: list[tuple[str, float, float]] = []
    if words:
        for w in words:
            if isinstance(w, dict):
                w_text = str(w.get("word", ""))
                w_s = float(w.get("start", s_start))
                w_e = float(w.get("end", w_s))
            else:
                w_text = str(getattr(w, "word", ""))
                w_s = float(getattr(w, "start", s_start))
                w_e = float(getattr(w, "end", w_s))
            if w_text.strip():
                normalized_words.append((w_text, max(s_start, w_s), min(s_end, max(w_s, w_e))))

    if normalized_words:
        results: list[dict[str, Any]] = []
        buf_words: list[tuple[str, float, float]] = []

        for idx, (w_text, w_s, w_e) in enumerate(normalized_words):
            if buf_words:
                cur_joined = "".join(item[0] for item in buf_words).strip()
                candidate_text = (cur_joined + w_text).strip()
                buf_start = buf_words[0][1]
                would_exceed_chars = _visual_char_len(candidate_text) > max_chars
                would_exceed_dur = (w_e - buf_start) > max_duration_sec
            else:
                would_exceed_chars = False
                would_exceed_dur = False

            if buf_words and (would_exceed_chars or would_exceed_dur):
                # 回溯保护：如果结尾落在悬垂虚词上，最多让出末尾 2 个虚词给下一块开头
                carry_words: list[tuple[str, float, float]] = []
                while (
                    len(buf_words) >= 3
                    and len(carry_words) < 2
                    and _is_dangling_token(buf_words[-1][0])
                ):
                    carry_words.insert(0, buf_words.pop())

                chunk_text = "".join(item[0] for item in buf_words).strip()
                if chunk_text:
                    c_start = buf_words[0][1]
                    c_end = max(item[2] for item in buf_words)
                    results.append(
                        {
                            "start_sec": round(c_start, 2),
                            "end_sec": round(max(c_start + 0.1, c_end), 2),
                            "text": chunk_text,
                        }
                    )
                buf_words = carry_words + [(w_text, w_s, w_e)]
            else:
                buf_words.append((w_text, w_s, w_e))

            stripped = "".join(item[0] for item in buf_words).strip()
            cur_start = buf_words[0][1]
            cur_end = max(item[2] for item in buf_words)
            cur_dur = cur_end - cur_start
            cur_len = _visual_char_len(stripped)
            has_next = idx + 1 < len(normalized_words)

            # 遇到自然标点且当前句长已合适时提前断句，避免两句粘在一起
            is_sentence_end = stripped.endswith((".", "!", "?", "。", "！", "？"))
            is_clause_pause = stripped.endswith((",", ";", "，", "；", "、"))
            if has_next and stripped:
                if (is_sentence_end and (cur_dur >= 1.2 or cur_len >= 14)) or (
                    is_clause_pause and (cur_dur >= 2.2 or cur_len >= 28)
                ):
                    results.append(
                        {
                            "start_sec": round(cur_start, 2),
                            "end_sec": round(max(cur_start + 0.1, cur_end), 2),
                            "text": stripped,
                        }
                    )
                    buf_words = []

        if buf_words:
            tail_text = "".join(item[0] for item in buf_words).strip()
            if tail_text:
                t_start = buf_words[0][1]
                t_end = max(item[2] for item in buf_words)
                results.append(
                    {
                        "start_sec": round(t_start, 2),
                        "end_sec": round(min(s_end, max(t_start + 0.1, t_end)), 2),
                        "text": tail_text,
                    }
                )
        if results:
            return _merge_tiny_segments(results, max_duration_sec=max_duration_sec, max_chars=max_chars)

    # 路径 B：无词级时间戳时按标点/词边界拆分，并按字符视觉宽度比例分配时间
    pieces = _split_text_without_words(clean_text, max_chars=max_chars)
    if len(pieces) <= 1:
        return [{"start_sec": round(s_start, 2), "end_sec": round(s_end, 2), "text": clean_text}]

    # 如果拆完仍有单块估计时长超标，继续细拆长块
    target_pieces: list[str] = []
    weights = [max(1, _visual_char_len(p)) for p in pieces]
    total_w = sum(weights)
    for piece, w in zip(pieces, weights):
        est_dur = duration * (w / total_w)
        if est_dur > max_duration_sec and _visual_char_len(piece) > 16:
            sub_max = max(14, int(_visual_char_len(piece) * (max_duration_sec / est_dur)))
            target_pieces.extend(_split_text_without_words(piece, max_chars=sub_max))
        else:
            target_pieces.append(piece)

    final_weights = [max(1, _visual_char_len(p)) for p in target_pieces]
    total_final_w = sum(final_weights)
    cursor = s_start
    out: list[dict[str, Any]] = []
    for idx, (piece, w) in enumerate(zip(target_pieces, final_weights)):
        if idx == len(target_pieces) - 1:
            piece_end = s_end
        else:
            piece_end = cursor + duration * (w / total_final_w)
        out.append(
            {
                "start_sec": round(cursor, 2),
                "end_sec": round(max(cursor, piece_end), 2),
                "text": piece.strip(),
            }
        )
        cursor = piece_end

    return _merge_tiny_segments(out, max_duration_sec=max_duration_sec, max_chars=max_chars)


def align_segments_with_peaks(
    transcript_segments: list[dict[str, Any]],
    peak_segments: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """将字幕片段与 Step 1 高音量时间段做时间重叠匹配，打上情绪高能标记。

    如果某句台词的时间区间与任意高音量区间有重叠，就标记 `is_high_volume = True`，
    并记录所有重叠高音量区间中的最高 `peak_rms`，方便 Step 3 大模型一眼定位重点台词。
    """
    aligned: list[dict[str, Any]] = []

    for seg in transcript_segments:
        s_start = float(seg["start_sec"])
        s_end = float(seg["end_sec"])

        has_overlap = False
        best_peak_rms = 0.0

        for peak in peak_segments:
            p_start = float(peak["start_sec"])
            p_end = float(peak["end_sec"])
            # 区间有交集：两个区间起点的较大值 < 终点的较小值
            overlap = max(s_start, p_start) < min(s_end, p_end)
            # 兼容起止时间相等的瞬时字幕点
            if not overlap and s_start == s_end:
                overlap = p_start <= s_start <= p_end

            if overlap:
                has_overlap = True
                best_peak_rms = max(best_peak_rms, float(peak.get("peak_rms", 0.0)))

        updated = dict(seg)
        updated["is_high_volume"] = has_overlap
        updated["peak_rms"] = round(best_peak_rms, 4) if has_overlap else 0.0
        aligned.append(updated)

    return aligned


def load_whisper_model(
    model_size: str = "large-v3-turbo",
    device: str = "auto",
    compute_type: str = "auto",
) -> tuple[Any, str, str]:
    """初始化并返回 faster-whisper 模型（优先 RTX CUDA 加速，异常时安全降级 CPU）。

    针对 6GB 显存显卡（如 RTX 2060），加载 `large-v3-turbo` / `large-v3` / `distil-large-v3`
    时默认自动选用 `int8_float16`（显存仅约 1.8GB，精度近乎无损），兼顾高准度与稳定性。

    返回：
        (WhisperModel 实例, 实际使用的设备名, 实际使用的计算精度)
    """
    _register_nvidia_dll_dirs()

    try:
        import ctranslate2
        from faster_whisper import WhisperModel
    except ImportError as exc:
        raise RuntimeError(
            "尚未安装 faster-whisper。请先运行：\n"
            "uv pip install --python .venv\\Scripts\\python.exe faster-whisper nvidia-cublas-cu12 nvidia-cudnn-cu12"
        ) from exc

    if device == "auto":
        has_cuda = False
        try:
            has_cuda = ctranslate2.get_cuda_device_count() > 0
        except Exception:
            has_cuda = False
        target_device = "cuda" if has_cuda else "cpu"
    else:
        target_device = device

    if compute_type == "auto":
        if target_device == "cuda":
            model_lower = str(model_size or "").lower()
            target_compute = "int8_float16" if "large" in model_lower else "float16"
        else:
            target_compute = "int8"
    else:
        target_compute = compute_type

    try:
        model = WhisperModel(model_size, device=target_device, compute_type=target_compute)
        return model, target_device, target_compute
    except Exception as exc:
        if target_device == "cuda" and device == "auto":
            print(
                f"[警告] CUDA GPU 模式初始化失败（{exc}），已自动切换为 CPU (int8) 模式继续运行。",
                file=sys.stderr,
            )
            fallback_model = WhisperModel(model_size, device="cpu", compute_type="int8")
            return fallback_model, "cpu", "int8"
        raise RuntimeError(
            f"加载 Whisper 模型失败（model={model_size}, device={target_device}, compute={target_compute}）：{exc}"
        ) from exc


def _run_local_whisper_transcribe(
    model: Any,
    audio_input: Any,
    language: str | None = None,
    vad_threshold: float = 0.30,
    hotwords: str = "",
) -> tuple[Any, Any]:
    """以调优后的低漏词 VAD、防幻觉与热词参数调用本地 faster-whisper。"""
    vad_params = {
        "threshold": float(vad_threshold),
        "neg_threshold": max(0.10, round(float(vad_threshold) - 0.12, 2)),
        "min_speech_duration_ms": 150,
        "max_speech_duration_s": 15.0,
        "min_silence_duration_ms": 500,
        "speech_pad_ms": 250,
    }
    kwargs: dict[str, Any] = {
        "language": language,
        "vad_filter": True,
        "vad_parameters": vad_params,
        "beam_size": 5,
        "word_timestamps": True,
        "condition_on_previous_text": False,
        "hallucination_silence_threshold": 1.5,
        "no_speech_threshold": 0.55,
    }
    clean_hotwords = (hotwords or "").strip()
    if clean_hotwords:
        kwargs["hotwords"] = clean_hotwords
        kwargs["initial_prompt"] = f"Gaming livestream transcript. Terms: {clean_hotwords}."

    try:
        return model.transcribe(audio_input, **kwargs)
    except TypeError:
        # 兼容部分精简 mock 或旧版 whisper 不支持 hotwords / hallucination_silence_threshold
        for opt_key in ("hotwords", "hallucination_silence_threshold", "initial_prompt"):
            kwargs.pop(opt_key, None)
        return model.transcribe(audio_input, **kwargs)


def detect_dominant_language(
    model: Any,
    wav_path: Path,
    candidate_windows: list[dict[str, Any]],
    normalize_audio: bool = True,
    vad_threshold: float = 0.30,
) -> tuple[str | None, float]:
    """从最长的几个候选窗口中抽样识别主语言，避免单段几秒纯尖叫造成语言误判。

    返回：
        (主语言代码如 "en" / "zh" 或 None, 平均置信概率)
    """
    if not candidate_windows:
        return None, 0.0

    # 挑出时长最长的最多 3 个窗口做语言投票，通常比短尖叫更包含完整对话
    sample_windows = sorted(
        candidate_windows,
        key=lambda w: float(w.get("duration_sec", 0.0)),
        reverse=True,
    )[:3]

    votes: dict[str, list[float]] = {}
    vad_params = {
        "threshold": float(vad_threshold),
        "neg_threshold": max(0.10, round(float(vad_threshold) - 0.12, 2)),
        "min_silence_duration_ms": 500,
    }
    for win in sample_windows:
        w_start = float(win["start_sec"])
        # 每次最多抽 30 秒音频做语种检测即可，极快且够用
        w_end = min(float(win["end_sec"]), w_start + 30.0)
        audio_np = read_wav_segment_float32(wav_path, w_start, w_end)
        if audio_np.size < 1600:
            continue
        if normalize_audio:
            audio_np = normalize_audio_for_asr(audio_np, 16000)

        try:
            _, info = model.transcribe(
                audio_np,
                beam_size=1,
                vad_filter=True,
                vad_parameters=vad_params,
            )
        except Exception:
            continue

        lang = getattr(info, "language", None)
        prob = float(getattr(info, "language_probability", 0.0))
        if lang:
            votes.setdefault(lang, []).append(prob)

    if not votes:
        return None, 0.0

    # 按总置信概率之和选出主语言
    best_lang = max(votes.items(), key=lambda item: sum(item[1]))[0]
    avg_prob = sum(votes[best_lang]) / len(votes[best_lang])
    return best_lang, round(avg_prob, 4)


def export_readable_transcript(result: dict[str, Any], txt_path: str | Path) -> Path:
    """将结构化字幕导出为方便阅读与 Step 3 喂大模型的 TXT 文本稿。"""
    dst_txt = Path(txt_path)
    dst_txt.parent.mkdir(parents=True, exist_ok=True)

    provider_tag = result.get("asr_provider") or "local"
    lines: list[str] = [
        f"# 字幕与高能情绪稿：{Path(result['video_path']).name}",
        f"# 引擎：{provider_tag} ({result.get('asr_model') or result.get('model_size')}) | "
        f"模式：{result['mode']} | 语言：{result.get('language') or 'auto'} | "
        f"总字幕：{result['segment_count']} 条 | 高音量命中：{result['high_volume_count']} 条",
        "",
    ]

    segments: list[dict[str, Any]] = result.get("segments", [])
    windows_by_id: dict[int, dict[str, Any]] = {
        int(w["window_id"]): w for w in result.get("candidate_windows", [])
    }

    current_window_id: int | None = None
    for seg in segments:
        win_id = seg.get("window_id")
        if win_id is not None and win_id != current_window_id:
            current_window_id = win_id
            win = windows_by_id.get(int(win_id))
            if lines and lines[-1] != "":
                lines.append("")
            if win:
                lines.append(
                    f"=== 候选窗口 #{win['window_id']} "
                    f"[{win['start_str']} - {win['end_str']}] "
                    f"(最高RMS: {win['max_peak_rms']:.4f}) ==="
                )
            else:
                lines.append(f"=== 候选窗口 #{win_id} ===")

        tag = f" [HIGH_VOL rms={seg['peak_rms']:.4f}]" if seg.get("is_high_volume") else ""
        lines.append(f"[{seg['start_str']} - {seg['end_str']}]{tag} {seg['text']}")

    dst_txt.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return dst_txt


def _derive_stem(peaks_json_path: Path) -> str:
    """从 Step 1 报告文件名中还原原始视频名称前缀（例如 demo_audio_peaks.json -> demo）。"""
    stem = peaks_json_path.stem
    suffix = "_audio_peaks"
    if stem.endswith(suffix):
        return stem[: -len(suffix)]
    return stem


def _build_full_stream_slices(
    total_duration_sec: float,
    slice_sec: float = 180.0,
) -> list[dict[str, Any]]:
    """为 full 全量模式生成定长流式切片窗口，防止大文件一次性读爆内存。"""
    slices: list[dict[str, Any]] = []
    cursor = 0.0
    while cursor < total_duration_sec - 0.05:
        end_t = min(total_duration_sec, cursor + slice_sec)
        slices.append({"window_id": None, "start_sec": round(cursor, 2), "end_sec": round(end_t, 2)})
        cursor = end_t
    return slices


def transcribe_with_audio_peaks(
    peaks_json_path: str | Path,
    wav_path: str | Path | None = None,
    output_dir: str | Path | None = None,
    mode: str = "peaks",
    top_k_peaks: int = 20,
    context_pad_sec: float = 15.0,
    merge_gap_sec: float = 3.0,
    model_size: str | None = None,
    language: str | None = None,
    device: str = "auto",
    compute_type: str = "auto",
    asr_provider: str | None = None,
    asr_api_key: str | None = None,
    asr_base_url: str | None = None,
    asr_model: str | None = None,
    hotwords: str | None = None,
    normalize_audio: bool = True,
    vad_threshold: float = 0.30,
    concurrency: int | None = None,
    asr_caller: Callable[..., Any] | None = None,
) -> dict[str, Any]:
    """Step 2 主入口：读取 Step 1 报告与 WAV，生成带高音量标记的时间戳字幕稿。

    支持三大独立语音识别引擎（默认 `deepseek` 云端 ASR）：
    - `deepseek`：本地温和响度归一化 + 敏感 Silero VAD 切出精确语段 + 并发调用 DeepSeek `/v1/tools/asr`
    - `local`：本地 RTX CUDA / CPU 增强版 faster-whisper（默认 `large-v3-turbo` + 调优 VAD + 热词表）
    - `openai_compatible`：通用 OpenAI `/audio/transcriptions` 兼容接口（Groq / OpenAI / 硅基流动等）
    """
    if mode not in {"peaks", "full"}:
        raise ValueError(f"mode 仅支持 'peaks' 或 'full'，当前传入：{mode}")

    peaks_path = Path(peaks_json_path)
    if not peaks_path.is_file():
        raise FileNotFoundError(f"找不到 Step 1 输出文件：{peaks_path}")

    peaks_data = json.loads(peaks_path.read_text(encoding="utf-8"))
    stem = _derive_stem(peaks_path)

    # 解析音频路径：优先用参数传入，其次用 JSON 记录的路径，最后尝试同目录推测
    if wav_path is not None:
        resolved_wav = Path(wav_path)
    else:
        json_wav = peaks_data.get("wav_path", "")
        resolved_wav = Path(json_wav) if json_wav else (peaks_path.parent / f"{stem}_16k.wav")
        if not resolved_wav.is_file():
            fallback_wav = peaks_path.parent / f"{stem}_16k.wav"
            if fallback_wav.is_file():
                resolved_wav = fallback_wav

    if not resolved_wav.is_file():
        raise FileNotFoundError(f"找不到 16kHz WAV 音频文件：{resolved_wav}")

    out_dir = Path(output_dir) if output_dir is not None else peaks_path.parent
    out_dir.mkdir(parents=True, exist_ok=True)
    transcript_json_path = out_dir / f"{stem}_transcript.json"
    transcript_txt_path = out_dir / f"{stem}_transcript.txt"

    peak_segments: list[dict[str, Any]] = peaks_data.get("peak_segments", [])
    with wave.open(str(resolved_wav), "rb") as wf:
        wav_sample_rate = wf.getframerate()
        wav_duration_sec = wf.getnframes() / float(wav_sample_rate) if wav_sample_rate > 0 else 0.0
    total_duration_sec = float(peaks_data.get("total_duration_sec", 0.0)) or wav_duration_sec

    start_time = time.perf_counter()

    # 第一步：根据模式确定转写范围
    candidate_windows: list[dict[str, Any]] = []
    if mode == "peaks":
        candidate_windows = select_candidate_windows(
            peak_segments=peak_segments,
            total_duration_sec=total_duration_sec,
            top_k_peaks=top_k_peaks,
            context_pad_sec=context_pad_sec,
            merge_gap_sec=merge_gap_sec,
        )

    # 第二步：解析独立 ASR 引擎配置（peaks 模式若无候选窗口则无需校验 API Key 或加载模型）
    has_work = not (mode == "peaks" and not candidate_windows)
    effective_explicit_model = asr_model if asr_model is not None else model_size
    asr_cfg = resolve_asr_config(
        provider=asr_provider,
        api_key=asr_api_key,
        base_url=asr_base_url,
        model=effective_explicit_model,
        concurrency=concurrency,
        hotwords=hotwords,
        require_api_key=(has_work and asr_caller is None),
    )

    resolved_provider = asr_cfg["provider"]
    resolved_model = asr_cfg["model"]
    resolved_hotwords = asr_cfg["hotwords"]
    resolved_concurrency = int(asr_cfg["concurrency"])

    raw_segments: list[dict[str, Any]] = []
    actual_device = "cloud" if resolved_provider != "local" else device
    actual_compute_type = resolved_provider if resolved_provider != "local" else compute_type
    detected_language = language
    language_probability = 1.0 if language else 0.0
    transcribed_audio_sec = 0.0
    billed_audio_sec = 0.0
    asr_request_count = 0

    def _append_split_segments(
        window_id: int | None,
        w_start: float,
        w_end: float,
        rel_start: float,
        rel_end: float,
        text: str,
        words: list[Any] | None = None,
    ) -> None:
        sub_chunks = split_long_segment_by_words(
            text=text,
            start_sec=rel_start,
            end_sec=rel_end,
            words=words,
        )
        for sub in sub_chunks:
            abs_start = round(w_start + float(sub["start_sec"]), 2)
            abs_end = round(min(w_end, w_start + float(sub["end_sec"])), 2)
            if abs_end < abs_start:
                abs_end = abs_start
            entry: dict[str, Any] = {
                "start_sec": abs_start,
                "end_sec": abs_end,
                "start_str": format_timestamp(abs_start),
                "end_str": format_timestamp(abs_end),
                "text": sub["text"],
            }
            if window_id is not None:
                entry["window_id"] = window_id
            raw_segments.append(entry)

    if not has_work:
        actual_device = "skipped"
        actual_compute_type = "skipped"
    elif resolved_provider in {"deepseek", "openai_compatible"}:
        # 云端独立 ASR 引擎路径（DeepSeek `/v1/tools/asr` 或 OpenAI 兼容 `/audio/transcriptions`）
        work_slices = (
            candidate_windows
            if mode == "peaks"
            else _build_full_stream_slices(total_duration_sec, slice_sec=180.0)
        )
        for win in work_slices:
            w_start = float(win["start_sec"])
            w_end = float(win["end_sec"])
            transcribed_audio_sec += max(0.0, w_end - w_start)

            audio_np = read_wav_segment_float32(resolved_wav, w_start, w_end)
            if audio_np.size == 0:
                continue

            if resolved_provider == "deepseek":
                win_res = transcribe_window_with_deepseek_asr(
                    audio=audio_np,
                    sample_rate=wav_sample_rate,
                    api_key=asr_cfg["api_key"],
                    base_url=asr_cfg["base_url"],
                    normalize_audio=normalize_audio,
                    vad_threshold=vad_threshold,
                    concurrency=resolved_concurrency,
                    http_post=asr_caller,
                )
            else:
                win_res = transcribe_window_with_openai_compatible_asr(
                    audio=audio_np,
                    sample_rate=wav_sample_rate,
                    api_key=asr_cfg["api_key"],
                    base_url=asr_cfg["base_url"],
                    model=resolved_model,
                    language=detected_language,
                    hotwords=resolved_hotwords,
                    normalize_audio=normalize_audio,
                    vad_threshold=vad_threshold,
                    concurrency=resolved_concurrency,
                    http_post=asr_caller,
                )

            billed_audio_sec += float(win_res.get("billed_audio_sec", 0.0))
            asr_request_count += int(win_res.get("request_count", 0))
            if detected_language is None and win_res.get("detected_language"):
                detected_language = str(win_res["detected_language"])
                language_probability = 1.0

            for seg in win_res.get("segments", []):
                text = str(seg.get("text") or "").strip()
                if not text:
                    continue
                _append_split_segments(
                    window_id=win.get("window_id"),
                    w_start=w_start,
                    w_end=w_end,
                    rel_start=float(seg["start_sec"]),
                    rel_end=float(seg["end_sec"]),
                    text=text,
                    words=seg.get("words"),
                )
    else:
        # 本地增强 faster-whisper 引擎路径（RTX CUDA int8_float16/float16 + 响度归一化 + 低漏词 VAD + 热词）
        model, actual_device, actual_compute_type = load_whisper_model(
            model_size=resolved_model,
            device=device,
            compute_type=compute_type,
        )

        # 未显式指定语言时：通过最长窗口投票，仅在高置信度 (>=0.90) 时锁定单语种，防止双语主播翻车
        whisper_lang_arg = detected_language
        if detected_language is None and mode == "peaks":
            voted_lang, voted_prob = detect_dominant_language(
                model=model,
                wav_path=resolved_wav,
                candidate_windows=candidate_windows,
                normalize_audio=normalize_audio,
                vad_threshold=vad_threshold,
            )
            detected_language = voted_lang
            language_probability = voted_prob
            whisper_lang_arg = voted_lang if voted_prob >= 0.90 else None

        work_slices = (
            candidate_windows
            if mode == "peaks"
            else _build_full_stream_slices(total_duration_sec, slice_sec=300.0)
        )
        for win in work_slices:
            w_start = float(win["start_sec"])
            w_end = float(win["end_sec"])
            transcribed_audio_sec += max(0.0, w_end - w_start)

            audio_np = read_wav_segment_float32(resolved_wav, w_start, w_end)
            if audio_np.size == 0:
                continue
            if normalize_audio:
                audio_np = normalize_audio_for_asr(audio_np, wav_sample_rate)

            seg_iter, info = _run_local_whisper_transcribe(
                model=model,
                audio_input=audio_np,
                language=whisper_lang_arg,
                vad_threshold=vad_threshold,
                hotwords=resolved_hotwords,
            )
            if detected_language is None and info is not None:
                detected_language = getattr(info, "language", None)
                language_probability = round(float(getattr(info, "language_probability", 0.0)), 4)

            for seg in seg_iter:
                text = (seg.text or "").strip()
                if not text:
                    continue
                _append_split_segments(
                    window_id=win.get("window_id"),
                    w_start=w_start,
                    w_end=w_end,
                    rel_start=float(seg.start),
                    rel_end=float(seg.end),
                    text=text,
                    words=getattr(seg, "words", None),
                )

    # 第三步：按时间顺序整理并对每条字幕融合 Step 1 高音量标记
    raw_segments.sort(key=lambda s: (float(s["start_sec"]), float(s["end_sec"])))
    aligned_segments = align_segments_with_peaks(raw_segments, peak_segments)
    high_volume_count = sum(1 for s in aligned_segments if s["is_high_volume"])
    elapsed_sec = round(time.perf_counter() - start_time, 2)

    result: dict[str, Any] = {
        "video_path": peaks_data.get("video_path", ""),
        "wav_path": str(resolved_wav.resolve()),
        "peaks_json_path": str(peaks_path.resolve()),
        "transcript_json_path": str(transcript_json_path.resolve()),
        "transcript_txt_path": str(transcript_txt_path.resolve()),
        "mode": mode,
        "top_k_peaks": int(top_k_peaks),
        "asr_provider": resolved_provider,
        "asr_model": resolved_model,
        "asr_base_url": asr_cfg["base_url"],
        "model_size": resolved_model,
        "device": actual_device,
        "compute_type": actual_compute_type,
        "normalize_audio": bool(normalize_audio),
        "vad_threshold": float(vad_threshold),
        "hotwords": resolved_hotwords,
        "language": detected_language,
        "language_probability": language_probability,
        "total_duration_sec": round(total_duration_sec, 2),
        "total_duration_str": format_timestamp(total_duration_sec),
        "transcribed_audio_sec": round(transcribed_audio_sec, 2),
        "billed_audio_sec": round(billed_audio_sec, 2),
        "asr_request_count": int(asr_request_count),
        "elapsed_sec": elapsed_sec,
        "candidate_window_count": len(candidate_windows),
        "candidate_windows": candidate_windows,
        "segment_count": len(aligned_segments),
        "high_volume_count": high_volume_count,
        "segments": aligned_segments,
    }

    transcript_json_path.write_text(
        json.dumps(result, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    export_readable_transcript(result, transcript_txt_path)
    return result


def _print_summary(result: dict[str, Any]) -> None:
    """在终端打印清晰的 Step 2 字幕提取与情绪融合摘要。"""
    print("=" * 68)
    print("Step 2 语音识别与高音量情绪融合完成")
    print("=" * 68)
    print(f"源视频     : {result['video_path']}")
    print(
        f"识别引擎   : {result.get('asr_provider', 'local')} "
        f"(model={result.get('asr_model') or result['model_size']}, "
        f"device={result['device']}/{result['compute_type']}, "
        f"模式={result['mode']})"
    )
    norm_str = "开启" if result.get("normalize_audio", True) else "关闭"
    print(
        f"音频预处理 : 轻声响度归一化={norm_str} | VAD门限={result.get('vad_threshold', 0.30)} | "
        f"识别语言={result['language'] or '自动检测'}"
    )
    billed = float(result.get("billed_audio_sec", 0.0))
    billed_info = f" | 云端计费时长 {billed:.1f}s（{result.get('asr_request_count', 0)} 次请求）" if billed > 0 else ""
    print(
        f"音频时长   : 全片 {result['total_duration_str']} | "
        f"窗口总长 {result['transcribed_audio_sec']}s{billed_info} | 耗时 {result['elapsed_sec']}s"
    )
    if result["mode"] == "peaks":
        print(f"候选窗口   : {result['candidate_window_count']} 个（由 Top K 高能峰值扩展合并）")
    print(
        f"字幕统计   : 共 {result['segment_count']} 条字幕 | "
        f"其中命中高音量标记 {result['high_volume_count']} 条"
    )
    print("-" * 68)

    high_segs = [s for s in result["segments"] if s["is_high_volume"]]
    preview = sorted(high_segs, key=lambda s: float(s["peak_rms"]), reverse=True)[:5]
    if not preview:
        preview = result["segments"][:5]

    if not preview:
        print("未提取到有效语音台词（可能所选时段仅有纯音乐或背景噪音）。")
    else:
        print("高能字幕预览（按重叠峰值 RMS 从高到低展示前 5 条）：")
        for idx, seg in enumerate(preview, start=1):
            rms_tag = f"rms={seg['peak_rms']:.4f}" if seg["is_high_volume"] else "常规音量"
            print(f"{idx}. [{seg['start_str']} - {seg['end_str']}] ({rms_tag}) {seg['text']}")

    print("-" * 68)
    print(f"JSON 字幕稿 : {result['transcript_json_path']}")
    print(f"TXT 可读稿  : {result['transcript_txt_path']}")
    print("=" * 68)


def build_arg_parser() -> argparse.ArgumentParser:
    """构建命令行参数解析器。"""
    parser = argparse.ArgumentParser(
        description="Step 2：通过独立 ASR 引擎（默认 DeepSeek 云端 ASR，支持本地 large-v3-turbo）提取带时间戳字幕并融合高音量标记。"
    )
    parser.add_argument(
        "peaks_json",
        help="Step 1 生成的 *_audio_peaks.json 报告路径（如 output/shylily_1_audio_peaks.json）",
    )
    parser.add_argument(
        "--wav-path",
        default=None,
        help="可选，手动指定 16kHz WAV 音频路径（默认自动读取 JSON 中记录的路径）",
    )
    parser.add_argument(
        "--output-dir",
        default=None,
        help="输出目录（默认与 peaks_json 所在目录相同）",
    )
    parser.add_argument(
        "--mode",
        choices=["peaks", "full"],
        default="peaks",
        help="转写模式：peaks 仅转写高音量周边上下文（极速推荐），full 转写整场视频（默认：peaks）",
    )
    parser.add_argument(
        "--asr-provider",
        choices=["deepseek", "local", "openai_compatible"],
        default=None,
        help="语音识别引擎：deepseek（默认云端）/ local（本地增强 Whisper）/ openai_compatible",
    )
    parser.add_argument(
        "--asr-api-key",
        default=None,
        help="覆盖 .env 中 ASR_API_KEY 密钥（云端引擎使用）",
    )
    parser.add_argument(
        "--asr-base-url",
        default=None,
        help="覆盖 .env 中 ASR_BASE_URL 接口地址（云端引擎使用）",
    )
    parser.add_argument(
        "--asr-model",
        default=None,
        help="指定识别模型名（本地模式默认 large-v3-turbo，OpenAI 兼容默认 whisper-large-v3-turbo）",
    )
    parser.add_argument(
        "--hotwords",
        default=None,
        help="主播/游戏专词热词表（逗号分隔，例如 'Shylily, Where Winds Meet, KEKW'）",
    )
    parser.add_argument(
        "--vad-threshold",
        type=float,
        default=0.30,
        help="Silero VAD 灵敏度门限（越低越容易捞回轻声吐槽，默认：0.30）",
    )
    parser.add_argument(
        "--no-normalize",
        action="store_true",
        help="关闭 ASR 前置轻声动态响度归一化",
    )
    parser.add_argument(
        "--concurrency",
        type=int,
        default=None,
        help="云端 ASR 并发请求线程数（默认：4）",
    )
    parser.add_argument(
        "--top-k-peaks",
        type=int,
        default=20,
        help="peaks 模式下选取峰值最高的多少个锚点（默认：20）",
    )
    parser.add_argument(
        "--context-pad",
        type=float,
        default=15.0,
        help="peaks 模式下每个高能锚点前后各扩展多少秒上下文（默认：15.0）",
    )
    parser.add_argument(
        "--merge-gap",
        type=float,
        default=3.0,
        help="peaks 模式下相邻窗口间隔小于等于多少秒时自动合并（默认：3.0）",
    )
    parser.add_argument(
        "--model-size",
        default=None,
        help="兼容参数：本地 Whisper 模型规格（默认：large-v3-turbo）",
    )
    parser.add_argument(
        "--language",
        default=None,
        help="手动指定语言代码（如 en、zh、ja），不填则自动检测",
    )
    parser.add_argument(
        "--device",
        choices=["auto", "cuda", "cpu"],
        default="auto",
        help="本地模式推理设备：auto 自动优先 RTX GPU（默认：auto）",
    )
    parser.add_argument(
        "--compute-type",
        default="auto",
        help="本地模式计算精度（如 auto、int8_float16、float16、int8，默认：auto）",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    """命令行程序主入口。"""
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")

    parser = build_arg_parser()
    args = parser.parse_args(argv)

    try:
        result = transcribe_with_audio_peaks(
            peaks_json_path=args.peaks_json,
            wav_path=args.wav_path,
            output_dir=args.output_dir,
            mode=args.mode,
            top_k_peaks=args.top_k_peaks,
            context_pad_sec=args.context_pad,
            merge_gap_sec=args.merge_gap,
            model_size=args.model_size,
            language=args.language,
            device=args.device,
            compute_type=args.compute_type,
            asr_provider=args.asr_provider,
            asr_api_key=args.asr_api_key,
            asr_base_url=args.asr_base_url,
            asr_model=args.asr_model,
            hotwords=args.hotwords,
            normalize_audio=not args.no_normalize,
            vad_threshold=args.vad_threshold,
            concurrency=args.concurrency,
        )
    except (FileNotFoundError, ValueError, RuntimeError) as exc:
        print(f"[错误] {exc}", file=sys.stderr)
        return 1

    _print_summary(result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
