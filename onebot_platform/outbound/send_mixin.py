from __future__ import annotations

import onebot_platform.adapter_runtime as _runtime
globals().update({k: v for k, v in vars(_runtime).items() if not k.startswith('__')})

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
    def _media_extension(self, path: str) -> str:
        parsed_path = urlparse(path).path if "://" in str(path) else str(path)
        return os.path.splitext(parsed_path)[1].lower()
    def _classify_media_path(self, path: str, *, force_voice: bool = False, as_document: bool = False) -> str:
        if as_document:
            return "document"
        mime, _ = mimetypes.guess_type(path)
        ext = self._media_extension(path)
        if force_voice:
            return "voice"
        for kind, exts in _MEDIA_KIND_EXTS.items():
            if ext in exts or (mime and mime.startswith(f"{kind}/")):
                return kind
        return "document"
    def _outbound_media_key(self, chat_id: str, seg_type: str, file_val: str) -> tuple:
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
    def _mark_outbound_media_once(self, chat_id: str, seg_type: str, file_val: str, ttl: float = 20.0) -> bool:
        now = time.monotonic()
        recent = getattr(self, "_recent_outbound_media", None)
        if recent is None:
            recent = {}
            self._recent_outbound_media = recent
        cutoff = now - ttl
        for key, ts in list(recent.items()):
            if ts < cutoff:
                recent.pop(key, None)
        key = self._outbound_media_key(chat_id, seg_type, file_val)
        if key in recent:
            logger.debug("Skipping duplicate outbound media send for %s", key)
            return False
        recent[key] = now
        return True
    def _as_onebot_file_value(self, path_or_url: str, *, require_safe_local: bool = True) -> str:
        raw = str(path_or_url).strip()
        if raw.startswith(("http://", "https://")):
            return raw
        if require_safe_local:
            staged = self._media_cache.prepare_outbound_local_file(raw)
            if staged:
                raw = staged
        if raw.startswith("file://"):
            if require_safe_local and not _is_safe_outbound_local_path(raw):
                raise ValueError("local file path is outside allowed outbound media roots")
            return raw
        p = Path(os.path.expanduser(raw)).resolve()
        if require_safe_local and not _is_safe_outbound_local_path(p):
            raise ValueError("local file path is outside allowed outbound media roots")
        try:
            return p.as_uri()
        except ValueError:
            return "file://" + url_quote(str(p))
    async def _send_media_path(
        self, chat_id: str, media_path: str, *, caption: Optional[str] = None,
        reply_to: Optional[str] = None, metadata: Optional[Dict[str, Any]] = None,
        force_voice: bool = False, as_document: bool = False,
    ) -> SendResult:
        kind = self._classify_media_path(media_path, force_voice=force_voice, as_document=as_document)
        senders = {
            "voice": self.send_voice,
            "video": self.send_video,
            "image": self.send_image,
            "document": self.send_document,
        }
        return await senders.get(kind, self.send_document)(
            chat_id, media_path, caption=caption, reply_to=reply_to, metadata=metadata
        )
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
        if result.get("retcode") != 0:
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
        is_url = raw_path.startswith(("http://", "https://", "file://"))
        local_path = ""
        if raw_path.startswith("file://"):
            local_path = url_unquote(urlparse(raw_path).path)
        elif not raw_path.startswith(("http://", "https://")):
            local_path = str(Path(os.path.expanduser(raw_path)).resolve())
            if not os.path.isfile(local_path):
                return SendResult(success=False, error=f"file not found: {raw_path}")
        name_source = local_path or urlparse(raw_path).path or raw_path
        name = file_name or os.path.basename(name_source) or "file"
        try:
            trusted_local = bool((metadata or {}).get("trusted_local_file"))
            file_uri = raw_path if is_url else self._as_onebot_file_value(local_path, require_safe_local=not trusted_local)
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
    def _notice_sender_name(self, data: dict) -> str:
        return str(data.get("nickname") or data.get("card") or data.get("user_id") or "system")
    async def _dispatch_notice_text(self, data: dict, conn: _NapCatConnection, text: str, *, media_url: str = "", media_type: str = "") -> None:
        msg_type = "group" if data.get("group_id") else "private"
        user_id = str(data.get("user_id") or data.get("operator_id") or "")
        if user_id and not await self._check_authorization_async(user_id, msg_type, {"message_type": msg_type, **data}, conn):
            return
        chat_id = f"group_{data.get('group_id')}" if msg_type == "group" else f"private_{user_id}"
        if self._multi_account:
            chat_id = f"{conn.name}:{chat_id}"
        source = self.build_source(
            chat_id=chat_id,
            user_id=user_id or str(data.get("self_id") or "system"),
            user_name=self._notice_sender_name(data),
            message_id=str(data.get("message_id") or data.get("file", {}).get("id") or data.get("flag") or ""),
            chat_type="group" if msg_type == "group" else "dm",
        )
        event = MessageEvent(source=source, text=text, message_type=MessageType.TEXT, raw_message=data, message_id=source.message_id)
        if media_url:
            event.media_urls = [media_url]
            event.media_types = [media_type or "file"]
        await self.handle_message(event)
    async def _handle_group_upload_notice(self, data: dict, conn: _NapCatConnection) -> None:
        file_info = data.get("file") or {}
        name = file_info.get("name") or file_info.get("file") or "未知文件"
        size = file_info.get("size")
        file_url = file_info.get("url") or file_info.get("file_url") or ""
        if not file_url:
            file_url = await self._resolve_file_url(file_info, conn)
        seg = {"type": "file", "data": {**file_info, "name": name}}
        if file_url:
            seg["data"]["url"] = file_url
        injected = await self._inject_file_content([seg], "", conn)
        text = f"[群文件上传: {name}"
        if size:
            text += f" size={size}"
        text += "]"
        if injected:
            text += "\n" + injected
        await self._dispatch_notice_text(data, conn, text, media_url=file_url, media_type="file")
    async def _handle_notice(self, data: dict, conn: _NapCatConnection) -> None:
        notice_type = data.get("notice_type", "")
        sub_type = data.get("sub_type", "")
        if notice_type == "group_upload":
            # Group file-upload notices are passive context only. Do not turn
            # them into MessageEvent objects, otherwise every uploaded group
            # file actively wakes Hermes and produces an unsolicited reply.
            return
        if notice_type in {"group_recall", "friend_recall", "group_increase", "group_decrease", "group_ban"}:
            return
        if notice_type == "notify" and sub_type == "poke":
            poker_id = str(data.get("user_id", ""))
            target_id = data.get("target_id", "")
            self_id = data.get("self_id", "")
            if str(target_id) != str(self_id):
                return
            if _HAS_APPROVAL:
                # A poke approval should resolve the pending approval in the
                # chat where the approval prompt was sent.  Group poke notices
                # include group_id, but older code only checked private_<user>,
                # so group approvals could never be accepted by poking.
                candidate_chat_ids = []
                if data.get("group_id"):
                    candidate_chat_ids.append(f"group_{data.get('group_id')}")
                candidate_chat_ids.append(f"private_{poker_id}")
                if self._multi_account:
                    candidate_chat_ids = [f"{conn.name}:{cid}" for cid in candidate_chat_ids] + candidate_chat_ids
                admin_qq = os.getenv("ONEBOT_ADMIN_QQ") or conn.admin_qq or (conn.allowed_users[0] if conn.allowed_users else None)
                for chat_id in candidate_chat_ids:
                    is_admin_approval = self._pending_approval_admin.get(chat_id, False)
                    if is_admin_approval and (not admin_qq or str(poker_id) != str(admin_qq)):
                        continue
                    if chat_id in self._pending_approvals:
                        await self._resolve_approval_shortcut(chat_id, "1", poker_id, admin_qq)
                        return
            await self._dispatch_notice_text(data, conn, f"[戳一戳: {poker_id}]")
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
                          caption: str = None, reply_to: str = None, timeout: float = 30.0,
                          metadata: Optional[Dict[str, Any]] = None) -> SendResult:
        if not self._mark_outbound_media_once(chat_id, seg_type, file_val):
            return SendResult(success=True)
        conn = self._get_conn_for_chat(chat_id)
        msg_kind, target_id = _parse_chat_id(chat_id)
        segments = self._with_metadata_mention(chat_id, metadata, {"type": seg_type, "data": {"file": file_val}})
        if caption:
            segments.append({"type": "text", "data": {"text": caption}})
        message = self._message_with_optional_reply(chat_id, reply_to, *segments)
        if conn.http_api_url:
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
                    return SendResult(success=True, message_id=str(data.get("message_id", "")))
            except Exception as e:
                logger.debug("HTTP fallback for media send failed: %s", e)
        action, params = self._send_msg_params(msg_kind, target_id, message)
        result = await self._send_action_conn(conn, action, params, timeout=timeout)
        retcode = result.get("retcode")
        if retcode == 200:
            return SendResult(success=True)
        if retcode == -1:
            msg = result.get("msg", "")
            if "timeout" in msg.lower():
                return SendResult(success=False, error="send timeout; delivery not confirmed", retryable=True)
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
