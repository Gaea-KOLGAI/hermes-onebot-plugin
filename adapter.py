from __future__ import annotations
import asyncio
import functools
import json
import logging
import os
import re
import shutil
import socket
import tempfile
import time

# Regex to match tool progress messages from gateway.
# Format: "⚡ tool_name(...)" or "⚡ tool_name: ..." or "⚡ tool_name..."
# Some emojis (⚙️) include a variation selector U+FE0F.
_TOOL_PROGRESS_RE = re.compile(
    r'^[\u26a1\u2699\U0001f527\U0001f50d\U0001f310\U0001f4be\U0001f4dd\U0001f4cb\U0001f5d1\U0000270f\U0001f5bc\U0001f9e0\U0001f441]\ufe0f?\s+\w+[\(:.]'
)
import uuid
import random as _random
from collections import OrderedDict
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple
from dataclasses import dataclass
from urllib.parse import urlparse, unquote as url_unquote
import hmac
import ipaddress
import urllib.request
import urllib.error
logger = logging.getLogger(__name__)
from gateway.platforms.base import (
    BasePlatformAdapter,
    SendResult,
    MessageEvent,
    MessageType,
)
from gateway.config import Platform
try:
    from tools.approval import has_blocking_approval
    _HAS_APPROVAL = True
except ImportError:
    _HAS_APPROVAL = False
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
try:
    import httpx
    HTTPX_AVAILABLE = True
except ImportError:
    HTTPX_AVAILABLE = False
    httpx = None
try:
    import websockets
    WEBSOCKETS_AVAILABLE = True
except ImportError:
    WEBSOCKETS_AVAILABLE = False
    websockets = None
_WS_CONNECT_KWARGS = dict(
    ping_interval=None, ping_timeout=None,
    close_timeout=5, max_size=10 * 1024 * 1024,
)
DEDUP_WINDOW_SECONDS = 5
DEDUP_MAX_SIZE = 2000
RECONNECT_BASE_DELAY = 2
RECONNECT_MAX_DELAY = 60
HEARTBEAT_INTERVAL_EXPECTED = 30
HEARTBEAT_TIMEOUT = HEARTBEAT_INTERVAL_EXPECTED * 3
RECV_TIMEOUT = 45
RATE_LIMIT_MESSAGES_PER_SECOND = 5
RATE_LIMIT_BURST = 10
REJECT_NOTIFY_TTL = 86400
ECHO_STALE_TIMEOUT = 30
MAX_MULTIMSG_PREVIEW = 200
MAX_TITLE_PREVIEW = 80
MAX_QUOTE_TEXT = 300
IMAGE_DOWNLOAD_CONCURRENCY = 5
PENDING_UPDATE_TTL = 300
HEARTBEAT_CHECK_INTERVAL = 15
SEQ_CLEANUP_INTERVAL = 60
FILE_INJECTION_MAX_BYTES = 65536
SSRF_BLOCKED_NETWORKS = [
    ipaddress.ip_network("127.0.0.0/8"),
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("169.254.0.0/16"),
    ipaddress.ip_network("0.0.0.0/8"),
    ipaddress.ip_network("::1/128"),
    ipaddress.ip_network("fc00::/7"),
    ipaddress.ip_network("fe80::/10"),
    ipaddress.ip_network("::ffff:0:0/96"),
]
MAX_TASKS_PER_CHAT = 5
_MSG_TYPE_MAP = {"images": MessageType.PHOTO, "voice_url": MessageType.VOICE, "video_url": MessageType.VIDEO}
_TEXT_EXTS = frozenset({".txt", ".md", ".csv", ".json", ".xml", ".yaml", ".yml",
              ".log", ".py", ".js", ".ts", ".html", ".css", ".ini", ".cfg",
              ".toml", ".sh", ".bat", ".sql", ".env"})
def _hermes_onebot_data_dir() -> Path:
    try:
        from hermes_constants import get_hermes_home
        base = get_hermes_home()
    except Exception:
        base = Path.home() / ".hermes"
    path = base / "plugins" / "onebot-platform"
    path.mkdir(parents=True, exist_ok=True)
    return path

def _truthy(value: Any, default: bool = False) -> bool:
    if value is None or value == "":
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in ("1", "true", "yes", "on", "y")

def _csv_list(raw: Any) -> List[str]:
    if raw is None:
        return []
    if isinstance(raw, (list, tuple, set)):
        return [str(v).strip() for v in raw if str(v).strip()]
    return [s.strip() for s in str(raw).split(",") if s.strip()]

DATA_DIR = _hermes_onebot_data_dir()
MEDIA_CACHE_DIR = DATA_DIR / "media_cache"
def _safe_int(val, label: str = "") -> int:
    try:
        return int(val)
    except (ValueError, TypeError):
        raise ValueError(f"Invalid {label or 'value'}: {val!r}")
def _safe_target_id(target_id) -> "int | SendResult":
    try:
        return _safe_int(target_id, "target_id")
    except ValueError as e:
        return SendResult(success=False, error=str(e))
def _strip_slash(text: str) -> str:
    return text[1:] if text.startswith("/") else text
_CQ_STRIP_RE = re.compile(r'\[CQ:[^\]]*\]')
# NOTE: CQ code parameters cannot contain literal commas in values.
# This is a known OneBot protocol limitation - commas are used as delimiters.
_CQ_SEGMENT_RE = re.compile(r'\[CQ:(\w+)((?:,[^,\]]+=[^,\]]*)*)\]')
def _extract_text_from_message(message: Any) -> str:
    if isinstance(message, str):
        return _segments_text(_extract_segments(message))
    if isinstance(message, list):
        return _segments_text(message)
    return ""

def _segments_text(segments: List[Dict[str, Any]]) -> str:
    return "".join(
        _cq_unescape(str((seg.get("data") or {}).get("text", "")))
        for seg in segments
        if isinstance(seg, dict) and seg.get("type") == "text"
    ).strip()
def _cq_unescape(s: str) -> str:
    return s.replace("&#91;", "[").replace("&#93;", "]").replace("&#44;", ",").replace("&#10;", "\n").replace("&#13;", "\r").replace("&amp;", "&")
def _extract_segments(message: Any) -> List[Dict[str, Any]]:
    if isinstance(message, str):
        segments = []
        last_end = 0
        for m in _CQ_SEGMENT_RE.finditer(message):
            if m.start() > last_end:
                text = message[last_end:m.start()].strip()
                if text:
                    segments.append({"type": "text", "data": {"text": _cq_unescape(text)}})
            seg_type = m.group(1)
            params = {}
            if m.group(2):
                for kv in m.group(2).lstrip(",").split(","):
                    if "=" in kv:
                        k, v = kv.split("=", 1)
                        params[k] = _cq_unescape(v)
            segments.append({"type": seg_type, "data": params})
            last_end = m.end()
        if last_end < len(message):
            text = message[last_end:].strip()
            if text:
                segments.append({"type": "text", "data": {"text": _cq_unescape(text)}})
        return segments
    elif isinstance(message, list):
        return [s for s in message if isinstance(s, dict)]
    return []
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
    return [str((seg.get("data") or {}).get("qq", ""))
            for seg in segments if seg.get("type") == "at"
            and (seg.get("data") or {}).get("qq")]
def _extract_forward(segments: List[Dict]) -> Optional[str]:
    for seg in segments:
        seg_type = seg.get("type", "")
        if seg_type in ("forward", "forward_msg", "nodes"):
            data = (seg.get("data") or {})
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
    texts = [item["text"].strip().replace("[图片]", "").strip()
             for item in news_items if isinstance(item, dict) and isinstance(item.get("text"), str)]
    texts = [t for t in texts if t]
    return "\n".join(texts).strip() or None
def _extract_json_card(segments: List[Dict]) -> Optional[str]:
    for seg in segments:
        if seg.get("type") == "json":
            raw = (seg.get("data") or {}).get("data", "")
            if not raw:
                return "[卡片消息]"
            if isinstance(raw, str) and "&#44;" in raw:
                raw = raw.replace("&#44;", ",")
            try:
                obj = json.loads(raw)
                multimsg_text = _extract_multimsg_text(obj)
                if multimsg_text:
                    if len(multimsg_text) > MAX_MULTIMSG_PREVIEW:
                        multimsg_text = multimsg_text[:MAX_MULTIMSG_PREVIEW] + "…"
                    return f"[合并转发预览]\n{multimsg_text}"
                title = (
                    obj.get("meta", {}).get("news", {}).get("title")
                    or obj.get("meta", {}).get("detail_1", {}).get("desc")
                    or obj.get("prompt", "")
                    or obj.get("title", "")
                    or obj.get("desc", "")
                )
                if title:
                    if len(title) > MAX_TITLE_PREVIEW:
                        title = title[:MAX_TITLE_PREVIEW] + "…"
                    return f"[卡片消息: {title}]"
            except (json.JSONDecodeError, AttributeError):
                pass
            return "[卡片消息]"
    return None
_XML_TITLE_RE = re.compile(r'<title[^>]*>(.*?)</title>', re.IGNORECASE)
_XML_BRIEF_RE = re.compile(r'action="[^"]*"[^>]*brief="([^"]*)"', re.IGNORECASE)
def _extract_xml(segments: List[Dict]) -> Optional[str]:
    for seg in segments:
        if seg.get("type") != "xml":
            continue
        raw = (seg.get("data") or {}).get("data", "")
        if not raw:
            return "[XML消息]"
        for pat in (_XML_TITLE_RE, _XML_BRIEF_RE):
            m = pat.search(raw)
            if m:
                val = m.group(1).strip()[:MAX_TITLE_PREVIEW]
                return f"[XML消息: {val}]"
        return "[XML消息]"
    return None
def _fmt_rps(d):
    rid = str(d.get("id", d.get("result", "")))
    rps_map = {"0": "石头", "1": "剪刀", "2": "布"}
    return f"[猜拳: {rps_map.get(rid, rid)}]"
_SEGMENT_FORMATTERS: Dict[str, Callable] = {
    "file": lambda d: (
        f"[文件: {d.get('name') or d.get('file') or '未知文件'} {(u := d.get('url') or d.get('file_url') or '')}]"
        if (u := d.get("url") or d.get("file_url") or "").startswith("http")
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
    "mface": lambda d: (
        f"[商城表情: {n}]" if (n := d.get("name") or d.get("face_id") or d.get("emoji_id") or "")
        else "[商城表情]"
    ),
    "rps": _fmt_rps,
    "dice": lambda d: f"[骰子: {d.get('id', d.get('result', ''))}]",
}
_SEGMENT_KEY_MAP = {
    "file": "file_seg", "location": "location_msg", "share": "share_msg",
    "contact": "contact_msg", "music": "music_msg", "mface": "mface_msg",
    "rps": "rps_msg", "dice": "dice_msg",
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
    elif chat_id.startswith("private_"):
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
_CODEBLOCK_RE = re.compile(r'```[\s\S]*?```')
_EXCESSIVE_NEWLINES_RE = re.compile(r'\n{3,}')

def _sanitize_log(s: str, max_len: int = 100) -> str:
    """Strip ANSI escape codes, control characters, and Unicode bidi overrides from log strings."""
    if not s:
        return ""
    s = str(s)
    s = re.sub(r'\x1b\[[0-9;]*[a-zA-Z]', '', s)
    s = re.sub(r'[\x00-\x08\x0b\x0c\x0e-\x1f\x7f\u200b-\u200f\u202a-\u202e\u2060-\u206f\ufeff]', '', s)
    return s.replace("\n", "\\n").replace("\r", "\\r")[:max_len]

def _format_message(content: str) -> str:
    if not content:
        return content
    code_blocks = []
    def _save_code_block(m):
        code_blocks.append(m.group(0))
        return f"\x00CODEBLOCK{len(code_blocks) - 1}\x00"
    processed = _CODEBLOCK_RE.sub(_save_code_block, content)
    processed = strip_markdown(processed)
    for i, block in enumerate(code_blocks):
        processed = processed.replace(f"\x00CODEBLOCK{i}\x00", block)
    processed = _EXCESSIVE_NEWLINES_RE.sub('\n\n', processed)
    return processed.strip()

_BLOCKED_PATH_PREFIXES = ("/etc/", "/proc/", "/sys/", "/dev/", "/root/", "/home/")

def _is_path_safe(resolved: str) -> bool:
    return not resolved.startswith(_BLOCKED_PATH_PREFIXES)

class DedupCache:
    def __init__(self, ttl: float = DEDUP_WINDOW_SECONDS, max_size: int = DEDUP_MAX_SIZE):
        self._ttl = ttl
        self._max_size = max_size
        self._cache: OrderedDict = OrderedDict()
    def is_duplicate(self, dedup_key: str) -> bool:
        now = time.time()
        if dedup_key in self._cache:
            _, ts = self._cache[dedup_key]
            if now - ts <= self._ttl:
                return True
            self._cache.pop(dedup_key, None)
        if len(self._cache) > self._max_size // 2:
            expired = [k for k, (_, ts) in self._cache.items() if now - ts > self._ttl]
            for k in expired:
                self._cache.pop(k, None)
        while len(self._cache) >= self._max_size:
            self._cache.popitem(last=False)
        self._cache[dedup_key] = (True, now)
        return False
class RateLimiter:
    def __init__(self, rate: float = RATE_LIMIT_MESSAGES_PER_SECOND, burst: int = RATE_LIMIT_BURST):
        self._rate = rate
        self._burst = float(burst)
        self._tokens = float(burst)
        self._last = time.monotonic()
        self._lock = asyncio.Lock()
    async def acquire(self):
        async with self._lock:
            now = time.monotonic()
            elapsed = now - self._last
            self._tokens = min(self._burst, self._tokens + elapsed * self._rate)
            self._last = now
            if self._tokens < 1:
                wait = (1 - self._tokens) / self._rate
                await asyncio.sleep(wait)
                self._tokens = 0
                self._last = time.monotonic()
            else:
                self._tokens -= 1
class MemberCache:
    def __init__(self, ttl: float = 300):
        self._ttl = ttl
        self._max_size = 5000
        self._cache: OrderedDict[str, Dict[str, Any]] = OrderedDict()
    def get(self, group_id: str, user_id: str) -> Optional[Dict]:
        key = f"{group_id}_{user_id}"
        entry = self._cache.get(key)
        if entry and time.time() - entry["_ts"] < self._ttl:
            self._cache.move_to_end(key)
            return entry
        return None
    def set(self, group_id: str, user_id: str, info: Dict):
        key = f"{group_id}_{user_id}"
        entry = dict(info)
        entry["_ts"] = time.time()
        if key in self._cache:
            self._cache.move_to_end(key)
        self._cache[key] = entry
        while len(self._cache) > self._max_size:
            self._cache.popitem(last=False)
    def set_from_sender(self, group_id: str, user_id: str, sender: Dict):
        self.set(group_id, user_id, {
            "nickname": sender.get("nickname", ""),
            "card": sender.get("card", ""),
            "role": sender.get("role", ""),
            "title": sender.get("title", ""),
        })
class _MediaCache:
    _EXT_MAP = [
        ("png", ".png"),
        ("gif", ".gif"),
        ("webp", ".webp"),
        ("jpeg", ".jpg"),
        ("jpg", ".jpg"),
    ]
    def __init__(self, cache_dir: Path, max_files: int = 500, max_file_size: int = 20 * 1024 * 1024):
        self._dir = cache_dir
        self._max_files = max_files
        self._max_size = max_file_size
        cache_dir.mkdir(parents=True, exist_ok=True)
    def get_path(self, filename: str) -> Path:
        candidate = (self._dir / filename).resolve()
        base = self._dir.resolve()
        if not (str(candidate) == str(base) or str(candidate).startswith(str(base) + os.sep)):
            raise ValueError(f"Path traversal blocked: {filename}")
        return candidate
    def _validate_local_path(self, url: str) -> Optional[str]:
        cache_dir = str(self._dir.resolve())
        if url.startswith("file://"):
            local_path = url_unquote(url[7:])
            resolved = str(Path(local_path).resolve())
            if not (resolved == cache_dir or resolved.startswith(cache_dir + os.sep)):
                return None
            return resolved if Path(resolved).is_file() else None
        if url.startswith("/"):
            resolved = str(Path(url).resolve())
            if (resolved == cache_dir or resolved.startswith(cache_dir + os.sep)) and os.path.isfile(resolved):
                return resolved
        return None
    async def download(self, url: str, http_client, media_type: str = "image") -> Optional[str]:
        if not url or not HTTPX_AVAILABLE:
            return None
        if url.startswith("file://") or url.startswith("/"):
            return self._validate_local_path(url)
        if not url.startswith("http://") and not url.startswith("https://"):
            return None
        own_client = False
        client = None
        tmp_path: Optional[Path] = None
        try:
            client = http_client
            own_client = not bool(http_client)
            if not client:
                parsed_url = urlparse(url)
                hostname = parsed_url.hostname
                if hostname:
                    try:
                        # Use getaddrinfo to get all addresses (IPv4+IPv6)
                        addrinfo = socket.getaddrinfo(hostname, None, socket.AF_UNSPEC, socket.SOCK_STREAM)
                        for family, socktype, proto, canonname, sockaddr in addrinfo:
                            ip_str = sockaddr[0]
                            ip = ipaddress.ip_address(ip_str)
                            for net in SSRF_BLOCKED_NETWORKS:
                                if ip in net:
                                    logger.warning("SSRF blocked: %s resolves to private IP %s", url, ip)
                                    return None
                    except (socket.gaierror, ValueError) as e:
                        logger.warning("SSRF check failed for %s: %s", url, e)
                        return None
                client = httpx.AsyncClient(timeout=30.0, follow_redirects=False)
            ext = _guess_ext_from_url(url)
            cache_subdir = self._dir / media_type
            cache_subdir.mkdir(parents=True, exist_ok=True)
            filename = f"{media_type}_{uuid.uuid4().hex[:12]}{ext}"
            filepath = cache_subdir / filename
            async with client.stream(
                "GET", url,
                headers={
                    "User-Agent": "Mozilla/5.0 (compatible; HermesAgent/1.0)",
                    "Accept": "*/*",
                },
            ) as resp:
                if resp.status_code != 200:
                    resp.raise_for_status()
                total = 0
                content_type = resp.headers.get("content-type", "")
                for substr, ct_ext in self._EXT_MAP:
                    if substr in content_type:
                        ext = ct_ext
                        filepath = filepath.with_suffix(ext)
                        break
                with tempfile.NamedTemporaryFile(prefix=f".{media_type}_", suffix=ext, dir=cache_subdir, delete=False) as tmp:
                    tmp_path = Path(tmp.name)
                    async for chunk in resp.aiter_bytes():
                        total += len(chunk)
                        if total > self._max_size:
                            tmp.close()
                            tmp_path.unlink(missing_ok=True)
                            return None
                        tmp.write(chunk)
            if tmp_path is None:
                return None
            tmp_path.replace(filepath)
            await asyncio.to_thread(self._cleanup_subdir, cache_subdir)
            return str(filepath)
        except Exception as e:
            if tmp_path is not None:
                tmp_path.unlink(missing_ok=True)
            return None
        finally:
            if own_client and client is not None:
                await client.aclose()
    def _cleanup_subdir(self, cache_subdir: Path):
        try:
            all_files = sorted(
                (cache_subdir / f for f in os.listdir(cache_subdir) if os.path.isfile(cache_subdir / f)),
                key=lambda p: os.path.getmtime(p),
            )
            if len(all_files) > self._max_files:
                for old_file in all_files[:100]:
                    old_file.unlink(missing_ok=True)
        except OSError:
            pass
class _NapCatConnection:
    def __init__(self, name: str, ws_url: str, access_token: str = "",
                 ws_mode: str = "forward", allowed_users: List[str] = None,
                 group_ids: List[str] = None,
                 home_channel: str = "",
                 allow_all: bool = False, admin_qq: str = "",
                 http_api_url: str = ""):
        self.name = name
        self.ws_url = ws_url
        self.access_token = access_token
        self.ws_mode = ws_mode
        self.allowed_users: List[str] = list(allowed_users or [])
        self.group_ids: List[str] = list(group_ids or [])
        self.home_channel = home_channel
        self.allow_all = allow_all
        self.admin_qq = admin_qq or ""
        self.http_api_url = http_api_url
        self.ws = None
        self.ws_server = None
        self.recv_task: Optional[asyncio.Task] = None
        self.heartbeat_task: Optional[asyncio.Task] = None
        self.reconnect_task: Optional[asyncio.Task] = None
        self.echo_futures: Dict[str, asyncio.Future] = {}
        self._echo_timestamps: Dict[str, float] = {}
        self.self_id: Optional[str] = None
        self.self_nickname: Optional[str] = None
        self.last_heartbeat: float = 0
        self.connected_since: float = 0
        self.reverse_ws_clients: set = set()
        self.dedup = DedupCache()
        self.rate_limiter = RateLimiter()
        self.member_cache = MemberCache()
        self._warnings: set = set()
    @property
    def is_connected(self) -> bool:
        if self.ws_mode == "reverse":
            return bool(self.reverse_ws_clients)
        return bool(self.ws and self.ws.close_code is None)
    def add_allowed_user(self, qq_number: str) -> bool:
        qq_number = str(qq_number).strip()
        if not qq_number.isdigit():
            return False
        if not (5 <= len(qq_number) <= 15):
            return False
        if qq_number not in self.allowed_users:
            self.allowed_users.append(qq_number)
            return True
        return False
    def remove_allowed_user(self, qq_number: str) -> bool:
        qq_number = str(qq_number).strip()
        if qq_number in self.allowed_users:
            self.allowed_users.remove(qq_number)
            return True
        return False
    def list_allowed_users(self) -> List[str]:
        return list(self.allowed_users)
    def is_user_authorized(self, user_id: str, msg_type: str, data: dict) -> bool:
        if self.allow_all:
            return True
        if msg_type == "private":
            if self.allowed_users:
                return user_id in self.allowed_users
            if "empty_allowlist" not in self._warnings:
                logger.warning("OneBot: empty allowlist — denying all users (set allow_all=True to allow everyone)")
                self._warnings.add("empty_allowlist")
            return False
        if msg_type == "group":
            group_id = str(data.get("group_id", ""))
            if self.group_ids and group_id not in self.group_ids:
                return False
            if self.allowed_users:
                return user_id in self.allowed_users
            if "empty_group_allowlist" not in self._warnings:
                logger.warning("OneBot: empty group allowlist — denying all group users")
                self._warnings.add("empty_group_allowlist")
            return False
        return False
    def is_group_wake_triggered(self, raw_message: Any, text: str, segments: List[Dict]) -> bool:
        return bool(self.self_id and self.self_id in _extract_at(segments))
class _PluginSettings:
    def __init__(self, path: Path):
        self._path = path
        self._bak_path = path.with_suffix(".json.bak")
        self._data: dict = {}
        self._lock = asyncio.Lock()
    def load(self) -> dict:
        try:
            if self._path.exists():
                with open(self._path, 'r', encoding='utf-8') as f:
                    self._data = json.load(f)
        except Exception as e:
            self._data = {}
        return self._data
    async def save(self):
        async with self._lock:
            try:
                if self._path.exists():
                    shutil.copy2(str(self._path), str(self._bak_path))
                data = json.dumps(self._data, ensure_ascii=False, indent=2)
                await asyncio.to_thread(self._path.write_text, data, encoding='utf-8')
            except Exception as e:
                logger.warning("Failed to save plugin settings: %s", e)
    @property
    def data(self) -> dict:
        return self._data
    def _normalize_key(self, chat_id: str) -> str:
        """Strip account prefix (e.g. 'main:group_123' -> 'group_123')."""
        if ":" in chat_id:
            _, _, suffix = chat_id.partition(":")
            if suffix.startswith(("group_", "private_")):
                return suffix
        return chat_id
    def get_chat(self, chat_id: str) -> dict:
        key = self._normalize_key(chat_id)
        return self._data.get(key, {})
    def ensure_chat(self, chat_id: str) -> dict:
        key = self._normalize_key(chat_id)
        if key not in self._data:
            self._data[key] = {}
        return self._data[key]
    def get_global(self) -> dict:
        if "_global" not in self._data:
            self._data["_global"] = {}
        return self._data["_global"]
@dataclass
class _CmdDef:
    name: str
    handler: Callable
    admin_only: bool = True
    group_only: bool = False
class SettingsMixin:
    async def _ensure_settings_loaded(self):
        if self._settings_loaded:
            return
        async with self._settings_lock:
            if self._settings_loaded:
                return
            self._plugin_settings = _PluginSettings(self._settings_path)
            self._plugin_settings.load()
            self._settings_loaded = True
            self._apply_persisted_settings()
    async def _save_settings(self):
        await self._plugin_settings.save()
    def _restore_account_list(self, conn: _NapCatConnection, val, attr: str) -> None:
        if attr == "allowed_users" and isinstance(val, list):
            conn.allowed_users = list(set(conn.allowed_users + [str(u) for u in val]))
        elif attr == "allow_all":
            conn.allow_all = bool(val)
    def _apply_persisted_settings(self):
        try:
            if not self._plugin_settings.data:
                return
            gs = self._plugin_settings.data.get("_global", {})
            group_ids_by_account = gs.get("group_ids_by_account")
            if isinstance(group_ids_by_account, dict) and group_ids_by_account:
                for name, gids in group_ids_by_account.items():
                    conn = self._connections.get(name)
                    if conn and isinstance(gids, list):
                        conn.group_ids = [str(g) for g in gids]
            elif gs.get("group_ids") is not None:
                group_list = [str(g) for g in gs["group_ids"]]
                for conn in self._connections.values():
                    conn.group_ids = list(group_list)
            for key, attr in [("allowed_users_by_account", "allowed_users"),
                              ("allow_all_by_account", "allow_all")]:
                acct_dict = gs.get(key, {})
                if not isinstance(acct_dict, dict):
                    continue
                for name, val in acct_dict.items():
                    conn = self._connections.get(name)
                    if conn is not None:
                        self._restore_account_list(conn, val, attr)
        except Exception as e:
            logger.debug("Failed to apply persisted settings: %s", e)
    async def _persist_account_setting(self, conn: _NapCatConnection, key: str, value_list):
        gs = self._get_global_settings()
        gs.setdefault(key, {})[conn.name] = list(value_list)
        await self._save_settings()
    async def _persist_allowed_users(self, conn: _NapCatConnection):
        await self._persist_account_setting(conn, "allowed_users_by_account", conn.allowed_users)
    def _get_chat_settings(self, chat_id: str) -> dict:
        return self._plugin_settings.ensure_chat(chat_id)
    def _get_global_settings(self) -> dict:
        return self._plugin_settings.get_global()
    def _get_conn_for_chat(self, chat_id: str) -> _NapCatConnection:
        if not self._multi_account:
            return self._default_conn
        account = _extract_account_from_chat_id(chat_id)
        if account and account in self._connections:
            return self._connections[account]
        return self._default_conn
class ConnectionMixin:
    def _set_fatal_if_default(self, conn, error_type: str, msg: str, retryable: bool = False):
        if conn is self._default_conn:
            self._set_fatal_error(error_type, msg, retryable=retryable)
    async def connect(self) -> bool:
        await self._ensure_settings_loaded()
        self._shutting_down = False
        if len(self._connections) == 1 and not self._multi_account:
            conn = self._default_conn
            if conn.ws_mode == "reverse":
                return await self._connect_reverse_conn(conn)
            return await self._connect_forward_conn(conn)
        any_connected = False
        for name, conn in self._connections.items():
            if conn.ws_mode == "reverse":
                ok = await self._connect_reverse_conn(conn)
            else:
                ok = await self._connect_forward_conn(conn)
            if ok:
                any_connected = True
        if any_connected:
            self._mark_connected()
        return any_connected
    def _check_ws_prereqs(self, conn: _NapCatConnection) -> bool:
        if not conn.ws_url:
            self._set_fatal_if_default(conn, "config_missing", "ONEBOT_WS_URL must be set", retryable=False)
            return False
        if not WEBSOCKETS_AVAILABLE:
            self._set_fatal_if_default(conn, "missing_dependency", "pip install websockets", retryable=False)
            return False
        return True
    async def _connect_forward_conn(self, conn: _NapCatConnection) -> bool:
        if conn.ws and conn.ws.close_code is None:
            return True
        if not self._check_ws_prereqs(conn):
            return False
        headers = {"Authorization": f"Bearer {conn.access_token}"} if conn.access_token else None
        try:
            conn.ws = await asyncio.wait_for(
                websockets.connect(conn.ws_url, additional_headers=headers, **_WS_CONNECT_KWARGS),
                timeout=15.0,
            )
        except Exception as e:
            self._set_fatal_if_default(conn, "connect_failed", str(e), retryable=True)
            return False
        conn.connected_since = conn.last_heartbeat = time.time()
        conn.recv_task = asyncio.create_task(self._receive_loop_conn(conn))
        conn.heartbeat_task = asyncio.create_task(self._heartbeat_monitor_conn(conn))
        self._mark_connected()
        asyncio.create_task(self._fetch_self_info_conn(conn))
        return True
    async def _connect_reverse_conn(self, conn: _NapCatConnection) -> bool:
        if not self._check_ws_prereqs(conn):
            return False
        parsed = urlparse(conn.ws_url)
        host = parsed.hostname or "0.0.0.0"
        port = parsed.port or 8082
        async def handler(websocket, path=None):
            await self._handle_reverse_ws_client(conn, websocket)
        try:
            conn.ws_server = await websockets.serve(
                handler, host, port,
                ping_interval=30,
                ping_timeout=10,
                max_size=10 * 1024 * 1024,
            )
            return True
        except Exception as e:
            self._set_fatal_if_default(conn, "connect_failed", str(e), retryable=True)
            return False
    async def _handle_reverse_ws_client(self, conn: _NapCatConnection, websocket) -> None:
        if conn.access_token:
            auth_ok = False
            try:
                headers = getattr(websocket.request, 'headers', None)
                if headers:
                    auth = headers.get("Authorization", "")
                    auth_ok = hmac.compare_digest(auth, f"Bearer {conn.access_token}")
            except Exception:
                pass
            if not auth_ok:
                try:
                    headers = getattr(websocket, 'request_headers', {})
                    auth = headers.get("Authorization", "")
                    auth_ok = hmac.compare_digest(auth, f"Bearer {conn.access_token}")
                except Exception:
                    pass
            if not auth_ok:
                try:
                    await websocket.close(4001, "Unauthorized")
                except Exception:
                    pass
                return
        else:
            if "reverse_ws_no_token" not in conn._warnings:
                logger.info("OneBot reverse WebSocket has no access token; accepting local NapCat connections")
                conn._warnings.add("reverse_ws_no_token")
        conn.ws = websocket
        conn.reverse_ws_clients.add(websocket)
        conn.connected_since = time.time()
        conn.last_heartbeat = time.time()
        self._mark_connected()
        try:
            await self._cancel_conn_tasks(conn, "recv_task", "heartbeat_task")
            conn.recv_task = asyncio.create_task(self._receive_loop_conn(conn))
            conn.heartbeat_task = asyncio.create_task(self._heartbeat_monitor_conn(conn))
            asyncio.create_task(self._fetch_self_info_conn(conn))
            await websocket.wait_closed()
        finally:
            conn.reverse_ws_clients.discard(websocket)
            if conn.ws is websocket:
                if conn.reverse_ws_clients:
                    conn.ws = next(iter(conn.reverse_ws_clients))
                    if conn.recv_task and not conn.recv_task.done():
                        conn.recv_task.cancel()
                        try:
                            await conn.recv_task
                        except asyncio.CancelledError:
                            pass
                    if conn.heartbeat_task and not conn.heartbeat_task.done():
                        conn.heartbeat_task.cancel()
                        try:
                            await conn.heartbeat_task
                        except asyncio.CancelledError:
                            pass
                    conn.recv_task = asyncio.create_task(self._receive_loop_conn(conn))
                    conn.heartbeat_task = asyncio.create_task(self._heartbeat_monitor_conn(conn))
                else:
                    conn.ws = None
            if not conn.reverse_ws_clients and conn is self._default_conn:
                self._mark_disconnected()
    async def disconnect(self) -> None:
        self._shutting_down = True
        self._running = False
        for name, conn in self._connections.items():
            await self._disconnect_conn(conn)
        if self._http_client:
            try:
                await self._http_client.aclose()
            except Exception:
                pass
            self._http_client = None
        self._mark_disconnected()
    async def _cancel_conn_tasks(self, conn: _NapCatConnection, *task_attrs: str, clear: bool = False):
        for attr in task_attrs:
            task = getattr(conn, attr, None)
            if task and not task.done():
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
                except Exception as e:
                    pass
            if clear:
                setattr(conn, attr, None)
    async def _disconnect_conn(self, conn: _NapCatConnection) -> None:
        await self._cancel_conn_tasks(conn, "recv_task", "heartbeat_task", "reconnect_task", clear=True)
        if conn.ws:
            try:
                await asyncio.wait_for(conn.ws.close(), timeout=5.0)
            except Exception:
                pass
            conn.ws = None
        if conn.ws_server:
            try:
                conn.ws_server.close()
                await asyncio.wait_for(conn.ws_server.wait_closed(), timeout=5.0)
            except Exception:
                pass
            conn.ws_server = None
        for fut in conn.echo_futures.values():
            if not fut.done():
                fut.set_exception(asyncio.TimeoutError("echo stale"))
        conn.echo_futures.clear()
        conn._echo_timestamps.clear()
        conn.reverse_ws_clients.clear()
        prefix = f"{conn.name}:" if self._multi_account else ""
        conn_names = tuple(f"{n}:" for n in self._connections.keys())
        def _is_ours(key):
            if prefix:
                return key.startswith(prefix)
            return not (conn_names and key.startswith(conn_names))
        for chat_id in [k for k in self._active_tasks if _is_ours(k)]:
            task = self._active_tasks.pop(chat_id, None)
            if task and not task.done():
                task.cancel()
    async def _fetch_self_info_conn(self, conn: _NapCatConnection):
        try:
            result = await self._send_action_conn(conn, "get_login_info", {})
            if result.get("retcode") == 0:
                data = result.get("data", {})
                conn.self_id = str(data.get("user_id", ""))
                conn.self_nickname = data.get("nickname", "")
        except Exception as e:
            logger.debug("Failed to fetch self info: %s", e)
    async def _force_close_ws(self, conn: _NapCatConnection) -> None:
        ws = conn.ws
        if ws is None:
            return
        try:
            await asyncio.wait_for(ws.close(), timeout=3.0)
            return
        except Exception:
            pass
        try:
            if hasattr(ws, 'transport') and ws.transport:
                ws.transport.close()
        except Exception:
            pass
    async def _heartbeat_monitor_conn(self, conn: _NapCatConnection) -> None:
        while self._running:
            await asyncio.sleep(15)
            if not self._running:
                return
            ws = conn.ws
            if ws is None or ws.close_code is not None:
                break
            elapsed = time.time() - conn.last_heartbeat
            if elapsed > HEARTBEAT_TIMEOUT:
                if conn.recv_task and not conn.recv_task.done():
                    conn.recv_task.cancel()
                await self._force_close_ws(conn)
                if conn.ws_mode == "forward":
                    if not conn.reconnect_task or conn.reconnect_task.done():
                        conn.reconnect_task = asyncio.create_task(self._reconnect_loop_conn(conn))
                break
    async def _reconnect_loop_conn(self, conn: _NapCatConnection) -> None:
        attempt = 0
        while self._running:
            delay = min(RECONNECT_BASE_DELAY * (2 ** attempt), RECONNECT_MAX_DELAY)
            jitter = delay * 0.2 * (_random.random() - 0.5)
            wait = max(1.0, delay + jitter)
            await asyncio.sleep(wait)
            if not self._running:
                return
            await self._cancel_conn_tasks(conn, "recv_task", "heartbeat_task", clear=True)
            if conn.ws:
                try:
                    await asyncio.wait_for(conn.ws.close(), timeout=3.0)
                except Exception:
                    pass
                conn.ws = None
            try:
                ok = await self._connect_forward_conn(conn)
                if ok:
                    return
            except Exception as e:
                pass
            attempt += 1
    def _dispatch_for_chat(self, chat_id: str, coro, *, notice: bool = False) -> None:
        key = (chat_id + ":notice") if notice else chat_id
        # Limit concurrent tasks per chat to prevent unbounded growth
        chat_tasks = [(k, t) for k, t in self._active_tasks.items()
                      if k.startswith(chat_id) and not t.done()]
        while len(chat_tasks) >= MAX_TASKS_PER_CHAT:
            # Cancel the oldest task (dict preserves insertion order)
            oldest_key, oldest_task = chat_tasks.pop(0)
            self._active_tasks.pop(oldest_key, None)
            oldest_task.cancel()
        async def _wrapper():
            try:
                await coro
            except asyncio.CancelledError:
                pass
            except Exception as e:
                logger.debug("Task exception in %s: %s", key, e)
            finally:
                if self._active_tasks.get(key) is asyncio.current_task():
                    self._active_tasks.pop(key, None)
        task = asyncio.create_task(_wrapper())
        self._active_tasks[key] = task
    async def _receive_loop_conn(self, conn: _NapCatConnection) -> None:
        while self._running:
            ws = conn.ws
            if ws is None or ws.close_code is not None:
                break
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=RECV_TIMEOUT)
            except asyncio.TimeoutError:
                now = time.time()
                stale_keys = [k for k, ts in conn._echo_timestamps.items() if now - ts > ECHO_STALE_TIMEOUT]
                for k in stale_keys:
                    fut = conn.echo_futures.pop(k, None)
                    conn._echo_timestamps.pop(k, None)
                    if fut and not fut.done():
                        fut.set_exception(asyncio.TimeoutError("echo stale"))
                continue
            except websockets.ConnectionClosed as e:
                break
            except asyncio.CancelledError:
                return
            except Exception as e:
                if self._running:
                    await asyncio.sleep(1)
                continue
            try:
                data = json.loads(raw)
            except (json.JSONDecodeError, TypeError):
                continue
            try:
                await self._process_event_conn(conn, data)
            except asyncio.CancelledError:
                return
            except Exception as e:
                logger.debug("Event processing error: %s", e)
        if self._running and conn.ws_mode == "forward":
            if not conn.reconnect_task or conn.reconnect_task.done():
                if not getattr(self, '_shutting_down', False):
                    self._running = True
                conn.reconnect_task = asyncio.create_task(self._reconnect_loop_conn(conn))
    async def _process_event_conn(self, conn: _NapCatConnection, data: dict) -> None:
        if "self_id" in data and not conn.self_id:
            conn.self_id = str(data["self_id"])
        post_type = data.get("post_type", "")
        echo = data.get("echo")
        if echo and echo in conn.echo_futures:
            fut = conn.echo_futures.pop(echo)
            conn._echo_timestamps.pop(echo, None)
            if not fut.done():
                fut.set_result(data)
            return
        if post_type == "meta_event":
            sub = data.get("meta_event_type", "")
            if sub == "heartbeat":
                conn.last_heartbeat = time.time()
            elif sub == "lifecycle":
                sub_type = data.get("sub_type", "")
                if sub_type == "connect":
                    asyncio.create_task(self._fetch_self_info_conn(conn))
            return
        if post_type == "message":
            account_name = conn.name if self._multi_account else ""
            chat_id = _make_chat_id(data, account_name)
            self._dispatch_for_chat(chat_id, self._handle_message(data, conn=conn))
            return
        if post_type == "notice":
            self._dispatch_for_chat(f"notice:{data.get('notice_type', '')}", self._handle_notice(data, conn), notice=True)
            return
class MessageMixin:
    async def _handle_message(self, data: dict, conn: Optional[_NapCatConnection] = None):
        if conn is None:
            conn = self._default_conn
        account_name = conn.name if self._multi_account else ""
        msg_type = data.get("message_type", "")
        user_id = str(data.get("user_id", ""))
        raw_message = data.get("message", "")
        message_id = str(data.get("message_id", ""))
        if self._check_duplicate_and_self(data, conn):
            return
        early_chat_id = _make_chat_id(data, account_name)
        self._chat_msg_seq[early_chat_id] = self._chat_msg_seq.get(early_chat_id, 0) + 1
        segments = _extract_segments(raw_message)
        text_for_cmd = _segments_text(segments)
        is_slash_cmd = text_for_cmd.startswith("/")
        if not await self._check_authorization_async(user_id, msg_type, data, conn):
            return
        admin_qq = (getattr(conn, 'admin_qq', None)
                    or os.getenv("ONEBOT_ADMIN_QQ", "").strip()
                    or (conn.allowed_users[0] if conn.allowed_users else None))
        if await self._try_handle_command(data, conn, text_for_cmd, msg_type, user_id, admin_qq):
            return
        if not self._check_wake_trigger(msg_type, is_slash_cmd, text_for_cmd, raw_message, conn, segments):
            return
        parsed = await self._parse_message_segments(data, conn, raw_message, text_for_cmd, segments)
        if parsed is None:
            return
        chat_id = _make_chat_id(data, account_name)
        if parsed["text"] and not parsed["images"] and not parsed["voice_url"]:
            if await self._resolve_approval_shortcut(chat_id, parsed["text"], user_id, admin_qq) or \
               await self._handle_update_shortcut(chat_id, parsed["text"]):
                return
        text = self._strip_at_mentions(parsed["text"], raw_message, conn, msg_type)
        if message_id:
            self._last_msg_id[chat_id] = message_id
        self._msg_receive_seq[message_id] = self._chat_msg_seq.get(chat_id, 0)
        self._cleanup_seq_dictionaries(chat_id)
        sender = data.get("sender", {})
        sender_name = sender.get("card") or sender.get("nickname") or user_id
        if msg_type == "group":
            conn.member_cache.set_from_sender(str(data.get("group_id", "")), user_id, sender)
        if self._show_qq_id and sender_name != user_id:
            sender_name = f"{sender_name}({user_id})"
        display_text, text, quoted_images = await self._build_display_text(
            parsed, text, msg_type, is_slash_cmd, sender_name, conn)
        await self._build_and_dispatch_event(
            parsed, display_text, text, chat_id, user_id, sender_name,
            message_id, msg_type, conn=conn, data=data,
            quoted_images=quoted_images)
    def _check_duplicate_and_self(self, data: dict, conn) -> bool:
        user_id = str(data.get("user_id", ""))
        message_id = str(data.get("message_id", ""))
        dedup_key = f"{message_id}_{user_id}_{data.get('time', '')}"
        if conn.dedup.is_duplicate(dedup_key):
            return True
        if conn.self_id and user_id == conn.self_id:
            return True
        return False
    async def _check_authorization_async(self, user_id: str, msg_type: str, data: dict, conn) -> bool:
        if conn.is_user_authorized(user_id, msg_type, data):
            return True
        if msg_type == "private":
            account_name = conn.name if self._multi_account else ""
            reject_key = _make_chat_id(data, account_name)
            last = self._reject_notified.get(reject_key, 0)
            if time.time() - last > REJECT_NOTIFY_TTL:
                await self._send_reply_async_conn(conn, data, "您好，当前bot仅对白名单用户开放，请联系管理员添加。")
                self._reject_notified[reject_key] = time.time()
        return False
    def _check_wake_trigger(self, msg_type: str, is_slash_cmd: bool,
                            text_for_cmd: str, raw_message, conn,
                            segments: Optional[List[Dict[str, Any]]] = None) -> bool:
        if msg_type == "group" and not is_slash_cmd:
            segments_for_wake = segments if segments is not None else _extract_segments(raw_message)
            if not conn.is_group_wake_triggered(raw_message, text_for_cmd, segments_for_wake):
                return False
        return True
    async def _parse_message_segments(self, data: dict, conn, raw_message, text_for_cmd: str = "",
                                      segments: Optional[List[Dict[str, Any]]] = None) -> Optional[dict]:
        segments = segments if segments is not None else _extract_segments(raw_message)
        text = text_for_cmd or _segments_text(segments)
        images = _extract_images(segments)
        voice_url = _extract_voice(segments)
        video_url = _extract_video(segments)
        at_targets = _extract_at(segments)
        reply_id = _extract_reply(segments)
        face_id = _extract_face(segments)
        forward_id = _extract_forward(segments)
        typed = _extract_typed_segments(segments)
        json_card = _extract_json_card(segments)
        xml_msg = _extract_xml(segments)
        if not text and not images and not voice_url and not video_url and not forward_id and not face_id \
                and not json_card and not xml_msg and not any(typed.values()):
            return None
        forward_content = ""
        forward_images: List[str] = []
        if forward_id:
            try:
                fwd_text, fwd_imgs = await self._resolve_forward_message(forward_id, conn)
                if fwd_text:
                    forward_content = fwd_text
                forward_images = fwd_imgs
            except Exception as e:
                pass
        return {
            "segments": segments,
            "text": text,
            "images": images,
            "voice_url": voice_url,
            "video_url": video_url,
            "at_targets": at_targets,
            "reply_id": reply_id,
            "face_id": face_id,
            "forward_id": forward_id,
            "json_card": json_card,
            "xml_msg": xml_msg,
            **typed,
            "forward_content": forward_content,
            "forward_images": forward_images,
        }
    @staticmethod
    def _prune_oldest(d: dict, max_size: int, prune_count: int = None):
        if len(d) <= max_size:
            return
        prune_count = prune_count or (max_size // 2)
        oldest = sorted(d, key=d.get)[:prune_count]
        for k in oldest:
            del d[k]
    @staticmethod
    def _prune_arbitrary(d: dict, max_size: int, prune_count: int = 100):
        if len(d) <= max_size:
            return
        for k in list(d.keys())[:prune_count]:
            del d[k]
    def _cleanup_seq_dictionaries(self, chat_id: str):
        now = time.time()
        if now - self._last_seq_cleanup_time < 60:
            return
        self._last_seq_cleanup_time = now
        self._prune_oldest(self._msg_receive_seq, 200, 50)
        self._prune_oldest(self._chat_msg_seq, 500, 300)
        for d in (self._last_msg_id, self._active_input_status):
            self._prune_arbitrary(d, 500)
        if len(self._last_progress_msg) > 200:
            for k in list(self._last_progress_msg)[:len(self._last_progress_msg) - 200]:
                del self._last_progress_msg[k]
        if len(self._pending_approvals) > 50 and _HAS_APPROVAL:
            stale = [k for k, sk in self._pending_approvals.items() if not has_blocking_approval(sk)]
            for k in stale:
                self._pending_approvals.pop(k, None)
                self._pending_approval_admin.pop(k, None)
        if len(self._approval_locks) > 100:
            active_approvals = set(self._pending_approvals)
            self._approval_locks = {k: v for k, v in self._approval_locks.items() if k in active_approvals}
        self._bg_delete_tasks = {t for t in self._bg_delete_tasks if not t.done()}
        for k in [k for k, t in self._active_tasks.items() if t.done()]:
            del self._active_tasks[k]
        for d, ttl in ((self._reject_notified, REJECT_NOTIFY_TTL), (self._pending_update_chats, PENDING_UPDATE_TTL)):
            for k in [k for k, v in d.items() if now - v > ttl]:
                del d[k]
    def _strip_at_mentions(self, text: str, raw_message, conn, msg_type: str) -> str:
        if msg_type == "group" and conn.self_id:
            text = re.sub(r'\[CQ:at,qq=' + re.escape(conn.self_id) + r'\]', '', text).strip()
            if isinstance(raw_message, list) and any(
                seg.get("type") == "at" and str((seg.get("data") or {}).get("qq", "")) == conn.self_id
                for seg in raw_message
            ):
                text = text.lstrip()
        return text
    async def _build_display_text(self, parsed: dict, text: str, msg_type: str,
                                   is_slash_cmd: bool, sender_name: str, conn) -> tuple:
        display_text = text
        if msg_type == "group" and not is_slash_cmd:
            display_text = f"[{sender_name}] {text}" if text else f"[{sender_name}]"
        if parsed["voice_url"] and not text:
            display_text = (display_text or "") + " [语音消息]"
        if parsed["video_url"]:
            display_text = (display_text or "") + " [视频消息]"
        if parsed["face_id"]:
            display_text = (display_text or "") + f" [表情{parsed['face_id']}]"
        for _seg_text in (parsed["json_card"], parsed["xml_msg"], parsed["file_seg"],
                          parsed["location_msg"], parsed["share_msg"], parsed["contact_msg"],
                          parsed["music_msg"], parsed["mface_msg"], parsed["rps_msg"], parsed["dice_msg"]):
            if _seg_text:
                display_text = (display_text or "") + " " + _seg_text
        if parsed["forward_content"]:
            display_text = (display_text or "") + parsed["forward_content"]
        if parsed["reply_id"]:
            display_text, quoted_images = await self._append_reply_context(display_text, parsed["reply_id"], conn)
        else:
            quoted_images = []
        forward_images = parsed.get("forward_images", [])
        if forward_images:
            quoted_images = (quoted_images or []) + forward_images
        return display_text, text, quoted_images
    async def _download_images_parallel(self, urls: List[str]) -> List[str]:
        if not urls:
            return []
        sem = asyncio.Semaphore(IMAGE_DOWNLOAD_CONCURRENCY)
        async def _download_one(img_url):
            async with sem:
                return await self._media_cache.download(img_url, self._http_client, "image")
        results = await asyncio.gather(*[_download_one(u) for u in urls], return_exceptions=True)
        paths = []
        for r in results:
            if isinstance(r, str) and r:
                paths.append(r)
            elif isinstance(r, Exception):
                pass
        return paths
    async def _inject_file_content(self, segments: List[Dict], text: str, conn) -> str:
        injected = text
        for seg in segments:
            if seg.get("type") != "file":
                continue
            data = seg.get("data") or {}
            file_name = data.get("name") or data.get("file") or ""
            ext = Path(file_name).suffix.lower() if file_name else ""
            if ext not in _TEXT_EXTS:
                continue
            local_path = None
            file_url = data.get("url") or data.get("file_url") or ""
            # C3: Only allow files from media cache directory (allowlist approach)
            cache_dir = str(self._media_cache._dir.resolve())
            if file_url.startswith("file://"):
                decoded = url_unquote(file_url[7:])
                resolved = str(Path(decoded).resolve())
                if resolved.startswith(cache_dir + os.sep) and os.path.isfile(resolved) and not os.path.islink(resolved):
                    local_path = resolved
            elif file_url.startswith(("http://", "https://")):
                try:
                    local_path = await self._media_cache.download(file_url, self._http_client, "file")
                except Exception as e:
                    logger.debug("File download failed: %s", e)
                    continue
            if not local_path or not os.path.isfile(local_path):
                continue
            # M7: File size limit
            if os.path.getsize(local_path) > FILE_INJECTION_MAX_BYTES:
                logger.debug("File too large for injection: %s", local_path)
                continue
            try:
                with open(local_path, "r", errors="replace") as f:
                    file_content = f.read(FILE_INJECTION_MAX_BYTES)
                display_name = os.path.basename(local_path)
                injection = f"[Content of {display_name}]:\n{file_content}"
                injected = f"{injection}\n\n{injected}" if injected.strip() else injection
            except Exception as e:
                pass
        return injected
    async def _build_and_dispatch_event(self, parsed: dict, display_text: str, text: str,
                                         chat_id: str, user_id: str, sender_name: str,
                                         message_id: str, msg_type: str, *,
                                         conn, data: dict,
                                         quoted_images: Optional[List[str]] = None):
        images = parsed["images"]
        voice_url = parsed["voice_url"]
        video_url = parsed["video_url"]
        reply_id = parsed["reply_id"]
        all_images = list(images) if images else []
        if quoted_images:
            all_images.extend(quoted_images)
        local_image_paths = await self._download_images_parallel(all_images)
        display_text = await self._inject_file_content(parsed.get("segments", []), display_text, conn)
        source = self.build_source(
            chat_id=chat_id, user_id=user_id, user_name=sender_name,
            message_id=message_id, chat_type="dm" if msg_type == "private" else "group",
        )
        msg_type_enum = MessageType.TEXT
        for key, mtype in _MSG_TYPE_MAP.items():
            if parsed.get(key):
                msg_type_enum = mtype
                break
        event = MessageEvent(
            source=source, text=display_text, message_type=msg_type_enum,
            raw_message=data, message_id=message_id,
        )
        all_media, all_types = [], []
        img_src = local_image_paths or all_images
        if img_src:
            all_media.extend(img_src)
            all_types.extend(["image"] * len(img_src))
        for url, mtype in ((voice_url, "voice"), (video_url, "video")):
            if url:
                all_media.append(url)
                all_types.append(mtype)
        if all_media:
            event.media_urls = all_media
            event.media_types = all_types
        if reply_id:
            event.reply_to_message_id = reply_id
        await self.handle_message(event)
    _FORWARD_MAX_DEPTH = 3
    _FORWARD_MAX_FETCHES = 8
    _FORWARD_MAX_IMAGES = 10
    async def _resolve_forward_message(self, forward_id: str, conn, *, depth: int = 0,
                                        _seen: Optional[set] = None,
                                        _fetch_count: List[int] = None) -> Tuple[str, List[str]]:
        if _seen is None:
            _seen = set()
        if _fetch_count is None:
            _fetch_count = [0]
        if forward_id in _seen or depth > self._FORWARD_MAX_DEPTH:
            return "", []
        _seen.add(forward_id)
        _fetch_count[0] += 1
        if _fetch_count[0] > self._FORWARD_MAX_FETCHES:
            return "", []
        fwd_lines: List[str] = []
        fwd_images: List[str] = []
        try:
            forward_msgs = await self.get_forward_msg(forward_id, conn=conn)
        except Exception as e:
            return "", []
        if not forward_msgs:
            return "", []
        for fmsg in forward_msgs:
            f_name = (fmsg.get("sender", {}).get("nickname")
                      or fmsg.get("sender", {}).get("card") or "未知")
            raw_content = fmsg.get("content") or fmsg.get("message") or ""
            f_segments = _extract_segments(raw_content)
            f_text = _extract_text_from_message(raw_content)
            f_images = _extract_images(f_segments)
            nested_forward_id = _extract_forward(f_segments)
            json_card = _extract_json_card(f_segments)
            line_parts = []
            if f_text:
                line_parts.append(f_text)
            if f_images and len(fwd_images) < self._FORWARD_MAX_IMAGES:
                fwd_images.extend(f_images[:self._FORWARD_MAX_IMAGES - len(fwd_images)])
                line_parts.append("[图片]")
            if json_card:
                line_parts.append(json_card)
            if nested_forward_id:
                nested_text, nested_imgs = await self._resolve_forward_message(
                    nested_forward_id, conn, depth=depth + 1, _seen=_seen, _fetch_count=_fetch_count)
                if nested_text:
                    line_parts.append(nested_text)
                if nested_imgs and len(fwd_images) < self._FORWARD_MAX_IMAGES:
                    fwd_images.extend(nested_imgs[:self._FORWARD_MAX_IMAGES - len(fwd_images)])
            if line_parts:
                fwd_lines.append(f"{'  ' * depth}{f_name}: {' '.join(line_parts)}")
        text_block = ""
        if fwd_lines:
            text_block = "\n[合并转发消息]\n" + "\n".join(fwd_lines) + "\n[转发结束]"
        return text_block, fwd_images
    async def _append_reply_context(self, display_text: str, reply_id: str, conn) -> tuple:
        quoted_images: List[str] = []
        _fallback = "\n[引用了一条消息，但无法获取内容]"
        try:
            quoted_obj = await asyncio.wait_for(self.get_msg(reply_id, conn=conn), timeout=10.0)
            if not quoted_obj:
                return (display_text or "") + _fallback, quoted_images
            quoted_message = quoted_obj.get("message", "")
            quoted_text = _extract_text_from_message(quoted_message)
            quoted_name = (quoted_obj.get("sender", {}).get("nickname") or quoted_obj.get("real_id", "?"))
            quoted_segments = _extract_segments(quoted_message)
            quoted_images = _extract_images(quoted_segments)
            quoted_forward_id = _extract_forward(quoted_segments)
            quoted_json_card = _extract_json_card(quoted_segments)
            quote_parts = []
            if quoted_text:
                quote_parts.append(quoted_text[:MAX_MULTIMSG_PREVIEW] + "…" if len(quoted_text) > MAX_MULTIMSG_PREVIEW else quoted_text)
            elif quoted_images:
                quote_parts.append("[图片]")
            if quoted_forward_id:
                try:
                    fwd_text, fwd_imgs = await self._resolve_forward_message(quoted_forward_id, conn)
                    if fwd_text:
                        quote_parts.append(fwd_text)
                    if fwd_imgs:
                        quoted_images.extend(fwd_imgs)
                except Exception:
                    pass
            elif quoted_json_card:
                quote_parts.append(quoted_json_card)
            if quote_parts:
                combined = " ".join(quote_parts)
                combined = combined[:MAX_QUOTE_TEXT] + "…" if len(combined) > MAX_QUOTE_TEXT else combined
                display_text = (display_text or "") + f"\n[引用 {quoted_name}: {combined}]"
            else:
                display_text = (display_text or "") + _fallback
        except Exception:
            display_text = (display_text or "") + _fallback
        return display_text, quoted_images
    async def _try_action_formats(self, action: str, params_list: List[dict], conn=None) -> dict:
        for params in params_list:
            if conn is not None:
                result = await self._send_action_conn(conn, action, params)
            else:
                result = await self._send_action(action, params)
            if result.get("retcode") == 0:
                data = result.get("data", {})
                if data:
                    return data
        return {}
    async def get_msg(self, message_id: str, conn=None) -> Dict:
        mid_str = str(message_id).strip()
        if not mid_str:
            return {}
        params_list: List[dict] = []
        try:
            mid_int = int(mid_str)
            params_list.extend([{"message_id": mid_int}, {"id": mid_int}])
        except (ValueError, TypeError):
            pass
        params_list.extend([{"message_id": mid_str}, {"id": mid_str}])
        cached = getattr(conn, '_working_msg_params', None) if conn else None
        if cached:
            key_name = cached.get("_key", "message_id")
            for p in [{key_name: mid_str}] + ([{key_name: int(mid_str)}] if mid_str.isdigit() else []):
                result = await self._send_action_conn(conn, "get_msg", p)
                if result.get("retcode") == 0 and result.get("data"):
                    return result["data"]
            params_list = [p for p in params_list if p != {key_name: mid_str}]
        data = await self._try_action_formats("get_msg", params_list, conn)
        if data and conn is not None:
            conn._working_msg_params = {"_key": "message_id"}
        return data
    async def get_forward_msg(self, forward_id: str, conn=None) -> List[Dict]:
        fid_str = str(forward_id).strip()
        if not fid_str:
            return []
        params_list: List[dict] = [{"id": fid_str}, {"forward_id": fid_str}]
        try:
            fid_int = int(fid_str)
            params_list.insert(0, {"id": fid_int})
        except (ValueError, TypeError):
            pass
        data = await self._try_action_formats("get_forward_msg", params_list, conn)
        if isinstance(data, dict):
            msgs = data.get("messages") or data.get("message") or data.get("items") or []
            return msgs if msgs else []
        if isinstance(data, list):
            return data
        return []
class CommandMixin:
    def _register_commands(self):
        _lm = self._cmd_list_mutate
        cmds = [
            ("/adduser", functools.partial(_lm, entity_type="user", action="add"), True),
            ("/removeuser", functools.partial(_lm, entity_type="user", action="remove"), True),
            ("/listusers", functools.partial(_lm, entity_type="user", action="list"), True),
            ("/help", self._cmd_help, False),
            ("/addgroup", functools.partial(_lm, entity_type="group", action="add"), True),
            ("/rmgroup", functools.partial(_lm, entity_type="group", action="remove"), True),
            ("/listgroups", functools.partial(_lm, entity_type="group", action="list"), True),
            ("/settool", self._cmd_settool, True),
            ("/setmd", self._cmd_setmd, True),
            ("/setallowall", self._cmd_setallowall, True),
            ("/config", self._cmd_config, True),
        ]
        for name, handler, admin in cmds:
            self._commands[name] = _CmdDef(name, handler, admin_only=admin)
    async def _try_handle_command(self, data: dict, conn, text_for_cmd: str,
                                   msg_type: str, user_id: str, admin_qq) -> bool:
        matches = [(name, defn) for name, defn in self._commands.items()
                   if text_for_cmd == name or text_for_cmd.startswith(name + " ")]
        if not matches:
            return False
        cmd_name, cmd_def = max(matches, key=lambda m: len(m[0]))
        cmd_args = text_for_cmd[len(cmd_name):].strip()
        if cmd_def.admin_only and user_id != admin_qq:
            await self._send_reply_async_conn(conn, data, "✗ 只有管理员可以执行此命令")
            return True
        if cmd_def.group_only and msg_type != "group":
            await self._send_reply_async_conn(conn, data, "✗ 此命令只能在群聊中使用")
            return True
        await cmd_def.handler(conn, data, cmd_args, user_id, admin_qq)
        return True
    async def _cmd_list_mutate(self, conn, data, args, user_id, admin_qq,
                                entity_type: str, action: str):
        labels = {"user": ("白名单", "用户", "QQ号", "adduser", "removeuser"),
                  "group": ("群白名单", "群", "群号", "addgroup", "rmgroup")}
        list_label, entity_label, id_label, add_cmd, rm_cmd = labels[entity_type]
        _reply = lambda msg: self._send_reply_async_conn(conn, data, msg)
        get_list = conn.list_allowed_users if entity_type == "user" else lambda: list(conn.group_ids)
        persist = self._persist_allowed_users if entity_type == "user" else self._persist_group_ids
        if action == "list":
            items = get_list()
            msg = f"当前{list_label}：\n" + "\n".join(f"• {u}" for u in items) if items else f"{list_label}为空"
            await _reply(msg)
            return
        val = args.strip()
        if not val:
            await _reply(f"✗ 用法: /{add_cmd if action == 'add' else rm_cmd} <{id_label}>")
            return
        if entity_type == "group":
            if action == "add":
                if not val.isdigit():
                    await _reply(f"✗ {id_label}格式错误")
                elif val not in conn.group_ids:
                    conn.group_ids.append(val)
                    await persist(conn)
                    await _reply(f"✓ 已添加{entity_label} {val}")
                else:
                    await _reply(f"✗ {entity_label} {val} 已在{list_label}中")
            elif val in conn.group_ids:
                conn.group_ids.remove(val)
                await persist(conn)
                await _reply(f"✓ 已移除{entity_label} {val}")
            else:
                await _reply(f"✗ {entity_label} {val} 不在{list_label}中")
            return
        add_fn = conn.add_allowed_user
        remove_fn = conn.remove_allowed_user
        if action == "add":
            if add_fn(val):
                await persist(conn)
                await _reply(f"✓ 已添加{entity_label} {val} 到{list_label}")
            else:
                await _reply(f"✗ 添加失败，{entity_label}可能已存在或格式错误")
        elif val == admin_qq:
            await _reply("✗ 不能移除管理员账户")
        elif remove_fn(val):
            await persist(conn)
            await _reply(f"✓ 已从{list_label}移除{entity_label} {val}")
        else:
            await _reply(f"✗ 移除失败，{entity_label}可能不存在")
    async def _cmd_help(self, conn, data, args, user_id, admin_qq):
        msg = (
            "📋 指令列表\n"
            "【查询】/config 当前配置\n"
            "【管理】/adduser <QQ> 加人 | /removeuser <QQ> 删人 | /listusers 白名单\n"
            "【群聊】/addgroup <群号> | /rmgroup <群号> | /listgroups\n"
            "【开关】/settool on|off 工具提示 | /setmd on|off 去Markdown\n"
            "/setallowall on|off 允许所有人\n"
            "【Hermes】/approve 批准 | /deny 拒绝 | /new 新会话 | /stop 停止 | /model 切换模型"
        )
        await self._send_reply_async_conn(conn, data, msg)
    async def _persist_group_ids(self, conn):
        await self._persist_account_setting(conn, "group_ids_by_account", conn.group_ids)
    async def _cmd_toggle_setting(self, conn, data, args, setting_key, label, cmd_name, is_global=False):
        val = args.strip().lower()
        if val not in ("on", "off"):
            await self._send_reply_async_conn(conn, data, f"✗ 用法: /{cmd_name} on|off")
            return
        if is_global:
            cs = self._get_global_settings()
        else:
            account_name = conn.name if self._multi_account else ""
            cs = self._get_chat_settings(_make_chat_id(data, account_name))
        cs[setting_key] = (val == "on")
        await self._save_settings()
        await self._send_reply_async_conn(conn, data, f"✓ {label}: {'开启' if val == 'on' else '关闭'}")
    async def _cmd_settool(self, conn, data, args, user_id, admin_qq):
        await self._cmd_toggle_setting(conn, data, args, "tool_progress", "工具调用提示", "settool")
    async def _cmd_setmd(self, conn, data, args, user_id, admin_qq):
        await self._cmd_toggle_setting(conn, data, args, "strip_markdown", "Markdown清理", "setmd")
    async def _cmd_setallowall(self, conn, data, args, user_id, admin_qq):
        val = args.strip().lower()
        if val not in ("on", "off"):
            await self._send_reply_async_conn(conn, data, "✗ 用法: /setallowall on|off")
            return
        conn.allow_all = (val == "on")
        gs = self._get_global_settings()
        if "allow_all_by_account" not in gs:
            gs["allow_all_by_account"] = {}
        gs["allow_all_by_account"][conn.name] = conn.allow_all
        await self._save_settings()
        await self._send_reply_async_conn(conn, data,
            f"✓ 允许所有人使用: {'开启' if val == 'on' else '关闭'}")
    async def _cmd_config(self, conn, data, args, user_id, admin_qq):
        account_name = conn.name if self._multi_account else ""
        _cfg_chat_id = _make_chat_id(data, account_name)
        cs = self._plugin_settings.get_chat(_cfg_chat_id)
        gs = self._plugin_settings.get_chat("_global")
        lines = [f"📋 配置 — {_cfg_chat_id}\n"]
        for key, label in [("tool_progress", "工具调用提示"),
                           ("strip_markdown", "Markdown清理")]:
            val = cs.get(key)
            lines.append(f"• {label}: {'开启' if val else ('关闭' if val is not None else '默认')}")
        allow_all_accounts = gs.get("allow_all_by_account", {})
        conn_allow_all = allow_all_accounts.get(conn.name, conn.allow_all)
        lines.append(f"• 允许所有人使用: {'开启' if conn_allow_all else '关闭'}")
        lines.append(f"• 显示QQ号: {'开启' if self._show_qq_id else '关闭'}")
        if conn.http_api_url:
            lines.append(f"• HTTP API: {conn.http_api_url}")
        lines.append(f"• 主页频道: {conn.home_channel or '未设置'}")
        if conn.group_ids:
            lines.append(f"• 群白名单: {', '.join(conn.group_ids)}")
        else:
            lines.append("• 群白名单: 未限制（所有群均可用）")
        if conn.allowed_users:
            lines.append(f"• 用户白名单: {', '.join(conn.allowed_users)}")
        else:
            lines.append("• 用户白名单: 未限制（所有用户均可用）")
        await self._send_reply_async_conn(conn, data, "\n".join(lines))
def _result_to_send_result(result: dict, action_name: str, extract_msg_id: bool = False) -> SendResult:
    if result.get("retcode") == 0:
        if extract_msg_id:
            msg_id = str(result.get("data", {}).get("message_id", ""))
            return SendResult(success=True, message_id=msg_id)
        return SendResult(success=True)
    err = result.get("msg") or result.get("wording") or f"{action_name} failed"
    return SendResult(success=False, error=err, retryable=result.get("retcode") == -1)
class SendMixin:
    @staticmethod
    def _cleanup_echo(conn: _NapCatConnection, echo: str):
        conn.echo_futures.pop(echo, None)
        conn._echo_timestamps.pop(echo, None)
    async def _wait_for_ready_ws_conn(self, conn: _NapCatConnection, timeout: float = 10.0):
        """Wait briefly for reverse-WS NapCat clients before outbound sends."""
        deadline = time.monotonic() + max(0.0, timeout)
        while time.monotonic() < deadline:
            ws = conn.ws
            if ws and getattr(ws, "close_code", None) is None:
                return ws
            await asyncio.sleep(0.25)
        return conn.ws

    async def _send_action_conn(self, conn: _NapCatConnection, action: str, params: dict, timeout: float = 15.0) -> dict:
        if action in self._unsupported_actions:
            return {"status": "failed", "retcode": 1, "msg": f"action '{action}' not supported by this NapCat version"}
        ws = conn.ws
        if (not ws or getattr(ws, "close_code", None) is not None) and conn.ws_mode == "reverse":
            ws = await self._wait_for_ready_ws_conn(conn)
        if not ws or getattr(ws, "close_code", None) is not None:
            if conn.http_api_url:
                return await self._http_call_conn(conn, action, params)
            return {"status": "failed", "retcode": -1, "msg": "not connected"}
        echo = str(uuid.uuid4())
        payload = {"action": action, "params": params, "echo": echo}
        loop = asyncio.get_running_loop()
        fut = loop.create_future()
        conn.echo_futures[echo] = fut
        conn._echo_timestamps[echo] = time.time()
        try:
            await conn.rate_limiter.acquire()
            await ws.send(json.dumps(payload))
            result = await asyncio.wait_for(fut, timeout=timeout)
            if result.get("status") == "failed" or (result.get("retcode") is not None and result.get("retcode") != 0):
                msg = (result.get("msg", "") or result.get("wording", "") or "").lower()
                if "不支持" in msg or "not supported" in msg or "unknown action" in msg:
                    self._unsupported_actions.add(action)
            return result
        except asyncio.TimeoutError:
            self._cleanup_echo(conn, echo)
            return {"status": "failed", "retcode": -1, "msg": "timeout"}
        except asyncio.CancelledError:
            self._cleanup_echo(conn, echo)
            raise
        except Exception as e:
            self._cleanup_echo(conn, echo)
            return {"status": "failed", "retcode": -1, "msg": str(e)}
    async def _send_reply_async_conn(self, conn: _NapCatConnection, data: dict, text: str):
        msg_type = data.get("message_type", "")
        msg_kind = "group" if msg_type == "group" else "private"
        target_id = str(data.get("group_id" if msg_kind == "group" else "user_id", ""))
        try:
            action, params = self._send_msg_params(msg_kind, target_id, [{"type": "text", "data": {"text": text}}])
            result = await self._send_action_conn(conn, action, params)
        except (ValueError, TypeError) as e:
            return
        if result.get("retcode") != 0:
            logger.debug("Reply send failed: retcode=%s", result.get("retcode"))
    async def _send_action(self, action: str, params: dict, timeout: float = 15.0) -> dict:
        return await self._send_action_conn(self._default_conn, action, params, timeout)
    async def _http_call_conn(self, conn: _NapCatConnection, action: str, params: dict) -> dict:
        if not conn.http_api_url:
            return {"status": "failed", "retcode": -1, "msg": "HTTP API not configured"}
        parsed = urlparse(conn.http_api_url)
        if parsed.scheme not in ("http", "https"):
            return {"status": "failed", "retcode": -1, "msg": "invalid URL scheme"}
        url = f"{conn.http_api_url.rstrip('/')}/{action}"
        payload = json.dumps(params).encode()
        headers = {"Content-Type": "application/json"}
        if conn.access_token:
            headers["Authorization"] = f"Bearer {conn.access_token}"
        def _sync_call():
            req = urllib.request.Request(url, data=payload, headers=headers)
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.loads(resp.read().decode())
        try:
            return await asyncio.to_thread(_sync_call)
        except urllib.error.HTTPError as e:
            return {"status": "failed", "retcode": e.code, "msg": f"HTTP {e.code}: {e.reason}"}
        except Exception as e:
            return {"status": "failed", "retcode": -1, "msg": str(e)}
    def _send_msg_params(self, msg_kind: str, target_id: str, message_segments: list) -> Tuple[str, dict]:
        tid = _safe_int(target_id, "target_id")
        return (f"send_{msg_kind}_msg", {_onebot_target_key(msg_kind): tid, "message": message_segments})
    def _should_quote(self, chat_id: str, reply_to: Optional[str]) -> Optional[str]:
        if not reply_to:
            return None
        receive_seq = self._msg_receive_seq.get(reply_to)
        if receive_seq is None:
            return None
        return reply_to if self._chat_msg_seq.get(chat_id, 0) > receive_seq else None

    def _message_with_optional_reply(self, chat_id: str, reply_to: Optional[str], *segments: dict) -> List[dict]:
        message = []
        quoted = self._should_quote(chat_id, reply_to)
        if quoted:
            message.append({"type": "reply", "data": {"id": str(quoted)}})
        message.extend(segments)
        return message

    async def _send_chat_segments(self, chat_id: str, segments: List[dict], timeout: float = 15.0) -> dict:
        conn = self._get_conn_for_chat(chat_id)
        msg_kind, target_id = _parse_chat_id(chat_id)
        action, params = self._send_msg_params(msg_kind, target_id, segments)
        return await self._send_action_conn(conn, action, params, timeout=timeout)
    async def send(
        self,
        chat_id: str,
        content: str,
        reply_to: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> SendResult:
        if chat_id not in getattr(self, '_in_edit_resend_count', {}) or getattr(self, '_in_edit_resend_count', {}).get(chat_id, 0) <= 0:
            _progress_map = getattr(self, '_last_progress_msg', None)
            if _progress_map is not None:
                _prev_progress = _progress_map.pop(chat_id, None)
                if _prev_progress:
                    self._fire_and_forget_delete(chat_id, _prev_progress)
        await self.clear_input_status(chat_id)
        settings = self._plugin_settings.get_chat(chat_id)
        # When tool_progress is off for this chat, intercept tool progress
        # messages (emoji + tool_name pattern) — return success without sending.
        if settings.get("tool_progress") is False and _TOOL_PROGRESS_RE.match(content):
            return SendResult(success=True)
        if settings.get("strip_markdown", True):
            content = self.format_message(content)
        message_segments = self._message_with_optional_reply(
            chat_id, reply_to, {"type": "text", "data": {"text": content}}
        )
        result = await self._send_chat_segments(chat_id, message_segments)
        if result.get("retcode") != 0:
            logger.debug("Send failed: retcode=%s", result.get("retcode"))
        return _result_to_send_result(result, "send", extract_msg_id=True)
    async def send_image(
        self, chat_id: str, image_url: str, caption: Optional[str] = None,
        reply_to: Optional[str] = None, metadata: Optional[Dict[str, Any]] = None,
    ) -> SendResult:
        return await self._send_media(chat_id, "image", image_url, caption, reply_to)
    async def _send_local_file(
        self, chat_id: str, path: str, seg_type: str, caption: Optional[str] = None,
        reply_to: Optional[str] = None, timeout: float = 30.0,
    ) -> SendResult:
        file_uri = f"file://{os.path.abspath(path)}"
        return await self._send_media(chat_id, seg_type, file_uri, caption, reply_to, timeout=timeout)
    async def send_image_file(
        self, chat_id: str, image_path: str, caption: Optional[str] = None,
        reply_to: Optional[str] = None, metadata: Optional[Dict[str, Any]] = None, **kwargs,
    ) -> SendResult:
        return await self._send_local_file(chat_id, image_path, "image", caption, reply_to)
    async def send_voice(
        self, chat_id: str, audio_path: str, caption: Optional[str] = None,
        reply_to: Optional[str] = None, metadata: Optional[Dict[str, Any]] = None, **kwargs,
    ) -> SendResult:
        return await self._send_local_file(chat_id, audio_path, "record", caption, reply_to)
    async def send_video(
        self, chat_id: str, video_path: str, caption: Optional[str] = None,
        reply_to: Optional[str] = None, metadata: Optional[Dict[str, Any]] = None, **kwargs,
    ) -> SendResult:
        return await self._send_local_file(chat_id, video_path, "video", caption, reply_to, timeout=60.0)
    async def send_document(
        self,
        chat_id: str,
        file_path: str,
        caption: Optional[str] = None,
        file_name: Optional[str] = None,
        reply_to: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
        **kwargs,
    ) -> SendResult:
        conn = self._get_conn_for_chat(chat_id)
        msg_kind, target_id = _parse_chat_id(chat_id)
        abs_path = os.path.abspath(file_path)
        name = file_name or os.path.basename(abs_path)
        file_uri = f"file://{abs_path}"
        tid = _safe_target_id(target_id)
        if isinstance(tid, SendResult):
            return tid
        action = "upload_group_file" if msg_kind == "group" else "upload_private_file"
        params = {_onebot_target_key(msg_kind): tid, "file": file_uri, "name": name}
        result = await self._send_action_conn(conn, action, params, timeout=60.0)
        return _result_to_send_result(result, "send_document")
    async def send_poke(self, chat_id: str, user_id: str) -> SendResult:
        conn = self._get_conn_for_chat(chat_id)
        msg_kind, target_id = _parse_chat_id(chat_id)
        if msg_kind == "group":
            message = [{"type": "poke", "data": {"qq": str(user_id)}}]
            action, params = self._send_msg_params(msg_kind, target_id, message)
            result = await self._send_action_conn(conn, action, params)
        else:
            try:
                uid = _safe_int(user_id, "user_id")
            except ValueError as e:
                return SendResult(success=False, error=str(e))
            result = await self._send_action_conn(conn, "friend_poke", {"user_id": uid})
        return _result_to_send_result(result, "send_poke", extract_msg_id=True)
    async def send_emoji_reaction(self, chat_id: str, message_id: str, emoji_id: int) -> SendResult:
        conn = self._get_conn_for_chat(chat_id)
        try:
            mid = _safe_int(message_id, "message_id")
            eid = _safe_int(emoji_id, "emoji_id")
        except ValueError as e:
            return SendResult(success=False, error=str(e))
        result = await self._send_action_conn(conn, "set_msg_emoji_like", {
            "message_id": mid,
            "emoji_id": eid,
        })
        return _result_to_send_result(result, "set_msg_emoji_like")
    async def _handle_notice(self, data: dict, conn: _NapCatConnection) -> None:
        notice_type = data.get("notice_type", "")
        sub_type = data.get("sub_type", "")
        if notice_type == "notify" and sub_type == "poke":
            poker_id = str(data.get("user_id", ""))
            target_id = data.get("target_id", "")
            self_id = data.get("self_id", "")
            if str(target_id) != str(self_id):
                return
            if _HAS_APPROVAL:
                chat_id = f"private_{poker_id}"
                is_admin_approval = self._pending_approval_admin.get(chat_id, False)
                admin_qq = os.getenv("ONEBOT_ADMIN_QQ") or conn.admin_qq or (conn.allowed_users[0] if conn.allowed_users else None)
                if is_admin_approval and admin_qq and str(poker_id) != str(admin_qq):
                    return
                if chat_id in self._pending_approvals:
                    admin_qq = admin_qq or os.getenv("ONEBOT_ADMIN_QQ") or conn.admin_qq or (conn.allowed_users[0] if conn.allowed_users else None)
                    await self._resolve_approval_shortcut(chat_id, "1", poker_id, admin_qq)
    async def set_input_status(self, chat_id: str, event_type: int = 1) -> SendResult:
        msg_kind, target_id = _parse_chat_id(chat_id)
        if msg_kind == "group":
            return SendResult(success=False, error="typing indicator only supports private chats")
        conn = self._get_conn_for_chat(chat_id)
        tid = _safe_target_id(target_id)
        if isinstance(tid, SendResult):
            return tid
        result = await self._send_action_conn(conn, "set_input_status", {
            "user_id": tid,
            "event_type": event_type,
        })
        if event_type:
            self._active_input_status[chat_id] = True
        else:
            self._active_input_status.pop(chat_id, None)
        return _result_to_send_result(result, "set_input_status")
    async def clear_input_status(self, chat_id: str) -> None:
        if self._active_input_status.get(chat_id):
            await self.set_input_status(chat_id, event_type=0)
    async def send_typing(self, chat_id: str, metadata=None) -> None:
        try:
            await self.set_input_status(chat_id, event_type=1)
        except Exception:
            pass
    def format_message(self, content: str) -> str:
        return _format_message(content)
    async def edit_message(
        self,
        chat_id: str,
        message_id: str,
        content: str,
        metadata: Optional[Dict[str, Any]] = None,
        finalize: bool = False,
    ) -> SendResult:
        if not self._delete_msg_supported:
            return SendResult(success=True, message_id=message_id)
        _progress_map = getattr(self, '_last_progress_msg', None)
        real_message_id = (_progress_map or {}).get(chat_id) or message_id
        delete_result = await self._delete_message_with_status(chat_id, real_message_id, timeout=2.0)
        if delete_result is None:
            self._delete_msg_supported = False
            return SendResult(success=True, message_id=message_id)
        if not delete_result:
            return SendResult(success=True, message_id=message_id)
        self._in_edit_resend_count[chat_id] = self._in_edit_resend_count.get(chat_id, 0) + 1
        try:
            result = await self.send(chat_id, content, metadata=metadata)
        finally:
            self._in_edit_resend_count[chat_id] -= 1
            if self._in_edit_resend_count[chat_id] <= 0:
                del self._in_edit_resend_count[chat_id]
        if not result.success:
            if _progress_map is not None and chat_id in _progress_map:
                del _progress_map[chat_id]
            return SendResult(success=True, message_id=message_id)
        if result.message_id and _progress_map is not None:
            _progress_map[chat_id] = str(result.message_id)
        return SendResult(success=True, message_id=message_id)
    async def stop_typing(self, chat_id: str) -> None:
        await self.clear_input_status(chat_id)
    async def delete_message(self, chat_id: str, message_id: str, timeout: float = 5.0) -> bool:
        if not self._delete_msg_supported:
            return False
        status = await self._delete_message_with_status(chat_id, message_id, timeout=timeout)
        return status is True
    def _fire_and_forget_delete(self, chat_id: str, message_id: str) -> None:
        if not self._delete_msg_supported:
            return
        try:
            task = asyncio.ensure_future(self._bg_delete(chat_id, message_id))
            self._bg_delete_tasks.add(task)
            task.add_done_callback(self._bg_delete_tasks.discard)
        except Exception:
            pass
    async def _bg_delete(self, chat_id: str, message_id: str) -> None:
        try:
            status = await self._delete_message_with_status(chat_id, message_id, timeout=3.0)
            if status is None:
                self._delete_msg_supported = False
        except Exception:
            pass
    async def _delete_message_with_status(self, chat_id: str, message_id: str, timeout: float = 15.0) -> Optional[bool]:
        conn = self._get_conn_for_chat(chat_id)
        try:
            mid = _safe_int(message_id, "message_id")
        except ValueError:
            return False
        try:
            result = await self._send_action_conn(conn, "delete_msg", {"message_id": mid}, timeout=timeout)
            retcode = result.get("retcode")
            if retcode == 0:
                return True
            if retcode == -1:
                return None
            return False
        except Exception:
            return None
    async def _send_media(self, chat_id: str, seg_type: str, file_val: str,
                          caption: str = None, reply_to: str = None, timeout: float = 30.0) -> SendResult:
        conn = self._get_conn_for_chat(chat_id)
        msg_kind, target_id = _parse_chat_id(chat_id)
        segments = [{"type": seg_type, "data": {"file": file_val}}]
        if caption:
            segments.append({"type": "text", "data": {"text": caption}})
        message = self._message_with_optional_reply(chat_id, reply_to, *segments)
        if seg_type == "image" and conn.http_api_url:
            try:
                tid = _safe_target_id(target_id)
                if isinstance(tid, SendResult):
                    return tid
                action = f"send_{msg_kind}_msg"
                params = {_onebot_target_key(msg_kind): tid, "message": message}
                result = await self._http_call_conn(conn, action, params)
                retcode = result.get("retcode", -1)
                if retcode in (0, 200):
                    return SendResult(success=True)
            except Exception as e:
                logger.debug("HTTP fallback for media send failed: %s", e)
        action, params = self._send_msg_params(msg_kind, target_id, message)
        result = await self._send_action_conn(conn, action, params, timeout=timeout)
        retcode = result.get("retcode")
        if seg_type == "image" and retcode == 200:
            return SendResult(success=True)
        if seg_type == "image" and retcode == -1:
            msg = result.get("msg", "")
            if "timeout" in msg.lower():
                return SendResult(success=True)
        return _result_to_send_result(result, f"send_{seg_type}", extract_msg_id=True)
    async def send_forward_message(
        self, chat_id: str, messages: List[Dict[str, Any]],
        conn: Optional[_NapCatConnection] = None,
    ) -> SendResult:
        if conn is None:
            conn = self._get_conn_for_chat(chat_id)
        msg_kind, target_id = _parse_chat_id(chat_id)
        tid = _safe_target_id(target_id)
        if isinstance(tid, SendResult):
            return tid
        nodes = []
        for msg in messages:
            content = msg.get("content", "")
            segs = content if isinstance(content, list) else [{"type": "text", "data": {"text": str(content)}}]
            sid = msg.get("sender_id", "10000")
            nodes.append({"type": "node", "data": {
                "nickname": msg.get("sender_name", "匿名"),
                "user_id": int(sid) if str(sid).isdigit() else 10000,
                "content": segs,
            }})
        if not nodes:
            return SendResult(success=False, error="No messages to forward")
        action = f"send_{msg_kind}_forward_msg"
        result = await self._send_action_conn(conn, action, {_onebot_target_key(msg_kind): tid, "messages": nodes}, timeout=30.0)
        if result.get("retcode") == 0:
            d = result.get("data", {})
            return SendResult(success=True, message_id=str(d.get("message_id", "") or d.get("forward_id", "")))
        return SendResult(success=False, error=result.get("msg", "send_forward_message failed"))
_APPROVAL_CHOICES = {
    "1": "once", "approve": "once", "批准": "once", "y": "once", "yes": "once",
    "2": "always", "approve always": "always", "永久批准": "always", "always": "always",
    "3": "deny", "deny": "deny", "拒绝": "deny", "n": "deny", "no": "deny",
}
_UPDATE_CHOICES = {
    "1": "y", "y": "y", "yes": "y", "是": "y", "确认": "y",
    "2": "n", "n": "n", "no": "n", "否": "n", "取消": "n",
}
class ApprovalMixin:
    async def send_exec_approval(
        self,
        chat_id: str,
        command: str,
        session_key: str,
        description: str = "dangerous command",
        metadata: Optional[Dict[str, Any]] = None,
    ) -> SendResult:
        self._pending_approvals[chat_id] = session_key
        if metadata and metadata.get("admin_only"):
            self._pending_approval_admin[chat_id] = True
        else:
            self._pending_approval_admin.pop(chat_id, None)
        cmd_preview = command[:300] + "..." if len(command) > 300 else command
        msg = (
            f"⚠️ 危险命令审批:\n"
            f"命令: {cmd_preview}\n"
            f"原因: {description}\n"
            f"戳一戳我批准一次 / 回复1批准 / 回复2永久批准 / 回复3拒绝"
        )
        reply_to = self._last_msg_id.get(chat_id)
        result = await self.send(chat_id, msg, reply_to=reply_to)
        return result
    async def send_update_prompt(
        self,
        chat_id: str,
        prompt: str,
        default: str = "",
        session_key: str = "",
        metadata: Optional[Dict[str, Any]] = None,
    ) -> SendResult:
        msg = (
            f"🔄 更新确认:\n"
            f"{prompt}\n\n"
            f"回复 1 或 y → 确认\n"
            f"回复 2 或 n → 取消"
        )
        reply_to = self._last_msg_id.get(chat_id)
        self._pending_update_chats[chat_id] = time.time()
        return await self.send(chat_id, msg, reply_to=reply_to)
    async def _resolve_approval_shortcut(
        self, chat_id: str, user_text: str, user_id: str = "", admin_qq: str = "",
    ) -> bool:
        if not _HAS_APPROVAL:
            return False
        lock = self._approval_locks.setdefault(chat_id, asyncio.Lock())
        async with lock:
            session_key = self._pending_approvals.pop(chat_id, None)
            is_admin_approval = self._pending_approval_admin.pop(chat_id, False)
            if not session_key:
                return False
            try:
                from tools.approval import has_blocking_approval
                if not has_blocking_approval(session_key):
                    return False
            except ImportError:
                pass
            if is_admin_approval and admin_qq and user_id and str(user_id) != str(admin_qq):
                self._pending_approvals[chat_id] = session_key
                self._pending_approval_admin[chat_id] = True
                return False
            text = _strip_slash(user_text.strip().lower())
            choice = _APPROVAL_CHOICES.get(text)
            if choice is None:
                self._pending_approvals[chat_id] = session_key
                if is_admin_approval:
                    self._pending_approval_admin[chat_id] = True
                return False
        try:
            from tools.approval import resolve_gateway_approval
            count = resolve_gateway_approval(session_key, choice)
            choice_text = {"once": "批准一次", "always": "永久批准", "deny": "已拒绝"}
            await self.send(chat_id, f"✓ {choice_text.get(choice, choice)}")
            return True
        except Exception as e:
            return False
    async def _handle_update_shortcut(self, chat_id: str, user_text: str) -> bool:
        if chat_id not in self._pending_update_chats:
            return False
        text = _strip_slash(user_text.strip().lower())
        answer = _UPDATE_CHOICES.get(text)
        if answer is None:
            return False
        self._pending_update_chats.pop(chat_id, None)
        try:
            from hermes_constants import get_hermes_home
            home = get_hermes_home()
            response_path = home / ".update_response"
            tmp = response_path.with_suffix(".tmp")
            tmp.write_text(answer)
            tmp.replace(response_path)
            await self.send(chat_id, f"✓ 已{'确认' if answer == 'y' else '取消'}更新")
            return True
        except Exception as e:
            return False
def _parse_single_account_env(extra: dict) -> dict:
    def _csv_env(key: str, fallback_key: str) -> list:
        raw = os.getenv(key)
        return _csv_list(raw) if raw else _csv_list(extra.get(fallback_key, []))
    return {
        "ws_url": os.getenv("ONEBOT_WS_URL") or extra.get("ws_url", ""),
        "access_token": os.getenv("ONEBOT_ACCESS_TOKEN") or extra.get("access_token", ""),
        "ws_mode": os.getenv("ONEBOT_WS_MODE") or extra.get("ws_mode", "forward"),
        "allowed_users": _csv_env("ONEBOT_ALLOWED_USERS", "allowed_users"),
        "group_ids": _csv_env("ONEBOT_GROUP_IDS", "group_ids"),
        "allow_all": _truthy(os.getenv("ONEBOT_ALLOW_ALL_USERS"), _truthy(extra.get("allow_all"), False)),
        "home_channel": os.getenv("ONEBOT_HOME_CHANNEL") or str(extra.get("home_channel", "")),
        "admin_qq": os.getenv("ONEBOT_ADMIN_QQ") or str(extra.get("admin_qq", "") or ""),
        "http_api_url": os.getenv("ONEBOT_HTTP_API_URL") or str(extra.get("http_api_url", "")),
    }
class OneBotAdapter(SettingsMixin, ConnectionMixin, MessageMixin, CommandMixin, SendMixin, ApprovalMixin, BasePlatformAdapter):
    SUPPORTS_MESSAGE_EDITING = True
    def __init__(self, config, **kwargs):
        platform = Platform("onebot")
        super().__init__(config=config, platform=platform)
        extra = getattr(config, "extra", {}) or {}
        self._init_connections(extra)
        self._init_shared_state(extra, kwargs)
        self._settings_path = kwargs.get("settings_path", DATA_DIR / "settings.json")
        self._plugin_settings = kwargs.get("settings")
        self._settings_loaded = self._plugin_settings is not None
        if self._settings_loaded:
            self._apply_persisted_settings()
        self._commands: Dict[str, _CmdDef] = {}
        self._register_commands()
    def _init_connections(self, extra: dict):
        accounts_cfg = extra.get("accounts", [])
        self._connections: Dict[str, _NapCatConnection] = {}
        self._multi_account: bool = False
        if isinstance(accounts_cfg, list) and accounts_cfg:
            self._multi_account = True
            for acct in accounts_cfg:
                name = str(acct.get("name", "default")).strip()
                if not name:
                    continue
                conn = _NapCatConnection(
                    name=name, ws_url=acct.get("ws_url", ""),
                    access_token=acct.get("access_token", ""),
                    ws_mode=acct.get("ws_mode", "forward"),
                    allowed_users=[str(u) for u in acct.get("allowed_users", [])],
                    group_ids=[str(g) for g in acct.get("group_ids", [])],
                    home_channel=str(acct.get("home_channel", "")),
                    allow_all=_truthy(acct.get("allow_all"), False),
                    admin_qq=str(acct.get("admin_qq", "")).strip(),
                    http_api_url=str(acct.get("http_api_url", "")).strip(),
                )
                self._connections[name] = conn
        if not self._connections:
            p = _parse_single_account_env(extra)
            conn = _NapCatConnection(
                name="default", ws_url=p["ws_url"], access_token=p["access_token"],
                ws_mode=p["ws_mode"], allowed_users=p["allowed_users"], group_ids=p["group_ids"],
                home_channel=p["home_channel"], allow_all=p["allow_all"], admin_qq=p["admin_qq"],
                http_api_url=p["http_api_url"],
            )
            self._connections["default"] = conn
        self._default_conn: _NapCatConnection = next(iter(self._connections.values()))
    def _init_shared_state(self, extra: dict, kwargs: dict):
        self._http_client = kwargs.get("http_client")
        self._show_qq_id: bool = bool(extra.get("show_qq_id", False))
        self._settings_lock = asyncio.Lock()
        self._chat_msg_seq: Dict[str, int] = {}
        self._msg_receive_seq: Dict[str, int] = {}
        self._last_msg_id: Dict[str, str] = {}
        self._pending_approvals: Dict[str, str] = {}
        self._pending_approval_admin: Dict[str, bool] = {}
        self._approval_locks: Dict[str, asyncio.Lock] = {}
        self._pending_update_chats: Dict[str, float] = {}
        self._unsupported_actions: set = set()
        self._delete_msg_supported: bool = True
        self._last_progress_msg: Dict[str, str] = {}
        self._in_edit_resend_count: Dict[str, int] = {}
        self._bg_delete_tasks: set = set()
        self._active_input_status: Dict[str, bool] = {}
        self._active_tasks: Dict[str, asyncio.Task] = {}
        self._reject_notified: Dict[str, float] = {}
        self._last_seq_cleanup_time: float = 0
        self._media_cache = kwargs.get("media_cache") or _MediaCache(MEDIA_CACHE_DIR)
    @property
    def name(self) -> str:
        return "OneBot"
    @property
    def allowed_users(self) -> List[str]:
        return self._default_conn.allowed_users
    @property
    def is_connected(self) -> bool:
        return any(conn.is_connected for conn in self._connections.values())
    async def get_chat_info(self, chat_id: str) -> Dict[str, Any]:
        msg_type, raw_id = _parse_chat_id(chat_id)
        if msg_type == "group":
            name = f"group_{raw_id}"
            action, params, name_key = "get_group_info", {"group_id": int(raw_id)}, "group_name"
            chat_type = "group"
        else:
            name = f"user_{raw_id}"
            action, params, name_key = "get_stranger_info", {"user_id": int(raw_id)}, "nickname"
            chat_type = "dm"
        conn = self._get_conn_for_chat(chat_id)
        if conn.is_connected:
            try:
                resp = await self._send_action_conn(conn, action, params, timeout=5.0)
                rdata = resp.get("data") or {}
                if rdata.get(name_key):
                    name = rdata[name_key]
            except Exception:
                pass
        return {"name": name, "type": chat_type}
def _config_extra(config) -> dict:
    if isinstance(config, dict):
        return config.get("extra", {}) or {}
    return getattr(config, "extra", {}) or {}

def check_requirements() -> bool:
    return WEBSOCKETS_AVAILABLE
def validate_config(config) -> bool:
    extra = _config_extra(config)
    accounts = extra.get("accounts", [])
    if isinstance(accounts, list) and accounts:
        for i, acct in enumerate(accounts):
            ws_url = acct.get("ws_url", "")
            if not ws_url:
                return False
            if not ws_url.startswith(("ws://", "wss://")):
                return False
        return True
    ws_url = os.getenv("ONEBOT_WS_URL") or extra.get("ws_url", "")
    if not ws_url:
        return False
    if not ws_url.startswith(("ws://", "wss://")):
        return False
    return True
def is_configured(config) -> bool:
    extra = _config_extra(config)
    accounts = extra.get("accounts", [])
    if isinstance(accounts, list) and accounts:
        return True
    ws_url = os.getenv("ONEBOT_WS_URL") or extra.get("ws_url", "")
    return bool(ws_url)
def _env_enablement() -> Optional[dict]:
    if not (ws_url := os.getenv("ONEBOT_WS_URL")):
        return None
    extra = {"ws_url": ws_url}
    for env_name, key in (
        ("ONEBOT_ACCESS_TOKEN", "access_token"),
        ("ONEBOT_WS_MODE", "ws_mode"),
        ("ONEBOT_HTTP_API_URL", "http_api_url"),
        ("ONEBOT_ADMIN_QQ", "admin_qq"),
    ):
        if val := os.getenv(env_name):
            extra[key] = val
    if vals := _csv_list(os.getenv("ONEBOT_ALLOWED_USERS")):
        extra["allowed_users"] = vals
    if vals := _csv_list(os.getenv("ONEBOT_GROUP_IDS")):
        extra["group_ids"] = vals
    if os.getenv("ONEBOT_ALLOW_ALL_USERS") is not None:
        extra["allow_all"] = _truthy(os.getenv("ONEBOT_ALLOW_ALL_USERS"))
    result = {"extra": extra}
    if home_channel := os.getenv("ONEBOT_HOME_CHANNEL"):
        result["home_channel"] = {"chat_id": home_channel}
    return result
def _apply_yaml_config(yaml_cfg: dict, platform_cfg: dict) -> dict:
    """Bridge config.yaml onebot settings into env/extra for Hermes gateway."""
    extra = dict((platform_cfg or {}).get("extra") or {})
    # Accept both gateway.platforms.onebot and top-level onebot blocks.
    top = yaml_cfg.get("onebot") if isinstance(yaml_cfg, dict) else None
    if isinstance(top, dict):
        extra = {**top.get("extra", {}), **extra}
    mapping = {
        "ws_url": "ONEBOT_WS_URL",
        "access_token": "ONEBOT_ACCESS_TOKEN",
        "ws_mode": "ONEBOT_WS_MODE",
        "http_api_url": "ONEBOT_HTTP_API_URL",
        "home_channel": "ONEBOT_HOME_CHANNEL",
        "admin_qq": "ONEBOT_ADMIN_QQ",
    }
    for key, env_name in mapping.items():
        if extra.get(key) not in (None, "") and not os.getenv(env_name):
            os.environ[env_name] = str(extra[key])
    for key, env_name in (("allowed_users", "ONEBOT_ALLOWED_USERS"), ("group_ids", "ONEBOT_GROUP_IDS")):
        vals = _csv_list(extra.get(key))
        if vals and not os.getenv(env_name):
            os.environ[env_name] = ",".join(vals)
    if "allow_all" in extra and not os.getenv("ONEBOT_ALLOW_ALL_USERS"):
        os.environ["ONEBOT_ALLOW_ALL_USERS"] = "true" if _truthy(extra.get("allow_all")) else "false"
    return extra

async def _standalone_send(
    platform: str, chat_id: str, message: str, config: Any = None, **kwargs,
) -> dict:
    if not WEBSOCKETS_AVAILABLE:
        return {"success": False, "error": "websockets package not installed"}
    extra = (config.extra or {}) if config and hasattr(config, "extra") else {}
    ws_url, token = "", ""
    accounts = extra.get("accounts", [])
    if isinstance(accounts, list) and accounts:
        account_name = _extract_account_from_chat_id(chat_id)
        match = next((a for a in accounts if a.get("name") == account_name), accounts[0])
        ws_url = match.get("ws_url", "")
        token = match.get("access_token", "")
    if not ws_url:
        ws_url = os.getenv("ONEBOT_WS_URL", "") or extra.get("ws_url", "")
        token = token or os.getenv("ONEBOT_ACCESS_TOKEN", "") or extra.get("access_token", "")
    if not ws_url:
        return {"success": False, "error": "ONEBOT_WS_URL not set"}
    msg_kind, target_id = _parse_chat_id(chat_id)
    tid = _safe_target_id(target_id)
    if isinstance(tid, SendResult):
        return {"success": False, "error": tid.error}
    headers = {"Authorization": f"Bearer {token}"} if token else None
    echo = str(uuid.uuid4())
    id_key = "group_id" if msg_kind == "group" else "user_id"
    action = f"send_{msg_kind}_msg"
    payload = {"action": action, "params": {id_key: tid, "message": [{"type": "text", "data": {"text": message}}]}, "echo": echo}
    try:
        async with websockets.connect(ws_url, additional_headers=headers, open_timeout=15, **_WS_CONNECT_KWARGS) as ws:
            await ws.send(json.dumps(payload))
            for _ in range(5):
                data = json.loads(await asyncio.wait_for(ws.recv(), timeout=10.0))
                if data.get("echo") == echo:
                    return {"success": data.get("retcode") == 0}
            return {"success": False, "error": "no matching echo response"}
    except Exception as e:
        return {"success": False, "error": str(e)}
def interactive_setup() -> dict:
    print("\n=== OneBot (NapCat) Setup ===")
    print("  Forward WS: Hermes connects to NapCat's WS server")
    print("  Reverse WS: NapCat connects to Hermes' WS server\n")
    mode = input("Mode [forward/reverse] (default: forward): ").strip().lower()
    if not mode:
        mode = "forward"
    if mode == "forward":
        ws_url = input("NapCat WebSocket URL [ws://127.0.0.1:3001]: ").strip()
        if not ws_url:
            ws_url = "ws://127.0.0.1:3001"
    else:
        ws_url = input("Listen address [ws://0.0.0.0:8082]: ").strip()
        if not ws_url:
            ws_url = "ws://0.0.0.0:8082"
    token = input("Access token (leave empty if none): ").strip()
    allowed = input("Allowed QQ numbers (comma-separated, empty for all): ").strip()
    groups = input("Group IDs to listen (comma-separated, empty for all): ").strip()
    env_vars = {
        "ONEBOT_WS_URL": ws_url,
        "ONEBOT_WS_MODE": mode,
    }
    if token:
        env_vars["ONEBOT_ACCESS_TOKEN"] = token
    if allowed:
        env_vars["ONEBOT_ALLOWED_USERS"] = allowed
    if groups:
        env_vars["ONEBOT_GROUP_IDS"] = groups
    return env_vars
