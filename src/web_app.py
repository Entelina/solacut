"""公会级游戏录屏高光审片工作台 Web 后端服务（FastAPI）。

提供：
- 静态资源托管（web/index.html, styles.css, app.js）
- 工作台全局配置与 .env API 密钥管理（脱敏读写）
- Windows 原生系统文件夹选择框（tkinter）与资源管理器定位
- 输入文件夹视频扫描 & 多阶段产物状态探测
- 支持 HTTP 206 Range 的本地大视频极速流式播放
- 人工审片数据（勾选、起止时间、中英双语字幕、标题梗概）实时保存并同步更新 review.md
- 后台排队任务执行引擎（防止多个 Whisper/NVENC 任务同时争抢 RTX 2060 显存）

运行方式：
    python src/web_app.py
"""

from __future__ import annotations

import argparse
import html
import json
import math
import mimetypes
import os
import re
import secrets
import shutil
import subprocess
import sys
import threading
import time
import uuid
import webbrowser
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

_IS_FROZEN = bool(getattr(sys, "frozen", False))
_PROJECT_ROOT = (
    Path(sys.executable).resolve().parent
    if _IS_FROZEN
    else Path(__file__).resolve().parent.parent
)
_RESOURCE_ROOT = Path(getattr(sys, "_MEIPASS", _PROJECT_ROOT)) if _IS_FROZEN else _PROJECT_ROOT
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from src.chat_radar import (  # noqa: E402
    analyze_chat_spikes,
    fuse_audio_and_chat_peaks,
    parse_chat_file,
    save_chat_messages_db,
    search_chat_keywords,
    search_chat_messages_db,
)
from src.config import (  # noqa: E402
    mask_secret as _mask_secret,
    read_workbench_config as _read_workbench_config_from_paths,
    redact_secrets_in_text as _redact_secrets,
    write_env_settings as _write_env_settings_to_path,
)
from src.corpus_rag import (  # noqa: E402
    DEFAULT_CORPUS_ROOT,
    get_corpus_stats,
    load_user_glossary,
    save_user_glossary,
)
from src.nle_export import extract_video_chapters  # noqa: E402
from src.pipeline import (  # noqa: E402
    SUPPORTED_VIDEO_EXTS,
    get_or_compute_diagnostics,
    run_analysis_pipeline,
    run_export_pipeline,
    run_quick_diagnose,
    scan_input_videos,
)
from src.step1_audio import (  # noqa: E402
    check_ffmpeg_available,
    extract_video_thumbnail,
    format_timestamp,
    probe_audio_streams,
    probe_media_info,
    sample_wav_waveform,
)
from src.step3_llm import (  # noqa: E402
    VALID_REVIEW_STATUSES,
    atomic_write_json,
    compute_review_flags,
    enrich_highlights_with_bilingual_subtitles,
    export_review_markdown,
    get_highlights_file_lock,
    normalize_review_status,
)
from src.step4_cut import VALID_EXPORT_MODES, normalize_subtitle_lang_mode  # noqa: E402

_SUPPORTED_SUBTITLE_LANGS: frozenset[str] = frozenset({"en", "zh", "ja", "ko", "ru", "es"})
_SUPPORTED_CHAT_EXTS: frozenset[str] = frozenset({".json", ".jsonl", ".xml"})
_MAX_TRANSCRIPT_SEGMENTS = 5000


def _validate_optional_lang_code(raw: str | None, field_name: str = "language") -> str | None:
    """校验可选语种参数，放行 6 语种（en/zh/ja/ko/ru/es）及 auto/空值。"""
    cleaned = (raw or "").strip().lower()
    if not cleaned or cleaned == "auto":
        return None
    if cleaned not in _SUPPORTED_SUBTITLE_LANGS:
        raise HTTPException(
            status_code=400,
            detail=f"不支持的语种参数 {field_name}={raw}，仅支持 en/zh/ja/ko/ru/es 或 auto",
        )
    return cleaned

def _resolve_resource_dir(rel: str) -> Path:
    p1 = _PROJECT_ROOT / rel
    if p1.exists():
        return p1
    p2 = _RESOURCE_ROOT / rel
    if p2.exists():
        return p2
    return p1


_WEB_DIR = _resolve_resource_dir("web")
_DEMO_ASSETS_DIR = _resolve_resource_dir("assets/demo")
_CONFIG_PATH = _PROJECT_ROOT / ".workbench_config.json"
_MEDIA_BINS_PATH = _PROJECT_ROOT / ".media_bins.json"
_ENV_PATH = _PROJECT_ROOT / ".env"
_CORPUS_ROOT = DEFAULT_CORPUS_ROOT


def _read_workbench_config() -> dict[str, Any]:
    """通过统一配置中心读取工作台目录配置与三层脱敏接口状态。"""
    return _read_workbench_config_from_paths(config_path=_CONFIG_PATH, env_path=_ENV_PATH)


def _write_env_values(updates: dict[str, str | None]) -> None:
    """通过统一配置中心安全更新标准 KEY=VALUE 格式 .env 文件。"""
    _write_env_settings_to_path(_ENV_PATH, updates)


class TaskManager:
    """单工人顺序排队的后台任务管理器（保证同一时间只跑一个 GPU/FFmpeg 重任务）。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._tasks: dict[str, dict[str, Any]] = {}
        self._order: list[str] = []
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="clipper-worker")

    def submit(self, task_type: str, title: str, worker_fn: Any) -> dict[str, Any]:
        task_id = uuid.uuid4().hex[:10]
        record: dict[str, Any] = {
            "task_id": task_id,
            "task_type": task_type,
            "title": title,
            "status": "queued",
            "stage": "queued",
            "percent": 0,
            "message": "任务已加入后台队列，等待执行...",
            "logs": [f"[{time.strftime('%H:%M:%S')}] 任务已加入队列：{title}"],
            "result": None,
            "error": None,
            "created_at": time.time(),
            "updated_at": time.time(),
        }
        with self._lock:
            self._tasks[task_id] = record
            self._order.insert(0, task_id)
            if len(self._order) > 30:
                stale = self._order.pop()
                self._tasks.pop(stale, None)

        def _runner() -> None:
            with self._lock:
                cur_rec = self._tasks.get(task_id)
                if cur_rec is None or cur_rec.get("status") == "cancelled":
                    return
            self.update(task_id, status="running", stage="running", percent=2, message="开始执行...")
            try:
                res = worker_fn(lambda stage, pct, msg: self.update_progress(task_id, stage, pct, msg))
                self.update(
                    task_id,
                    status="completed",
                    stage="done",
                    percent=100,
                    message="任务全部完成",
                    result=res,
                )
            except Exception as exc:
                err_msg = _redact_secrets(str(exc) or exc.__class__.__name__)
                self.append_log(task_id, f"[错误] {err_msg}")
                self.update(
                    task_id,
                    status="failed",
                    stage="error",
                    message=f"执行失败：{err_msg}",
                    error=err_msg,
                )

        self._executor.submit(_runner)
        return self.get(task_id)  # type: ignore[return-value]

    def update_progress(self, task_id: str, stage: str, percent: int, message: str) -> None:
        self.append_log(task_id, message)
        self.update(task_id, stage=stage, percent=max(0, min(100, int(percent))), message=message)

    def append_log(self, task_id: str, message: str) -> None:
        line = f"[{time.strftime('%H:%M:%S')}] {message}"
        with self._lock:
            rec = self._tasks.get(task_id)
            if rec is None:
                return
            rec["logs"].append(line)
            if len(rec["logs"]) > 120:
                rec["logs"] = rec["logs"][-120:]
            rec["updated_at"] = time.time()

    def update(self, task_id: str, **kwargs: Any) -> None:
        with self._lock:
            rec = self._tasks.get(task_id)
            if rec is None:
                return
            rec.update(kwargs)
            rec["updated_at"] = time.time()

    def get(self, task_id: str) -> dict[str, Any] | None:
        with self._lock:
            rec = self._tasks.get(task_id)
            return json.loads(json.dumps(rec, ensure_ascii=False)) if rec else None

    def list_recent(self) -> list[dict[str, Any]]:
        with self._lock:
            return [
                json.loads(json.dumps(self._tasks[tid], ensure_ascii=False))
                for tid in self._order
                if tid in self._tasks
            ]

    def cancel_queued(self, task_id: str) -> tuple[str, dict[str, Any] | None]:
        """取消处于 queued 排队状态的任务；返回 (code, record)。"""
        with self._lock:
            rec = self._tasks.get(task_id)
            if rec is None:
                return "not_found", None
            if rec.get("status") != "queued":
                return "not_queued", json.loads(json.dumps(rec, ensure_ascii=False))
            rec["status"] = "cancelled"
            rec["stage"] = "cancelled"
            rec["message"] = "已由用户取消排队"
            rec["logs"].append(f"[{time.strftime('%H:%M:%S')}] 任务已在排队阶段取消")
            rec["updated_at"] = time.time()
            return "cancelled", json.loads(json.dumps(rec, ensure_ascii=False))

    def clear_finished(self) -> int:
        """清理所有已结束（completed / failed / cancelled）的历史任务记录。"""
        finished_states = {"completed", "failed", "cancelled"}
        removed = 0
        with self._lock:
            keep_order: list[str] = []
            for tid in self._order:
                rec = self._tasks.get(tid)
                if rec is not None and rec.get("status") in finished_states:
                    self._tasks.pop(tid, None)
                    removed += 1
                elif rec is not None:
                    keep_order.append(tid)
            self._order = keep_order
        return removed


_TASK_MANAGER = TaskManager()
_MEDIA_INFO_CACHE: dict[tuple[str, int, int], dict[str, Any]] = {}
_MEDIA_INFO_LOCK = threading.Lock()
_FILE_DIALOG_LOCK = threading.Lock()

# Windows 注册表对 .mkv/.ts/.flv/.mov 等扩展名常缺少或错配 MIME，显式声明以提升 WebView2/浏览器播放兼容性
_VIDEO_MIME_TYPES: dict[str, str] = {
    ".mp4": "video/mp4",
    ".m4v": "video/mp4",
    ".webm": "video/webm",
    ".mov": "video/quicktime",
    ".mkv": "video/x-matroska",
    ".flv": "video/x-flv",
    ".ts": "video/mp2t",
}


class ConfigUpdateReq(BaseModel):
    input_dir: str | None = None
    output_dir: str | None = None
    api_key: str | None = None
    base_url: str | None = None
    llm_model: str | None = None
    # 第 1 层：ASR 语音识别独立接口
    asr_provider: str | None = None
    asr_api_key: str | None = None
    asr_base_url: str | None = None
    asr_model: str | None = None
    asr_hotwords: str | None = None
    # 第 2 层：粗筛大模型
    screen_llm_model: str | None = None
    screen_llm_base_url: str | None = None
    screen_llm_api_key: str | None = None
    screen_reasoning_effort: str | None = None
    # 第 3 层：双语精修大模型
    polish_llm_model: str | None = None
    polish_llm_base_url: str | None = None
    polish_llm_api_key: str | None = None
    polish_reasoning_effort: str | None = None


class FolderReq(BaseModel):
    initial_dir: str | None = None
    title: str = "选择文件夹"


class BrowseFileReq(BaseModel):
    initial_dir: str | None = None
    title: str = "选择视频文件"
    kind: str = "video"


class ChatImportReq(BaseModel):
    stem: str
    chat_path: str
    output_dir: str | None = None
    bucket_sec: float = Field(default=5.0, ge=1.0, le=120.0, allow_inf_nan=False)


class ManualHighlightsCreateReq(BaseModel):
    video_path: str
    output_dir: str | None = None
    items: list[dict[str, Any]] = Field(min_length=1, max_length=200)


class OpenFolderReq(BaseModel):
    path: str


class HighlightsUpdateReq(BaseModel):
    highlights_json_path: str
    highlights: list[dict[str, Any]]


class AnalyzeTaskReq(BaseModel):
    video_paths: list[str] = Field(min_length=1)
    output_dir: str
    top_percent: float = Field(default=15.0, gt=0.0, le=100.0, allow_inf_nan=False)
    asr_provider: str | None = None
    asr_mode: str = "peaks"
    top_k_peaks: int = Field(default=20, ge=1, le=500)
    model_size: str | None = None
    asr_model: str | None = None
    hotwords: str | None = None
    normalize_audio: bool = True
    vad_threshold: float = Field(default=0.30, ge=0.0, le=1.0, allow_inf_nan=False)
    language: str | None = None
    target_language: str | None = None
    top_k_highlights: int | None = None
    min_clip_sec: int | None = None
    max_clip_sec: int | None = None
    bilingual_subtitles: bool = True
    audio_stream: int = Field(default=0, ge=0)
    vocal_bandpass: bool = True
    force_rerun: bool = False


class RetranslateTaskReq(BaseModel):
    highlights_json_path: str
    hotwords: str | None = None
    extra_context: str | None = None
    source_language: str | None = None
    target_language: str | None = None
    ranks: list[int] | None = None


class CorpusExpandReq(BaseModel):
    lang_pairs: list[str] | None = None
    commercial_only: bool = False


class ExportTaskReq(BaseModel):
    highlights_json_path: str
    video_path: str | None = None
    output_dir: str | None = None
    mode: str = "lossless"
    subtitle_lang: str = "bilingual"
    subtitle_mode: str | None = None
    show_title_banner: bool = False
    burn_subtitles: bool = True
    only_reviewed: bool = False
    encoder: str = "auto"
    ranks: list[int] | None = None


class DiagnoseTaskReq(BaseModel):
    video_paths: list[str] = Field(min_length=1)
    output_dir: str
    top_percent: float = Field(default=15.0, gt=0.0, le=100.0, allow_inf_nan=False)
    audio_stream: int = Field(default=0, ge=0)
    vocal_bandpass: bool = True
    force_rerun: bool = False


class UserGlossaryUpdateReq(BaseModel):
    terms: list[dict[str, Any]] = Field(default_factory=list)


class MediaBinsUpdateReq(BaseModel):
    folders: list[dict[str, Any]] = Field(default_factory=list)
    assignments: dict[str, str] = Field(default_factory=dict)
    tags: dict[str, str] = Field(default_factory=dict)


_SESSION_TOKEN: str = secrets.token_urlsafe(32)
_SESSION_COOKIE_NAME = "solacut_session"
_SESSION_HEADER_NAME = "x-solacut-token"
_ALLOWED_LOCAL_HOSTS: frozenset[str] = frozenset({"127.0.0.1", "localhost", "::1", "testserver"})
_COOKIE_AUTH_MEDIA_PATHS: frozenset[str] = frozenset({"/api/video-stream", "/api/thumbnail"})


def _extract_host_and_port(host_header: str) -> tuple[str, int | None]:
    """安全解析 Host 请求头中的主机名与端口（兼容 IPv6 `[::1]:8765`）。"""
    raw = (host_header or "").strip()
    if not raw:
        return "", None
    try:
        parsed = urlsplit(f"http://{raw}")
        return (parsed.hostname or "").lower(), parsed.port
    except ValueError:
        return "", None


def _is_same_local_origin(origin_header: str, request_host_header: str) -> bool:
    """校验 Origin 是否指向本机 loopback 且主机与端口严格同源，彻底阻断跨站/跨端口 CSRF。"""
    try:
        parsed = urlsplit(origin_header.strip())
    except ValueError:
        return False
    if parsed.scheme not in {"http", "https"}:
        return False
    origin_host = (parsed.hostname or "").lower()
    if origin_host not in _ALLOWED_LOCAL_HOSTS:
        return False
    req_host, req_port = _extract_host_and_port(request_host_header)
    if not req_host:
        return False
    if origin_host != req_host and not ({origin_host, req_host} <= {"127.0.0.1", "localhost"}):
        return False
    origin_port = parsed.port if parsed.port is not None else (443 if parsed.scheme == "https" else 80)
    effective_req_port = req_port if req_port is not None else 80
    if origin_port != effective_req_port:
        return False
    return True


_WINDOWS_RESERVED_DEVICE_NAMES: frozenset[str] = frozenset(
    {
        "con",
        "prn",
        "aux",
        "nul",
        *(f"com{i}" for i in range(1, 10)),
        *(f"lpt{i}" for i in range(1, 10)),
    }
)
_CTRL_CHARS_RE = re.compile(r"[\x00-\x1f\x7f]+")

# 服务端内存白名单：仅记录用户通过系统原生对话框 /api/browse-file 显式选中的文件路径，
# 既 100% 支持读取任意盘符 4K 原片/弹幕文件，又彻底封死任意磁盘路径探测漏洞
_USER_AUTHORIZED_FILES: set[str] = set()
_AUTHORIZED_FILES_LOCK = threading.Lock()
_CHAT_LOCKS_GUARD = threading.Lock()
_CHAT_STEM_LOCKS: dict[str, threading.Lock] = {}


def _authorize_user_file(path: Path | str) -> None:
    """将用户经系统原生文件框显式选中的本地路径加入服务端内存授权白名单。"""
    try:
        key = os.path.normcase(str(Path(path).resolve()))
    except (OSError, ValueError):
        return
    with _AUTHORIZED_FILES_LOCK:
        if len(_USER_AUTHORIZED_FILES) >= 1024:
            _USER_AUTHORIZED_FILES.clear()
        _USER_AUTHORIZED_FILES.add(key)


def _is_user_authorized_file(path: Path) -> bool:
    """判断路径是否已通过 /api/browse-file 显式授权。"""
    try:
        key = os.path.normcase(str(path.resolve()))
    except (OSError, ValueError):
        return False
    with _AUTHORIZED_FILES_LOCK:
        return key in _USER_AUTHORIZED_FILES


def _get_chat_stem_lock(out_dir: Path, stem: str) -> threading.Lock:
    """获取按 (output_dir, stem) 隔离的弹幕导入互斥锁，防并发导入撕裂 DB/JSON。"""
    key = os.path.normcase(str((out_dir / stem).resolve()))
    with _CHAT_LOCKS_GUARD:
        lock = _CHAT_STEM_LOCKS.get(key)
        if lock is None:
            lock = threading.Lock()
            _CHAT_STEM_LOCKS[key] = lock
        return lock


def _is_windows_reserved_name(segment: str) -> bool:
    """检测单个文件名/目录分量是否命中 Windows 保留设备名（含 CON.json / NUL.txt 等扩展名形式）。"""
    cleaned = (segment or "").strip().rstrip(". ")
    if not cleaned:
        return False
    base = cleaned.split(".", 1)[0].rstrip(". ").lower()
    return base in _WINDOWS_RESERVED_DEVICE_NAMES


def _reject_unsafe_path(raw_path: str | None) -> None:
    """强约束路径守卫：拦截空字节、UNC、`\\\\?\\`、`\\??\\` 与 Windows 保留设备名（含扩展名形式）。"""
    cleaned = (raw_path or "").strip()
    if not cleaned:
        return
    if "\x00" in cleaned:
        raise HTTPException(status_code=400, detail="安全限制：路径包含非法空字节")
    norm = cleaned.replace("/", "\\")
    if norm.startswith("\\\\") or norm.startswith("\\??\\") or norm.startswith("\\\\?\\") or norm.startswith("\\\\.\\"):
        raise HTTPException(status_code=400, detail="安全限制：不允许访问网络 UNC 或系统设备路径")
    # 检查路径每个分量是否命中 Windows 保留设备名（CON/PRN/AUX/NUL/COM1-9/LPT1-9 及其带后缀形式）
    for part in norm.split("\\"):
        if not part or (len(part) == 2 and part[1] == ":" and part[0].isalpha()):
            continue
        if _is_windows_reserved_name(part):
            raise HTTPException(
                status_code=400,
                detail=f"安全限制：路径包含 Windows 保留设备名 '{part}'，已拒绝访问",
            )


# 兼容历史调用别名
_reject_unc_path = _reject_unsafe_path


def _validate_safe_stem(stem: str) -> str:
    """校验视频 stem 仅允许纯文件名片段，严禁路径分隔符、盘符、`..` 穿越与 Windows 保留设备名。"""
    cleaned = (stem or "").strip()
    if (
        not cleaned
        or cleaned in {".", ".."}
        or "/" in cleaned
        or "\\" in cleaned
        or ":" in cleaned
        or "\x00" in cleaned
        or ".." in cleaned
        or _is_windows_reserved_name(cleaned)
    ):
        raise HTTPException(status_code=400, detail="安全限制：非法的视频标识 stem")
    return cleaned


def _is_path_within(child: Path, parent: Path) -> bool:
    """安全判断 child（解开所有符号链接后）是否严格位于 parent 目录或其子目录下。"""
    try:
        child.resolve().relative_to(parent.resolve())
        return True
    except (OSError, ValueError):
        return False


def _has_symlink_escape(raw_target: Path, allowed_roots: list[Path]) -> bool:
    """检测路径自身或祖先中是否存在指向允许根目录之外的符号链接。"""
    try:
        resolved = raw_target.resolve()
    except (OSError, ValueError):
        return True
    if not any(_is_path_within(resolved, root) for root in allowed_roots):
        return True
    # 进一步检查路径本身若为 symlink，解析后仍必须留在允许根目录内
    cur = raw_target
    for _ in range(32):
        if cur.is_symlink():
            if not any(_is_path_within(cur.resolve(), root) for root in allowed_roots):
                return True
        if cur.parent == cur:
            break
        cur = cur.parent
    return False


def _get_allowed_write_roots(cfg: dict[str, Any]) -> list[Path]:
    """返回所有允许写入的工作区根目录（仅限已配置 output_dir 与 _PROJECT_ROOT）。"""
    roots = [_PROJECT_ROOT.resolve()]
    cfg_out = str(cfg.get("output_dir") or "").strip()
    if cfg_out:
        try:
            roots.append(Path(cfg_out).resolve())
        except (OSError, ValueError):
            pass
    return roots


def _get_allowed_read_roots(cfg: dict[str, Any]) -> list[Path]:
    """返回所有允许读取的工作区根目录（input_dir / output_dir / _PROJECT_ROOT）。"""
    roots = _get_allowed_write_roots(cfg)
    cfg_in = str(cfg.get("input_dir") or "").strip()
    if cfg_in:
        try:
            roots.append(Path(cfg_in).resolve())
        except (OSError, ValueError):
            pass
    return roots


def _resolve_allowed_output_dir(
    raw_dir: str | None,
    *,
    cfg: dict[str, Any] | None = None,
    create: bool = False,
) -> Path:
    """校验并返回允许写入的输出目录，禁止任意盘符 mkdir/覆写与符号链接穿越。"""
    _reject_unsafe_path(raw_dir)
    active_cfg = cfg if cfg is not None else _read_workbench_config()
    target_str = (raw_dir or "").strip() or str(active_cfg.get("output_dir") or (_PROJECT_ROOT / "output"))
    _reject_unsafe_path(target_str)

    raw_target = Path(target_str)
    allowed_roots = _get_allowed_write_roots(active_cfg)
    if _has_symlink_escape(raw_target, allowed_roots):
        raise HTTPException(
            status_code=403,
            detail="安全限制：输出目录必须位于已配置的工作区 output_dir 或项目目录内，已拒绝跨目录写入",
        )
    resolved = raw_target.resolve()
    if resolved.exists() and not resolved.is_dir():
        raise HTTPException(status_code=400, detail="安全限制：输出目录路径指向了一个已存在的普通文件")
    if create:
        resolved.mkdir(parents=True, exist_ok=True)
    return resolved


def _resolve_allowed_input_dir(
    raw_dir: str | None,
    *,
    cfg: dict[str, Any] | None = None,
) -> Path:
    """校验并返回允许扫描的输入目录（仅限已配置 input_dir / output_dir / _PROJECT_ROOT 内）。"""
    _reject_unsafe_path(raw_dir)
    active_cfg = cfg if cfg is not None else _read_workbench_config()
    target_str = (raw_dir or "").strip() or str(active_cfg.get("input_dir") or (_PROJECT_ROOT / "input"))
    _reject_unsafe_path(target_str)

    raw_target = Path(target_str)
    allowed_roots = _get_allowed_read_roots(active_cfg)
    if _has_symlink_escape(raw_target, allowed_roots):
        raise HTTPException(
            status_code=403,
            detail="安全限制：仅允许访问已配置工作区目录（input_dir / output_dir / 项目目录）",
        )
    return raw_target.resolve()


def _resolve_allowed_read_path(
    raw_path: str,
    allowed_exts: frozenset[str],
    *,
    cfg: dict[str, Any] | None = None,
    bad_ext_status: int = 404,
) -> Path:
    """校验并返回允许读取的文件路径：仅放行已配置工作区或经 /api/browse-file 显式授权的文件。"""
    _reject_unsafe_path(raw_path)
    cleaned = (raw_path or "").strip()
    if not cleaned:
        raise HTTPException(status_code=400, detail="文件路径不能为空")

    raw_target = Path(cleaned)
    ext = raw_target.suffix.lower()
    if bad_ext_status == 400 and ext not in allowed_exts:
        raise HTTPException(
            status_code=400,
            detail=f"安全限制：不支持的文件格式（当前：{ext or '无扩展名'}）",
        )

    active_cfg = cfg if cfg is not None else _read_workbench_config()
    allowed_roots = _get_allowed_read_roots(active_cfg)
    try:
        resolved = raw_target.resolve()
    except (OSError, ValueError) as exc:
        raise HTTPException(status_code=400, detail="非法的文件路径") from exc

    in_workspace = not _has_symlink_escape(raw_target, allowed_roots)
    authorized = _is_user_authorized_file(resolved)
    if not in_workspace and not authorized:
        raise HTTPException(
            status_code=403,
            detail="安全限制：仅允许访问工作区目录内或通过系统文件选择框显式授权的文件",
        )

    if ext not in allowed_exts:
        raise HTTPException(status_code=bad_ext_status, detail="找不到有效格式的受支持文件")
    if not resolved.is_file():
        raise HTTPException(status_code=404, detail=f"找不到指定文件：{resolved.name}")
    return resolved


def _validate_highlights_json_path(
    raw_path: str,
    *,
    cfg: dict[str, Any] | None = None,
) -> Path:
    """校验高光方案 JSON 路径必须位于允许输出目录内且以 `*_highlights.json` 结尾。"""
    _reject_unsafe_path(raw_path)
    raw_p = Path(raw_path)
    if not raw_p.name.lower().endswith("_highlights.json"):
        raise HTTPException(status_code=400, detail="安全限制：仅允许读写 *_highlights.json 高光方案文件")
    active_cfg = cfg if cfg is not None else _read_workbench_config()
    allowed_roots = _get_allowed_write_roots(active_cfg)
    if _has_symlink_escape(raw_p, allowed_roots):
        raise HTTPException(
            status_code=403,
            detail="安全限制：高光方案文件必须位于已配置的工作区 output_dir 或项目目录内",
        )
    resolved = raw_p.resolve()
    if not resolved.is_file():
        raise HTTPException(status_code=404, detail=f"找不到高光 JSON 文件：{resolved}")
    return resolved


def _require_finite_float(value: Any, field_name: str) -> float:
    """严格解析有限浮点数，拦截 NaN / Infinity / 非数字并抛出 HTTP 400。"""
    if isinstance(value, bool):
        raise HTTPException(status_code=400, detail=f"参数 {field_name} 必须为有限数字")
    try:
        num = float(value)
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=f"参数 {field_name} 必须为有限数字") from exc
    if not math.isfinite(num):
        raise HTTPException(status_code=400, detail=f"参数 {field_name} 不允许为 NaN 或 Infinity")
    return num


def _sanitize_client_subtitles(
    raw_subs: list[Any],
    *,
    clip_start_sec: float,
    clip_end_sec: float,
    is_same_lang: bool,
    is_zh_src: bool,
) -> list[dict[str, Any]]:
    """严格清洗并校验客户端提交的手动字幕数组（限条数、限字长、有限时间轴、剥控制符）。"""
    if len(raw_subs) > 500:
        raise HTTPException(status_code=400, detail="单条片段最多允许提交 500 条字幕")

    cleaned: list[dict[str, Any]] = []
    for idx, row in enumerate(raw_subs, start=1):
        if not isinstance(row, dict):
            raise HTTPException(status_code=400, detail=f"第 {idx} 条字幕格式必须是对象")
        s_val = _require_finite_float(row.get("start_sec", clip_start_sec), f"subtitles[{idx}].start_sec")
        e_val = _require_finite_float(row.get("end_sec", clip_end_sec), f"subtitles[{idx}].end_sec")
        s_clip = round(max(0.0, s_val), 2)
        e_clip = round(max(0.0, e_val), 2)
        if e_clip <= s_clip:
            raise HTTPException(status_code=400, detail=f"第 {idx} 条字幕结束时间必须大于开始时间")

        def _clean_sub_text(val: Any) -> str:
            if val is None:
                return ""
            return _CTRL_CHARS_RE.sub(" ", str(val)).strip()[:500]

        t_orig = _clean_sub_text(row.get("text_orig") or row.get("text"))
        t_src = _clean_sub_text(row.get("text_src"))
        t_tgt = _clean_sub_text(row.get("text_tgt"))
        t_zh = _clean_sub_text(row.get("text_zh"))
        t_en = _clean_sub_text(row.get("text_en"))

        if not (t_orig or t_src or t_tgt or t_zh or t_en):
            continue

        if not t_src:
            t_src = t_orig or (t_zh if is_zh_src else t_en)
        if not t_orig:
            t_orig = t_src or t_tgt
        if is_same_lang:
            unified = t_tgt or t_src or t_orig
            t_src = unified
            t_tgt = unified
            t_zh = unified
            t_en = unified
        else:
            if not t_zh and is_zh_src:
                t_zh = t_src
            if not t_en and not is_zh_src:
                t_en = t_src

        cleaned.append(
            {
                "start_sec": s_clip,
                "end_sec": e_clip,
                "text_orig": t_orig,
                "text_src": t_src,
                "text_tgt": t_tgt,
                "text_zh": t_zh,
                "text_en": t_en,
            }
        )
    return cleaned


# 生产态关闭自动暴露的 Swagger/OpenAPI 交互文档，减小本地攻击面
app = FastAPI(
    title="Solacut 工作台",
    version="1.0.0",
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
)


@app.middleware("http")
async def _security_and_cache_middleware(request: Request, call_next: Any) -> Any:
    """本地安全守卫：防 DNS Rebinding、防跨站/跨端口 CSRF、校验每会话随机令牌并注入安全响应头。"""
    host_header = request.headers.get("host", "")
    hostname, _ = _extract_host_and_port(host_header)
    if not hostname or hostname not in _ALLOWED_LOCAL_HOSTS:
        return JSONResponse(status_code=403, content={"detail": "禁止非本机或非法 Host 访问"})

    path = request.url.path
    if path.startswith("/api/") and path != "/api/health":
        sec_fetch_site = (request.headers.get("sec-fetch-site") or "").strip().lower()
        if sec_fetch_site in {"cross-site", "same-site"}:
            return JSONResponse(status_code=403, content={"detail": "已拦截跨站或跨端口请求"})

        origin = (request.headers.get("origin") or "").strip()
        if origin and not _is_same_local_origin(origin, host_header):
            return JSONResponse(status_code=403, content={"detail": "已拦截非同源跨站请求"})

        header_token = (request.headers.get(_SESSION_HEADER_NAME) or "").strip()
        token_ok = bool(header_token) and secrets.compare_digest(header_token, _SESSION_TOKEN)

        if not token_ok and path in _COOKIE_AUTH_MEDIA_PATHS and request.method.upper() in {"GET", "HEAD"}:
            cookie_token = (request.cookies.get(_SESSION_COOKIE_NAME) or "").strip()
            token_ok = bool(cookie_token) and secrets.compare_digest(cookie_token, _SESSION_TOKEN)

        if not token_ok:
            return JSONResponse(status_code=403, content={"detail": "缺少或无效的本地工作台会话令牌"})

    resp = await call_next(request)
    # 基础响应安全头
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["X-Frame-Options"] = "DENY"
    resp.headers["Referrer-Policy"] = "no-referrer"
    # 防止桌面 WebView2 持久化 Profile 缓存旧版前端 JS/CSS/HTML 导致版本错乱
    if path == "/" or path.startswith("/static/"):
        resp.headers["Cache-Control"] = "no-store"
    return resp


if _WEB_DIR.is_dir():
    app.mount("/static", StaticFiles(directory=str(_WEB_DIR)), name="static")


@app.get("/favicon.ico", include_in_schema=False)
def serve_favicon() -> FileResponse:
    """提供应用品牌图标给浏览器标签页与快捷方式。"""
    fav = _WEB_DIR / "brand" / "favicon.ico"
    if not fav.is_file():
        raise HTTPException(status_code=404, detail="找不到图标文件")
    return FileResponse(path=str(fav), media_type="image/x-icon")


@app.get("/", response_class=HTMLResponse)
def serve_index() -> HTMLResponse:
    """返回工作台单页前端页面，并注入当前进程随机本地会话令牌与 SameSite=Strict 媒体 Cookie。"""
    index_file = _WEB_DIR / "index.html"
    if not index_file.is_file():
        raise HTTPException(status_code=404, detail="找不到前端页面 web/index.html")
    raw_html = index_file.read_text(encoding="utf-8")
    meta_tag = f'  <meta name="solacut-token" content="{html.escape(_SESSION_TOKEN, quote=True)}">\n'
    if "</head>" in raw_html:
        injected_html = raw_html.replace("</head>", f"{meta_tag}</head>", 1)
    else:
        injected_html = meta_tag + raw_html
    resp = HTMLResponse(content=injected_html)
    resp.set_cookie(
        key=_SESSION_COOKIE_NAME,
        value=_SESSION_TOKEN,
        path="/",
        httponly=True,
        samesite="strict",
    )
    return resp


@app.get("/api/health")
def health_check() -> dict[str, Any]:
    """桌面窗口启动探活接口。"""
    return {"ok": True, "service": "solacut"}


@app.get("/api/config")
def get_config() -> dict[str, Any]:
    """读取当前工作台目录设置与脱敏后的三层 API 状态。"""
    return _read_workbench_config()


def _read_media_bins() -> dict[str, Any]:
    """读取本地媒体池虚拟文件夹（Bin）与素材颜色标签持久化数据。"""
    default_data: dict[str, Any] = {"folders": [], "assignments": {}, "tags": {}}
    if not _MEDIA_BINS_PATH.is_file():
        return default_data
    try:
        raw = json.loads(_MEDIA_BINS_PATH.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            return default_data
        folders = raw.get("folders") if isinstance(raw.get("folders"), list) else []
        assignments = raw.get("assignments") if isinstance(raw.get("assignments"), dict) else {}
        tags = raw.get("tags") if isinstance(raw.get("tags"), dict) else {}
        return {"folders": folders, "assignments": assignments, "tags": tags}
    except Exception:
        return default_data


@app.get("/api/media-bins")
def api_get_media_bins() -> dict[str, Any]:
    """获取媒体池自定义文件夹与素材自由分类映射。"""
    return _read_media_bins()


@app.put("/api/media-bins")
def api_save_media_bins(req: MediaBinsUpdateReq) -> dict[str, Any]:
    """持久化保存媒体池自定义文件夹（Bin）、素材归属与颜色标记（零修改磁盘原视频）。"""
    clean_folders: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for item in req.folders[:200]:
        if not isinstance(item, dict):
            continue
        fid = str(item.get("id") or "").strip()[:64]
        name = str(item.get("name") or "").strip()[:60]
        color = str(item.get("color") or "#4f8ef7").strip()[:24]
        if not fid or not name or fid in seen_ids:
            continue
        seen_ids.add(fid)
        raw_created = item.get("created_at")
        created_ts = _require_finite_float(raw_created, "folders.created_at") if raw_created is not None else time.time()
        clean_folders.append(
            {
                "id": fid,
                "name": name,
                "color": color,
                "created_at": created_ts,
            }
        )

    valid_ids = {f["id"] for f in clean_folders}
    clean_assignments: dict[str, str] = {}
    for k, v in req.assignments.items():
        ks = str(k or "").strip()
        vs = str(v or "").strip()
        if ks and vs in valid_ids:
            clean_assignments[ks] = vs

    valid_tag_colors = {"red", "orange", "green", "blue", "purple"}
    clean_tags: dict[str, str] = {}
    for k, v in req.tags.items():
        ks = str(k or "").strip()
        vs = str(v or "").strip().lower()
        if ks and vs in valid_tag_colors:
            clean_tags[ks] = vs

    payload = {
        "folders": clean_folders,
        "assignments": clean_assignments,
        "tags": clean_tags,
    }
    atomic_write_json(_MEDIA_BINS_PATH, payload, indent=2)
    return {"ok": True, **payload}


@app.get("/api/corpus/stats")
def api_corpus_stats() -> dict[str, Any]:
    """返回本地开源高质量双语语料库与专家词典统计信息。"""
    return get_corpus_stats(_CORPUS_ROOT)


@app.get("/api/corpus/user-glossary")
def api_get_user_glossary() -> dict[str, Any]:
    """读取公会与主播专属自定义术语/ASR误听记忆库。"""
    return {"terms": load_user_glossary(_CORPUS_ROOT)}


@app.post("/api/corpus/user-glossary")
def api_save_user_glossary(req: UserGlossaryUpdateReq) -> dict[str, Any]:
    """保存公会与主播专属自定义术语/ASR误听记忆库（即时对下一次双语精修生效）。"""
    saved = save_user_glossary(req.terms, _CORPUS_ROOT)
    return {"ok": True, "count": len(saved), "terms": saved}


@app.post("/api/corpus/expand")
def api_expand_corpus(req: CorpusExpandReq) -> dict[str, Any]:
    """后台一键下载/扩展 6 大语种完整影视口语平行语料包（60 万级），用于个人研究体验。"""
    from src.corpus_builder import ALL_LANG_PAIRS, build_corpus_for_lang_pair
    from src.corpus_rag import DEFAULT_CORPUS_RAG

    targets = [lp for lp in (req.lang_pairs or list(ALL_LANG_PAIRS)) if lp in ALL_LANG_PAIRS]
    if not targets:
        raise HTTPException(status_code=400, detail="没有有效的目标语种对")

    mode_label = "商用纯净语料包" if req.commercial_only else "60万全量影视口语扩展包"
    title = f"构建{mode_label}（{len(targets)} 语种）"

    def _worker(report_cb: Any) -> dict[str, Any]:
        results: dict[str, Any] = {}
        total = len(targets)
        for idx, lp in enumerate(targets, start=1):
            pct = int(((idx - 1) / total) * 95)
            report_cb("corpus", pct, f"[{idx}/{total}] 正在流式构建 {lp} 语料与 FTS5 索引...")
            info = build_corpus_for_lang_pair(
                lang_pair=lp,
                corpus_root=_CORPUS_ROOT,
                commercial_only=req.commercial_only,
                include_opensubtitles=not req.commercial_only,
            )
            results[lp] = info
        DEFAULT_CORPUS_RAG.invalidate_cache()
        report_cb("done", 100, f"全部 {total} 个语种语料索引更新完成！")
        return {"updated": list(results.keys()), "stats": get_corpus_stats(_CORPUS_ROOT)}

    return _TASK_MANAGER.submit("corpus_expand", title, _worker)


@app.get("/api/onboarding/status")
def api_onboarding_status() -> dict[str, Any]:
    """检测首次启动就绪状态：是否已配 API Key、是否具备内置 30 秒示例工程及 FFmpeg 状态。"""
    cfg = _read_workbench_config()
    has_key = bool(
        cfg.get("has_api_key")
        or cfg.get("has_screen_api_key")
        or cfg.get("has_polish_api_key")
    )
    demo_video = _DEMO_ASSETS_DIR / "demo_highlight_30s.mp4"
    demo_hl = _DEMO_ASSETS_DIR / "demo_highlight_30s_highlights.json"
    has_demo = demo_video.is_file() and demo_hl.is_file()
    ffmpeg_ok = True
    try:
        check_ffmpeg_available()
    except RuntimeError:
        ffmpeg_ok = False
    return {
        "has_api_key": has_key,
        "has_demo": has_demo,
        "has_ffmpeg": ffmpeg_ok,
        "input_dir": cfg.get("input_dir", ""),
        "output_dir": cfg.get("output_dir", ""),
    }


@app.post("/api/onboarding/load-demo")
def api_load_demo_project() -> dict[str, Any]:
    """将内置 30 秒免版权多语种体验工程导入工作区，零 API Key 即可体验完整审片/改词/导出流程。"""
    demo_video = _DEMO_ASSETS_DIR / "demo_highlight_30s.mp4"
    if not demo_video.is_file():
        raise HTTPException(status_code=404, detail="内置示例视频缺失，请检查 assets/demo/ 目录")

    cfg = _read_workbench_config()
    in_dir = Path(cfg["input_dir"])
    out_dir = Path(cfg["output_dir"])
    in_dir.mkdir(parents=True, exist_ok=True)
    out_dir.mkdir(parents=True, exist_ok=True)

    dst_video = in_dir / demo_video.name
    shutil.copy2(demo_video, dst_video)
    stem = dst_video.stem

    # 同步拷贝预置的 16k WAV 与 JSON，并把内部绝对路径实时改写为当前工作区路径
    demo_wav = _DEMO_ASSETS_DIR / f"{stem}_16k.wav"
    dst_wav = out_dir / f"{stem}_16k.wav"
    if demo_wav.is_file():
        shutil.copy2(demo_wav, dst_wav)

    for suffix in ("_audio_peaks.json", "_transcript.json", "_highlights.json"):
        src_j = _DEMO_ASSETS_DIR / f"{stem}{suffix}"
        if not src_j.is_file():
            continue
        payload = json.loads(src_j.read_text(encoding="utf-8"))
        if "video_path" in payload:
            payload["video_path"] = str(dst_video.resolve())
        if "wav_path" in payload and dst_wav.is_file():
            payload["wav_path"] = str(dst_wav.resolve())
        if "peaks_json_path" in payload:
            payload["peaks_json_path"] = str((out_dir / f"{stem}_audio_peaks.json").resolve())
        if "transcript_json_path" in payload:
            payload["transcript_json_path"] = str((out_dir / f"{stem}_transcript.json").resolve())
        if "highlights_json_path" in payload:
            payload["highlights_json_path"] = str((out_dir / f"{stem}_highlights.json").resolve())
        if "review_md_path" in payload:
            payload["review_md_path"] = str((out_dir / f"{stem}_review.md").resolve())
        dst_j = out_dir / f"{stem}{suffix}"
        atomic_write_json(dst_j, payload, indent=2)

    # 同步预热内置弹幕文件 -> 本地 SQLite FTS5 库与轻量弹幕热力图 JSON，使体验工程开箱即带音弹双雷达
    demo_chat = _DEMO_ASSETS_DIR / f"{stem}_chat.json"
    if demo_chat.is_file():
        try:
            parsed_chat = parse_chat_file(demo_chat)
            peaks_file = out_dir / f"{stem}_audio_peaks.json"
            demo_audio_peaks: list[dict[str, Any]] = []
            demo_dur_hint: float | None = 30.0
            if peaks_file.is_file():
                pdata = json.loads(peaks_file.read_text(encoding="utf-8"))
                if isinstance(pdata, dict):
                    if isinstance(pdata.get("peak_segments"), list):
                        demo_audio_peaks = pdata["peak_segments"]
                    dval = float(pdata.get("total_duration_sec") or 0.0)
                    if math.isfinite(dval) and dval > 0:
                        demo_dur_hint = dval
            spikes = analyze_chat_spikes(
                parsed_chat["messages"],
                duration_sec=demo_dur_hint,
                bucket_sec=2.0,
            )
            fusion_peaks = (
                fuse_audio_and_chat_peaks(demo_audio_peaks, spikes["chat_peaks"])
                if demo_audio_peaks
                else []
            )
            db_path = out_dir / f"{stem}_chat_messages.db"
            radar_path = out_dir / f"{stem}_chat_radar.json"
            radar_payload: dict[str, Any] = {
                "stem": stem,
                "source_filename": demo_chat.name,
                "format": parsed_chat["format"],
                "message_count": parsed_chat["message_count"],
                "duration_sec": spikes["duration_sec"],
                "bucket_sec": spikes["bucket_sec"],
                "total_hype_messages": spikes["total_hype_messages"],
                "baseline_median": spikes["baseline_median"],
                "baseline_mad": spikes["baseline_mad"],
                "messages_db": db_path.name,
                "buckets": spikes["buckets"],
                "chat_peaks": spikes["chat_peaks"],
                "fusion_peaks": fusion_peaks,
            }
            with _get_chat_stem_lock(out_dir, stem):
                save_chat_messages_db(db_path, parsed_chat["messages"])
                atomic_write_json(radar_path, radar_payload, indent=None)
        except Exception as exc:
            logger.warning("预热内置示例弹幕雷达失败（已静默降级）: %s", exc)

    return {
        "ok": True,
        "video_path": str(dst_video.resolve()),
        "stem": stem,
        "output_dir": str(out_dir.resolve()),
    }


@app.post("/api/config")
def update_config(req: ConfigUpdateReq) -> dict[str, Any]:
    """保存目录偏好与 .env 三层接口配置（ASR / 粗筛 / 双语精修）。"""
    current = _read_workbench_config()
    new_in = req.input_dir.strip() if req.input_dir is not None else current["input_dir"]
    new_out = req.output_dir.strip() if req.output_dir is not None else current["output_dir"]
    if new_in:
        _reject_unsafe_path(new_in)
        in_p = Path(new_in)
        if in_p.exists() and not in_p.is_dir():
            raise HTTPException(status_code=400, detail="输入目录路径不能指向已存在的普通文件")
    if new_out:
        _reject_unsafe_path(new_out)
        out_p = Path(new_out)
        if out_p.exists() and not out_p.is_dir():
            raise HTTPException(status_code=400, detail="输出目录路径不能指向已存在的普通文件")

    env_updates: dict[str, str | None] = {
        "OPENAI_API_KEY": req.api_key,
        "OPENAI_BASE_URL": req.base_url,
        "LLM_MODEL": req.llm_model if req.llm_model is not None else req.screen_llm_model,
        "ASR_PROVIDER": req.asr_provider,
        "ASR_API_KEY": req.asr_api_key,
        "ASR_BASE_URL": req.asr_base_url,
        "ASR_MODEL": req.asr_model,
        "ASR_HOTWORDS": req.asr_hotwords,
        "SCREEN_LLM_MODEL": req.screen_llm_model,
        "SCREEN_LLM_BASE_URL": req.screen_llm_base_url,
        "SCREEN_LLM_API_KEY": req.screen_llm_api_key,
        "SCREEN_LLM_REASONING_EFFORT": req.screen_reasoning_effort,
        "POLISH_LLM_MODEL": req.polish_llm_model,
        "POLISH_LLM_BASE_URL": req.polish_llm_base_url,
        "POLISH_LLM_API_KEY": req.polish_llm_api_key,
        "POLISH_LLM_REASONING_EFFORT": req.polish_reasoning_effort,
    }
    if any(v is not None for v in env_updates.values()):
        try:
            _write_env_values(env_updates)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    if new_in:
        Path(new_in).mkdir(parents=True, exist_ok=True)
    if new_out:
        Path(new_out).mkdir(parents=True, exist_ok=True)

    atomic_write_json(
        _CONFIG_PATH,
        {"input_dir": new_in, "output_dir": new_out},
        indent=2,
    )

    return _read_workbench_config()


def _run_tk_file_dialog(dialog_cb: Any, *, timeout_sec: float = 180.0) -> str | None:
    """在独立线程并串行互斥下弹出置顶系统选择框；超时若线程仍存活绝不提前释放锁，防并发炸毁 Tcl。"""
    dialog_lock = _FILE_DIALOG_LOCK
    if not dialog_lock.acquire(blocking=False):
        raise HTTPException(status_code=409, detail="已有系统文件选择窗口正在打开，请先完成当前选择")

    result_box: dict[str, Any] = {"path": None, "error": None}

    def _ask() -> None:
        root = None
        try:
            import tkinter as tk

            root = tk.Tk()
            root.withdraw()
            root.lift()
            root.attributes("-topmost", True)
            try:
                root.focus_force()
            except Exception:
                pass
            chosen = dialog_cb(root)
            if chosen:
                result_box["path"] = str(Path(chosen).resolve())
        except Exception as exc:
            result_box["error"] = exc
        finally:
            if root is not None:
                try:
                    root.destroy()
                except Exception:
                    pass
            # 锁的释放权严格归属对话框线程，确保系统对话框未真正关闭前绝不重入
            dialog_lock.release()

    t = threading.Thread(target=_ask, name="solacut-file-dialog", daemon=True)
    try:
        t.start()
    except Exception:
        dialog_lock.release()
        raise

    t.join(timeout=timeout_sec)
    if t.is_alive():
        raise HTTPException(
            status_code=408,
            detail="系统文件选择对话框等待超时，请先关闭当前已打开的系统选择窗口后再重试",
        )
    if result_box["error"] is not None:
        raise result_box["error"]
    return result_box["path"]


@app.post("/api/browse-folder")
def browse_folder(req: FolderReq) -> dict[str, Any]:
    """弹出系统原生文件夹选择框（Windows tkinter），用户取消则返回 null。"""
    _reject_unsafe_path(req.initial_dir)
    initial = req.initial_dir or str(_PROJECT_ROOT)
    if not Path(initial).is_dir():
        initial = str(_PROJECT_ROOT)

    try:
        from tkinter import filedialog

        chosen = _run_tk_file_dialog(
            lambda root: filedialog.askdirectory(parent=root, initialdir=initial, title=req.title)
        )
        return {"path": chosen, "cancelled": not bool(chosen)}
    except HTTPException:
        raise
    except Exception:
        return {"path": None, "cancelled": True}


@app.post("/api/browse-file")
def browse_file(req: BrowseFileReq) -> dict[str, Any]:
    """弹出系统原生文件选择对话框（支持视频原片或本地弹幕文件），选中后自动加入服务端授权白名单。"""
    _reject_unsafe_path(req.initial_dir)
    kind = (req.kind or "video").strip().lower()
    if kind not in {"video", "chat"}:
        raise HTTPException(status_code=400, detail=f"不支持的文件选择类型：{req.kind}")
    init_dir = req.initial_dir if req.initial_dir and Path(req.initial_dir).is_dir() else str(_PROJECT_ROOT)
    filetypes = (
        [
            ("Chat Logs (JSON / JSONL / XML)", "*.json *.jsonl *.xml"),
            ("All Files", "*.*"),
        ]
        if kind == "chat"
        else [
            ("视频文件", "*.mp4 *.mkv *.mov *.avi *.flv *.ts *.webm *.m4v"),
            ("所有文件", "*.*"),
        ]
    )
    try:
        from tkinter import filedialog

        chosen = _run_tk_file_dialog(
            lambda root: filedialog.askopenfilename(
                parent=root,
                title=req.title,
                initialdir=init_dir,
                filetypes=filetypes,
            )
        )
        if chosen:
            _authorize_user_file(chosen)
        return {"path": chosen or ""}
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"无法打开系统文件选择框：{exc}") from exc


@app.post("/api/open-folder")
def open_folder(req: OpenFolderReq) -> dict[str, Any]:
    """在系统文件管理器中安全打开指定本地文件夹（无论存在与否均强制工作区边界校验）。"""
    cfg = _read_workbench_config()
    raw_path = (req.path or "").strip()
    if not raw_path:
        raw_path = cfg.get("output_dir") or str(_PROJECT_ROOT / "output")
    _reject_unsafe_path(raw_path)

    target = Path(raw_path)
    if target.is_file():
        target = target.parent

    allowed_roots = _get_allowed_read_roots(cfg)
    if _has_symlink_escape(target, allowed_roots):
        raise HTTPException(
            status_code=403,
            detail="安全限制：仅允许打开已配置工作区（input_dir / output_dir / 项目目录）内的文件夹",
        )

    if not target.exists():
        target.mkdir(parents=True, exist_ok=True)

    resolved_target = target.resolve()
    if not resolved_target.is_dir():
        raise HTTPException(status_code=400, detail="安全限制：仅允许打开文件夹目录")

    if sys.platform == "win32":
        # 显式使用 explorer.exe 打开目录而非裸 os.startfile，从根源杜绝意外执行非目录文件
        subprocess.Popen(["explorer.exe", str(resolved_target)])
    return {"ok": True, "path": str(resolved_target)}


@app.get("/api/videos")
def list_videos(
    input_dir: str | None = Query(default=None),
    output_dir: str | None = Query(default=None),
) -> dict[str, Any]:
    """扫描输入目录的全部视频并统计各自的高光分析进度。"""
    cfg = _read_workbench_config()
    in_dir = _resolve_allowed_input_dir(input_dir, cfg=cfg)
    out_dir = _resolve_allowed_output_dir(output_dir, cfg=cfg, create=False)
    items = scan_input_videos(in_dir, out_dir)
    return {
        "input_dir": str(in_dir),
        "output_dir": str(out_dir),
        "count": len(items),
        "videos": items,
    }


@app.get("/api/video-stream")
def stream_video(request: Request, path: str = Query(...)) -> Any:
    """支持 HTTP 206 Range 断点快进的视频流接口，秒开数 GB 直播录屏。"""
    video_file = _resolve_allowed_read_path(path, SUPPORTED_VIDEO_EXTS, bad_ext_status=404)

    file_size = video_file.stat().st_size
    if file_size <= 0:
        raise HTTPException(status_code=404, detail="视频文件为空或尚未写入完成")

    ext = video_file.suffix.lower()
    content_type, _ = mimetypes.guess_type(str(video_file))
    media_type = _VIDEO_MIME_TYPES.get(ext) or content_type or "video/mp4"
    range_header = request.headers.get("range")

    if not range_header:
        return FileResponse(path=str(video_file), media_type=media_type)

    # 解析形如 "bytes=1024-2047"、"bytes=1024-" 或后缀 "bytes=-1024"
    try:
        units, _, range_spec = range_header.strip().partition("=")
        if units.lower() != "bytes":
            raise ValueError("Unsupported unit")
        # 若浏览器发送多段 Range，安全取首段
        first_spec = range_spec.split(",", 1)[0].strip()
        start_s, _, end_s = first_spec.partition("-")
        if not start_s and end_s:
            suffix_len = int(end_s)
            if suffix_len <= 0:
                raise HTTPException(status_code=416, detail="Range 超出文件大小")
            start = max(0, file_size - suffix_len)
            end = file_size - 1
        else:
            start = int(start_s) if start_s else 0
            end = int(end_s) if end_s else file_size - 1
            end = min(end, file_size - 1)
        if start < 0 or start > end or start >= file_size:
            raise HTTPException(status_code=416, detail="Range 超出文件大小")
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=400, detail="无效的 Range 请求头") from exc

    chunk_len = end - start + 1

    def _file_iterator(file_path: Path, offset: int, length: int, block_size: int = 1024 * 512):
        remaining = length
        with file_path.open("rb") as f:
            f.seek(offset)
            while remaining > 0:
                to_read = min(block_size, remaining)
                data = f.read(to_read)
                if not data:
                    break
                remaining -= len(data)
                yield data

    headers = {
        "Content-Range": f"bytes {start}-{end}/{file_size}",
        "Accept-Ranges": "bytes",
        "Content-Length": str(chunk_len),
    }
    return StreamingResponse(
        _file_iterator(video_file, start, chunk_len),
        status_code=206,
        media_type=media_type,
        headers=headers,
    )


def _sanitize_review_flags(raw_flags: Any) -> list[str]:
    """安全清洗高光复核提醒标签列表（去重保序、剔除空白与控制字符）。"""
    if not isinstance(raw_flags, list):
        return []
    cleaned: list[str] = []
    for item in raw_flags:
        if not isinstance(item, str):
            continue
        val = "".join(ch for ch in item.strip() if ord(ch) >= 32 and ord(ch) != 127)[:48]
        if val and val not in cleaned:
            cleaned.append(val)
    return cleaned


@app.get("/api/highlights")
def get_highlights(
    stem: str = Query(...),
    output_dir: str | None = Query(default=None),
) -> dict[str, Any]:
    """读取某个视频的 Step 3 高光审片数据及最新切片产出清单。"""
    safe_stem = _validate_safe_stem(stem)
    cfg = _read_workbench_config()
    out_dir = _resolve_allowed_output_dir(output_dir, cfg=cfg, create=False)
    hl_path = out_dir / f"{safe_stem}_highlights.json"
    if not _is_path_within(hl_path, out_dir) or not hl_path.is_file():
        raise HTTPException(status_code=404, detail=f"尚未生成 {safe_stem} 的高光粗筛结果")

    data = json.loads(hl_path.read_text(encoding="utf-8"))
    hl_list = data.get("highlights") if isinstance(data.get("highlights"), list) else []
    reviewed_count = 0
    for item in hl_list:
        if not isinstance(item, dict):
            continue
        st = normalize_review_status(item.get("review_status"))
        item["review_status"] = st
        if isinstance(item.get("review_flags"), list):
            item["review_flags"] = _sanitize_review_flags(item.get("review_flags"))
        else:
            item["review_flags"] = compute_review_flags(
                item,
                source_language=data.get("source_language"),
                target_language=data.get("target_language"),
            )
        if st == "reviewed":
            reviewed_count += 1
    data["reviewed_count"] = reviewed_count
    data["draft_count"] = len(hl_list) - reviewed_count

    manifest_path = out_dir / f"{safe_stem}_clips" / f"{safe_stem}_clips_manifest.json"
    clips_manifest = None
    if manifest_path.is_file():
        try:
            clips_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except Exception:
            clips_manifest = None

    data["clips_manifest"] = clips_manifest
    return data


@app.get("/api/chapters")
def api_get_video_chapters(video_path: str = Query(...)) -> dict[str, Any]:
    """静默提取视频内嵌 OBS 章节标记（严格受控于工作区与授权白名单边界）。"""
    target = _resolve_allowed_read_path(video_path, SUPPORTED_VIDEO_EXTS, bad_ext_status=404)

    chapters = extract_video_chapters(target)
    return {
        "video_path": str(target.resolve()),
        "count": len(chapters),
        "chapters": chapters,
    }


@app.get("/api/transcript")
def api_get_transcript(
    stem: str = Query(...),
    output_dir: str | None = Query(default=None),
    q: str | None = Query(default=None, max_length=200),
    around_sec: float | None = Query(default=None, ge=0.0, allow_inf_nan=False),
    window_sec: float = Query(default=30.0, ge=1.0, le=600.0, allow_inf_nan=False),
) -> dict[str, Any]:
    """读取全片逐字稿、关键词检索或 ±30s 上下文句段核查（无逐字稿时返回 exists=False 不刷 404）。"""
    safe_stem = _validate_safe_stem(stem)
    if around_sec is not None:
        around_sec = _require_finite_float(around_sec, "around_sec")
    window_sec = _require_finite_float(window_sec, "window_sec")

    cfg = _read_workbench_config()
    out_dir = _resolve_allowed_output_dir(output_dir, cfg=cfg, create=False)
    tr_path = out_dir / f"{safe_stem}_transcript.json"
    if not _is_path_within(tr_path, out_dir):
        raise HTTPException(status_code=400, detail="安全限制：非法的逐字稿路径")

    if not tr_path.is_file():
        return {
            "exists": False,
            "stem": safe_stem,
            "language": "",
            "total_duration_sec": 0.0,
            "total_count": 0,
            "matched_count": 0,
            "truncated": False,
            "segments": [],
        }

    try:
        tr_data = json.loads(tr_path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"逐字稿 JSON 读取失败：{exc}") from exc

    raw_segs = tr_data.get("segments") if isinstance(tr_data.get("segments"), list) else []
    light_segs: list[dict[str, Any]] = []
    for idx, seg in enumerate(raw_segs, start=1):
        if not isinstance(seg, dict):
            continue
        text = str(seg.get("text") or "").strip()
        if not text:
            continue
        try:
            st = max(0.0, float(seg.get("start_sec", seg.get("start", 0.0))))
            ed = max(st, float(seg.get("end_sec", seg.get("end", st))))
        except (TypeError, ValueError):
            continue
        if not (math.isfinite(st) and math.isfinite(ed)):
            continue
        light_segs.append(
            {
                "id": int(seg.get("id", idx)),
                "start": round(st, 2),
                "end": round(ed, 2),
                "text": text,
            }
        )

    filtered = light_segs
    if around_sec is not None:
        win_start = max(0.0, float(around_sec) - float(window_sec))
        win_end = float(around_sec) + float(window_sec)
        filtered = [s for s in filtered if s["end"] >= win_start and s["start"] <= win_end]

    query_clean = (q or "").strip()
    if query_clean:
        tokens = [tok.lower() for tok in query_clean.split() if tok.strip()][:10]
        if tokens:
            filtered = [
                s for s in filtered if all(tok in s["text"].lower() for tok in tokens)
            ]

    matched_count = len(filtered)
    truncated = matched_count > _MAX_TRANSCRIPT_SEGMENTS
    return {
        "exists": True,
        "stem": safe_stem,
        "language": str(tr_data.get("language") or ""),
        "total_duration_sec": float(tr_data.get("total_duration_sec") or 0.0),
        "total_count": len(light_segs),
        "matched_count": matched_count,
        "truncated": truncated,
        "segments": filtered[:_MAX_TRANSCRIPT_SEGMENTS],
    }


@app.post("/api/chat/import")
def api_import_chat_radar(req: ChatImportReq) -> dict[str, Any]:
    """导入 Twitch / B站 / YouTube 本地弹幕文件：原始弹幕拆离存 SQLite，轻量 JSON 仅存曲线与爆点。"""
    safe_stem = _validate_safe_stem(req.stem)
    bucket_sec = _require_finite_float(req.bucket_sec, "bucket_sec")
    cfg = _read_workbench_config()
    chat_file = _resolve_allowed_read_path(
        req.chat_path,
        _SUPPORTED_CHAT_EXTS,
        cfg=cfg,
        bad_ext_status=400,
    )
    out_dir = _resolve_allowed_output_dir(req.output_dir, cfg=cfg, create=True)
    radar_path = out_dir / f"{safe_stem}_chat_radar.json"
    db_path = out_dir / f"{safe_stem}_chat_messages.db"
    if not _is_path_within(radar_path, out_dir) or not _is_path_within(db_path, out_dir):
        raise HTTPException(status_code=400, detail="安全限制：非法的弹幕雷达输出路径")

    try:
        parsed = parse_chat_file(chat_file)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    peaks_path = out_dir / f"{safe_stem}_audio_peaks.json"
    audio_peaks: list[dict[str, Any]] = []
    duration_hint: float | None = None
    if peaks_path.is_file() and _is_path_within(peaks_path, out_dir):
        try:
            peaks_data = json.loads(peaks_path.read_text(encoding="utf-8"))
            if isinstance(peaks_data, dict):
                raw_peaks = peaks_data.get("peak_segments")
                if isinstance(raw_peaks, list):
                    audio_peaks = raw_peaks
                dur_val = float(peaks_data.get("total_duration_sec") or 0.0)
                if math.isfinite(dur_val) and dur_val > 0:
                    duration_hint = dur_val
        except Exception:
            pass

    spikes = analyze_chat_spikes(
        parsed["messages"],
        duration_sec=duration_hint,
        bucket_sec=bucket_sec,
    )
    fusion_peaks = (
        fuse_audio_and_chat_peaks(audio_peaks, spikes["chat_peaks"])
        if audio_peaks
        else []
    )

    # 拆离存储 + 按 stem 加互斥锁 + 原子替换：
    # 原始弹幕写入 SQLite FTS5 库；*_chat_radar.json 彻底不再保存 messages 巨无霸数组，防止百兆 JSON OOM
    saved_payload: dict[str, Any] = {
        "stem": safe_stem,
        "source_filename": chat_file.name,
        "format": parsed["format"],
        "message_count": parsed["message_count"],
        "duration_sec": spikes["duration_sec"],
        "bucket_sec": spikes["bucket_sec"],
        "total_hype_messages": spikes["total_hype_messages"],
        "baseline_median": spikes["baseline_median"],
        "baseline_mad": spikes["baseline_mad"],
        "messages_db": db_path.name,
        "buckets": spikes["buckets"],
        "chat_peaks": spikes["chat_peaks"],
        "fusion_peaks": fusion_peaks,
    }
    with _get_chat_stem_lock(out_dir, safe_stem):
        save_chat_messages_db(db_path, parsed["messages"])
        atomic_write_json(radar_path, saved_payload, indent=None)

    return {
        "ok": True,
        "exists": True,
        "stem": safe_stem,
        "source_filename": chat_file.name,
        "format": parsed["format"],
        "message_count": parsed["message_count"],
        "duration_sec": spikes["duration_sec"],
        "bucket_sec": spikes["bucket_sec"],
        "total_hype_messages": spikes["total_hype_messages"],
        "buckets": spikes["buckets"],
        "chat_peaks": spikes["chat_peaks"],
        "fusion_peaks": fusion_peaks,
    }


@app.get("/api/chat/heatmap")
def api_get_chat_heatmap(
    stem: str = Query(...),
    output_dir: str | None = Query(default=None),
    q: str | None = Query(default=None, max_length=200),
) -> dict[str, Any]:
    """读取已导入弹幕的热力图曲线、爆点与基于 SQLite FTS5 的低内存梗词搜索结果。"""
    safe_stem = _validate_safe_stem(stem)
    cfg = _read_workbench_config()
    out_dir = _resolve_allowed_output_dir(output_dir, cfg=cfg, create=False)
    radar_path = out_dir / f"{safe_stem}_chat_radar.json"
    db_path = out_dir / f"{safe_stem}_chat_messages.db"
    if not _is_path_within(radar_path, out_dir):
        raise HTTPException(status_code=400, detail="安全限制：非法的弹幕雷达路径")

    if not radar_path.is_file():
        return {
            "exists": False,
            "stem": safe_stem,
            "buckets": [],
            "chat_peaks": [],
            "fusion_peaks": [],
            "search": None,
        }

    try:
        saved = json.loads(radar_path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"弹幕雷达缓存读取失败：{exc}") from exc

    search_res = None
    query_clean = (q or "").strip()
    if query_clean:
        try:
            if db_path.is_file() and _is_path_within(db_path, out_dir):
                search_res = search_chat_messages_db(db_path, query_clean, max_matches=300)
            else:
                # 平滑兼容历史上曾内嵌 messages 数组的旧缓存
                raw_search = search_chat_keywords(saved.get("messages") or [], query_clean)
                matches = raw_search.get("matches") or []
                search_res = {
                    **raw_search,
                    "matches": matches[:300],
                    "matches_truncated": len(matches) > 300,
                }
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    return {
        "exists": True,
        "stem": safe_stem,
        "source_filename": saved.get("source_filename", ""),
        "format": saved.get("format", ""),
        "message_count": int(saved.get("message_count", 0) or 0),
        "duration_sec": float(saved.get("duration_sec", 0.0) or 0.0),
        "bucket_sec": float(saved.get("bucket_sec", 5.0) or 5.0),
        "total_hype_messages": int(saved.get("total_hype_messages", 0) or 0),
        "buckets": saved.get("buckets") or [],
        "chat_peaks": saved.get("chat_peaks") or [],
        "fusion_peaks": saved.get("fusion_peaks") or [],
        "search": search_res,
    }


@app.get("/api/audio-streams")
def list_video_audio_streams(path: str = Query(...)) -> dict[str, Any]:
    """探测指定视频文件的全部音频轨道，支持 OBS 多音轨人声麦轨自动推荐。"""
    video_file = _resolve_allowed_read_path(path, SUPPORTED_VIDEO_EXTS, bad_ext_status=404)

    try:
        streams = probe_audio_streams(video_file)
    except RuntimeError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    recommended = 0
    for st in streams:
        if st.get("is_likely_mic"):
            recommended = int(st.get("audio_index", 0))
            break

    return {
        "path": str(video_file.resolve()),
        "count": len(streams),
        "recommended_stream": recommended,
        "streams": streams,
    }


@app.get("/api/diagnostics")
def get_video_diagnostics(
    stem: str = Query(...),
    output_dir: str | None = Query(default=None),
) -> dict[str, Any]:
    """获取指定视频的 Step 1 直播状态评级、分小时热度与智能参数推荐。"""
    safe_stem = _validate_safe_stem(stem)
    cfg = _read_workbench_config()
    out_dir = _resolve_allowed_output_dir(output_dir, cfg=cfg, create=False)
    peaks_path = out_dir / f"{safe_stem}_audio_peaks.json"
    if not _is_path_within(peaks_path, out_dir) or not peaks_path.is_file():
        raise HTTPException(status_code=404, detail=f"尚未生成 {safe_stem} 的音频分析文件")

    peaks_data = json.loads(peaks_path.read_text(encoding="utf-8"))
    diag = get_or_compute_diagnostics(peaks_data)
    dead_air_segs = peaks_data.get("dead_air_segments")
    if not isinstance(dead_air_segs, list):
        dead_air_segs = []
    dead_air_total = float(
        peaks_data.get(
            "dead_air_total_sec",
            sum(float(seg.get("duration_sec", 0.0) or 0.0) for seg in dead_air_segs if isinstance(seg, dict)),
        )
        or 0.0
    )
    dead_air_count = int(peaks_data.get("dead_air_count", len(dead_air_segs)) or 0)
    return {
        "stem": safe_stem,
        "total_duration_sec": peaks_data.get("total_duration_sec", 0.0),
        "total_duration_str": peaks_data.get("total_duration_str", "-"),
        "rms_threshold": peaks_data.get("rms_threshold", 0.0),
        "segment_count": peaks_data.get("segment_count", 0),
        "audio_stream": int(peaks_data.get("audio_stream", 0)),
        "vocal_bandpass": peaks_data.get("vocal_bandpass"),
        "diagnostics": diag,
        "peak_segments": peaks_data.get("peak_segments", []),
        "dead_air_segments": dead_air_segs,
        "dead_air_total_sec": round(dead_air_total, 2),
        "dead_air_count": dead_air_count,
    }


@app.get("/api/media-info")
def api_media_info(path: str = Query(...)) -> dict[str, Any]:
    """获取视频分辨率、真实 FPS、编码与总时长元数据（带文件指纹内存缓存）。"""
    video_file = _resolve_allowed_read_path(path, SUPPORTED_VIDEO_EXTS, bad_ext_status=404)

    stat = video_file.stat()
    cache_key = (str(video_file.resolve()), stat.st_mtime_ns, stat.st_size)
    with _MEDIA_INFO_LOCK:
        cached = _MEDIA_INFO_CACHE.get(cache_key)
        if cached is not None:
            return dict(cached)

    try:
        info = probe_media_info(video_file)
    except RuntimeError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    with _MEDIA_INFO_LOCK:
        if len(_MEDIA_INFO_CACHE) > 256:
            _MEDIA_INFO_CACHE.clear()
        _MEDIA_INFO_CACHE[cache_key] = dict(info)
    return info


@app.get("/api/waveform")
def api_audio_waveform(
    stem: str = Query(...),
    output_dir: str | None = Query(default=None),
    start: float = Query(default=0.0, ge=0.0, allow_inf_nan=False),
    end: float | None = Query(default=None, allow_inf_nan=False),
    points: int = Query(default=1200, ge=16, le=4000),
) -> dict[str, Any]:
    """返回指定视频区间的高分辨率音频能量波形数组（优先直读 16k WAV，全片总览自动落盘缓存）。"""
    safe_stem = _validate_safe_stem(stem)
    start = _require_finite_float(start, "start")
    if end is not None:
        end = _require_finite_float(end, "end")

    cfg = _read_workbench_config()
    out_dir = _resolve_allowed_output_dir(output_dir, cfg=cfg, create=False)
    wav_path = out_dir / f"{safe_stem}_16k.wav"
    peaks_path = out_dir / f"{safe_stem}_audio_peaks.json"
    if not _is_path_within(wav_path, out_dir) or not _is_path_within(peaks_path, out_dir):
        raise HTTPException(status_code=400, detail="安全限制：非法的波形路径")

    rms_threshold = 0.0
    if peaks_path.is_file():
        try:
            peaks_meta = json.loads(peaks_path.read_text(encoding="utf-8"))
            rms_threshold = float(peaks_meta.get("rms_threshold", 0.0))
        except Exception:
            peaks_meta = {}
    else:
        peaks_meta = {}

    if wav_path.is_file():
        is_overview = start <= 0.01 and (end is None or end <= 0.0) and points == 1200
        overview_cache = out_dir / f"{safe_stem}_waveform_overview.json"
        wav_stat = wav_path.stat()

        if is_overview and overview_cache.is_file():
            try:
                cached = json.loads(overview_cache.read_text(encoding="utf-8"))
                if (
                    cached.get("wav_mtime_ns") == wav_stat.st_mtime_ns
                    and cached.get("wav_size") == wav_stat.st_size
                    and cached.get("points") == points
                ):
                    cached["rms_threshold"] = rms_threshold
                    return cached
            except Exception:
                pass

        try:
            wf = sample_wav_waveform(wav_path, start_sec=start, end_sec=end, points=points)
        except Exception as exc:
            raise HTTPException(status_code=500, detail=f"采样音频波形失败：{exc}") from exc

        wf["stem"] = safe_stem
        wf["rms_threshold"] = rms_threshold

        if is_overview:
            try:
                to_cache = dict(wf)
                to_cache["wav_mtime_ns"] = wav_stat.st_mtime_ns
                to_cache["wav_size"] = wav_stat.st_size
                atomic_write_json(overview_cache, to_cache, indent=None)
            except Exception:
                pass
        return wf

    # 若 WAV 尚未提取或已清理，但 audio_peaks.json 存在，则平滑降级由 peak_segments 构造能量包络
    if peaks_meta:
        total_dur = max(1.0, float(peaks_meta.get("total_duration_sec", 0.0)))
        s_sec = max(0.0, min(total_dur, float(start)))
        e_sec = total_dur if end is None or end <= 0 else max(s_sec + 0.1, min(total_dur, float(end)))
        span = max(0.1, e_sec - s_sec)
        n = max(16, min(4000, int(points)))
        rms_arr = [0.02] * n
        peak_arr = [0.03] * n
        for seg in peaks_meta.get("peak_segments", []):
            seg_s = float(seg.get("start_sec", 0.0))
            seg_e = float(seg.get("end_sec", seg_s + 1.0))
            if seg_e < s_sec or seg_s > e_sec:
                continue
            val = min(1.0, max(0.05, float(seg.get("peak_rms", 0.1))))
            i0 = max(0, int(((seg_s - s_sec) / span) * n))
            i1 = min(n - 1, int(((seg_e - s_sec) / span) * n))
            for idx in range(i0, i1 + 1):
                if val > peak_arr[idx]:
                    peak_arr[idx] = round(val, 4)
                    rms_arr[idx] = round(val * 0.72, 4)
        return {
            "stem": safe_stem,
            "source": "peaks_fallback",
            "start_sec": round(s_sec, 3),
            "end_sec": round(e_sec, 3),
            "total_duration_sec": round(total_dur, 3),
            "points": n,
            "rms": rms_arr,
            "peak": peak_arr,
            "rms_threshold": rms_threshold,
        }

    raise HTTPException(status_code=404, detail=f"尚未找到 {safe_stem} 的音频 WAV 或峰值分析数据")


@app.get("/api/thumbnail")
def api_video_thumbnail(
    path: str = Query(...),
    time_sec: float | None = Query(default=None, alias="time", ge=0.0, allow_inf_nan=False),
    sec: float | None = Query(default=None, ge=0.0, allow_inf_nan=False),
    w: int | None = Query(default=None, ge=80, le=960),
    width: int | None = Query(default=None, ge=80, le=960),
    output_dir: str | None = Query(default=None),
) -> FileResponse:
    """极速抽取并缓存视频指定时间点的缩略图 JPEG（强制写入受控 cfg['output_dir']/.thumb_cache，忽略外部 output_dir）。"""
    del output_dir  # 安全铁律：彻底忽略外部传入的 output_dir，禁止向任意目录创建 .thumb_cache
    cfg = _read_workbench_config()
    video_file = _resolve_allowed_read_path(path, SUPPORTED_VIDEO_EXTS, cfg=cfg, bad_ext_status=404)

    effective_time = time_sec if time_sec is not None else (sec if sec is not None else 10.0)
    effective_time = _require_finite_float(effective_time, "time")
    effective_width = w if w is not None else (width if width is not None else 320)

    out_dir = Path(cfg["output_dir"]).resolve()
    cache_dir = out_dir / ".thumb_cache"
    try:
        jpg_path = extract_video_thumbnail(
            video_file, cache_dir=cache_dir, time_sec=effective_time, width=effective_width
        )
    except RuntimeError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    return FileResponse(
        path=str(jpg_path),
        media_type="image/jpeg",
        headers={"Cache-Control": "public, max-age=86400"},
    )


@app.put("/api/highlights")
def save_highlights(req: HighlightsUpdateReq) -> dict[str, Any]:
    """保存人工审片后的勾选状态、微调起止时间、标题与双语字幕，并同步刷新 review.md。"""
    hl_path = _validate_highlights_json_path(req.highlights_json_path)
    safe_stem = _validate_safe_stem(hl_path.stem.removesuffix("_highlights"))

    with get_highlights_file_lock(hl_path):
        data = json.loads(hl_path.read_text(encoding="utf-8"))
        total_dur = float(data.get("total_duration_sec", 0.0) or 0.0)
        hl_src_lang = str(data.get("source_language") or "en").strip().lower()
        hl_tgt_lang = str(data.get("target_language") or ("en" if hl_src_lang == "zh" else "zh")).strip().lower()
        is_same_lang = hl_src_lang == hl_tgt_lang
        is_zh_src = hl_src_lang == "zh"
        is_zh_en = hl_src_lang == "zh" and hl_tgt_lang == "en"

        sanitized: list[dict[str, Any]] = []
        seen_ranks: set[int] = set()
        for idx, item in enumerate(req.highlights, start=1):
            if not isinstance(item, dict):
                raise HTTPException(status_code=400, detail=f"第 {idx} 条高光片段必须是对象")

            s_sec = max(0.0, _require_finite_float(item.get("start_sec", 0.0), f"highlights[{idx}].start_sec"))
            e_sec = _require_finite_float(item.get("end_sec", 0.0), f"highlights[{idx}].end_sec")

            if total_dur > 0:
                e_sec = min(total_dur, e_sec)
            if e_sec <= s_sec:
                raise HTTPException(
                    status_code=400,
                    detail=f"第 {idx} 条结束时间（{e_sec}s）必须大于开始时间（{s_sec}s）",
                )

            # 强制唯一正整数 rank（拒绝 bool / 浮点数 / <=0 / 重复值）
            raw_rank = item.get("rank", idx)
            if isinstance(raw_rank, bool) or isinstance(raw_rank, float):
                raise HTTPException(status_code=400, detail=f"第 {idx} 条 rank 必须为唯一正整数")
            try:
                rank_int = int(raw_rank)
            except (TypeError, ValueError) as exc:
                raise HTTPException(status_code=400, detail=f"第 {idx} 条 rank 必须为唯一正整数") from exc
            if rank_int <= 0 or rank_int in seen_ranks:
                raise HTTPException(status_code=400, detail=f"第 {idx} 条 rank={rank_int} 非法或重复，必须为唯一正整数")
            seen_ranks.add(rank_int)

            updated = dict(item)
            updated["rank"] = rank_int
            updated["selected"] = bool(item.get("selected", True))
            updated["start_sec"] = round(s_sec, 2)
            updated["end_sec"] = round(e_sec, 2)
            updated["duration_sec"] = round(e_sec - s_sec, 2)
            updated["start_str"] = format_timestamp(s_sec)
            updated["end_str"] = format_timestamp(e_sec)

            # 兼容手动片段无 AI 评分（score=None）；有显式值时强制校验有限数值
            if "score" in item:
                raw_score = item.get("score")
                if raw_score is None or (isinstance(raw_score, str) and not raw_score.strip()):
                    updated["score"] = None
                else:
                    updated["score"] = round(_require_finite_float(raw_score, f"highlights[{idx}].score"), 1)

            if "subtitles" in item and item.get("subtitles") is not None:
                raw_subs = item.get("subtitles")
                if not isinstance(raw_subs, list):
                    raise HTTPException(status_code=400, detail=f"第 {idx} 条 subtitles 必须是数组")
                updated["subtitles"] = _sanitize_client_subtitles(
                    raw_subs,
                    clip_start_sec=s_sec,
                    clip_end_sec=e_sec,
                    is_same_lang=is_same_lang,
                    is_zh_src=is_zh_src,
                )

            t_tgt = str(updated.get("title_tgt") or "").strip()
            t_src = str(updated.get("title_src") or "").strip()
            t_zh = str(updated.get("title_zh") or "").strip()
            t_en = str(updated.get("title_en") or "").strip()
            if is_zh_en:
                updated["title_tgt"] = t_tgt or t_en or t_zh
                updated["title_src"] = t_src or t_zh or t_en
                if not t_en and updated["title_tgt"]:
                    updated["title_en"] = updated["title_tgt"]
                if not t_zh and updated["title_src"]:
                    updated["title_zh"] = updated["title_src"]
            else:
                updated["title_tgt"] = t_tgt or t_zh or t_en
                updated["title_src"] = t_src or t_en or t_zh
                if not t_zh and updated["title_tgt"]:
                    updated["title_zh"] = updated["title_tgt"]
                if not t_en and updated["title_src"]:
                    updated["title_en"] = updated["title_src"]
            updated["review_status"] = normalize_review_status(item.get("review_status"))
            if "review_flags" in item and isinstance(item.get("review_flags"), list):
                updated["review_flags"] = _sanitize_review_flags(item.get("review_flags"))
            else:
                updated["review_flags"] = compute_review_flags(
                    updated,
                    source_language=hl_src_lang,
                    target_language=hl_tgt_lang,
                )
            sanitized.append(updated)

        reviewed_count = sum(1 for item in sanitized if item.get("review_status") == "reviewed")
        draft_count = len(sanitized) - reviewed_count
        review_md_path = hl_path.with_name(f"{safe_stem}_review.md")

        data["highlights"] = sanitized
        data["highlight_count"] = len(sanitized)
        data["reviewed_count"] = reviewed_count
        data["draft_count"] = draft_count
        # 先更新 review_md_path 再原子写盘，保证磁盘 JSON 与导出 Markdown 路径完全一致
        data["review_md_path"] = str(review_md_path.resolve())
        atomic_write_json(hl_path, data, indent=2)

        transcript_segments: list[dict[str, Any]] = []
        tr_raw = str(data.get("transcript_json_path") or "").strip()
        if tr_raw:
            try:
                _reject_unsafe_path(tr_raw)
                tr_path = Path(tr_raw)
                if tr_path.is_file() and _is_path_within(tr_path, hl_path.parent):
                    transcript_segments = json.loads(tr_path.read_text(encoding="utf-8")).get("segments", [])
            except Exception:
                transcript_segments = []

        export_review_markdown(data, review_md_path, transcript_segments=transcript_segments)

    return {
        "ok": True,
        "selected_count": sum(1 for item in sanitized if item["selected"]),
        "reviewed_count": reviewed_count,
        "draft_count": draft_count,
        "highlights_json_path": str(hl_path.resolve()),
        "review_md_path": str(review_md_path.resolve()),
        "highlights": sanitized,
    }


@app.post("/api/highlights/create-manual")
def create_manual_highlights(req: ManualHighlightsCreateReq) -> dict[str, Any]:
    """从逐字稿、OBS 章节或弹幕爆点一键手动新建高光片段（无需先跑 AI 粗筛，默认标为 reviewed 且 score=None）。"""
    cfg = _read_workbench_config()
    video_file = _resolve_allowed_read_path(
        req.video_path,
        SUPPORTED_VIDEO_EXTS,
        cfg=cfg,
        bad_ext_status=404,
    )
    safe_stem = _validate_safe_stem(video_file.stem)
    out_dir = _resolve_allowed_output_dir(req.output_dir, cfg=cfg, create=True)
    hl_path = out_dir / f"{safe_stem}_highlights.json"
    tr_path = out_dir / f"{safe_stem}_transcript.json"
    peaks_path = out_dir / f"{safe_stem}_audio_peaks.json"
    review_md_path = out_dir / f"{safe_stem}_review.md"
    if not _is_path_within(hl_path, out_dir):
        raise HTTPException(status_code=400, detail="安全限制：非法的高光方案输出路径")

    transcript_segments: list[dict[str, Any]] = []
    tr_data: dict[str, Any] = {}
    if tr_path.is_file() and _is_path_within(tr_path, out_dir):
        try:
            loaded_tr = json.loads(tr_path.read_text(encoding="utf-8"))
            if isinstance(loaded_tr, dict):
                tr_data = loaded_tr
                raw_tr_segs = loaded_tr.get("segments")
                if isinstance(raw_tr_segs, list):
                    transcript_segments = [s for s in raw_tr_segs if isinstance(s, dict)]
        except Exception:
            tr_data = {}

    with get_highlights_file_lock(hl_path):
        if hl_path.is_file():
            data = json.loads(hl_path.read_text(encoding="utf-8"))
            existing_hls = data.get("highlights") if isinstance(data.get("highlights"), list) else []
        else:
            total_dur = float(tr_data.get("total_duration_sec") or 0.0)
            if total_dur <= 0 and peaks_path.is_file():
                try:
                    p_data = json.loads(peaks_path.read_text(encoding="utf-8"))
                    total_dur = float(p_data.get("total_duration_sec") or 0.0)
                except Exception:
                    total_dur = 0.0
            if total_dur <= 0:
                try:
                    info = probe_media_info(video_file)
                    total_dur = float(info.get("duration_sec") or 0.0)
                except Exception:
                    total_dur = 0.0

            src_lang = str(tr_data.get("language") or "en").strip().lower()
            if src_lang not in _SUPPORTED_SUBTITLE_LANGS:
                src_lang = "en"
            tgt_lang = "en" if src_lang == "zh" else "zh"

            data = {
                "video_path": str(video_file.resolve()),
                "transcript_json_path": str(tr_path.resolve()) if tr_path.is_file() else "",
                "highlights_json_path": str(hl_path.resolve()),
                "review_md_path": str(review_md_path.resolve()),
                "model": "manual",
                "source_language": src_lang,
                "target_language": tgt_lang,
                "subtitle_pair": f"{src_lang}->{tgt_lang}",
                "bilingual_subtitles": False,
                "total_duration_sec": round(total_dur, 2),
                "total_duration_str": format_timestamp(total_dur) if total_dur > 0 else "-",
                "selection_plan": {
                    "duration_tier": "manual",
                    "target_count": 0,
                    "min_clip_sec": 5,
                    "max_clip_sec": 300,
                },
                "elapsed_sec": 0.0,
                "highlight_count": 0,
                "highlights": [],
            }
            existing_hls = data["highlights"]

        total_dur = float(data.get("total_duration_sec") or 0.0)
        hl_src_lang = str(data.get("source_language") or "en").strip().lower()
        hl_tgt_lang = str(data.get("target_language") or ("en" if hl_src_lang == "zh" else "zh")).strip().lower()
        is_same_lang = hl_src_lang == hl_tgt_lang
        is_zh_src = hl_src_lang == "zh"
        is_zh_en = hl_src_lang == "zh" and hl_tgt_lang == "en"

        max_rank = max((int(h.get("rank", 0) or 0) for h in existing_hls if isinstance(h, dict)), default=0)
        added_ranks: list[int] = []
        for idx, raw_item in enumerate(req.items, start=1):
            if not isinstance(raw_item, dict):
                raise HTTPException(status_code=400, detail=f"第 {idx} 条手动片段必须是对象")
            s_sec = max(0.0, _require_finite_float(raw_item.get("start_sec", 0.0), f"items[{idx}].start_sec"))
            e_sec = _require_finite_float(raw_item.get("end_sec", 0.0), f"items[{idx}].end_sec")
            if total_dur > 0:
                e_sec = min(total_dur, e_sec)
            if e_sec <= s_sec:
                e_sec = s_sec + 1.0
                if total_dur > 0 and e_sec > total_dur:
                    s_sec = max(0.0, total_dur - 1.0)
                    e_sec = total_dur
                if e_sec <= s_sec:
                    raise HTTPException(status_code=400, detail=f"第 {idx} 条手动片段结束时间必须大于开始时间")

            max_rank += 1
            title_base = (
                str(
                    raw_item.get("title_tgt")
                    or raw_item.get("title_zh")
                    or raw_item.get("title_src")
                    or raw_item.get("title_en")
                    or ""
                ).strip()
                or f"Clip #{max_rank}"
            )
            title_src_val = str(raw_item.get("title_src") or raw_item.get("title_en") or title_base).strip()
            title_tgt_val = str(raw_item.get("title_tgt") or raw_item.get("title_zh") or title_base).strip()

            raw_subs_field = raw_item.get("subtitles")
            if raw_subs_field is not None:
                if not isinstance(raw_subs_field, list):
                    raise HTTPException(status_code=400, detail=f"第 {idx} 条手动片段 subtitles 必须是数组")
                raw_subs = _sanitize_client_subtitles(
                    raw_subs_field,
                    clip_start_sec=s_sec,
                    clip_end_sec=e_sec,
                    is_same_lang=is_same_lang,
                    is_zh_src=is_zh_src,
                )
            elif transcript_segments:
                extracted_subs: list[dict[str, Any]] = []
                for seg in transcript_segments:
                    try:
                        ss = float(seg.get("start_sec", seg.get("start", 0.0)))
                        se = float(seg.get("end_sec", seg.get("end", ss)))
                    except (TypeError, ValueError):
                        continue
                    if not (math.isfinite(ss) and math.isfinite(se)):
                        continue
                    if se < s_sec or ss > e_sec:
                        continue
                    txt = _CTRL_CHARS_RE.sub(" ", str(seg.get("text") or "")).strip()[:500]
                    if not txt:
                        continue
                    cs = round(max(s_sec, ss), 2)
                    ce = round(min(e_sec, se), 2)
                    if ce <= cs:
                        continue
                    # 跨语种自动截取仅填源语字幕，严禁把未翻译原片台词错误填入目标语/text_zh！
                    if is_same_lang:
                        sub_entry = {
                            "start_sec": cs,
                            "end_sec": ce,
                            "text_orig": txt,
                            "text_src": txt,
                            "text_tgt": txt,
                            "text_en": txt,
                            "text_zh": txt,
                        }
                    else:
                        sub_entry = {
                            "start_sec": cs,
                            "end_sec": ce,
                            "text_orig": txt,
                            "text_src": txt,
                            "text_tgt": "",
                            "text_en": "" if is_zh_src else txt,
                            "text_zh": txt if is_zh_src else "",
                        }
                    extracted_subs.append(sub_entry)
                raw_subs = extracted_subs
            else:
                raw_subs = []

            raw_tags = raw_item.get("tags") if isinstance(raw_item.get("tags"), list) else []
            clean_tags = [str(tg).strip()[:32] for tg in raw_tags if str(tg).strip()][:8]
            if not clean_tags:
                source_tag = str(raw_item.get("source") or "manual").strip()[:24] or "manual"
                clean_tags = [source_tag]

            # 手动新建片段默认 score = None（绝不伪造 8.5 AI 评分）
            raw_score = raw_item.get("score")
            if raw_score is None or (isinstance(raw_score, str) and not raw_score.strip()):
                score_val: float | None = None
            else:
                score_num = _require_finite_float(raw_score, f"items[{idx}].score")
                score_val = round(min(10.0, max(1.0, score_num)), 1)

            new_clip: dict[str, Any] = {
                "rank": max_rank,
                "selected": True,
                "review_status": normalize_review_status(raw_item.get("review_status"), default="reviewed"),
                "start_sec": round(s_sec, 2),
                "end_sec": round(e_sec, 2),
                "duration_sec": round(e_sec - s_sec, 2),
                "start_str": format_timestamp(s_sec),
                "end_str": format_timestamp(e_sec),
                "score": score_val,
                "title_tgt": title_tgt_val,
                "title_src": title_src_val,
                "title_zh": title_src_val if is_zh_en else title_tgt_val,
                "title_en": title_tgt_val if is_zh_en else title_src_val,
                "summary_zh": str(raw_item.get("summary_zh") or "").strip(),
                "reason": str(raw_item.get("reason") or "Manual selection").strip(),
                "tags": clean_tags,
                "subtitles": raw_subs,
            }
            new_clip["review_flags"] = compute_review_flags(
                new_clip,
                source_language=hl_src_lang,
                target_language=hl_tgt_lang,
            )
            existing_hls.append(new_clip)
            added_ranks.append(max_rank)

        if total_dur <= 0 and existing_hls:
            inferred = max(float(h.get("end_sec", 0.0) or 0.0) for h in existing_hls)
            data["total_duration_sec"] = round(inferred, 2)
            data["total_duration_str"] = format_timestamp(inferred)

        reviewed_count = sum(
            1 for item in existing_hls if normalize_review_status(item.get("review_status")) == "reviewed"
        )
        data["highlights"] = existing_hls
        data["highlight_count"] = len(existing_hls)
        data["reviewed_count"] = reviewed_count
        data["draft_count"] = len(existing_hls) - reviewed_count
        data["highlights_json_path"] = str(hl_path.resolve())
        data["review_md_path"] = str(review_md_path.resolve())

        atomic_write_json(hl_path, data, indent=2)
        export_review_markdown(data, review_md_path, transcript_segments=transcript_segments)

    return {
        "ok": True,
        "added_ranks": added_ranks,
        "selected_count": sum(1 for item in existing_hls if item.get("selected", True) is True),
        "reviewed_count": reviewed_count,
        "draft_count": len(existing_hls) - reviewed_count,
        **data,
    }


def _resolve_safe_audio_stream(
    video_path: Path,
    requested_stream: int,
    on_fallback: Any = None,
) -> int:
    """批量任务时安全校对单个视频的音轨数量，若缺失目标音轨则自动回退到音轨 0 避免中断。"""
    target = max(0, int(requested_stream))
    if target == 0:
        return 0
    try:
        streams = probe_audio_streams(video_path)
    except Exception:
        return target
    if streams and target >= len(streams):
        if on_fallback is not None:
            on_fallback(
                "step1",
                2,
                f"[{video_path.stem}] 仅有 {len(streams)} 条音轨，已自动回退使用音轨 #0 避免中断",
            )
        return 0
    return target


@app.post("/api/tasks/analyze")
def start_analyze_task(req: AnalyzeTaskReq) -> dict[str, Any]:
    """提交单个或批量视频粗筛任务到后台队列（Step 1 -> Step 2 -> Step 3）。"""
    cfg = _read_workbench_config()
    out_dir = _resolve_allowed_output_dir(req.output_dir, cfg=cfg, create=True)
    _require_finite_float(req.top_percent, "top_percent")
    _require_finite_float(req.vad_threshold, "vad_threshold")

    valid_src_lang = _validate_optional_lang_code(req.language, "language")
    valid_tgt_lang = _validate_optional_lang_code(req.target_language, "target_language")
    if not req.video_paths:
        raise HTTPException(status_code=400, detail="没有找到有效的视频文件")

    valid_paths: list[Path] = []
    for raw_vp in req.video_paths:
        vp = _resolve_allowed_read_path(
            raw_vp,
            SUPPORTED_VIDEO_EXTS,
            cfg=cfg,
            bad_ext_status=404,
        )
        _validate_safe_stem(vp.stem)
        valid_paths.append(vp)

    title = (
        f"AI 粗筛：{valid_paths[0].name}"
        if len(valid_paths) == 1
        else f"批量 AI 粗筛（共 {len(valid_paths)} 个视频）"
    )

    def _worker(report_cb: Any) -> dict[str, Any]:
        total = len(valid_paths)
        outputs: list[dict[str, Any]] = []
        for idx, vpath in enumerate(valid_paths, start=1):
            base_pct = int(((idx - 1) / total) * 100)
            span_pct = 100.0 / total

            def _scaled_cb(
                stage: str,
                pct: int,
                msg: str,
                _idx: int = idx,
                _base: int = base_pct,
                _span: float = span_pct,
            ) -> None:
                global_pct = min(99, int(_base + (pct / 100.0) * _span))
                report_cb(stage, global_pct, f"[{_idx}/{total}] {msg}")

            safe_stream = _resolve_safe_audio_stream(vpath, req.audio_stream, on_fallback=_scaled_cb)
            res = run_analysis_pipeline(
                video_path=vpath,
                output_dir=out_dir,
                top_percent=req.top_percent,
                asr_provider=req.asr_provider or None,
                asr_mode=req.asr_mode,
                top_k_peaks=req.top_k_peaks,
                model_size=req.model_size,
                asr_model=req.asr_model or None,
                hotwords=req.hotwords,
                normalize_audio=req.normalize_audio,
                vad_threshold=req.vad_threshold,
                language=valid_src_lang,
                target_language=valid_tgt_lang,
                top_k_highlights=req.top_k_highlights,
                min_clip_sec=req.min_clip_sec,
                max_clip_sec=req.max_clip_sec,
                bilingual_subtitles=req.bilingual_subtitles,
                audio_stream=safe_stream,
                vocal_bandpass=req.vocal_bandpass,
                force_rerun=req.force_rerun,
                on_progress=_scaled_cb,
            )
            outputs.append(
                {
                    "stem": res["stem"],
                    "highlights_json_path": res["highlights_json_path"],
                    "highlight_count": res["step3"].get("highlight_count", 0),
                }
            )
        return {"processed_count": len(outputs), "items": outputs}

    return _TASK_MANAGER.submit("analyze", title, _worker)


@app.post("/api/tasks/retranslate")
def start_retranslate_task(req: RetranslateTaskReq) -> dict[str, Any]:
    """对已有高光方案整场或定点单条重刷商业级双语字幕（支持附加语境背景与热词）。"""
    hl_path = _validate_highlights_json_path(req.highlights_json_path)
    valid_src_lang = _validate_optional_lang_code(req.source_language, "source_language")
    valid_tgt_lang = _validate_optional_lang_code(req.target_language, "target_language")

    clean_ranks: list[int] | None = None
    if req.ranks is not None:
        if len(req.ranks) == 0:
            raise HTTPException(status_code=400, detail="ranks 不能为空列表；如需全量重刷请传 null")
        clean_ranks = []
        for r in req.ranks:
            if isinstance(r, bool) or int(r) <= 0:
                raise HTTPException(status_code=400, detail=f"非法片段排名 rank={r}，必须为正整数")
            ri = int(r)
            if ri not in clean_ranks:
                clean_ranks.append(ri)

    stem = _validate_safe_stem(hl_path.stem.removesuffix("_highlights"))
    rank_label = (
        f"片段 #{', #'.join(str(r) for r in clean_ranks)}"
        if clean_ranks
        else "全部高光"
    )
    title = f"重刷双语字幕（{rank_label}）：{stem}"

    def _worker(report_cb: Any) -> dict[str, Any]:
        report_cb("step3", 20, f"[{stem}] 正在调用商业字幕精修引擎重新翻译{rank_label}...")
        res = enrich_highlights_with_bilingual_subtitles(
            input_json_path=hl_path,
            hotwords=req.hotwords,
            extra_context=req.extra_context,
            source_language=valid_src_lang,
            target_language=valid_tgt_lang,
            ranks=clean_ranks,
        )
        ok_cnt = int(res.get("translated_ok_count", 0))
        fb_cnt = int(res.get("translation_fallback_count", 0))
        report_cb(
            "done",
            100,
            f"[{stem}] {rank_label}双语重刷完成：成功 {ok_cnt} 条" + (f"，回退 {fb_cnt} 条" if fb_cnt else ""),
        )
        return {
            "stem": stem,
            "highlights_json_path": res["highlights_json_path"],
            "translated_ok_count": ok_cnt,
            "translation_fallback_count": fb_cnt,
            "ranks": clean_ranks,
        }

    return _TASK_MANAGER.submit("retranslate", title, _worker)


@app.post("/api/tasks/export")
def start_export_task(req: ExportTaskReq) -> dict[str, Any]:
    """提交 Step 4 切片与专业 NLE 工程包导出任务到后台队列。"""
    cfg = _read_workbench_config()
    hl_path = _validate_highlights_json_path(req.highlights_json_path, cfg=cfg)
    if req.mode not in VALID_EXPORT_MODES:
        raise HTTPException(
            status_code=400,
            detail=f"不支持的导出模式：{req.mode}，仅支持 {', '.join(sorted(VALID_EXPORT_MODES))}",
        )
    raw_sub_mode = req.subtitle_mode if req.subtitle_mode is not None else req.subtitle_lang
    try:
        resolved_sub_mode = normalize_subtitle_lang_mode(raw_sub_mode)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    resolved_video_path: Path | None = None
    if req.video_path:
        resolved_video_path = _resolve_allowed_read_path(
            req.video_path,
            SUPPORTED_VIDEO_EXTS,
            cfg=cfg,
            bad_ext_status=400,
        )

    resolved_out_dir: Path | None = None
    if req.output_dir:
        resolved_out_dir = _resolve_allowed_output_dir(req.output_dir, cfg=cfg, create=False)

    clean_ranks: list[int] | None = None
    if req.ranks is not None:
        if len(req.ranks) == 0:
            raise HTTPException(status_code=400, detail="ranks 不能为空列表；如需导出全部已选片段请传 null")
        clean_ranks = []
        for r in req.ranks:
            if isinstance(r, bool) or int(r) <= 0:
                raise HTTPException(status_code=400, detail=f"非法片段排名 rank={r}，必须为正整数")
            ri = int(r)
            if ri not in clean_ranks:
                clean_ranks.append(ri)

    try:
        hl_data = json.loads(hl_path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"高光方案 JSON 解析失败：{exc}") from exc

    all_hls = hl_data.get("highlights") or []
    if clean_ranks is not None:
        rank_set = set(clean_ranks)
        candidates = [
            h for h in all_hls if isinstance(h, dict) and int(h.get("rank", 0) or 0) in rank_set
        ]
        if not candidates:
            raise HTTPException(status_code=400, detail=f"找不到指定的片段排名：{clean_ranks}")
    else:
        candidates = [h for h in all_hls if isinstance(h, dict) and h.get("selected", True) is True]
        if not candidates:
            raise HTTPException(status_code=400, detail="当前没有勾选任何要导出的高光片段，请先勾选后再导出")

    # 无论是否传 ranks，只要 only_reviewed=True 必须统一强制门禁，绝不允许指定 ranks 绕过审校！
    if req.only_reviewed:
        reviewed_candidates = [
            h for h in candidates if normalize_review_status(h.get("review_status")) == "reviewed"
        ]
        if not reviewed_candidates:
            raise HTTPException(
                status_code=400,
                detail="当前没有已标记为「已人工校对 (Reviewed)」的目标片段，请先完成人工审校或取消勾选「仅导出已人工校对」",
            )

    safe_stem = _validate_safe_stem(hl_path.stem.removesuffix("_highlights"))
    title = f"导出交付（{req.mode}）：{safe_stem}"

    def _worker(report_cb: Any) -> dict[str, Any]:
        return run_export_pipeline(
            highlights_json_path=hl_path,
            video_path=resolved_video_path,
            output_dir=resolved_out_dir,
            mode=req.mode,
            burn_subtitles=req.burn_subtitles and resolved_sub_mode != "none",
            subtitle_lang=resolved_sub_mode,
            show_title_banner=req.show_title_banner,
            only_reviewed=req.only_reviewed,
            encoder=req.encoder,
            ranks=clean_ranks,
            on_progress=report_cb,
        )

    return _TASK_MANAGER.submit("export", title, _worker)


@app.post("/api/tasks/diagnose")
def start_diagnose_task(req: DiagnoseTaskReq) -> dict[str, Any]:
    """提交零 API 消耗的仅音频快速体检任务（批量测算直播评级与分小时热度）。"""
    cfg = _read_workbench_config()
    out_dir = _resolve_allowed_output_dir(req.output_dir, cfg=cfg, create=True)
    _require_finite_float(req.top_percent, "top_percent")

    if not req.video_paths:
        raise HTTPException(status_code=400, detail="没有找到有效的视频文件")

    valid_paths: list[Path] = []
    for raw_vp in req.video_paths:
        vp = _resolve_allowed_read_path(
            raw_vp,
            SUPPORTED_VIDEO_EXTS,
            cfg=cfg,
            bad_ext_status=404,
        )
        _validate_safe_stem(vp.stem)
        valid_paths.append(vp)

    title = (
        f"快速音频体检：{valid_paths[0].name}"
        if len(valid_paths) == 1
        else f"批量音频体检（共 {len(valid_paths)} 个视频）"
    )

    def _worker(report_cb: Any) -> dict[str, Any]:
        total = len(valid_paths)
        results: list[dict[str, Any]] = []
        for idx, vpath in enumerate(valid_paths, start=1):
            base_pct = int(((idx - 1) / total) * 100)
            span_pct = 100.0 / total

            def _scaled_cb(
                stage: str,
                pct: int,
                msg: str,
                _idx: int = idx,
                _base: int = base_pct,
                _span: float = span_pct,
            ) -> None:
                global_pct = min(99, int(_base + (pct / 100.0) * _span))
                report_cb(stage, global_pct, f"[{_idx}/{total}] {msg}")

            safe_stream = _resolve_safe_audio_stream(vpath, req.audio_stream, on_fallback=_scaled_cb)
            res = run_quick_diagnose(
                video_path=vpath,
                output_dir=out_dir,
                top_percent=req.top_percent,
                audio_stream=safe_stream,
                vocal_bandpass=req.vocal_bandpass,
                force_rerun=req.force_rerun,
                on_progress=_scaled_cb,
            )
            results.append(res)
        return {"diagnosed_count": len(results), "items": results}

    return _TASK_MANAGER.submit("diagnose", title, _worker)


@app.get("/api/tasks")
def list_tasks() -> dict[str, Any]:
    """获取最近的全部后台任务及实时进度日志。"""
    return {"tasks": _TASK_MANAGER.list_recent()}


@app.get("/api/tasks/{task_id}")
def get_task(task_id: str) -> dict[str, Any]:
    """获取单个任务的详细进度与输出日志。"""
    rec = _TASK_MANAGER.get(task_id)
    if rec is None:
        raise HTTPException(status_code=404, detail="找不到指定任务")
    return rec


@app.delete("/api/tasks/{task_id}")
def cancel_task(task_id: str) -> dict[str, Any]:
    """取消处于排队中 (queued) 的任务。"""
    code, rec = _TASK_MANAGER.cancel_queued(task_id)
    if code == "not_found":
        raise HTTPException(status_code=404, detail="找不到指定任务")
    if code == "not_queued":
        raise HTTPException(status_code=409, detail="仅支持取消尚未开始执行的排队任务")
    return {"ok": True, "task": rec}


@app.post("/api/tasks/clear")
def clear_finished_tasks() -> dict[str, Any]:
    """清理已完成、失败或已取消的历史任务记录。"""
    removed = _TASK_MANAGER.clear_finished()
    return {"ok": True, "removed": removed}


def main(argv: list[str] | None = None) -> int:
    """启动本地 Web 工作台服务器并自动在默认浏览器打开。"""
    import uvicorn

    parser = argparse.ArgumentParser(description="启动公会高光切片图形化审片工作台")
    parser.add_argument("--host", default="127.0.0.1", help="监听主机（默认 127.0.0.1）")
    parser.add_argument("--port", type=int, default=8765, help="监听端口（默认 8765）")
    parser.add_argument("--no-browser", action="store_true", help="启动后不自动打开浏览器")
    args = parser.parse_args(argv)

    url = f"http://{args.host}:{args.port}"
    print("=" * 64)
    print("公会高光切片工作台已启动")
    print(f"访问地址：{url}")
    print("按 Ctrl+C 可随时安全关闭服务")
    print("=" * 64)

    if not args.no_browser:
        threading.Timer(1.0, lambda: webbrowser.open(url)).start()

    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
