"""纯本地多平台弹幕解析、热力图曲线、跨文化梗词检索与音弹双雷达融合引擎。

核心设计目标：
1. 零云端 API 费用、纯本地毫秒级运行，彻底补齐纯音频音量检测容易漏掉“冷面神吐槽”“无声极限操作”的短板；
2. 黑客级输入安全防御：严格限制文件最大体积防内存耗尽，XML 解析前硬拦截 `<!DOCTYPE` / `<!ENTITY`，
   彻底杜绝 XXE 与 Billion Laughs 实体扩展攻击；
3. 原生支持海内外四大主流弹幕导出格式：
   - TwitchDownloader JSON (`twitch_json`)
   - B站 XML 弹幕 (`bilibili_xml`)
   - YouTube `yt-dlp` Live Chat JSONL / JSON (`youtube_live_chat`)
   - 通用精简 JSON 列表 (`generic_json`)
4. 提供跨文化多语种直播高能梗词雷达（英/中/日/韩/西/俄）、稳健中位数+MAD 刷屏峰值检测、
   Peepovod 风格任意梗词秒级跳转查询，以及音频+弹幕双模态证据加权融合。
"""

from __future__ import annotations

import io
import json
import math
import os
import re
import sqlite3
import time
import uuid
import xml.etree.ElementTree as ET
from collections import Counter
from pathlib import Path
from typing import Any

import numpy as np

from src.step1_audio import format_timestamp

# 安全资源硬上限（防御超大/恶意构造弹幕 OOM 与 CPU 耗尽）
_MAX_CHAT_MESSAGES = 300_000
_MAX_MESSAGE_TEXT_LEN = 500
_MAX_AUTHOR_LEN = 120
_MAX_YT_STACK_DEPTH = 16
_MAX_SEARCH_QUERY_LEN = 200
_MAX_SEARCH_TERMS = 10
_MAX_BUCKET_SAMPLES = 12

# XML 安全防线：严格拦截任何 DOCTYPE / ENTITY 声明（剥离 CDATA 防正文误报），杜绝 XXE 与实体爆炸攻击
_XML_CDATA_RE = re.compile(r"<!\[CDATA\[[\s\S]*?\]\]>")
_XML_DANGEROUS_DECL_RE = re.compile(r"<!\s*(?:DOCTYPE|ENTITY)\b", re.IGNORECASE)
_XML_DECL_ENCODING_RE = re.compile(
    r'(<\?xml\b[^?]*?\bencoding\s*=\s*["\'])([^"\']+)(["\'])',
    re.IGNORECASE,
)
# 非内置命名实体检测（仅允许 XML 5 大标准实体与数字字符引用 &#...; / &#x...;）
_XML_ENTITY_REF_RE = re.compile(r"&([A-Za-z_][A-Za-z0-9._-]*);")
_XML_SAFE_BUILTIN_ENTITIES = frozenset({"lt", "gt", "amp", "quot", "apos"})

# 多语种直播高能梗词与表情字典：(规范展示词, 编译正则)
_HYPE_TERM_SPECS: tuple[tuple[str, str], ...] = (
    # 英文 / Twitch / YouTube 全球直播高频梗
    ("OMEGALUL", r"(?<![A-Za-z])omegalul+w?(?![A-Za-z])"),
    ("LUL", r"(?<![A-Za-z])lul(?:w|z)?(?![A-Za-z])"),
    ("KEKW", r"(?<![A-Za-z])kekw?(?![A-Za-z])"),
    ("ICANT", r"(?<![A-Za-z])icant(?![A-Za-z])"),
    ("POG", r"(?<![A-Za-z])pog(?:gers?|champ|u)?(?![A-Za-z])"),
    ("CLIP IT", r"(?<![A-Za-z])clip\s+(?:it|that)(?![A-Za-z])"),
    ("LMAO", r"(?<![A-Za-z])lmfa?o+(?![A-Za-z])"),
    ("WTF", r"(?<![A-Za-z])wtf+(?![A-Za-z])"),
    ("NO WAY", r"(?<![A-Za-z])(?:aint\s*)?no\s+way(?![A-Za-z])"),
    ("AINTNOWAY", r"(?<![A-Za-z])aintnoway(?![A-Za-z])"),
    ("CLUTCH", r"(?<![A-Za-z])clutch(?:ed|ing)?(?![A-Za-z])"),
    ("COOKED", r"(?<![A-Za-z])(?:we'?re\s+)?cooked(?![A-Za-z])"),
    ("CINEMA", r"(?<![A-Za-z])(?:absolute\s+)?cinema(?![A-Za-z])"),
    ("INSANE", r"(?<![A-Za-z])insane(?![A-Za-z])"),
    ("MONKAS", r"(?<![A-Za-z])monkas(?![A-Za-z])"),
    ("PEPELAUGH", r"(?<![A-Za-z])pepelaugh(?![A-Za-z])"),
    ("SHEESH", r"(?<![A-Za-z])shee+sh(?![A-Za-z])"),
    ("HOLY", r"(?<![A-Za-z])holyy+(?![A-Za-z])"),
    # 中文 / B站 / 抖音 / 虎牙 / 斗鱼直播高频梗
    ("草", r"草+"),
    ("笑死", r"笑死"),
    ("666", r"(?<!\d)6{3,}(?!\d)"),
    ("离谱", r"离大?谱"),
    ("卧槽", r"[卧我沃][槽草操艹]"),
    ("牛逼", r"牛[逼批啤bB]"),
    ("绷不住", r"绷不住"),
    ("名场面", r"名场面"),
    ("？？？", r"[？?]{3,}"),
    ("哈哈哈哈", r"哈{3,}"),
    ("红温", r"红温"),
    ("破防", r"破防"),
    ("好活", r"好活"),
    ("绝杀", r"绝杀"),
    ("逆天", r"逆天"),
    ("爆典", r"爆典"),
    # 日文 / Niconico / YouTube JP / 日V 烤肉高频梗
    ("www", r"(?<![A-Za-z])[wWｗＷ]{3,}(?![A-Za-z])"),
    ("わろた", r"(?:わろた|ワロタ|ワロス)"),
    ("神", r"神(?:回|プレイ|すぎ|かよ)?"),
    ("えぐい", r"(?:えぐい|エグい|エグイ|えぐっ)"),
    ("待って", r"(?:待って|まって)"),
    ("やばい", r"(?:やばい|ヤバい|ヤバイ|やっば)"),
    ("てぇてぇ", r"(?:てぇてぇ|尊い)"),
    ("助かる", r"助かる"),
    ("うおお", r"(?:うおお+|ウオオ+)"),
    # 韩文 / AfreecaTV / CHZZK / Twitch KR 直播高频梗
    ("ㅋㅋ", r"ㅋ{2,}"),
    ("ㅎㅎ", r"ㅎ{2,}"),
    ("레전드", r"레전드"),
    ("미쳤다", r"미쳤(?:다|네|어|냐)"),
    ("찢었다", r"찢었(?:다|네|어)"),
    ("대박", r"대박"),
    ("실화", r"실화"),
    # 西语 / 俄语直播高频梗
    ("jajaja", r"(?<![A-Za-z])(?:ja){2,}j?(?![A-Za-z])"),
    ("XD", r"(?<![A-Za-z])x+d+(?![A-Za-z])"),
    ("no manches", r"(?<![A-Za-z])no\s+(?:manches|mames)(?![A-Za-z])"),
    ("locura", r"(?<![A-Za-z])(?:qu[eé]\s+)?locura(?![A-Za-z])"),
    ("лол", r"(?<![\u0400-\u04ff])лол+(?![\u0400-\u04ff])"),
    # 无重叠确定性线性正则，彻底消除多项式回溯（ReDoS）风险
    ("ахах", r"(?<![\u0400-\u04ff])(?:ах(?:ах)+а?|ха(?:ха)+х?)(?![\u0400-\u04ff])"),
    ("жесть", r"(?<![\u0400-\u04ff])жесть(?![\u0400-\u04ff])"),
    ("имба", r"(?<![\u0400-\u04ff])имба(?![\u0400-\u04ff])"),
)

_COMPILED_HYPE_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = tuple(
    (label, re.compile(pattern, re.IGNORECASE)) for label, pattern in _HYPE_TERM_SPECS
)

_QUERY_SPLIT_RE = re.compile(r"[\s,，;；|]+")
_TIMECODE_RE = re.compile(r"^(?:(\d+):)?(\d{1,2}):(\d{1,2}(?:\.\d+)?)$")
_ASCII_LETTERS_RE = re.compile(r"^[A-Za-z]+$")


def _to_finite_float(value: Any) -> float | None:
    """安全解析浮点数或 MM:SS / HH:MM:SS 时间码，非法值返回 None。"""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        num = float(value)
        return num if math.isfinite(num) else None
    text = str(value).strip()
    if not text:
        return None
    try:
        num = float(text)
        return num if math.isfinite(num) else None
    except ValueError:
        pass
    match = _TIMECODE_RE.match(text)
    if match:
        hours = int(match.group(1) or 0)
        minutes = int(match.group(2))
        secs = float(match.group(3))
        total = hours * 3600.0 + minutes * 60.0 + secs
        return total if math.isfinite(total) else None
    return None


def _clean_text(raw: Any, *, max_len: int = _MAX_MESSAGE_TEXT_LEN) -> str:
    """清洗弹幕文本，压缩多余空白与控制字符并执行长度硬截断。"""
    if raw is None:
        return ""
    text = str(raw).replace("\r", " ").replace("\n", " ").replace("\t", " ")
    cleaned = re.sub(r"\s{2,}", " ", text).strip()
    if max_len > 0 and len(cleaned) > max_len:
        cleaned = cleaned[:max_len]
    return cleaned


def _normalize_message_list(
    raw_messages: list[dict[str, Any]],
    *,
    max_messages: int = _MAX_CHAT_MESSAGES,
) -> list[dict[str, Any]]:
    """过滤负时间与空白消息，强制文本长度与总条数硬上限，并按 time_sec 稳定升序排序。"""
    cleaned: list[dict[str, Any]] = []
    limit = max(1, int(max_messages))
    for item in raw_messages:
        if len(cleaned) >= limit:
            break
        if not isinstance(item, dict):
            continue
        t_val = _to_finite_float(item.get("time_sec"))
        if t_val is None or t_val < 0.0:
            continue
        text = _clean_text(item.get("text"), max_len=_MAX_MESSAGE_TEXT_LEN)
        if not text:
            continue
        author = _clean_text(item.get("author"), max_len=_MAX_AUTHOR_LEN)
        cleaned.append(
            {
                "time_sec": round(t_val, 3),
                "text": text,
                "author": author,
            }
        )
    cleaned.sort(key=lambda m: m["time_sec"])
    return cleaned


def _decode_chat_bytes(raw_bytes: bytes) -> str:
    """稳健解码弹幕字节流，原生支持 UTF-16 LE/BE BOM 与 UTF-8 BOM。"""
    if raw_bytes.startswith((b"\xff\xfe", b"\xfe\xff")):
        try:
            return raw_bytes.decode("utf-16")
        except UnicodeDecodeError:
            return raw_bytes.decode("utf-16", errors="replace")
    try:
        return raw_bytes.decode("utf-8-sig")
    except UnicodeDecodeError:
        return raw_bytes.decode("utf-8", errors="replace").lstrip("\ufeff")


def _validate_safe_xml_content(xml_text: str) -> None:
    """严格防御 XXE 与 Billion Laughs 实体扩展攻击（跳过 CDATA 正文防误报）。"""
    # 剥离合法 CDATA 段落后再检测 DOCTYPE / ENTITY 声明与命名实体引用，避免弹幕正文误报
    stripped_cdata = _XML_CDATA_RE.sub("", xml_text)
    if _XML_DANGEROUS_DECL_RE.search(stripped_cdata):
        raise ValueError(
            "检测到不安全的 XML 声明（包含 <!DOCTYPE 或 <!ENTITY），"
            "已拒绝解析以防御 XXE 与实体扩展攻击。"
        )
    # 额外拦截自定义命名实体引用（仅放行 XML 5 个内置字符实体）
    for match in _XML_ENTITY_REF_RE.finditer(stripped_cdata):
        entity_name = match.group(1)
        if entity_name not in _XML_SAFE_BUILTIN_ENTITIES:
            raise ValueError(
                f"检测到未授权的自定义 XML 实体引用 '&{entity_name};'，已拒绝解析以防御实体扩展风险。"
            )


def _create_safe_xml_parser() -> ET.XMLParser:
    """创建解析器级禁用 DOCTYPE / 自定义 ENTITY / 外部实体扩展的安全 XMLParser。"""
    parser = ET.XMLParser()
    expat = getattr(parser, "parser", None)
    if expat is not None:
        def _forbid_doctype(*_args: Any, **_kwargs: Any) -> None:
            raise ValueError("解析器已拦截 <!DOCTYPE 声明以防御 XXE 攻击。")

        def _forbid_entity(*_args: Any, **_kwargs: Any) -> None:
            raise ValueError("解析器已拦截自定义 <!ENTITY 声明以防御实体扩展攻击。")

        def _forbid_external(*_args: Any, **_kwargs: Any) -> int:
            raise ValueError("解析器已拦截外部实体引用。")

        if hasattr(expat, "StartDoctypeDeclHandler"):
            expat.StartDoctypeDeclHandler = _forbid_doctype
        if hasattr(expat, "EntityDeclHandler"):
            expat.EntityDeclHandler = _forbid_entity
        if hasattr(expat, "ExternalEntityRefHandler"):
            expat.ExternalEntityRefHandler = _forbid_external
    return parser


def _parse_bilibili_xml(xml_text: str) -> list[dict[str, Any]]:
    """流式安全解析 B站 XML 弹幕文件：<i ...><d p='123.45,1,25,...'>弹幕正文</d></i>。

    防爆设计：
    - 解析前剥离 CDATA 后强拦截 <!DOCTYPE / <!ENTITY 与自定义实体，且解析器层禁用实体扩展；
    - 采用 `ET.iterparse` 流式遍历，仅读 `<d>` 的直接 `elem.text`（绝不调用 `itertext()` 防止嵌套 O(N^2) 爆炸），
      每次读取后立即 `elem.clear()` 释放内存；
    - 严格遵守 `_MAX_CHAT_MESSAGES` 与 `_MAX_MESSAGE_TEXT_LEN` 上限。
    """
    _validate_safe_xml_content(xml_text)

    # 若原文件由 UTF-16 解码而来且头部声明了 UTF-16 编码，转为 UTF-8 字节流前重写声明避免 Expat 编码冲突
    normalized_xml = _XML_DECL_ENCODING_RE.sub(r"\1UTF-8\3", xml_text, count=1)
    xml_stream = io.BytesIO(normalized_xml.encode("utf-8"))
    parser = _create_safe_xml_parser()

    raw_messages: list[dict[str, Any]] = []
    try:
        for _event, elem in ET.iterparse(xml_stream, events=("end",), parser=parser):
            tag = elem.tag.split("}", 1)[-1].lower()
            if tag == "d" and len(raw_messages) < _MAX_CHAT_MESSAGES:
                p_attr = elem.attrib.get("p") or ""
                parts = p_attr.split(",")
                if parts:
                    time_sec = _to_finite_float(parts[0])
                    if time_sec is not None and time_sec >= 0.0:
                        # 只读直接文本节点 elem.text，严禁对嵌套标签调 itertext() 防 O(N^2) OOM
                        direct_text = _clean_text(elem.text, max_len=_MAX_MESSAGE_TEXT_LEN)
                        if direct_text:
                            author = _clean_text(
                                parts[6] if len(parts) >= 7 else "",
                                max_len=_MAX_AUTHOR_LEN,
                            )
                            raw_messages.append(
                                {
                                    "time_sec": round(time_sec, 3),
                                    "text": direct_text,
                                    "author": author,
                                }
                            )
            elem.clear()
    except ValueError:
        raise
    except ET.ParseError as exc:
        raise ValueError(f"B站 XML 弹幕解析失败，文件结构不合法：{exc}") from exc

    raw_messages.sort(key=lambda m: m["time_sec"])
    return raw_messages


def _parse_twitch_json(payload: dict[str, Any]) -> tuple[list[dict[str, Any]], float]:
    """解析 TwitchDownloader JSON：{"comments": [...]}。"""
    comments = payload.get("comments")
    if not isinstance(comments, list):
        raise ValueError("TwitchDownloader JSON 缺少合法的 comments 列表。")

    raw_messages: list[dict[str, Any]] = []
    for item in comments:
        if len(raw_messages) >= _MAX_CHAT_MESSAGES:
            break
        if not isinstance(item, dict):
            continue
        t_val = _to_finite_float(
            item.get("content_offset_seconds")
            if "content_offset_seconds" in item
            else item.get("offset_seconds", item.get("timestamp"))
        )
        if t_val is None:
            continue

        msg_obj = item.get("message")
        text = ""
        if isinstance(msg_obj, dict):
            text = _clean_text(msg_obj.get("body"))
            if not text and isinstance(msg_obj.get("fragments"), list):
                frag_texts = [
                    _clean_text(frag.get("text"))
                    for frag in msg_obj["fragments"]
                    if isinstance(frag, dict) and frag.get("text")
                ]
                text = " ".join(t for t in frag_texts if t)
        elif isinstance(msg_obj, str):
            text = _clean_text(msg_obj)

        commenter = item.get("commenter")
        author = ""
        if isinstance(commenter, dict):
            author = _clean_text(commenter.get("display_name") or commenter.get("name"))
        elif isinstance(commenter, str):
            author = _clean_text(commenter)

        raw_messages.append({"time_sec": t_val, "text": text, "author": author})

    messages = _normalize_message_list(raw_messages)
    video_meta = payload.get("video")
    hint_dur = 0.0
    if isinstance(video_meta, dict):
        v_len = _to_finite_float(video_meta.get("length") or video_meta.get("duration"))
        if v_len is not None and v_len > 0:
            hint_dur = v_len
    return messages, hint_dur


def _extract_yt_runs_text(runs: Any) -> str:
    """将 YouTube message.runs 列表（文字 + 自定义表情 shortcuts）拼接为完整弹幕文本。"""
    if not isinstance(runs, list):
        return ""
    parts: list[str] = []
    for run in runs:
        if not isinstance(run, dict):
            continue
        if "text" in run and run["text"] is not None:
            parts.append(str(run["text"]))
            continue
        emoji = run.get("emoji")
        if isinstance(emoji, dict):
            shortcuts = emoji.get("shortcuts")
            if isinstance(shortcuts, list) and shortcuts:
                parts.append(str(shortcuts[0]))
            elif isinstance(emoji.get("searchTerms"), list) and emoji["searchTerms"]:
                parts.append(str(emoji["searchTerms"][0]))
            elif emoji.get("emojiId"):
                parts.append(str(emoji["emojiId"]))
    return "".join(parts)


def _extract_yt_author(renderer: dict[str, Any]) -> str:
    """从 YouTube Live Chat renderer 提取发言者昵称。"""
    author_obj = renderer.get("authorName")
    if isinstance(author_obj, dict):
        if author_obj.get("simpleText"):
            return _clean_text(author_obj["simpleText"])
        if isinstance(author_obj.get("runs"), list):
            return _clean_text(_extract_yt_runs_text(author_obj["runs"]))
    elif isinstance(author_obj, str):
        return _clean_text(author_obj)
    return ""


def _extract_youtube_messages_from_obj(obj: Any, out: list[dict[str, Any]]) -> bool:
    """使用显式迭代栈（深度 <= 16）安全提取 YouTube Live Chat 弹幕，彻底杜绝深层嵌套 RecursionError。"""
    if not isinstance(obj, dict):
        return False

    matched_yt = False
    # 栈元素：(当前节点字典, 当前嵌套深度)
    stack: list[tuple[dict[str, Any], int]] = [(obj, 0)]

    while stack and len(out) < _MAX_CHAT_MESSAGES:
        cur, depth = stack.pop()
        if depth > _MAX_YT_STACK_DEPTH:
            continue

        replay_action = cur.get("replayChatItemAction")
        if isinstance(replay_action, dict):
            matched_yt = True
            offset_ms = _to_finite_float(replay_action.get("videoOffsetTimeMsec"))
            actions = replay_action.get("actions")
            if offset_ms is not None and isinstance(actions, list):
                time_sec = offset_ms / 1000.0
                for act in actions:
                    if len(out) >= _MAX_CHAT_MESSAGES:
                        break
                    if not isinstance(act, dict):
                        continue
                    add_act = act.get("addChatItemAction")
                    if not isinstance(add_act, dict):
                        continue
                    item = add_act.get("item")
                    if not isinstance(item, dict):
                        continue
                    renderer = (
                        item.get("liveChatTextMessageRenderer")
                        or item.get("liveChatPaidMessageRenderer")
                        or item.get("liveChatMembershipItemRenderer")
                    )
                    if not isinstance(renderer, dict):
                        continue
                    msg_obj = renderer.get("message")
                    text = ""
                    if isinstance(msg_obj, dict):
                        if isinstance(msg_obj.get("runs"), list):
                            text = _extract_yt_runs_text(msg_obj["runs"])
                        elif msg_obj.get("simpleText"):
                            text = str(msg_obj["simpleText"])
                    elif isinstance(msg_obj, str):
                        text = msg_obj
                    author = _extract_yt_author(renderer)
                    out.append({"time_sec": time_sec, "text": text, "author": author})
            continue

        # 兼容直接提供 videoOffsetTimeMsec + addChatItemAction 的精简导出
        if "videoOffsetTimeMsec" in cur and isinstance(cur.get("addChatItemAction"), dict):
            matched_yt = True
            offset_ms = _to_finite_float(cur.get("videoOffsetTimeMsec"))
            item = cur["addChatItemAction"].get("item")
            if offset_ms is not None and isinstance(item, dict):
                renderer = (
                    item.get("liveChatTextMessageRenderer")
                    or item.get("liveChatPaidMessageRenderer")
                    or item.get("liveChatMembershipItemRenderer")
                )
                if isinstance(renderer, dict):
                    msg_obj = renderer.get("message")
                    text = ""
                    if isinstance(msg_obj, dict):
                        text = (
                            _extract_yt_runs_text(msg_obj["runs"])
                            if isinstance(msg_obj.get("runs"), list)
                            else str(msg_obj.get("simpleText") or "")
                        )
                    out.append(
                        {
                            "time_sec": offset_ms / 1000.0,
                            "text": text,
                            "author": _extract_yt_author(renderer),
                        }
                    )
            continue

        # 兼容顶层包装 {"actions": [{"replayChatItemAction": ...}]}，逆序入栈以保持原顺序
        top_actions = cur.get("actions")
        if isinstance(top_actions, list) and top_actions and depth < _MAX_YT_STACK_DEPTH:
            for sub in reversed(top_actions):
                if isinstance(sub, dict):
                    stack.append((sub, depth + 1))

    return matched_yt


def _parse_generic_json_list(items: list[Any]) -> list[dict[str, Any]]:
    """解析通用精简 JSON 弹幕列表（受限于 _MAX_CHAT_MESSAGES）。"""
    raw_messages: list[dict[str, Any]] = []
    for item in items:
        if len(raw_messages) >= _MAX_CHAT_MESSAGES:
            break
        if not isinstance(item, dict):
            continue
        t_val: float | None = None
        for key in ("time_sec", "timestamp", "offset_sec", "offset", "time", "t"):
            if key in item and item[key] is not None:
                t_val = _to_finite_float(item[key])
                if t_val is not None:
                    break
        if t_val is None:
            for ms_key in ("time_ms", "offset_ms", "videoOffsetTimeMsec"):
                if ms_key in item and item[ms_key] is not None:
                    ms_val = _to_finite_float(item[ms_key])
                    if ms_val is not None:
                        t_val = ms_val / 1000.0
                        break
        if t_val is None:
            continue

        text = ""
        for text_key in ("text", "body", "message", "content", "msg"):
            val = item.get(text_key)
            if isinstance(val, str) and val.strip():
                text = val
                break
            if isinstance(val, dict) and isinstance(val.get("body"), str):
                text = val["body"]
                break

        author = ""
        for auth_key in ("author", "user", "username", "display_name", "sender"):
            val = item.get(auth_key)
            if isinstance(val, str) and val.strip():
                author = val
                break

        raw_messages.append({"time_sec": t_val, "text": text, "author": author})

    return _normalize_message_list(raw_messages)


def parse_chat_file(chat_path: Path | str, max_file_mb: float = 80.0) -> dict[str, Any]:
    """安全解析 Twitch / B站 / YouTube / 通用 JSON 弹幕文件为统一规范结构。

    安全防御：
    - 限制单文件体积不超过 `max_file_mb`（默认 80 MB）、消息数 <= 300,000、单条 <= 500 字符；
    - 支持 UTF-16 BOM / UTF-8 BOM 解码；
    - 流式 `ET.iterparse` 解析 XML（仅读直接 `elem.text` + 立即 `clear()`），拦截 `<!DOCTYPE`/`<!ENTITY`
      与自定义实体，捕获深层 JSON `RecursionError` 转 `ValueError`。

    返回：
        {
            "format": "twitch_json" | "bilibili_xml" | "youtube_live_chat" | "generic_json",
            "message_count": int,
            "duration_sec": float,
            "messages": [{"time_sec": float, "text": str, "author": str}],
        }
    """
    try:
        max_mb_val = float(max_file_mb)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"max_file_mb 必须为有限正数：{max_file_mb}") from exc
    if not math.isfinite(max_mb_val) or max_mb_val <= 0.0:
        raise ValueError(f"max_file_mb 必须为有限正数，当前传入：{max_file_mb}")

    path = Path(chat_path)
    if not path.is_file():
        raise FileNotFoundError(f"找不到弹幕文件：{path}")

    file_size_bytes = path.stat().st_size
    max_bytes = int(max_mb_val * 1024 * 1024)
    if file_size_bytes > max_bytes:
        actual_mb = file_size_bytes / (1024.0 * 1024.0)
        raise ValueError(
            f"弹幕文件体积过大（{actual_mb:.1f} MB），超过安全上限 {max_mb_val:.1f} MB，已拒绝加载以防内存耗尽。"
        )

    raw_bytes = path.read_bytes()
    content = _decode_chat_bytes(raw_bytes)

    stripped = content.strip()
    if not stripped:
        raise ValueError(f"弹幕文件内容为空：{path.name}")

    # 1. XML 格式识别（优先按后缀与尖括号首字符判断，先过 XXE/实体爆炸与流式解析防线）
    is_xml = path.suffix.lower() == ".xml" or stripped.startswith("<")
    if is_xml:
        messages = _parse_bilibili_xml(stripped)
        duration_sec = messages[-1]["time_sec"] if messages else 0.0
        return {
            "format": "bilibili_xml",
            "message_count": len(messages),
            "duration_sec": round(duration_sec, 3),
            "messages": messages,
        }

    # 2. 尝试整体 JSON 解析（捕获深层嵌套 RecursionError 转为 ValueError）
    parsed_whole: Any = None
    whole_json_ok = False
    try:
        parsed_whole = json.loads(stripped)
        whole_json_ok = True
    except RecursionError as exc:
        raise ValueError(f"弹幕 JSON 嵌套层级过深，已拒绝解析以防御栈溢出：{path.name}") from exc
    except json.JSONDecodeError:
        whole_json_ok = False

    if whole_json_ok:
        if isinstance(parsed_whole, dict):
            # 2a. TwitchDownloader JSON
            if isinstance(parsed_whole.get("comments"), list):
                messages, hint_dur = _parse_twitch_json(parsed_whole)
                last_t = messages[-1]["time_sec"] if messages else 0.0
                duration_sec = max(hint_dur, last_t)
                return {
                    "format": "twitch_json",
                    "message_count": len(messages),
                    "duration_sec": round(duration_sec, 3),
                    "messages": messages,
                }

            # 2b. YouTube Live Chat 单对象或 {"actions": [...]}
            yt_raw: list[dict[str, Any]] = []
            if _extract_youtube_messages_from_obj(parsed_whole, yt_raw):
                messages = _normalize_message_list(yt_raw)
                duration_sec = messages[-1]["time_sec"] if messages else 0.0
                return {
                    "format": "youtube_live_chat",
                    "message_count": len(messages),
                    "duration_sec": round(duration_sec, 3),
                    "messages": messages,
                }

            # 2c. 包装式通用 JSON（如 {"messages": [...]} / {"danmaku": [...]}）
            for list_key in ("messages", "danmaku", "chats", "items"):
                if isinstance(parsed_whole.get(list_key), list):
                    messages = _parse_generic_json_list(parsed_whole[list_key])
                    duration_sec = messages[-1]["time_sec"] if messages else 0.0
                    return {
                        "format": "generic_json",
                        "message_count": len(messages),
                        "duration_sec": round(duration_sec, 3),
                        "messages": messages,
                    }

            raise ValueError(f"无法识别该 JSON 弹幕文件的字段结构：{path.name}")

        if isinstance(parsed_whole, list):
            # 检查列表内部是否为 YouTube replayChatItemAction 对象
            yt_raw_list: list[dict[str, Any]] = []
            matched_yt = False
            for item in parsed_whole:
                if len(yt_raw_list) >= _MAX_CHAT_MESSAGES:
                    break
                if _extract_youtube_messages_from_obj(item, yt_raw_list):
                    matched_yt = True
            if matched_yt:
                messages = _normalize_message_list(yt_raw_list)
                duration_sec = messages[-1]["time_sec"] if messages else 0.0
                return {
                    "format": "youtube_live_chat",
                    "message_count": len(messages),
                    "duration_sec": round(duration_sec, 3),
                    "messages": messages,
                }

            messages = _parse_generic_json_list(parsed_whole)
            duration_sec = messages[-1]["time_sec"] if messages else 0.0
            return {
                "format": "generic_json",
                "message_count": len(messages),
                "duration_sec": round(duration_sec, 3),
                "messages": messages,
            }

        raise ValueError(f"弹幕 JSON 顶层必须是对象或列表：{path.name}")

    # 3. JSONL 逐行解析（yt-dlp *.live_chat.json 或每行一条通用 JSON）
    yt_jsonl_raw: list[dict[str, Any]] = []
    generic_jsonl_items: list[dict[str, Any]] = []
    matched_yt_lines = 0
    valid_json_lines = 0

    for line in stripped.splitlines():
        if len(yt_jsonl_raw) >= _MAX_CHAT_MESSAGES and len(generic_jsonl_items) >= _MAX_CHAT_MESSAGES:
            break
        row_text = line.strip()
        if not row_text:
            continue
        try:
            row_obj = json.loads(row_text)
        except RecursionError as exc:
            raise ValueError(f"弹幕 JSONL 行嵌套层级过深，已拒绝解析：{path.name}") from exc
        except json.JSONDecodeError:
            continue
        if not isinstance(row_obj, dict):
            continue
        valid_json_lines += 1
        if _extract_youtube_messages_from_obj(row_obj, yt_jsonl_raw):
            matched_yt_lines += 1
        elif len(generic_jsonl_items) < _MAX_CHAT_MESSAGES:
            generic_jsonl_items.append(row_obj)

    if matched_yt_lines > 0:
        messages = _normalize_message_list(yt_jsonl_raw)
        duration_sec = messages[-1]["time_sec"] if messages else 0.0
        return {
            "format": "youtube_live_chat",
            "message_count": len(messages),
            "duration_sec": round(duration_sec, 3),
            "messages": messages,
        }

    if valid_json_lines > 0 and generic_jsonl_items:
        messages = _parse_generic_json_list(generic_jsonl_items)
        duration_sec = messages[-1]["time_sec"] if messages else 0.0
        return {
            "format": "generic_json",
            "message_count": len(messages),
            "duration_sec": round(duration_sec, 3),
            "messages": messages,
        }

    raise ValueError(
        f"无法识别的弹幕文件格式（仅支持 TwitchDownloader JSON、B站 XML、YouTube Live Chat JSONL/JSON 或通用 JSON）：{path.name}"
    )


def _compile_keyword_pattern(term: str) -> re.Pattern[str]:
    """编译自定义检索词正则：纯短拉丁字母词加词边界防子串误伤，其余大小写不敏感匹配。"""
    cleaned = term.strip()
    escaped = re.escape(cleaned).replace(r"\ ", r"\s+")
    if _ASCII_LETTERS_RE.match(cleaned) and len(cleaned) <= 4:
        return re.compile(rf"(?<![A-Za-z]){escaped}(?![A-Za-z])", re.IGNORECASE)
    return re.compile(escaped, re.IGNORECASE)


def extract_hype_terms(text: str) -> list[str]:
    """提取单条弹幕中命中的跨文化直播高能梗词标签列表（去重保序）。"""
    if not text:
        return []
    hits: list[str] = []
    for label, pattern in _COMPILED_HYPE_PATTERNS:
        if pattern.search(text):
            hits.append(label)
    return hits


def analyze_chat_spikes(
    messages: list[dict[str, Any]],
    duration_sec: float | None = None,
    bucket_sec: float = 5.0,
    custom_keywords: list[str] | None = None,
    top_k: int = 20,
) -> dict[str, Any]:
    """计算弹幕时间桶热力图曲线，并用稳健中位数 + MAD 自适应检测刷屏爆点区间。

    参数：
        messages: `parse_chat_file()["messages"]` 或兼容格式列表。
        duration_sec: 视频总时长（秒）；留空则自动按最后一条弹幕时间推断。
        bucket_sec: 热力图时间桶宽度（秒，默认 5.0）。
        custom_keywords: 可选用户自定义检索词列表（命中权重加成并单独统计 `keyword_hits`）。
        top_k: 最多返回多少条高能弹幕峰值 `chat_peaks`（默认 20）。
    返回：
        含 `buckets`（含 0.0~1.0 `norm` 曲线）与 `chat_peaks` 的完整热力分析字典。
    """
    bucket_sec = _validate_positive_finite(bucket_sec, "bucket_sec")
    if duration_sec is not None:
        try:
            dur_check = float(duration_sec)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"duration_sec 必须为有限数字：{duration_sec}") from exc
        if not math.isfinite(dur_check) or dur_check < 0.0:
            raise ValueError(f"duration_sec 必须为有限非负数，当前传入：{duration_sec}")
        duration_sec = dur_check
    if top_k < 1:
        raise ValueError(f"top_k 必须大于等于 1，当前传入：{top_k}")

    valid_messages = _normalize_message_list(messages or [])

    # 编译用户自定义关键词
    compiled_custom: list[tuple[str, re.Pattern[str]]] = []
    seen_kw: set[str] = set()
    for kw in custom_keywords or []:
        c_kw = str(kw).strip()
        if not c_kw:
            continue
        key = c_kw.lower()
        if key in seen_kw:
            continue
        seen_kw.add(key)
        compiled_custom.append((c_kw, _compile_keyword_pattern(c_kw)))

    if not valid_messages:
        eff_dur = max(0.0, float(duration_sec or 0.0))
        num_empty = int(math.ceil(eff_dur / bucket_sec)) if eff_dur > 0 else 0
        empty_buckets = [
            {
                "bucket_index": i,
                "start_sec": round(i * bucket_sec, 2),
                "end_sec": round(min(eff_dur, (i + 1) * bucket_sec), 2),
                "count": 0,
                "hype_count": 0,
                "keyword_hits": 0,
                "norm": 0.0,
            }
            for i in range(num_empty)
        ]
        return {
            "bucket_sec": float(bucket_sec),
            "duration_sec": round(eff_dur, 2),
            "total_messages": 0,
            "total_hype_messages": 0,
            "baseline_median": 0.0,
            "baseline_mad": 0.0,
            "buckets": empty_buckets,
            "chat_peaks": [],
        }

    max_msg_time = valid_messages[-1]["time_sec"]
    if duration_sec is not None and float(duration_sec) > 0:
        eff_dur = max(float(duration_sec), max_msg_time + 0.001)
    else:
        eff_dur = max(bucket_sec, max_msg_time + 0.001)

    num_buckets = max(1, int(math.ceil(eff_dur / bucket_sec)))
    counts = np.zeros(num_buckets, dtype=np.int64)
    hype_counts = np.zeros(num_buckets, dtype=np.int64)
    kw_counts = np.zeros(num_buckets, dtype=np.int64)

    bucket_terms: list[Counter[str]] = [Counter() for _ in range(num_buckets)]
    bucket_samples: list[list[tuple[int, float, str]]] = [[] for _ in range(num_buckets)]

    total_hype = 0
    for msg in valid_messages:
        t_sec = msg["time_sec"]
        b_idx = min(num_buckets - 1, max(0, int(t_sec // bucket_sec)))
        text = msg["text"]
        counts[b_idx] += 1

        hype_hits = extract_hype_terms(text)
        if hype_hits:
            hype_counts[b_idx] += 1
            total_hype += 1
            for term in hype_hits:
                bucket_terms[b_idx][term] += 1

        kw_matched: list[str] = []
        if compiled_custom:
            for kw_label, kw_pat in compiled_custom:
                if kw_pat.search(text):
                    kw_matched.append(kw_label)
                    bucket_terms[b_idx][kw_label] += 1
            if kw_matched:
                kw_counts[b_idx] += 1

        # 严格容量上限 <= 12 条：按代表性优先级替换最低优条目，彻底杜绝高能刷屏桶无界暴涨 OOM
        priority = (2 if kw_matched else 0) + (1 if hype_hits else 0)
        samples_list = bucket_samples[b_idx]
        candidate_sample = (priority, t_sec, text[:120])
        if len(samples_list) < _MAX_BUCKET_SAMPLES:
            samples_list.append(candidate_sample)
        elif priority > 0:
            min_idx = min(range(len(samples_list)), key=lambda s_idx: samples_list[s_idx][0])
            if priority > samples_list[min_idx][0]:
                samples_list[min_idx] = candidate_sample

    # 综合活跃度曲线（总弹幕密度 + 多语种高能梗词权重 + 自定义关键词权重）
    raw_heat = counts.astype(np.float64) + 0.65 * hype_counts.astype(np.float64) + 1.0 * kw_counts.astype(np.float64)
    max_heat = float(np.max(raw_heat)) if raw_heat.size > 0 else 0.0

    buckets: list[dict[str, Any]] = []
    for i in range(num_buckets):
        b_start = round(i * bucket_sec, 2)
        b_end = round(min(eff_dur, (i + 1) * bucket_sec), 2)
        norm_val = round(float(raw_heat[i] / max_heat), 4) if max_heat > 0.0 else 0.0
        buckets.append(
            {
                "bucket_index": i,
                "start_sec": b_start,
                "end_sec": b_end,
                "count": int(counts[i]),
                "hype_count": int(hype_counts[i]),
                "keyword_hits": int(kw_counts[i]),
                "norm": norm_val,
            }
        )

    # 稳健中位数 + MAD 自适应峰值检测
    series = counts.astype(np.float64) + 0.5 * hype_counts.astype(np.float64) + 0.8 * kw_counts.astype(np.float64)
    median_val = float(np.median(series))
    abs_dev = np.abs(series - median_val)
    mad_val = float(np.median(abs_dev))

    if mad_val > 1e-6:
        robust_scale = 1.4826 * mad_val
    else:
        # 当超过半数时间桶弹幕量一致导致 MAD=0 时，使用均值绝对偏差（MeanAD -> sigma 近似）稳健回退
        mean_ad = float(np.mean(abs_dev))
        robust_scale = max(0.5, 1.2533 * mean_ad) if mean_ad > 1e-6 else 0.0

    if robust_scale <= 1e-6:
        z_scores = np.zeros(num_buckets, dtype=np.float64)
    else:
        z_scores = (series - median_val) / robust_scale

    median_count = float(np.median(counts))
    min_peak_bucket_count = max(3, int(math.ceil(median_count + 2.0)))

    spike_indices: list[int] = []
    for i in range(num_buckets):
        z_i = float(z_scores[i])
        c_i = int(counts[i])
        kw_i = int(kw_counts[i])
        if (z_i >= 2.5 and c_i >= min_peak_bucket_count) or (kw_i >= 2 and z_i >= 1.8 and c_i >= 2):
            spike_indices.append(i)

    # 将连续或仅隔 1 个桶（<= bucket_sec）的突增桶合并为完整刷屏事件段
    merged_runs: list[tuple[int, int]] = []
    for idx in spike_indices:
        if not merged_runs:
            merged_runs.append((idx, idx))
        else:
            prev_s, prev_e = merged_runs[-1]
            if idx - prev_e <= 2:
                merged_runs[-1] = (prev_s, idx)
            else:
                merged_runs.append((idx, idx))

    peaks_unsorted: list[dict[str, Any]] = []
    for b_start_idx, b_end_idx in merged_runs:
        seg_start = round(b_start_idx * bucket_sec, 2)
        seg_end = round(min(eff_dur, (b_end_idx + 1) * bucket_sec), 2)
        seg_dur = round(max(0.01, seg_end - seg_start), 2)

        sub_counts = counts[b_start_idx : b_end_idx + 1]
        sub_hype = hype_counts[b_start_idx : b_end_idx + 1]
        sub_kw = kw_counts[b_start_idx : b_end_idx + 1]
        sub_series = series[b_start_idx : b_end_idx + 1]
        sub_z = z_scores[b_start_idx : b_end_idx + 1]

        peak_rel = int(np.argmax(sub_series))
        peak_bucket_idx = b_start_idx + peak_rel
        peak_sec = round(min(eff_dur, (peak_bucket_idx + 0.5) * bucket_sec), 2)

        msg_count = int(np.sum(sub_counts))
        hype_cnt = int(np.sum(sub_hype))
        kw_cnt = int(np.sum(sub_kw))
        msgs_per_sec = round(msg_count / max(seg_dur, 1.0), 2)
        max_z = round(float(np.max(sub_z)), 2)

        agg_terms: Counter[str] = Counter()
        candidate_samples: list[tuple[int, float, str]] = []
        for bi in range(b_start_idx, b_end_idx + 1):
            agg_terms.update(bucket_terms[bi])
            candidate_samples.extend(bucket_samples[bi])

        top_terms = [term for term, _ in agg_terms.most_common(5)]

        # 优先挑选命中梗词/自定义词的代表性去重弹幕，最多 5 条
        candidate_samples.sort(key=lambda x: (-x[0], x[1]))
        sample_messages: list[str] = []
        seen_samples: set[str] = set()
        for _, _, s_txt in candidate_samples:
            norm_key = s_txt.lower()
            if norm_key in seen_samples:
                continue
            seen_samples.add(norm_key)
            sample_messages.append(s_txt)
            if len(sample_messages) >= 5:
                break

        peaks_unsorted.append(
            {
                "start_sec": seg_start,
                "end_sec": seg_end,
                "peak_sec": peak_sec,
                "duration_sec": seg_dur,
                "start_str": format_timestamp(seg_start),
                "end_str": format_timestamp(seg_end),
                "peak_str": format_timestamp(peak_sec),
                "msg_count": msg_count,
                "hype_count": hype_cnt,
                "keyword_hits": kw_cnt,
                "msgs_per_sec": msgs_per_sec,
                "z_score": max_z,
                "top_terms": top_terms,
                "sample_messages": sample_messages,
            }
        )

    peaks_unsorted.sort(key=lambda p: (-p["z_score"], -p["msg_count"], p["start_sec"]))
    chat_peaks: list[dict[str, Any]] = []
    for rank_idx, peak in enumerate(peaks_unsorted[:top_k], start=1):
        chat_peaks.append({"rank": rank_idx, **peak})

    return {
        "bucket_sec": float(bucket_sec),
        "duration_sec": round(eff_dur, 2),
        "total_messages": len(valid_messages),
        "total_hype_messages": total_hype,
        "baseline_median": round(median_val, 3),
        "baseline_mad": round(mad_val, 3),
        "buckets": buckets,
        "chat_peaks": chat_peaks,
    }


def _parse_search_query_terms(query: str) -> tuple[str, list[str]]:
    """校验并拆分弹幕搜索词：限制 query <= 200 字符、最多 10 个去重查询词，防 DoS。"""
    raw_query = str(query or "").strip()
    if len(raw_query) > _MAX_SEARCH_QUERY_LEN:
        raise ValueError(
            f"弹幕检索词过长（{len(raw_query)} 字符），超过安全上限 {_MAX_SEARCH_QUERY_LEN} 字符。"
        )
    terms: list[str] = []
    seen: set[str] = set()
    for part in _QUERY_SPLIT_RE.split(raw_query):
        cleaned = part.strip()
        if not cleaned:
            continue
        low = cleaned.lower()
        if low not in seen:
            seen.add(low)
            terms.append(cleaned)
            if len(terms) >= _MAX_SEARCH_TERMS:
                break
    return raw_query, terms


def _validate_positive_finite(val: float, name: str) -> float:
    """校验数值必须为有限正数。"""
    try:
        num = float(val)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} 必须为有限正数，当前传入：{val}") from exc
    if not math.isfinite(num) or num <= 0.0:
        raise ValueError(f"{name} 必须为有限正数，当前传入：{val}")
    return num


def _build_search_curve_and_top(
    bucket_hits: np.ndarray,
    eff_dur: float,
    bucket_sec: float,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """将按桶命中统计数组转换为前端曲线与 Top 10 命中桶列表。"""
    max_hit = int(np.max(bucket_hits)) if bucket_hits.size > 0 else 0
    curve: list[dict[str, Any]] = []
    for i in range(int(bucket_hits.size)):
        hits_i = int(bucket_hits[i])
        b_start = round(i * bucket_sec, 2)
        b_end = round(min(eff_dur, (i + 1) * bucket_sec), 2)
        curve.append(
            {
                "bucket_index": i,
                "start_sec": b_start,
                "end_sec": b_end,
                "start_str": format_timestamp(b_start),
                "end_str": format_timestamp(b_end),
                "hits": hits_i,
                "norm": round(hits_i / max_hit, 4) if max_hit > 0 else 0.0,
            }
        )

    top_buckets = sorted(
        (b for b in curve if b["hits"] > 0),
        key=lambda b: (-b["hits"], b["start_sec"]),
    )[:10]
    return curve, top_buckets


def search_chat_keywords(
    messages: list[dict[str, Any]],
    query: str,
    bucket_sec: float = 10.0,
) -> dict[str, Any]:
    """Peepovod 风格纯本地弹幕梗词检索（内存兼容版，支持最多 10 词 OR 匹配、q <= 200 字符）。"""
    safe_bucket = _validate_positive_finite(bucket_sec, "bucket_sec")
    raw_query, terms = _parse_search_query_terms(query)

    valid_messages = _normalize_message_list(messages or [])
    if not terms or not valid_messages:
        return {
            "query": raw_query,
            "terms": terms,
            "bucket_sec": safe_bucket,
            "match_count": 0,
            "matches": [],
            "curve": [],
            "top_buckets": [],
        }

    compiled = [(t, _compile_keyword_pattern(t)) for t in terms]
    eff_dur = max(safe_bucket, valid_messages[-1]["time_sec"] + 0.001)
    num_buckets = max(1, int(math.ceil(eff_dur / safe_bucket)))
    bucket_hits = np.zeros(num_buckets, dtype=np.int64)

    matches: list[dict[str, Any]] = []
    for msg in valid_messages:
        text = msg["text"]
        hit_terms = [label for label, pat in compiled if pat.search(text)]
        if not hit_terms:
            continue
        t_sec = msg["time_sec"]
        b_idx = min(num_buckets - 1, max(0, int(t_sec // safe_bucket)))
        bucket_hits[b_idx] += 1
        matches.append(
            {
                "time_sec": t_sec,
                "time_str": format_timestamp(t_sec),
                "text": text,
                "author": msg["author"],
                "matched_terms": hit_terms,
            }
        )

    curve, top_buckets = _build_search_curve_and_top(bucket_hits, eff_dur, safe_bucket)
    return {
        "query": raw_query,
        "terms": terms,
        "bucket_sec": safe_bucket,
        "match_count": len(matches),
        "matches": matches,
        "curve": curve,
        "top_buckets": top_buckets,
    }


def save_chat_messages_db(
    db_path: Path | str,
    messages: list[dict[str, Any]],
) -> Path:
    """将原始弹幕以 SQLite + FTS5 索引表原子落盘，与轻量化 `*_chat_radar.json` 彻底拆离防 OOM。"""
    dst_db = Path(db_path)
    dst_db.parent.mkdir(parents=True, exist_ok=True)
    valid_messages = _normalize_message_list(messages or [], max_messages=_MAX_CHAT_MESSAGES)
    max_time = valid_messages[-1]["time_sec"] if valid_messages else 0.0

    tmp_db = dst_db.parent / f".{dst_db.name}.{uuid.uuid4().hex[:8]}.tmp"
    conn: sqlite3.Connection | None = None
    try:
        conn = sqlite3.connect(str(tmp_db))
        conn.execute("PRAGMA journal_mode=OFF")
        conn.execute("PRAGMA synchronous=OFF")
        conn.execute("PRAGMA temp_store=MEMORY")
        conn.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        conn.execute(
            """
            CREATE TABLE messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                time_sec REAL NOT NULL,
                text TEXT NOT NULL,
                author TEXT NOT NULL
            )
            """
        )
        conn.execute("CREATE INDEX idx_messages_time ON messages(time_sec)")
        conn.execute(
            """
            CREATE VIRTUAL TABLE messages_fts USING fts5(
                text,
                content='messages',
                content_rowid='id',
                tokenize='unicode61'
            )
            """
        )
        if valid_messages:
            conn.executemany(
                "INSERT INTO messages (time_sec, text, author) VALUES (?, ?, ?)",
                ((m["time_sec"], m["text"], m["author"]) for m in valid_messages),
            )
            conn.execute("INSERT INTO messages_fts(messages_fts) VALUES('rebuild')")
        conn.executemany(
            "INSERT INTO meta (key, value) VALUES (?, ?)",
            (
                ("message_count", str(len(valid_messages))),
                ("max_time_sec", f"{max_time:.3f}"),
                ("schema_version", "1"),
            ),
        )
        conn.commit()
    finally:
        if conn is not None:
            conn.close()

    try:
        for attempt in range(3):
            try:
                os.replace(tmp_db, dst_db)
                break
            except PermissionError:
                if attempt == 2:
                    raise
                time.sleep(0.12)
    finally:
        if tmp_db.exists():
            tmp_db.unlink(missing_ok=True)

    return dst_db.resolve()


def search_chat_messages_db(
    db_path: Path | str,
    query: str,
    bucket_sec: float = 10.0,
    *,
    max_matches: int = 300,
) -> dict[str, Any]:
    """基于 SQLite FTS5 / 索引表流式检索弹幕梗词，内存恒定且零 JSON OOM。"""
    safe_bucket = _validate_positive_finite(bucket_sec, "bucket_sec")
    raw_query, terms = _parse_search_query_terms(query)
    safe_max_matches = max(1, int(max_matches))

    src_db = Path(db_path)
    if not src_db.is_file() or not terms:
        return {
            "query": raw_query,
            "terms": terms,
            "bucket_sec": safe_bucket,
            "match_count": 0,
            "matches": [],
            "matches_truncated": False,
            "curve": [],
            "top_buckets": [],
        }

    compiled = [(t, _compile_keyword_pattern(t)) for t in terms]
    conn = sqlite3.connect(str(src_db), timeout=5.0)
    try:
        meta_rows = dict(conn.execute("SELECT key, value FROM meta").fetchall())
        msg_count = int(meta_rows.get("message_count", "0") or 0)
        max_time = _to_finite_float(meta_rows.get("max_time_sec")) or 0.0
        if msg_count <= 0:
            return {
                "query": raw_query,
                "terms": terms,
                "bucket_sec": safe_bucket,
                "match_count": 0,
                "matches": [],
                "matches_truncated": False,
                "curve": [],
                "top_buckets": [],
            }

        eff_dur = max(safe_bucket, max_time + 0.001)
        num_buckets = max(1, int(math.ceil(eff_dur / safe_bucket)))
        bucket_hits = np.zeros(num_buckets, dtype=np.int64)

        # 纯短拉丁词（词边界语义）走 FTS5 毫秒级倒排召回；含 CJK/符号/子串词走参数化 INSTR 预筛防漏检
        can_use_fts = all(bool(_ASCII_LETTERS_RE.match(t)) and len(t) <= 4 for t in terms)
        if can_use_fts:
            fts_expr = " OR ".join(f'"{t}"' for t in terms)
            cursor = conn.execute(
                """
                SELECT m.time_sec, m.text, m.author
                FROM messages_fts f
                JOIN messages m ON m.id = f.rowid
                WHERE messages_fts MATCH ?
                ORDER BY m.time_sec ASC, m.id ASC
                """,
                (fts_expr,),
            )
        else:
            where_clauses: list[str] = []
            sql_params: list[str] = []
            for t in terms:
                # 若词内含空白，取最长非空段做 SQL 粗筛，再由 Python 精确正则复核
                pieces = [p for p in t.lower().split() if p]
                needle = max(pieces, key=len) if pieces else t.lower()
                where_clauses.append("instr(lower(text), ?) > 0")
                sql_params.append(needle)
            sql = (
                "SELECT time_sec, text, author FROM messages "
                f"WHERE {' OR '.join(where_clauses)} "
                "ORDER BY time_sec ASC, id ASC"
            )
            cursor = conn.execute(sql, sql_params)

        total_matches = 0
        matches: list[dict[str, Any]] = []
        while True:
            batch = cursor.fetchmany(4000)
            if not batch:
                break
            for t_sec_raw, text, author in batch:
                hit_terms = [label for label, pat in compiled if pat.search(text)]
                if not hit_terms:
                    continue
                t_sec = float(t_sec_raw)
                b_idx = min(num_buckets - 1, max(0, int(t_sec // safe_bucket)))
                bucket_hits[b_idx] += 1
                total_matches += 1
                if len(matches) < safe_max_matches:
                    matches.append(
                        {
                            "time_sec": round(t_sec, 3),
                            "time_str": format_timestamp(t_sec),
                            "text": text,
                            "author": author,
                            "matched_terms": hit_terms,
                        }
                    )
    finally:
        conn.close()

    curve, top_buckets = _build_search_curve_and_top(bucket_hits, eff_dur, safe_bucket)
    return {
        "query": raw_query,
        "terms": terms,
        "bucket_sec": safe_bucket,
        "match_count": total_matches,
        "matches": matches,
        "matches_truncated": total_matches > len(matches),
        "curve": curve,
        "top_buckets": top_buckets,
    }


def fuse_audio_and_chat_peaks(
    audio_peaks: list[dict[str, Any]],
    chat_peaks: list[dict[str, Any]],
    top_k: int = 20,
) -> list[dict[str, Any]]:
    """将音频高能段与弹幕刷屏峰值做证据加权双雷达融合。

    特性：
    - 兼顾直播弹幕反应延迟（弹幕高潮常晚于声音爆发 0~12 秒），自动把同一名场面的音/弹段关联为 `audio+chat`；
    - 保留纯音频爆发段（`audio`）与纯弹幕名场面（`chat`，如冷面吐槽、无声极限操作），统一按融合分排序输出。
    """
    if top_k < 1:
        raise ValueError(f"top_k 必须大于等于 1，当前传入：{top_k}")

    clean_audio: list[dict[str, Any]] = []
    for seg in audio_peaks or []:
        if not isinstance(seg, dict):
            continue
        s = _to_finite_float(seg.get("start_sec"))
        e = _to_finite_float(seg.get("end_sec"))
        if s is None or e is None or e <= s:
            continue
        peak_rms = float(_to_finite_float(seg.get("peak_rms")) or 0.0)
        mean_rms = float(_to_finite_float(seg.get("mean_rms")) or peak_rms)
        clean_audio.append(
            {
                "start_sec": round(max(0.0, s), 2),
                "end_sec": round(e, 2),
                "peak_rms": round(max(0.0, peak_rms), 4),
                "mean_rms": round(max(0.0, mean_rms), 4),
            }
        )

    clean_chat: list[dict[str, Any]] = []
    for seg in chat_peaks or []:
        if not isinstance(seg, dict):
            continue
        s = _to_finite_float(seg.get("start_sec"))
        e = _to_finite_float(seg.get("end_sec"))
        if s is None or e is None or e <= s:
            continue
        z_score = float(_to_finite_float(seg.get("z_score")) or 0.0)
        msg_count = int(seg.get("msg_count") or 0)
        msgs_per_sec = float(_to_finite_float(seg.get("msgs_per_sec")) or 0.0)
        clean_chat.append(
            {
                "start_sec": round(max(0.0, s), 2),
                "end_sec": round(e, 2),
                "peak_sec": round(float(_to_finite_float(seg.get("peak_sec")) or ((s + e) / 2.0)), 2),
                "z_score": round(max(0.0, z_score), 2),
                "msg_count": msg_count,
                "msgs_per_sec": round(max(0.0, msgs_per_sec), 2),
                "top_terms": list(seg.get("top_terms") or [])[:5],
                "sample_messages": list(seg.get("sample_messages") or [])[:5],
            }
        )

    if not clean_audio and not clean_chat:
        return []

    max_audio_rms = max((a["peak_rms"] for a in clean_audio), default=0.0)
    max_chat_strength = max(
        (c["z_score"] + 0.25 * c["msgs_per_sec"] for c in clean_chat),
        default=0.0,
    )

    for a in clean_audio:
        a["audio_score"] = round(a["peak_rms"] / max_audio_rms, 4) if max_audio_rms > 0 else 0.5
    for c in clean_chat:
        strength = c["z_score"] + 0.25 * c["msgs_per_sec"]
        c["chat_score"] = round(strength / max_chat_strength, 4) if max_chat_strength > 0 else 0.5

    # 允许弹幕晚于音频爆发最多 12 秒，或音频晚于弹幕起点最多 5 秒视为同一高能事件
    chat_lag_tol = 12.0
    audio_lag_tol = 5.0

    matched_chat_indices: set[int] = set()
    fused_candidates: list[dict[str, Any]] = []

    for a in clean_audio:
        a_s, a_e = a["start_sec"], a["end_sec"]
        linked_chats: list[tuple[int, dict[str, Any]]] = []
        for c_idx, c in enumerate(clean_chat):
            c_s, c_e = c["start_sec"], c["end_sec"]
            if c_s <= a_e + chat_lag_tol and a_s <= c_e + audio_lag_tol:
                linked_chats.append((c_idx, c))

        if linked_chats:
            for c_idx, _ in linked_chats:
                matched_chat_indices.add(c_idx)
            f_start = min([a_s] + [c["start_sec"] for _, c in linked_chats])
            f_end = max([a_e] + [c["end_sec"] for _, c in linked_chats])
            best_chat = max(linked_chats, key=lambda pair: pair[1]["chat_score"])[1]
            total_chat_msgs = sum(c["msg_count"] for _, c in linked_chats)
            max_z = max(c["z_score"] for _, c in linked_chats)
            top_terms: list[str] = []
            samples: list[str] = []
            for _, c in linked_chats:
                for t in c["top_terms"]:
                    if t not in top_terms:
                        top_terms.append(t)
                for s_msg in c["sample_messages"]:
                    if s_msg not in samples:
                        samples.append(s_msg)

            fusion_score = round(0.55 * a["audio_score"] + 0.55 * best_chat["chat_score"] + 0.35, 4)
            fused_candidates.append(
                {
                    "start_sec": round(f_start, 2),
                    "end_sec": round(f_end, 2),
                    "fusion_source": "audio+chat",
                    "fusion_score": fusion_score,
                    "audio_score": a["audio_score"],
                    "chat_score": best_chat["chat_score"],
                    "peak_rms": a["peak_rms"],
                    "mean_rms": a["mean_rms"],
                    "chat_msg_count": total_chat_msgs,
                    "chat_z_score": max_z,
                    "top_terms": top_terms[:5],
                    "sample_messages": samples[:5],
                }
            )
        else:
            fused_candidates.append(
                {
                    "start_sec": a_s,
                    "end_sec": a_e,
                    "fusion_source": "audio",
                    "fusion_score": round(0.72 * a["audio_score"], 4),
                    "audio_score": a["audio_score"],
                    "chat_score": 0.0,
                    "peak_rms": a["peak_rms"],
                    "mean_rms": a["mean_rms"],
                    "chat_msg_count": 0,
                    "chat_z_score": 0.0,
                    "top_terms": [],
                    "sample_messages": [],
                }
            )

    # 纳入纯弹幕爆点（无声神操作、冷面神吐槽、画面名场面）
    for c_idx, c in enumerate(clean_chat):
        if c_idx in matched_chat_indices:
            continue
        fused_candidates.append(
            {
                "start_sec": c["start_sec"],
                "end_sec": c["end_sec"],
                "fusion_source": "chat",
                "fusion_score": round(0.78 * c["chat_score"], 4),
                "audio_score": 0.0,
                "chat_score": c["chat_score"],
                "peak_rms": 0.0,
                "mean_rms": 0.0,
                "chat_msg_count": c["msg_count"],
                "chat_z_score": c["z_score"],
                "top_terms": c["top_terms"][:5],
                "sample_messages": c["sample_messages"][:5],
            }
        )

    # 对重叠的融合候选再做一次合并去重，避免两个接近的音频峰共享同一弹幕峰产生重复项
    fused_candidates.sort(key=lambda x: (x["start_sec"], -x["fusion_score"]))
    merged: list[dict[str, Any]] = []
    for cand in fused_candidates:
        if not merged:
            merged.append(cand)
            continue
        prev = merged[-1]
        if cand["start_sec"] <= prev["end_sec"]:
            prev["end_sec"] = round(max(prev["end_sec"], cand["end_sec"]), 2)
            sources = {prev["fusion_source"], cand["fusion_source"]}
            if "audio+chat" in sources or ("audio" in sources and "chat" in sources):
                prev["fusion_source"] = "audio+chat"
            prev["audio_score"] = round(max(prev["audio_score"], cand["audio_score"]), 4)
            prev["chat_score"] = round(max(prev["chat_score"], cand["chat_score"]), 4)
            if prev["fusion_source"] == "audio+chat":
                prev["fusion_score"] = round(
                    max(
                        prev["fusion_score"],
                        cand["fusion_score"],
                        0.55 * prev["audio_score"] + 0.55 * prev["chat_score"] + 0.35,
                    ),
                    4,
                )
            else:
                prev["fusion_score"] = round(max(prev["fusion_score"], cand["fusion_score"]), 4)
            prev["peak_rms"] = round(max(prev["peak_rms"], cand["peak_rms"]), 4)
            prev["mean_rms"] = round(max(prev["mean_rms"], cand["mean_rms"]), 4)
            prev["chat_msg_count"] = max(prev["chat_msg_count"], cand["chat_msg_count"])
            prev["chat_z_score"] = round(max(prev["chat_z_score"], cand["chat_z_score"]), 2)
            for t in cand["top_terms"]:
                if t not in prev["top_terms"] and len(prev["top_terms"]) < 5:
                    prev["top_terms"].append(t)
            for s_msg in cand["sample_messages"]:
                if s_msg not in prev["sample_messages"] and len(prev["sample_messages"]) < 5:
                    prev["sample_messages"].append(s_msg)
        else:
            merged.append(cand)

    source_priority = {"audio+chat": 2, "chat": 1, "audio": 0}
    merged.sort(
        key=lambda x: (-x["fusion_score"], -source_priority.get(x["fusion_source"], 0), x["start_sec"])
    )

    output: list[dict[str, Any]] = []
    for rank_idx, item in enumerate(merged[:top_k], start=1):
        s_sec = item["start_sec"]
        e_sec = item["end_sec"]
        output.append(
            {
                "rank": rank_idx,
                "start_sec": s_sec,
                "end_sec": e_sec,
                "duration_sec": round(max(0.0, e_sec - s_sec), 2),
                "start_str": format_timestamp(s_sec),
                "end_str": format_timestamp(e_sec),
                "fusion_source": item["fusion_source"],
                "fusion_score": item["fusion_score"],
                "audio_score": item["audio_score"],
                "chat_score": item["chat_score"],
                "peak_rms": item["peak_rms"],
                "mean_rms": item["mean_rms"],
                "chat_msg_count": item["chat_msg_count"],
                "chat_z_score": item["chat_z_score"],
                "top_terms": item["top_terms"],
                "sample_messages": item["sample_messages"],
            }
        )
    return output
