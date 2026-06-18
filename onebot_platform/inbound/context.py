from __future__ import annotations

import asyncio
from typing import Dict, List, Optional, Tuple

import onebot_platform.adapter_runtime as _runtime
globals().update({k: v for k, v in vars(_runtime).items() if not k.startswith('__')})
from onebot_platform.parsing.segments import (
    _extract_forward,
    _extract_images,
    _extract_json_card,
    _extract_segments,
    _extract_text_from_message,
    _extract_typed_segments,
    _extract_xml,
)


FORWARD_MAX_DEPTH = 3
FORWARD_MAX_FETCHES = 8
FORWARD_MAX_IMAGES = 10


def short(text: str, limit: int = MAX_QUOTE_TEXT) -> str:
    return text[:limit] + "…" if len(text) > limit else text


def take_forward_images(self, dst: List[str], src: List[str]) -> bool:
    max_images = getattr(self, "_FORWARD_MAX_IMAGES", FORWARD_MAX_IMAGES)
    if not src or len(dst) >= max_images:
        return False
    dst.extend(src[:max_images - len(dst)])
    return True


async def forward_line_parts(self, segments: List[Dict], raw_content, conn, fwd_images: List[str], depth: int, seen: set, fetch_count: List[int]) -> List[str]:
    parts = [_extract_text_from_message(raw_content)]
    if take_forward_images(self, fwd_images, _extract_images(segments)):
        parts.append("[图片]")
    parts.extend(x for x in (_extract_json_card(segments), _extract_xml(segments), *_extract_typed_segments(segments).values()) if x)
    try:
        if injected := await self._inject_file_content(segments, "", conn):
            parts.append(short(injected))
    except Exception:
        pass
    if nested_id := _extract_forward(segments):
        nested_text, nested_imgs = await resolve_forward_message(self, nested_id, conn, depth=depth + 1, _seen=seen, _fetch_count=fetch_count)
        if nested_text:
            parts.append(nested_text)
        take_forward_images(self, fwd_images, nested_imgs)
    return [p for p in parts if p]


async def resolve_forward_message(self, forward_id: str, conn, *, depth: int = 0,
                                  _seen: Optional[set] = None,
                                  _fetch_count: List[int] = None) -> Tuple[str, List[str]]:
    if _seen is None:
        _seen = set()
    if _fetch_count is None:
        _fetch_count = [0]
    if forward_id in _seen or depth > getattr(self, "_FORWARD_MAX_DEPTH", FORWARD_MAX_DEPTH):
        return "", []
    _seen.add(forward_id)
    _fetch_count[0] += 1
    if _fetch_count[0] > getattr(self, "_FORWARD_MAX_FETCHES", FORWARD_MAX_FETCHES):
        return "", []
    try:
        forward_msgs = await self.get_forward_msg(forward_id, conn=conn)
    except Exception:
        return "", []
    fwd_lines, fwd_images = [], []
    for fmsg in forward_msgs or []:
        sender = fmsg.get("sender", {})
        name = sender.get("nickname") or sender.get("card") or "未知"
        raw = fmsg.get("content") or fmsg.get("message") or ""
        parts = await forward_line_parts(self, _extract_segments(raw), raw, conn, fwd_images, depth, _seen, _fetch_count)
        if parts:
            fwd_lines.append(f"{'  ' * depth}{name}: {' '.join(parts)}")
    return ("\n[合并转发消息]\n" + "\n".join(fwd_lines) + "\n[转发结束]", fwd_images) if fwd_lines else ("", fwd_images)


def reply_segment_fallback(segments: List[Dict]) -> str:
    for seg in segments or []:
        if seg.get("type") != "reply":
            continue
        data = seg.get("data") or {}
        sender = data.get("qq") or data.get("user_id") or data.get("sender_id") or "?"
        body = next((str(data.get(k)) for k in ("text", "content", "message", "summary") if data.get(k)), "无法从NapCat取回原文")
        return f"\n[引用 {sender}: {body[:MAX_QUOTE_TEXT]}]"
    return ""


async def append_reply_context(self, display_text: str, reply_id: str, conn, source_segments: Optional[List[Dict]] = None) -> tuple:
    quoted_images: List[str] = []
    fallback = reply_segment_fallback(source_segments or []) or "\n[引用了一条消息，但无法获取内容]"
    try:
        quoted_obj = await asyncio.wait_for(self.get_msg(reply_id, conn=conn), timeout=10.0)
        if not quoted_obj:
            return (display_text or "") + fallback, quoted_images
        quoted_message = quoted_obj.get("message", "")
        quoted_segments = _extract_segments(quoted_message)
        quoted_images = _extract_images(quoted_segments)
        quoted_forward_id = _extract_forward(quoted_segments)
        quote_parts = []
        if quoted_text := _extract_text_from_message(quoted_message):
            quote_parts.append(short(quoted_text, MAX_MULTIMSG_PREVIEW))
        elif quoted_images:
            quote_parts.append("[图片]")
        quote_parts.extend(x for x in _extract_typed_segments(quoted_segments).values() if x)
        if quoted_file_text := await self._inject_file_content(quoted_segments, "", conn):
            quote_parts.append(short(quoted_file_text))
        if quoted_forward_id:
            try:
                fwd_text, fwd_imgs = await resolve_forward_message(self, quoted_forward_id, conn)
                if fwd_text:
                    quote_parts.append(fwd_text)
                if fwd_imgs:
                    quoted_images.extend(fwd_imgs)
            except Exception:
                pass
        elif quoted_json_card := _extract_json_card(quoted_segments):
            quote_parts.append(quoted_json_card)
        if quote_parts:
            name = quoted_obj.get("sender", {}).get("nickname") or quoted_obj.get("real_id", "?")
            display_text = (display_text or "") + f"\n[引用 {name}: {short(' '.join(quote_parts))}]"
        else:
            display_text = (display_text or "") + fallback
    except Exception:
        display_text = (display_text or "") + fallback
    return display_text, quoted_images
