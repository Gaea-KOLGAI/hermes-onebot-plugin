from __future__ import annotations

import asyncio
from typing import Any, Dict, Tuple

import onebot_platform.adapter_runtime as _runtime

globals().update({k: v for k, v in vars(_runtime).items() if not k.startswith("__")})

_RUNTIME_MAP_ATTRS = (
    "_chat_msg_seq", "_msg_receive_seq", "_last_msg_id", "_last_msg_user_id",
    "_pending_approvals", "_pending_approval_admin", "_pending_approval_messages", "_approval_locks",
    "_pending_update_chats", "_last_progress_msg", "_in_edit_resend_count",
    "_active_input_status", "_active_tasks", "_reject_notified",
    "_recent_outbound_media",
)


def build_connections(extra: dict) -> Tuple[Dict[str, _NapCatConnection], bool, _NapCatConnection]:
    """Build OneBot account connections without coupling adapter facade to config parsing."""
    accounts_cfg = extra.get("accounts", [])
    connections: Dict[str, _NapCatConnection] = {}
    multi_account = False

    if isinstance(accounts_cfg, list) and accounts_cfg:
        multi_account = True
        seen_names = set()
        for acct in accounts_cfg:
            if not isinstance(acct, dict):
                continue
            name = str(acct.get("name", "default")).strip()
            if not name:
                continue
            if name in seen_names:
                raise ValueError(f"duplicate OneBot account name: {name}")
            seen_names.add(name)
            ws_url = str(acct.get("ws_url", "")).strip()
            if not ws_url.startswith(("ws://", "wss://")):
                logger.warning("Skipping OneBot account %s with invalid ws_url", name)
                continue
            connections[name] = _NapCatConnection(
                name=name,
                ws_url=ws_url,
                access_token=acct.get("access_token", ""),
                ws_mode=acct.get("ws_mode", "forward"),
                allowed_users=[str(u) for u in acct.get("allowed_users", [])],
                group_ids=[str(g) for g in acct.get("group_ids", [])],
                home_channel=str(acct.get("home_channel", "")),
                allow_all=_truthy(acct.get("allow_all"), False),
                admin_qq=str(acct.get("admin_qq", "")).strip(),
                http_api_url=str(acct.get("http_api_url", "")).strip(),
            )

    if not connections:
        parsed = _parse_single_account_env(extra)
        connections["default"] = _NapCatConnection(
            name="default",
            ws_url=parsed["ws_url"],
            access_token=parsed["access_token"],
            ws_mode=parsed["ws_mode"],
            allowed_users=parsed["allowed_users"],
            group_ids=parsed["group_ids"],
            home_channel=parsed["home_channel"],
            allow_all=parsed["allow_all"],
            admin_qq=parsed["admin_qq"],
            http_api_url=parsed["http_api_url"],
        )

    return connections, multi_account, next(iter(connections.values()))


def install_shared_state(target: Any, extra: dict, kwargs: dict) -> None:
    """Install mutable runtime state used across inbound/outbound/command mixins."""
    target._http_client = kwargs.get("http_client")
    target._show_qq_id = bool(extra.get("show_qq_id", False))
    target._settings_lock = asyncio.Lock()
    target._chat_seq_lock = asyncio.Lock()
    for attr in _RUNTIME_MAP_ATTRS:
        setattr(target, attr, {})
    target._unsupported_actions = set()
    target._delete_msg_supported = True
    target._delete_msg_circuit = {"failures": 0, "opened_until": 0.0}
    target._bg_delete_tasks = set()
    target._last_seq_cleanup_time = 0
    target._media_cache = kwargs.get("media_cache") or _MediaCache(MEDIA_CACHE_DIR)
