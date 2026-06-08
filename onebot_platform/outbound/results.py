from __future__ import annotations

import asyncio
import json
import os
import uuid
from typing import Any, Callable, Optional

from gateway.platforms.base import SendResult


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


def _result_to_send_result(result: dict, action_name: str, extract_msg_id: bool = False) -> SendResult:
    if result.get("retcode") == 0:
        if extract_msg_id:
            msg_id = str(result.get("data", {}).get("message_id", ""))
            return SendResult(success=True, message_id=msg_id)
        return SendResult(success=True)
    err = result.get("msg") or result.get("wording") or f"{action_name} failed"
    return SendResult(success=False, error=err, retryable=result.get("retcode") == -1)


async def _standalone_send(
    config: Any,
    chat_id: str,
    message: str,
    *,
    media_cache_factory: Callable,
    media_cache_dir,
    parse_chat_id: Callable[[str], tuple[str, str]],
    extract_account_from_chat_id: Callable[[str], str],
    guess_media_segment_type: Callable[..., str],
    websockets_available: bool,
    websockets_connect: Callable,
    ws_connect_kwargs: Optional[dict] = None,
    **kwargs,
) -> dict:
    if not websockets_available:
        return {"success": False, "error": "websockets package not installed"}
    extra = (config.extra or {}) if config and hasattr(config, "extra") else {}
    ws_url, token = "", ""
    accounts = extra.get("accounts", [])
    if isinstance(accounts, list) and accounts:
        account_name = extract_account_from_chat_id(chat_id)
        match = next((account for account in accounts if account.get("name") == account_name), accounts[0])
        ws_url = match.get("ws_url", "")
        token = match.get("access_token", "")
    if not ws_url:
        ws_url = os.getenv("ONEBOT_WS_URL", "") or extra.get("ws_url", "")
        token = token or os.getenv("ONEBOT_ACCESS_TOKEN", "") or extra.get("access_token", "")
    if not ws_url:
        return {"success": False, "error": "ONEBOT_WS_URL not set"}
    msg_kind, target_id = parse_chat_id(chat_id)
    target = _safe_target_id(target_id)
    if isinstance(target, SendResult):
        return {"success": False, "error": target.error}
    headers = {"Authorization": f"Bearer {token}"} if token else None
    echo = str(uuid.uuid4())
    id_key = "group_id" if msg_kind == "group" else "user_id"
    action = f"send_{msg_kind}_msg"
    segments = []
    if str(message).strip():
        segments.append({"type": "text", "data": {"text": message}})
    for media_path, is_voice in kwargs.get("media_files") or []:
        staged_path = media_cache_factory(media_cache_dir).prepare_outbound_local_file(media_path)
        if not staged_path or not os.path.exists(staged_path):
            return {"success": False, "error": f"Media file not found or could not be staged: {media_path}"}
        file_uri = f"file://{os.path.abspath(staged_path)}"
        seg_type = guess_media_segment_type(staged_path, is_voice=is_voice)
        segments.append({"type": seg_type, "data": {"file": file_uri}})
    if not segments:
        return {"success": False, "error": "No message or media to send"}
    payload = {"action": action, "params": {id_key: target, "message": segments}, "echo": echo}
    try:
        ws = await websockets_connect(ws_url, headers=headers, timeout=15, open_timeout=15, **(ws_connect_kwargs or {}))
        async with ws:
            await ws.send(json.dumps(payload))
            for _ in range(5):
                data = json.loads(await asyncio.wait_for(ws.recv(), timeout=10.0))
                if data.get("echo") == echo:
                    return {"success": data.get("retcode") == 0}
            return {"success": False, "error": "no matching echo response"}
    except Exception as e:
        return {"success": False, "error": str(e)}
