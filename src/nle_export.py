"""专业 NLE 互操作与 OBS 章节标记引擎。

本模块负责将 Solacut 的高光精选结果、人工审校状态、多语种双语字幕以及原片内嵌的
OBS 章节标记，无损导出为海内外主流非编软件（DaVinci Resolve / Final Cut Pro /
Adobe Premiere Pro）可直接拖入时间线精剪的标准工程文件：

1. `extract_video_chapters`: 基于项目统一安全子进程封装调用 `ffprobe`，静默提取
   OBS 录制 MP4/MKV 内嵌章节标记（防黑窗、路径规范化、畸形输出容错、控制字符清洗）。
2. `export_fcpxml`: 严格基于标准库 `xml.etree.ElementTree` 输出 FCPXML 1.10 工程，
   包含双序列专业结构：
   - 序列 1（Selected Highlights）：选中高光紧凑精剪序列 + 评分/校对状态/多语种注释标记 + 双语字幕标记
   - 序列 2（OBS Chapters & Full Timeline）：完整原片时间线 + 全量 OBS 章节 `<chapter-marker>` + 高光定位 `<marker>`
3. `export_premiere_xml`: 严格基于标准库 `xml.etree.ElementTree` 输出 Adobe Premiere Pro
   原生 FCP7 XML（`<xmeml version="5">`），彻底解决 Premiere Pro 原生无法读取 OBS MP4
   章节标记的行业痛点，同样输出双序列（精剪序列 + OBS 章节完整原片序列），支持音画联动
   与原始素材句柄保留（可自由拉长/缩短入出点）。
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import uuid
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any
from urllib.parse import quote

from src.step1_audio import (
    _parse_smpte_timecode_to_sec,
    check_ffprobe_available,
    probe_media_info,
    run_media_subprocess,
)

# 常见广播与录屏帧率到精确有理数 (exact_fps, frame_num, frame_den, timebase, is_ntsc, format_tag) 的映射
# 例如 29.97fps -> 1 帧时长为 1001/30000 秒，timebase=30, ntsc=True
_KNOWN_FPS_TABLE: tuple[tuple[float, int, int, int, bool, str], ...] = (
    (24000.0 / 1001.0, 1001, 24000, 24, True, "2398"),
    (24.0, 1, 24, 24, False, "24"),
    (25.0, 1, 25, 25, False, "25"),
    (30000.0 / 1001.0, 1001, 30000, 30, True, "2997"),
    (30.0, 1, 30, 30, False, "30"),
    (48.0, 1, 48, 48, False, "48"),
    (50.0, 1, 50, 50, False, "50"),
    (60000.0 / 1001.0, 1001, 60000, 60, True, "5994"),
    (60.0, 1, 60, 60, False, "60"),
    (120000.0 / 1001.0, 1001, 120000, 120, True, "11988"),
    (120.0, 1, 120, 120, False, "120"),
)

# VFR（如 OBS 59.83fps）强制贴靠的标准 NLE 帧率集合，杜绝非法帧时长（如 100/5983s）
_STANDARD_NLE_SNAP_TABLE: tuple[tuple[float, int, int, int, bool, str], ...] = (
    (24000.0 / 1001.0, 1001, 24000, 24, True, "2398"),
    (24.0, 1, 24, 24, False, "24"),
    (25.0, 1, 25, 25, False, "25"),
    (30000.0 / 1001.0, 1001, 30000, 30, True, "2997"),
    (30.0, 1, 30, 30, False, "30"),
    (50.0, 1, 50, 50, False, "50"),
    (60000.0 / 1001.0, 1001, 60000, 60, True, "5994"),
    (60.0, 1, 60, 60, False, "60"),
    (120.0, 1, 120, 120, False, "120"),
)

# FCPXML 1.10 DTD 严格允许的 <sequence audioRate="..."> 枚举白名单
_FCPXML_AUDIO_RATE_MAP: dict[int, str] = {
    32000: "32k",
    44100: "44.1k",
    48000: "48k",
    88200: "88.2k",
    96000: "96k",
    176400: "176.4k",
    192000: "192k",
}

_FCPXML_STANDARD_FORMAT_FPS_TAGS: frozenset[str] = frozenset(
    {"2398", "24", "25", "2997", "30", "50", "5994", "60"}
)

# XML 1.0 规范严格禁止的控制字符（保留 \t \n \r，剔除其余 0x00-0x1F、0x7F 及非法码位）
_XML_ILLEGAL_CTRL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f\ufffe\uffff]+")
_WHITESPACE_COLLAPSE_RE = re.compile(r"\s+")

_MAX_CHAPTER_TITLE_LEN = 120
_MAX_MARKER_VALUE_LEN = 180
_MAX_MARKER_NOTE_LEN = 1200


def _safe_ffprobe_bin() -> str | None:
    """安全获取 ffprobe 可执行文件路径，不可用时返回 None 而非崩溃。"""
    try:
        return check_ffprobe_available()
    except RuntimeError:
        return None


def _sanitize_xml_text(
    text: Any,
    *,
    max_len: int | None = None,
    single_line: bool = False,
) -> str:
    """清洗 XML 1.0 非法控制字符，按需折叠空白并做安全长度截断。"""
    if text is None:
        return ""
    cleaned = _XML_ILLEGAL_CTRL_RE.sub("", str(text))
    if single_line:
        cleaned = _WHITESPACE_COLLAPSE_RE.sub(" ", cleaned).strip()
    else:
        cleaned = cleaned.replace("\r\n", "\n").replace("\r", "\n").strip()
    if max_len is not None and max_len > 0 and len(cleaned) > max_len:
        if max_len > 1:
            cleaned = cleaned[: max_len - 1].rstrip() + "…"
        else:
            cleaned = cleaned[:max_len]
    return cleaned


def _safe_float(value: Any, default: float = 0.0) -> float:
    """安全将任意值转换为有限浮点数。"""
    try:
        num = float(value)
    except (TypeError, ValueError):
        return default
    if math.isnan(num) or math.isinf(num):
        return default
    return num


def _parse_rational_str(raw: str | None) -> float | None:
    """解析 ffprobe 形如 '1/1000' 或 '30000/1001' 的有理数字符串。"""
    if not raw:
        return None
    text = str(raw).strip()
    if not text:
        return None
    if "/" in text:
        num_s, den_s = text.split("/", 1)
        num = _safe_float(num_s, default=math.nan)
        den = _safe_float(den_s, default=math.nan)
        if math.isnan(num) or math.isnan(den) or den == 0.0:
            return None
        val = num / den
        return None if (math.isnan(val) or math.isinf(val)) else val
    val = _safe_float(text, default=math.nan)
    return None if math.isnan(val) else val


def _resolve_fps_entry(fps: float) -> tuple[int, int, int, bool, str]:
    """将任意（含 VFR）帧率解析并贴靠到标准 NLE 有理数帧率条目。"""
    val = _safe_float(fps, default=30.0)
    if val <= 0.5 or val > 240.0:
        val = 30.0

    for exact_fps, num, den, tb, is_ntsc, tag in _KNOWN_FPS_TABLE:
        if abs(val - exact_fps) <= 0.025:
            return num, den, tb, is_ntsc, tag

    # 对 VFR 录屏（如 59.83fps / 29.85fps）自动贴靠最接近标准 NLE 帧率，绝不输出非法 100/5983s
    _, num, den, tb, is_ntsc, tag = min(
        _STANDARD_NLE_SNAP_TABLE,
        key=lambda row: (abs(val - row[0]), row[0]),
    )
    return num, den, tb, is_ntsc, tag


def resolve_fps_rational(fps: float) -> tuple[int, int, int, bool]:
    """将浮点帧率解析为专业 NLE 所需的精确有理数四元组（VFR 自动贴靠标准 NLE 帧率）。

    返回：
        `(frame_num, frame_den, timebase, is_ntsc)`
        - `frame_num / frame_den` 为单帧时长（秒），如 29.97fps -> `(1001, 30000)`
        - `timebase` 为 Premiere / FCP7 XML 的整数时间基准，如 30
        - `is_ntsc` 表示是否为 NTSC 下拉帧率（23.976 / 29.97 / 59.94 / 119.88）
    """
    num, den, tb, is_ntsc, _ = _resolve_fps_entry(fps)
    return num, den, tb, is_ntsc


def _fps_format_tag(fps: float) -> str:
    """生成 FCPXML `<format name="FFVideoFormat...">` 标准帧率后缀标签。"""
    _, _, _, _, tag = _resolve_fps_entry(fps)
    return tag


def _frames_to_smpte_string(frames: int, timebase: int, *, drop_frame: bool = False) -> str:
    """将帧数格式化为 SMPTE 时间码字符串（HH:MM:SS:FF 或 HH:MM:SS;FF）。"""
    tb = max(1, int(timebase))
    safe_frames = max(0, int(frames))
    sep = ";" if (drop_frame and tb in {30, 60}) else ":"
    if sep == ";":
        drop_per_min = 2 if tb == 30 else 4
        frames_per_10min = tb * 600 - drop_per_min * 9
        frames_per_min = tb * 60 - drop_per_min
        ten_min_blocks, rem = divmod(safe_frames, frames_per_10min)
        if rem < tb * 60:
            extra_mins = 0
            rem_after_min = rem
        else:
            extra_mins = 1 + (rem - tb * 60) // frames_per_min
            rem_after_min = (rem - tb * 60) % frames_per_min + drop_per_min
        total_mins = ten_min_blocks * 10 + extra_mins
        hours, mins = divmod(total_mins, 60)
        secs, ff = divmod(rem_after_min, tb)
        return f"{hours:02d}:{mins:02d}:{secs:02d};{ff:02d}"

    ff = safe_frames % tb
    total_sec = safe_frames // tb
    total_min, secs = divmod(total_sec, 60)
    hours, mins = divmod(total_min, 60)
    return f"{hours:02d}:{mins:02d}:{secs:02d}:{ff:02d}"


def seconds_to_frames(seconds: float, fps: float, *, min_frames: int = 0) -> int:
    """将秒数按精确有理数帧率转换为非负整数帧号。"""
    frame_num, frame_den, _, _ = resolve_fps_rational(fps)
    safe_sec = max(0.0, _safe_float(seconds, default=0.0))
    frames = int(round(safe_sec * float(frame_den) / float(frame_num)))
    return max(min_frames, frames)


def frames_to_fcpxml_time(frames: int, frame_num: int, frame_den: int) -> str:
    """将整数帧号转换为严格对齐 `frameDuration` 整数倍的 FCPXML 有理数时间字符串。"""
    safe_frames = max(0, int(frames))
    if safe_frames == 0:
        return "0s"
    return f"{safe_frames * frame_num}/{frame_den}s"


def path_to_file_url(path: Path | str, *, premiere_localhost: bool = False) -> str:
    """将本地文件路径（含 Windows 盘符、UNC、空格、中文及特殊字符）编码为规范 file URL。

    - FCPXML 模式 (`premiere_localhost=False`):
      Windows -> `file:///C:/dir%20name/%E4%B8%AD%E6%96%87.mp4`
      POSIX   -> `file:///Users/name/video.mp4`
    - Premiere FCP7 XML 模式 (`premiere_localhost=True`):
      Windows -> `file://localhost/C%3a/dir%20name/%E4%B8%AD%E6%96%87.mp4`
      POSIX   -> `file://localhost/Users/name/video.mp4`
    """
    raw_str = str(Path(path).resolve())
    # 剥离 Windows 扩展长度前缀 \\?\ 或 \\?\UNC\
    if raw_str.startswith("\\\\?\\UNC\\"):
        raw_str = "\\\\" + raw_str[8:]
    elif raw_str.startswith("\\\\?\\"):
        raw_str = raw_str[4:]

    # Windows UNC 共享路径 \\server\share\file.mp4
    if raw_str.startswith("\\\\"):
        unc_posix = raw_str.lstrip("\\").replace("\\", "/")
        parts = unc_posix.split("/", 1)
        host = quote(parts[0], safe=".-_")
        rest = "/" + quote(parts[1], safe="/") if len(parts) > 1 else ""
        return f"file://{host}{rest}"

    posix_str = raw_str.replace("\\", "/")

    # Windows 盘符路径 C:/Users/...
    if len(posix_str) >= 2 and posix_str[1] == ":" and posix_str[0].isalpha():
        drive_letter = posix_str[0].upper()
        rest_path = posix_str[2:]
        if not rest_path.startswith("/"):
            rest_path = "/" + rest_path
        encoded_rest = quote(rest_path, safe="/")
        if premiere_localhost:
            return f"file://localhost/{drive_letter}%3a{encoded_rest}"
        return f"file:///{drive_letter}:{encoded_rest}"

    # POSIX 绝对路径 /Users/...
    if not posix_str.startswith("/"):
        posix_str = "/" + posix_str
    encoded = quote(posix_str, safe="/")
    if premiere_localhost:
        return f"file://localhost{encoded}"
    return f"file://{encoded}"


def extract_video_chapters(video_path: Path | str) -> list[dict[str, Any]]:
    """使用 ffprobe 原生静默提取视频文件内嵌章节标记（如 OBS Studio 章节标记）。

    安全保障：
    - 使用项目统一 `run_media_subprocess`（Windows 无黑窗、禁用 stdin、注入 `-hide_banner`）
    - 输入路径严格 `.resolve()`，不使用 shell 拼接，杜绝命令注入
    - 探测失败、文件不存在、无章节或畸形 JSON 时安全返回 `[]` 绝不崩溃
    - 自动清洗控制字符并截断超长章节标题

    返回：
        规范化章节列表，每项形如：
        `{"index": 1, "start_sec": 12.5, "end_sec": 45.0, "title": "Boss Kill", "source": "obs_chapter"}`
    """
    if not video_path:
        return []
    try:
        resolved = Path(video_path).resolve()
    except (OSError, ValueError):
        return []
    if not resolved.is_file():
        return []

    ffprobe_bin = _safe_ffprobe_bin()
    if not ffprobe_bin:
        return []

    cmd = [
        ffprobe_bin,
        "-v",
        "error",
        "-show_chapters",
        "-of",
        "json",
        str(resolved),
    ]
    try:
        proc = run_media_subprocess(cmd, timeout=20)
    except Exception:
        return []

    if proc.returncode != 0 or not (proc.stdout or "").strip():
        return []

    try:
        payload = json.loads(proc.stdout)
    except (json.JSONDecodeError, TypeError, ValueError):
        return []

    if not isinstance(payload, dict):
        return []

    raw_chapters = payload.get("chapters")
    if not isinstance(raw_chapters, list) or not raw_chapters:
        return []

    parsed: list[tuple[float, int, dict[str, Any]]] = []
    for raw_idx, raw in enumerate(raw_chapters):
        if not isinstance(raw, dict):
            continue

        start_sec = _safe_float(raw.get("start_time"), default=math.nan)
        end_sec = _safe_float(raw.get("end_time"), default=math.nan)

        if math.isnan(start_sec) or math.isnan(end_sec):
            tb_val = _parse_rational_str(raw.get("time_base"))
            if tb_val is not None and tb_val > 0:
                if math.isnan(start_sec) and raw.get("start") is not None:
                    start_sec = _safe_float(raw.get("start"), default=math.nan) * tb_val
                if math.isnan(end_sec) and raw.get("end") is not None:
                    end_sec = _safe_float(raw.get("end"), default=math.nan) * tb_val

        if math.isnan(start_sec):
            continue
        start_sec = max(0.0, start_sec)
        if math.isnan(end_sec) or end_sec < start_sec:
            end_sec = start_sec

        tags = raw.get("tags") if isinstance(raw.get("tags"), dict) else {}
        raw_title = ""
        for k, v in tags.items():
            if str(k).strip().lower() == "title" and v is not None:
                raw_title = str(v)
                break
        title = _sanitize_xml_text(raw_title, max_len=_MAX_CHAPTER_TITLE_LEN, single_line=True)

        parsed.append(
            (
                start_sec,
                raw_idx,
                {
                    "start_sec": round(start_sec, 3),
                    "end_sec": round(end_sec, 3),
                    "title": title,
                    "source": "obs_chapter",
                },
            )
        )

    parsed.sort(key=lambda item: (item[0], item[1]))

    chapters: list[dict[str, Any]] = []
    for idx, (_, _, item) in enumerate(parsed, start=1):
        title = item["title"] or f"Chapter {idx}"
        chapters.append(
            {
                "index": idx,
                "start_sec": item["start_sec"],
                "end_sec": item["end_sec"],
                "title": title,
                "source": "obs_chapter",
            }
        )
    return chapters


def probe_media_metadata(video_path: Path | str) -> dict[str, Any]:
    """统一复用 `step1_audio.probe_media_info` 探测源视频元数据，失败时回退安全默认值。"""
    defaults: dict[str, Any] = {
        "width": 1920,
        "height": 1080,
        "fps": 30.0,
        "duration_sec": 0.0,
        "has_video": True,
        "has_audio": True,
        "audio_channels": 2,
        "audio_rate": 48000,
        "start_timecode": "",
        "start_timecode_sec": 0.0,
    }
    if not video_path:
        return defaults
    try:
        info = probe_media_info(video_path)
    except Exception:
        return defaults

    w = int(_safe_float(info.get("width"), default=0.0))
    h = int(_safe_float(info.get("height"), default=0.0))
    if w > 0 and h > 0:
        defaults["width"] = w
        defaults["height"] = h
    fps_val = _safe_float(info.get("fps"), default=0.0)
    if 0.5 < fps_val <= 240.0:
        defaults["fps"] = round(fps_val, 3)
    dur_val = _safe_float(info.get("duration_sec"), default=0.0)
    if dur_val > 0.0:
        defaults["duration_sec"] = round(dur_val, 3)

    defaults["has_video"] = bool(info.get("has_video", True))
    has_audio = bool(info.get("has_audio", True))
    defaults["has_audio"] = has_audio
    if has_audio:
        ch = int(_safe_float(info.get("audio_channels"), default=2.0))
        sr = int(_safe_float(info.get("audio_rate"), default=48000.0))
        defaults["audio_channels"] = max(1, min(8, ch)) if ch > 0 else 2
        defaults["audio_rate"] = sr if sr >= 8000 else 48000
    else:
        defaults["audio_channels"] = 0
        defaults["audio_rate"] = 48000

    defaults["start_timecode"] = str(info.get("start_timecode") or "").strip()
    defaults["start_timecode_sec"] = max(0.0, _safe_float(info.get("start_timecode_sec"), default=0.0))
    return defaults


def _resolve_highlight_titles(
    item: dict[str, Any],
    *,
    source_language: str,
    target_language: str,
    fallback_idx: int,
) -> tuple[str, str]:
    """解析高光的 (主标题, 副标题)，兼容多语种 title_tgt/title_src 与历史 title_zh/title_en。"""
    tgt = (target_language or "zh").strip().lower()
    src = (source_language or "en").strip().lower()

    title_tgt = _sanitize_xml_text(item.get("title_tgt"), max_len=120, single_line=True)
    title_src = _sanitize_xml_text(item.get("title_src"), max_len=120, single_line=True)
    title_zh = _sanitize_xml_text(item.get("title_zh"), max_len=120, single_line=True)
    title_en = _sanitize_xml_text(item.get("title_en"), max_len=120, single_line=True)

    if title_tgt:
        primary = title_tgt
    elif tgt.startswith("en"):
        primary = title_en or title_zh or title_src or f"Highlight #{fallback_idx}"
    else:
        primary = title_zh or title_en or title_src or f"Highlight #{fallback_idx}"

    if title_src and title_src != primary:
        secondary = title_src
    elif src.startswith("zh") and title_zh and title_zh != primary:
        secondary = title_zh
    elif title_en and title_en != primary:
        secondary = title_en
    elif title_zh and title_zh != primary:
        secondary = title_zh
    else:
        secondary = ""

    return primary, secondary


def _format_score_tag(raw_score: Any) -> str:
    """将高光评分格式化为可读短标签；手动创建无评分（None）时返回 'Manual' 绝不伪造 0.0。"""
    if raw_score is None or raw_score == "":
        return "Manual"
    val = _safe_float(raw_score, default=math.nan)
    if math.isnan(val):
        return "Manual"
    return f"{val:.1f}"


def _build_highlight_note(
    item: dict[str, Any],
    *,
    primary_title: str,
    secondary_title: str,
    source_language: str,
    target_language: str,
) -> str:
    """构建专业剪辑师审校注释（含评分、校对状态、多语种标题与推荐理由）。"""
    score_tag = _format_score_tag(item.get("score"))
    review_status = _sanitize_xml_text(
        item.get("review_status") or "draft", max_len=32, single_line=True
    ).lower() or "draft"

    summary = _sanitize_xml_text(
        item.get("summary_tgt") or item.get("summary_zh") or item.get("summary") or "",
        max_len=400,
        single_line=True,
    )
    reason = _sanitize_xml_text(item.get("reason") or "", max_len=400, single_line=True)

    parts = [
        f"Score: {score_tag}",
        f"Status: {review_status}",
        f"Lang: {source_language}->{target_language}",
        f"Title: {primary_title}",
    ]
    if secondary_title:
        parts.append(f"AltTitle: {secondary_title}")
    if summary:
        parts.append(f"Summary: {summary}")
    if reason and reason != summary:
        parts.append(f"Reason: {reason}")
    return _sanitize_xml_text(" | ".join(parts), max_len=_MAX_MARKER_NOTE_LEN, single_line=True)


def _extract_subtitle_markers(
    item: dict[str, Any],
    clip_start_sec: float,
    clip_end_sec: float,
    *,
    source_language: str,
    target_language: str,
) -> list[dict[str, Any]]:
    """提取落在高光时间范围内的双语字幕条目，用于安全附挂 NLE 字幕注释标记。"""
    raw_subs = item.get("subtitles")
    if not isinstance(raw_subs, list) or not raw_subs:
        return []

    src_lang = (source_language or "en").strip().lower()
    tgt_lang = (target_language or "zh").strip().lower()
    results: list[dict[str, Any]] = []

    for sub in raw_subs:
        if not isinstance(sub, dict):
            continue
        s_start = _safe_float(sub.get("start_sec"), default=math.nan)
        s_end = _safe_float(sub.get("end_sec"), default=math.nan)
        if math.isnan(s_start) or math.isnan(s_end):
            continue
        clipped_s = max(clip_start_sec, s_start)
        clipped_e = min(clip_end_sec, s_end)
        if clipped_e - clipped_s < 0.05:
            continue

        text_tgt = _sanitize_xml_text(sub.get("text_tgt"), max_len=160, single_line=True)
        text_src = _sanitize_xml_text(sub.get("text_src"), max_len=160, single_line=True)
        text_zh = _sanitize_xml_text(sub.get("text_zh"), max_len=160, single_line=True)
        text_en = _sanitize_xml_text(sub.get("text_en"), max_len=160, single_line=True)
        text_orig = _sanitize_xml_text(
            sub.get("text_orig") or sub.get("text"), max_len=160, single_line=True
        )

        if text_tgt or text_src:
            tgt_line = text_tgt or text_zh or text_en or text_orig
            src_line = text_src or text_orig or text_en or text_zh
        elif src_lang.startswith("zh") and tgt_lang.startswith("en"):
            tgt_line = text_en or text_zh or text_orig
            src_line = text_zh or text_orig or text_en
        else:
            tgt_line = text_zh or text_en or text_orig
            src_line = text_en or text_orig or text_zh

        if not (tgt_line or src_line):
            continue
        results.append(
            {
                "start_sec": clipped_s,
                "end_sec": clipped_e,
                "text_tgt": tgt_line,
                "text_src": src_line if src_line != tgt_line else "",
            }
        )
    return results


def _normalize_chapters_input(chapters: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    """规范化外部传入的章节标记列表（清洗控制字符、排序并剔除非法时间）。"""
    if not chapters:
        return []
    cleaned: list[tuple[float, int, dict[str, Any]]] = []
    for idx, ch in enumerate(chapters):
        if not isinstance(ch, dict):
            continue
        s = _safe_float(ch.get("start_sec"), default=math.nan)
        if math.isnan(s):
            continue
        s = max(0.0, s)
        e = _safe_float(ch.get("end_sec"), default=s)
        if math.isnan(e) or e < s:
            e = s
        title = _sanitize_xml_text(ch.get("title"), max_len=_MAX_CHAPTER_TITLE_LEN, single_line=True)
        cleaned.append(
            (
                s,
                idx,
                {
                    "start_sec": s,
                    "end_sec": e,
                    "title": title,
                    "source": _sanitize_xml_text(
                        ch.get("source") or "obs_chapter", max_len=32, single_line=True
                    )
                    or "obs_chapter",
                },
            )
        )
    cleaned.sort(key=lambda pair: (pair[0], pair[1]))
    out: list[dict[str, Any]] = []
    for idx, (_, _, item) in enumerate(cleaned, start=1):
        out.append(
            {
                "index": idx,
                "start_sec": item["start_sec"],
                "end_sec": item["end_sec"],
                "title": item["title"] or f"Chapter {idx}",
                "source": item["source"],
            }
        )
    return out


def _normalize_highlights_input(
    highlights: list[dict[str, Any]] | None,
    total_duration_sec: float,
) -> list[dict[str, Any]]:
    """过滤并规范化高光片段起止时间，防止负值或零时长破坏时间轴结构。"""
    if not highlights:
        return []
    normalized: list[dict[str, Any]] = []
    for idx, item in enumerate(highlights, start=1):
        if not isinstance(item, dict):
            continue
        s = max(0.0, _safe_float(item.get("start_sec"), default=0.0))
        e = _safe_float(item.get("end_sec"), default=0.0)
        if total_duration_sec > 0.0:
            s = min(s, max(0.0, total_duration_sec - 0.1))
            e = min(e, total_duration_sec)
        if e <= s:
            continue
        copied = dict(item)
        copied["_norm_idx"] = idx
        copied["start_sec"] = s
        copied["end_sec"] = e
        normalized.append(copied)
    return normalized


def _infer_asset_duration_sec(
    explicit_duration_sec: float | None,
    probed_duration_sec: float,
    highlights: list[dict[str, Any]],
    chapters: list[dict[str, Any]],
) -> float:
    """稳健推断源片总时长，保证时间轴与源素材包络所有高光与章节标记。"""
    candidates = [
        _safe_float(explicit_duration_sec, default=0.0),
        _safe_float(probed_duration_sec, default=0.0),
    ]
    for h in highlights:
        candidates.append(_safe_float(h.get("end_sec"), default=0.0))
        candidates.append(_safe_float(h.get("start_sec"), default=0.0))
    for ch in chapters:
        candidates.append(_safe_float(ch.get("end_sec"), default=0.0))
        candidates.append(_safe_float(ch.get("start_sec"), default=0.0))
    max_dur = max(candidates) if candidates else 0.0
    return max(max_dur, 1.0)


def _atomic_write_xml(
    root: ET.Element,
    output_path: Path,
    *,
    doctype: str,
) -> Path:
    """使用标准库 ElementTree 序列化缩进 XML 并以同目录临时文件原子落盘。"""
    dst = Path(output_path)
    dst.parent.mkdir(parents=True, exist_ok=True)
    ET.indent(root, space="  ")
    body = ET.tostring(root, encoding="unicode")
    full_xml = f'<?xml version="1.0" encoding="UTF-8"?>\n{doctype}\n{body}\n'

    tmp_file = dst.parent / f".{dst.name}.part_{uuid.uuid4().hex[:8]}"
    try:
        tmp_file.write_text(full_xml, encoding="utf-8")
        os.replace(tmp_file, dst)
    finally:
        if tmp_file.exists():
            tmp_file.unlink(missing_ok=True)
    return dst.resolve()


def _format_fcpxml_format_name(width: int, height: int, fps: float) -> str | None:
    """构建严格合规的 FCPXML `<format name="...">` 名称；非标准宽高/帧率返回 None（省略 name）。"""
    fps_tag = _fps_format_tag(fps)
    if fps_tag not in _FCPXML_STANDARD_FORMAT_FPS_TAGS:
        return None
    if width == 1920 and height == 1080:
        return f"FFVideoFormat1080p{fps_tag}"
    if width == 1280 and height == 720:
        return f"FFVideoFormat720p{fps_tag}"
    if width == 3840 and height == 2160:
        return f"FFVideoFormat3840x2160p{fps_tag}"
    return None


def _resolve_source_timecode_info(
    meta: dict[str, Any],
    fps: float,
    timebase: int,
    is_ntsc: bool,
) -> tuple[int, str, str]:
    """解析源片真实起始时间码，返回 (source_tc_frames, smpte_string, displayformat)。"""
    raw_tc = str(meta.get("start_timecode") or "").strip()
    tc_sec = max(0.0, _safe_float(meta.get("start_timecode_sec"), default=0.0))
    if raw_tc and tc_sec <= 0.0:
        norm_tc, parsed_sec = _parse_smpte_timecode_to_sec(raw_tc, fps)
        raw_tc = norm_tc
        tc_sec = parsed_sec

    is_df = bool(raw_tc and ";" in raw_tc and is_ntsc and timebase in {30, 60})
    display_fmt = "DF" if is_df else "NDF"
    tc_frames = seconds_to_frames(tc_sec, fps, min_frames=0) if tc_sec > 0.0 else 0
    smpte_str = raw_tc if raw_tc else _frames_to_smpte_string(tc_frames, timebase, drop_frame=is_df)
    return tc_frames, smpte_str, display_fmt


def export_fcpxml(
    highlights: list[dict[str, Any]],
    video_path: Path | str,
    output_path: Path | str,
    fps: float = 30.0,
    total_duration_sec: float | None = None,
    chapters: list[dict[str, Any]] | None = None,
    source_language: str = "en",
    target_language: str = "zh",
    media_metadata: dict[str, Any] | None = None,
    include_subtitle_markers: bool = False,
    subtitle_lang: str | None = None,
) -> Path:
    """导出严格符合 FCPXML 1.10 DTD 的专业工程文件（供 DaVinci Resolve / Final Cut Pro 原生导入）。

    双序列专业结构：
    - `<project name="<视频名> - Selected Highlights">`：选中高光紧凑精剪序列（为空时不输出 0s 空壳），
      每段含评分/校对状态/多语种标题注释、可选字幕标记以及落入片段内的 OBS 章节标记。
    - `<project name="OBS Chapters & Full Timeline">`：完整原片单条时间线，完整挂出全部
      OBS `<chapter-marker>` 与全量高光区间 `<marker>`，一次导入同时获得精选序列与全片标记。
    """
    src_video = Path(video_path).resolve()
    dst_path = Path(output_path)

    meta = media_metadata if media_metadata is not None else probe_media_metadata(src_video)
    width = int(_safe_float(meta.get("width"), default=1920.0)) or 1920
    height = int(_safe_float(meta.get("height"), default=1080.0)) or 1080
    has_audio = bool(meta.get("has_audio", True))
    raw_ch = int(_safe_float(meta.get("audio_channels"), default=2.0 if has_audio else 0.0))
    audio_channels = max(1, min(8, raw_ch)) if has_audio else 0
    audio_rate = int(_safe_float(meta.get("audio_rate"), default=48000.0)) or 48000

    effective_sub_markers = bool(include_subtitle_markers)
    if (subtitle_lang or "").strip().lower() in {"none", "off"}:
        effective_sub_markers = False

    frame_num, frame_den, timebase, is_ntsc = resolve_fps_rational(fps)
    frame_duration_str = f"{frame_num}/{frame_den}s"
    one_frame_str = frame_duration_str

    source_tc_frames, _, tc_display_fmt = _resolve_source_timecode_info(
        meta, fps=fps, timebase=timebase, is_ntsc=is_ntsc
    )

    norm_chapters = _normalize_chapters_input(chapters)
    # 先用显式/探测时长做初步边界裁剪（若均未提供则不提前截断）
    bound_dur = _safe_float(total_duration_sec, default=0.0) or _safe_float(
        meta.get("duration_sec"), default=0.0
    )
    norm_highlights = _normalize_highlights_input(highlights, total_duration_sec=bound_dur)
    asset_duration_sec = _infer_asset_duration_sec(
        explicit_duration_sec=total_duration_sec,
        probed_duration_sec=_safe_float(meta.get("duration_sec"), default=0.0),
        highlights=norm_highlights,
        chapters=norm_chapters,
    )
    asset_total_frames = seconds_to_frames(asset_duration_sec, fps, min_frames=1)
    asset_start_str = frames_to_fcpxml_time(source_tc_frames, frame_num, frame_den)
    asset_duration_str = frames_to_fcpxml_time(asset_total_frames, frame_num, frame_den)

    file_url = path_to_file_url(src_video, premiere_localhost=False)
    asset_uid = hashlib.md5(str(src_video).encode("utf-8", errors="replace")).hexdigest().upper()
    video_name = _sanitize_xml_text(src_video.name, max_len=120, single_line=True) or "source.mp4"
    video_stem = _sanitize_xml_text(src_video.stem, max_len=80, single_line=True) or "Solacut"

    fcpxml = ET.Element("fcpxml", {"version": "1.10"})

    # 1. <resources>
    resources = ET.SubElement(fcpxml, "resources")
    fmt_attrib: dict[str, str] = {
        "id": "r1",
        "frameDuration": frame_duration_str,
        "width": str(width),
        "height": str(height),
        "colorSpace": "1-1-1 (Rec. 709)",
    }
    fmt_name = _format_fcpxml_format_name(width, height, fps)
    if fmt_name is not None:
        fmt_attrib["name"] = fmt_name
    ET.SubElement(resources, "format", fmt_attrib)

    # FCPXML 1.10 严格合规：<asset> 不允许携带已废弃的 src 属性，仅由 <media-rep> 声明媒体地址
    asset_attrib: dict[str, str] = {
        "id": "r2",
        "name": video_name,
        "uid": asset_uid,
        "start": asset_start_str,
        "duration": asset_duration_str,
        "hasVideo": "1",
        "format": "r1",
        "hasAudio": "1" if has_audio else "0",
    }
    if has_audio:
        asset_attrib["audioSources"] = "1"
        asset_attrib["audioChannels"] = str(audio_channels)
        asset_attrib["audioRate"] = str(audio_rate)

    asset_el = ET.SubElement(resources, "asset", asset_attrib)
    ET.SubElement(
        asset_el,
        "media-rep",
        {
            "kind": "original-media",
            "src": file_url,
        },
    )

    # 2. <library><event>
    library = ET.SubElement(fcpxml, "library")
    event = ET.SubElement(library, "event", {"name": f"Solacut - {video_stem}"})

    if has_audio and audio_channels >= 6:
        audio_layout = "surround"
    elif has_audio and audio_channels == 1:
        audio_layout = "mono"
    else:
        audio_layout = "stereo"
    audio_rate_label = _FCPXML_AUDIO_RATE_MAP.get(audio_rate, "48k")

    seq_cursor_frames = 0
    clip_records: list[tuple[dict[str, Any], int, int, int, int, str, str]] = []
    for seq_idx, item in enumerate(norm_highlights, start=1):
        start_f = seconds_to_frames(item["start_sec"], fps, min_frames=0)
        end_f = seconds_to_frames(item["end_sec"], fps, min_frames=start_f + 1)
        dur_f = max(1, end_f - start_f)
        primary_title, secondary_title = _resolve_highlight_titles(
            item,
            source_language=source_language,
            target_language=target_language,
            fallback_idx=seq_idx,
        )
        note_text = _build_highlight_note(
            item,
            primary_title=primary_title,
            secondary_title=secondary_title,
            source_language=source_language,
            target_language=target_language,
        )
        clip_records.append(
            (item, seq_cursor_frames, start_f, dur_f, end_f, primary_title, note_text)
        )
        seq_cursor_frames += dur_f

    # 2A. 序列 1：精选高光剪辑序列（norm_highlights 为空时不生成空 0s <spine/>）
    if clip_records:
        proj_highlights = ET.SubElement(event, "project", {"name": f"{video_stem} - Selected Highlights"})
        seq_hl_duration = frames_to_fcpxml_time(seq_cursor_frames, frame_num, frame_den)
        seq_hl = ET.SubElement(
            proj_highlights,
            "sequence",
            {
                "format": "r1",
                "duration": seq_hl_duration,
                "tcStart": "0s",
                "tcFormat": tc_display_fmt,
                "audioLayout": audio_layout,
                "audioRate": audio_rate_label,
            },
        )
        spine_hl = ET.SubElement(seq_hl, "spine")

        for seq_idx, (item, offset_f, start_f, dur_f, end_f, primary_title, note_text) in enumerate(
            clip_records, start=1
        ):
            rank = int(_safe_float(item.get("rank"), default=float(seq_idx)))
            score_tag = _format_score_tag(item.get("score"))
            review_status = (
                _sanitize_xml_text(item.get("review_status") or "draft", max_len=24, single_line=True).lower()
                or "draft"
            )
            clip_name = _sanitize_xml_text(
                f"#{rank:02d} [{score_tag}] {primary_title}",
                max_len=_MAX_MARKER_VALUE_LEN,
                single_line=True,
            )
            media_start_f = source_tc_frames + start_f

            clip_el = ET.SubElement(
                spine_hl,
                "asset-clip",
                {
                    "ref": "r2",
                    "offset": frames_to_fcpxml_time(offset_f, frame_num, frame_den),
                    "name": clip_name,
                    "start": frames_to_fcpxml_time(media_start_f, frame_num, frame_den),
                    "duration": frames_to_fcpxml_time(dur_f, frame_num, frame_den),
                    "format": "r1",
                    "tcFormat": tc_display_fmt,
                },
            )

            # 按 FCPXML 1.10 DTD 顺序：<note> 置于 <marker> / <chapter-marker> 之前
            note_el = ET.SubElement(clip_el, "note")
            note_el.text = note_text

            marker_value = _sanitize_xml_text(
                f"[#{rank:02d} {score_tag} | {review_status}] {primary_title}",
                max_len=_MAX_MARKER_VALUE_LEN,
                single_line=True,
            )
            ET.SubElement(
                clip_el,
                "marker",
                {
                    "start": frames_to_fcpxml_time(media_start_f, frame_num, frame_den),
                    "duration": one_frame_str,
                    "value": marker_value,
                    "note": note_text,
                },
            )

            # 仅在显式请求 include_subtitle_markers=True 且非无字幕模式时附挂字幕 marker，默认保持时间线洁净
            if effective_sub_markers:
                sub_rows = _extract_subtitle_markers(
                    item,
                    clip_start_sec=item["start_sec"],
                    clip_end_sec=item["end_sec"],
                    source_language=source_language,
                    target_language=target_language,
                )
                for sub in sub_rows:
                    sub_sf = max(start_f, min(end_f - 1, seconds_to_frames(sub["start_sec"], fps)))
                    sub_ef = max(sub_sf + 1, min(end_f, seconds_to_frames(sub["end_sec"], fps)))
                    sub_val = _sanitize_xml_text(
                        f"[SUB] {sub['text_tgt'] or sub['text_src']}",
                        max_len=_MAX_MARKER_VALUE_LEN,
                        single_line=True,
                    )
                    sub_note_parts = []
                    if sub["text_tgt"]:
                        sub_note_parts.append(f"{target_language}: {sub['text_tgt']}")
                    if sub["text_src"]:
                        sub_note_parts.append(f"{source_language}: {sub['text_src']}")
                    sub_note = _sanitize_xml_text(
                        " | ".join(sub_note_parts), max_len=_MAX_MARKER_NOTE_LEN, single_line=True
                    )
                    ET.SubElement(
                        clip_el,
                        "marker",
                        {
                            "start": frames_to_fcpxml_time(source_tc_frames + sub_sf, frame_num, frame_den),
                            "duration": frames_to_fcpxml_time(sub_ef - sub_sf, frame_num, frame_den),
                            "value": sub_val,
                            "note": sub_note,
                        },
                    )

            # 若有 OBS 章节标记落在此高光区间内，也一并挂在片段上方便直接定位
            for ch in norm_chapters:
                if item["start_sec"] <= ch["start_sec"] < item["end_sec"]:
                    ch_f = max(start_f, min(end_f - 1, seconds_to_frames(ch["start_sec"], fps)))
                    ET.SubElement(
                        clip_el,
                        "chapter-marker",
                        {
                            "start": frames_to_fcpxml_time(source_tc_frames + ch_f, frame_num, frame_den),
                            "duration": one_frame_str,
                            "value": _sanitize_xml_text(
                                f"OBS: {ch['title']}",
                                max_len=_MAX_MARKER_VALUE_LEN,
                                single_line=True,
                            ),
                            "posterOffset": "0s",
                        },
                    )

    # 2B. 序列 2：OBS 章节与完整原片全量标记序列
    proj_full = ET.SubElement(event, "project", {"name": "OBS Chapters & Full Timeline"})
    seq_full = ET.SubElement(
        proj_full,
        "sequence",
        {
            "format": "r1",
            "duration": asset_duration_str,
            "tcStart": asset_start_str,
            "tcFormat": tc_display_fmt,
            "audioLayout": audio_layout,
            "audioRate": audio_rate_label,
        },
    )
    spine_full = ET.SubElement(seq_full, "spine")
    full_clip = ET.SubElement(
        spine_full,
        "asset-clip",
        {
            "ref": "r2",
            "offset": asset_start_str,
            "name": video_name,
            "start": asset_start_str,
            "duration": asset_duration_str,
            "format": "r1",
            "tcFormat": tc_display_fmt,
        },
    )
    full_note = ET.SubElement(full_clip, "note")
    full_note.text = (
        f"Solacut Full Timeline | Highlights: {len(clip_records)} | "
        f"OBS Chapters: {len(norm_chapters)} | Lang: {source_language}->{target_language}"
    )

    for ch in norm_chapters:
        ch_f = min(asset_total_frames - 1, seconds_to_frames(ch["start_sec"], fps, min_frames=0))
        ET.SubElement(
            full_clip,
            "chapter-marker",
            {
                "start": frames_to_fcpxml_time(source_tc_frames + ch_f, frame_num, frame_den),
                "duration": one_frame_str,
                "value": _sanitize_xml_text(
                    ch["title"], max_len=_MAX_MARKER_VALUE_LEN, single_line=True
                ),
                "posterOffset": "0s",
            },
        )

    for seq_idx, (item, _, start_f, dur_f, _, primary_title, note_text) in enumerate(
        clip_records, start=1
    ):
        rank = int(_safe_float(item.get("rank"), default=float(seq_idx)))
        score_tag = _format_score_tag(item.get("score"))
        review_status = (
            _sanitize_xml_text(item.get("review_status") or "draft", max_len=24, single_line=True).lower()
            or "draft"
        )
        safe_sf = min(asset_total_frames - 1, start_f)
        safe_dur = max(1, min(dur_f, asset_total_frames - safe_sf))
        ET.SubElement(
            full_clip,
            "marker",
            {
                "start": frames_to_fcpxml_time(source_tc_frames + safe_sf, frame_num, frame_den),
                "duration": frames_to_fcpxml_time(safe_dur, frame_num, frame_den),
                "value": _sanitize_xml_text(
                    f"[#{rank:02d} {score_tag} | {review_status}] {primary_title}",
                    max_len=_MAX_MARKER_VALUE_LEN,
                    single_line=True,
                ),
                "note": note_text,
            },
        )

    return _atomic_write_xml(fcpxml, dst_path, doctype="<!DOCTYPE fcpxml>")


def _append_rate(parent: ET.Element, timebase: int, is_ntsc: bool) -> ET.Element:
    """向 FCP7 XML 节点追加标准 `<rate>` 子树。"""
    rate_el = ET.SubElement(parent, "rate")
    ET.SubElement(rate_el, "timebase").text = str(timebase)
    ET.SubElement(rate_el, "ntsc").text = "TRUE" if is_ntsc else "FALSE"
    return rate_el


def _append_marker(
    parent: ET.Element,
    *,
    name: str,
    comment: str,
    in_frame: int,
    out_frame: int,
) -> ET.Element:
    """向 FCP7 XML 序列或素材项追加标准 `<marker>` 节点（out <= in 自动防呆为点标记 -1）。"""
    safe_in = max(0, int(in_frame))
    safe_out = int(out_frame)
    if safe_out <= safe_in:
        safe_out = -1

    marker_el = ET.SubElement(parent, "marker")
    ET.SubElement(marker_el, "name").text = _sanitize_xml_text(
        name, max_len=_MAX_MARKER_VALUE_LEN, single_line=True
    )
    ET.SubElement(marker_el, "comment").text = _sanitize_xml_text(
        comment, max_len=_MAX_MARKER_NOTE_LEN, single_line=True
    )
    ET.SubElement(marker_el, "in").text = str(safe_in)
    ET.SubElement(marker_el, "out").text = str(safe_out)
    return marker_el


def _append_premiere_file_node(
    parent: ET.Element,
    *,
    file_id: str,
    defined: bool,
    video_name: str,
    pathurl: str,
    timebase: int,
    is_ntsc: bool,
    total_frames: int,
    width: int,
    height: int,
    has_audio: bool,
    audio_channels: int,
    audio_rate: int,
    tc_string: str = "00:00:00:00",
    tc_frame: int = 0,
    tc_displayformat: str = "NDF",
) -> ET.Element:
    """创建 Premiere `<file id="file-1">` 定义节点，首次完整定义、后续仅复用 ID 引用。"""
    file_el = ET.SubElement(parent, "file", {"id": file_id})
    if defined:
        return file_el

    ET.SubElement(file_el, "name").text = video_name
    ET.SubElement(file_el, "pathurl").text = pathurl
    _append_rate(file_el, timebase, is_ntsc)
    ET.SubElement(file_el, "duration").text = str(total_frames)

    tc_el = ET.SubElement(file_el, "timecode")
    _append_rate(tc_el, timebase, is_ntsc)
    ET.SubElement(tc_el, "string").text = tc_string
    ET.SubElement(tc_el, "frame").text = str(max(0, int(tc_frame)))
    ET.SubElement(tc_el, "displayformat").text = tc_displayformat

    media_el = ET.SubElement(file_el, "media")
    v_el = ET.SubElement(media_el, "video")
    v_sc = ET.SubElement(v_el, "samplecharacteristics")
    _append_rate(v_sc, timebase, is_ntsc)
    ET.SubElement(v_sc, "width").text = str(width)
    ET.SubElement(v_sc, "height").text = str(height)

    if has_audio:
        a_el = ET.SubElement(media_el, "audio")
        a_sc = ET.SubElement(a_el, "samplecharacteristics")
        ET.SubElement(a_sc, "depth").text = "16"
        ET.SubElement(a_sc, "samplerate").text = str(audio_rate)
        ET.SubElement(a_el, "channelcount").text = str(audio_channels)

    return file_el


def _append_link(
    parent: ET.Element,
    *,
    clip_ref: str,
    media_type: str,
    track_index: int,
    clip_index: int,
    group_index: int | None = None,
) -> None:
    """为 Premiere 音视频 `<clipitem>` 建立双向联动 `<link>`（立体声双轨附带 `<groupindex>`）。"""
    link_el = ET.SubElement(parent, "link")
    ET.SubElement(link_el, "linkclipref").text = clip_ref
    ET.SubElement(link_el, "mediatype").text = media_type
    ET.SubElement(link_el, "trackindex").text = str(track_index)
    ET.SubElement(link_el, "clipindex").text = str(clip_index)
    if group_index is not None:
        ET.SubElement(link_el, "groupindex").text = str(group_index)


def export_premiere_xml(
    highlights: list[dict[str, Any]],
    video_path: Path | str,
    output_path: Path | str,
    fps: float = 30.0,
    total_duration_sec: float | None = None,
    chapters: list[dict[str, Any]] | None = None,
    source_language: str = "en",
    target_language: str = "zh",
    media_metadata: dict[str, Any] | None = None,
    include_subtitle_markers: bool = False,
    subtitle_lang: str | None = None,
) -> Path:
    """导出 Adobe Premiere Pro 原生 FCP7 XML（`<xmeml version="5">`）。

    核心特性：
    - 顶层 `<clip id="masterclip-1">` 统一定义主素材与 `<file id="file-1">`，所有 `<clipitem>`
      声明 `<masterclipid>masterclip-1</masterclipid>` 彻底消灭跨序列脱机与重复素材；
    - 立体声（`audio_channels >= 2`）按 Premiere 规范输出双轨 exploded 立体声结构（保住右声道）；
    - 彻底解决 Premiere Pro 原生无法读取 OBS MP4 章节标记的痛点；
    - 所有 `<marker>` 自动防呆 `out <= in -> -1`。
    """
    src_video = Path(video_path).resolve()
    dst_path = Path(output_path)

    meta = media_metadata if media_metadata is not None else probe_media_metadata(src_video)
    width = int(_safe_float(meta.get("width"), default=1920.0)) or 1920
    height = int(_safe_float(meta.get("height"), default=1080.0)) or 1080
    has_audio = bool(meta.get("has_audio", True))
    raw_ch = int(_safe_float(meta.get("audio_channels"), default=2.0 if has_audio else 0.0))
    audio_channels = max(1, min(8, raw_ch)) if has_audio else 0
    audio_rate = int(_safe_float(meta.get("audio_rate"), default=48000.0)) or 48000
    is_stereo = bool(has_audio and audio_channels >= 2)

    effective_sub_markers = bool(include_subtitle_markers)
    if (subtitle_lang or "").strip().lower() in {"none", "off"}:
        effective_sub_markers = False

    _, _, timebase, is_ntsc = resolve_fps_rational(fps)
    source_tc_frames, source_tc_str, tc_display_fmt = _resolve_source_timecode_info(
        meta, fps=fps, timebase=timebase, is_ntsc=is_ntsc
    )

    norm_chapters = _normalize_chapters_input(chapters)
    bound_dur = _safe_float(total_duration_sec, default=0.0) or _safe_float(
        meta.get("duration_sec"), default=0.0
    )
    norm_highlights = _normalize_highlights_input(highlights, total_duration_sec=bound_dur)
    asset_duration_sec = _infer_asset_duration_sec(
        explicit_duration_sec=total_duration_sec,
        probed_duration_sec=_safe_float(meta.get("duration_sec"), default=0.0),
        highlights=norm_highlights,
        chapters=norm_chapters,
    )
    asset_total_frames = seconds_to_frames(asset_duration_sec, fps, min_frames=1)

    pathurl = path_to_file_url(src_video, premiere_localhost=True)
    video_name = _sanitize_xml_text(src_video.name, max_len=120, single_line=True) or "source.mp4"
    video_stem = _sanitize_xml_text(src_video.stem, max_len=80, single_line=True) or "Solacut"
    masterclip_id = "masterclip-1"
    master_uuid = str(uuid.uuid5(uuid.NAMESPACE_URL, str(src_video))).upper()

    xmeml = ET.Element("xmeml", {"version": "5"})
    project = ET.SubElement(xmeml, "project")
    ET.SubElement(project, "name").text = f"Solacut - {video_stem}"
    children = ET.SubElement(project, "children")

    file_defined = False

    def _attach_file(parent: ET.Element) -> None:
        nonlocal file_defined
        _append_premiere_file_node(
            parent,
            file_id="file-1",
            defined=file_defined,
            video_name=video_name,
            pathurl=pathurl,
            timebase=timebase,
            is_ntsc=is_ntsc,
            total_frames=asset_total_frames,
            width=width,
            height=height,
            has_audio=has_audio,
            audio_channels=audio_channels if has_audio else 2,
            audio_rate=audio_rate,
            tc_string=source_tc_str,
            tc_frame=source_tc_frames,
            tc_displayformat=tc_display_fmt,
        )
        file_defined = True

    # 0. 在 <project><children> 下定义主素材 <clip id="masterclip-1">（首次完整声明 <file id="file-1">）
    master_clip = ET.SubElement(children, "clip", {"id": masterclip_id})
    ET.SubElement(master_clip, "uuid").text = master_uuid
    ET.SubElement(master_clip, "masterclipid").text = masterclip_id
    ET.SubElement(master_clip, "ismasterclip").text = "TRUE"
    ET.SubElement(master_clip, "duration").text = str(asset_total_frames)
    _append_rate(master_clip, timebase, is_ntsc)
    ET.SubElement(master_clip, "name").text = video_name
    mc_media = ET.SubElement(master_clip, "media")
    mc_video = ET.SubElement(mc_media, "video")
    mc_vtrack = ET.SubElement(mc_video, "track")
    mc_vitem = ET.SubElement(mc_vtrack, "clipitem", {"id": "masterclip-v-1"})
    ET.SubElement(mc_vitem, "masterclipid").text = masterclip_id
    ET.SubElement(mc_vitem, "name").text = video_name
    _append_rate(mc_vitem, timebase, is_ntsc)
    ET.SubElement(mc_vitem, "duration").text = str(asset_total_frames)
    _attach_file(mc_vitem)

    if has_audio:
        mc_audio = ET.SubElement(mc_media, "audio")
        mc_track_count = 2 if is_stereo else 1
        for ch_idx in range(1, mc_track_count + 1):
            mc_atrack = ET.SubElement(mc_audio, "track")
            mc_aitem = ET.SubElement(
                mc_atrack,
                "clipitem",
                {
                    "id": f"masterclip-a-{ch_idx}",
                    "premiereChannelType": "stereo" if is_stereo else "mono",
                },
            )
            ET.SubElement(mc_aitem, "masterclipid").text = masterclip_id
            ET.SubElement(mc_aitem, "name").text = video_name
            _append_rate(mc_aitem, timebase, is_ntsc)
            ET.SubElement(mc_aitem, "duration").text = str(asset_total_frames)
            _attach_file(mc_aitem)
            mc_st = ET.SubElement(mc_aitem, "sourcetrack")
            ET.SubElement(mc_st, "mediatype").text = "audio"
            ET.SubElement(mc_st, "trackindex").text = str(ch_idx)

    def _build_sequence_skeleton(
        seq_id: str,
        seq_name: str,
        seq_duration_frames: int,
        seq_tc_str: str = "00:00:00:00",
        seq_tc_frame: int = 0,
    ) -> tuple[ET.Element, ET.Element, list[ET.Element]]:
        seq_el = ET.SubElement(children, "sequence", {"id": seq_id})
        ET.SubElement(seq_el, "name").text = _sanitize_xml_text(seq_name, max_len=140, single_line=True)
        ET.SubElement(seq_el, "duration").text = str(max(0, seq_duration_frames))
        _append_rate(seq_el, timebase, is_ntsc)

        tc_el = ET.SubElement(seq_el, "timecode")
        _append_rate(tc_el, timebase, is_ntsc)
        ET.SubElement(tc_el, "string").text = seq_tc_str
        ET.SubElement(tc_el, "frame").text = str(max(0, int(seq_tc_frame)))
        ET.SubElement(tc_el, "displayformat").text = tc_display_fmt

        media_el = ET.SubElement(seq_el, "media")
        video_el = ET.SubElement(media_el, "video")
        v_fmt = ET.SubElement(video_el, "format")
        v_sc = ET.SubElement(v_fmt, "samplecharacteristics")
        _append_rate(v_sc, timebase, is_ntsc)
        ET.SubElement(v_sc, "width").text = str(width)
        ET.SubElement(v_sc, "height").text = str(height)
        ET.SubElement(v_sc, "anamorphic").text = "FALSE"
        ET.SubElement(v_sc, "pixelaspectratio").text = "square"
        ET.SubElement(v_sc, "fielddominance").text = "none"
        v_track = ET.SubElement(video_el, "track")

        audio_el = ET.SubElement(media_el, "audio")
        out_ch_num = 2 if (is_stereo or not has_audio) else 1
        ET.SubElement(audio_el, "numOutputChannels").text = str(out_ch_num)
        a_fmt = ET.SubElement(audio_el, "format")
        a_sc = ET.SubElement(a_fmt, "samplecharacteristics")
        ET.SubElement(a_sc, "depth").text = "16"
        ET.SubElement(a_sc, "samplerate").text = str(audio_rate)

        outputs_el = ET.SubElement(audio_el, "outputs")
        grp_el = ET.SubElement(outputs_el, "group")
        ET.SubElement(grp_el, "index").text = "1"
        ET.SubElement(grp_el, "numchannels").text = str(out_ch_num)
        ET.SubElement(grp_el, "downmix").text = "0"
        for ch_i in range(1, out_ch_num + 1):
            ch_node = ET.SubElement(grp_el, "channel")
            ET.SubElement(ch_node, "index").text = str(ch_i)

        audio_tracks: list[ET.Element] = []
        if is_stereo:
            audio_tracks.append(
                ET.SubElement(
                    audio_el,
                    "track",
                    {
                        "premiereTrackType": "Stereo",
                        "currentExplodedTrackIndex": "0",
                        "totalExplodedTrackCount": "2",
                    },
                )
            )
            audio_tracks.append(
                ET.SubElement(
                    audio_el,
                    "track",
                    {
                        "premiereTrackType": "Stereo",
                        "currentExplodedTrackIndex": "1",
                        "totalExplodedTrackCount": "2",
                    },
                )
            )
        else:
            audio_tracks.append(
                ET.SubElement(
                    audio_el,
                    "track",
                    {"premiereTrackType": "Mono" if has_audio else "Stereo"},
                )
            )
        return seq_el, v_track, audio_tracks

    def _append_av_links(
        node: ET.Element,
        *,
        v_id: str,
        audio_ids: list[str],
        clip_index: int,
    ) -> None:
        _append_link(node, clip_ref=v_id, media_type="video", track_index=1, clip_index=clip_index)
        for trk_idx, a_id in enumerate(audio_ids, start=1):
            _append_link(
                node,
                clip_ref=a_id,
                media_type="audio",
                track_index=trk_idx,
                clip_index=clip_index,
                group_index=1 if is_stereo else None,
            )

    def _append_audio_clipitems(
        audio_tracks: list[ET.Element],
        *,
        v_id: str,
        audio_ids: list[str],
        clip_title: str,
        clip_index: int,
        start_tl: int,
        end_tl: int,
        in_f: int,
        out_f: int,
    ) -> None:
        ch_type = "stereo" if is_stereo else "mono"
        for trk_idx, (a_track, a_id) in enumerate(zip(audio_tracks, audio_ids), start=1):
            a_clip = ET.SubElement(
                a_track,
                "clipitem",
                {"id": a_id, "premiereChannelType": ch_type},
            )
            ET.SubElement(a_clip, "masterclipid").text = masterclip_id
            ET.SubElement(a_clip, "name").text = clip_title
            ET.SubElement(a_clip, "enabled").text = "TRUE"
            ET.SubElement(a_clip, "duration").text = str(asset_total_frames)
            _append_rate(a_clip, timebase, is_ntsc)
            ET.SubElement(a_clip, "start").text = str(start_tl)
            ET.SubElement(a_clip, "end").text = str(end_tl)
            ET.SubElement(a_clip, "in").text = str(in_f)
            ET.SubElement(a_clip, "out").text = str(out_f)
            _attach_file(a_clip)
            st_el = ET.SubElement(a_clip, "sourcetrack")
            ET.SubElement(st_el, "mediatype").text = "audio"
            ET.SubElement(st_el, "trackindex").text = str(trk_idx)
            _append_av_links(a_clip, v_id=v_id, audio_ids=audio_ids, clip_index=clip_index)

    # 1. 准备高光条目
    clip_records: list[tuple[dict[str, Any], int, int, int, int, str, str]] = []
    seq_cursor = 0
    for seq_idx, item in enumerate(norm_highlights, start=1):
        in_f = seconds_to_frames(item["start_sec"], fps, min_frames=0)
        out_f = seconds_to_frames(item["end_sec"], fps, min_frames=in_f + 1)
        dur_f = max(1, out_f - in_f)
        start_tl = seq_cursor
        end_tl = seq_cursor + dur_f
        seq_cursor = end_tl

        primary_title, secondary_title = _resolve_highlight_titles(
            item,
            source_language=source_language,
            target_language=target_language,
            fallback_idx=seq_idx,
        )
        note_text = _build_highlight_note(
            item,
            primary_title=primary_title,
            secondary_title=secondary_title,
            source_language=source_language,
            target_language=target_language,
        )
        clip_records.append((item, start_tl, end_tl, in_f, out_f, primary_title, note_text))

    # 1A. 序列 1：精选高光精剪序列（为空时不输出空 0 帧序列）
    if clip_records:
        seq1, v_track1, a_tracks1 = _build_sequence_skeleton(
            seq_id="sequence-1",
            seq_name=f"{video_stem} - Selected Highlights",
            seq_duration_frames=seq_cursor,
        )

        for idx, (item, start_tl, end_tl, in_f, out_f, primary_title, note_text) in enumerate(
            clip_records, start=1
        ):
            rank = int(_safe_float(item.get("rank"), default=float(idx)))
            score_tag = _format_score_tag(item.get("score"))
            review_status = (
                _sanitize_xml_text(item.get("review_status") or "draft", max_len=24, single_line=True).lower()
                or "draft"
            )
            clip_title = _sanitize_xml_text(
                f"#{rank:02d} [{score_tag}] {primary_title}",
                max_len=_MAX_MARKER_VALUE_LEN,
                single_line=True,
            )
            v_id = f"clipitem-v-{idx}"
            audio_ids = (
                [f"clipitem-a1-{idx}", f"clipitem-a2-{idx}"]
                if is_stereo
                else ([f"clipitem-a1-{idx}"] if has_audio else [])
            )

            v_clip = ET.SubElement(v_track1, "clipitem", {"id": v_id})
            ET.SubElement(v_clip, "masterclipid").text = masterclip_id
            ET.SubElement(v_clip, "name").text = clip_title
            ET.SubElement(v_clip, "enabled").text = "TRUE"
            ET.SubElement(v_clip, "duration").text = str(asset_total_frames)
            _append_rate(v_clip, timebase, is_ntsc)
            ET.SubElement(v_clip, "start").text = str(start_tl)
            ET.SubElement(v_clip, "end").text = str(end_tl)
            ET.SubElement(v_clip, "in").text = str(in_f)
            ET.SubElement(v_clip, "out").text = str(out_f)
            _attach_file(v_clip)

            if has_audio:
                _append_av_links(v_clip, v_id=v_id, audio_ids=audio_ids, clip_index=idx)

            # 片内高光标记（源片帧号坐标）
            marker_label = f"[#{rank:02d} {score_tag} | {review_status}] {primary_title}"
            _append_marker(
                v_clip,
                name=marker_label,
                comment=note_text,
                in_frame=in_f,
                out_frame=out_f,
            )

            # 仅显式开启且非无字幕时写入片内双语字幕注释标记
            if effective_sub_markers:
                sub_rows = _extract_subtitle_markers(
                    item,
                    clip_start_sec=item["start_sec"],
                    clip_end_sec=item["end_sec"],
                    source_language=source_language,
                    target_language=target_language,
                )
                for sub in sub_rows:
                    sub_in = max(in_f, min(out_f - 1, seconds_to_frames(sub["start_sec"], fps)))
                    sub_out = max(sub_in + 1, min(out_f, seconds_to_frames(sub["end_sec"], fps)))
                    sub_comment_parts = []
                    if sub["text_tgt"]:
                        sub_comment_parts.append(f"{target_language}: {sub['text_tgt']}")
                    if sub["text_src"]:
                        sub_comment_parts.append(f"{source_language}: {sub['text_src']}")
                    _append_marker(
                        v_clip,
                        name=f"[SUB] {sub['text_tgt'] or sub['text_src']}",
                        comment=" | ".join(sub_comment_parts),
                        in_frame=sub_in,
                        out_frame=sub_out,
                    )

            # 片内落入的 OBS 章节标记
            for ch in norm_chapters:
                if item["start_sec"] <= ch["start_sec"] < item["end_sec"]:
                    ch_in = max(in_f, min(out_f - 1, seconds_to_frames(ch["start_sec"], fps)))
                    ch_out = (
                        min(out_f, seconds_to_frames(ch["end_sec"], fps))
                        if ch["end_sec"] > ch["start_sec"]
                        else -1
                    )
                    _append_marker(
                        v_clip,
                        name=f"[OBS] {ch['title']}",
                        comment=f"OBS Chapter #{ch['index']} ({ch['start_sec']:.2f}s - {ch['end_sec']:.2f}s)",
                        in_frame=ch_in,
                        out_frame=ch_out,
                    )

            if has_audio:
                _append_audio_clipitems(
                    a_tracks1,
                    v_id=v_id,
                    audio_ids=audio_ids,
                    clip_title=clip_title,
                    clip_index=idx,
                    start_tl=start_tl,
                    end_tl=end_tl,
                    in_f=in_f,
                    out_f=out_f,
                )

            # 序列级时间轴标记（Premiere 时间标尺顶部一目了然）
            _append_marker(
                seq1,
                name=marker_label,
                comment=note_text,
                in_frame=start_tl,
                out_frame=end_tl,
            )

    # 2. 序列 2：OBS 章节与完整原片序列（解决 Premiere Pro 读不出 OBS 章节痛点）
    seq2, v_track2, a_tracks2 = _build_sequence_skeleton(
        seq_id="sequence-2",
        seq_name="OBS Chapters & Full Timeline",
        seq_duration_frames=asset_total_frames,
        seq_tc_str=source_tc_str,
        seq_tc_frame=source_tc_frames,
    )
    full_v_id = "clipitem-full-v-1"
    full_audio_ids = (
        ["clipitem-full-a1-1", "clipitem-full-a2-1"]
        if is_stereo
        else (["clipitem-full-a1-1"] if has_audio else [])
    )

    full_v_clip = ET.SubElement(v_track2, "clipitem", {"id": full_v_id})
    ET.SubElement(full_v_clip, "masterclipid").text = masterclip_id
    ET.SubElement(full_v_clip, "name").text = video_name
    ET.SubElement(full_v_clip, "enabled").text = "TRUE"
    ET.SubElement(full_v_clip, "duration").text = str(asset_total_frames)
    _append_rate(full_v_clip, timebase, is_ntsc)
    ET.SubElement(full_v_clip, "start").text = "0"
    ET.SubElement(full_v_clip, "end").text = str(asset_total_frames)
    ET.SubElement(full_v_clip, "in").text = "0"
    ET.SubElement(full_v_clip, "out").text = str(asset_total_frames)
    _attach_file(full_v_clip)
    if has_audio:
        _append_av_links(full_v_clip, v_id=full_v_id, audio_ids=full_audio_ids, clip_index=1)
        _append_audio_clipitems(
            a_tracks2,
            v_id=full_v_id,
            audio_ids=full_audio_ids,
            clip_title=video_name,
            clip_index=1,
            start_tl=0,
            end_tl=asset_total_frames,
            in_f=0,
            out_f=asset_total_frames,
        )

    # 序列 2 写入全部 OBS 章节标记与全部高光区间标记
    for ch in norm_chapters:
        ch_in = min(asset_total_frames - 1, seconds_to_frames(ch["start_sec"], fps, min_frames=0))
        ch_out = (
            min(asset_total_frames, seconds_to_frames(ch["end_sec"], fps, min_frames=ch_in + 1))
            if ch["end_sec"] > ch["start_sec"]
            else -1
        )
        _append_marker(
            seq2,
            name=f"[OBS] {ch['title']}",
            comment=f"OBS Chapter #{ch['index']} ({ch['start_sec']:.2f}s - {ch['end_sec']:.2f}s)",
            in_frame=ch_in,
            out_frame=ch_out,
        )

    for idx, (item, _, _, in_f, out_f, primary_title, note_text) in enumerate(clip_records, start=1):
        rank = int(_safe_float(item.get("rank"), default=float(idx)))
        score_tag = _format_score_tag(item.get("score"))
        review_status = (
            _sanitize_xml_text(item.get("review_status") or "draft", max_len=24, single_line=True).lower()
            or "draft"
        )
        safe_in = min(asset_total_frames - 1, in_f)
        safe_out = max(safe_in + 1, min(asset_total_frames, out_f))
        _append_marker(
            seq2,
            name=f"[#{rank:02d} {score_tag} | {review_status}] {primary_title}",
            comment=note_text,
            in_frame=safe_in,
            out_frame=safe_out,
        )

    return _atomic_write_xml(xmeml, dst_path, doctype="<!DOCTYPE xmeml>")


__all__ = [
    "extract_video_chapters",
    "export_fcpxml",
    "export_premiere_xml",
    "frames_to_fcpxml_time",
    "path_to_file_url",
    "probe_media_metadata",
    "resolve_fps_rational",
    "seconds_to_frames",
]
