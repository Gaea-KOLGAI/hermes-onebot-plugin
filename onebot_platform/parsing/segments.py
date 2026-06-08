from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple
from urllib.parse import urlparse

MAX_MULTIMSG_PREVIEW = 200
MAX_TITLE_PREVIEW = 80

_CQ_SEGMENT_RE = re.compile(r'\[CQ:(\w+)((?:,[^,\]]+=[^,\]]*)*)\]')
_XML_TITLE_RE = re.compile(r'<title[^>]*>(.*?)</title>', re.IGNORECASE)
_XML_BRIEF_RE = re.compile(r'action="[^"]*"[^>]*brief="([^"]*)"', re.IGNORECASE)
_CODEBLOCK_RE = re.compile(r'```[\s\S]*?```')
_EXCESSIVE_NEWLINES_RE = re.compile(r'\n{3,}')
_MD_COMPILED = [
    (re.compile(r'\*\*(.+?)\*\*'), r'\1'),
    (re.compile(r'\*(.+?)\*'), r'\1'),
    (re.compile(r'__(.+?)__'), r'\1'),
    (re.compile(r'_(.+?)_'), r'\1'),
    (re.compile(r'^#{1,6}\s*', re.MULTILINE), ''),
    (re.compile(r'^>\s*', re.MULTILINE), ''),
    (re.compile(r'```\w*\n?'), ''),
    (re.compile(r'`(.+?)`'), r'\1'),
    (re.compile(r'\[(.+?)\]\((.+?)\)'), r'\1 (\2)'),
    (re.compile(r'^[\-\*]\s+', re.MULTILINE), '• '),
]


def strip_markdown(text: str) -> str:
    for pat, repl in _MD_COMPILED:
        text = pat.sub(repl, text)
    return text.strip()


def _strip_slash(text: str) -> str:
    return text[1:] if text.startswith("/") else text


def _cq_unescape(s: str) -> str:
    return s.replace("&#91;", "[").replace("&#93;", "]").replace("&#44;", ",").replace("&#10;", "\n").replace("&#13;", "\r").replace("&amp;", "&")


def _extract_segments(message: Any) -> List[Dict[str, Any]]:
    if isinstance(message, str):
        segments = []
        last_end = 0
        for match in _CQ_SEGMENT_RE.finditer(message):
            if match.start() > last_end:
                text = message[last_end:match.start()].strip()
                if text:
                    segments.append({"type": "text", "data": {"text": _cq_unescape(text)}})
            seg_type = match.group(1)
            params = {}
            if match.group(2):
                for kv in match.group(2).lstrip(",").split(","):
                    if "=" in kv:
                        key, value = kv.split("=", 1)
                        params[key] = _cq_unescape(value)
            segments.append({"type": seg_type, "data": params})
            last_end = match.end()
        if last_end < len(message):
            text = message[last_end:].strip()
            if text:
                segments.append({"type": "text", "data": {"text": _cq_unescape(text)}})
        return segments
    if isinstance(message, list):
        return [segment for segment in message if isinstance(segment, dict)]
    return []


def _segments_text(segments: List[Dict[str, Any]]) -> str:
    return "".join(
        _cq_unescape(str((seg.get("data") or {}).get("text", "")))
        for seg in segments
        if isinstance(seg, dict) and seg.get("type") == "text"
    ).strip()


def _extract_text_from_message(message: Any) -> str:
    if isinstance(message, str):
        return _segments_text(_extract_segments(message))
    if isinstance(message, list):
        return _segments_text(message)
    return ""


def _extract_first(segments: List[Dict], seg_type: str, key: str = "url", fallback: str = "") -> Optional[str]:
    for seg in segments:
        if seg.get("type") == seg_type:
            data = seg.get("data") or {}
            val = data.get(key)
            if val:
                return val
            if fallback:
                return data.get(fallback, "")
            return ""
    return None


def _extract_seg_text(segments: List[Dict], seg_type: str, formatter) -> Optional[str]:
    for seg in segments:
        if seg.get("type") == seg_type:
            data = seg.get("data") or {}
            result = formatter(data)
            if result:
                return result
    return None


def _extract_images(segments: List[Dict]) -> List[str]:
    return [
        seg["data"].get("url") or seg["data"].get("file", "")
        for seg in segments
        if seg.get("type") == "image" and seg.get("data")
        and (seg["data"].get("url") or seg["data"].get("file"))
    ]


def _extract_voice(segs):
    return _extract_first(segs, "record", "url", fallback="file")


def _extract_video(segs):
    return _extract_first(segs, "video", "url", fallback="file")


def _extract_face(segs):
    return _extract_first(segs, "face", "id")


def _extract_reply(segs):
    return _extract_first(segs, "reply", "id")


def _extract_at(segments: List[Dict]) -> List[str]:
    return [str((seg.get("data") or {}).get("qq", "")) for seg in segments if seg.get("type") == "at" and (seg.get("data") or {}).get("qq")]


def _extract_forward(segments: List[Dict]) -> Optional[str]:
    for seg in segments:
        seg_type = seg.get("type", "")
        if seg_type in ("forward", "forward_msg", "nodes"):
            data = seg.get("data") or {}
            fid = data.get("id") or data.get("forward_id") or data.get("message_id") or ""
            if fid:
                return str(fid)
    return None


def _extract_multimsg_text(obj: dict) -> Optional[str]:
    if not isinstance(obj, dict) or obj.get("app") != "com.tencent.multimsg":
        return None
    config = obj.get("config")
    if not isinstance(config, dict) or config.get("forward") != 1:
        return None
    detail = obj.get("meta", {}).get("detail")
    if not isinstance(detail, dict):
        return None
    news_items = detail.get("news")
    if not isinstance(news_items, list):
        return None
    texts = [item["text"].strip().replace("[图片]", "").strip() for item in news_items if isinstance(item, dict) and isinstance(item.get("text"), str)]
    texts = [text for text in texts if text]
    return "\n".join(texts).strip() or None


def _json_card_values(obj: Any, limit: int = 16) -> List[str]:
    keys = {"title", "desc", "description", "summary", "prompt", "text", "content", "name", "brief", "source", "tag", "url"}
    values: List[str] = []
    seen = set()

    def add(value):
        if not isinstance(value, str):
            return
        value = value.strip().replace("[图片]", "").strip()
        if not value or value in seen:
            return
        if len(value) > MAX_MULTIMSG_PREVIEW:
            value = value[:MAX_MULTIMSG_PREVIEW] + "…"
        seen.add(value)
        values.append(value)

    def walk(item, depth=0, key=""):
        if len(values) >= limit or depth > 5:
            return
        if isinstance(item, dict):
            for sub_key, sub_value in item.items():
                lowered = str(sub_key).lower()
                if lowered in keys:
                    add(sub_value)
                walk(sub_value, depth + 1, lowered)
        elif isinstance(item, list):
            for child in item[:20]:
                walk(child, depth + 1, key)
        elif key in keys:
            add(item)

    walk(obj)
    return values


def _extract_json_card(segments: List[Dict]) -> Optional[str]:
    for seg in segments:
        if seg.get("type") != "json":
            continue
        raw = (seg.get("data") or {}).get("data", "")
        if not raw:
            return "[卡片消息]"
        if isinstance(raw, str):
            raw = _cq_unescape(raw)
        try:
            obj = json.loads(raw)
            multimsg_text = _extract_multimsg_text(obj)
            if multimsg_text:
                if len(multimsg_text) > MAX_MULTIMSG_PREVIEW:
                    multimsg_text = multimsg_text[:MAX_MULTIMSG_PREVIEW] + "…"
                return f"[合并转发预览]\n{multimsg_text}"
            values = _json_card_values(obj)
            if values:
                return "[卡片消息]\n" + "\n".join(f"- {value}" for value in values[:10])
        except (json.JSONDecodeError, AttributeError, TypeError):
            pass
        return "[卡片消息]"
    return None


def _extract_xml(segments: List[Dict]) -> Optional[str]:
    for seg in segments:
        if seg.get("type") != "xml":
            continue
        raw = (seg.get("data") or {}).get("data", "")
        if not raw:
            return "[XML消息]"
        for pattern in (_XML_TITLE_RE, _XML_BRIEF_RE):
            match = pattern.search(raw)
            if match:
                value = match.group(1).strip()[:MAX_TITLE_PREVIEW]
                return f"[XML消息: {value}]"
        return "[XML消息]"
    return None


def _fmt_rps(data):
    rid = str(data.get("id", data.get("result", "")))
    rps_map = {"0": "石头", "1": "剪刀", "2": "布"}
    return f"[猜拳: {rps_map.get(rid, rid)}]"


_SEGMENT_FORMATTERS: Dict[str, Callable] = {
    "file": lambda d: (
        f"[文件: {d.get('name') or d.get('file') or '未知文件'} {d.get('url') or d.get('file_url') or ''}]"
        if (d.get("url") or d.get("file_url") or "").startswith("http")
        else f"[文件: {d.get('name') or d.get('file') or '未知文件'} (file_id={d.get('file_id') or d.get('id') or ''})]"
        if d.get("file_id") or d.get("id")
        else f"[文件: {d.get('name') or d.get('file') or '未知文件'}]"
    ),
    "location": lambda d: (
        f"[位置: {d.get('title', '')} ({d.get('lat', '')},{d.get('lon', '')})]" if d.get("title")
        else f"[位置: ({d.get('lat', '')},{d.get('lon', '')})]" if d.get("lat") and d.get("lon")
        else "[位置]"
    ),
    "share": lambda d: (
        f"[分享: {d.get('title', '')} {d.get('url', '')}]" if d.get("title") and d.get("url")
        else f"[分享: {d.get('title', '')}]" if d.get("title")
        else f"[分享: {d.get('url', '')}]" if d.get("url")
        else "[分享]"
    ),
    "contact": lambda d: (
        f"[推荐群: {d.get('id', '')}]" if d.get("type") == "group"
        else f"[推荐好友: {d.get('id', '')}]"
    ),
    "music": lambda d: (
        f"[音乐: {d.get('title', '')} {d.get('type', '')}]" if d.get("title")
        else f"[音乐: {d.get('type', '')}:{d.get('id', '')}]" if d.get("id")
        else f"[音乐: {d.get('type', '')}]"
    ),
    "mface": lambda d: f"[商城表情: {n}]" if (n := d.get("name") or d.get("face_id") or d.get("emoji_id") or "") else "[商城表情]",
    "rps": _fmt_rps,
    "dice": lambda d: f"[骰子: {d.get('id', d.get('result', ''))}]",
    "basketball": lambda d: f"[篮球: {d.get('id', d.get('result', ''))}]",
    "poke": lambda d: f"[戳一戳: {d.get('qq') or d.get('target_id') or ''}]",
    "anonymous": lambda d: f"[匿名: {d.get('name') or d.get('id') or ''}]",
    "markdown": lambda d: f"[Markdown消息: {(d.get('content') or d.get('data') or '')[:MAX_TITLE_PREVIEW]}]",
    "node": lambda d: "[转发节点]",
}

_SEGMENT_KEY_MAP = {
    "file": "file_seg",
    "location": "location_msg",
    "share": "share_msg",
    "contact": "contact_msg",
    "music": "music_msg",
    "mface": "mface_msg",
    "rps": "rps_msg",
    "dice": "dice_msg",
    "basketball": "basketball_msg",
    "poke": "poke_msg",
    "anonymous": "anonymous_msg",
    "markdown": "markdown_msg",
    "node": "node_msg",
}


def _extract_typed_segments(segments: List[Dict]) -> Dict[str, Optional[str]]:
    result: Dict[str, Optional[str]] = {}
    for seg_type, key in _SEGMENT_KEY_MAP.items():
        formatter = _SEGMENT_FORMATTERS.get(seg_type)
        if formatter:
            result[key] = _extract_seg_text(segments, seg_type, formatter)
    return result


def _make_chat_id(data: dict, account_name: str = "") -> str:
    msg_type = data.get("message_type", "")
    if msg_type == "group":
        base = f"group_{data.get('group_id', '')}"
    else:
        base = f"private_{data.get('user_id', '')}"
    if account_name:
        return f"{account_name}:{base}"
    return base


def _parse_chat_id(chat_id: str) -> Tuple[str, str]:
    if ":" in chat_id:
        parts = chat_id.split(":", 1)
        if parts[1].startswith(("group_", "private_")):
            chat_id = parts[1]
    if chat_id.startswith("group_"):
        return ("group", chat_id[6:])
    if chat_id.startswith("private_"):
        return ("private", chat_id[8:])
    return ("private", chat_id)


def _onebot_target_key(msg_kind: str) -> str:
    return "group_id" if msg_kind == "group" else "user_id"


def _extract_account_from_chat_id(chat_id: str) -> str:
    if ":" in chat_id:
        parts = chat_id.split(":", 1)
        if parts[1].startswith(("group_", "private_")):
            return parts[0]
    return ""


def _guess_ext_from_url(url: str, default: str = ".jpg") -> str:
    try:
        path = urlparse(url).path
        ext = Path(path).suffix.lower()
        if ext and len(ext) <= 6:
            return ext
    except Exception:
        pass
    return default


def _format_message(content: str) -> str:
    if not content:
        return content
    code_blocks = []

    def _save_code_block(match):
        code_blocks.append(match.group(0))
        return f"\x00CODEBLOCK{len(code_blocks) - 1}\x00"

    processed = _CODEBLOCK_RE.sub(_save_code_block, content)
    processed = strip_markdown(processed)
    for index, block in enumerate(code_blocks):
        processed = processed.replace(f"\x00CODEBLOCK{index}\x00", block)
    processed = _EXCESSIVE_NEWLINES_RE.sub("\n\n", processed)
    return processed.strip()
