"""一键全流程自动化流水线模块（支持单视频 / 批量文件夹、断点续跑与进度回调）。

本模块将 Step 1 ~ Step 4 串联为面向公会批量生产与 GUI 调用的统一接口：
- `scan_input_videos`: 扫描输入文件夹中所有视频，并自动核对对应产物完成阶段。
- `run_analysis_pipeline`: 一键执行 Step 1（音频高能）-> Step 2（字幕提取）-> Step 3（AI 粗筛 + 双语纠错），
  默认支持中间产物缓存跳过（断点续跑），也可勾选 `force_rerun=True` 全量重跑。
- `run_export_pipeline`: 根据人工审片确认后的 `*_highlights.json` 一键导出切片与剪辑软件标记。

命令行运行示例：
    python src/pipeline.py input/demo_highlight_30s.mp4
    python src/pipeline.py input/ --output-dir output --export-nle-xml
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Callable

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from src.config import resolve_asr_config, resolve_llm_role_config  # noqa: E402
from src.step1_audio import analyze_video_audio, diagnose_stream_energy  # noqa: E402
from src.step2_asr import transcribe_with_audio_peaks  # noqa: E402
from src.step3_llm import (  # noqa: E402
    atomic_write_json,
    enrich_highlights_with_bilingual_subtitles,
    select_highlights_with_llm,
)
from src.step4_cut import render_highlight_clips  # noqa: E402

SUPPORTED_VIDEO_EXTS = {".mp4", ".mkv", ".mov", ".flv", ".ts", ".webm"}

ProgressCallback = Callable[[str, int, str], None]


def _emit(on_progress: ProgressCallback | None, stage: str, percent: int, message: str) -> None:
    """安全触发进度回调。"""
    if on_progress is not None:
        on_progress(stage, percent, message)


def _safe_read_json(path: Path) -> dict[str, Any]:
    """安全读取 JSON 文件，损坏或不存在时返回空字典。"""
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _peaks_cache_matches(
    peaks_data: dict[str, Any],
    audio_stream: int,
    vocal_bandpass: bool,
    top_percent: float,
    video_path: Path | None = None,
) -> bool:
    """检查已有 Step 1 缓存是否与当前源视频指纹、音轨、带通滤波与前百分比设置完全一致。"""
    if not peaks_data:
        return False
    # 早期未携带 vocal_bandpass 或 dead_air_segments 字段的老缓存自动视作过期，确保平滑升级
    if "vocal_bandpass" not in peaks_data or "dead_air_segments" not in peaks_data:
        return False
    if video_path is not None and video_path.is_file():
        cached_size = peaks_data.get("video_size")
        cached_mtime = peaks_data.get("video_mtime_ns")
        if cached_size is not None and cached_mtime is not None:
            st = video_path.stat()
            if int(cached_size) != int(st.st_size) or int(cached_mtime) != int(st.st_mtime_ns):
                return False
    try:
        cached_stream = int(peaks_data.get("audio_stream", 0))
        cached_bp = bool(peaks_data.get("vocal_bandpass"))
        cached_pct = float(peaks_data.get("top_percent", 15.0))
    except (TypeError, ValueError):
        return False

    return (
        cached_stream == int(audio_stream)
        and cached_bp == bool(vocal_bandpass)
        and abs(cached_pct - float(top_percent)) < 1e-6
    )


def _transcript_cache_matches(
    tr_data: dict[str, Any],
    asr_provider: str,
    asr_mode: str,
    top_k_peaks: int,
    asr_model: str,
    language: str | None,
    normalize_audio: bool,
    vad_threshold: float,
) -> bool:
    """检查已有 Step 2 字幕缓存是否与当前引擎、模式、模型、高峰数及归一化参数一致。"""
    if not tr_data:
        return False
    # 早期未携带 asr_provider 的旧缓存若用户切到了新引擎（如 deepseek），自动失效重建
    cached_provider = str(tr_data.get("asr_provider") or "local").lower()
    if cached_provider != str(asr_provider).lower():
        return False
    if str(tr_data.get("mode") or "") != str(asr_mode):
        return False
    cached_model = str(tr_data.get("asr_model") or tr_data.get("model_size") or "")
    if cached_model != str(asr_model):
        return False
    if asr_mode == "peaks":
        if "top_k_peaks" not in tr_data:
            return False
        if int(tr_data.get("top_k_peaks", 0)) != int(top_k_peaks):
            return False
    if bool(tr_data.get("normalize_audio", False)) != bool(normalize_audio):
        return False
    if abs(float(tr_data.get("vad_threshold", 0.50)) - float(vad_threshold)) > 1e-4:
        return False
    if language and str(tr_data.get("language") or "").lower() != language.lower():
        return False
    return True


def _highlights_cache_matches(
    hl_data: dict[str, Any],
    screen_model: str,
    polish_model: str,
    top_k_highlights: int | None,
    min_clip_sec: int | None,
    max_clip_sec: int | None,
    bilingual_subtitles: bool,
    target_language: str | None = None,
    source_language: str | None = None,
) -> bool:
    """检查已有 Step 3 高光缓存是否与当前粗筛/精修模型、源/目标翻译语种及手动筛选参数一致。"""
    if not hl_data:
        return False
    cached_screen = str(hl_data.get("screen_model") or hl_data.get("model") or "").strip()
    # 纯手动创建（model="manual"）或缺失模型信息的旧缓存不得当作 AI 粗筛缓存复用
    if not cached_screen or cached_screen.lower() == "manual":
        return False
    if cached_screen != "mock/custom" and cached_screen != str(screen_model):
        return False

    cached_src = str(hl_data.get("source_language") or "").strip().lower()
    req_src = (source_language or "").strip().lower()
    if req_src and req_src != "auto":
        if cached_src and cached_src != req_src:
            return False

    effective_src = req_src if (req_src and req_src != "auto") else (cached_src or "en")
    default_tgt_for_src = "en" if effective_src.startswith("zh") else "zh"
    default_cached_tgt = "en" if cached_src.startswith("zh") else "zh"
    cached_tgt = str(hl_data.get("target_language") or default_cached_tgt).strip().lower()

    req_tgt = (target_language or "").strip().lower()
    expected_tgt = req_tgt if (req_tgt and req_tgt != "auto") else default_tgt_for_src

    if req_tgt and req_tgt != "auto":
        if cached_tgt != req_tgt:
            return False

    if bilingual_subtitles:
        if cached_tgt != expected_tgt:
            return False
        cached_polish = str(hl_data.get("polish_model") or "").strip()
        if hl_data.get("bilingual_subtitles"):
            eff_cached_polish = cached_polish or cached_screen
            if eff_cached_polish != "mock/custom" and eff_cached_polish != str(polish_model):
                return False
        elif cached_polish and cached_polish != "mock/custom" and cached_polish != str(polish_model):
            return False

    plan = hl_data.get("selection_plan") or {}
    if top_k_highlights is not None and int(plan.get("target_count", -1)) != int(top_k_highlights):
        return False
    if min_clip_sec is not None and int(plan.get("min_clip_sec", -1)) != int(min_clip_sec):
        return False
    if max_clip_sec is not None and int(plan.get("max_clip_sec", -1)) != int(max_clip_sec):
        return False
    return True


def get_or_compute_diagnostics(peaks_data: dict[str, Any]) -> dict[str, Any] | None:
    """获取或兼容计算 Step 1 直播状态诊断报告（兼容早期未带 diagnostics 字段的 JSON）。"""
    if not peaks_data:
        return None
    existing = peaks_data.get("diagnostics")
    if isinstance(existing, dict) and "grade" in existing and "recommendation" in existing:
        return existing

    return diagnose_stream_energy(
        peak_segments=peaks_data.get("peak_segments", []),
        total_duration_sec=float(peaks_data.get("total_duration_sec", 0.0)),
        rms_threshold=float(peaks_data.get("rms_threshold", 0.0)),
    )


def scan_input_videos(
    input_dir: str | Path,
    output_dir: str | Path,
) -> list[dict[str, Any]]:
    """递归扫描输入目录中的全部视频，并检测每个视频目前已经完成的流水线阶段。"""
    in_root = Path(input_dir)
    out_root = Path(output_dir)
    if not in_root.is_dir():
        return []

    video_files = [
        p
        for p in sorted(in_root.rglob("*"))
        if p.is_file()
        and p.suffix.lower() in SUPPORTED_VIDEO_EXTS
        and not p.name.startswith("._")
    ]

    results: list[dict[str, Any]] = []
    for vpath in video_files:
        stem = vpath.stem
        try:
            rel_path = str(vpath.relative_to(in_root)).replace("\\", "/")
        except ValueError:
            rel_path = vpath.name

        peaks_json = out_root / f"{stem}_audio_peaks.json"
        transcript_json = out_root / f"{stem}_transcript.json"
        highlights_json = out_root / f"{stem}_highlights.json"
        clips_dir = out_root / f"{stem}_clips"
        clips_manifest = clips_dir / f"{stem}_clips_manifest.json"

        peaks_data = _safe_read_json(peaks_json)
        transcript_data = _safe_read_json(transcript_json)
        highlights_data = _safe_read_json(highlights_json)
        manifest_data = _safe_read_json(clips_manifest)

        has_peaks = bool(peaks_data)
        has_transcript = bool(transcript_data)
        has_highlights = bool(highlights_data)

        # 精确区分「已实际导出 MP4 切片」与「仅导出 NLE 工程包（nle_only）」，避免误报“已出片”
        if manifest_data:
            if "exported_clip_count" in manifest_data:
                exported_clip_count = int(manifest_data.get("exported_clip_count") or 0)
            elif str(manifest_data.get("mode") or "").strip().lower() == "nle_only":
                raw_clips = manifest_data.get("clips") if isinstance(manifest_data.get("clips"), list) else []
                exported_clip_count = sum(
                    1 for c in raw_clips if isinstance(c, dict) and str(c.get("mp4_path") or "").strip()
                )
            else:
                exported_clip_count = int(manifest_data.get("exported_count") or 0)
        else:
            exported_clip_count = 0

        has_clips = bool(manifest_data) and exported_clip_count > 0
        has_nle_export = bool(manifest_data) and bool(
            manifest_data.get("fcpxml_path") or manifest_data.get("premiere_xml_path")
        )

        if has_clips:
            stage = "exported"
            stage_label = "已出片"
        elif has_highlights:
            stage = "analyzed"
            stage_label = "待审片"
        elif has_transcript:
            stage = "transcribed"
            stage_label = "已出字幕"
        elif has_peaks:
            stage = "audio_only"
            stage_label = "已测音量"
        else:
            stage = "idle"
            stage_label = "未处理"

        hl_list = highlights_data.get("highlights", [])
        selected_count = sum(1 for item in hl_list if item.get("selected", True) is True)
        reviewed_count = sum(
            1 for item in hl_list if str(item.get("review_status") or "draft").strip().lower() == "reviewed"
        )
        draft_count = len(hl_list) - reviewed_count

        duration_str = (
            highlights_data.get("total_duration_str")
            or transcript_data.get("total_duration_str")
            or peaks_data.get("total_duration_str")
            or "-"
        )
        duration_sec = float(
            highlights_data.get("total_duration_sec")
            or transcript_data.get("total_duration_sec")
            or peaks_data.get("total_duration_sec")
            or 0.0
        )
        diagnostics = get_or_compute_diagnostics(peaks_data)

        results.append(
            {
                "stem": stem,
                "filename": vpath.name,
                "rel_path": rel_path,
                "video_path": str(vpath.resolve()),
                "size_mb": round(vpath.stat().st_size / (1024.0 * 1024.0), 1),
                "stage": stage,
                "stage_label": stage_label,
                "has_peaks": has_peaks,
                "has_transcript": has_transcript,
                "has_highlights": has_highlights,
                "has_clips": has_clips,
                "has_nle_export": has_nle_export,
                "total_duration_sec": duration_sec,
                "total_duration_str": duration_str,
                "highlight_count": len(hl_list),
                "selected_count": selected_count,
                "reviewed_count": reviewed_count,
                "draft_count": draft_count,
                "dead_air_total_sec": float(peaks_data.get("dead_air_total_sec", 0.0) or 0.0),
                "bilingual_subtitles": bool(highlights_data.get("bilingual_subtitles", False)),
                "source_language": highlights_data.get("source_language") or transcript_data.get("language") or "",
                "target_language": highlights_data.get("target_language") or "",
                "subtitle_pair": highlights_data.get("subtitle_pair") or "",
                "exported_count": int(manifest_data.get("exported_count", 0)),
                "exported_clip_count": exported_clip_count,
                "audio_stream": int(peaks_data.get("audio_stream", 0)),
                "vocal_bandpass": peaks_data.get("vocal_bandpass"),
                "grade": diagnostics.get("grade") if diagnostics else None,
                "diagnostics": diagnostics,
                "highlights_json_path": str(highlights_json.resolve()) if has_highlights else "",
                "clips_dir": str(clips_dir.resolve()),
            }
        )

    return results


def run_quick_diagnose(
    video_path: str | Path,
    output_dir: str | Path = "output",
    top_percent: float = 15.0,
    audio_stream: int = 0,
    vocal_bandpass: bool = True,
    force_rerun: bool = False,
    on_progress: ProgressCallback | None = None,
) -> dict[str, Any]:
    """零 API 消耗快速体检：仅跑 Step 1 音频分析，立即产出本场直播状态评级与推荐峰值。"""
    src_video = Path(video_path)
    if not src_video.is_file():
        raise FileNotFoundError(f"找不到输入视频文件：{src_video}")

    out_dir = Path(output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    stem = src_video.stem
    peaks_json = out_dir / f"{stem}_audio_peaks.json"
    wav_file = out_dir / f"{stem}_16k.wav"

    cached_peaks = _safe_read_json(peaks_json) if (peaks_json.is_file() and wav_file.is_file()) else {}
    cache_ok = (not force_rerun) and _peaks_cache_matches(
        cached_peaks,
        audio_stream=audio_stream,
        vocal_bandpass=vocal_bandpass,
        top_percent=top_percent,
        video_path=src_video,
    )

    if cache_ok:
        _emit(on_progress, "step1", 80, f"[{stem}] 读取已有音频数据计算直播体检报告...")
        diag = get_or_compute_diagnostics(cached_peaks)
        if diag and "diagnostics" not in cached_peaks:
            cached_peaks["diagnostics"] = diag
            atomic_write_json(peaks_json, cached_peaks, indent=2)
        res = cached_peaks
    else:
        bp_tag = "300~3400Hz人声带通" if vocal_bandpass else "全频段"
        _emit(
            on_progress,
            "step1",
            25,
            f"[{stem}] 正在抽取音轨 #{audio_stream} 并按{bp_tag}测算爆发分布（零 API 消耗）...",
        )
        res = analyze_video_audio(
            video_path=src_video,
            output_dir=out_dir,
            top_percent=top_percent,
            audio_stream=audio_stream,
            vocal_bandpass=vocal_bandpass,
        )

    diag = res.get("diagnostics") or get_or_compute_diagnostics(res)
    grade_lbl = diag.get("grade_label", "-") if diag else "-"
    _emit(on_progress, "done", 100, f"[{stem}] 体检完成：{grade_lbl}")
    return {
        "stem": stem,
        "peaks_json_path": str(peaks_json.resolve()),
        "diagnostics": diag,
    }


def run_analysis_pipeline(
    video_path: str | Path,
    output_dir: str | Path = "output",
    top_percent: float = 15.0,
    asr_mode: str = "peaks",
    top_k_peaks: int = 20,
    model_size: str | None = None,
    language: str | None = None,
    target_language: str | None = None,
    top_k_highlights: int | None = None,
    min_clip_sec: int | None = None,
    max_clip_sec: int | None = None,
    bilingual_subtitles: bool = True,
    llm_model: str | None = None,
    base_url: str | None = None,
    api_key: str | None = None,
    polish_model: str | None = None,
    polish_base_url: str | None = None,
    polish_api_key: str | None = None,
    asr_provider: str | None = None,
    asr_model: str | None = None,
    asr_base_url: str | None = None,
    asr_api_key: str | None = None,
    hotwords: str | None = None,
    normalize_audio: bool = True,
    vad_threshold: float = 0.30,
    audio_stream: int = 0,
    vocal_bandpass: bool = True,
    force_rerun: bool = False,
    on_progress: ProgressCallback | None = None,
) -> dict[str, Any]:
    """一键运行 Step 1 -> Step 2 -> Step 3 粗筛流程（全阶段参数感知缓存，改参自动级联刷新）。"""
    src_video = Path(video_path)
    if not src_video.is_file():
        raise FileNotFoundError(f"找不到输入视频文件：{src_video}")

    out_dir = Path(output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    stem = src_video.stem

    peaks_json = out_dir / f"{stem}_audio_peaks.json"
    wav_file = out_dir / f"{stem}_16k.wav"
    transcript_json = out_dir / f"{stem}_transcript.json"
    highlights_json = out_dir / f"{stem}_highlights.json"

    # 解析有效 ASR 与双角色 LLM 配置用于缓存签名核对
    eff_asr_model_arg = asr_model if asr_model is not None else model_size
    asr_cfg = resolve_asr_config(
        provider=asr_provider,
        api_key=asr_api_key,
        base_url=asr_base_url,
        model=eff_asr_model_arg,
        hotwords=hotwords,
        require_api_key=False,
    )
    screen_cfg = resolve_llm_role_config(
        role="screen",
        api_key=api_key,
        base_url=base_url,
        model=llm_model,
        require_api_key=False,
    )
    polish_cfg = resolve_llm_role_config(
        role="polish",
        api_key=polish_api_key if polish_api_key is not None else api_key,
        base_url=polish_base_url if polish_base_url is not None else base_url,
        model=polish_model if polish_model is not None else llm_model,
        require_api_key=False,
    )

    # Step 1：音频高能检测（多音轨选择 + 人声带通参数感知缓存）
    cached_peaks = _safe_read_json(peaks_json) if (peaks_json.is_file() and wav_file.is_file()) else {}
    step1_cache_hit = (not force_rerun) and _peaks_cache_matches(
        cached_peaks,
        audio_stream=audio_stream,
        vocal_bandpass=vocal_bandpass,
        top_percent=top_percent,
        video_path=src_video,
    )
    step1_regenerated = False

    if step1_cache_hit:
        _emit(on_progress, "step1", 28, f"[{stem}] Step 1 复用缓存：{peaks_json.name}（轨#{audio_stream}）")
        step1_res = cached_peaks
    else:
        bp_tag = "300~3400Hz带通" if vocal_bandpass else "全频段"
        _emit(on_progress, "step1", 8, f"[{stem}] Step 1 提取音轨 #{audio_stream} 并按{bp_tag}分析高音量峰值...")
        step1_res = analyze_video_audio(
            video_path=src_video,
            output_dir=out_dir,
            top_percent=top_percent,
            audio_stream=audio_stream,
            vocal_bandpass=vocal_bandpass,
        )
        step1_regenerated = True
        _emit(
            on_progress,
            "step1",
            30,
            f"[{stem}] Step 1 完成：共发现 {step1_res.get('segment_count', 0)} 个高音量区间",
        )

    # Step 2：独立 ASR 语音转字幕 + 高音量对齐（参数指纹比对，避免改引擎/改高峰数仍命中旧缓存）
    cached_tr = _safe_read_json(transcript_json) if transcript_json.is_file() else {}
    step2_cache_hit = (
        not force_rerun
        and not step1_regenerated
        and _transcript_cache_matches(
            cached_tr,
            asr_provider=asr_cfg["provider"],
            asr_mode=asr_mode,
            top_k_peaks=top_k_peaks,
            asr_model=asr_cfg["model"],
            language=language,
            normalize_audio=normalize_audio,
            vad_threshold=vad_threshold,
        )
    )
    step2_regenerated = False

    if step2_cache_hit:
        _emit(
            on_progress,
            "step2",
            62,
            f"[{stem}] Step 2 复用缓存：{transcript_json.name}（{asr_cfg['provider']}/{asr_cfg['model']}）",
        )
        step2_res = cached_tr
    else:
        _emit(
            on_progress,
            "step2",
            36,
            f"[{stem}] Step 2 启动语音识别（引擎={asr_cfg['provider']}, 模型={asr_cfg['model']}, 模式={asr_mode}）...",
        )
        step2_res = transcribe_with_audio_peaks(
            peaks_json_path=peaks_json,
            wav_path=wav_file,
            output_dir=out_dir,
            mode=asr_mode,
            top_k_peaks=top_k_peaks,
            language=language,
            asr_provider=asr_cfg["provider"],
            asr_api_key=asr_api_key,
            asr_base_url=asr_base_url,
            asr_model=eff_asr_model_arg,
            hotwords=hotwords,
            normalize_audio=normalize_audio,
            vad_threshold=vad_threshold,
        )
        step2_regenerated = True
        billed_s = float(step2_res.get("billed_audio_sec", 0.0))
        extra_info = f"，云端计费 {billed_s:.1f}s" if billed_s > 0 else ""
        _emit(
            on_progress,
            "step2",
            65,
            f"[{stem}] Step 2 完成：识别 {step2_res.get('segment_count', 0)} 句字幕（{step2_res.get('asr_provider')}/{step2_res.get('asr_model')}{extra_info}）",
        )

    # Step 3：AI 自适应粗筛（screen）+ 双语精修（polish）
    existing_hl = _safe_read_json(highlights_json)
    step3_cache_hit = (
        not force_rerun
        and not step2_regenerated
        and _highlights_cache_matches(
            existing_hl,
            screen_model=screen_cfg["model"],
            polish_model=polish_cfg["model"],
            top_k_highlights=top_k_highlights,
            min_clip_sec=min_clip_sec,
            max_clip_sec=max_clip_sec,
            bilingual_subtitles=bilingual_subtitles,
            target_language=target_language,
            source_language=language,
        )
    )
    if step3_cache_hit:
        if bilingual_subtitles and not existing_hl.get("bilingual_subtitles"):
            # 仅对尚未人工审校（review_status != "reviewed"）的条目补齐双语，绝不覆毁人工已审校内容
            unreviewed_ranks = [
                int(h.get("rank") or idx)
                for idx, h in enumerate(existing_hl.get("highlights") or [], start=1)
                if isinstance(h, dict)
                and str(h.get("review_status") or "draft").strip().lower() != "reviewed"
            ]
            if unreviewed_ranks:
                _emit(
                    on_progress,
                    "step3",
                    78,
                    f"[{stem}] Step 3 检测到已有粗筛结果，正在为 {len(unreviewed_ranks)} 条待审校片段补充双语字幕...",
                )
                step3_res = enrich_highlights_with_bilingual_subtitles(
                    input_json_path=highlights_json,
                    output_dir=out_dir,
                    model=polish_model if polish_model is not None else llm_model,
                    base_url=polish_base_url if polish_base_url is not None else base_url,
                    api_key=polish_api_key if polish_api_key is not None else api_key,
                    hotwords=hotwords,
                    source_language=language,
                    target_language=target_language,
                    ranks=unreviewed_ranks,
                )
            else:
                _emit(on_progress, "step3", 96, f"[{stem}] Step 3 全部高光均已人工审校，跳过覆盖直接复用缓存")
                step3_res = existing_hl
        else:
            _emit(on_progress, "step3", 96, f"[{stem}] Step 3 复用缓存：{highlights_json.name}")
            step3_res = existing_hl
    else:
        _emit(
            on_progress,
            "step3",
            70,
            f"[{stem}] Step 3 启动大模型粗筛（{screen_cfg['model']}）与多语种双语精修（{polish_cfg['model']}）...",
        )
        step3_res = select_highlights_with_llm(
            transcript_json_path=transcript_json,
            output_dir=out_dir,
            top_k=top_k_highlights,
            min_clip_sec=min_clip_sec,
            max_clip_sec=max_clip_sec,
            model=llm_model,
            base_url=base_url,
            api_key=api_key,
            polish_model=polish_model,
            polish_base_url=polish_base_url,
            polish_api_key=polish_api_key,
            hotwords=hotwords,
            bilingual_subtitles=bilingual_subtitles,
            target_language=target_language,
            source_language=language,
        )
        _emit(
            on_progress,
            "step3",
            98,
            f"[{stem}] Step 3 完成：精选出 {step3_res.get('highlight_count', 0)} 条高光待审片",
        )

    _emit(on_progress, "done", 100, f"[{stem}] 分析粗筛全部完成！请审核勾选高光后导出。")
    return {
        "stem": stem,
        "video_path": str(src_video.resolve()),
        "output_dir": str(out_dir.resolve()),
        "peaks_json_path": str(peaks_json.resolve()),
        "transcript_json_path": str(transcript_json.resolve()),
        "highlights_json_path": str(highlights_json.resolve()),
        "step1": step1_res,
        "step2": step2_res,
        "step3": step3_res,
    }


def run_export_pipeline(
    highlights_json_path: str | Path,
    video_path: str | Path | None = None,
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
    only_reviewed: bool = False,
    on_progress: ProgressCallback | None = None,
) -> dict[str, Any]:
    """一键运行 Step 4 导出 NLE 工程包（FCPXML / Premiere XML / EDL / CSV）与可选切片，并上报进度。"""
    hl_path = Path(highlights_json_path)
    _emit(on_progress, "step4", 10, f"开始导出交付包（模式：{mode} | 字幕：{subtitle_lang}）...")
    res = render_highlight_clips(
        highlights_json_path=hl_path,
        video_path=video_path,
        output_dir=output_dir,
        mode=mode,
        burn_subtitles=burn_subtitles,
        subtitle_lang=subtitle_lang,
        show_title_banner=show_title_banner,
        encoder=encoder,
        width=width,
        height=height,
        limit=limit,
        ranks=ranks,
        only_reviewed=only_reviewed,
        on_progress=on_progress,
    )
    nle_names = [
        Path(str(res[k])).name
        for k in ("fcpxml_path", "premiere_xml_path", "markers_edl_path", "markers_csv_path")
        if res.get(k)
    ]
    nle_tag = f" | NLE: {', '.join(nle_names)}" if nle_names else ""
    if mode == "nle_only":
        done_msg = f"NLE 工程包导出完成（零转码，{res.get('elapsed_sec', 0)}s）→ {res.get('output_dir')}{nle_tag}"
    else:
        done_msg = (
            f"导出完成！共输出 {res.get('exported_count', 0)} 条切片与 NLE 工程包到 {res.get('output_dir')}{nle_tag}"
        )
    _emit(on_progress, "done", 100, done_msg)
    return res


def build_arg_parser() -> argparse.ArgumentParser:
    """构建流水线 CLI 参数。"""
    parser = argparse.ArgumentParser(description="一键运行游戏录屏高光粗筛与免转码 NLE 工程包导出流水线。")
    parser.add_argument("target", help="单个视频路径，或包含多个视频的输入文件夹路径")
    parser.add_argument("--output-dir", default="output", help="统一输出目录（默认：output）")
    parser.add_argument("--audio-stream", type=int, default=0, help="提取视频的第几条音轨（0 起算，适合 OBS 多音轨）")
    parser.add_argument("--no-vocal-bandpass", action="store_true", help="关闭 300~3400Hz 人声带通峰值检测")
    parser.add_argument(
        "--asr-provider",
        choices=["deepseek", "local", "openai_compatible"],
        default=None,
        help="Step 2 语音识别引擎（默认走 .env 配置或 deepseek 云端 ASR）",
    )
    parser.add_argument("--asr-model", default=None, help="指定语音识别模型名（本地默认 large-v3-turbo）")
    parser.add_argument("--hotwords", default=None, help="主播/游戏专词热词表（逗号分隔）")
    parser.add_argument("--asr-mode", choices=["peaks", "full"], default="peaks", help="Step 2 模式")
    parser.add_argument("--top-k-peaks", type=int, default=20, help="Step 2 选取高峰数量")
    parser.add_argument("--model-size", default=None, help="兼容参数：本地 Whisper 模型规格")
    parser.add_argument("--screen-model", default=None, help="覆盖 Step 3 粗筛大模型（默认 deepseek-flash）")
    parser.add_argument("--polish-model", default=None, help="覆盖 Step 3 双语精修大模型")
    parser.add_argument("--force", action="store_true", help="忽略已有缓存，强制从头重跑")
    parser.add_argument(
        "--export-nle-xml",
        action="store_true",
        help="粗筛后自动输出免转码 NLE 工程包（FCPXML / Premiere XML / EDL / CSV），供导入剪辑软件人工精剪（不自动出成片）",
    )
    parser.add_argument(
        "--only-reviewed",
        action="store_true",
        help="仅导出已人工确认（review_status=reviewed）的高光片段",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    """命令行批量流水线入口。"""
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")

    args = build_arg_parser().parse_args(argv)
    target = Path(args.target)
    out_dir = Path(args.output_dir)

    if target.is_file():
        videos = [target]
    elif target.is_dir():
        scanned = scan_input_videos(target, out_dir)
        videos = [Path(item["video_path"]) for item in scanned]
        if not videos:
            print(f"[提示] 目录中未扫描到支持的视频文件：{target}")
            return 0
    else:
        print(f"[错误] 找不到目标文件或目录：{target}", file=sys.stderr)
        return 1

    def _log(stage: str, pct: int, msg: str) -> None:
        print(f"[{pct:3d}% | {stage}] {msg}")

    for idx, vpath in enumerate(videos, start=1):
        print(f"\n>>> 处理第 {idx}/{len(videos)} 个视频：{vpath.name}")
        res = run_analysis_pipeline(
            video_path=vpath,
            output_dir=out_dir,
            asr_mode=args.asr_mode,
            top_k_peaks=args.top_k_peaks,
            model_size=args.model_size,
            asr_provider=args.asr_provider,
            asr_model=args.asr_model,
            hotwords=args.hotwords,
            llm_model=args.screen_model,
            polish_model=args.polish_model,
            audio_stream=args.audio_stream,
            vocal_bandpass=not args.no_vocal_bandpass,
            force_rerun=args.force,
            on_progress=_log,
        )
        if args.export_nle_xml:
            run_export_pipeline(
                highlights_json_path=res["highlights_json_path"],
                mode="nle_only",
                only_reviewed=args.only_reviewed,
                on_progress=_log,
            )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
