"""Step 4：基于 FFmpeg 的原画质无损切片、剪辑软件标记导出与双语竖屏成片渲染。

本模块负责整个高光切片流程的最后一步（面向大公会与个人创作者的双工作流）：
1. 读取 Step 3 生成的 `<视频名>_highlights.json`（自动识别 `"selected"` 人工审片开关及
   精修后的中英双语字幕 `subtitles`）以及 Step 2 的 `<视频名>_transcript.json`。
2. 专业剪辑师工作流：
   - 导出 `<视频名>_markers.csv` 与 `<视频名>_markers.edl` 时间轴标记文件，可直接导入
     DaVinci Resolve / Premiere Pro 套在 4K 原片时间线上定位。
   - 支持 `lossless` 原画质无损切片（`FFmpeg -c copy`），并自动检测 AV1/H.264 前导关键帧
     偏移补偿，动态修正外挂 `.srt` 起止时间，彻底消除长 GOP 视频的无损字幕抢跑问题。
3. 个人博主一键成片工作流：
   - 支持 `vertical`（9:16 竖屏：前景居中完整保留游戏画面，上下高斯模糊填充）和
     `landscape`（横屏重编码）模式，默认优先调用 RTX `h264_nvenc` 硬件加速。
   - 生成专业 `.ass` 高级字幕脚本（锁定 `PlayResX`/`PlayResY` 等比排版），支持中英双语分层样式
     （上中文纯白粗体 + 下英文冰灰小字，互不遮挡）以及顶部模糊区爆款中文标题条。

命令行运行示例：
    # 1. 默认无损原画切片 + 关键帧对齐双语 SRT + 达芬奇/PR 标记文件（仅导出前 3 条测试）
    python src/step4_cut.py output/shylily_1_highlights.json --limit 3

    # 2. 一键导出 1080x1920 竖屏模糊背景 + 顶部标题条 + 中英双语硬字幕成片（仅切第 2 名）
    python src/step4_cut.py output/shylily_1_highlights.json --mode vertical --ranks 2 --subtitle-lang bilingual
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import os
import re
import shutil
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Any, Callable

# 确保既能用 `python src/step4_cut.py` 直接运行，也能用 `from src.step4_cut import ...` 导入
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from src.nle_export import (  # noqa: E402
    export_fcpxml,
    export_premiere_xml,
    extract_video_chapters,
    probe_media_metadata,
    resolve_fps_rational,
    seconds_to_frames,
)
from src.step1_audio import (  # noqa: E402
    check_ffmpeg_available,
    format_timestamp,
    probe_media_info,
    run_media_subprocess,
)
from src.step2_asr import _visual_char_len, split_long_segment_by_words  # noqa: E402
from src.step3_llm import atomic_write_json  # noqa: E402

VALID_EXPORT_MODES: frozenset[str] = frozenset(
    {
        "lossless",
        "vertical",
        "landscape",
        "nle_only",
    }
)

VALID_SUBTITLE_LANG_MODES: frozenset[str] = frozenset(
    {
        "bilingual",
        "bilingual_src_top",
        "bilingual_rev",
        "target_only",
        "target",
        "source_only",
        "source",
        "zh_only",
        "zh",
        "en_only",
        "en",
        "none",
    }
)


def normalize_subtitle_lang_mode(subtitle_lang: str | None) -> str:
    """将全部规范字幕模式与历史兼容别名归一化为内部标准值。"""
    raw = (subtitle_lang or "bilingual").strip().lower()
    alias_map: dict[str, str] = {
        "bilingual": "bilingual",
        "bilingual_src_top": "bilingual_src_top",
        "bilingual_rev": "bilingual_src_top",
        "target_only": "target_only",
        "target": "target_only",
        "zh_only": "target_only",
        "source_only": "source_only",
        "source": "source_only",
        "en_only": "source_only",
        "zh": "zh",
        "en": "en",
        "none": "none",
        "off": "none",
    }
    if raw not in alias_map:
        raise ValueError(
            "subtitle_lang 仅支持 'bilingual' / 'bilingual_src_top' / 'target_only' / 'source_only' / 'none'"
            f"（兼容 bilingual_rev / target / source / zh_only / en_only / zh / en），当前：{subtitle_lang}"
        )
    return alias_map[raw]


def _resolve_highlight_title(
    item: dict[str, Any],
    target_language: str | None = None,
    rank: int = 1,
) -> str:
    """按目标语种优先解析高光标题（优先 title_tgt，中文回落 title_zh，英文回落 title_en）。"""
    tgt = (target_language or "").strip().lower()
    title_tgt = str(item.get("title_tgt") or "").strip()
    title_zh = str(item.get("title_zh") or "").strip()
    title_en = str(item.get("title_en") or "").strip()
    if title_tgt:
        return title_tgt
    if tgt.startswith("en"):
        return title_en or title_zh or f"clip_{rank}"
    return title_zh or title_en or f"clip_{rank}"


def _resolve_ass_font_for_lang(lang_code: str | None, fallback: str = "Microsoft YaHei") -> str:
    """按语种选择 Windows 原生零缺字字体，防止日文/韩文/西里尔 ASS 渲染出现豆腐块。"""
    code = (lang_code or "").strip().lower()
    if code.startswith("zh"):
        return "Microsoft YaHei"
    if code.startswith("ja") or code == "jp":
        return "Yu Gothic UI"
    if code.startswith("ko") or code == "kr":
        return "Malgun Gothic"
    if code.startswith(("en", "ru", "es", "fr", "de")):
        return "Segoe UI"
    return fallback


def _max_width_for_lang(lang_code: str | None, is_primary: bool = True) -> int:
    """按语种视觉字符宽返回最舒适的单行自动换行阈值。"""
    code = (lang_code or "").strip().lower()
    if code.startswith(("zh", "ja", "ko")):
        return 30 if is_primary else 34
    return 40 if is_primary else 46


def _resolve_row_pair_texts(
    row: dict[str, Any],
    source_language: str | None = None,
    target_language: str | None = None,
) -> tuple[str, str]:
    """从单条字幕中解析 (主行目标译文 text_tgt, 副行源语原文 text_src)，100% 兼容历史中英字段。"""
    src_lang = (source_language or "").strip().lower()
    tgt_lang = (target_language or "").strip().lower()
    text_tgt = str(row.get("text_tgt") or "").strip()
    text_src = str(row.get("text_src") or "").strip()
    text_zh = str(row.get("text_zh") or "").strip()
    text_en = str(row.get("text_en") or "").strip()
    text_orig = str(row.get("text_orig") or "").strip()

    if text_tgt or text_src:
        return (text_tgt or text_zh or text_en or text_orig), (text_src or text_orig or text_en or text_zh)

    # 兼容历史 JSON：中文源→英译时英文是目标译文、中文是源语；其他默认中文是目标译文、text_en/orig 是源语
    if src_lang.startswith("zh") and tgt_lang.startswith("en"):
        return (text_en or text_zh), (text_zh or text_orig)
    return (text_zh or text_en or text_orig), (text_en or text_orig or text_zh)


def format_srt_timestamp(seconds: float) -> str:
    """将秒数格式化为标准 SRT 字幕时间码（HH:MM:SS,mmm）。

    用整数毫秒再逐级做整除取余，避免浮点四舍五入出现 `00:00:59,1000` 进位错误。
    """
    total_ms = max(0, int(round(seconds * 1000.0)))
    total_sec, ms = divmod(total_ms, 1000)
    total_min, secs = divmod(total_sec, 60)
    hours, mins = divmod(total_min, 60)
    return f"{hours:02d}:{mins:02d}:{secs:02d},{ms:03d}"


def format_ass_timestamp(seconds: float) -> str:
    """将秒数格式化为 ASS 高级字幕时间码（H:MM:SS.cc，百分之一秒）。"""
    total_cs = max(0, int(round(seconds * 100.0)))
    total_sec, cs = divmod(total_cs, 100)
    total_min, secs = divmod(total_sec, 60)
    hours, mins = divmod(total_min, 60)
    return f"{hours}:{mins:02d}:{secs:02d}.{cs:02d}"


def format_timecode_fps(seconds: float, fps: float = 30.0) -> str:
    """将秒数按精确有理数帧率（复用 `nle_export.seconds_to_frames`）格式化为专业剪辑时间码（HH:MM:SS:FF）。"""
    _, _, timebase, _ = resolve_fps_rational(fps)
    total_frames = seconds_to_frames(seconds, fps, min_frames=0)
    frames = total_frames % timebase
    total_sec = total_frames // timebase
    total_min, secs = divmod(total_sec, 60)
    hours, mins = divmod(total_min, 60)
    return f"{hours:02d}:{mins:02d}:{secs:02d}:{frames:02d}"


def sanitize_filename(name: str, max_len: int = 36) -> str:
    """清理标题中的 Windows 非法文件名字符，生成安全整齐的输出文件名片段。"""
    cleaned = re.sub(r'[<>:"/\\|?*\x00-\x1f]+', " ", str(name or ""))
    cleaned = re.sub(r"\s+", "_", cleaned).strip("_. ")
    if len(cleaned) > max_len:
        cleaned = cleaned[:max_len].rstrip("_. ")
    return cleaned or "clip"


def wrap_subtitle_text(text: str, max_width: int = 32, newline: str = "\n") -> str:
    """按视觉宽度（汉字=2、英文=1）智能将单句最多换为两行居中显示，避免字幕撑满屏幕。"""
    clean = re.sub(r"\s+", " ", str(text or "").strip())
    if not clean or _visual_char_len(clean) <= max_width:
        return clean

    total_w = _visual_char_len(clean)
    target_half = total_w / 2.0

    # 英文（含空格）优先在最接近视觉中点的空格处一分为二
    if " " in clean:
        best_pos = -1
        best_diff = float("inf")
        acc_w = 0
        for idx, ch in enumerate(clean):
            acc_w += _visual_char_len(ch)
            if ch == " " and idx > 0:
                diff = abs(acc_w - target_half)
                if diff < best_diff:
                    best_diff = diff
                    best_pos = idx
        if best_pos != -1:
            left = clean[:best_pos].strip()
            right = clean[best_pos + 1 :].strip()
            if left and right:
                return f"{left}{newline}{right}"

    # 纯中文等无空格字符串：在最接近中点的位置断行（尽量不在标点前断行）
    acc_w = 0
    split_idx = len(clean) // 2
    best_diff = float("inf")
    for idx in range(1, len(clean)):
        acc_w += _visual_char_len(clean[idx - 1])
        ch = clean[idx]
        if ch in "，。！？、；：,.!?;:":
            continue
        diff = abs(acc_w - target_half)
        if diff < best_diff:
            best_diff = diff
            split_idx = idx

    left = clean[:split_idx].strip()
    right = clean[split_idx:].strip()
    if left and right:
        return f"{left}{newline}{right}"
    return clean


def probe_clip_actual_start_offset(
    clipped_mp4: Path,
    requested_duration_sec: float,
) -> float:
    """测量无损切片因关键帧前移而多出的前导秒数（复用 `step1_audio.probe_media_info`）。

    在 AV1/H.264 长 GOP 直播录屏上直接 `-c copy` 时，FFmpeg 会对齐到请求起点前的最近关键帧，
    导致成片实际时长往往比 `end_sec - start_sec` 多出 0.5 ~ 4 秒；如果字幕不顺延补偿这段
    前导画面，外挂字幕就会提前几秒出现。
    """
    if not clipped_mp4.is_file():
        return 0.0
    try:
        info = probe_media_info(clipped_mp4)
        actual_duration = float(info.get("duration_sec") or 0.0)
        if actual_duration > 0.0:
            lead_in = actual_duration - float(requested_duration_sec)
            if 0.1 <= lead_in <= 15.0:
                return round(lead_in, 3)
    except Exception:
        pass
    return 0.0


def _extract_clip_subtitle_rows(
    transcript_segments: list[dict[str, Any]],
    clip_start_sec: float,
    clip_end_sec: float,
    highlight_subtitles: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """统一抽取并归零切片时间范围内的字幕条目（优先用 Step 3 双语精修字幕）。"""
    clip_duration = max(0.0, float(clip_end_sec) - float(clip_start_sec))
    rows: list[dict[str, Any]] = []

    if highlight_subtitles:
        for seg in highlight_subtitles:
            s_start = float(seg.get("start_sec", 0.0))
            s_end = float(seg.get("end_sec", 0.0))
            if max(s_start, clip_start_sec) >= min(s_end, clip_end_sec):
                continue
            rel_start = max(0.0, s_start - clip_start_sec)
            rel_end = min(clip_duration, s_end - clip_start_sec)
            if rel_end - rel_start < 0.05:
                continue

            text_zh = str(seg.get("text_zh") or "").strip()
            text_en = str(seg.get("text_en") or "").strip()
            text_src = str(seg.get("text_src") or "").strip()
            text_tgt = str(seg.get("text_tgt") or "").strip()
            text_orig = str(seg.get("text_orig") or seg.get("text") or "").strip()
            if not (text_zh or text_en or text_src or text_tgt or text_orig):
                continue
            rows.append(
                {
                    "rel_start": rel_start,
                    "rel_end": rel_end,
                    "text_zh": text_zh,
                    "text_en": text_en or text_orig,
                    "text_src": text_src,
                    "text_tgt": text_tgt,
                    "text_orig": text_orig,
                }
            )
        return rows

    # 未经过 Step 3 双语精修时：对 Step 2 字幕做紧凑断句保护后再归零
    for seg in transcript_segments:
        s_start = float(seg.get("start_sec", 0.0))
        s_end = float(seg.get("end_sec", 0.0))
        text = str(seg.get("text", "")).strip()
        if not text:
            continue
        if max(s_start, clip_start_sec) >= min(s_end, clip_end_sec):
            continue

        clipped_s = max(clip_start_sec, s_start)
        clipped_e = min(clip_end_sec, s_end)
        if clipped_e - clipped_s < 0.05:
            continue

        # 只有超过长句门槛时才切分，普通短句直接保留原文
        split_parts = split_long_segment_by_words(
            text=text,
            start_sec=clipped_s,
            end_sec=clipped_e,
            words=None,
            max_duration_sec=5.0,
            max_chars=52,
        )
        for part in split_parts:
            rel_start = max(0.0, float(part["start_sec"]) - clip_start_sec)
            rel_end = min(clip_duration, float(part["end_sec"]) - clip_start_sec)
            if rel_end - rel_start < 0.05:
                continue
            rows.append(
                {
                    "rel_start": rel_start,
                    "rel_end": rel_end,
                    "text_zh": "",
                    "text_en": part["text"],
                    "text_orig": part["text"],
                }
            )
    return rows


def generate_clip_srt(
    transcript_segments: list[dict[str, Any]],
    clip_start_sec: float,
    clip_end_sec: float,
    srt_path: str | Path,
    subtitle_lang: str = "bilingual",
    highlight_subtitles: list[dict[str, Any]] | None = None,
    source_language: str | None = None,
    target_language: str | None = None,
) -> int:
    """从多语种精修字幕或 Step 2 字幕中提取当前切片区间的台词并保存为 .srt 文件。"""
    norm_mode = normalize_subtitle_lang_mode(subtitle_lang)
    if norm_mode == "none":
        return 0

    dst_srt = Path(srt_path)
    dst_srt.parent.mkdir(parents=True, exist_ok=True)
    rows = _extract_clip_subtitle_rows(
        transcript_segments=transcript_segments,
        clip_start_sec=clip_start_sec,
        clip_end_sec=clip_end_sec,
        highlight_subtitles=highlight_subtitles,
    )

    tgt_w = _max_width_for_lang(target_language or "zh", is_primary=True)
    src_w = _max_width_for_lang(source_language or "en", is_primary=False)

    blocks: list[str] = []
    for idx, row in enumerate(rows, start=1):
        tgt_text, src_text = _resolve_row_pair_texts(
            row,
            source_language=source_language,
            target_language=target_language,
        )
        zh = row["text_zh"]
        en = row["text_en"] or row["text_orig"]

        if norm_mode == "target_only":
            body = wrap_subtitle_text(tgt_text or src_text, max_width=tgt_w, newline="\n")
        elif norm_mode == "source_only":
            body = wrap_subtitle_text(src_text or tgt_text, max_width=src_w, newline="\n")
        elif norm_mode == "zh":
            body = wrap_subtitle_text(zh or tgt_text or en, max_width=32, newline="\n")
        elif norm_mode == "en":
            body = wrap_subtitle_text(en or src_text or zh, max_width=42, newline="\n")
        elif norm_mode == "bilingual_src_top":
            if src_text and tgt_text and src_text != tgt_text:
                body = (
                    f"{wrap_subtitle_text(src_text, max_width=src_w, newline=' ')}\n"
                    f"{wrap_subtitle_text(tgt_text, max_width=tgt_w, newline=' ')}"
                )
            else:
                body = wrap_subtitle_text(src_text or tgt_text, max_width=38, newline="\n")
        else:
            if tgt_text and src_text and tgt_text != src_text:
                body = (
                    f"{wrap_subtitle_text(tgt_text, max_width=tgt_w, newline=' ')}\n"
                    f"{wrap_subtitle_text(src_text, max_width=src_w, newline=' ')}"
                )
            else:
                body = wrap_subtitle_text(tgt_text or src_text, max_width=38, newline="\n")

        blocks.append(
            f"{idx}\n"
            f"{format_srt_timestamp(row['rel_start'])} --> {format_srt_timestamp(row['rel_end'])}\n"
            f"{body}\n"
        )

    dst_srt.write_text("\n".join(blocks), encoding="utf-8")
    return len(blocks)


def _escape_ass_text(text: str) -> str:
    """转义 ASS 文本里的花括号避免被误认作样式覆盖标签。"""
    return str(text or "").replace("{", "｛").replace("}", "｝")


def generate_clip_ass(
    transcript_segments: list[dict[str, Any]],
    clip_start_sec: float,
    clip_end_sec: float,
    ass_path: str | Path,
    width: int = 1080,
    height: int = 1920,
    subtitle_lang: str = "bilingual",
    title_banner: str | None = None,
    mode: str = "vertical",
    highlight_subtitles: list[dict[str, Any]] | None = None,
    source_language: str | None = None,
    target_language: str | None = None,
) -> int:
    """生成锁定 PlayResX/Y 的专业多语种 ASS 高级字幕（支持任意双语分层排版、多语种原生字体与顶部标题条）。"""
    norm_mode = normalize_subtitle_lang_mode(subtitle_lang)
    if norm_mode == "none":
        return 0

    dst_ass = Path(ass_path)
    dst_ass.parent.mkdir(parents=True, exist_ok=True)
    clip_duration = max(0.1, float(clip_end_sec) - float(clip_start_sec))
    rows = _extract_clip_subtitle_rows(
        transcript_segments=transcript_segments,
        clip_start_sec=clip_start_sec,
        clip_end_sec=clip_end_sec,
        highlight_subtitles=highlight_subtitles,
    )

    if mode == "vertical":
        scale = float(height) / 1920.0
        title_size = max(14, round(54 * scale))
        title_margin_v = max(24, round(235 * scale))
        title_margin_lr = max(16, round(64 * scale))
        zh_size = max(12, round(46 * scale))
        en_size = max(10, round(29 * scale))
        zh_margin_v = max(24, round(432 * scale))
        en_margin_v = max(16, round(346 * scale))
        single_margin_v = max(20, round(405 * scale))
        sub_margin_lr = max(14, round(52 * scale))
        outline_zh = max(1.2, round(3.2 * scale, 1))
        outline_en = max(1.0, round(2.2 * scale, 1))
        outline_title = max(1.5, round(3.8 * scale, 1))
        shadow_sz = max(0.8, round(1.8 * scale, 1))
    else:
        scale = float(height) / 1080.0
        title_size = max(12, round(38 * scale))
        title_margin_v = max(12, round(32 * scale))
        title_margin_lr = max(20, round(60 * scale))
        zh_size = max(12, round(44 * scale))
        en_size = max(10, round(28 * scale))
        zh_margin_v = max(16, round(82 * scale))
        en_margin_v = max(8, round(34 * scale))
        single_margin_v = max(14, round(54 * scale))
        sub_margin_lr = max(20, round(60 * scale))
        outline_zh = max(1.2, round(3.0 * scale, 1))
        outline_en = max(1.0, round(2.0 * scale, 1))
        outline_title = max(1.5, round(3.4 * scale, 1))
        shadow_sz = max(0.8, round(1.5 * scale, 1))

    is_dual = norm_mode in {"bilingual", "bilingual_src_top"}
    # 顶部标题条固定采用目标语种零缺字字体；主行（Style: ZH）与副行（Style: EN）按实际上下行语种精准选字
    eff_tgt_lang = (target_language or "zh").strip().lower()
    eff_src_lang = (source_language or "en").strip().lower()
    title_font = _resolve_ass_font_for_lang(eff_tgt_lang, fallback="Microsoft YaHei")
    if norm_mode in {"bilingual_src_top", "source_only", "en"}:
        top_lang = eff_src_lang
        bot_lang = eff_tgt_lang
    else:
        top_lang = eff_tgt_lang
        bot_lang = eff_src_lang
    primary_font = _resolve_ass_font_for_lang(top_lang, fallback="Microsoft YaHei")
    secondary_font = _resolve_ass_font_for_lang(bot_lang, fallback="Segoe UI")

    # 当切换到单语模式时，将主样式放置在最佳单语位置
    zh_mv = zh_margin_v if is_dual else single_margin_v
    en_mv = en_margin_v if is_dual else single_margin_v

    header_lines = [
        "[Script Info]",
        "ScriptType: v4.00+",
        f"PlayResX: {width}",
        f"PlayResY: {height}",
        "WrapStyle: 2",
        "ScaledBorderAndShadow: yes",
        "",
        "[V4+ Styles]",
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, "
        "Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, "
        "Shadow, Alignment, MarginL, MarginR, MarginV, Encoding",
        # Title：暖亮金黄 (#FDE047 -> BGR &H0047E0FD)，粗体，顶部居中 (Alignment=8)，始终随目标语种零缺字
        f"Style: Title,{title_font},{title_size},&H0047E0FD,&H000000FF,&H00120E0A,&H80000000,"
        f"-1,0,0,0,100,100,1,0,1,{outline_title},{shadow_sz},8,{title_margin_lr},{title_margin_lr},{title_margin_v},1",
        # ZH（上行主字幕样式）：纯白粗体，黑描边，底部居中 (Alignment=2)
        f"Style: ZH,{primary_font},{zh_size},&H00FFFFFF,&H000000FF,&H00120E0A,&H64000000,"
        f"-1,0,0,0,100,100,0.5,0,1,{outline_zh},{shadow_sz},2,{sub_margin_lr},{sub_margin_lr},{zh_mv},1",
        # EN（下行副字幕样式）：柔和冰灰白 (#D5DFE8 -> BGR &H00E8DFD5)，轻描边，紧贴主行下方
        f"Style: EN,{secondary_font},{en_size},&H00E8DFD5,&H000000FF,&H00120E0A,&H64000000,"
        f"0,0,0,0,100,100,0,0,1,{outline_en},{shadow_sz},2,{sub_margin_lr},{sub_margin_lr},{en_mv},1",
        "",
        "[Events]",
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text",
    ]

    events: list[str] = []
    clean_banner = str(title_banner or "").strip()
    if clean_banner and mode == "vertical":
        banner_w = 24 if eff_tgt_lang.startswith(("zh", "ja", "ko")) else 34
        wrapped_banner = wrap_subtitle_text(clean_banner, max_width=banner_w, newline=r"\N")
        events.append(
            f"Dialogue: 1,0:00:00.00,{format_ass_timestamp(clip_duration)},Title,,0,0,0,,{_escape_ass_text(wrapped_banner)}"
        )

    top_w = _max_width_for_lang(top_lang, is_primary=True)
    bot_w = _max_width_for_lang(bot_lang, is_primary=False)

    dialog_count = 0
    for row in rows:
        t_start = format_ass_timestamp(row["rel_start"])
        t_end = format_ass_timestamp(row["rel_end"])
        tgt_text, src_text = _resolve_row_pair_texts(
            row,
            source_language=source_language,
            target_language=target_language,
        )
        zh = row["text_zh"]
        en = row["text_en"] or row["text_orig"]

        if norm_mode == "target_only":
            txt = wrap_subtitle_text(tgt_text or src_text, max_width=top_w, newline=r"\N")
            if txt:
                events.append(f"Dialogue: 0,{t_start},{t_end},ZH,,0,0,0,,{_escape_ass_text(txt)}")
                dialog_count += 1
        elif norm_mode == "source_only":
            txt = wrap_subtitle_text(src_text or tgt_text, max_width=top_w, newline=r"\N")
            if txt:
                events.append(f"Dialogue: 0,{t_start},{t_end},ZH,,0,0,0,,{_escape_ass_text(txt)}")
                dialog_count += 1
        elif norm_mode == "zh":
            txt = wrap_subtitle_text(zh or tgt_text or en, max_width=28, newline=r"\N")
            if txt:
                events.append(f"Dialogue: 0,{t_start},{t_end},ZH,,0,0,0,,{_escape_ass_text(txt)}")
                dialog_count += 1
        elif norm_mode == "en":
            txt = wrap_subtitle_text(en or src_text or zh, max_width=42, newline=r"\N")
            if txt:
                events.append(f"Dialogue: 0,{t_start},{t_end},ZH,,0,0,0,,{_escape_ass_text(txt)}")
                dialog_count += 1
        elif norm_mode == "bilingual_src_top":
            has_both = bool(src_text and tgt_text and src_text != tgt_text)
            if has_both:
                top_txt = wrap_subtitle_text(src_text, max_width=top_w, newline=r"\N")
                bot_txt = wrap_subtitle_text(tgt_text, max_width=bot_w, newline=r"\N")
                events.append(f"Dialogue: 0,{t_start},{t_end},ZH,,0,0,0,,{_escape_ass_text(top_txt)}")
                events.append(f"Dialogue: 0,{t_start},{t_end},EN,,0,0,0,,{_escape_ass_text(bot_txt)}")
                dialog_count += 1
            else:
                single_txt = wrap_subtitle_text(src_text or tgt_text, max_width=top_w, newline=r"\N")
                if single_txt:
                    events.append(f"Dialogue: 0,{t_start},{t_end},ZH,,0,0,0,,{_escape_ass_text(single_txt)}")
                    dialog_count += 1
        else:
            has_both = bool(tgt_text and src_text and tgt_text != src_text)
            if has_both:
                top_txt = wrap_subtitle_text(tgt_text, max_width=top_w, newline=r"\N")
                bot_txt = wrap_subtitle_text(src_text, max_width=bot_w, newline=r"\N")
                events.append(f"Dialogue: 0,{t_start},{t_end},ZH,,0,0,0,,{_escape_ass_text(top_txt)}")
                events.append(f"Dialogue: 0,{t_start},{t_end},EN,,0,0,0,,{_escape_ass_text(bot_txt)}")
                dialog_count += 1
            else:
                single_txt = wrap_subtitle_text(tgt_text or src_text, max_width=top_w, newline=r"\N")
                style_name = "ZH" if tgt_text else "EN"
                if single_txt:
                    events.append(
                        f"Dialogue: 0,{t_start},{t_end},{style_name},,0,0,0,,{_escape_ass_text(single_txt)}"
                    )
                    dialog_count += 1

    if not events:
        dst_ass.write_text("", encoding="utf-8")
        return 0

    dst_ass.write_text("\n".join(header_lines + events) + "\n", encoding="utf-8")
    return dialog_count if dialog_count > 0 else len(events)


_CSV_FORMULA_PREFIX_RE = re.compile(r"^[\s]*[=+\-@\t\r]")


def _sanitize_csv_cell(value: Any) -> str:
    """防御 Excel / 表格软件 CSV 公式注入：对前导 `= + - @ \\t \\r` 统一前置单引号。"""
    text = str(value or "").strip()
    if text and _CSV_FORMULA_PREFIX_RE.match(text):
        return f"'{text}"
    return text


def _parse_optional_score(raw_score: Any) -> float | None:
    """安全解析可选评分；手动片段（None/空串）返回 None 绝不伪造假分。"""
    if raw_score is None or raw_score == "":
        return None
    try:
        val = float(raw_score)
    except (TypeError, ValueError):
        return None
    return round(val, 1) if math.isfinite(val) else None


def _score_to_marker_color(score: float | None) -> str:
    """根据高光评分返回剪辑软件标记颜色标签（高分标红、次高标黄、手动/其余标绿）。"""
    if score is None:
        return "Blue"
    if score >= 9.0:
        return "Red"
    if score >= 8.0:
        return "Yellow"
    return "Green"


def export_edl_and_markers_csv(
    highlights: list[dict[str, Any]],
    csv_path: str | Path,
    edl_path: str | Path,
    video_name: str,
    fps: float = 30.0,
) -> tuple[Path, Path]:
    """导出面向 DaVinci Resolve / Premiere Pro 的 CSV 标记表与 CMX 3600 EDL 时间线文件。

    大公会剪辑师可直接把 4K 原片拉进剪辑软件并导入这两个文件，无需反复查表定位时间点。
    """
    dst_csv = Path(csv_path)
    dst_edl = Path(edl_path)
    dst_csv.parent.mkdir(parents=True, exist_ok=True)
    dst_edl.parent.mkdir(parents=True, exist_ok=True)

    tmp_csv = dst_csv.with_name(f".{dst_csv.name}.{uuid.uuid4().hex[:8]}.tmp")
    try:
        with tmp_csv.open("w", encoding="utf-8-sig", newline="") as f:
            writer = csv.writer(f)
            writer.writerow(
                [
                    "Rank",
                    "Record In",
                    "Record Out",
                    "Duration Sec",
                    "Color",
                    "Name",
                    "Notes",
                    "Start Sec",
                    "End Sec",
                    "Score",
                ]
            )
            for item in highlights:
                start_s = float(item["start_sec"])
                end_s = float(item["end_sec"])
                duration_s = round(end_s - start_s, 2)
                score_val = _parse_optional_score(item.get("score"))
                title_cell = _sanitize_csv_cell(
                    item.get("title_zh") or item.get("title_tgt") or item.get("title_en") or ""
                )
                notes_cell = _sanitize_csv_cell(
                    item.get("summary_zh") or item.get("summary_tgt") or item.get("reason") or ""
                )
                writer.writerow(
                    [
                        item.get("rank", ""),
                        format_timecode_fps(start_s, fps),
                        format_timecode_fps(end_s, fps),
                        duration_s,
                        _score_to_marker_color(score_val),
                        title_cell,
                        notes_cell,
                        round(start_s, 2),
                        round(end_s, 2),
                        "" if score_val is None else score_val,
                    ]
                )
        os.replace(tmp_csv, dst_csv)
    finally:
        if tmp_csv.exists():
            tmp_csv.unlink(missing_ok=True)

    safe_title = sanitize_filename(Path(video_name).stem, max_len=24).upper()
    edl_lines: list[str] = [
        f"TITLE: {safe_title}_HIGHLIGHTS",
        "FCM: NON-DROP FRAME",
        "",
    ]
    rec_cursor = 0.0
    for idx, item in enumerate(highlights, start=1):
        start_s = float(item["start_sec"])
        end_s = float(item["end_sec"])
        duration_s = max(0.1, end_s - start_s)
        src_in = format_timecode_fps(start_s, fps)
        src_out = format_timecode_fps(end_s, fps)
        rec_in = format_timecode_fps(rec_cursor, fps)
        rec_out = format_timecode_fps(rec_cursor + duration_s, fps)
        rec_cursor += duration_s

        rank = item.get("rank", idx)
        score_val = _parse_optional_score(item.get("score"))
        score_label = "手动" if score_val is None else f"{score_val:.1f}分"
        title_zh = str(
            item.get("title_zh") or item.get("title_tgt") or item.get("title_en") or ""
        ).strip()
        summary_zh = str(
            item.get("summary_zh") or item.get("summary_tgt") or item.get("reason") or ""
        ).strip()

        edl_lines.extend(
            [
                f"{idx:03d}  AX       V     C        {src_in} {src_out} {rec_in} {rec_out}",
                f"* FROM CLIP NAME: {video_name}",
                f"* COMMENT: [#{rank} {score_label}] {title_zh} | {summary_zh}",
                "",
            ]
        )

    tmp_edl = dst_edl.with_name(f".{dst_edl.name}.{uuid.uuid4().hex[:8]}.tmp")
    try:
        tmp_edl.write_text("\n".join(edl_lines), encoding="utf-8")
        os.replace(tmp_edl, dst_edl)
    finally:
        if tmp_edl.exists():
            tmp_edl.unlink(missing_ok=True)
    return dst_csv, dst_edl


def build_vertical_filter_complex(
    width: int = 1080,
    height: int = 1920,
    srt_filename: str | None = None,
) -> str:
    """构建横屏转 9:16 竖屏的 FFmpeg 滤镜链（高斯模糊背景 + 居中完整游戏画面 + 可选 ASS/SRT 烧录）。"""
    if width <= 0 or height <= 0:
        raise ValueError(f"目标分辨率 width/height 必须为正整数，当前：{width}x{height}")

    base_label = "[vbase]" if srt_filename else "[vout]"
    parts = [
        f"[0:v]scale={width}:{height}:force_original_aspect_ratio=increase,"
        f"crop={width}:{height},boxblur=25:5,eq=brightness=-0.06[bg]",
        f"[0:v]scale={width}:-2:force_original_aspect_ratio=decrease[fg]",
        f"[bg][fg]overlay=(W-w)/2:(H-h)/2{base_label}",
    ]

    if srt_filename:
        if srt_filename.lower().endswith(".ass"):
            parts.append(f"[vbase]ass={srt_filename}[vout]")
        else:
            style = (
                "FontSize=13,PrimaryColour=&H00FFFFFF,OutlineColour=&H00000000,"
                "BorderStyle=1,Outline=2,Shadow=1,Alignment=2,MarginV=44"
            )
            parts.append(
                f"[vbase]subtitles={srt_filename}:charenc=UTF-8:force_style='{style}'[vout]"
            )

    return ";".join(parts)


def _run_ffmpeg(cmd: list[str], cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    """统一静默执行 FFmpeg 命令并捕获 UTF-8 日志（Windows 无黑窗 + 禁用 stdin）。"""
    return run_media_subprocess(cmd, cwd=cwd)


def _is_transient_ffmpeg_error(stderr: str, returncode: int) -> bool:
    """判断 FFmpeg 错误是否属于可重试的瞬时中断/IO 锁冲突。"""
    low = (stderr or "").lower()
    if returncode in {-15, 15, 255} and "received signal" in low:
        return True
    transient_markers = (
        "received signal",
        "resource temporarily unavailable",
        "device or resource busy",
        "permission denied",
        "error opening input: end of file",
        "error opening input files: end of file",
    )
    return any(m in low for m in transient_markers)


def _is_corrupt_container_error(stderr: str) -> bool:
    """判断 FFmpeg 错误是否提示源视频容器/原子轻度异常（适合容错重读）。"""
    low = (stderr or "").lower()
    corrupt_markers = (
        "corrupted",
        "stsc",
        "error reading header",
        "moov atom",
        "invalid data found",
        "invalid nal unit",
        "non-monotonous dts",
    )
    return any(m in low for m in corrupt_markers)


def _is_mp4_audio_mux_error(stderr: str) -> bool:
    """判断无损封装 MP4 时是否因源视频音轨编码不兼容（如 OBS MKV 的 PCM/FLAC/Opus）而失败。"""
    low = (stderr or "").lower()
    mux_markers = (
        "could not find tag for codec",
        "not currently supported in container",
        "incompatible with output codec",
        "error muxing a packet",
        "could not write header",
        "invalid audio stream",
    )
    return any(m in low for m in mux_markers)


def _diagnose_ffmpeg_error(stderr: str) -> str:
    """将底层 FFmpeg 英文报错翻译为面向创作者的可读中文排查建议。"""
    low = (stderr or "").lower()
    if "no space left on device" in low or "disk full" in low:
        return "原因诊断：目标磁盘可用空间不足，请清理硬盘空间或更换输出目录。"
    if "permission denied" in low:
        return "原因诊断：目标文件正被播放器/剪辑软件占用，或当前目录无写入权限，请关闭占用后重试。"
    if "received signal" in low:
        return "原因诊断：FFmpeg 进程被外部信号中止，请确认系统/杀软未拦截 ffmpeg.exe 后重试。"
    if any(k in low for k in ("corrupted", "stsc", "moov atom", "error reading header", "invalid data found")):
        return "原因诊断：源视频文件头部/索引疑似受损或不完整（常见于录屏断电未正常封尾），建议先用 OBS「录像重封装」或 FFmpeg 修复源片。"
    if any(k in low for k in ("nvenc", "cannot load nvcuda", "no capable devices found")):
        return "原因诊断：显卡硬件编码器（NVENC）不可用，请在导出设置中改选「自动」或「x264 软编码」。"
    if any(k in low for k in ("not divisible by 2", "width not divisible", "height not divisible")):
        return "原因诊断：源视频宽高不是偶数导致编码器报错，请重试或升级至最新版本。"
    return ""


def _format_ffmpeg_error_report(prefix: str, stderr: str, max_lines: int = 12) -> str:
    """组装带中文诊断的精简 FFmpeg 错误消息。"""
    non_empty = [line for line in (stderr or "").splitlines() if line.strip()]
    tail = "\n".join(non_empty[-max_lines:]) if non_empty else "未知错误（FFmpeg 未输出详细日志）"
    diag = _diagnose_ffmpeg_error(stderr)
    if diag:
        return f"{prefix}\n{diag}\n底层日志：\n{tail}"
    return f"{prefix}\n{tail}"


def _atomic_replace_output(tmp_file: Path, dst_file: Path) -> None:
    """原子替换目标切片文件，并针对 Windows 播放器文件锁做短退避与中文友好报错。"""
    for attempt in range(2):
        try:
            os.replace(tmp_file, dst_file)
            return
        except PermissionError as exc:
            if attempt == 0:
                time.sleep(0.3)
                continue
            raise RuntimeError(
                f"目标文件正被其他软件（如播放器或剪辑软件）占用，无法覆盖写入：{dst_file.name}。"
                "请关闭正在播放该切片的程序后重试。"
            ) from exc


def cut_single_clip(
    video_path: str | Path,
    start_sec: float,
    end_sec: float,
    out_mp4: str | Path,
    mode: str = "lossless",
    srt_path: str | Path | None = None,
    ass_path: str | Path | None = None,
    burn_subtitles: bool = True,
    encoder: str = "auto",
    width: int = 1080,
    height: int = 1920,
) -> dict[str, Any]:
    """使用 FFmpeg 导出单条高光视频片段（支持无损快切、竖屏模糊背景、横屏重编码）。"""
    if mode not in {"lossless", "vertical", "landscape"}:
        raise ValueError(f"mode 仅支持 'lossless' / 'vertical' / 'landscape'，当前传入：{mode}")
    if encoder not in {"auto", "nvenc", "x264"}:
        raise ValueError(f"encoder 仅支持 'auto' / 'nvenc' / 'x264'，当前传入：{encoder}")

    src_video = Path(video_path).resolve()
    dst_mp4 = Path(out_mp4).resolve()
    if not src_video.is_file():
        raise FileNotFoundError(f"找不到源视频文件：{src_video}")
    safe_start = max(0.0, float(start_sec))
    safe_end = float(end_sec)
    if safe_end <= safe_start:
        raise ValueError(f"切片起止时间无效：{start_sec} -> {end_sec}")

    ffmpeg_bin = check_ffmpeg_available()
    dst_mp4.parent.mkdir(parents=True, exist_ok=True)
    duration = max(0.1, safe_end - safe_start)
    t0 = time.perf_counter()

    # 统一采用同目录临时文件原子写入，彻底杜绝失败遗留 0KB/损坏半截 MP4
    tmp_mp4 = dst_mp4.parent / f".part_{uuid.uuid4().hex[:10]}.mp4"

    # 1. 原画质无损切片模式：直接流拷贝（支持瞬时重试、坏容器容错与 OBS MKV 音轨自动转 AAC 降级）
    if mode == "lossless":
        def _build_lossless_cmd(
            audio_args: list[str],
            tolerant: bool = False,
        ) -> list[str]:
            cmd = [ffmpeg_bin, "-y"]
            if tolerant:
                cmd.extend(["-err_detect", "ignore_err", "-fflags", "+genpts+discardcorrupt"])
            cmd.extend(
                [
                    "-ss",
                    f"{safe_start:.3f}",
                    "-to",
                    f"{safe_end:.3f}",
                    "-i",
                    str(src_video),
                    "-map",
                    "0:v:0",
                    "-map",
                    "0:a?",
                    "-dn",
                    "-sn",
                    "-c:v",
                    "copy",
                    *audio_args,
                    "-avoid_negative_ts",
                    "make_zero",
                    "-movflags",
                    "+faststart",
                    tmp_mp4.name,
                ]
            )
            return cmd

        used_encoder = ""
        last_stderr = ""
        try:
            # 第 1 轮：纯流拷贝（原画+原音）
            proc = _run_ffmpeg(_build_lossless_cmd(["-c:a", "copy"], tolerant=False), cwd=dst_mp4.parent)
            if proc.returncode == 0 and tmp_mp4.is_file() and tmp_mp4.stat().st_size > 0:
                used_encoder = "copy"
            else:
                last_stderr = proc.stderr
                tmp_mp4.unlink(missing_ok=True)

                # 第 2 轮：若为瞬时信号中断/锁冲突或容器头部轻度异常，自动退避 + 容错标志重试
                if _is_transient_ffmpeg_error(last_stderr, proc.returncode) or _is_corrupt_container_error(last_stderr):
                    time.sleep(0.35)
                    tolerant_flag = _is_corrupt_container_error(last_stderr)
                    proc_retry = _run_ffmpeg(
                        _build_lossless_cmd(["-c:a", "copy"], tolerant=tolerant_flag),
                        cwd=dst_mp4.parent,
                    )
                    if proc_retry.returncode == 0 and tmp_mp4.is_file() and tmp_mp4.stat().st_size > 0:
                        used_encoder = "copy"
                    else:
                        last_stderr = proc_retry.stderr
                        tmp_mp4.unlink(missing_ok=True)

                # 第 3 轮：若源视频（常见 OBS .mkv）含 MP4 不支持的音轨（PCM/FLAC/Opus），
                # 自动降级为「视频原画 100% 无损直拷 + 音频转 AAC 192k」
                if not used_encoder and _is_mp4_audio_mux_error(last_stderr):
                    proc_aac = _run_ffmpeg(
                        _build_lossless_cmd(["-c:a", "aac", "-b:a", "192k"], tolerant=True),
                        cwd=dst_mp4.parent,
                    )
                    if proc_aac.returncode == 0 and tmp_mp4.is_file() and tmp_mp4.stat().st_size > 0:
                        used_encoder = "copy+aac"
                    else:
                        last_stderr = proc_aac.stderr
                        tmp_mp4.unlink(missing_ok=True)

            if not used_encoder:
                raise RuntimeError(_format_ffmpeg_error_report("FFmpeg 无损切片失败：", last_stderr, max_lines=12))

            _atomic_replace_output(tmp_mp4, dst_mp4)
        finally:
            if tmp_mp4.exists():
                tmp_mp4.unlink(missing_ok=True)

        lead_in_offset = probe_clip_actual_start_offset(dst_mp4, requested_duration_sec=duration)
        return {
            "mp4_path": str(dst_mp4),
            "encoder": used_encoder,
            "burned_subtitles": False,
            "lead_in_offset_sec": lead_in_offset,
            "elapsed_sec": round(time.perf_counter() - t0, 2),
        }

    # 2. 重编码模式（vertical / landscape）：优先采用 ASS 精细排版字幕，其次兼容 SRT
    src_sub: Path | None = None
    if ass_path is not None and Path(ass_path).is_file() and Path(ass_path).stat().st_size > 0:
        src_sub = Path(ass_path).resolve()
    elif srt_path is not None and Path(srt_path).is_file() and Path(srt_path).stat().st_size > 0:
        src_sub = Path(srt_path).resolve()

    should_burn = bool(burn_subtitles and src_sub is not None)

    tmp_sub_path: Path | None = None
    tmp_sub_name: str | None = None
    if should_burn and src_sub is not None:
        ext = src_sub.suffix.lower() or ".ass"
        tmp_sub_name = f"_burn_tmp_{uuid.uuid4().hex[:8]}{ext}"
        tmp_sub_path = dst_mp4.parent / tmp_sub_name
        shutil.copyfile(src_sub, tmp_sub_path)

    if encoder == "auto":
        encoder_candidates = [
            ("h264_nvenc", ["-c:v", "h264_nvenc", "-preset", "p4", "-cq", "20", "-pix_fmt", "yuv420p"]),
            ("libx264", ["-c:v", "libx264", "-preset", "fast", "-crf", "20", "-pix_fmt", "yuv420p"]),
        ]
    elif encoder == "nvenc":
        encoder_candidates = [
            ("h264_nvenc", ["-c:v", "h264_nvenc", "-preset", "p4", "-cq", "20", "-pix_fmt", "yuv420p"]),
        ]
    else:
        encoder_candidates = [
            ("libx264", ["-c:v", "libx264", "-preset", "fast", "-crf", "20", "-pix_fmt", "yuv420p"]),
        ]

    def _build_reencode_cmd(enc_args: list[str], tolerant: bool = False) -> list[str]:
        cmd = [ffmpeg_bin, "-y"]
        if tolerant:
            cmd.extend(["-err_detect", "ignore_err", "-fflags", "+genpts+discardcorrupt"])
        cmd.extend(
            [
                "-ss",
                f"{safe_start:.3f}",
                "-i",
                str(src_video),
                "-t",
                f"{duration:.3f}",
            ]
        )

        if mode == "vertical":
            filter_str = build_vertical_filter_complex(
                width=width,
                height=height,
                srt_filename=tmp_sub_name,
            )
            cmd.extend(["-filter_complex", filter_str, "-map", "[vout]", "-map", "0:a?", "-dn", "-sn"])
        else:
            # 横屏重编码统一做偶数宽高对齐，杜绝奇数窗口录屏（如 853x480）在 yuv420p/x264/nvenc 下报错
            vf_parts = ["scale=trunc(iw/2)*2:trunc(ih/2)*2"]
            if tmp_sub_name:
                if tmp_sub_name.lower().endswith(".ass"):
                    vf_parts.append(f"ass={tmp_sub_name}")
                else:
                    style = (
                        "FontSize=14,PrimaryColour=&H00FFFFFF,OutlineColour=&H00000000,"
                        "BorderStyle=1,Outline=2,Shadow=1,Alignment=2,MarginV=24"
                    )
                    vf_parts.append(f"subtitles={tmp_sub_name}:charenc=UTF-8:force_style='{style}'")
            cmd.extend(["-vf", ",".join(vf_parts), "-map", "0:v:0", "-map", "0:a?", "-dn", "-sn"])

        cmd.extend(enc_args)
        cmd.extend(["-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart", tmp_mp4.name])
        return cmd

    used_encoder = ""
    last_stderr = ""
    try:
        for idx, (enc_name, enc_args) in enumerate(encoder_candidates):
            tmp_mp4.unlink(missing_ok=True)
            proc = _run_ffmpeg(_build_reencode_cmd(enc_args, tolerant=False), cwd=dst_mp4.parent)
            if proc.returncode == 0 and tmp_mp4.is_file() and tmp_mp4.stat().st_size > 0:
                used_encoder = enc_name
                break

            last_stderr = proc.stderr
            tmp_mp4.unlink(missing_ok=True)

            # 遇到瞬时中断或源容器轻度受损时先用同编码器容错重试一次
            if _is_transient_ffmpeg_error(last_stderr, proc.returncode) or _is_corrupt_container_error(last_stderr):
                time.sleep(0.35)
                proc_retry = _run_ffmpeg(
                    _build_reencode_cmd(enc_args, tolerant=_is_corrupt_container_error(last_stderr)),
                    cwd=dst_mp4.parent,
                )
                if proc_retry.returncode == 0 and tmp_mp4.is_file() and tmp_mp4.stat().st_size > 0:
                    used_encoder = enc_name
                    break
                last_stderr = proc_retry.stderr
                tmp_mp4.unlink(missing_ok=True)

            if idx == 0 and len(encoder_candidates) > 1:
                print(
                    f"[警告] {enc_name} 硬件编码失败，自动切换为 libx264 软编码继续渲染...",
                    file=sys.stderr,
                )

        if not used_encoder:
            raise RuntimeError(_format_ffmpeg_error_report("FFmpeg 视频渲染失败：", last_stderr, max_lines=14))

        _atomic_replace_output(tmp_mp4, dst_mp4)
    finally:
        if tmp_mp4.exists():
            tmp_mp4.unlink(missing_ok=True)
        if tmp_sub_path is not None and tmp_sub_path.exists():
            tmp_sub_path.unlink(missing_ok=True)

    return {
        "mp4_path": str(dst_mp4),
        "encoder": used_encoder,
        "burned_subtitles": should_burn,
        "lead_in_offset_sec": 0.0,
        "elapsed_sec": round(time.perf_counter() - t0, 2),
    }


def _derive_stem(highlights_json_path: Path) -> str:
    """从 Step 3 方案文件名还原视频前缀（例如 shylily_1_highlights.json -> shylily_1）。"""
    stem = highlights_json_path.stem
    suffix = "_highlights"
    if stem.endswith(suffix):
        return stem[: -len(suffix)]
    return stem


def render_highlight_clips(
    highlights_json_path: str | Path,
    video_path: str | Path | None = None,
    transcript_json_path: str | Path | None = None,
    output_dir: str | Path | None = None,
    mode: str = "lossless",
    burn_subtitles: bool = True,
    subtitle_lang: str = "bilingual",
    show_title_banner: bool = False,
    encoder: str = "auto",
    width: int = 1080,
    height: int = 1920,
    limit: int | None = None,
    ranks: list[int] | None = None,
    on_progress: Callable[[str, int, str], None] | None = None,
    only_reviewed: bool = False,
) -> dict[str, Any]:
    """Step 4 主入口：按审片结果批量导出 NLE 工程交付包（FCPXML / Premiere XML / EDL / CSV）、双语字幕与视频切片。"""
    if mode not in VALID_EXPORT_MODES:
        raise ValueError(
            f"mode 仅支持 'lossless' / 'vertical' / 'landscape' / 'nle_only'，当前传入：{mode}"
        )
    norm_sub_mode = normalize_subtitle_lang_mode(subtitle_lang)

    highlights_path = Path(highlights_json_path)
    if not highlights_path.is_file():
        raise FileNotFoundError(f"找不到 Step 3 高光方案 JSON：{highlights_path}")

    if limit is not None and limit < 0:
        raise ValueError(f"limit 不能为负数，当前传入：{limit}")

    data = json.loads(highlights_path.read_text(encoding="utf-8"))
    stem = _derive_stem(highlights_path)
    hl_src_lang = str(data.get("source_language") or "").strip().lower() or None
    hl_tgt_lang = str(data.get("target_language") or "").strip().lower() or None

    resolved_video_str = str(video_path) if video_path is not None else str(data.get("video_path", ""))
    resolved_video = Path(resolved_video_str) if resolved_video_str else Path("")
    if not resolved_video_str or not resolved_video.is_file():
        raise FileNotFoundError(
            f"找不到原始视频文件：{resolved_video_str or '（空路径）'}，请通过 --video-path 参数显式指定。"
        )

    transcript_segments: list[dict[str, Any]] = []
    if transcript_json_path is not None:
        resolved_transcript = Path(transcript_json_path)
        if not resolved_transcript.is_file():
            raise FileNotFoundError(f"找不到指定的字幕文件：{resolved_transcript}")
        transcript_data = json.loads(resolved_transcript.read_text(encoding="utf-8"))
        transcript_segments = transcript_data.get("segments", [])
    else:
        json_transcript_str = str(data.get("transcript_json_path", ""))
        candidate_transcript = (
            Path(json_transcript_str)
            if json_transcript_str
            else (highlights_path.parent / f"{stem}_transcript.json")
        )
        if candidate_transcript.is_file():
            transcript_data = json.loads(candidate_transcript.read_text(encoding="utf-8"))
            transcript_segments = transcript_data.get("segments", [])

    out_dir = Path(output_dir) if output_dir is not None else (highlights_path.parent / f"{stem}_clips")
    out_dir.mkdir(parents=True, exist_ok=True)

    all_highlights: list[dict[str, Any]] = data.get("highlights", [])
    if ranks is not None:
        if len(ranks) == 0:
            raise ValueError("ranks 不能为空列表；如需导出全部已选片段请传 ranks=None。")
        # 显式指定定点 ranks 时按排名匹配，但不允许绕过 only_reviewed 审校门禁
        rank_set = {int(r) for r in ranks}
        selected_items = [item for item in all_highlights if int(item.get("rank", 0)) in rank_set]
        if not selected_items:
            raise ValueError(f"找不到指定的片段排名：{ranks}")
    else:
        selected_items = [item for item in all_highlights if item.get("selected", True) is True]

    # 统一强制执行 only_reviewed 门禁（无论是否显式传入 ranks）
    if only_reviewed:
        selected_items = [
            item
            for item in selected_items
            if str(item.get("review_status") or "draft").strip().lower() == "reviewed"
        ]
        if not selected_items:
            raise ValueError(
                "当前没有已标记为「已人工校对」的高光片段，请先完成审校或取消勾选「仅导出已人工校对」"
            )
    if limit is not None:
        selected_items = selected_items[:limit]

    total_t0 = time.perf_counter()
    media_meta = probe_media_metadata(resolved_video)
    fps = float(media_meta.get("fps") or 30.0)
    video_duration_sec = float(media_meta.get("duration_sec") or 0.0) or float(data.get("total_duration_sec") or 0.0)
    chapters = extract_video_chapters(resolved_video)
    if not chapters and isinstance(data.get("chapters"), list):
        chapters = [ch for ch in data["chapters"] if isinstance(ch, dict)]

    ass_w, ass_h = (width, height) if mode == "vertical" else (int(media_meta["width"]), int(media_meta["height"]))

    # 先对全部选中高光执行统一入出点边界校验与裁剪，保证 NLE 时间线与切片/SRT 坐标 100% 一致
    prepared_items: list[dict[str, Any]] = []
    for seq_idx, item in enumerate(selected_items, start=1):
        rank = int(item.get("rank", seq_idx))
        start_sec = max(0.0, float(item["start_sec"]))
        end_sec = float(item["end_sec"])
        if video_duration_sec > 0:
            start_sec = min(start_sec, max(0.0, video_duration_sec - 0.1))
            end_sec = min(end_sec, video_duration_sec)
        if end_sec <= start_sec:
            raise ValueError(f"片段 #{rank} 的起止时间无效（{start_sec:.2f}s -> {end_sec:.2f}s），请先在审片区修正入出点。")
        cloned = dict(item)
        cloned["start_sec"] = start_sec
        cloned["end_sec"] = end_sec
        prepared_items.append(cloned)

    if not prepared_items:
        raise ValueError("没有可导出的有效高光片段（请至少勾选 1 条片段或检查筛选条件），已中止导出以保护现有产物。")

    csv_path = out_dir / f"{stem}_markers.csv"
    edl_path = out_dir / f"{stem}_markers.edl"
    fcpxml_path = out_dir / f"{stem}_timeline.fcpxml"
    premiere_xml_path = out_dir / f"{stem}_premiere.xml"

    export_edl_and_markers_csv(
        highlights=prepared_items,
        csv_path=csv_path,
        edl_path=edl_path,
        video_name=resolved_video.name,
        fps=fps,
    )
    export_fcpxml(
        highlights=prepared_items,
        video_path=resolved_video,
        output_path=fcpxml_path,
        fps=fps,
        total_duration_sec=video_duration_sec or None,
        chapters=chapters,
        source_language=hl_src_lang or "en",
        target_language=hl_tgt_lang or "zh",
        media_metadata=media_meta,
        include_subtitle_markers=False,
        subtitle_lang=norm_sub_mode,
    )
    export_premiere_xml(
        highlights=prepared_items,
        video_path=resolved_video,
        output_path=premiere_xml_path,
        fps=fps,
        total_duration_sec=video_duration_sec or None,
        chapters=chapters,
        source_language=hl_src_lang or "en",
        target_language=hl_tgt_lang or "zh",
        media_metadata=media_meta,
        include_subtitle_markers=False,
        subtitle_lang=norm_sub_mode,
    )

    exported_clips: list[dict[str, Any]] = []
    total_count = len(prepared_items)

    # 极速 NLE 工程包交付模式：零视频编码、零转码等待，仅产出 NLE 工程文件与独立双语 SRT
    if mode == "nle_only":
        if on_progress is not None:
            on_progress(
                "step4",
                35,
                f"已生成 FCPXML / Premiere XML / EDL / CSV（检测到 {len(chapters)} 个 OBS 章节）...",
            )
        for seq_idx, item in enumerate(prepared_items, start=1):
            rank = int(item.get("rank", seq_idx))
            score_val = _parse_optional_score(item.get("score"))
            score_tag = "手动" if score_val is None else f"{score_val:.1f}分"
            start_sec = float(item["start_sec"])
            end_sec = float(item["end_sec"])
            title_display = _resolve_highlight_title(item, target_language=hl_tgt_lang, rank=rank)
            title_zh = str(item.get("title_zh") or title_display)
            safe_title = sanitize_filename(title_display, max_len=36)
            item_subs = item.get("subtitles")

            base_name = f"{rank:02d}_{score_tag}_{safe_title}"
            out_srt = out_dir / f"{base_name}.srt"
            srt_count = 0
            if norm_sub_mode != "none":
                srt_count = generate_clip_srt(
                    transcript_segments=transcript_segments,
                    clip_start_sec=start_sec,
                    clip_end_sec=end_sec,
                    srt_path=out_srt,
                    subtitle_lang=norm_sub_mode,
                    highlight_subtitles=item_subs,
                    source_language=hl_src_lang,
                    target_language=hl_tgt_lang,
                )

            exported_clips.append(
                {
                    "rank": rank,
                    "score": score_val,
                    "title_tgt": title_display,
                    "title_zh": title_zh,
                    "start_sec": start_sec,
                    "end_sec": end_sec,
                    "start_str": item.get("start_str") or format_timestamp(start_sec),
                    "end_str": item.get("end_str") or format_timestamp(end_sec),
                    "duration_sec": round(end_sec - start_sec, 2),
                    "mp4_path": "",
                    "srt_path": str(out_srt.resolve()) if norm_sub_mode != "none" else "",
                    "srt_line_count": srt_count,
                    "subtitle_lang": norm_sub_mode,
                    "lead_in_offset_sec": 0.0,
                    "encoder": "nle_only",
                    "burned_subtitles": False,
                    "size_mb": 0.0,
                    "elapsed_sec": 0.0,
                }
            )
            if on_progress is not None:
                pct = 40 + int((seq_idx / max(1, total_count)) * 55)
                on_progress(
                    "step4",
                    min(96, pct),
                    f"[{seq_idx}/{total_count}] 已打包 NLE 工程与字幕 #{rank:02d} {title_display}",
                )
    else:
        for seq_idx, item in enumerate(prepared_items, start=1):
            rank = int(item.get("rank", seq_idx))
            score_val = _parse_optional_score(item.get("score"))
            score_tag = "手动" if score_val is None else f"{score_val:.1f}分"
            start_sec = float(item["start_sec"])
            end_sec = float(item["end_sec"])
            title_display = _resolve_highlight_title(item, target_language=hl_tgt_lang, rank=rank)
            title_zh = str(item.get("title_zh") or title_display)
            safe_title = sanitize_filename(title_display, max_len=36)
            item_subs = item.get("subtitles")

            if on_progress is not None:
                pct = 15 + int(((seq_idx - 1) / max(1, total_count)) * 80)
                on_progress(
                    "step4",
                    pct,
                    f"[{seq_idx}/{total_count}] 正在导出 #{rank:02d} {title_display}（{format_timestamp(start_sec)}-{format_timestamp(end_sec)}）...",
                )

            base_name = f"{rank:02d}_{score_tag}_{safe_title}"
            out_mp4 = out_dir / f"{base_name}.mp4"
            out_srt = out_dir / f"{base_name}.srt"
            tmp_ass_for_render = out_dir / f"_render_style_{uuid.uuid4().hex[:6]}.ass"
            srt_existed_before = out_srt.exists()

            srt_count = 0
            if norm_sub_mode != "none":
                srt_count = generate_clip_srt(
                    transcript_segments=transcript_segments,
                    clip_start_sec=start_sec,
                    clip_end_sec=end_sec,
                    srt_path=out_srt,
                    subtitle_lang=norm_sub_mode,
                    highlight_subtitles=item_subs,
                    source_language=hl_src_lang,
                    target_language=hl_tgt_lang,
                )

            ass_events_count = 0
            effective_burn = bool(burn_subtitles and norm_sub_mode != "none")
            if mode != "lossless" and effective_burn:
                ass_events_count = generate_clip_ass(
                    transcript_segments=transcript_segments,
                    clip_start_sec=start_sec,
                    clip_end_sec=end_sec,
                    ass_path=tmp_ass_for_render,
                    width=ass_w,
                    height=ass_h,
                    subtitle_lang=norm_sub_mode,
                    title_banner=title_display if show_title_banner else None,
                    mode=mode,
                    highlight_subtitles=item_subs,
                    source_language=hl_src_lang,
                    target_language=hl_tgt_lang,
                )

            try:
                cut_info = cut_single_clip(
                    video_path=resolved_video,
                    start_sec=start_sec,
                    end_sec=end_sec,
                    out_mp4=out_mp4,
                    mode=mode,
                    srt_path=out_srt if srt_count > 0 else None,
                    ass_path=tmp_ass_for_render if ass_events_count > 0 else None,
                    burn_subtitles=effective_burn,
                    encoder=encoder,
                    width=width,
                    height=height,
                )
            except Exception:
                # 切片失败时清理本次新建的孤儿 .srt，绝不留下无对应视频的垃圾字幕
                if not srt_existed_before and out_srt.exists():
                    out_srt.unlink(missing_ok=True)
                raise
            finally:
                if tmp_ass_for_render.exists():
                    tmp_ass_for_render.unlink(missing_ok=True)

            # 若为无损模式且存在明显前导关键帧偏移，重新生成对齐后的 SRT 防止字幕抢跑
            lead_in = float(cut_info.get("lead_in_offset_sec", 0.0))
            if mode == "lossless" and lead_in > 0.0 and norm_sub_mode != "none":
                aligned_start = max(0.0, start_sec - lead_in)
                srt_count = generate_clip_srt(
                    transcript_segments=transcript_segments,
                    clip_start_sec=aligned_start,
                    clip_end_sec=end_sec,
                    srt_path=out_srt,
                    subtitle_lang=norm_sub_mode,
                    highlight_subtitles=item_subs,
                    source_language=hl_src_lang,
                    target_language=hl_tgt_lang,
                )

            size_mb = round(out_mp4.stat().st_size / (1024.0 * 1024.0), 2)
            if on_progress is not None:
                pct = 15 + int((seq_idx / max(1, total_count)) * 80)
                enc_tag = cut_info["encoder"]
                aac_note = "（已自动转 AAC 兼容封装）" if enc_tag == "copy+aac" else ""
                on_progress(
                    "step4",
                    min(96, pct),
                    f"[{seq_idx}/{total_count}] 完成 #{rank:02d} ({size_mb:.2f}MB | {cut_info['elapsed_sec']}s | {enc_tag}){aac_note}",
                )
            exported_clips.append(
                {
                    "rank": rank,
                    "score": score_val,
                    "title_tgt": title_display,
                    "title_zh": title_zh,
                    "start_sec": start_sec,
                    "end_sec": end_sec,
                    "start_str": item.get("start_str") or format_timestamp(start_sec),
                    "end_str": item.get("end_str") or format_timestamp(end_sec),
                    "duration_sec": round(end_sec - start_sec, 2),
                    "mp4_path": cut_info["mp4_path"],
                    "srt_path": str(out_srt.resolve()) if norm_sub_mode != "none" else "",
                    "srt_line_count": srt_count,
                    "subtitle_lang": norm_sub_mode,
                    "lead_in_offset_sec": lead_in,
                    "encoder": cut_info["encoder"],
                    "burned_subtitles": cut_info["burned_subtitles"],
                    "size_mb": size_mb,
                    "elapsed_sec": cut_info["elapsed_sec"],
                }
            )

    exported_clip_count = sum(1 for c in exported_clips if c.get("mp4_path"))
    manifest_path = out_dir / f"{stem}_clips_manifest.json"
    result: dict[str, Any] = {
        "video_path": str(resolved_video.resolve()),
        "highlights_json_path": str(highlights_path.resolve()),
        "output_dir": str(out_dir.resolve()),
        "mode": mode,
        "subtitle_lang": norm_sub_mode,
        "show_title_banner": show_title_banner,
        "only_reviewed": bool(only_reviewed),
        "fps": fps,
        "chapters_count": len(chapters),
        "selected_count": len(prepared_items),
        "exported_count": len(exported_clips),
        "exported_clip_count": exported_clip_count,
        "nle_item_count": len(prepared_items),
        "elapsed_sec": round(time.perf_counter() - total_t0, 2),
        "fcpxml_path": str(fcpxml_path.resolve()),
        "premiere_xml_path": str(premiere_xml_path.resolve()),
        "markers_csv_path": str(csv_path.resolve()),
        "markers_edl_path": str(edl_path.resolve()),
        "manifest_json_path": str(manifest_path.resolve()),
        "clips": exported_clips,
    }

    atomic_write_json(manifest_path, result, indent=2)
    return result


def _parse_ranks_arg(ranks_str: str | None) -> list[int] | None:
    """把逗号分隔的排名字符串（如 '1,2,5'）解析为整数列表。"""
    if ranks_str is None or not ranks_str.strip():
        return None
    parts = [p.strip() for p in ranks_str.split(",") if p.strip()]
    ranks: list[int] = []
    for p in parts:
        try:
            val = int(p)
        except ValueError as exc:
            raise ValueError(f"--ranks 参数格式错误（应为逗号分隔整数，如 1,2,5）：{ranks_str}") from exc
        if val < 1:
            raise ValueError(f"排名必须为正整数，当前传入：{val}")
        ranks.append(val)
    return ranks


def _print_summary(result: dict[str, Any]) -> None:
    """在终端打印清晰的 Step 4 出片清单摘要。"""
    print("=" * 72)
    print("Step 4 切片导出与专业 NLE 时间线工程生成完成")
    print("=" * 72)
    print(f"原视频     : {result['video_path']}")
    print(
        f"导出模式   : {result['mode']} | 字幕语言 {result['subtitle_lang']} | "
        f"帧率 {result['fps']} fps | OBS 章节 {result.get('chapters_count', 0)} 个 | "
        f"导出 {result['exported_count']} 条 | 总耗时 {result['elapsed_sec']} 秒"
    )
    print("-" * 72)

    clips = result["clips"]
    if not clips:
        print("没有符合条件（selected=true / --ranks）的待切片段，仅生成了空表头标记文件。")
    else:
        for clip in clips:
            burn_tag = "含烧录双语字幕" if clip["burned_subtitles"] else f"外挂SRT({clip['srt_line_count']}句)"
            offset_note = (
                f" | 关键帧补偿-{clip['lead_in_offset_sec']:.2f}s"
                if clip.get("lead_in_offset_sec", 0.0) > 0
                else ""
            )
            score_disp = "手动" if clip.get("score") is None else f"{float(clip['score']):.1f}分"
            print(
                f"#{clip['rank']:02d} [{score_disp}] {clip['start_str']}-{clip['end_str']} "
                f"({clip['duration_sec']:.1f}s | {clip['size_mb']:.2f}MB | {clip['encoder']} | {burn_tag}{offset_note})"
            )
            if clip.get("mp4_path"):
                print(f"    -> {Path(clip['mp4_path']).name}")
            elif clip.get("srt_path"):
                print(f"    -> (NLE 工程模式) {Path(clip['srt_path']).name}")

    print("-" * 72)
    print(f"输出目录       : {result['output_dir']}")
    if result.get("fcpxml_path"):
        print(f"DaVinci/FCP    : {result['fcpxml_path']}")
    if result.get("premiere_xml_path"):
        print(f"Premiere XML   : {result['premiere_xml_path']}")
    print(f"达芬奇/Excel表 : {result['markers_csv_path']}")
    print(f"PR/达芬奇 EDL  : {result['markers_edl_path']}")
    print(f"切片汇总清单   : {result['manifest_json_path']}")
    print("=" * 72)


def build_arg_parser() -> argparse.ArgumentParser:
    """构建命令行参数解析器。"""
    parser = argparse.ArgumentParser(
        description="Step 4：按审片结果导出专业 NLE 工程交付包（FCPXML / Premiere XML / EDL / CSV）、原画质无损切片或双语竖屏短视频。"
    )
    parser.add_argument(
        "highlights_json",
        help="Step 3 生成的 *_highlights.json 路径（如 output/shylily_1_highlights.json）",
    )
    parser.add_argument(
        "--video-path",
        default=None,
        help="可选，显式指定原画质源视频路径（例如用 4K 原片替换代理视频）",
    )
    parser.add_argument(
        "--transcript-json",
        default=None,
        help="可选，显式指定 Step 2 *_transcript.json 路径",
    )
    parser.add_argument(
        "--output-dir",
        default=None,
        help="输出文件夹路径（默认在同目录下新建 <视频名>_clips 文件夹）",
    )
    parser.add_argument(
        "--mode",
        choices=["lossless", "vertical", "landscape", "nle_only"],
        default="lossless",
        help="导出模式：nle_only 极速仅导出 NLE 工程包+SRT，lossless 原画无损快切，vertical 9:16模糊背景竖屏，landscape 横屏重编码（默认：lossless）",
    )
    parser.add_argument(
        "--subtitle-lang",
        choices=sorted(VALID_SUBTITLE_LANG_MODES),
        default="bilingual",
        help="字幕语言模式：bilingual / bilingual_src_top / target_only / source_only / none（兼容 bilingual_rev / target / source / zh_only / en_only / zh / en）",
    )
    parser.add_argument(
        "--show-title-banner",
        action="store_true",
        help="在竖屏模式顶部显式开启标题条（默认关闭，保持画面干净克制）",
    )
    parser.add_argument(
        "--no-title-banner",
        action="store_true",
        help="显式关闭竖屏顶部标题条（兼容旧命令，现已默认关闭）",
    )
    parser.add_argument(
        "--only-reviewed",
        action="store_true",
        help="仅导出已标记为「已人工校对」(review_status=reviewed) 且已勾选的高光片段",
    )
    parser.add_argument(
        "--no-burn-subtitles",
        action="store_true",
        help="在 vertical/landscape 模式下关闭字幕硬烧录（仍会保留同名 .srt 文件）",
    )
    parser.add_argument(
        "--encoder",
        choices=["auto", "nvenc", "x264"],
        default="auto",
        help="重编码时的视频编码器：auto 优先 RTX h264_nvenc（默认：auto）",
    )
    parser.add_argument(
        "--width",
        type=int,
        default=1080,
        help="vertical 模式输出宽度（默认：1080）",
    )
    parser.add_argument(
        "--height",
        type=int,
        default=1920,
        help="vertical 模式输出高度（默认：1920）",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="仅导出前 N 条已选高光（适合先切 2~3 条快速验货）",
    )
    parser.add_argument(
        "--ranks",
        default=None,
        help="只导出指定排名的高光，用英文逗号分隔（例如 --ranks 1,2,5）",
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
        parsed_ranks = _parse_ranks_arg(args.ranks)
        show_banner = bool(args.show_title_banner and not args.no_title_banner)
        result = render_highlight_clips(
            highlights_json_path=args.highlights_json,
            video_path=args.video_path,
            transcript_json_path=args.transcript_json,
            output_dir=args.output_dir,
            mode=args.mode,
            burn_subtitles=not args.no_burn_subtitles,
            subtitle_lang=args.subtitle_lang,
            show_title_banner=show_banner,
            encoder=args.encoder,
            width=args.width,
            height=args.height,
            limit=args.limit,
            ranks=parsed_ranks,
            only_reviewed=bool(args.only_reviewed),
        )
    except (FileNotFoundError, ValueError, RuntimeError) as exc:
        print(f"[错误] {exc}", file=sys.stderr)
        return 1

    _print_summary(result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
