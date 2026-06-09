from __future__ import annotations

import onebot_platform.adapter_runtime as _runtime
globals().update({k: v for k, v in vars(_runtime).items() if not k.startswith('__')})
from onebot_platform.outbound import deletion as _deletion
from onebot_platform.outbound import media as _media
from onebot_platform.outbound import notices as _notices

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
            if result.get("status") == "failed" or (result.get("retcode") is not None and result.get("retcode") not in (0, 200)):
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
            if conn.http_api_url:
                logger.debug("WS send failed for %s; trying HTTP fallback: %s", action, e)
                return await self._http_call_conn(conn, action, params)
            return {"status": "failed", "retcode": -1, "msg": str(e)}
        finally:
            self._cleanup_echo(conn, echo)
    async def _send_reply_async_conn(self, conn: _NapCatConnection, data: dict, text: str):
        msg_type = data.get("message_type", "")
        msg_kind = "group" if msg_type == "group" else "private"
        target_id = str(data.get("group_id" if msg_kind == "group" else "user_id", ""))
        try:
            action, params = self._send_msg_params(msg_kind, target_id, [{"type": "text", "data": {"text": text}}])
            result = await self._send_action_conn(conn, action, params)
        except (ValueError, TypeError) as e:
            return
        if result.get("retcode") not in (0, 200):
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
                return _read_bounded_json_response(resp)
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
    def _mention_segments_for_metadata(self, chat_id: str, metadata: Optional[Dict[str, Any]]) -> List[dict]:
        if not isinstance(metadata, dict):
            return []
        msg_kind, _target_id = _parse_chat_id(chat_id)
        if msg_kind != "group":
            return []
        user_id = str(metadata.get("mention_user_id") or metadata.get("mention_originator_user_id") or "").strip()
        if not user_id or not user_id.isdigit():
            return []
        return [
            {"type": "at", "data": {"qq": user_id}},
            {"type": "text", "data": {"text": " "}},
        ]
    def _with_metadata_mention(self, chat_id: str, metadata: Optional[Dict[str, Any]], *segments: dict) -> List[dict]:
        mention = self._mention_segments_for_metadata(chat_id, metadata)
        if not mention:
            return list(segments)
        if segments and segments[0].get("type") == "at" and str(segments[0].get("data", {}).get("qq", "")) == mention[0]["data"]["qq"]:
            return list(segments)
        return [*mention, *segments]
    async def _send_chat_segments(self, chat_id: str, segments: List[dict], timeout: float = 15.0) -> dict:
        conn = self._get_conn_for_chat(chat_id)
        msg_kind, target_id = _parse_chat_id(chat_id)
        action, params = self._send_msg_params(msg_kind, target_id, segments)
        return await self._send_action_conn(conn, action, params, timeout=timeout)
    _media_extension = _media.media_extension
    _classify_media_path = _media.classify_media_path
    _outbound_media_key = _media.outbound_media_key
    _prune_recent_outbound_media = _media.prune_recent_outbound_media
    _is_recent_outbound_media = _media.is_recent_outbound_media
    _mark_outbound_media_once = _media.mark_outbound_media_once
    _as_onebot_file_value = _media.as_onebot_file_value
    _send_media_path = _media.send_media_path
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
        # Tool-progress visibility is resolved by the Hermes gateway layer
        # (display.platforms.onebot.tool_progress).  Keep the adapter as a
        # transport only; do not apply a second plugin-local prompt filter.
        media_files, cleaned_content = self.extract_media(content or "")
        media_files = self.filter_media_delivery_paths(media_files)
        if media_files:
            as_document = "[[as_document]]" in (content or "")
            if settings.get("strip_markdown", True):
                cleaned_content = self.format_message(cleaned_content)
            caption = cleaned_content.strip() or None
            last_result = SendResult(success=True)
            for idx, (media_path, is_voice) in enumerate(media_files):
                result = await self._send_media_path(
                    chat_id, media_path,
                    caption=caption if idx == 0 else None,
                    reply_to=reply_to if idx == 0 else None,
                    metadata=metadata, force_voice=is_voice, as_document=as_document,
                )
                if not result.success:
                    return result
                last_result = result
            return last_result
        if settings.get("strip_markdown", True):
            content = self.format_message(content or "")
        if not content:
            return SendResult(success=True)
        message_segments = self._message_with_optional_reply(
            chat_id, reply_to, *self._with_metadata_mention(chat_id, metadata, {"type": "text", "data": {"text": content}})
        )
        result = await self._send_chat_segments(chat_id, message_segments)
        if result.get("retcode") not in (0, 200):
            logger.debug("Send failed: retcode=%s", result.get("retcode"))
        return _result_to_send_result(result, "send", extract_msg_id=True)
    async def _send_file_segment(
        self, chat_id: str, path_or_url: str, seg_type: str, caption: Optional[str] = None,
        reply_to: Optional[str] = None, timeout: float = 30.0, metadata: Optional[Dict[str, Any]] = None,
    ) -> SendResult:
        try:
            file_value = self._as_onebot_file_value(path_or_url)
        except ValueError as e:
            return SendResult(success=False, error=str(e))
        return await self._send_media(chat_id, seg_type, file_value, caption, reply_to, timeout=timeout, metadata=metadata)
    async def send_image(
        self, chat_id: str, image_url: str, caption: Optional[str] = None,
        reply_to: Optional[str] = None, metadata: Optional[Dict[str, Any]] = None,
    ) -> SendResult:
        return await self._send_file_segment(chat_id, image_url, "image", caption, reply_to, metadata=metadata)
    async def send_animation(
        self, chat_id: str, animation_url: str, caption: Optional[str] = None,
        reply_to: Optional[str] = None, metadata: Optional[Dict[str, Any]] = None,
    ) -> SendResult:
        return await self.send_image(chat_id, animation_url, caption=caption, reply_to=reply_to, metadata=metadata)
    async def send_image_file(
        self, chat_id: str, image_path: str, caption: Optional[str] = None,
        reply_to: Optional[str] = None, metadata: Optional[Dict[str, Any]] = None, **kwargs,
    ) -> SendResult:
        return await self._send_file_segment(chat_id, image_path, "image", caption, reply_to, metadata=metadata)
    async def send_voice(
        self, chat_id: str, audio_path: str, caption: Optional[str] = None,
        reply_to: Optional[str] = None, metadata: Optional[Dict[str, Any]] = None, **kwargs,
    ) -> SendResult:
        return await self._send_file_segment(chat_id, audio_path, "record", caption, reply_to, metadata=metadata)
    async def send_video(
        self, chat_id: str, video_path: str, caption: Optional[str] = None,
        reply_to: Optional[str] = None, metadata: Optional[Dict[str, Any]] = None, **kwargs,
    ) -> SendResult:
        return await self._send_file_segment(chat_id, video_path, "video", caption, reply_to, timeout=60.0, metadata=metadata)
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
        raw_path = str(file_path).strip()
        if not raw_path:
            return SendResult(success=False, error="empty outbound file path")
        is_remote_url = raw_path.startswith(("http://", "https://"))
        local_path = ""
        if raw_path.startswith("file://"):
            local_path = url_unquote(urlparse(raw_path).path)
        elif not is_remote_url:
            local_path = str(Path(os.path.expanduser(raw_path)).resolve())
            if not os.path.isfile(local_path):
                return SendResult(success=False, error=f"file not found: {raw_path}")
        name_source = local_path or urlparse(raw_path).path or raw_path
        name = file_name or os.path.basename(name_source) or "file"
        try:
            file_uri = raw_path if is_remote_url else self._as_onebot_file_value(raw_path)
        except ValueError as e:
            return SendResult(success=False, error=str(e))
        tid = _safe_target_id(target_id)
        if isinstance(tid, SendResult):
            return tid
        if caption:
            await self.send(chat_id, caption, reply_to=reply_to, metadata=metadata)
            reply_to = None
        action = "upload_group_file" if msg_kind == "group" else "upload_private_file"
        params = {_onebot_target_key(msg_kind): tid, "file": file_uri, "name": name}
        result = await self._send_action_conn(conn, action, params, timeout=60.0)
        sr = _result_to_send_result(result, "send_document")
        if sr.success:
            return sr
        # Some OneBot implementations do not support upload_private_file or URL uploads.
        # Degrade clearly instead of silently dropping the attachment.
        if raw_path.startswith(("http://", "https://")):
            return await self.send(chat_id, f"文件链接: {raw_path}", reply_to=reply_to, metadata=metadata)
        return sr
    _notice_sender_name = _notices.notice_sender_name
    _dispatch_notice_text = _notices.dispatch_notice_text
    _handle_group_upload_notice = _notices.handle_group_upload_notice
    _handle_notice = _notices.handle_notice
    async def _handle_request(self, data: dict, conn: _NapCatConnection) -> None:
        return
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
        sr = _result_to_send_result(result, "set_input_status")
        if sr.success:
            if event_type:
                self._active_input_status[chat_id] = True
            else:
                self._active_input_status.pop(chat_id, None)
        return sr
    async def clear_input_status(self, chat_id: str) -> None:
        if self._active_input_status.get(chat_id):
            await self.set_input_status(chat_id, event_type=0)
    async def send_typing(self, chat_id: str, metadata=None) -> None:
        try:
            await self.set_input_status(chat_id, event_type=1)
        except Exception:
            pass
    format_message = staticmethod(_format_message)
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
    _fire_and_forget_delete = _deletion.fire_and_forget_delete
    _bg_delete = _deletion.bg_delete
    _delete_message_with_status = _deletion.delete_message_with_status
    _send_media = _media.send_media
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
            content = msg.get("segments") if "segments" in msg else msg.get("content", "")
            segs = content if isinstance(content, list) else [{"type": "text", "data": {"text": str(content)}}]
            sid = msg.get("sender_id") or msg.get("user_id") or msg.get("uin") or "10000"
            nodes.append({"type": "node", "data": {
                "nickname": msg.get("sender_name") or msg.get("name") or msg.get("nickname") or "匿名",
                "user_id": int(sid) if str(sid).isdigit() else 10000,
                "content": segs,
            }})
        if not nodes:
            return SendResult(success=False, error="No messages to forward")
        action = f"send_{msg_kind}_forward_msg"
        result = await self._send_action_conn(conn, action, {_onebot_target_key(msg_kind): tid, "messages": nodes}, timeout=30.0)
        if result.get("retcode") in (0, 200):
            d = result.get("data", {})
            return SendResult(success=True, message_id=str(d.get("message_id", "") or d.get("forward_id", "")))
        return SendResult(success=False, error=result.get("msg", "send_forward_message failed"))
