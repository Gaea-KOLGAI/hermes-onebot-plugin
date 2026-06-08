from __future__ import annotations
import asyncio
import functools
import json
import logging
import mimetypes
import os
import re

import socket
import tempfile
import time
import uuid
import random as _random
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from dataclasses import dataclass
from urllib.parse import urlparse, unquote as url_unquote, quote as url_quote
import hmac
import ipaddress
import urllib.request
import urllib.error
import hashlib
logger = logging.getLogger(__name__)
from gateway.platforms.base import (
    BasePlatformAdapter,
    SendResult,
    MessageEvent,
    MessageType,
)
from gateway.config import Platform
from onebot_platform.config.core import (
    _hermes_onebot_data_dir,
    _hermes_config_path as _config_utils_hermes_config_path,
    _normalise_tool_progress_mode,
    _load_gateway_tool_progress_mode as _config_utils_load_gateway_tool_progress_mode,
    _save_gateway_tool_progress_mode as _config_utils_save_gateway_tool_progress_mode,
    _truthy,
    _csv_list,
    build_runtime_paths,
    _is_ip_blocked as _config_utils_is_ip_blocked,
    _is_safe_media_download_url as _config_utils_is_safe_media_download_url,
    _is_safe_outbound_local_path as _config_utils_is_safe_outbound_local_path,
    _message_fingerprint,
    _guess_media_segment_type as _config_utils_guess_media_segment_type,
    _parse_single_account_env as _config_utils_parse_single_account_env,
    _config_extra,
    _configured_ws_urls,
    validate_config as _config_utils_validate_config,
    is_configured as _config_utils_is_configured,
    _env_enablement as _config_utils_env_enablement,
    _apply_yaml_config as _config_utils_apply_yaml_config,
)
from onebot_platform.parsing.segments import (
    _strip_slash,
    _extract_text_from_message,
    _segments_text,
    _cq_unescape,
    _extract_segments,
    _extract_first,
    _extract_seg_text,
    _extract_images,
    _extract_voice,
    _extract_video,
    _extract_face,
    _extract_reply,
    _extract_at,
    _extract_forward,
    _extract_multimsg_text,
    _extract_json_card,
    _extract_xml,
    _extract_typed_segments,
    _make_chat_id,
    _parse_chat_id,
    _onebot_target_key,
    _extract_account_from_chat_id,
    _guess_ext_from_url,
    strip_markdown,
    _format_message as _message_utils_format_message,
)
from onebot_platform.state.core import (
    DedupCache,
    RateLimiter,
    MemberCache,
    _MediaCache as _StateMediaCache,
    _NapCatConnection as _StateNapCatConnection,
    _PluginSettings,
    _CmdDef,
)
from onebot_platform.outbound.results import (
    _safe_int,
    _safe_target_id,
    _result_to_send_result,
    _standalone_send as _send_utils_standalone_send,
)
try:
    from tools.approval import has_blocking_approval
    _HAS_APPROVAL = True
except ImportError:
    _HAS_APPROVAL = False
try:
    import httpx
    HTTPX_AVAILABLE = True
except ImportError:
    HTTPX_AVAILABLE = False
    httpx = None
from onebot_platform.transport.ws import (
    websockets,
    WEBSOCKETS_AVAILABLE,
    WS_CONNECT_KWARGS as _WS_CONNECT_KWARGS,
    _websockets_connect,
    _ws_authorization,
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
_IMAGE_EXTS = frozenset({".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".tiff"})
_VIDEO_EXTS = frozenset({".mp4", ".mov", ".mkv", ".webm", ".avi", ".flv", ".m4v"})
_VOICE_EXTS = frozenset({".silk", ".amr", ".spx"})
_AUDIO_EXTS = frozenset({".mp3", ".wav", ".ogg", ".opus", ".m4a", ".aac", ".flac"})
_MEDIA_KIND_EXTS = {
    "voice": _AUDIO_EXTS | _VOICE_EXTS | frozenset({".oga"}),
    "video": _VIDEO_EXTS,
    "image": _IMAGE_EXTS,
}
def _hermes_onebot_data_dir() -> Path:
    try:
        from hermes_constants import get_hermes_home
        base = get_hermes_home()
    except Exception:
        base = Path.home() / ".hermes"
    path = base / "plugins" / "onebot-platform"
    path.mkdir(parents=True, exist_ok=True)
    return path
DATA_DIR, MEDIA_CACHE_DIR, OUTBOUND_FILE_ALLOWED_ROOTS = build_runtime_paths()
def _hermes_config_path() -> Path:
    return _config_utils_hermes_config_path()

def _load_gateway_tool_progress_mode(platform_key: str = "onebot") -> str:
    return _config_utils_load_gateway_tool_progress_mode(platform_key, config_path_getter=_hermes_config_path)

def _save_gateway_tool_progress_mode(mode: str, platform_key: str = "onebot") -> None:
    _config_utils_save_gateway_tool_progress_mode(mode, platform_key, config_path_getter=_hermes_config_path)

def _is_ip_blocked(ip_str: str) -> bool:
    return _config_utils_is_ip_blocked(ip_str, SSRF_BLOCKED_NETWORKS)

def _is_safe_media_download_url(url: str) -> bool:
    return _config_utils_is_safe_media_download_url(url, blocked_networks=SSRF_BLOCKED_NETWORKS)

def _is_safe_outbound_local_path(path_or_uri: Any) -> bool:
    return _config_utils_is_safe_outbound_local_path(path_or_uri, allowed_roots=OUTBOUND_FILE_ALLOWED_ROOTS)

def _guess_media_segment_type(path: str, *, is_voice: bool = False) -> str:
    return _config_utils_guess_media_segment_type(path, media_kind_exts=_MEDIA_KIND_EXTS, is_voice=is_voice)
def _parse_single_account_env(extra: dict) -> dict:
    return _config_utils_parse_single_account_env(extra)

class _MediaCache(_StateMediaCache):
    def __init__(self, cache_dir: Path, max_files: int = 500, max_file_size: int = 20 * 1024 * 1024):
        super().__init__(
            cache_dir,
            max_files=max_files,
            max_file_size=max_file_size,
            httpx_available=HTTPX_AVAILABLE,
            is_safe_media_download_url=_is_safe_media_download_url,
            guess_ext_from_url=_guess_ext_from_url,
        )


class _NapCatConnection(_StateNapCatConnection):
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
    ):
        super().__init__(
            name=name,
            ws_url=ws_url,
            access_token=access_token,
            ws_mode=ws_mode,
            allowed_users=allowed_users,
            group_ids=group_ids,
            home_channel=home_channel,
            allow_all=allow_all,
            admin_qq=admin_qq,
            http_api_url=http_api_url,
            dedup_ttl=DEDUP_WINDOW_SECONDS,
            dedup_max_size=DEDUP_MAX_SIZE,
            rate_limit_messages_per_second=RATE_LIMIT_MESSAGES_PER_SECOND,
            rate_limit_burst=RATE_LIMIT_BURST,
        )

    def is_group_wake_triggered(self, raw_message: Any, text: str, segments: List[Dict]) -> bool:
        return bool(self.self_id and self.self_id in _extract_at(segments))


def _format_message(content: str) -> str:
    return _message_utils_format_message(content)


from onebot_platform.state.settings_mixin import SettingsMixin
from onebot_platform.transport.connection_mixin import ConnectionMixin
from onebot_platform.inbound.message_mixin import MessageMixin
from onebot_platform.commands.mixin import CommandMixin
from onebot_platform.outbound.send_mixin import SendMixin
from onebot_platform.gateway_integration.approvals import ApprovalMixin, _APPROVAL_CHOICES, _UPDATE_CHOICES
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
def _onebot_platform_blocks(yaml_cfg: dict) -> List[dict]:
    if not isinstance(yaml_cfg, dict):
        return []
    blocks: List[dict] = []
    nested_platforms = yaml_cfg.get("platforms") if isinstance(yaml_cfg.get("platforms"), dict) else {}
    gateway = yaml_cfg.get("gateway") if isinstance(yaml_cfg.get("gateway"), dict) else {}
    gateway_platforms = gateway.get("platforms") if isinstance(gateway.get("platforms"), dict) else {}
    for block in (yaml_cfg.get("onebot"), nested_platforms.get("onebot"), gateway_platforms.get("onebot")):
        if isinstance(block, dict):
            blocks.append(block)
    return blocks
def _merge_onebot_platform_blocks(yaml_cfg: dict, platform_cfg: dict = None) -> dict:
    merged: dict = {}
    merged_extra: dict = {}
    for block in [*_onebot_platform_blocks(yaml_cfg), platform_cfg or {}]:
        if not isinstance(block, dict):
            continue
        extra = block.get("extra") if isinstance(block.get("extra"), dict) else {}
        merged.update({k: v for k, v in block.items() if k != "extra"})
        merged_extra.update(extra)
    if merged_extra:
        merged["extra"] = merged_extra
    return merged
class OneBotAdapter(SettingsMixin, ConnectionMixin, MessageMixin, CommandMixin, SendMixin, ApprovalMixin, BasePlatformAdapter):
    SUPPORTS_MESSAGE_EDITING = True
    def __init__(self, config, **kwargs):
        platform = Platform("onebot")
        super().__init__(config=config, platform=platform)
        extra = _config_extra(config)
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
        self._recent_outbound_media: Dict[tuple, float] = {}
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
            chat_type = "group"
            try:
                action, params, name_key = "get_group_info", {"group_id": int(raw_id)}, "group_name"
            except (ValueError, TypeError):
                return {"id": chat_id, "name": name, "type": chat_type}
        else:
            name = f"user_{raw_id}"
            chat_type = "dm"
            try:
                action, params, name_key = "get_stranger_info", {"user_id": int(raw_id)}, "nickname"
            except (ValueError, TypeError):
                return {"id": chat_id, "name": name, "type": chat_type}
        conn = self._get_conn_for_chat(chat_id)
        if conn.is_connected:
            try:
                resp = await self._send_action_conn(conn, action, params, timeout=5.0)
                rdata = resp.get("data") or {}
                if rdata.get(name_key):
                    name = rdata[name_key]
            except Exception:
                pass
        return {"id": chat_id, "name": name, "type": chat_type}
def _config_extra(config) -> dict:
    if isinstance(config, dict):
        return config.get("extra", {}) or {}
    return getattr(config, "extra", {}) or {}
def check_requirements() -> bool:
    return WEBSOCKETS_AVAILABLE
def validate_config(config) -> bool:
    return _config_utils_validate_config(config)
def is_configured(config) -> bool:
    return _config_utils_is_configured(config)
def _env_enablement() -> Optional[dict]:
    return _config_utils_env_enablement()
def _apply_yaml_config(yaml_cfg: dict, platform_cfg: dict) -> dict:
    """Bridge config.yaml onebot settings into env/extra for Hermes gateway."""
    return _config_utils_apply_yaml_config(
        yaml_cfg,
        platform_cfg,
        merge_platform_blocks=_merge_onebot_platform_blocks,
    )
async def _standalone_send(
    config: Any, chat_id: str, message: str, **kwargs,
) -> dict:
    return await _send_utils_standalone_send(
        config,
        chat_id,
        message,
        media_cache_factory=_MediaCache,
        media_cache_dir=MEDIA_CACHE_DIR,
        parse_chat_id=_parse_chat_id,
        extract_account_from_chat_id=_extract_account_from_chat_id,
        guess_media_segment_type=_guess_media_segment_type,
        websockets_available=WEBSOCKETS_AVAILABLE,
        websockets_connect=_websockets_connect,
        ws_connect_kwargs=_WS_CONNECT_KWARGS,
        **kwargs,
    )
def interactive_setup() -> dict:
    print("\n=== OneBot (NapCat) Setup ===")
    print("  Forward WS: Hermes connects to NapCat's WS server")
    print("  Reverse WS: NapCat connects to Hermes' WS server\n")
    mode = input("Mode [forward/reverse] (default: forward): ").strip().lower()
    mode = mode or "forward"
    prompt, default = ("NapCat WebSocket URL [ws://127.0.0.1:3001]: ", "ws://127.0.0.1:3001") if mode == "forward" else ("Listen address [ws://0.0.0.0:8082]: ", "ws://0.0.0.0:8082")
    ws_url = input(prompt).strip() or default
    token = input("Access token (leave empty if none): ").strip()
    allowed = input("Allowed QQ numbers (comma-separated, 留空则拒绝所有用户): ").strip()
    groups = input("Group IDs to listen (comma-separated, 留空则拒绝所有群): ").strip()
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
