from __future__ import annotations

import asyncio
import json
import os
import uuid
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import urlparse, unquote as url_unquote
from typing import Any, Callable, Optional

from gateway.platforms.base import SendResult


_MAX_ONEBOT_HTTP_RESPONSE_BYTES = 1024 * 1024


def _read_bounded_json_response(resp, *, max_bytes: int = _MAX_ONEBOT_HTTP_RESPONSE_BYTES) -> dict:
    """Read a OneBot HTTP JSON response with a hard memory cap."""
    chunks = []
    remaining = max_bytes + 1
    while remaining > 0:
        try:
            chunk = resp.read(min(65536, remaining))
        except TypeError:
            chunk = resp.read()
        if not chunk:
            break
        chunks.append(chunk)
        remaining -= len(chunk)
        if remaining <= 0:
            break
        if len(chunk) < min(65536, remaining + len(chunk)):
            break
    raw = b"".join(chunks)
    if len(raw) > max_bytes:
        raise ValueError(f"OneBot HTTP response exceeds {max_bytes} bytes")
    return json.loads(raw.decode())


def _safe_int(val, label: str = "") -> int:
    if isinstance(val, bool):
        raise ValueError(f"Invalid {label or 'value'}: {val!r}")
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
    if result.get("retcode") in (0, 200):
        if extract_msg_id:
            data = result.get("data") or {}
            msg_id = str(data.get("message_id") or data.get("file_id") or data.get("forward_id") or "")
            return SendResult(success=True, message_id=msg_id)
        return SendResult(success=True)
    err = result.get("msg") or result.get("wording") or f"{action_name} failed"
    return SendResult(success=False, error=err, retryable=result.get("retcode") == -1)


def _account_extra(extra: dict, chat_id: str, extract_account_from_chat_id: Callable[[str], str]) -> dict:
    accounts = extra.get("accounts", []) if isinstance(extra, dict) else []
    valid_accounts = [account for account in accounts if isinstance(account, dict)] if isinstance(accounts, list) else []
    if valid_accounts:
        account_name = extract_account_from_chat_id(chat_id)
        return next((account for account in valid_accounts if account.get("name") == account_name), valid_accounts[0]) or {}
    return extra if isinstance(extra, dict) else {}


def _post_onebot_http(http_api_url: str, token: str, action: str, params: dict) -> dict:
    parsed = urlparse(str(http_api_url or ""))
    if parsed.scheme not in {"http", "https"}:
        return {"success": False, "error": "invalid ONEBOT_HTTP_API_URL"}
    payload = json.dumps(params).encode()
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(f"{http_api_url.rstrip('/')}/{action}", data=payload, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            result = _read_bounded_json_response(resp)
    except urllib.error.HTTPError as e:
        return {"success": False, "error": f"HTTP {e.code}: {e.reason}"}
    except Exception as e:
        return {"success": False, "error": str(e)}
    if result.get("retcode") in (0, 200):
        data = result.get("data") or {}
        return {"success": True, "message_id": str(data.get("message_id") or data.get("file_id") or data.get("forward_id") or "")}
    return {"success": False, "error": result.get("msg") or result.get("wording") or f"{action} failed"}


def _file_uri(path: str) -> str:
    raw = str(path or "").strip()
    if not raw:
        raise ValueError("empty standalone file path")
    if raw.startswith(("http://", "https://")):
        return raw
    if raw.startswith("file://"):
        local_path = Path(url_unquote(urlparse(raw).path)).expanduser().resolve()
    else:
        local_path = Path(raw).expanduser().resolve()
    if not str(local_path) or not local_path.is_file():
        raise ValueError(f"local file not found: {raw}")
    return local_path.as_uri()


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
    extra = (config.extra or {}) if config and hasattr(config, "extra") else {}
    extra = extra if isinstance(extra, dict) else {}
    account = _account_extra(extra, chat_id, extract_account_from_chat_id)
    ws_url = account.get("ws_url", "") or os.getenv("ONEBOT_WS_URL", "") or extra.get("ws_url", "")
    token = account.get("access_token", "") or os.getenv("ONEBOT_ACCESS_TOKEN", "") or extra.get("access_token", "")
    http_api_url = account.get("http_api_url", "") or os.getenv("ONEBOT_HTTP_API_URL", "") or extra.get("http_api_url", "")
    msg_kind, target_id = parse_chat_id(chat_id)
    target = _safe_target_id(target_id)
    if isinstance(target, SendResult):
        return {"success": False, "error": target.error}
    id_key = "group_id" if msg_kind == "group" else "user_id"
    send_action = f"send_{msg_kind}_msg"
    media_files = kwargs.get("media_files") or []
    media_cache = media_cache_factory(media_cache_dir) if media_files else None

    if http_api_url:
        last_result = None
        if str(message).strip():
            last_result = await asyncio.to_thread(
                _post_onebot_http,
                http_api_url,
                token,
                send_action,
                {id_key: target, "message": [{"type": "text", "data": {"text": message}}]},
            )
            if not last_result.get("success"):
                return last_result
        for media_path, is_voice in media_files:
            staged_path = media_cache.prepare_outbound_local_file(media_path) if media_cache else None
            if not staged_path or not os.path.exists(staged_path):
                return {"success": False, "error": f"Media file not found or could not be staged: {media_path}"}
            seg_type = guess_media_segment_type(staged_path, is_voice=is_voice)
            file_uri = _file_uri(staged_path)
            original_name_source = url_unquote(urlparse(str(media_path)).path) if str(media_path).startswith("file://") else str(media_path)
            upload_name = os.path.basename(original_name_source) or os.path.basename(staged_path) or "file"
            if kwargs.get("force_document") or seg_type not in {"image", "record", "video"}:
                upload_action = "upload_group_file" if msg_kind == "group" else "upload_private_file"
                params = {id_key: target, "file": file_uri, "name": upload_name}
                last_result = await asyncio.to_thread(_post_onebot_http, http_api_url, token, upload_action, params)
            else:
                last_result = await asyncio.to_thread(
                    _post_onebot_http,
                    http_api_url,
                    token,
                    send_action,
                    {id_key: target, "message": [{"type": seg_type, "data": {"file": file_uri}}]},
                )
            if not last_result.get("success"):
                return last_result
        if last_result is not None:
            return last_result
        return {"success": False, "error": "No message or media to send"}

    if not websockets_available:
        return {"success": False, "error": "websockets package not installed"}
    if not ws_url:
        return {"success": False, "error": "ONEBOT_WS_URL not set"}
    headers = {"Authorization": f"Bearer {token}"} if token else None
    echo = str(uuid.uuid4())
    segments = []
    if str(message).strip():
        segments.append({"type": "text", "data": {"text": message}})
    for media_path, is_voice in media_files:
        staged_path = media_cache.prepare_outbound_local_file(media_path) if media_cache else None
        if not staged_path or not os.path.exists(staged_path):
            return {"success": False, "error": f"Media file not found or could not be staged: {media_path}"}
        file_uri = _file_uri(staged_path)
        seg_type = guess_media_segment_type(staged_path, is_voice=is_voice)
        segments.append({"type": seg_type, "data": {"file": file_uri}})
    if not segments:
        return {"success": False, "error": "No message or media to send"}
    payload = {"action": send_action, "params": {id_key: target, "message": segments}, "echo": echo}
    try:
        ws = await websockets_connect(ws_url, headers=headers, timeout=15, open_timeout=15, **(ws_connect_kwargs or {}))
        async with ws:
            await ws.send(json.dumps(payload))
            for _ in range(5):
                data = json.loads(await asyncio.wait_for(ws.recv(), timeout=10.0))
                if data.get("echo") == echo:
                    ok = data.get("retcode") in (0, 200)
                    result: dict[str, Any] = {"success": ok}
                    if ok:
                        payload_data = data.get("data") or {}
                        message_id = payload_data.get("message_id") or payload_data.get("file_id") or payload_data.get("forward_id")
                        if message_id:
                            result["message_id"] = str(message_id)
                    elif data.get("msg") or data.get("wording"):
                        result["error"] = data.get("msg") or data.get("wording")
                    return result
            return {"success": False, "error": "no matching echo response"}
    except Exception as e:
        return {"success": False, "error": str(e)}
