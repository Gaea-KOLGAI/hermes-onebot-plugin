from __future__ import annotations

import onebot_platform.adapter_runtime as _runtime
globals().update({k: v for k, v in vars(_runtime).items() if not k.startswith('__')})


def media_extension(self, path: str) -> str:
    parsed_path = urlparse(path).path if "://" in str(path) else str(path)
    return os.path.splitext(parsed_path)[1].lower()


def classify_media_path(self, path: str, *, force_voice: bool = False, as_document: bool = False) -> str:
    if as_document:
        return "document"
    mime, _ = mimetypes.guess_type(path)
    ext = media_extension(self, path)
    if force_voice:
        return "voice"
    for kind, exts in _MEDIA_KIND_EXTS.items():
        if ext in exts or (mime and mime.startswith(f"{kind}/")):
            return kind
    return "document"


def outbound_media_key(self, chat_id: str, seg_type: str, file_val: str) -> tuple:
    raw = str(file_val or "").strip()
    if raw.startswith("file://"):
        try:
            raw = str(Path(url_unquote(urlparse(raw).path)).resolve())
        except Exception:
            pass
    elif not raw.startswith(("http://", "https://")):
        try:
            raw = str(Path(os.path.expanduser(raw)).resolve())
        except Exception:
            pass
    return (str(chat_id), str(seg_type), raw)


def prune_recent_outbound_media(self, ttl: float = 20.0) -> dict:
    now = time.monotonic()
    recent = getattr(self, "_recent_outbound_media", None)
    if recent is None:
        recent = {}
        self._recent_outbound_media = recent
    cutoff = now - ttl
    for key, ts in list(recent.items()):
        if ts < cutoff:
            recent.pop(key, None)
    return recent


def is_recent_outbound_media(self, chat_id: str, seg_type: str, file_val: str, ttl: float = 20.0) -> bool:
    recent = prune_recent_outbound_media(self, ttl)
    key = outbound_media_key(self, chat_id, seg_type, file_val)
    if key in recent:
        logger.debug("Skipping duplicate outbound media send for %s", key)
        return True
    return False


def mark_outbound_media_once(self, chat_id: str, seg_type: str, file_val: str, ttl: float = 20.0) -> bool:
    recent = prune_recent_outbound_media(self, ttl)
    key = outbound_media_key(self, chat_id, seg_type, file_val)
    if key in recent:
        logger.debug("Skipping duplicate outbound media send for %s", key)
        return False
    recent[key] = time.monotonic()
    return True


def as_onebot_file_value(self, path_or_url: str, *, require_safe_local: bool = True) -> str:
    raw = str(path_or_url).strip()
    if not raw:
        raise ValueError("empty outbound file path")
    if raw.startswith(("http://", "https://")):
        return raw
    if raw.startswith("file://"):
        if require_safe_local and not _is_safe_outbound_local_path(raw):
            raise ValueError("local file path is outside allowed outbound media roots")
        staged = self._media_cache.prepare_outbound_local_file(raw) if require_safe_local else None
        return Path(staged).resolve().as_uri() if staged else raw
    if require_safe_local and not _is_safe_outbound_local_path(raw):
        raise ValueError("local file path is outside allowed outbound media roots")
    if require_safe_local:
        staged = self._media_cache.prepare_outbound_local_file(raw)
        if staged:
            raw = staged
    p = Path(os.path.expanduser(raw)).resolve()
    if require_safe_local and not _is_safe_outbound_local_path(p):
        raise ValueError("local file path is outside allowed outbound media roots")
    try:
        return p.as_uri()
    except ValueError:
        return "file://" + url_quote(str(p))


async def send_media_path(self, chat_id: str, media_path: str, *, caption: Optional[str] = None,
                          reply_to: Optional[str] = None, metadata: Optional[Dict[str, Any]] = None,
                          force_voice: bool = False, as_document: bool = False) -> SendResult:
    kind = classify_media_path(self, media_path, force_voice=force_voice, as_document=as_document)
    senders = {
        "voice": self.send_voice,
        "video": self.send_video,
        "image": self.send_image,
        "document": self.send_document,
    }
    return await senders.get(kind, self.send_document)(
        chat_id, media_path, caption=caption, reply_to=reply_to, metadata=metadata
    )


async def send_media(self, chat_id: str, seg_type: str, file_val: str,
                     caption: str = None, reply_to: str = None, timeout: float = 30.0,
                     metadata: Optional[Dict[str, Any]] = None) -> SendResult:
    if is_recent_outbound_media(self, chat_id, seg_type, file_val):
        return SendResult(success=True)
    conn = self._get_conn_for_chat(chat_id)
    msg_kind, target_id = _parse_chat_id(chat_id)
    segments = self._with_metadata_mention(chat_id, metadata, {"type": seg_type, "data": {"file": file_val}})
    if caption:
        segments.append({"type": "text", "data": {"text": caption}})
    message = self._message_with_optional_reply(chat_id, reply_to, *segments)
    ws = conn.ws
    ws_is_ready = ws is not None and getattr(ws, "close_code", None) is None
    if conn.http_api_url and not ws_is_ready:
        try:
            tid = _safe_target_id(target_id)
            if isinstance(tid, SendResult):
                return tid
            action = f"send_{msg_kind}_msg"
            params = {_onebot_target_key(msg_kind): tid, "message": message}
            result = await self._http_call_conn(conn, action, params)
            retcode = result.get("retcode", -1)
            if retcode in (0, 200):
                data = result.get("data") or {}
                if mark_outbound_media_once(self, chat_id, seg_type, file_val):
                    return SendResult(success=True, message_id=str(data.get("message_id", "")))
                return SendResult(success=True)
        except Exception as e:
            logger.debug("HTTP fallback for media send failed: %s", e)
    action, params = self._send_msg_params(msg_kind, target_id, message)
    result = await self._send_action_conn(conn, action, params, timeout=timeout)
    retcode = result.get("retcode")
    if retcode in (0, 200):
        if mark_outbound_media_once(self, chat_id, seg_type, file_val):
            data = result.get("data") or {}
            return SendResult(success=True, message_id=str(data.get("message_id", "")))
        return SendResult(success=True)
    if retcode == -1:
        msg = result.get("msg", "")
        if "timeout" in msg.lower():
            return SendResult(success=False, error="send timeout; delivery not confirmed", retryable=True)
    return _result_to_send_result(result, f"send_{seg_type}", extract_msg_id=True)
