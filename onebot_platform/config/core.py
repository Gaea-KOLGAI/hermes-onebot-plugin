from __future__ import annotations

import hashlib
import json
import logging
import os
import socket
import tempfile
from pathlib import Path
from typing import Any, List, Optional
from urllib.parse import urlparse, unquote as url_unquote
import ipaddress

logger = logging.getLogger(__name__)


def _hermes_onebot_data_dir() -> Path:
    try:
        from hermes_constants import get_hermes_home
        base = get_hermes_home()
    except Exception:
        base = Path.home() / ".hermes"
    path = base / "plugins" / "onebot-platform"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _hermes_config_path() -> Path:
    try:
        from hermes_constants import get_hermes_home
        return get_hermes_home() / "config.yaml"
    except Exception:
        return Path.home() / ".hermes" / "config.yaml"


def _normalise_tool_progress_mode(value: Any) -> str:
    if value is False:
        return "off"
    if value is True:
        return "all"
    mode = str(value or "").strip().lower()
    return mode if mode in {"off", "new", "all", "verbose"} else "all"


def _load_gateway_tool_progress_mode(platform_key: str = "onebot", *, config_path_getter=_hermes_config_path) -> str:
    try:
        import yaml
        from gateway.display_config import resolve_display_setting
        config_path = config_path_getter()
        if not config_path.exists():
            return "all"
        user_config = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
        return _normalise_tool_progress_mode(
            resolve_display_setting(user_config, platform_key, "tool_progress", "all")
        )
    except Exception as exc:
        logger.debug("Failed to load gateway tool_progress mode: %s", exc)
    return "all"


def _save_gateway_tool_progress_mode(mode: str, platform_key: str = "onebot", *, config_path_getter=_hermes_config_path) -> None:
    import yaml
    from utils import atomic_yaml_write
    config_path = config_path_getter()
    user_config = {}
    if config_path.exists():
        user_config = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
        if not isinstance(user_config, dict):
            user_config = {}
    display = user_config.setdefault("display", {})
    if not isinstance(display, dict):
        display = user_config["display"] = {}
    platforms = display.setdefault("platforms", {})
    if not isinstance(platforms, dict):
        platforms = display["platforms"] = {}
    platform_display = platforms.setdefault(platform_key, {})
    if not isinstance(platform_display, dict):
        platform_display = platforms[platform_key] = {}
    platform_display["tool_progress"] = _normalise_tool_progress_mode(mode)
    atomic_yaml_write(config_path, user_config)


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


def build_runtime_paths() -> tuple[Path, Path, tuple[Path, Path]]:
    data_dir = _hermes_onebot_data_dir()
    media_cache_dir = (
        Path(os.getenv("ONEBOT_MEDIA_CACHE_DIR", "")).expanduser().resolve()
        if os.getenv("ONEBOT_MEDIA_CACHE_DIR", "").strip()
        else (Path("/var/lib/napcat/hermes-media-cache") if Path("/var/lib/napcat").exists() else data_dir / "media_cache")
    )
    outbound_roots = (media_cache_dir, Path(tempfile.gettempdir()))
    return data_dir, media_cache_dir, outbound_roots


def _is_ip_blocked(ip_str: str, blocked_networks) -> bool:
    try:
        ip = ipaddress.ip_address(ip_str)
    except ValueError:
        return True
    return any(ip in net for net in blocked_networks)


def _is_safe_media_download_url(url: str, *, blocked_networks) -> bool:
    parsed_url = urlparse(str(url or ""))
    if parsed_url.scheme not in {"http", "https"} or not parsed_url.hostname:
        return False
    try:
        addrinfo = socket.getaddrinfo(parsed_url.hostname, None, socket.AF_UNSPEC, socket.SOCK_STREAM)
    except (socket.gaierror, ValueError) as exc:
        logger.warning("SSRF check failed for %s: %s", url, exc)
        return False
    for *_, sockaddr in addrinfo:
        ip_str = sockaddr[0]
        if _is_ip_blocked(ip_str, blocked_networks):
            logger.warning("SSRF blocked: %s resolves to blocked IP %s", url, ip_str)
            return False
    return True


def _is_safe_outbound_local_path(path_or_uri: Any, *, allowed_roots) -> bool:
    raw = str(path_or_uri or "").strip()
    if not raw:
        return False
    if raw.startswith("file://"):
        raw = url_unquote(urlparse(raw).path)
    try:
        resolved = Path(os.path.expanduser(raw)).resolve()
    except (OSError, RuntimeError):
        return False
    if not resolved.is_file():
        return False
    roots = [Path(root).resolve() for root in allowed_roots]
    return any(resolved == root or root in resolved.parents for root in roots)


def _message_fingerprint(message: Any) -> str:
    try:
        payload = json.dumps(message, ensure_ascii=False, sort_keys=True, default=str)
    except TypeError:
        payload = str(message)
    return hashlib.sha256(payload.encode("utf-8", "ignore")).hexdigest()[:16]


def _guess_media_segment_type(path: str, *, media_kind_exts, is_voice: bool = False) -> str:
    if is_voice:
        return "record"
    ext = os.path.splitext(urlparse(str(path)).path if "://" in str(path) else str(path))[1].lower()
    kind_to_segment = {"image": "image", "video": "video", "voice": "record"}
    return next((kind_to_segment[kind] for kind, exts in media_kind_exts.items() if ext in exts), "file")


def _parse_single_account_env(extra: dict, *, csv_list=_csv_list, truthy=_truthy) -> dict:
    extra = extra if isinstance(extra, dict) else {}
    def _csv_env(key: str, fallback_key: str) -> list:
        return csv_list(os.getenv(key)) or csv_list(extra.get(fallback_key))

    return {
        "ws_url": os.getenv("ONEBOT_WS_URL", "") or extra.get("ws_url", ""),
        "access_token": os.getenv("ONEBOT_ACCESS_TOKEN", "") or extra.get("access_token", ""),
        "ws_mode": os.getenv("ONEBOT_WS_MODE", "") or extra.get("ws_mode", "forward"),
        "allowed_users": _csv_env("ONEBOT_ALLOWED_USERS", "allowed_users"),
        "group_ids": _csv_env("ONEBOT_GROUP_IDS", "group_ids"),
        "home_channel": os.getenv("ONEBOT_HOME_CHANNEL", "") or extra.get("home_channel", ""),
        "allow_all": truthy(os.getenv("ONEBOT_ALLOW_ALL_USERS"), truthy(extra.get("allow_all"), False)),
        "admin_qq": os.getenv("ONEBOT_ADMIN_QQ", "") or str(extra.get("admin_qq", "")).strip(),
        "http_api_url": os.getenv("ONEBOT_HTTP_API_URL", "") or str(extra.get("http_api_url", "")).strip(),
    }


def _configured_ws_urls(extra: dict) -> List[str]:
    if not isinstance(extra, dict):
        return []
    accounts = extra.get("accounts", [])
    if isinstance(accounts, list) and accounts:
        return [
            str(acct.get("ws_url", "")).strip()
            for acct in accounts
            if isinstance(acct, dict) and str(acct.get("ws_url", "")).strip()
        ]
    url = str(os.getenv("ONEBOT_WS_URL") or extra.get("ws_url", "")).strip()
    return [url] if url else []


def _config_extra(config) -> dict:
    if isinstance(config, dict):
        extra = config.get("extra", {}) or {}
    else:
        extra = getattr(config, "extra", {}) or {}
    return extra if isinstance(extra, dict) else {}


def validate_config(config) -> bool:
    urls = _configured_ws_urls(_config_extra(config))
    return bool(urls) and all(url.startswith(("ws://", "wss://")) for url in urls)


def is_configured(config) -> bool:
    return bool(_configured_ws_urls(_config_extra(config)))


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
    for env_name, key in (("ONEBOT_ALLOWED_USERS", "allowed_users"), ("ONEBOT_GROUP_IDS", "group_ids")):
        if vals := _csv_list(os.getenv(env_name)):
            extra[key] = vals
    if os.getenv("ONEBOT_ALLOW_ALL_USERS") is not None:
        extra["allow_all"] = _truthy(os.getenv("ONEBOT_ALLOW_ALL_USERS"))
    result = {"extra": extra}
    if home_channel := os.getenv("ONEBOT_HOME_CHANNEL"):
        result["home_channel"] = {"chat_id": home_channel}
    return result


def _apply_yaml_config(yaml_cfg: dict, platform_cfg: dict, *, merge_platform_blocks, csv_list=_csv_list, truthy=_truthy) -> dict:
    effective_cfg = merge_platform_blocks(yaml_cfg, platform_cfg)
    extra = dict(effective_cfg.get("extra") or {})
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
        vals = csv_list(extra.get(key))
        if vals and not os.getenv(env_name):
            os.environ[env_name] = ",".join(vals)
    if "allow_all" in extra and not os.getenv("ONEBOT_ALLOW_ALL_USERS"):
        os.environ["ONEBOT_ALLOW_ALL_USERS"] = "true" if truthy(extra.get("allow_all")) else "false"
    return extra
