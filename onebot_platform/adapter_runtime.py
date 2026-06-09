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
from gateway.platform_registry import PlatformEntry, platform_registry

# Register the user-installed plugin platform at import time too. The normal
# plugin loader registers the full entry via ctx.register_platform(), but tests
# and compatibility imports can construct OneBotAdapter directly before that
# entry point runs. Platform._missing_ only creates plugin enum members for
# names already known to platform_registry, so seed a minimal entry first; the
# real plugin registration replaces it at gateway startup.
if not platform_registry.is_registered("onebot"):
    platform_registry.register(PlatformEntry(
        name="onebot",
        label="OneBot (NapCat)",
        adapter_factory=lambda cfg: None,
        check_fn=lambda: True,
        source="plugin",
        plugin_name="onebot-platform",
    ))
Platform("onebot")
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
    _read_bounded_json_response,
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


