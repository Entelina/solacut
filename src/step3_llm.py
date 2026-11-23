"""Step 3：调用大模型对带高音量标记的字幕稿进行自适应粗筛与人工审片清单生成。

本模块负责整个高光切片流程的第三步：
1. 读取 Step 2 生成的 `<视频名>_transcript.json`。
2. 根据直播回放总时长自动规划筛选策略（`compute_adaptive_selection_plan`）：
   - 30 分钟以内短回放：建议精选 4 条，每条 15~45 秒
   - 30 分钟 ~ 2 小时常规场：建议精选 8 条，每条 20~60 秒
   - 2 ~ 4 小时直播：建议精选 10 条，每条 25~75 秒
   - 4 小时以上超长直播：按每小时约 2.5 条自适应上限 15 条，每条 25~90 秒
   （以上参数均可随时通过 CLI 或函数参数手动覆盖）
3. 将字幕（带精确秒数、可读时间戳与 `[HIGH_VOL]` 情绪峰值）按上限长度智能分块，
   调用 OpenAI 兼容接口（DeepSeek / 通义千问 / OpenAI 等）输出结构化高光列表。
4. 对大模型返回结果做边界裁剪、重叠率去重与字段校验，导出两份专业审片产物：
   - `<视频名>_highlights.json`：供 Step 4 切片读取，每条默认带 `"selected": true` 开关，
     剪辑师审片时只需将不需要的条目改为 `false` 即可跳过导出。
   - `<视频名>_review.md`：供人工 1 分钟快速审片的 Markdown 清单（总览表 + 中文梗概 + 原文台词摘录）。

命令行运行示例：
    python src/step3_llm.py output/shylily_1_transcript.json
    python src/step3_llm.py output/shylily_1_transcript.json --top-k 12 --min-clip-sec 20 --max-clip-sec 60
"""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Callable

# 确保既能用 `python src/step3_llm.py` 直接运行，也能用 `from src.step3_llm import ...` 导入
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from src.config import resolve_llm_role_config  # noqa: E402
from src.corpus_rag import (  # noqa: E402
    _normalize_lang_pair,
    detect_source_language,
    format_rag_reference_block,
    retrieve_translation_references,
)
from src.llm_client import DEFAULT_LLM_CLIENT, TokenUsageTracker  # noqa: E402
from src.step1_audio import format_timestamp  # noqa: E402
from src.step2_asr import split_long_segment_by_words  # noqa: E402


def compute_adaptive_selection_plan(
    total_duration_sec: float,
    candidate_window_count: int = 0,
    top_k: int | None = None,
    min_clip_sec: int | None = None,
    max_clip_sec: int | None = None,
) -> dict[str, Any]:
    """根据直播回放总时长自动计算高光筛选数量与单段目标时长范围。

    为什么不做固定条数？
    因为一场 20 分钟的试玩录像硬挑 15 条会凑进大量水片段；而一场 5 小时的马拉松直播
    只允许挑 4 条又会漏掉大量爆笑名场面。这里的分档算法能随视频长度自动伸缩，
    同时完全支持通过传参手动覆盖。
    """
    if total_duration_sec <= 0:
        raise ValueError(f"total_duration_sec 必须大于 0，当前传入：{total_duration_sec}")

    if total_duration_sec < 1800:
        duration_tier = "< 30分钟（短回放）"
        auto_top_k = 4
        auto_min_sec = 15
        auto_max_sec = 45
    elif total_duration_sec <= 7200:
        duration_tier = "30分钟 ~ 2小时（常规直播）"
        auto_top_k = 8
        auto_min_sec = 20
        auto_max_sec = 60
    elif total_duration_sec <= 14400:
        duration_tier = "2小时 ~ 4小时（长直播）"
        auto_top_k = 10
        auto_min_sec = 25
        auto_max_sec = 75
    else:
        duration_tier = "> 4小时（超长直播）"
        hours = total_duration_sec / 3600.0
        auto_top_k = min(15, max(10, round(hours * 2.5)))
        auto_min_sec = 25
        auto_max_sec = 90

    # 如果处于 Step 2 的 peaks 锚点模式且用户没手动指定条数，
    # 目标条数不超过已有候选窗口上限（保底至少 3 条），避免强迫大模型硬凑
    if top_k is None and candidate_window_count > 0:
        auto_top_k = min(auto_top_k, max(candidate_window_count, 3))

    target_count = auto_top_k if top_k is None else int(top_k)
    final_min_sec = auto_min_sec if min_clip_sec is None else int(min_clip_sec)
    final_max_sec = auto_max_sec if max_clip_sec is None else int(max_clip_sec)

    if target_count < 1:
        raise ValueError(f"目标筛选条数 top_k 必须大于等于 1，当前为：{target_count}")
    if final_min_sec <= 0 or final_min_sec >= final_max_sec:
        raise ValueError(
            f"切片时长范围非法：min_clip_sec ({final_min_sec}) 必须大于 0 且小于 max_clip_sec ({final_max_sec})"
        )

    return {
        "target_count": target_count,
        "min_clip_sec": final_min_sec,
        "max_clip_sec": final_max_sec,
        "duration_tier": duration_tier,
        "is_auto_count": top_k is None,
        "is_auto_clip_range": (min_clip_sec is None and max_clip_sec is None),
    }


def _format_transcript_line(seg: dict[str, Any]) -> str:
    """将单条字幕格式化为大模型最易精准读取的文本行。"""
    s_sec = float(seg["start_sec"])
    e_sec = float(seg["end_sec"])
    s_str = seg.get("start_str") or format_timestamp(s_sec)
    e_str = seg.get("end_str") or format_timestamp(e_sec)
    vol_tag = (
        f" [HIGH_VOL rms={float(seg.get('peak_rms', 0.0)):.4f}]"
        if seg.get("is_high_volume")
        else ""
    )
    text = str(seg.get("text", "")).strip()
    return f"[{s_sec:.2f}-{e_sec:.2f} | {s_str}-{e_str}]{vol_tag} {text}"


def _pack_lines_into_chunks(lines: list[str], max_chars_per_chunk: int) -> list[str]:
    """将若干文本行按最大字符数贪心打包成多块，保证不丢失任何一行。"""
    chunks: list[str] = []
    current_lines: list[str] = []
    current_len = 0

    for line in lines:
        line_len = len(line) + 1
        if current_lines and current_len + line_len > max_chars_per_chunk:
            chunks.append("\n".join(current_lines))
            current_lines = [line]
            current_len = line_len
        else:
            current_lines.append(line)
            current_len += line_len

    if current_lines:
        chunks.append("\n".join(current_lines))
    return chunks


def build_transcript_chunks(
    segments: list[dict[str, Any]],
    candidate_windows: list[dict[str, Any]] | None = None,
    max_chars_per_chunk: int = 16000,
) -> list[str]:
    """将字幕列表整理并按字符上限分块，尽量保持同一个候选窗口不被从中间腰斩。

    参数：
        segments: Step 2 输出的字幕条目列表。
        candidate_windows: Step 2 `peaks` 模式输出的候选窗口元数据列表（可选）。
        max_chars_per_chunk: 每次发给大模型的最大字符数（默认 16000，兼容绝大多数模型上下文）。
    返回：
        分块后的字幕文本列表，每块可直接填入提示词发送给大模型。
    """
    if max_chars_per_chunk <= 0:
        raise ValueError(f"max_chars_per_chunk 必须大于 0，当前传入：{max_chars_per_chunk}")
    if not segments:
        return []

    windows_by_id: dict[int, dict[str, Any]] = {
        int(w["window_id"]): w for w in (candidate_windows or []) if "window_id" in w
    }
    has_window_ids = bool(windows_by_id) and any(
        seg.get("window_id") is not None for seg in segments
    )

    if not has_window_ids:
        all_lines = [_format_transcript_line(seg) for seg in segments]
        return _pack_lines_into_chunks(all_lines, max_chars_per_chunk)

    # 先按 window_id 保持原有顺序分组，把每个完整窗口拼成一个逻辑块
    grouped_blocks: list[list[str]] = []
    current_key: Any = object()
    current_block: list[str] = []

    for seg in segments:
        win_id = seg.get("window_id")
        if win_id != current_key:
            if current_block:
                grouped_blocks.append(current_block)
            current_key = win_id
            current_block = []
            if win_id is not None:
                win = windows_by_id.get(int(win_id))
                if win:
                    w_start = float(win["start_sec"])
                    w_end = float(win["end_sec"])
                    header = (
                        f"=== 候选窗口 #{win['window_id']} "
                        f"[{w_start:.2f}-{w_end:.2f} | {win['start_str']}-{win['end_str']}] "
                        f"(最高RMS: {float(win['max_peak_rms']):.4f}) ==="
                    )
                else:
                    header = f"=== 候选窗口 #{win_id} ==="
                current_block.append(header)
        current_block.append(_format_transcript_line(seg))

    if current_block:
        grouped_blocks.append(current_block)

    # 按窗口整体打包；如果某个单窗口本身就超过字符上限，再按行拆开
    chunks: list[str] = []
    active_text_blocks: list[str] = []
    active_len = 0

    for block_lines in grouped_blocks:
        block_text = "\n".join(block_lines)
        block_len = len(block_text) + 2

        if len(block_text) > max_chars_per_chunk:
            if active_text_blocks:
                chunks.append("\n\n".join(active_text_blocks))
                active_text_blocks = []
                active_len = 0
            chunks.extend(_pack_lines_into_chunks(block_lines, max_chars_per_chunk))
            continue

        if active_text_blocks and active_len + block_len > max_chars_per_chunk:
            chunks.append("\n\n".join(active_text_blocks))
            active_text_blocks = [block_text]
            active_len = block_len
        else:
            active_text_blocks.append(block_text)
            active_len += block_len

    if active_text_blocks:
        chunks.append("\n\n".join(active_text_blocks))

    return chunks


def build_llm_prompts(
    chunk_text: str,
    plan: dict[str, Any],
    video_name: str,
    chunk_index: int = 1,
    total_chunks: int = 1,
    source_language: str | None = None,
) -> tuple[str, str]:
    """构建 Step 3 高光精选与中文审片摘要的系统提示词与用户提示词。"""
    target_count = plan["target_count"]
    min_sec = plan["min_clip_sec"]
    max_sec = plan["max_clip_sec"]

    system_prompt = (
        "你是一位经验丰富的游戏直播切片主编与双语短视频策划。\n"
        "你的任务是从带精确秒数时间戳和 [HIGH_VOL] 音量峰值标记的直播字幕稿中，"
        "挑选出真正值得剪成爆款短视频的高光片段，并整理成中文审片清单供专业剪辑师 1 分钟快速审核。\n\n"
        "必须严格遵守的筛选原则：\n"
        "1. 真实性：start_sec 和 end_sec 必须严格落在本段提供的字幕时间范围内，严禁编造不存在的时间点。\n"
        "2. 精准修剪入出点：不要机械照抄整个候选窗口的起止边界！请根据实际台词内容，果断裁掉开头上一个话题的残话、\n"
        "   长时间无台词空白或结尾拖沓废话，让切片一秒入戏、在包袱或情绪峰值落幕后利落收尾。\n"
        "3. 完整性：优先挑选有完整起因、包袱/操作反转、高能反应或笑点收尾的片段，切忌在一句话说到一半时生硬截断。\n"
        f"4. 时长控制：单段切片建议控制在 {min_sec} ~ {max_sec} 秒之间（只有为保持剧情完整才可微幅浮动）。\n"
        "5. 宁缺毋滥：纯无意义的咳嗽、杂音、口水废话或纯游戏 NPC 旁白即使标了 HIGH_VOL 也不要选；本段没好片段可少选或不选。\n"
        "6. 输出格式：只允许输出合法 JSON 对象（不要输出 markdown 解释或前后废话），顶层结构必须为 {\"highlights\": [...]}。"
    )

    part_info = (
        f"第 {chunk_index}/{total_chunks} 部分字幕"
        if total_chunks > 1
        else "完整待筛高光字幕"
    )

    # 粗筛阶段自动识别源语种并同步做轻量 ASR 误听与公会专属词库检索，防止粗筛标题/梗概被同音误听带偏
    detected_lang = (source_language or "").strip() or detect_source_language(chunk_text)
    screen_refs = retrieve_translation_references(
        chunk_text,
        source_language=detected_lang,
        max_terms=8,
        max_idioms=4,
        max_exemplars=0,
    )
    screen_glossary_block = format_rag_reference_block(screen_refs)
    screen_glossary_section = f"\n{screen_glossary_block}\n\n" if screen_glossary_block else "\n"

    user_prompt = (
        f"请分析以下视频的{part_info}，从中精选最多 {target_count} 个最具传播潜力的高光切片。\n"
        f"- 视频名称：{video_name}\n"
        f"- 时长档位：{plan['duration_tier']}\n"
        f"- 单段建议时长：{min_sec} ~ {max_sec} 秒\n"
        f"{screen_glossary_section}"
        "每条高光对象必须包含以下字段：\n"
        "- start_sec (float): 切片起始秒数（必须来自字幕真实时间范围）\n"
        "- end_sec (float): 切片结束秒数（必须大于 start_sec）\n"
        "- score (float): 综合爆款推荐分，1.0 ~ 10.0（9 分以上代表极佳节目效果）\n"
        "- title_zh (string): 适合 B 站 / 抖音的中文吸睛标题（12~28 字）\n"
        "- title_en (string): 地道自然的英文短视频标题\n"
        "- summary_zh (string): 给剪辑师看的中文一句话剧情梗概（交代前因、发生什么、爆点在哪，25~65 字）\n"
        "- reason (string): 中文入选理由（说明情绪、操作或梗点为何值得剪）\n"
        "- tags (list[string]): 2~4 个简短中文标签（例如 [\"爆笑吐槽\", \"情绪爆炸\"]）\n\n"
        "返回 JSON 格式示例：\n"
        "{\n"
        '  "highlights": [\n'
        "    {\n"
        '      "start_sec": 124.5,\n'
        '      "end_sec": 168.0,\n'
        '      "score": 9.2,\n'
        '      "title_zh": "残血极限一穿三！主播破音狂喜拍桌",\n'
        '      "title_en": "1 HP Triple Kill Clutch! Streamer Loses It",\n'
        '      "summary_zh": "主播残血被三人围堵原本已经放弃，结果极限反杀完成翻盘，当场兴奋破音大喊。",\n'
        '      "reason": "逆风反转张力拉满且伴随明显音量爆发，前因后果完整，极具短视频传播力。",\n'
        '      "tags": ["残血反杀", "名场面", "情绪拉满"]\n'
        "    }\n"
        "  ]\n"
        "}\n\n"
        "以下是带时间戳的字幕输入（格式为 [开始秒-结束秒 | 可读时间] [可选高能标记] 台词）：\n"
        "----------------------------------------\n"
        f"{chunk_text}\n"
        "----------------------------------------"
    )
    return system_prompt, user_prompt


def _extract_json_payload(raw_text: str) -> dict[str, Any]:
    """从大模型原始回复中容错提取 JSON 对象（自动剥离 Markdown 代码块、修复尾逗号并输出头尾诊断信息）。"""
    text = (raw_text or "").strip()
    if not text:
        raise ValueError("大模型返回内容为空，无法解析高光 JSON。")

    # 尝试剥离 ```json ... ``` 或 ``` ... ``` 代码块围栏
    fence_match = re.search(r"```(?:json)?\s*([\s\S]*?)\s*```", text, flags=re.IGNORECASE)
    if fence_match:
        text = fence_match.group(1).strip()

    candidates = [text]
    # 如果前后仍夹杂说明性文字，截取最外层的 {...} 或 [...]
    first_brace = text.find("{")
    last_brace = text.rfind("}")
    if first_brace != -1 and last_brace > first_brace:
        candidates.append(text[first_brace : last_brace + 1])

    first_bracket = text.find("[")
    last_bracket = text.rfind("]")
    if first_bracket != -1 and last_bracket > first_bracket:
        candidates.append(text[first_bracket : last_bracket + 1])

    last_err: json.JSONDecodeError | None = None
    for candidate in candidates:
        for attempt_text in (candidate, re.sub(r",\s*([}\]])", r"\1", candidate)):
            try:
                parsed = json.loads(attempt_text)
            except json.JSONDecodeError as exc:
                last_err = exc
                continue

            if isinstance(parsed, dict):
                return parsed
            if isinstance(parsed, list):
                return {"highlights": parsed}

    compact = re.sub(r"\s+", " ", text)
    if len(compact) <= 280:
        preview = compact
    else:
        preview = f"{compact[:140]} ... {compact[-120:]}"
    truncated_hint = "（结尾未闭合，疑似输出被长度上限截断）" if not text.endswith(("}", "]")) else ""
    err_detail = f" [JSON错误: {last_err.msg} @ pos {last_err.pos}]" if last_err else ""
    raise ValueError(
        f"大模型返回内容无法解析为合法 JSON（共 {len(text)} 字符）{truncated_hint}{err_detail}：{preview}"
    )


def _to_float(value: Any) -> float | None:
    """安全将输入转为有限浮点数，失败则返回 None。"""
    try:
        num = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(num):
        return None
    return num


def _normalize_tags(raw_tags: Any) -> list[str]:
    """将 tags 字段清洗为去重后的字符串列表。"""
    if isinstance(raw_tags, list):
        items = [str(item).strip() for item in raw_tags if str(item).strip()]
    elif isinstance(raw_tags, str):
        items = [part.strip() for part in re.split(r"[,，、;；]+", raw_tags) if part.strip()]
    else:
        items = []

    seen: set[str] = set()
    unique: list[str] = []
    for item in items:
        if item not in seen:
            seen.add(item)
            unique.append(item)
    return unique[:6]


def parse_and_validate_highlights(
    raw_items: list[dict[str, Any]],
    total_duration_sec: float,
    target_count: int,
    overlap_ratio_threshold: float = 0.6,
) -> list[dict[str, Any]]:
    """校验、清洗大模型输出的候选高光，进行重叠去重并补齐审片字段。

    处理规则：
    1. 丢弃起止时间缺失、倒挂、或不足 2 秒的无效项；并将时间裁剪到 [0, total_duration_sec]。
    2. 按评分 score 从高到低排序。
    3. 计算候选之间重叠比例，若与已选高分片段重叠 >= overlap_ratio_threshold 则去重跳过。
    4. 截取前 target_count 条，注入 `rank`（1 起）与 `selected: True`（人工审片默认勾选开关）。
    """
    if target_count < 1:
        raise ValueError(f"target_count 必须大于等于 1，当前传入：{target_count}")

    cleaned: list[dict[str, Any]] = []
    for item in raw_items:
        if not isinstance(item, dict):
            continue

        raw_start = _to_float(item.get("start_sec"))
        raw_end = _to_float(item.get("end_sec"))
        if raw_start is None or raw_end is None:
            continue

        start_sec = max(0.0, raw_start)
        end_sec = min(float(total_duration_sec), raw_end) if total_duration_sec > 0 else raw_end
        if end_sec - start_sec < 2.0:
            continue

        raw_score = _to_float(item.get("score"))
        score = 5.0 if raw_score is None else max(1.0, min(10.0, raw_score))

        title_zh = str(item.get("title_zh") or item.get("title") or "").strip()
        title_en = str(item.get("title_en") or "").strip()
        summary_zh = str(item.get("summary_zh") or "").strip()
        reason = str(item.get("reason") or "").strip()
        if not title_zh and title_en:
            title_zh = title_en
        if not title_zh:
            title_zh = f"高光片段 {format_timestamp(start_sec)}"
        if not summary_zh:
            summary_zh = reason or title_zh

        cleaned.append(
            {
                "start_sec": round(start_sec, 2),
                "end_sec": round(end_sec, 2),
                "score": round(score, 1),
                "title_zh": title_zh,
                "title_en": title_en,
                "summary_zh": summary_zh,
                "reason": reason,
                "tags": _normalize_tags(item.get("tags")),
            }
        )

    # 先按评分降序排列，同分时靠前的片段优先
    cleaned.sort(key=lambda h: (-h["score"], h["start_sec"]))

    deduped: list[dict[str, Any]] = []
    for cand in cleaned:
        c_start = cand["start_sec"]
        c_end = cand["end_sec"]
        c_len = c_end - c_start

        is_duplicate = False
        for kept in deduped:
            k_start = kept["start_sec"]
            k_end = kept["end_sec"]
            inter = max(0.0, min(c_end, k_end) - max(c_start, k_start))
            min_len = min(c_len, k_end - k_start)
            if min_len > 0 and (inter / min_len) >= overlap_ratio_threshold:
                is_duplicate = True
                break

        if not is_duplicate:
            start_rounded = cand["start_sec"]
            end_rounded = cand["end_sec"]
            deduped.append(
                {
                    "rank": len(deduped) + 1,
                    "selected": True,  # 人工审片开关：不想要某条时改为 false，Step 4 会自动跳过
                    "start_sec": start_rounded,
                    "end_sec": end_rounded,
                    "duration_sec": round(end_rounded - start_rounded, 2),
                    "start_str": format_timestamp(start_rounded),
                    "end_str": format_timestamp(end_rounded),
                    "score": cand["score"],
                    "title_zh": cand["title_zh"],
                    "title_en": cand["title_en"],
                    "summary_zh": cand["summary_zh"],
                    "reason": cand["reason"],
                    "tags": cand["tags"],
                }
            )
            if len(deduped) >= target_count:
                break

    return deduped


def call_openai_compatible_llm(
    system_prompt: str,
    user_prompt: str,
    api_key: str,
    base_url: str | None,
    model: str,
    timeout: float = 120.0,
    reasoning_effort: str = "",
    role: str = "screen",
    usage_tracker: TokenUsageTracker | None = None,
) -> str:
    """调用任意 OpenAI 兼容接口（内置连接池复用、429/5xx 指数退避、思考开关与 Token 统计）。"""
    return DEFAULT_LLM_CLIENT.complete_json(
        system_prompt=system_prompt,
        user_prompt=user_prompt,
        api_key=api_key,
        base_url=base_url,
        model=model,
        temperature=0.3,
        max_tokens=8192,
        reasoning_effort=reasoning_effort,
        role=role,
        usage_tracker=usage_tracker,
        timeout=timeout,
    )


def _collect_quotes_for_highlight(
    highlight: dict[str, Any],
    transcript_segments: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """从 Step 2 字幕列表中捞出与当前高光区间有重叠的原文台词，方便人工审片核对。"""
    h_start = float(highlight["start_sec"])
    h_end = float(highlight["end_sec"])
    matched: list[dict[str, Any]] = []
    for seg in transcript_segments:
        s_start = float(seg["start_sec"])
        s_end = float(seg["end_sec"])
        if max(h_start, s_start) < min(h_end, s_end):
            matched.append(seg)
    return matched


def split_raw_subtitle_line(
    seg: dict[str, Any],
    max_duration_sec: float = 4.5,
    max_chars: int = 48,
) -> list[dict[str, Any]]:
    """将单条可能过长的原始字幕拆分成适合屏幕显示与翻译的 2~4 秒紧凑短句。

    直接复用 Step 2 的词界/标点平滑断句逻辑，确保即使加载旧版未拆句的 transcript.json，
    也能自动变为紧凑易读的短句。
    """
    s_start = float(seg.get("start_sec", 0.0))
    s_end = float(seg.get("end_sec", s_start))
    text = str(seg.get("text", "")).strip()
    if not text:
        return []

    sub_items = split_long_segment_by_words(
        text=text,
        start_sec=s_start,
        end_sec=s_end,
        words=None,
        max_duration_sec=max_duration_sec,
        max_chars=max_chars,
    )

    out: list[dict[str, Any]] = []
    for sub in sub_items:
        sub_start = float(sub["start_sec"])
        sub_end = float(sub["end_sec"])
        out.append(
            {
                "start_sec": sub_start,
                "end_sec": sub_end,
                "start_str": format_timestamp(sub_start),
                "end_str": format_timestamp(sub_end),
                "is_high_volume": bool(seg.get("is_high_volume", False)),
                "peak_rms": float(seg.get("peak_rms", 0.0)),
                "text": sub["text"],
            }
        )
    return out


def _prepare_highlight_raw_lines(
    highlight: dict[str, Any],
    transcript_segments: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """提取并时间裁剪落在当前高光区间内的 Step 2 原始台词（保留完整语流不做硬切碎）。"""
    h_start = float(highlight["start_sec"])
    h_end = float(highlight["end_sec"])
    matched = _collect_quotes_for_highlight(highlight, transcript_segments)

    raw_lines: list[dict[str, Any]] = []
    for seg in matched:
        clipped_start = max(h_start, float(seg["start_sec"]))
        clipped_end = min(h_end, float(seg["end_sec"]))
        text = str(seg.get("text", "")).strip()
        if clipped_end - clipped_start < 0.05 or not text:
            continue
        raw_lines.append(
            {
                "start_sec": round(clipped_start, 2),
                "end_sec": round(clipped_end, 2),
                "start_str": format_timestamp(clipped_start),
                "end_str": format_timestamp(clipped_end),
                "text": text,
                "is_high_volume": bool(seg.get("is_high_volume", False)),
                "peak_rms": float(seg.get("peak_rms", 0.0)),
            }
        )
    return raw_lines


_LANG_DISPLAY_NAMES: dict[str, str] = {
    "zh": "简体中文",
    "en": "地道母语英语",
    "ja": "自然口语日语",
    "ko": "自然口语韩语",
    "ru": "自然口语俄语",
    "es": "自然口语西班牙语",
}


def _normalize_single_lang_code(lang: str | None) -> str:
    """将任意语种别名规范化为标准 2 字母代码（zh/en/ja/ko/ru/es），空或 auto 返回空串。"""
    raw = (lang or "").strip().lower()
    if not raw or raw in {"auto", "unknown", "none"}:
        return ""
    if "_" in raw:
        raw = raw.split("_", 1)[0]
    if raw.startswith("zh") or raw in {"cmn", "zho", "cn", "chinese"}:
        return "zh"
    if raw.startswith("ja") or raw in {"jp", "jpn", "japanese"}:
        return "ja"
    if raw.startswith("ko") or raw in {"kr", "kor", "korean"}:
        return "ko"
    if raw.startswith("ru") or raw in {"rus", "russian"}:
        return "ru"
    if raw.startswith("es") or raw in {"spa", "spanish"}:
        return "es"
    if raw.startswith("en") or raw in {"eng", "english"}:
        return "en"
    return raw[:2]


def resolve_subtitle_language_pair(
    source_language: str | None = None,
    target_language: str | None = None,
    sample_text: str = "",
) -> tuple[str, str, str]:
    """解析源语种、目标译文语种与组合标识，例如 ('ja', 'zh', 'ja_zh') 或 ('zh', 'en', 'zh_en')。"""
    src_code = _normalize_single_lang_code(source_language)
    if not src_code:
        src_code = detect_source_language(sample_text) if sample_text else "en"
    tgt_code = _normalize_single_lang_code(target_language)
    if not tgt_code:
        tgt_code = "en" if src_code == "zh" else "zh"
    if tgt_code == src_code:
        tgt_code = "en" if src_code == "zh" else "zh"
    return src_code, tgt_code, f"{src_code}_{tgt_code}"


def _build_lang_specific_style_guide(lang_pair: str, src_code: str = "en", tgt_code: str = "zh") -> str:
    """按源语种与目标语种动态返回对应顶级母语字幕组风格的专属意译铁律。"""
    guides: dict[str, str] = {
        "en_zh": (
            "   - 【欧美主播英→中母语准则】：彻底抛弃英语从句语序与字面直译！"
            "如沉迷语境下 \"sucked me in\" 意译为“让我玩上头了 / 彻底陷进去了”；"
            "\"out of context\" 意译为“断章取义 / 单拎出来听太离谱”；\"God forbid...\" 译出自然反讽口吻；"
            "\"we're cooked / let him cook / skill issue / built different\" 等直播热梗必须用中文玩家原生黑话表达。"
        ),
        "zh_en": (
            "   - 【国产游戏/中文主播出海中→英母语准则】：彻底杜绝 Chinglish 中式直译！"
            "必须像美国/欧洲原生 Twitch 头部剪辑师一样自然精炼："
            "如“破防了/红温”译为 \"mentally boomed / tilted / malding\"，“寄了”译为 \"we're cooked / it's so over\"，"
            "“下饭操作”译为 \"what a throw / clown gameplay\"，“稳如老狗”译为 \"clean as hell / locked in\"，"
            "“有手就行”译为 \"free / light work\"，“别指指点点”译为 \"stop backseating\"。"
        ),
        "ja_zh": (
            "   - 【日V/日系游戏日→中烤肉曼母语准则】：精准保留主播人设萌点、敬语/关西腔反差与吐槽节奏！"
            "严禁词典死译：如「草 / 草生える」译为“笑死/哈哈哈哈”（绝不译“长草”）；"
            "「やばい」按语境灵活切换为“卧槽太绝了 / 完了要寄 / 太离谱了”；"
            "「てぇてぇ / 尊い」译为“太甜了磕到了”；「助かる」在福利/可爱瞬间译为“感谢款待太舒服了”；"
            "「台パン」译为“砸桌子破防”；日语省略主语处请结合上下文自然补全，且译文严禁残留任何日文假名！"
        ),
        "ko_zh": (
            "   - 【韩服电竞/韩区主播韩→中字幕组母语准则】：精准还原半语互怼与敬语礼貌的戏剧反差！"
            "严禁机械直译：句首独立「아니」译为“不是哥们 / 喂凭什么啊”（绝不死译“不”）；"
            "夸赞语境下「미쳤어/미쳤다」译为“太变态了/牛逼炸了”；「찢었다」译为“杀疯了/秀翻全场”；"
            "「폼 미쳤다」译为“状态无敌拉满”；「킹받네」译为“血压拉满太搞心态”；"
            "「억까」译为“纯纯针对演我”；「뇌절」译为“上头强送”；译文严禁残留任何韩文谚文！"
        ),
        "ru_zh": (
            "   - 【俄区 CS2/Dota2/塔科夫俄→中字幕组母语准则】：译出俄区硬核干脆、自带毛式幽默与高能爆发的节奏！"
            "「жесть」译为“卧槽太离谱了”；「пипец/капец」译为“完蛋彻底寄了”；"
            "「бомбит/подгорело」译为“彻底红温炸毛了”；「изи」译为“有手就行轻松拿捏”；"
            "「красава」译为“漂亮兄弟！”；高频俄语粗口请自然软化为中文主播常用高能感叹（卧槽/我靠/去你的/完蛋），"
            "且译文严禁残留任何西里尔字母！"
        ),
        "es_zh": (
            "   - 【西语/拉美 Twitch 主播西→中字幕组母语准则】：充分传达西语主播热情奔放、语速飞快的夸张感染力！"
            "兼顾西班牙与拉美口语：「no manches / no mames」译为“不是吧卧槽/离大谱”；"
            "「hostia / joder」按语境译为“我靠/卧槽太牛逼/气死我了”；「la liaste」译为“捅大篓子搞砸了”；"
            "「qué locura / brutal」译为“太疯了太猛了”；「manco」译为“菜得抠脚/手残下饭”；「qué crack」译为“顶级大神”；"
            "译文严禁残留未译西语！"
        ),
    }
    if lang_pair in guides:
        return guides[lang_pair]
    src_name = _LANG_DISPLAY_NAMES.get(src_code, src_code.upper())
    tgt_name = _LANG_DISPLAY_NAMES.get(tgt_code, tgt_code.upper())
    return (
        f"   - 【跨语种 {src_name} → {tgt_name} 母语字幕组准则】：彻底抛弃逐词机翻腔，"
        f"精准传达游戏/直播场景的情绪张力、电竞术语与母语级口语节奏，译文必须完全采用自然流畅的{tgt_name}表达，绝不可残留未译源语言！"
    )


def _build_bilingual_prompts(
    batch_entries: list[dict[str, Any]],
    source_language: str | None = None,
    target_language: str | None = None,
    hotwords: str = "",
    extra_context: str = "",
    quality_feedback: str = "",
) -> tuple[str, str]:
    """构建商业级多语种高光字幕「整段语义意群重组 + 去口吃清洗 + 听写纠错 + 母语级意译 + 零漏翻」提示词。"""
    raw_combined_speech = " ".join(
        str(seg.get("text", ""))
        for entry in batch_entries
        for seg in entry.get("raw_lines", [])
    )
    src_code, tgt_code, lang_pair = resolve_subtitle_language_pair(
        source_language=source_language,
        target_language=target_language,
        sample_text=raw_combined_speech,
    )
    src_name = _LANG_DISPLAY_NAMES.get(src_code, src_code.upper())
    tgt_name = _LANG_DISPLAY_NAMES.get(tgt_code, tgt_code.upper())
    is_zh_to_en = src_code == "zh" and tgt_code == "en"
    lang_style_guide = _build_lang_specific_style_guide(lang_pair, src_code=src_code, tgt_code=tgt_code)
    clean_hotwords = (hotwords or "").strip()
    clean_context = (extra_context or "").strip()
    clean_feedback = (quality_feedback or "").strip()

    if is_zh_to_en:
        field_rule = (
            "   - text_zh 填写去口吃、修正错别字后的中文原台词；text_en 填写地道母语级英文译文。\n"
            "   - 【零漏翻铁律（中→英）】：每一条字幕的 text_en 都必须翻译为地道母语英语，严禁留空、严禁残留中文汉字；\n"
            "     同时必须完整覆盖原始台词从开头到结尾的每一句有效对话，绝不允许跳过或漏译！"
        )
    elif tgt_code == "zh":
        field_rule = (
            f"   - text_en 填写去口吃并校对后的{src_name}原文（供双语对照）；text_zh 填写纯正简体中文译文。\n"
            "   - 【零漏翻铁律（外→中）】：每一条字幕的 text_zh 都必须翻译为纯正简体中文，严禁留空、严禁直接照抄外语原文、"
            "严禁在 text_zh 中残留日文假名/韩文谚文/西里尔字母或整句未译英西语！\n"
            "     同时必须完整覆盖原始台词从开头到结尾的每一句有效对话（只能清理纯口吃杂音），绝不允许漏翻任何一句！"
        )
    else:
        field_rule = (
            f"   - 跨语种搭配（{src_name} → {tgt_name}）：text_zh 固定作为【主译文行】填写纯正地道的{tgt_name}译文；"
            f"text_en 固定作为【副原文行】填写整理校对后的{src_name}原文（也可同步给出 text_tgt 与 text_src）。\n"
            f"   - 【零漏翻铁律】：每一条字幕的主译文行都必须完整翻译为纯正{tgt_name}，严禁留空或残留未译{src_name}，并覆盖全段有效对话！"
        )

    system_prompt = (
        "你是 B 站 / 抖音 / YouTube 百万粉多语种游戏切片频道的首席双语字幕组总监，出品标准必须达到真人母语字幕组的商业交付水准。\n"
        "你的任务是结合每段切片的标题、剧情梗概、补充背景与带秒级时间轴的语音识别（ASR）原始稿，"
        "完成【整句意群重组 + 去口吃清洗 + 语境听写纠错 + 母语级本土化双语意译】，输出干脆利落、零漏翻的高品质双语字幕。\n\n"
        "必须严格遵守的六大商业交付铁律：\n"
        "1. 【严禁 1:1 机械逐行直译与保留碎句】：\n"
        "   - 绝不能原封不动照抄原始 ASR 行数和时间切分！先通读整段发言，再按【完整语义意群】重新合并与断句。\n"
        "   - 彻底清除或吸收无意义口吃、结巴与填充词（如 I, I, / like, / uh, / um, / えーと / あの / 어 / 呃 / 嗯 等），\n"
        "     绝不允许出现“我，我，”“我需要像，”“像其他，呃，”“我就想，”这类垃圾残句单独成条！\n"
        "   - 严禁在介词、冠词、连词、代词、助词处生硬腰斩句子，严禁腰斩专有名词。\n"
        "2. 【黄金字幕节奏与全覆盖时间轴】：\n"
        "   - 每条字幕理想时长为 1.5 ~ 5.5 秒、视觉字数紧凑清晰（简短爆点感叹除外）。\n"
        "   - 禁止输出 < 1.0 秒且仅 1~3 个字的碎词字幕（除非是独立成梗的爆笑尖叫/感叹句且前后有明显停顿）。\n"
        "   - 每条字幕的 start_sec 与 end_sec 必须对照原始时间轴平滑覆盖，严格落在该高光片段合法时间范围内，严格递增、互不重叠、不留有效台词黑洞。\n"
        "3. 【语境听写纠错 + 专词前后绝对统一】：\n"
        "   - 结合标题、梗概、热词表、本地专家术语库与上下文积极修正 ASR 同音误听（游戏名、主播名、电竞术语、直播黑话）。\n"
        "   - 同一片段内出现的同一个游戏名、人名、固定梗，前后译名与原文拼写必须 100% 保持统一。\n"
        "4. 【真人母语字幕组级地道意译（严禁机翻腔）】：\n"
        f"{lang_style_guide}\n"
        "   - 忠于主播原意与情绪张力，绝不凭空编造原话中没有的事实。\n"
        "5. 【商业短视频标点规范】：\n"
        "   - 字幕末尾不要保留丑陋的逗号、顿号或孤立句号（感叹号！、问号？、省略号…… 可按情绪保留），句内自然用空格或逗号停顿。\n"
        "6. 【双语精准对齐与零漏翻底线】：\n"
        f"{field_rule}\n\n"
        "输出格式要求：\n"
        "- 只允许输出合法 JSON 对象（不要输出任何 Markdown 解释或前后废话），格式严格为：\n"
        '  {"clips": [{"rank": 1, "subtitles": [{"start_sec": 1135.0, "end_sec": 1138.84, "text_en": "...", "text_zh": "..."}]}]}'
    )

    lines: list[str] = [
        f"语种搭配：{src_name}（{src_code}）→ {tgt_name}（{tgt_code}）。请对以下高光片段执行商业级整句重组、去口吃、听写纠错与母语级零漏翻双语意译，返回 JSON：",
    ]
    if clean_hotwords:
        lines.append(f"当前直播专有术语/热词参考（纠错与译名统一时优先采用）：{clean_hotwords}")
    if clean_context:
        lines.append(f"人工补充背景语境（务必严格遵守）：{clean_context}")
    if clean_feedback:
        lines.append(f"【质检打回整改要求】：{clean_feedback}")

    # 自动调用本地毫秒级六语种专家语料库 RAG：精准匹配 6 大语种对；非内置对则复用源语种误听与术语纠偏
    combined_for_rag = " ".join(
        [
            entry.get("title_zh", "") + " " + entry.get("summary_zh", "") + " "
            + " ".join(str(seg.get("text", "")) for seg in entry.get("raw_lines", []))
            for entry in batch_entries
        ]
    )
    direct_builtin = lang_pair in {"en_zh", "zh_en", "ja_zh", "ko_zh", "ru_zh", "es_zh"}
    rag_refs = retrieve_translation_references(
        combined_for_rag,
        source_language=lang_pair if direct_builtin else src_code,
        max_terms=10,
        max_idioms=8 if direct_builtin else 0,
        max_exemplars=6 if direct_builtin else 0,
    )
    rag_block = format_rag_reference_block(rag_refs)
    if rag_block:
        lines.append("")
        lines.append(rag_block)

    lines.append("")

    for entry in batch_entries:
        lines.append(
            f"### 片段 #{entry['rank']}《{entry['title_zh']}》"
            f"（合法时间范围：{entry['start_sec']:.2f}s ~ {entry['end_sec']:.2f}s，原始共 {len(entry['raw_lines'])} 句，请按完整语义合并精简并确保 100% 零漏翻）\n"
            f"剧情梗概：{entry['summary_zh']}\n"
            "原始时间轴台词："
        )
        for seg in entry["raw_lines"]:
            lines.append(f"- [{seg['start_sec']:.2f} - {seg['end_sec']:.2f}] {seg['text']}")
        lines.append("")

    return system_prompt, "\n".join(lines)


def _parse_rank_int(raw_rank: Any) -> int | None:
    """容错解析大模型返回的排名字段（支持 1、'1'、'#1' 等格式）。"""
    if isinstance(raw_rank, bool):
        return None
    if isinstance(raw_rank, int):
        return raw_rank
    text = str(raw_rank or "").strip()
    if not text:
        return None
    match = re.search(r"\d+", text)
    if not match:
        return None
    try:
        return int(match.group(0))
    except ValueError:
        return None


def _match_orig_text_for_span(
    start_sec: float,
    end_sec: float,
    raw_lines: list[dict[str, Any]],
) -> str:
    """从原始 ASR 台词中找出与新时间窗重叠的原文拼合留底，便于人工溯源。"""
    matched: list[str] = []
    for raw in raw_lines:
        r_start = float(raw["start_sec"])
        r_end = float(raw["end_sec"])
        if max(start_sec, r_start) < min(end_sec, r_end):
            txt = str(raw.get("text", "")).strip()
            if txt:
                matched.append(txt)
    return " ".join(matched).strip()


_CJK_RE = re.compile(r"[\u4e00-\u9fff]")
_JP_KANA_RE = re.compile(r"[\u3040-\u30ff]")
_KR_HANGUL_RE = re.compile(r"[\uac00-\ud7af]")
_CYRILLIC_RE = re.compile(r"[\u0400-\u04ff]")
_LATIN_WORD_RE = re.compile(r"[A-Za-zÁÉÍÓÚÜÑáéíóúüñ]{2,}")
_EN_ASCII_WORD_RE = re.compile(r"[A-Za-z]{2,}")
_LAZY_ZH_FRAGMENT_RE = re.compile(
    r"(?:^|[，。,\s])(?:我需要像|像其他[，,\s]*呃|我[，,\s]+我|但是[，,\s]*呃|我就想|呃|嗯)[，。,\s]*$"
)
_NOISE_ONLY_RAW_RE = re.compile(
    r"^(?:uh+|um+|ah+|oh+|eh+|hm+|hmm+|mhm+|huh|ooh|woo+|ha+|haha+|hehe+|yeah|yep|nope|"
    r"ok|okay|like|so|well|and|but|the|a|i|呃+|嗯+|啊+|哦+|哈+|唉+|えー+と?|あの+|うーん|어+|음+|아+|으+|ну|эм)$",
    re.IGNORECASE,
)
_SKIP_SHORT_INTERJECTION_WORDS = frozenset(
    {
        "ok", "okay", "gg", "ggs", "ggwp", "lol", "lmao", "kekw", "bro", "bruh", "yo",
        "oh", "ah", "uh", "um", "no", "yes", "yeah", "yep", "nah", "wow", "whoa", "hey",
        "hi", "bye", "haha", "hahaha", "hehe", "nice", "wtf", "omg", "rip", "pog", "poggers",
    }
)


def _is_subtitle_untranslated(
    row: dict[str, Any],
    source_language: str | None = None,
    target_language: str | None = None,
    fallback_lang_pair: str | None = None,
) -> bool:
    """源/目标语种感知精准漏翻判定：堵死未译英文、日语假名残留、韩语谚文残留、俄语西里尔残留及任意跨语种未译。"""
    text_zh = str(row.get("text_zh") or "").strip()
    text_en = str(row.get("text_en") or "").strip()
    text_tgt_raw = str(row.get("text_tgt") or "").strip()
    text_src_raw = str(row.get("text_src") or "").strip()
    text_orig = str(row.get("text_orig") or text_src_raw or text_en or text_zh).strip()

    if fallback_lang_pair and "_" in fallback_lang_pair:
        src_code, tgt_code = fallback_lang_pair.split("_", 1)
    else:
        src_code, tgt_code, _ = resolve_subtitle_language_pair(
            source_language=source_language,
            target_language=target_language,
            sample_text=text_orig,
        )

    is_zh_to_en = src_code == "zh" and tgt_code == "en"
    # 解析当前待检验的译文文本（target）与源语言文本（source）
    if text_tgt_raw:
        tgt_text = text_tgt_raw
        src_text = text_src_raw or text_orig
    elif is_zh_to_en:
        tgt_text = text_en
        src_text = text_zh or text_orig
    else:
        tgt_text = text_zh
        src_text = text_en or text_orig

    # 1. 目标语为英语（en）
    if tgt_code == "en":
        if src_code == "zh" and len(_CJK_RE.findall(src_text)) < 2:
            return False
        if not tgt_text:
            return True
        if _CJK_RE.search(tgt_text) or _JP_KANA_RE.search(tgt_text) or _KR_HANGUL_RE.search(tgt_text) or _CYRILLIC_RE.search(tgt_text):
            return True
        if not _EN_ASCII_WORD_RE.search(tgt_text):
            return True
        return False

    # 2. 目标语为日语（ja）
    if tgt_code == "ja":
        if not tgt_text:
            return True
        if len(src_text) >= 6 and tgt_text.lower() == src_text.lower():
            return True
        if src_code in {"en", "es", "ru"} and not (_JP_KANA_RE.search(tgt_text) or _CJK_RE.search(tgt_text)):
            return True
        if src_code == "zh" and not _JP_KANA_RE.search(tgt_text) and len(_CJK_RE.findall(src_text)) >= 4:
            return True
        return False

    # 3. 目标语为韩语（ko）
    if tgt_code == "ko":
        if not tgt_text or not _KR_HANGUL_RE.search(tgt_text):
            return True
        return False

    # 4. 目标语为俄语（ru）
    if tgt_code == "ru":
        if not tgt_text or not _CYRILLIC_RE.search(tgt_text):
            return True
        return False

    # 5. 目标语为西班牙语（es）
    if tgt_code == "es":
        if not tgt_text or not _LATIN_WORD_RE.search(tgt_text):
            return True
        if _CJK_RE.search(tgt_text) or _JP_KANA_RE.search(tgt_text) or _KR_HANGUL_RE.search(tgt_text) or _CYRILLIC_RE.search(tgt_text):
            return True
        if len(src_text) >= 6 and tgt_text.lower() == src_text.lower():
            return True
        return False

    # 6. 目标语为简体中文（zh，默认）
    if not tgt_text:
        return True

    if src_code == "ja":
        if len(_JP_KANA_RE.findall(tgt_text)) >= 2:
            return True
        if not _CJK_RE.search(tgt_text) and len(_JP_KANA_RE.findall(src_text)) >= 2:
            return True
        return False

    if src_code == "ko":
        if _KR_HANGUL_RE.search(tgt_text):
            return True
        if not _CJK_RE.search(tgt_text) and len(_KR_HANGUL_RE.findall(src_text)) >= 2:
            return True
        return False

    if src_code == "ru":
        if len(_CYRILLIC_RE.findall(tgt_text)) >= 2:
            return True
        if not _CJK_RE.search(tgt_text) and len(_CYRILLIC_RE.findall(src_text)) >= 3:
            return True
        return False

    # 英语 / 西语 → 中文
    cjk_cnt = len(_CJK_RE.findall(tgt_text))
    zh_latin_words = _LATIN_WORD_RE.findall(tgt_text)
    orig_latin_words = _LATIN_WORD_RE.findall(src_text)
    substantive_orig = [w for w in orig_latin_words if w.lower() not in _SKIP_SHORT_INTERJECTION_WORDS]

    if cjk_cnt == 0:
        if len(substantive_orig) >= 2:
            return True
        if len(substantive_orig) == 1 and len(substantive_orig[0]) >= 5 and tgt_text.lower() == src_text.lower():
            return True
        return False

    if cjk_cnt <= 2 and len(zh_latin_words) >= 4:
        return True
    return False


def _is_substantive_raw_line(raw: dict[str, Any]) -> bool:
    """判断单条原始 ASR 字幕是否为需要被字幕覆盖翻译的实质对话（剔除纯口吃/杂音碎片）。"""
    dur = float(raw.get("end_sec", 0.0)) - float(raw.get("start_sec", 0.0))
    txt = str(raw.get("text") or "").strip()
    if dur < 0.42 or not txt:
        return False
    core = re.sub(r"[\s\.,!?;:\-~…\"'“”‘’()\[\]【】《》、，。！？¿¡]+", "", txt)
    if not core or len(core) < 2:
        return False
    if _NOISE_ONLY_RAW_RE.match(core):
        return False
    # 英文/西语：仅含 1 个极短虚词时视为口吃噪音
    latin_words = _LATIN_WORD_RE.findall(txt)
    non_latin_cnt = len(_CJK_RE.findall(txt)) + len(_JP_KANA_RE.findall(txt)) + len(_KR_HANGUL_RE.findall(txt)) + len(_CYRILLIC_RE.findall(txt))
    if non_latin_cnt == 0:
        meaningful = [w for w in latin_words if w.lower() not in _SKIP_SHORT_INTERJECTION_WORDS]
        if not meaningful:
            return False
        if len(latin_words) == 1 and len(latin_words[0]) <= 3:
            return False
    return True


def _find_uncovered_raw_lines(
    raw_lines: list[dict[str, Any]],
    validated: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """精准找出所有有实质台词内容但被大模型跳漏（时间覆盖 < 35% 且未被紧邻句吸收）的原声漏句。"""
    uncovered: list[dict[str, Any]] = []
    for raw in raw_lines:
        if not _is_substantive_raw_line(raw):
            continue
        r_start = float(raw["start_sec"])
        r_end = float(raw["end_sec"])
        r_dur = max(0.01, r_end - r_start)

        overlap = 0.0
        for sub in validated:
            ov = min(r_end, float(sub["end_sec"])) - max(r_start, float(sub["start_sec"]))
            if ov > 0:
                overlap += ov
        if (overlap / r_dur) >= 0.35:
            continue

        # 二次防误报：检查该句核心词汇是否已被 ±1.2s 内紧邻字幕在合并意群时吸收
        raw_txt = str(raw.get("text") or "").strip()
        raw_words = {
            w.lower()
            for w in _LATIN_WORD_RE.findall(raw_txt)
            if len(w) >= 3 and w.lower() not in _SKIP_SHORT_INTERJECTION_WORDS
        }
        raw_non_latin = re.findall(r"[\u4e00-\u9fff\u3040-\u30ff\uac00-\ud7af\u0400-\u04ff]{2,}", raw_txt)
        absorbed = False
        for sub in validated:
            s_s, s_e = float(sub["start_sec"]), float(sub["end_sec"])
            if s_e < r_start - 1.2 or s_s > r_end + 1.2:
                continue
            neighbor_blob = f"{sub.get('text_en', '')} {sub.get('text_orig', '')}".lower()
            if raw_words:
                hits = sum(1 for w in raw_words if w in neighbor_blob)
                if hits / len(raw_words) >= 0.60:
                    absorbed = True
                    break
            elif raw_non_latin:
                hits = sum(1 for seg in raw_non_latin if seg.lower() in neighbor_blob)
                if hits / len(raw_non_latin) >= 0.60:
                    absorbed = True
                    break
        if not absorbed:
            uncovered.append(raw)
    return uncovered


def _enforce_monotonic_subtitles(
    items: list[dict[str, Any]],
    h_start: float,
    h_end: float,
) -> list[dict[str, Any]]:
    """保证合并回填后的双语字幕时间戳严格落在合法区间内且单调递增互不重叠。"""
    sorted_items = sorted(items, key=lambda x: (float(x["start_sec"]), float(x["end_sec"])))
    out: list[dict[str, Any]] = []
    for cur in sorted_items:
        s = round(max(h_start, float(cur["start_sec"])), 2)
        e = round(min(h_end, float(cur["end_sec"])), 2)
        if e - s < 0.20:
            continue
        if out:
            prev = out[-1]
            if s < prev["end_sec"]:
                if e - prev["end_sec"] >= 0.25:
                    s = round(prev["end_sec"], 2)
                else:
                    mid = round(
                        min(e - 0.20, max(prev["start_sec"] + 0.25, (s + prev["end_sec"]) / 2.0)),
                        2,
                    )
                    if mid - prev["start_sec"] >= 0.25 and e - mid >= 0.20:
                        prev["end_sec"] = mid
                        s = mid
                    else:
                        continue
        item_copy = dict(cur)
        item_copy["start_sec"] = s
        item_copy["end_sec"] = e
        out.append(item_copy)
    return out


def _repair_missing_subtitles(
    entry: dict[str, Any],
    validated: list[dict[str, Any]],
    llm_caller: Callable[[str, str], str],
    source_language: str | None = None,
    target_language: str | None = None,
    hotwords: str = "",
    extra_context: str = "",
) -> list[dict[str, Any]]:
    """手术刀级定点补翻闭环：精准定位零星漏翻句与时间轴漏段，带前后上下文单独补翻并无缝回填。"""
    raw_combined = " ".join(str(seg.get("text", "")) for seg in entry.get("raw_lines", []))
    src_code, tgt_code, lang_pair = resolve_subtitle_language_pair(
        source_language=source_language,
        target_language=target_language,
        sample_text=raw_combined,
    )
    is_zh_to_en = src_code == "zh" and tgt_code == "en"
    src_name = _LANG_DISPLAY_NAMES.get(src_code, src_code.upper())
    tgt_name = _LANG_DISPLAY_NAMES.get(tgt_code, tgt_code.upper())

    untranslated_indices = [
        idx
        for idx, row in enumerate(validated)
        if _is_subtitle_untranslated(
            row,
            source_language=src_code,
            target_language=tgt_code,
            fallback_lang_pair=lang_pair,
        )
    ]
    uncovered_raw = _find_uncovered_raw_lines(entry["raw_lines"], validated)

    tasks: list[dict[str, Any]] = []
    for idx in untranslated_indices:
        row = validated[idx]
        prev_ctx = validated[idx - 1].get("text_tgt") or validated[idx - 1]["text_zh"] if idx > 0 else ""
        next_ctx = validated[idx + 1].get("text_tgt") or validated[idx + 1]["text_zh"] if idx + 1 < len(validated) else ""
        tasks.append(
            {
                "id": len(tasks) + 1,
                "mode": "retranslate",
                "target_idx": idx,
                "start_sec": float(row["start_sec"]),
                "end_sec": float(row["end_sec"]),
                "src_text": str(row.get("text_orig") or row.get("text_src") or row.get("text_en") or row.get("text_zh") or "").strip(),
                "prev_ctx": prev_ctx,
                "next_ctx": next_ctx,
            }
        )

    for raw in uncovered_raw:
        r_s = float(raw["start_sec"])
        r_e = float(raw["end_sec"])
        prev_sub = next((x for x in reversed(validated) if float(x["end_sec"]) <= r_s + 0.5), None)
        next_sub = next((x for x in validated if float(x["start_sec"]) >= r_e - 0.5), None)
        tasks.append(
            {
                "id": len(tasks) + 1,
                "mode": "insert",
                "target_idx": -1,
                "start_sec": r_s,
                "end_sec": r_e,
                "src_text": str(raw.get("text") or "").strip(),
                "prev_ctx": (prev_sub.get("text_tgt") or prev_sub["text_zh"]) if prev_sub else "",
                "next_ctx": (next_sub.get("text_tgt") or next_sub["text_zh"]) if next_sub else "",
            }
        )

    if not tasks or len(tasks) > 10:
        return validated

    repair_blob = " ".join(t["src_text"] for t in tasks)
    direct_builtin = lang_pair in {"en_zh", "zh_en", "ja_zh", "ko_zh", "ru_zh", "es_zh"}
    rag_refs = retrieve_translation_references(
        repair_blob,
        source_language=lang_pair if direct_builtin else src_code,
        max_terms=6,
        max_idioms=5 if direct_builtin else 0,
        max_exemplars=3 if direct_builtin else 0,
    )
    rag_block = format_rag_reference_block(rag_refs)
    style_guide = _build_lang_specific_style_guide(lang_pair, src_code=src_code, tgt_code=tgt_code)

    if is_zh_to_en:
        repair_field_hint = "每一条必须提供地道母语英文 text_en（严禁含汉字）与校对后中文 text_zh。\n"
    elif tgt_code == "zh":
        repair_field_hint = f"每一条必须提供纯正简体中文 text_zh（严禁留白、严禁残留{src_name}原文）与整理后的{src_name}原文 text_en。\n"
    else:
        repair_field_hint = f"每一条必须将纯正{tgt_name}译文填入 text_zh，整理后的{src_name}原文填入 text_en（严禁残留未译源语）。\n"

    sys_prompt = (
        "你是顶级游戏切片双语字幕组的质检补漏专家。此前整段翻译遗漏了以下少量台词（未翻译或时间轴跳过），\n"
        f"请结合前后句语境，将原台词按 {src_name} → {tgt_name} 逐条输出地道母语级双语字幕，确保 100% 零漏翻。\n"
        f"{style_guide}\n"
        f"{repair_field_hint}"
        '严格只返回合法 JSON：{"repairs": [{"id": 1, "text_en": "...", "text_zh": "..."}]}'
    )

    usr_lines: list[str] = [
        f"片段 #{entry['rank']}《{entry['title_zh']}》（语种方向：{lang_pair}），共 {len(tasks)} 条待定点补翻台词：",
    ]
    if hotwords:
        usr_lines.append(f"专有热词：{hotwords}")
    if extra_context:
        usr_lines.append(f"补充背景：{extra_context}")
    if rag_block:
        usr_lines.append(rag_block)
    usr_lines.append("")
    for t in tasks:
        ctx_parts = []
        if t["prev_ctx"]:
            ctx_parts.append(f"上一句：{t['prev_ctx']}")
        if t["next_ctx"]:
            ctx_parts.append(f"下一句：{t['next_ctx']}")
        ctx_str = f"（{' | '.join(ctx_parts)}）" if ctx_parts else ""
        usr_lines.append(
            f"- id={t['id']} [{t['start_sec']:.2f} - {t['end_sec']:.2f}] 原台词：{t['src_text']} {ctx_str}"
        )

    try:
        raw_reply = llm_caller(sys_prompt, "\n".join(usr_lines))
        payload = _extract_json_payload(raw_reply)
        repairs_list = payload.get("repairs")
        if not isinstance(repairs_list, list) or not repairs_list:
            return validated
    except Exception:
        return validated

    repair_by_id: dict[int, dict[str, Any]] = {}
    for r in repairs_list:
        if not isinstance(r, dict):
            continue
        rid = _parse_rank_int(r.get("id"))
        if rid is not None:
            repair_by_id[rid] = r

    repaired_subs = [dict(x) for x in validated]
    inserted_subs: list[dict[str, Any]] = []
    repaired_count = 0

    for t in tasks:
        rep = repair_by_id.get(t["id"])
        if not rep:
            continue
        new_zh = re.sub(r"\s{2,}", " ", str(rep.get("text_zh") or rep.get("text_tgt") or "").strip())
        new_en = re.sub(r"\s{2,}", " ", str(rep.get("text_en") or rep.get("text_src") or "").strip())
        if is_zh_to_en:
            final_zh = new_zh or t["src_text"]
            final_en = new_en
            final_src = final_zh
            final_tgt = final_en
        else:
            final_en = new_en or t["src_text"]
            final_zh = new_zh
            final_src = final_en
            final_tgt = final_zh

        candidate = {
            "start_sec": round(t["start_sec"], 2),
            "end_sec": round(t["end_sec"], 2),
            "text_orig": t["src_text"],
            "text_src": final_src,
            "text_tgt": final_tgt,
            "text_zh": final_zh,
            "text_en": final_en,
        }
        if _is_subtitle_untranslated(
            candidate,
            source_language=src_code,
            target_language=tgt_code,
            fallback_lang_pair=lang_pair,
        ):
            continue
        if t["mode"] == "retranslate":
            idx = t["target_idx"]
            repaired_subs[idx]["text_zh"] = candidate["text_zh"]
            repaired_subs[idx]["text_en"] = candidate["text_en"]
            repaired_subs[idx]["text_src"] = candidate["text_src"]
            repaired_subs[idx]["text_tgt"] = candidate["text_tgt"]
            repaired_count += 1
        else:
            inserted_subs.append(candidate)
            repaired_count += 1

    if repaired_count == 0:
        return validated

    print(
        f"[定点补翻] 片段 #{entry['rank']} 已自动精准补齐 {repaired_count}/{len(tasks)} 条漏翻/漏段字幕。",
        file=sys.stderr,
    )
    if inserted_subs:
        repaired_subs.extend(inserted_subs)
        return _enforce_monotonic_subtitles(
            repaired_subs,
            h_start=float(entry["start_sec"]),
            h_end=float(entry["end_sec"]),
        )
    return repaired_subs


def _merge_micro_filler_subtitles(
    items: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """自动吸收残留的 < 0.90s 无意义微碎填充句（如“像其他，呃”“我就想”），防止成片闪现垃圾字幕。"""
    if len(items) <= 1:
        return items

    merged: list[dict[str, Any]] = [dict(x) for x in items]
    changed = True
    while changed and len(merged) > 1:
        changed = False
        idx = 0
        while idx < len(merged):
            cur = merged[idx]
            dur = float(cur["end_sec"]) - float(cur["start_sec"])
            zh = str(cur.get("text_zh") or "").strip()
            en = str(cur.get("text_en") or "").strip()
            zh_core = re.sub(r"[\s，。！？、,.!?;:~…\"'“”‘’]+", "", zh)
            has_strong_punct = any(ch in zh or ch in en for ch in ("！", "？", "!", "?"))
            is_pure_stutter = bool(_LAZY_ZH_FRAGMENT_RE.search(zh) and len(zh_core) <= 6)
            is_micro_filler = (
                (dur < 0.90 and len(zh_core) <= 5 and not has_strong_punct)
                or is_pure_stutter
            )
            if not is_micro_filler or len(merged) <= 1:
                idx += 1
                continue

            # 优先向间隔更近的相邻句吸收时间（对纯口吃垃圾句放宽合并总时长，若仍过长则直接丢弃防闪屏）
            max_combined_sec = 9.0 if is_pure_stutter else 6.8
            prev_gap = (
                float(cur["start_sec"]) - float(merged[idx - 1]["end_sec"])
                if idx > 0
                else 999.0
            )
            next_gap = (
                float(merged[idx + 1]["start_sec"]) - float(cur["end_sec"])
                if idx + 1 < len(merged)
                else 999.0
            )

            can_prev = (
                idx > 0
                and prev_gap <= 0.75
                and (float(cur["end_sec"]) - float(merged[idx - 1]["start_sec"])) <= max_combined_sec
            )
            can_next = (
                idx + 1 < len(merged)
                and next_gap <= 0.75
                and (float(merged[idx + 1]["end_sec"]) - float(cur["start_sec"])) <= max_combined_sec
            )

            if can_next and (not can_prev or next_gap <= prev_gap):
                nxt = merged[idx + 1]
                nxt["start_sec"] = round(float(cur["start_sec"]), 2)
                if not is_pure_stutter:
                    nxt["text_zh"] = f"{zh} {nxt['text_zh']}".strip()
                    nxt["text_en"] = f"{en} {nxt['text_en']}".strip()
                merged.pop(idx)
                changed = True
                continue
            if can_prev:
                prv = merged[idx - 1]
                prv["end_sec"] = round(float(cur["end_sec"]), 2)
                if not is_pure_stutter:
                    prv["text_zh"] = f"{prv['text_zh']} {zh}".strip()
                    prv["text_en"] = f"{prv['text_en']} {en}".strip()
                merged.pop(idx)
                changed = True
                continue
            if is_pure_stutter and dur < 1.0:
                merged.pop(idx)
                changed = True
                continue

            idx += 1

    return merged


def _detect_subtitle_quality_issue(
    raw_lines: list[dict[str, Any]],
    validated: list[dict[str, Any]],
    source_language: str | None = None,
    target_language: str | None = None,
) -> str | None:
    """商业级双语字幕严苛质检：拦截多语种漏翻、时间轴黑洞跳句或 1:1 逐行照抄残句，返回精准整改意见。"""
    if not validated:
        return "输出字幕为空，请重新按完整语义生成双语字幕。"

    raw_combined = " ".join(str(seg.get("text", "")) for seg in raw_lines)
    src_code, tgt_code, lang_pair = resolve_subtitle_language_pair(
        source_language=source_language,
        target_language=target_language,
        sample_text=raw_combined,
    )
    tgt_name = _LANG_DISPLAY_NAMES.get(tgt_code, tgt_code.upper())
    is_zh_to_en = src_code == "zh" and tgt_code == "en"

    untranslated_rows = [
        row
        for row in validated
        if _is_subtitle_untranslated(
            row,
            source_language=src_code,
            target_language=tgt_code,
            fallback_lang_pair=lang_pair,
        )
    ]
    untranslated_cnt = len(untranslated_rows)
    if untranslated_cnt >= 2 or (len(validated) >= 3 and (untranslated_cnt / len(validated)) >= 0.15):
        samples = "；".join(
            f"[{r['start_sec']:.1f}-{r['end_sec']:.1f}s]" for r in untranslated_rows[:4]
        )
        if is_zh_to_en:
            return (
                f"上一版有 {untranslated_cnt}/{len(validated)} 条字幕缺失英文翻译或残留汉字（如 {samples}）。"
                "请务必将每一条都翻译为地道母语英语填入 text_en，严禁漏翻！"
            )
        return (
            f"上一版有 {untranslated_cnt}/{len(validated)} 条主译文行漏翻或残留源语言字符（如 {samples}）。"
            f"请务必将每一条都翻译为纯正{tgt_name}填入 text_zh，严禁留白或照抄源语！"
        )

    # 2. 严苛时间轴黑洞漏句拦截（防大模型跳过中间或尾部的有效原声台词）
    uncovered_raw = _find_uncovered_raw_lines(raw_lines, validated)
    long_hole = any((float(r["end_sec"]) - float(r["start_sec"])) >= 3.0 for r in uncovered_raw)
    if len(uncovered_raw) >= 2 or long_hole:
        hole_desc = "；".join(
            f"[{r['start_sec']:.1f}-{r['end_sec']:.1f}s] {str(r.get('text', ''))[:28]}"
            for r in uncovered_raw[:4]
        )
        return (
            f"上一版跳漏了 {len(uncovered_raw)} 句有效原声台词未翻译覆盖（例如：{hole_desc}）。"
            "请务必完整覆盖原始时间轴内的所有有效对话，绝不可跳过漏翻！"
        )

    # 3. 拦截 1:1 机械逐行直译与口吃残句
    if len(raw_lines) >= 10:
        lazy_ratio = len(validated) / max(1, len(raw_lines))
        short_fragments = sum(
            1
            for row in validated
            if (float(row["end_sec"]) - float(row["start_sec"])) < 1.05
            and len(str(row.get("text_zh") or "").strip()) <= 6
            and not any(ch in str(row.get("text_zh") or "") for ch in ("！", "？", "!", "?"))
        )
        stutter_hits = sum(
            1 for row in validated if _LAZY_ZH_FRAGMENT_RE.search(str(row.get("text_zh") or ""))
        )
        if lazy_ratio >= 0.90 and (short_fragments >= 2 or stutter_hits >= 2):
            return (
                f"上一版几乎按原样 1:1 逐行直译（原 {len(raw_lines)} 句 -> 仍输出 {len(validated)} 句），"
                "保留了过多碎片残句或口吃填充词。请彻底合并跨行没说完的话、删掉无意义口吃，重组为自然流畅的完整意群字幕！"
            )

    return None


def _validate_resegmented_subtitles(
    raw_subs: Any,
    h_start: float,
    h_end: float,
    raw_lines: list[dict[str, Any]],
    source_language: str | None = None,
    target_language: str | None = None,
) -> list[dict[str, Any]] | None:
    """严格校验大模型重组后的双语字幕时间戳、单调性与综合覆盖率，异常时返回 None 触发兜底。"""
    if not isinstance(raw_subs, list) or not raw_subs:
        return None

    parsed: list[dict[str, Any]] = []
    for row in raw_subs:
        if not isinstance(row, dict):
            continue
        s_val = _to_float(row.get("start_sec"))
        e_val = _to_float(row.get("end_sec"))
        if s_val is None or e_val is None:
            continue

        s_clipped = max(h_start, s_val)
        e_clipped = min(h_end, e_val)
        if e_clipped <= s_clipped:
            continue

        text_zh = re.sub(r"\s{2,}", " ", str(row.get("text_zh") or "").strip())
        text_en = re.sub(r"\s{2,}", " ", str(row.get("text_en") or "").strip())
        if not text_zh and not text_en:
            continue

        parsed.append(
            {
                "start_sec": round(s_clipped, 2),
                "end_sec": round(e_clipped, 2),
                "text_zh": text_zh,
                "text_en": text_en,
            }
        )

    if not parsed:
        return None

    parsed.sort(key=lambda x: (x["start_sec"], x["end_sec"]))

    # 1. 消除相邻重叠，确保时间轴严格单调递增不打架
    monotonic: list[dict[str, Any]] = []
    for cur in parsed:
        s = cur["start_sec"]
        e = cur["end_sec"]
        if monotonic:
            prev = monotonic[-1]
            if s < prev["end_sec"]:
                shifted_s = prev["end_sec"]
                if e - shifted_s >= 0.25:
                    s = round(shifted_s, 2)
                else:
                    # 重叠过多导致当前段所剩无几时，尝试把上一条终点微调回缩到重叠区间中点
                    mid = round(
                        min(e - 0.25, max(prev["start_sec"] + 0.25, (s + prev["end_sec"]) / 2.0)),
                        2,
                    )
                    if mid - prev["start_sec"] >= 0.25 and e - mid >= 0.25:
                        prev["end_sec"] = mid
                        s = mid
                    else:
                        continue
        if e - s < 0.25:
            continue
        monotonic.append(
            {
                "start_sec": s,
                "end_sec": e,
                "text_zh": cur["text_zh"],
                "text_en": cur["text_en"],
            }
        )

    if not monotonic:
        return None

    # 2. 对过短（< 0.45s）但后方留有空白间隙的字幕适度延长，提升可读性；并自动吸收残余微碎口癖句
    final_cleaned: list[dict[str, Any]] = []
    for idx, cur in enumerate(monotonic):
        s = cur["start_sec"]
        e = cur["end_sec"]
        if e - s < 0.45:
            next_s = monotonic[idx + 1]["start_sec"] if idx + 1 < len(monotonic) else h_end
            extended_e = min(h_end, next_s, round(s + 0.85, 2))
            if extended_e > e:
                e = round(extended_e, 2)
        if e - s < 0.30:
            continue
        final_cleaned.append(
            {
                "start_sec": round(s, 2),
                "end_sec": round(e, 2),
                "text_zh": cur["text_zh"],
                "text_en": cur["text_en"],
            }
        )

    final_cleaned = _merge_micro_filler_subtitles(final_cleaned)
    if not final_cleaned:
        return None

    # 3. 综合覆盖率安全检查：同时核对时间覆盖与内容词数保留，既防大模型偷懒漏大段，又避免误杀正常压缩僵尸长句
    raw_span = sum(max(0.0, float(r["end_sec"]) - float(r["start_sec"])) for r in raw_lines)
    new_span = sum(max(0.0, item["end_sec"] - item["start_sec"]) for item in final_cleaned)
    raw_words = sum(len(str(r.get("text", "")).split()) for r in raw_lines)
    new_en_words = sum(len(item["text_en"].split()) for item in final_cleaned)
    new_zh_chars = sum(len(item["text_zh"]) for item in final_cleaned)
    if raw_span >= 8.0 and (new_span / raw_span) < 0.35:
        if raw_words >= 8 and new_en_words < max(4, raw_words * 0.45) and new_zh_chars < max(8, raw_words * 0.8):
            return None

    # 4. 组装标准输出字典并补全 text_orig / text_src / text_tgt（100% 兼容历史 text_zh / text_en）
    raw_combined = " ".join(str(seg.get("text", "")) for seg in raw_lines)
    src_code, tgt_code, _ = resolve_subtitle_language_pair(
        source_language=source_language,
        target_language=target_language,
        sample_text=raw_combined,
    )
    is_zh_to_en = src_code == "zh" and tgt_code == "en"
    output: list[dict[str, Any]] = []
    for item in final_cleaned:
        orig_snippet = _match_orig_text_for_span(item["start_sec"], item["end_sec"], raw_lines)
        tr_zh = item["text_zh"]
        tr_en = item["text_en"]

        if is_zh_to_en:
            final_zh = tr_zh or orig_snippet or tr_en
            final_en = tr_en
            final_src = final_zh
            final_tgt = final_en
        else:
            final_en = tr_en or orig_snippet
            final_zh = tr_zh or final_en
            final_src = final_en
            final_tgt = final_zh

        output.append(
            {
                "start_sec": item["start_sec"],
                "end_sec": item["end_sec"],
                "text_orig": orig_snippet or final_src or final_tgt,
                "text_src": final_src,
                "text_tgt": final_tgt,
                "text_zh": final_zh,
                "text_en": final_en,
            }
        )

    return output


def _build_fallback_subtitles_for_highlight(
    raw_lines: list[dict[str, Any]],
    max_duration_sec: float,
    max_chars: int,
) -> list[dict[str, Any]]:
    """当大模型双语重组异常或覆盖不足时，降级用规则拆句保留完整原台词兜底。"""
    sub_entries: list[dict[str, Any]] = []
    for seg in raw_lines:
        for sub in split_raw_subtitle_line(
            seg,
            max_duration_sec=max_duration_sec,
            max_chars=max_chars,
        ):
            orig = str(sub["text"]).strip()
            if not orig:
                continue
            sub_entries.append(
                {
                    "start_sec": round(float(sub["start_sec"]), 2),
                    "end_sec": round(float(sub["end_sec"]), 2),
                    "text_orig": orig,
                    "text_zh": orig,
                    "text_en": orig,
                }
            )
    return sub_entries


def _parse_batch_bilingual_reply(
    batch: list[dict[str, Any]],
    payload: dict[str, Any],
) -> dict[int, list[dict[str, Any]]]:
    """解析单个批次的大模型双语返回（兼容 clips / subtitles / 旧式顺序三种格式）。"""
    parsed_map: dict[int, list[dict[str, Any]]] = {}

    # 兼容返回格式 1：{"clips": [{"rank": 1, "subtitles": [...]}]}
    clips_list = payload.get("clips")
    if isinstance(clips_list, list):
        entry_by_rank = {entry["rank"]: entry for entry in batch}
        for clip_obj in clips_list:
            if not isinstance(clip_obj, dict):
                continue
            c_rank = _parse_rank_int(clip_obj.get("rank"))
            if c_rank is None and len(batch) == 1:
                c_rank = batch[0]["rank"]
            target_entry = entry_by_rank.get(c_rank) if c_rank is not None else None
            if target_entry is None and len(batch) == 1:
                target_entry = batch[0]
            if target_entry is None:
                continue
            subs = clip_obj.get("subtitles")
            if isinstance(subs, list):
                parsed_map[target_entry["list_idx"]] = subs
        return parsed_map

    # 兼容返回格式 2 / 3：{"subtitles": [...]}
    flat_subs = payload.get("subtitles")
    if isinstance(flat_subs, list):
        if len(batch) == 1:
            target_entry = batch[0]
            converted: list[dict[str, Any]] = []
            for i, row in enumerate(flat_subs):
                if not isinstance(row, dict):
                    continue
                if _to_float(row.get("start_sec")) is not None and _to_float(row.get("end_sec")) is not None:
                    converted.append(row)
                elif i < len(target_entry["raw_lines"]):
                    fallback_seg = target_entry["raw_lines"][i]
                    merged_row = dict(row)
                    merged_row["start_sec"] = fallback_seg["start_sec"]
                    merged_row["end_sec"] = fallback_seg["end_sec"]
                    converted.append(merged_row)
            parsed_map[target_entry["list_idx"]] = converted
        else:
            for row in flat_subs:
                if not isinstance(row, dict):
                    continue
                rs = _to_float(row.get("start_sec"))
                re_val = _to_float(row.get("end_sec"))
                if rs is None or re_val is None:
                    continue
                mid_t = (rs + re_val) / 2.0
                matched_entry = next(
                    (e for e in batch if e["start_sec"] - 1.0 <= mid_t <= e["end_sec"] + 1.0),
                    None,
                )
                if matched_entry is not None:
                    parsed_map.setdefault(matched_entry["list_idx"], []).append(row)
    return parsed_map


def translate_and_polish_highlight_subtitles(
    highlights: list[dict[str, Any]],
    transcript_segments: list[dict[str, Any]],
    llm_caller: Callable[[str, str], str],
    source_language: str | None = None,
    target_language: str | None = None,
    max_duration_sec: float = 4.5,
    max_chars: int = 48,
    hotwords: str = "",
    extra_context: str = "",
    concurrency: int = 4,
) -> list[dict[str, Any]]:
    """为每条高光独立并发执行商业级「整句意群重组 + 去口吃清洗 + 听写纠错 + 本土化意译 + 质检打回重写」。"""
    if not highlights:
        return []

    # 1. 为每条高光收集原始区间字幕，并拆分为单条独立任务（杜绝多条混批互相连坐、提升聚焦质量）
    prepared: list[tuple[dict[str, Any], list[dict[str, Any]]]] = []
    entries: list[dict[str, Any]] = []
    for idx_in_list, item in enumerate(highlights, start=1):
        raw_lines = _prepare_highlight_raw_lines(item, transcript_segments)
        prepared.append((item, raw_lines))
        if not raw_lines:
            continue
        rank_num = int(item.get("rank") or idx_in_list)
        entries.append(
            {
                "list_idx": idx_in_list - 1,
                "rank": rank_num,
                "start_sec": round(float(item["start_sec"]), 2),
                "end_sec": round(float(item["end_sec"]), 2),
                "title_zh": item.get("title_zh", ""),
                "summary_zh": item.get("summary_zh", ""),
                "raw_lines": raw_lines,
            }
        )

    validated_by_idx: dict[int, list[dict[str, Any]] | None] = {}

    def _polish_one_entry(entry: dict[str, Any]) -> tuple[int, list[dict[str, Any]] | None]:
        single_batch = [entry]
        best_valid: list[dict[str, Any]] | None = None
        feedback = ""

        for attempt in range(2):
            sys_p, usr_p = _build_bilingual_prompts(
                single_batch,
                source_language=source_language,
                target_language=target_language,
                hotwords=hotwords,
                extra_context=extra_context,
                quality_feedback=feedback,
            )
            try:
                raw_reply = llm_caller(sys_p, usr_p)
                payload = _extract_json_payload(raw_reply)
                parsed_map = _parse_batch_bilingual_reply(single_batch, payload)
                raw_subs = parsed_map.get(entry["list_idx"])
            except Exception as exc:
                if attempt == 0:
                    feedback = (
                        f"上一轮输出解析失败（{exc}），请严格返回合法 JSON："
                        '{"clips": [{"rank": %d, "subtitles": [...]}]}。' % entry["rank"]
                    )
                    print(
                        f"[警告] 片段 #{entry['rank']} 双语精修第 1 轮异常，正在自动重试（{exc}）...",
                        file=sys.stderr,
                    )
                    continue
                print(
                    f"[警告] 片段 #{entry['rank']} 双语精修重试仍失败，将保留兜底（{exc}）。",
                    file=sys.stderr,
                )
                break

            validated = _validate_resegmented_subtitles(
                raw_subs=raw_subs,
                h_start=entry["start_sec"],
                h_end=entry["end_sec"],
                raw_lines=entry["raw_lines"],
                source_language=source_language,
                target_language=target_language,
            )
            if validated is None:
                if attempt == 0:
                    feedback = (
                        f"上一轮字幕时间轴覆盖不完整或时间戳越界（合法范围 {entry['start_sec']:.2f}s ~ {entry['end_sec']:.2f}s），"
                        "请完整覆盖全部有效台词并保证时间戳严格递增。"
                    )
                    continue
                break

            issue = _detect_subtitle_quality_issue(
                raw_lines=entry["raw_lines"],
                validated=validated,
                source_language=source_language,
                target_language=target_language,
            )
            if issue is None:
                final_subs = _repair_missing_subtitles(
                    entry=entry,
                    validated=validated,
                    llm_caller=llm_caller,
                    source_language=source_language,
                    target_language=target_language,
                    hotwords=hotwords,
                    extra_context=extra_context,
                )
                return entry["list_idx"], final_subs

            best_valid = validated
            if attempt == 0:
                feedback = issue
                print(
                    f"[质检打回] 片段 #{entry['rank']} 触发字幕质检，正在自动重写：{issue}",
                    file=sys.stderr,
                )
                continue

        if best_valid is not None:
            repaired_best = _repair_missing_subtitles(
                entry=entry,
                validated=best_valid,
                llm_caller=llm_caller,
                source_language=source_language,
                target_language=target_language,
                hotwords=hotwords,
                extra_context=extra_context,
            )
            raw_combined = " ".join(str(seg.get("text", "")) for seg in entry["raw_lines"])
            src_c, tgt_c, lang_pair = resolve_subtitle_language_pair(
                source_language=source_language,
                target_language=target_language,
                sample_text=raw_combined,
            )
            untranslated_after = sum(
                1
                for r in repaired_best
                if _is_subtitle_untranslated(
                    r,
                    source_language=src_c,
                    target_language=tgt_c,
                    fallback_lang_pair=lang_pair,
                )
            )
            if len(repaired_best) < 3 or (untranslated_after / max(1, len(repaired_best))) <= 0.25:
                return entry["list_idx"], repaired_best
        return entry["list_idx"], None

    workers = max(1, min(int(concurrency or 1), len(entries))) if entries else 1
    if workers == 1:
        for entry in entries:
            idx_key, res_subs = _polish_one_entry(entry)
            validated_by_idx[idx_key] = res_subs
    else:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = [pool.submit(_polish_one_entry, entry) for entry in entries]
            for fut in as_completed(futures):
                try:
                    idx_key, res_subs = fut.result()
                    validated_by_idx[idx_key] = res_subs
                except Exception as exc:
                    print(
                        f"[警告] 单条双语字幕精修线程异常（{exc}）。",
                        file=sys.stderr,
                    )

    # 3. 写回每条高光的 `subtitles` 与显式 `translation_status` 字段
    updated_highlights: list[dict[str, Any]] = []
    for idx_in_list, (item, raw_lines) in enumerate(prepared):
        item_copy = dict(item)
        if not raw_lines:
            item_copy["subtitles"] = []
            item_copy["translation_status"] = "empty"
            updated_highlights.append(item_copy)
            continue

        validated = validated_by_idx.get(idx_in_list)
        if validated is None:
            rank_disp = item.get("rank", idx_in_list + 1)
            print(
                f"[警告] 片段 #{rank_disp} 双语意群重组未通过校验或调用失败，已自动保留原台词短句兜底。",
                file=sys.stderr,
            )
            item_copy["subtitles"] = _build_fallback_subtitles_for_highlight(
                raw_lines=raw_lines,
                max_duration_sec=max_duration_sec,
                max_chars=max_chars,
            )
            item_copy["translation_status"] = "fallback"
        else:
            item_copy["subtitles"] = validated
            item_copy["translation_status"] = "ok"

        updated_highlights.append(item_copy)

    return updated_highlights


def export_review_markdown(
    result: dict[str, Any],
    md_path: str | Path,
    transcript_segments: list[dict[str, Any]],
) -> Path:
    """导出面向公会剪辑师与运营的中文高光审片清单（Markdown 格式）。"""
    dst_md = Path(md_path)
    dst_md.parent.mkdir(parents=True, exist_ok=True)

    plan = result["selection_plan"]
    highlights: list[dict[str, Any]] = result.get("highlights", [])
    video_name = Path(result.get("video_path") or result["transcript_json_path"]).name

    lines: list[str] = [
        f"# 高光切片审片清单：{video_name}",
        "",
        "## 一、基础信息与筛选策略",
        "",
        f"- **原视频总时长**：`{result['total_duration_str']}`（{result['total_duration_sec']} 秒）",
        f"- **自适应档位**：{plan['duration_tier']} -> 目标精选 `{plan['target_count']}` 条，单段 `{plan['min_clip_sec']}~{plan['max_clip_sec']}` 秒",
        f"- **使用模型**：`{result['model']}` | 实际筛选命中：`{result['highlight_count']}` 条 | 耗时：`{result['elapsed_sec']}` 秒",
        f"- **人工审片指引**：默认全部 `" + '"selected": true' + f"`；如需剔除某段，请打开 `{Path(result['highlights_json_path']).name}` 把对应条目改为 `" + '"selected": false' + "`，下一步 Step 4 将自动跳过。",
        "",
        "## 二、高光总览速查表",
        "",
    ]

    if not highlights:
        lines.append("> 本次未筛出符合条件的高光片段，可尝试放宽 `--top-k-peaks` 或调整提示词策略。")
    else:
        lines.extend(
            [
                "| 排名 | 评分 | 时间段 | 时长 | 中文标题 | 一句话中文梗概 |",
                "| :---: | :---: | :--- | :---: | :--- | :--- |",
            ]
        )
        for item in highlights:
            safe_title = item["title_zh"].replace("|", "\\|")
            safe_summary = item["summary_zh"].replace("|", "\\|")
            lines.append(
                f"| #{item['rank']} | **{item['score']}** | `{item['start_str']} - {item['end_str']}` | "
                f"{item['duration_sec']:.1f}s | {safe_title} | {safe_summary} |"
            )

        lines.extend(["", "## 三、逐条审片详卡（附原声台词核对）", ""])
        for item in highlights:
            tags_str = " / ".join(f"`{t}`" for t in item["tags"]) if item["tags"] else "`无标签`"
            lines.extend(
                [
                    f"### #{item['rank']} [{item['score']}分] {item['title_zh']}",
                    "",
                    f"- **时间定位**：`{item['start_str']} - {item['end_str']}`（精确秒数：`{item['start_sec']}s ~ {item['end_sec']}s`，时长 `{item['duration_sec']:.1f}s`）",
                    f"- **英文标题**：{item['title_en'] or '（未生成）'}",
                    f"- **剧情梗概**：{item['summary_zh']}",
                    f"- **入选理由**：{item['reason'] or '（未填写）'}",
                    f"- **标签**：{tags_str}",
                    "- **台词字幕核对**：",
                ]
            )
            bilingual_subs = item.get("subtitles") or []
            if bilingual_subs:
                for q in bilingual_subs:
                    q_start = format_timestamp(float(q["start_sec"]))
                    q_end = format_timestamp(float(q["end_sec"]))
                    zh = str(q.get("text_zh") or "").strip()
                    en = str(q.get("text_en") or q.get("text_orig") or "").strip()
                    if zh and en and zh != en:
                        lines.append(f"  > `[{q_start} - {q_end}]` {zh}  |  *{en}*")
                    else:
                        lines.append(f"  > `[{q_start} - {q_end}]` {zh or en}")
            else:
                quotes = _collect_quotes_for_highlight(item, transcript_segments)
                if not quotes:
                    lines.append("  > （该区间未匹配到 Step 2 原始字幕行）")
                else:
                    for q in quotes:
                        q_start = q.get("start_str") or format_timestamp(float(q["start_sec"]))
                        q_end = q.get("end_str") or format_timestamp(float(q["end_sec"]))
                        vol = f" **[高音量 {float(q['peak_rms']):.4f}]**" if q.get("is_high_volume") else ""
                        lines.append(f"  > `[{q_start} - {q_end}]`{vol} {q['text']}")
            lines.append("")

    dst_md.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
    return dst_md


def _derive_stem(transcript_json_path: Path) -> str:
    """从 Step 2 文件名中还原视频前缀（例如 shylily_1_transcript.json -> shylily_1）。"""
    stem = transcript_json_path.stem
    suffix = "_transcript"
    if stem.endswith(suffix):
        return stem[: -len(suffix)]
    return stem


def _effective_polish_reasoning_effort(model_name: str, configured_effort: str) -> str:
    """为双语精修角色决定思考强度：未显式指定时，对 deepseek-flash 默认开启 low 轻量思考以彻底消除逐行机翻。"""
    effort = (configured_effort or "").strip().lower()
    if effort:
        return effort
    if "deepseek-flash" in (model_name or "").strip().lower():
        return "low"
    return ""


def _resolve_llm_config(
    api_key: str | None,
    base_url: str | None,
    model: str | None,
    require_api_key: bool,
    role: str = "screen",
    reasoning_effort: str | None = None,
) -> dict[str, Any]:
    """统一通过配置中心解析粗筛（screen）或双语精修（polish）的大模型接口配置。"""
    return resolve_llm_role_config(
        role=role,
        api_key=api_key,
        base_url=base_url,
        model=model,
        reasoning_effort=reasoning_effort,
        require_api_key=require_api_key,
    )


def select_highlights_with_llm(
    transcript_json_path: str | Path,
    output_dir: str | Path | None = None,
    top_k: int | None = None,
    min_clip_sec: int | None = None,
    max_clip_sec: int | None = None,
    model: str | None = None,
    base_url: str | None = None,
    api_key: str | None = None,
    polish_model: str | None = None,
    polish_base_url: str | None = None,
    polish_api_key: str | None = None,
    screen_reasoning_effort: str | None = None,
    polish_reasoning_effort: str | None = None,
    hotwords: str | None = None,
    max_chars_per_chunk: int = 48000,
    bilingual_subtitles: bool = True,
    target_language: str | None = None,
    concurrency: int = 4,
    llm_caller: Callable[[str, str], str] | None = None,
    polish_llm_caller: Callable[[str, str], str] | None = None,
) -> dict[str, Any]:
    """Step 3 主入口：粗筛（默认 deepseek-flash）与双语精修双角色路由，导出审片清单。

    支持：
    - 粗筛（screen）与双语精修（polish）各自独立配置模型、Base URL、API Key 和思考强度；
    - 单模型极简模式下只需传 1 套 Key（默认均走 `deepseek-flash`）；
    - 自动记录分角色 Token 消耗账单与逐条精修成功/兜底状态。
    """
    src_transcript = Path(transcript_json_path)
    if not src_transcript.is_file():
        raise FileNotFoundError(f"找不到 Step 2 字幕 JSON 文件：{src_transcript}")

    transcript_data = json.loads(src_transcript.read_text(encoding="utf-8"))
    stem = _derive_stem(src_transcript)

    out_dir = Path(output_dir) if output_dir is not None else src_transcript.parent
    out_dir.mkdir(parents=True, exist_ok=True)
    highlights_json_path = out_dir / f"{stem}_highlights.json"
    review_md_path = out_dir / f"{stem}_review.md"

    segments: list[dict[str, Any]] = transcript_data.get("segments", [])
    candidate_windows: list[dict[str, Any]] = transcript_data.get("candidate_windows", [])
    total_duration_sec = float(transcript_data.get("total_duration_sec", 0.0))
    if total_duration_sec <= 0 and segments:
        total_duration_sec = max(float(s["end_sec"]) for s in segments)
    if total_duration_sec <= 0:
        total_duration_sec = 1.0

    # 第一步：计算按直播回放长度自适应的筛选策略
    plan = compute_adaptive_selection_plan(
        total_duration_sec=total_duration_sec,
        candidate_window_count=len(candidate_windows),
        top_k=top_k,
        min_clip_sec=min_clip_sec,
        max_clip_sec=max_clip_sec,
    )

    # 第二步：分别解析粗筛角色（screen）与双语精修角色（polish）的模型配置
    screen_cfg = _resolve_llm_config(
        api_key=api_key,
        base_url=base_url,
        model=model,
        require_api_key=(llm_caller is None),
        role="screen",
        reasoning_effort=screen_reasoning_effort,
    )
    polish_cfg = _resolve_llm_config(
        api_key=polish_api_key if polish_api_key is not None else api_key,
        base_url=polish_base_url if polish_base_url is not None else base_url,
        model=polish_model if polish_model is not None else model,
        require_api_key=(bilingual_subtitles and polish_llm_caller is None and llm_caller is None),
        role="polish",
        reasoning_effort=polish_reasoning_effort,
    )

    effective_hotwords = (
        hotwords.strip()
        if hotwords is not None
        else str(transcript_data.get("hotwords") or "").strip()
    )

    start_time = time.perf_counter()
    usage_tracker = TokenUsageTracker()

    chunks = build_transcript_chunks(
        segments=segments,
        candidate_windows=candidate_windows,
        max_chars_per_chunk=max_chars_per_chunk,
    )

    video_path_str = str(transcript_data.get("video_path", ""))
    video_name = Path(video_path_str).name if video_path_str else f"{stem}.mp4"

    screen_caller: Callable[[str, str], str]
    if llm_caller is not None:
        screen_caller = llm_caller
    else:
        def _default_screen_caller(sys_p: str, usr_p: str) -> str:
            return call_openai_compatible_llm(
                system_prompt=sys_p,
                user_prompt=usr_p,
                api_key=screen_cfg["api_key"],
                base_url=screen_cfg["base_url"],
                model=screen_cfg["model"],
                reasoning_effort=screen_cfg["reasoning_effort"],
                role="screen",
                usage_tracker=usage_tracker,
            )

        screen_caller = _default_screen_caller

    polish_caller: Callable[[str, str], str]
    if polish_llm_caller is not None:
        polish_caller = polish_llm_caller
    elif llm_caller is not None:
        polish_caller = llm_caller
    else:
        effective_polish_effort = _effective_polish_reasoning_effort(
            polish_cfg["model"],
            polish_cfg["reasoning_effort"],
        )

        def _default_polish_caller(sys_p: str, usr_p: str) -> str:
            return call_openai_compatible_llm(
                system_prompt=sys_p,
                user_prompt=usr_p,
                api_key=polish_cfg["api_key"],
                base_url=polish_cfg["base_url"],
                model=polish_cfg["model"],
                reasoning_effort=effective_polish_effort,
                role="polish",
                usage_tracker=usage_tracker,
            )

        polish_caller = _default_polish_caller

    raw_candidates: list[dict[str, Any]] = []
    if chunks:
        total_chunks = len(chunks)
        for idx, chunk_text in enumerate(chunks, start=1):
            sys_prompt, usr_prompt = build_llm_prompts(
                chunk_text=chunk_text,
                plan=plan,
                video_name=video_name,
                chunk_index=idx,
                total_chunks=total_chunks,
                source_language=transcript_data.get("language"),
            )
            payload: dict[str, Any] | None = None
            last_parse_err: ValueError | None = None
            for attempt in range(2):
                raw_reply = screen_caller(sys_prompt, usr_prompt)
                try:
                    payload = _extract_json_payload(raw_reply)
                    break
                except ValueError as exc:
                    last_parse_err = exc
                    if attempt == 0:
                        print(
                            f"[警告] 第 {idx}/{total_chunks} 块粗筛 JSON 解析失败，正在自动重试一次（{exc}）...",
                            file=sys.stderr,
                        )
            if payload is None:
                assert last_parse_err is not None
                raise last_parse_err
            items = payload.get("highlights", [])
            if isinstance(items, list):
                raw_candidates.extend(item for item in items if isinstance(item, dict))

    # 第三步：跨块汇总后统一做校验、边界裁剪与重叠去重
    final_highlights = parse_and_validate_highlights(
        raw_items=raw_candidates,
        total_duration_sec=total_duration_sec,
        target_count=plan["target_count"],
    )

    # 第四步：针对精选出的高光区间并发执行整句语义重组、语境纠错与多语种母语意译
    sample_speech = " ".join(str(s.get("text", "")) for s in segments[:40])
    resolved_src_lang, resolved_tgt_lang, resolved_pair = resolve_subtitle_language_pair(
        source_language=transcript_data.get("language"),
        target_language=target_language,
        sample_text=sample_speech,
    )
    translated_ok_count = 0
    translation_fallback_count = 0
    if bilingual_subtitles and final_highlights and segments:
        final_highlights = translate_and_polish_highlight_subtitles(
            highlights=final_highlights,
            transcript_segments=segments,
            llm_caller=polish_caller,
            source_language=resolved_src_lang,
            target_language=resolved_tgt_lang,
            hotwords=effective_hotwords,
            concurrency=concurrency,
        )
        translated_ok_count = sum(1 for h in final_highlights if h.get("translation_status") == "ok")
        translation_fallback_count = sum(1 for h in final_highlights if h.get("translation_status") == "fallback")

    elapsed_sec = round(time.perf_counter() - start_time, 2)
    screen_model_name = screen_cfg["model"] if llm_caller is None else "mock/custom"
    polish_model_name = (
        polish_cfg["model"] if (polish_llm_caller is None and llm_caller is None) else "mock/custom"
    )

    result: dict[str, Any] = {
        "video_path": video_path_str,
        "transcript_json_path": str(src_transcript.resolve()),
        "highlights_json_path": str(highlights_json_path.resolve()),
        "review_md_path": str(review_md_path.resolve()),
        "model": screen_model_name,
        "screen_model": screen_model_name,
        "polish_model": polish_model_name,
        "base_url": screen_cfg["base_url"],
        "source_language": resolved_src_lang,
        "target_language": resolved_tgt_lang,
        "subtitle_pair": resolved_pair,
        "total_duration_sec": round(total_duration_sec, 2),
        "total_duration_str": format_timestamp(total_duration_sec),
        "selection_plan": plan,
        "chunk_count": len(chunks),
        "raw_candidate_count": len(raw_candidates),
        "highlight_count": len(final_highlights),
        "bilingual_subtitles": bool(bilingual_subtitles and translated_ok_count > 0),
        "translated_ok_count": translated_ok_count,
        "translation_fallback_count": translation_fallback_count,
        "token_usage": usage_tracker.summary(),
        "elapsed_sec": elapsed_sec,
        "highlights": final_highlights,
    }

    highlights_json_path.write_text(
        json.dumps(result, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    export_review_markdown(result, review_md_path, transcript_segments=segments)
    return result


def enrich_highlights_with_bilingual_subtitles(
    input_json_path: str | Path,
    output_dir: str | Path | None = None,
    model: str | None = None,
    base_url: str | None = None,
    api_key: str | None = None,
    reasoning_effort: str | None = None,
    hotwords: str | None = None,
    extra_context: str | None = None,
    source_language: str | None = None,
    target_language: str | None = None,
    ranks: list[int] | None = None,
    concurrency: int = 4,
    llm_caller: Callable[[str, str], str] | None = None,
) -> dict[str, Any]:
    """为已经生成的 `*_highlights.json` 补齐或定点重刷商业级双语字幕（保留既有起止时间与勾选状态）。"""
    src_path = Path(input_json_path)
    if not src_path.is_file():
        raise FileNotFoundError(f"找不到输入文件：{src_path}")

    if src_path.name.endswith("_highlights.json"):
        highlights_path = src_path
    else:
        stem = _derive_stem(src_path)
        highlights_path = src_path.parent / f"{stem}_highlights.json"
        if not highlights_path.is_file():
            raise FileNotFoundError(
                f"未找到已有高光方案：{highlights_path}，请先运行普通筛选流程或去掉 --translate-only。"
            )

    data = json.loads(highlights_path.read_text(encoding="utf-8"))
    transcript_path = Path(data.get("transcript_json_path", ""))
    if not transcript_path.is_file():
        stem = highlights_path.stem.removesuffix("_highlights")
        transcript_path = highlights_path.parent / f"{stem}_transcript.json"
    if not transcript_path.is_file():
        raise FileNotFoundError(f"找不到对应的 Step 2 字幕文件：{transcript_path}")

    transcript_data = json.loads(transcript_path.read_text(encoding="utf-8"))
    segments: list[dict[str, Any]] = transcript_data.get("segments", [])

    polish_cfg = _resolve_llm_config(
        api_key=api_key,
        base_url=base_url,
        model=model,
        require_api_key=(llm_caller is None),
        role="polish",
        reasoning_effort=reasoning_effort,
    )
    effective_hotwords = (
        hotwords.strip()
        if hotwords is not None
        else str(transcript_data.get("hotwords") or "").strip()
    )
    effective_context = (extra_context or "").strip()
    usage_tracker = TokenUsageTracker()

    if llm_caller is not None:
        active_caller = llm_caller
    else:
        effective_polish_effort = _effective_polish_reasoning_effort(
            polish_cfg["model"],
            polish_cfg["reasoning_effort"],
        )

        def _default_caller(sys_p: str, usr_p: str) -> str:
            return call_openai_compatible_llm(
                system_prompt=sys_p,
                user_prompt=usr_p,
                api_key=polish_cfg["api_key"],
                base_url=polish_cfg["base_url"],
                model=polish_cfg["model"],
                reasoning_effort=effective_polish_effort,
                role="polish",
                usage_tracker=usage_tracker,
            )

        active_caller = _default_caller

    t0 = time.perf_counter()
    highlights = list(data.get("highlights", []))
    target_ranks = {int(r) for r in ranks} if ranks else None
    sample_speech = " ".join(str(s.get("text", "")) for s in segments[:40])
    resolved_src_lang, resolved_tgt_lang, resolved_pair = resolve_subtitle_language_pair(
        source_language=source_language or data.get("source_language") or transcript_data.get("language"),
        target_language=target_language or data.get("target_language"),
        sample_text=sample_speech,
    )

    if target_ranks:
        subset_indices = [
            idx
            for idx, item in enumerate(highlights)
            if int(item.get("rank") or (idx + 1)) in target_ranks
        ]
        subset_items = [highlights[i] for i in subset_indices]
        polished_subset = translate_and_polish_highlight_subtitles(
            highlights=subset_items,
            transcript_segments=segments,
            llm_caller=active_caller,
            source_language=resolved_src_lang,
            target_language=resolved_tgt_lang,
            hotwords=effective_hotwords,
            extra_context=effective_context,
            concurrency=concurrency,
        )
        enriched = list(highlights)
        for orig_idx, updated_item in zip(subset_indices, polished_subset):
            enriched[orig_idx] = updated_item
    else:
        enriched = translate_and_polish_highlight_subtitles(
            highlights=highlights,
            transcript_segments=segments,
            llm_caller=active_caller,
            source_language=resolved_src_lang,
            target_language=resolved_tgt_lang,
            hotwords=effective_hotwords,
            extra_context=effective_context,
            concurrency=concurrency,
        )

    ok_count = sum(1 for h in enriched if h.get("translation_status") == "ok")
    fb_count = sum(1 for h in enriched if h.get("translation_status") == "fallback")

    out_dir = Path(output_dir) if output_dir is not None else highlights_path.parent
    out_dir.mkdir(parents=True, exist_ok=True)
    out_json = out_dir / highlights_path.name
    stem = out_json.stem.removesuffix("_highlights")
    out_md = out_dir / f"{stem}_review.md"

    data["highlights"] = enriched
    data["source_language"] = resolved_src_lang
    data["target_language"] = resolved_tgt_lang
    data["subtitle_pair"] = resolved_pair
    data["polish_model"] = polish_cfg["model"] if llm_caller is None else "mock/custom"
    data["bilingual_subtitles"] = ok_count > 0
    data["translated_ok_count"] = ok_count
    data["translation_fallback_count"] = fb_count
    data["token_usage"] = usage_tracker.summary()
    data["highlights_json_path"] = str(out_json.resolve())
    data["review_md_path"] = str(out_md.resolve())
    data["elapsed_sec"] = round(time.perf_counter() - t0, 2)

    out_json.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    export_review_markdown(data, out_md, transcript_segments=segments)
    return data


def _print_summary(result: dict[str, Any]) -> None:
    """在终端打印易读的 Step 3 高光粗筛结果与审片摘要表。"""
    plan = result["selection_plan"]
    print("=" * 72)
    print("Step 3 大模型高光粗筛与人工审片清单生成完成")
    print("=" * 72)
    print(f"原视频     : {result['video_path'] or result['transcript_json_path']}")
    print(
        f"视频时长   : {result['total_duration_str']} ({result['total_duration_sec']} 秒) "
        f"| 档位: {plan['duration_tier']}"
    )
    screen_m = result.get("screen_model") or result.get("model")
    polish_m = result.get("polish_model") or screen_m
    model_disp = screen_m if screen_m == polish_m else f"粗筛={screen_m} / 精修={polish_m}"
    print(
        f"筛选策略   : 目标 {plan['target_count']} 条 | "
        f"单段 {plan['min_clip_sec']}~{plan['max_clip_sec']} 秒 | "
        f"分块 {result.get('chunk_count', 1)} 批 | 模型 {model_disp} | 耗时 {result['elapsed_sec']} 秒"
    )
    usage = result.get("token_usage") or {}
    if usage.get("total_tokens"):
        print(
            f"Token 账单 : 总计 {usage['total_tokens']} tokens "
            f"(输入 {usage.get('prompt_tokens', 0)} / 输出 {usage.get('completion_tokens', 0)} / "
            f"{usage.get('request_count', 0)} 次调用)"
        )
    fb_cnt = int(result.get("translation_fallback_count", 0))
    fb_hint = f"（注意：有 {fb_cnt} 条触发兜底）" if fb_cnt > 0 else ""
    print(f"最终命中   : {result['highlight_count']} 条高光（默认全部 selected=true）{fb_hint}")
    print("-" * 72)

    highlights = result["highlights"]
    if not highlights:
        print("未筛出符合条件的高光片段。")
    else:
        print(f"{'排名':<4} {'评分':<5} {'时间段':<19} {'时长':<7} {'中文标题'}")
        for item in highlights:
            time_range = f"{item['start_str']} - {item['end_str']}"
            print(
                f"#{item['rank']:<3} {item['score']:<5.1f} {time_range:<19} "
                f"{item['duration_sec']:<5.1f}s  {item['title_zh']}"
            )
            print(f"     梗概: {item['summary_zh']}")

    print("-" * 72)
    print(f"高光 JSON（给 Step 4） : {result['highlights_json_path']}")
    print(f"中文审片清单（给人看）  : {result['review_md_path']}")
    print("=" * 72)


def build_arg_parser() -> argparse.ArgumentParser:
    """构建命令行参数解析器。"""
    parser = argparse.ArgumentParser(
        description="Step 3：根据回放时长自适应调用大模型（默认 deepseek-flash）精选高光并生成双语审片清单。"
    )
    parser.add_argument(
        "transcript_json",
        help="Step 2 生成的 *_transcript.json 路径（如 output/shylily_1_transcript.json）",
    )
    parser.add_argument(
        "--output-dir",
        default=None,
        help="输出目录（默认与 transcript_json 同目录）",
    )
    parser.add_argument(
        "--top-k",
        type=int,
        default=None,
        help="手动指定最多精选多少条高光；不填则根据直播总时长自动计算",
    )
    parser.add_argument(
        "--min-clip-sec",
        type=int,
        default=None,
        help="手动指定单条切片最小目标秒数；不填则按时长档位自动设定",
    )
    parser.add_argument(
        "--max-clip-sec",
        type=int,
        default=None,
        help="手动指定单条切片最大目标秒数；不填则按时长档位自动设定",
    )
    parser.add_argument(
        "--model",
        default=None,
        help="覆盖粗筛模型名（默认 deepseek-flash）",
    )
    parser.add_argument(
        "--base-url",
        default=None,
        help="覆盖粗筛接口地址（默认 https://api.deepseek.com/v1）",
    )
    parser.add_argument(
        "--api-key",
        default=None,
        help="覆盖 API 密钥",
    )
    parser.add_argument(
        "--polish-model",
        default=None,
        help="独立指定双语精修模型名（留空则沿用粗筛模型）",
    )
    parser.add_argument(
        "--polish-base-url",
        default=None,
        help="独立指定双语精修接口地址",
    )
    parser.add_argument(
        "--polish-api-key",
        default=None,
        help="独立指定双语精修 API 密钥",
    )
    parser.add_argument(
        "--hotwords",
        default=None,
        help="主播/游戏专词术语表（逗号分隔，用于纠错对齐）",
    )
    parser.add_argument(
        "--concurrency",
        type=int,
        default=3,
        help="双语精修并发批次数（默认：3）",
    )
    parser.add_argument(
        "--max-chars-per-chunk",
        type=int,
        default=48000,
        help="每次发给粗筛大模型的最大字幕字符数（默认：48000，适配 deepseek-flash 长上下文）",
    )
    parser.add_argument(
        "--skip-bilingual",
        action="store_true",
        help="跳过高光区间的听写纠错与中英双语字幕生成",
    )
    parser.add_argument(
        "--translate-only",
        action="store_true",
        help="只为已有的 *_highlights.json 重刷中英双语纠错字幕，不重新筛选时间段",
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
        if args.translate_only:
            result = enrich_highlights_with_bilingual_subtitles(
                input_json_path=args.transcript_json,
                output_dir=args.output_dir,
                model=args.polish_model or args.model,
                base_url=args.polish_base_url or args.base_url,
                api_key=args.polish_api_key or args.api_key,
                hotwords=args.hotwords,
                concurrency=args.concurrency,
            )
        else:
            result = select_highlights_with_llm(
                transcript_json_path=args.transcript_json,
                output_dir=args.output_dir,
                top_k=args.top_k,
                min_clip_sec=args.min_clip_sec,
                max_clip_sec=args.max_clip_sec,
                model=args.model,
                base_url=args.base_url,
                api_key=args.api_key,
                polish_model=args.polish_model,
                polish_base_url=args.polish_base_url,
                polish_api_key=args.polish_api_key,
                hotwords=args.hotwords,
                max_chars_per_chunk=args.max_chars_per_chunk,
                bilingual_subtitles=not args.skip_bilingual,
                concurrency=args.concurrency,
            )
    except (FileNotFoundError, ValueError, RuntimeError) as exc:
        print(f"[错误] {exc}", file=sys.stderr)
        return 1

    _print_summary(result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
