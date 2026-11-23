"""生产级 OpenAI 兼容大模型调度层（连接池复用、429/5xx 指数退避、推理参数兼容、Token 账单统计）。

支持任意 OpenAI 兼容厂商（DeepSeek-Flash / DeepSeek-V4 / 通义千问 / OpenAI / 硅基流动等）：
- 复用底层连接池避免频繁握手；
- 遇到 HTTP 429 限流、5xx 抖动或网络超时自动指数退避重试；
- 自动兼容 `deepseek-flash` 思考开关（`reasoning_effort` / `extra_body`）与推理模型不支持 `temperature` 等参数差异；
- 自动汇总粗筛（screen）与双语精修（polish）各角色的 Token 用量统计。
"""

from __future__ import annotations

import random
import threading
import time
from typing import Any, Callable


class TokenUsageTracker:
    """线程安全的分角色 Token 用量统计器。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._by_role: dict[str, dict[str, Any]] = {}

    def record(
        self,
        role: str,
        model: str,
        prompt_tokens: int,
        completion_tokens: int,
        total_tokens: int,
    ) -> None:
        p_tok = max(0, int(prompt_tokens or 0))
        c_tok = max(0, int(completion_tokens or 0))
        t_tok = max(p_tok + c_tok, int(total_tokens or 0))
        with self._lock:
            entry = self._by_role.setdefault(
                role,
                {
                    "model": model,
                    "request_count": 0,
                    "prompt_tokens": 0,
                    "completion_tokens": 0,
                    "total_tokens": 0,
                },
            )
            entry["model"] = model
            entry["request_count"] += 1
            entry["prompt_tokens"] += p_tok
            entry["completion_tokens"] += c_tok
            entry["total_tokens"] += t_tok

    def summary(self) -> dict[str, Any]:
        with self._lock:
            by_role = {k: dict(v) for k, v in self._by_role.items()}
        return {
            "request_count": sum(v["request_count"] for v in by_role.values()),
            "prompt_tokens": sum(v["prompt_tokens"] for v in by_role.values()),
            "completion_tokens": sum(v["completion_tokens"] for v in by_role.values()),
            "total_tokens": sum(v["total_tokens"] for v in by_role.values()),
            "by_role": by_role,
        }


def _extract_status_code(exc: Exception) -> int | None:
    """从 OpenAI SDK 或 httpx 异常对象中提取 HTTP 状态码。"""
    direct = getattr(exc, "status_code", None)
    if isinstance(direct, int):
        return direct
    resp = getattr(exc, "response", None)
    if resp is not None:
        code = getattr(resp, "status_code", None)
        if isinstance(code, int):
            return code
    return None


def _is_retryable_exception(exc: Exception, status_code: int | None) -> bool:
    """判断异常是否属于限流、网络抖动或服务端临时故障（值得指数退避重试）。"""
    if status_code in {408, 429, 500, 502, 503, 504}:
        return True
    cls_name = exc.__class__.__name__.lower()
    msg = str(exc).lower()
    if any(kw in cls_name for kw in ("ratelimit", "timeout", "connection", "internalserver", "serviceunavailable")):
        return True
    if any(kw in msg for kw in ("rate limit", "too many requests", "timed out", "connection reset", "temporarily unavailable")):
        return True
    return False


def _is_deepseek_toggleable_thinking_model(model: str) -> bool:
    """判断模型是否为默认开启思考且支持 `thinking: enabled/disabled` 开关的 DeepSeek 混合模型。"""
    m = (model or "").strip().lower()
    return any(tag in m for tag in ("deepseek-flash", "deepseek-v4", "deepseek-v3.2"))


def _is_always_thinking_model(model: str) -> bool:
    """判断模型是否为默认强制输出思维链（且 max_tokens 包含思维链）的纯推理模型。"""
    m = (model or "").strip().lower()
    if any(tag in m for tag in ("deepseek-reasoner", "deepseek-r1", "qwq", "qvq")):
        return True
    base = m.split("/")[-1]
    return base in {"o1", "o3", "o4-mini"} or base.startswith(("o1-", "o3-", "o4-"))


def _read_usage_counts(response: Any) -> tuple[int, int, int]:
    """兼容提取 ChatCompletion 响应中的 Token 计数。"""
    usage = getattr(response, "usage", None)
    if usage is None:
        return 0, 0, 0
    if isinstance(usage, dict):
        p = int(usage.get("prompt_tokens") or 0)
        c = int(usage.get("completion_tokens") or 0)
        t = int(usage.get("total_tokens") or (p + c))
        return p, c, t
    p = int(getattr(usage, "prompt_tokens", 0) or 0)
    c = int(getattr(usage, "completion_tokens", 0) or 0)
    t = int(getattr(usage, "total_tokens", 0) or (p + c))
    return p, c, t


def _read_choice_content_and_finish_reason(response: Any) -> tuple[str, str]:
    """安全读取首条回复的正文内容与 finish_reason。"""
    choices = getattr(response, "choices", None) or []
    if not choices:
        return "", ""
    first = choices[0]
    finish_reason = str(getattr(first, "finish_reason", "") or "").strip().lower()
    msg = getattr(first, "message", None)
    content = getattr(msg, "content", "") if msg is not None else ""
    return str(content or ""), finish_reason


class LLMClient:
    """带连接池复用、自动重试与多厂商参数自适应的 OpenAI 兼容调度器。"""

    def __init__(
        self,
        client_factory: Callable[..., Any] | None = None,
    ) -> None:
        self._lock = threading.Lock()
        self._clients: dict[tuple[str, str, float], Any] = {}
        self._client_factory = client_factory

    def _get_client(self, api_key: str, base_url: str | None, timeout: float) -> Any:
        cache_key = (api_key, base_url or "", float(timeout))
        with self._lock:
            cached = self._clients.get(cache_key)
            if cached is not None:
                return cached

        kwargs: dict[str, Any] = {"api_key": api_key, "timeout": timeout}
        if base_url:
            kwargs["base_url"] = base_url

        if self._client_factory is not None:
            client = self._client_factory(**kwargs)
        else:
            try:
                from openai import OpenAI
            except ImportError as exc:
                raise RuntimeError(
                    "尚未安装 openai SDK。请先运行：\n"
                    "uv pip install --python .venv\\Scripts\\python.exe openai python-dotenv"
                ) from exc
            client = OpenAI(**kwargs)

        with self._lock:
            self._clients[cache_key] = client
        return client

    def complete_json(
        self,
        system_prompt: str,
        user_prompt: str,
        api_key: str,
        base_url: str | None,
        model: str,
        temperature: float = 0.3,
        max_tokens: int = 8192,
        reasoning_effort: str = "",
        role: str = "screen",
        usage_tracker: TokenUsageTracker | None = None,
        timeout: float = 120.0,
        max_retries: int = 4,
        retry_base_delay: float = 1.0,
    ) -> str:
        """调用大模型获取 JSON 回复，自动处理 429 重试与推理/兼容模型参数差异。"""
        client = self._get_client(api_key=api_key, base_url=base_url, timeout=timeout)
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ]

        effort_clean = (reasoning_effort or "").strip().lower()
        toggleable_thinking = _is_deepseek_toggleable_thinking_model(model)
        always_thinking = _is_always_thinking_model(model)

        effective_max_tokens = int(max_tokens)
        req_kwargs: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": effective_max_tokens,
            "response_format": {"type": "json_object"},
        }

        if effort_clean in {"low", "medium", "high", "max"}:
            # 思考模式下 max_tokens 同时计入思维链与正文，必须自动放宽上限防止截断 JSON
            min_thinking_budget = 65536 if effort_clean in {"high", "max"} else 32768
            req_kwargs["max_tokens"] = max(effective_max_tokens, min_thinking_budget)
            req_kwargs["reasoning_effort"] = effort_clean
            req_kwargs["extra_body"] = {"thinking": {"type": "enabled"}}
        elif effort_clean in {"", "none", "off", "disabled", "false"}:
            if toggleable_thinking:
                # deepseek-flash 等混合模型默认开启 high 思考，关闭时必须显式传 thinking.disabled，
                # 否则不仅耗时暴涨、烧思考 Token，还会把 8192 max_tokens 吃光导致 JSON 截断。
                req_kwargs["extra_body"] = {"thinking": {"type": "disabled"}}
            elif always_thinking:
                # 强制推理模型（如 deepseek-reasoner / o3）不能关闭思考，自动放宽输出上限
                req_kwargs["max_tokens"] = max(effective_max_tokens, 32768)
        else:
            # 其他自定义 reasoning_effort 档位透传并自动给足思考预算
            req_kwargs["max_tokens"] = max(effective_max_tokens, 32768)
            req_kwargs["reasoning_effort"] = effort_clean
            if toggleable_thinking:
                req_kwargs["extra_body"] = {"thinking": {"type": "enabled"}}

        retry_attempt = 0
        param_downgrades = 0
        length_retries = 0

        while True:
            try:
                response = client.chat.completions.create(**req_kwargs)
            except Exception as exc:
                status_code = _extract_status_code(exc)
                err_msg = str(exc).lower()

                if status_code in {401, 403}:
                    raise RuntimeError(
                        f"大模型接口鉴权失败（HTTP {status_code}，模型：{model}），请核对 API Key 与 Base URL。"
                    ) from exc

                # 1. 处理参数不兼容（HTTP 400 / 推理模型不支持 temperature / 厂商不支持 response_format 等）
                if param_downgrades < 4 and (
                    status_code == 400
                    or any(
                        kw in err_msg
                        for kw in ("response_format", "json_object", "temperature", "reasoning_effort", "extra_body", "thinking", "max_tokens")
                    )
                ):
                    mutated = False
                    if "temperature" in err_msg and "temperature" in req_kwargs:
                        req_kwargs.pop("temperature", None)
                        mutated = True
                    if ("reasoning_effort" in err_msg or "extra_body" in err_msg or "thinking" in err_msg) and (
                        "reasoning_effort" in req_kwargs or "extra_body" in req_kwargs
                    ):
                        req_kwargs.pop("reasoning_effort", None)
                        req_kwargs.pop("extra_body", None)
                        # 若第三方网关不支持显式关闭 thinking，对默认思考模型同步放宽 max_tokens 防截断
                        if toggleable_thinking and "max_tokens" in req_kwargs:
                            req_kwargs["max_tokens"] = max(int(req_kwargs["max_tokens"]), 32768)
                        mutated = True
                    if ("response_format" in err_msg or "json_object" in err_msg) and "response_format" in req_kwargs:
                        req_kwargs.pop("response_format", None)
                        mutated = True
                    if "max_tokens" in err_msg and "max_tokens" in req_kwargs:
                        req_kwargs.pop("max_tokens", None)
                        mutated = True
                    # 兜底：若 400 未明确指出字段但仍带着 response_format，尝试去掉它再试一次
                    if not mutated and status_code == 400 and "response_format" in req_kwargs:
                        req_kwargs.pop("response_format", None)
                        mutated = True

                    if mutated:
                        param_downgrades += 1
                        continue

                # 2. 处理限流 429 / 服务端 5xx / 网络抖动：指数退避重试
                if _is_retryable_exception(exc, status_code) and retry_attempt < max_retries:
                    delay = retry_base_delay * (2 ** retry_attempt) + random.uniform(
                        0.0, retry_base_delay * 0.25
                    )
                    retry_attempt += 1
                    if delay > 0:
                        time.sleep(delay)
                    continue

                raise

            p_tok, c_tok, t_tok = _read_usage_counts(response)
            if usage_tracker is not None:
                usage_tracker.record(role, model, p_tok, c_tok, t_tok)
            content, finish_reason = _read_choice_content_and_finish_reason(response)

            # 拦截输出长度截断（finish_reason == "length"）：自动放宽上限或关闭思考重跑，绝不向后透传半截 JSON
            if finish_reason == "length":
                cur_limit = int(req_kwargs.get("max_tokens") or 0)
                thinking_obj = (
                    req_kwargs.get("extra_body", {}).get("thinking", {})
                    if isinstance(req_kwargs.get("extra_body"), dict)
                    else {}
                )
                is_thinking_disabled = (
                    isinstance(thinking_obj, dict) and thinking_obj.get("type") == "disabled"
                )

                if length_retries < 2:
                    if cur_limit and cur_limit < 65536:
                        req_kwargs["max_tokens"] = 65536
                        length_retries += 1
                        continue
                    if not is_thinking_disabled and (
                        toggleable_thinking
                        or "reasoning_effort" in req_kwargs
                        or "extra_body" in req_kwargs
                    ):
                        req_kwargs.pop("reasoning_effort", None)
                        if toggleable_thinking:
                            req_kwargs["extra_body"] = {"thinking": {"type": "disabled"}}
                        else:
                            req_kwargs.pop("extra_body", None)
                        length_retries += 1
                        continue

                raise RuntimeError(
                    f"大模型输出触发长度上限截断（finish_reason=length，模型：{model}，"
                    f"max_tokens={req_kwargs.get('max_tokens')}，输出字符数：{len(content)}，"
                    f"completion_tokens={c_tok}）。建议在设置中关闭思考模式或减少单批处理长度。"
                )

            return content


DEFAULT_LLM_CLIENT = LLMClient()
