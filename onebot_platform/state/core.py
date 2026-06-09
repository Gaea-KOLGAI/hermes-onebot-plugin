from __future__ import annotations

import asyncio
import json
import logging
import mimetypes
import os
import shutil
import tempfile
import time
import uuid
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional
from urllib.parse import urlparse, unquote as url_unquote
import hashlib

logger = logging.getLogger(__name__)


class DedupCache:
    def __init__(self, ttl: float, max_size: int):
        try:
            ttl_val = float(ttl)
        except (TypeError, ValueError):
            ttl_val = 60.0
        try:
            max_size_val = int(max_size or 1)
        except (TypeError, ValueError):
            max_size_val = 1
        self._ttl = max(0.0, ttl_val)
        self._max_size = max(1, max_size_val)
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
            for key in expired:
                self._cache.pop(key, None)
        while len(self._cache) >= self._max_size:
            self._cache.popitem(last=False)
        self._cache[dedup_key] = (True, now)
        return False


class RateLimiter:
    def __init__(self, rate: float, burst: int):
        try:
            rate_val = float(rate)
        except (TypeError, ValueError):
            rate_val = 1.0
        try:
            burst_val = float(burst)
        except (TypeError, ValueError):
            burst_val = 1.0
        self._rate = max(0.001, rate_val)
        self._burst = max(1.0, burst_val)
        self._tokens = self._burst
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
        try:
            ttl_val = float(ttl)
        except (TypeError, ValueError):
            ttl_val = 300.0
        self._ttl = max(0.0, ttl_val)
        self._max_size = 5000
        self._cache: OrderedDict[str, Dict[str, Any]] = OrderedDict()

    def get(self, group_id: str, user_id: str) -> Optional[Dict]:
        key = f"{group_id}_{user_id}"
        entry = self._cache.get(key)
        if entry and time.time() - entry["_ts"] < self._ttl:
            self._cache.move_to_end(key)
            return entry
        if entry:
            self._cache.pop(key, None)
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

    def __init__(
        self,
        cache_dir: Path,
        max_files: int = 500,
        max_file_size: int = 20 * 1024 * 1024,
        *,
        httpx_available: bool,
        is_safe_media_download_url,
        guess_ext_from_url,
    ):
        self._dir = cache_dir
        self._max_files = max_files
        self._max_size = max_file_size
        self._httpx_available = httpx_available
        self._is_safe_media_download_url = is_safe_media_download_url
        self._guess_ext_from_url = guess_ext_from_url
        cache_dir.mkdir(parents=True, exist_ok=True)

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

    def prepare_outbound_local_file(self, path_or_uri: str) -> Optional[str]:
        raw = str(path_or_uri or "").strip()
        if not raw or raw.startswith(("http://", "https://")):
            return raw or None
        if raw.startswith("file://"):
            raw = url_unquote(urlparse(raw).path)
        try:
            src = Path(os.path.expanduser(raw)).resolve()
        except (OSError, RuntimeError):
            return None
        if not src.is_file() or src.is_symlink():
            return None
        cache_dir = self._dir.resolve()
        if src == cache_dir or cache_dir in src.parents:
            return str(src)
        try:
            src.relative_to(cache_dir)
            return str(src)
        except ValueError:
            pass
        suffix = src.suffix or mimetypes.guess_extension(mimetypes.guess_type(str(src))[0] or "") or ".bin"
        digest = hashlib.sha256(str(src).encode("utf-8", "ignore") + b"\0" + str(src.stat().st_mtime_ns).encode()).hexdigest()[:16]
        dest = cache_dir / f"outbound-{digest}{suffix}"
        try:
            if not dest.exists() or dest.stat().st_size != src.stat().st_size:
                shutil.copy2(src, dest)
            os.chmod(dest, 0o644)
            return str(dest)
        except OSError as exc:
            logger.warning("Failed to stage outbound OneBot attachment %s: %s", src, exc)
            return None

    async def download(self, url: str, http_client, media_type: str = "image") -> Optional[str]:
        if not url or not self._httpx_available:
            return None
        if url.startswith("file://") or url.startswith("/"):
            return self._validate_local_path(url)
        if not self._is_safe_media_download_url(url):
            return None
        import httpx

        own_client = False
        client = None
        tmp_path: Optional[Path] = None
        try:
            client = http_client
            own_client = not bool(http_client)
            if not client:
                client = httpx.AsyncClient(timeout=30.0, follow_redirects=False)
            ext = self._guess_ext_from_url(url)
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
            if total <= 0:
                tmp_path.unlink(missing_ok=True)
                return None
            tmp_path.replace(filepath)
            await asyncio.to_thread(self._cleanup_subdir, cache_subdir)
            return str(filepath)
        except Exception:
            if tmp_path is not None:
                tmp_path.unlink(missing_ok=True)
            return None
        finally:
            if own_client and client is not None:
                await client.aclose()

    def _cleanup_subdir(self, cache_subdir: Path):
        try:
            all_files = sorted(
                (cache_subdir / filename for filename in os.listdir(cache_subdir) if os.path.isfile(cache_subdir / filename)),
                key=lambda path: os.path.getmtime(path),
            )
            if len(all_files) > self._max_files:
                overflow = len(all_files) - self._max_files
                for old_file in all_files[:overflow]:
                    old_file.unlink(missing_ok=True)
        except OSError:
            pass


class _NapCatConnection:
    def __init__(
        self,
        name: str,
        ws_url: str,
        access_token: str = "",
        ws_mode: str = "forward",
        allowed_users: List[str] = None,
        group_ids: List[str] = None,
        home_channel: str = "",
        allow_all: bool = False,
        admin_qq: str = "",
        http_api_url: str = "",
        *,
        dedup_ttl: float,
        dedup_max_size: int,
        rate_limit_messages_per_second: float,
        rate_limit_burst: int,
    ):
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
        self.dedup = DedupCache(dedup_ttl, dedup_max_size)
        self.rate_limiter = RateLimiter(rate_limit_messages_per_second, rate_limit_burst)
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

    def is_group_wake_triggered(self, raw_message: Any, text: str, segments: List[Dict], extract_at) -> bool:
        return bool(self.self_id and self.self_id in extract_at(segments))


class _PluginSettings:
    def __init__(self, path: Path):
        self._path = path
        self._bak_path = path.with_suffix(".json.bak")
        self._data: dict = {}
        self._lock = asyncio.Lock()

    def load(self) -> dict:
        try:
            if self._path.exists():
                with open(self._path, "r", encoding="utf-8") as handle:
                    self._data = json.load(handle)
        except Exception:
            self._data = {}
        return self._data

    async def save(self):
        async with self._lock:
            try:
                if self._path.exists():
                    shutil.copy2(str(self._path), str(self._bak_path))
                data = json.dumps(self._data, ensure_ascii=False, indent=2)
                await asyncio.to_thread(self._path.write_text, data, encoding="utf-8")
            except Exception as exc:
                logger.warning("Failed to save plugin settings: %s", exc)

    @property
    def data(self) -> dict:
        return self._data

    def _normalize_key(self, chat_id: str) -> str:
        return str(chat_id)

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
