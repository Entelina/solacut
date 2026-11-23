"""统一三层接口路由配置中心（ASR 独立语音识别 + 粗筛模型 + 双语精修模型）。

设计目标：
1. 极简上手：如果你使用 DeepSeek 全家桶，只需填 1 个 API Key（OPENAI_API_KEY 或 DEEPSEEK_API_KEY），
   系统自动同时驱动 DeepSeek 云端语音识别（/v1/tools/asr）+ deepseek-flash 高光粗筛与双语精修。
2. 专业混搭：大公会可随时按层独立指定不同厂商与模型：
   - 第 1 层（ASR_*）：DeepSeek 官方 ASR / 本地增强 faster-whisper (large-v3-turbo) / Groq 或 OpenAI 兼容 ASR
   - 第 2 层（SCREEN_LLM_*）：高速低价长上下文粗筛模型（默认 deepseek-flash）
   - 第 3 层（POLISH_LLM_*）：高文采字幕组精修翻译模型（未单独指定时自动沿用粗筛配置）
3. 单一事实来源：命令行脚本与 Web 工作台共用本模块，彻底杜绝前后端配置键名不一致问题。
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_ENV_PATH = PROJECT_ROOT / ".env"
DEFAULT_WORKBENCH_CONFIG_PATH = PROJECT_ROOT / ".workbench_config.json"

DEFAULT_BASE_URL = "https://api.deepseek.com/v1"
DEFAULT_LLM_MODEL = "deepseek-flash"
DEFAULT_ASR_PROVIDER = "deepseek"
DEFAULT_DEEPSEEK_ASR_URL = "https://api.deepseek.com/v1/tools/asr"
DEFAULT_OPENAI_ASR_URL = "https://api.openai.com/v1/audio/transcriptions"
DEFAULT_LOCAL_WHISPER_MODEL = "large-v3-turbo"
DEFAULT_OPENAI_ASR_MODEL = "whisper-large-v3-turbo"

VALID_ASR_PROVIDERS = ("deepseek", "local", "openai_compatible")
_PLACEHOLDER_SECRETS = {"", "your_api_key_here", "your_deepseek_api_key_here", "sk-xxx"}
_VALID_ENV_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_SECRET_REDACT_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"(Bearer\s+)[^\s\"',;]+", re.IGNORECASE), r"\1***REDACTED***"),
    (re.compile(r"\b(sk-[A-Za-z0-9_\-]{6,})\b"), "sk-***REDACTED***"),
    (re.compile(r"\b(ghp_[A-Za-z0-9]{10,}|github_pat_[A-Za-z0-9_]{10,})\b"), "***REDACTED_GH_TOKEN***"),
    (re.compile(r"((?:api[_-]?key|authorization|token)\s*[:=]\s*['\"]?)([^\s'\",;}{]{8,})", re.IGNORECASE), r"\1***REDACTED***"),
)
_URL_ENV_KEYS = frozenset(
    {
        "OPENAI_BASE_URL",
        "ASR_BASE_URL",
        "SCREEN_LLM_BASE_URL",
        "POLISH_LLM_BASE_URL",
    }
)


def redact_secrets_in_text(text: str) -> str:
    """对任意异常/日志字符串执行密钥脱敏，防止 API Key 或 Bearer Token 泄露到日志与前端。"""
    if not text:
        return ""
    out = str(text)
    for pat, repl in _SECRET_REDACT_PATTERNS:
        out = pat.sub(repl, out)
    return out


def _sanitize_env_value(key: str, raw_val: str) -> str:
    """清理环境变量值中的换行/空字节注入，并校验 URL 字段仅允许 http(s) 协议。"""
    # 剥离 \r / \n / \x00 彻底阻断 .env 多行注入
    cleaned = re.sub(r"[\r\n\x00]+", " ", str(raw_val)).strip()
    if key in _URL_ENV_KEYS and cleaned:
        low = cleaned.lower()
        if not (low.startswith("https://") or low.startswith("http://")):
            raise ValueError(f"配置项 {key} 必须以 https:// 或 http:// 开头")
    return cleaned

# 写出 .env 时的推荐键顺序，方便人工打开阅读和编辑
_ORDERED_ENV_KEYS = [
    "OPENAI_API_KEY",
    "DEEPSEEK_API_KEY",
    "OPENAI_BASE_URL",
    "LLM_MODEL",
    "ASR_PROVIDER",
    "ASR_API_KEY",
    "ASR_BASE_URL",
    "ASR_MODEL",
    "ASR_CONCURRENCY",
    "ASR_HOTWORDS",
    "SCREEN_LLM_API_KEY",
    "SCREEN_LLM_BASE_URL",
    "SCREEN_LLM_MODEL",
    "SCREEN_LLM_REASONING_EFFORT",
    "POLISH_LLM_API_KEY",
    "POLISH_LLM_BASE_URL",
    "POLISH_LLM_MODEL",
    "POLISH_LLM_REASONING_EFFORT",
]


def mask_secret(secret: str) -> str:
    """对 API Key 做脱敏掩码展示，防止日志或 Web 界面明文泄露密钥。"""
    s = (secret or "").strip()
    if not s or s.lower() in _PLACEHOLDER_SECRETS:
        return ""
    if len(s) <= 8:
        return "*" * len(s)
    return f"{s[:4]}{'*' * max(4, min(12, len(s) - 8))}{s[-4:]}"


def _is_valid_secret(secret: str | None) -> bool:
    """判断密钥字符串是否为非空、非占位符的有效值。"""
    s = (secret or "").strip()
    return bool(s) and s.lower() not in _PLACEHOLDER_SECRETS


def read_env_dict(env_path: Path | None = None) -> dict[str, str]:
    """安全解析 .env 文件中的合法 KEY=VALUE 字典（不直接覆写进程环境变量）。"""
    target = env_path if env_path is not None else DEFAULT_ENV_PATH
    if not target.is_file():
        return {}

    try:
        from dotenv import dotenv_values
    except ImportError:
        return {}

    raw = dotenv_values(target)
    cleaned: dict[str, str] = {}
    for k, v in raw.items():
        if not k or not _VALID_ENV_KEY_RE.match(k):
            continue
        cleaned[k] = str(v or "").strip()
    return cleaned


def _get_env_value(key: str, file_env: dict[str, str]) -> str:
    """读取单个配置值：显式进程环境变量（含测试 patch 设空）优先于 .env 文件。"""
    if key in os.environ:
        return os.environ[key].strip()
    return file_env.get(key, "").strip()


def _pick_first_secret(
    explicit: str | None,
    candidate_keys: tuple[str, ...],
    file_env: dict[str, str],
) -> str:
    """按优先级挑选第一个有效的 API Key（若显式传入空串则视作无密钥，便于离线测试）。"""
    if explicit is not None:
        return explicit.strip() if _is_valid_secret(explicit) else ""
    for key in candidate_keys:
        val = _get_env_value(key, file_env)
        if _is_valid_secret(val):
            return val
    return ""


def _pick_first_text(
    explicit: str | None,
    candidate_keys: tuple[str, ...],
    file_env: dict[str, str],
    default: str = "",
) -> str:
    """按优先级挑选第一个非空字符串配置。"""
    if explicit is not None and explicit.strip():
        return explicit.strip()
    for key in candidate_keys:
        val = _get_env_value(key, file_env)
        if val:
            return val
    return default


def resolve_asr_config(
    provider: str | None = None,
    api_key: str | None = None,
    base_url: str | None = None,
    model: str | None = None,
    concurrency: int | None = None,
    hotwords: str | None = None,
    require_api_key: bool = False,
    env_path: Path | None = None,
) -> dict[str, Any]:
    """解析第 1 层（独立语音识别 ASR）完整运行配置。

    支持：
    - `deepseek`（默认）：DeepSeek 官方 `/v1/tools/asr`
    - `local`：本地 RTX CUDA / CPU faster-whisper（默认 `large-v3-turbo`）
    - `openai_compatible`：通用 OpenAI `/audio/transcriptions` 兼容云端 ASR
    """
    file_env = read_env_dict(env_path)

    resolved_provider = _pick_first_text(
        provider, ("ASR_PROVIDER",), file_env, default=DEFAULT_ASR_PROVIDER
    ).lower()
    if resolved_provider not in VALID_ASR_PROVIDERS:
        raise ValueError(
            f"不支持的语音识别引擎 asr_provider='{resolved_provider}'，"
            f"可选值：{', '.join(VALID_ASR_PROVIDERS)}"
        )

    resolved_key = _pick_first_secret(
        api_key,
        ("ASR_API_KEY", "DEEPSEEK_API_KEY", "OPENAI_API_KEY"),
        file_env,
    )

    if resolved_provider == "deepseek":
        default_url = DEFAULT_DEEPSEEK_ASR_URL
        default_model = "deepseek-asr"
    elif resolved_provider == "openai_compatible":
        default_url = DEFAULT_OPENAI_ASR_URL
        default_model = DEFAULT_OPENAI_ASR_MODEL
    else:
        default_url = ""
        default_model = DEFAULT_LOCAL_WHISPER_MODEL

    resolved_url = _pick_first_text(base_url, ("ASR_BASE_URL",), file_env, default=default_url)
    resolved_model = _pick_first_text(model, ("ASR_MODEL",), file_env, default=default_model)

    if concurrency is not None:
        raw_conc = int(concurrency)
    else:
        conc_str = _get_env_value("ASR_CONCURRENCY", file_env)
        try:
            raw_conc = int(conc_str) if conc_str else 4
        except ValueError:
            raw_conc = 4
    resolved_concurrency = max(1, min(16, raw_conc))

    resolved_hotwords = (
        hotwords.strip()
        if hotwords is not None
        else _get_env_value("ASR_HOTWORDS", file_env)
    )

    if require_api_key and resolved_provider in {"deepseek", "openai_compatible"} and not resolved_key:
        target_env = env_path if env_path is not None else DEFAULT_ENV_PATH
        raise RuntimeError(
            f"当前语音识别引擎为 `{resolved_provider}`，但未检测到有效的 API Key（ASR_API_KEY / DEEPSEEK_API_KEY / OPENAI_API_KEY）。\n"
            f"请在工作台右上角「设置与外观」保存密钥，或在 `{target_env}` 中配置标准 KEY=VALUE 格式密钥；\n"
            "如果希望纯离线免费转写，可将语音识别引擎切换为 `local`（本地 Whisper）。"
        )

    return {
        "provider": resolved_provider,
        "api_key": resolved_key,
        "base_url": resolved_url,
        "model": resolved_model,
        "concurrency": resolved_concurrency,
        "hotwords": resolved_hotwords,
    }


def resolve_llm_role_config(
    role: str = "screen",
    api_key: str | None = None,
    base_url: str | None = None,
    model: str | None = None,
    reasoning_effort: str | None = None,
    require_api_key: bool = True,
    env_path: Path | None = None,
) -> dict[str, Any]:
    """解析第 2 层（粗筛 `screen`）或第 3 层（双语精修 `polish`）的大模型路由配置。"""
    role_norm = (role or "screen").strip().lower()
    if role_norm not in {"screen", "polish"}:
        raise ValueError(f"role 仅支持 'screen' 或 'polish'，当前传入：{role}")

    file_env = read_env_dict(env_path)

    if role_norm == "screen":
        key_chain = ("SCREEN_LLM_API_KEY", "OPENAI_API_KEY", "DEEPSEEK_API_KEY")
        url_chain = ("SCREEN_LLM_BASE_URL", "OPENAI_BASE_URL")
        model_chain = ("SCREEN_LLM_MODEL", "LLM_MODEL")
        effort_chain = ("SCREEN_LLM_REASONING_EFFORT",)
    else:
        key_chain = ("POLISH_LLM_API_KEY", "SCREEN_LLM_API_KEY", "OPENAI_API_KEY", "DEEPSEEK_API_KEY")
        url_chain = ("POLISH_LLM_BASE_URL", "SCREEN_LLM_BASE_URL", "OPENAI_BASE_URL")
        model_chain = ("POLISH_LLM_MODEL", "SCREEN_LLM_MODEL", "LLM_MODEL")
        effort_chain = ("POLISH_LLM_REASONING_EFFORT",)

    resolved_key = _pick_first_secret(api_key, key_chain, file_env)
    resolved_url = _pick_first_text(base_url, url_chain, file_env, default=DEFAULT_BASE_URL)
    resolved_model = _pick_first_text(model, model_chain, file_env, default=DEFAULT_LLM_MODEL)
    resolved_effort = _pick_first_text(reasoning_effort, effort_chain, file_env, default="")

    if require_api_key and not resolved_key:
        target_env = env_path if env_path is not None else DEFAULT_ENV_PATH
        raise RuntimeError(
            "未检测到有效的 OPENAI_API_KEY（或 DEEPSEEK_API_KEY）。\n"
            f"请先将项目根目录下的 `.env.example` 复制为 `.env`（位于 {target_env}），\n"
            "按标准 KEY=VALUE 格式填入你的 API Key / Base URL / 模型名，或在工作台「设置与外观」中保存后再试。"
        )

    return {
        "role": role_norm,
        "api_key": resolved_key,
        "base_url": resolved_url or None,
        "model": resolved_model,
        "reasoning_effort": resolved_effort,
    }


def read_workbench_config(
    config_path: Path = DEFAULT_WORKBENCH_CONFIG_PATH,
    env_path: Path = DEFAULT_ENV_PATH,
) -> dict[str, Any]:
    """统一读取工作台目录设置与脱敏后的三层 API 路由状态（供 Web 后端直接使用）。"""
    input_dir = str((PROJECT_ROOT / "input").resolve())
    output_dir = str((PROJECT_ROOT / "output").resolve())
    if config_path.is_file():
        try:
            saved = json.loads(config_path.read_text(encoding="utf-8"))
            if isinstance(saved, dict):
                if saved.get("input_dir"):
                    input_dir = str(saved["input_dir"])
                if saved.get("output_dir"):
                    output_dir = str(saved["output_dir"])
        except Exception:
            pass

    file_env = read_env_dict(env_path)
    primary_key = _pick_first_secret(None, ("OPENAI_API_KEY", "DEEPSEEK_API_KEY"), file_env)
    primary_url = _pick_first_text(None, ("OPENAI_BASE_URL",), file_env, default=DEFAULT_BASE_URL)
    primary_model = _pick_first_text(
        None, ("SCREEN_LLM_MODEL", "LLM_MODEL"), file_env, default=DEFAULT_LLM_MODEL
    )

    asr_cfg = resolve_asr_config(require_api_key=False, env_path=env_path)
    screen_cfg = resolve_llm_role_config("screen", require_api_key=False, env_path=env_path)
    polish_cfg = resolve_llm_role_config("polish", require_api_key=False, env_path=env_path)

    # 区分独立覆盖项与回退项，让界面输入框清爽显示
    raw_asr_key = _get_env_value("ASR_API_KEY", file_env)
    raw_screen_key = _get_env_value("SCREEN_LLM_API_KEY", file_env)
    raw_polish_key = _get_env_value("POLISH_LLM_API_KEY", file_env)

    return {
        # 向后兼容的顶层字段
        "input_dir": input_dir,
        "output_dir": output_dir,
        "has_api_key": bool(primary_key),
        "api_key_masked": mask_secret(primary_key),
        "base_url": primary_url,
        "llm_model": primary_model,
        # 第 1 层：ASR 语音识别配置
        "asr_provider": asr_cfg["provider"],
        "asr_base_url": _get_env_value("ASR_BASE_URL", file_env),
        "asr_effective_base_url": asr_cfg["base_url"],
        "asr_model": _get_env_value("ASR_MODEL", file_env),
        "asr_effective_model": asr_cfg["model"],
        "asr_concurrency": asr_cfg["concurrency"],
        "asr_hotwords": asr_cfg["hotwords"],
        "has_asr_api_key": bool(asr_cfg["api_key"]),
        "asr_api_key_masked": mask_secret(raw_asr_key) if _is_valid_secret(raw_asr_key) else "",
        # 第 2 层：粗筛大模型配置
        "screen_llm_model": screen_cfg["model"],
        "screen_llm_base_url": _get_env_value("SCREEN_LLM_BASE_URL", file_env),
        "screen_reasoning_effort": screen_cfg["reasoning_effort"],
        "has_screen_api_key": bool(screen_cfg["api_key"]),
        "screen_api_key_masked": mask_secret(raw_screen_key) if _is_valid_secret(raw_screen_key) else "",
        # 第 3 层：双语精修大模型配置
        "polish_llm_model": _get_env_value("POLISH_LLM_MODEL", file_env),
        "polish_effective_model": polish_cfg["model"],
        "polish_llm_base_url": _get_env_value("POLISH_LLM_BASE_URL", file_env),
        "polish_reasoning_effort": polish_cfg["reasoning_effort"],
        "has_polish_api_key": bool(polish_cfg["api_key"]),
        "polish_api_key_masked": mask_secret(raw_polish_key) if _is_valid_secret(raw_polish_key) else "",
    }


def write_env_settings(
    env_path: Path,
    updates: dict[str, str | None],
) -> None:
    """将配置安全写入标准 KEY=VALUE 格式的 .env 文件并同步更新进程环境变量。

    规则：
    - 仅保留合法环境变量键名，自动清理旧文件中的异常语法噪声；
    - 对于以 `_API_KEY` 结尾的密钥项，空串或包含 `*` 掩码时跳过（保留已有真实密钥）；
    - 对于非密钥配置项，传入空串 `""` 表示清空该项覆盖（恢复默认回退），传入 `None` 表示保持不变。
    """
    existing = read_env_dict(env_path)

    for key, raw_val in updates.items():
        if raw_val is None or not _VALID_ENV_KEY_RE.match(key):
            continue
        cleaned = _sanitize_env_value(key, str(raw_val))
        if key.endswith("_API_KEY"):
            if cleaned and "*" not in cleaned:
                existing[key] = cleaned
                os.environ[key] = cleaned
        else:
            if cleaned:
                existing[key] = cleaned
                os.environ[key] = cleaned
            else:
                existing.pop(key, None)
                os.environ.pop(key, None)

    lines = [
        "# 游戏录屏高光自动切片工具接口配置（由工作台自动维护，请勿提交到 Git）",
    ]
    written: set[str] = set()
    for key in _ORDERED_ENV_KEYS:
        if key in existing:
            lines.append(f"{key}={existing[key]}")
            written.add(key)
    for key, val in existing.items():
        if key not in written:
            lines.append(f"{key}={val}")

    env_path.parent.mkdir(parents=True, exist_ok=True)
    env_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
