from __future__ import annotations

import onebot_platform.adapter_runtime as _runtime
globals().update({k: v for k, v in vars(_runtime).items() if not k.startswith('__')})

class MessageMixin:
    async def _handle_message(self, data: dict, conn: Optional[_NapCatConnection] = None):
        if conn is None:
            conn = self._default_conn
        account_name = conn.name if self._multi_account else ""
        msg_type = data.get("message_type", "")
        user_id = str(data.get("user_id", ""))
        raw_message = data.get("message", "")
        message_id = str(data.get("message_id", ""))
        if self._check_duplicate_and_self(data, conn):
            return
        early_chat_id = _make_chat_id(data, account_name)
        self._chat_msg_seq[early_chat_id] = self._chat_msg_seq.get(early_chat_id, 0) + 1
        segments = _extract_segments(raw_message)
        text_for_cmd = _segments_text(segments)
        is_slash_cmd = text_for_cmd.startswith("/")
        if not await self._check_authorization_async(user_id, msg_type, data, conn):
            return
        admin_qq = (getattr(conn, 'admin_qq', None)
                    or os.getenv("ONEBOT_ADMIN_QQ", "").strip()
                    or (conn.allowed_users[0] if conn.allowed_users else None))
        if await self._try_handle_command(data, conn, text_for_cmd, msg_type, user_id, admin_qq):
            return
        if not self._check_wake_trigger(msg_type, is_slash_cmd, text_for_cmd, raw_message, conn, segments):
            return
        parsed = await self._parse_message_segments(data, conn, raw_message, text_for_cmd, segments)
        if parsed is None:
            return
        chat_id = _make_chat_id(data, account_name)
        if parsed["text"] and not parsed["images"] and not parsed["voice_url"]:
            if await self._resolve_approval_shortcut(chat_id, parsed["text"], user_id, admin_qq) or \
               await self._handle_update_shortcut(chat_id, parsed["text"]):
                return
        text = self._strip_at_mentions(parsed["text"], raw_message, conn, msg_type)
        if message_id:
            self._last_msg_id[chat_id] = message_id
        self._msg_receive_seq[message_id] = self._chat_msg_seq.get(chat_id, 0)
        self._cleanup_seq_dictionaries(chat_id)
        sender = data.get("sender", {})
        sender_name = sender.get("card") or sender.get("nickname") or user_id
        if msg_type == "group":
            conn.member_cache.set_from_sender(str(data.get("group_id", "")), user_id, sender)
        if self._show_qq_id and sender_name != user_id:
            sender_name = f"{sender_name}({user_id})"
        display_text, text, quoted_images = await self._build_display_text(
            parsed, text, msg_type, is_slash_cmd, sender_name, conn)
        await self._build_and_dispatch_event(
            parsed, display_text, text, chat_id, user_id, sender_name,
            message_id, msg_type, conn=conn, data=data,
            quoted_images=quoted_images)
    def _check_duplicate_and_self(self, data: dict, conn) -> bool:
        user_id = str(data.get("user_id", ""))
        message_id = str(data.get("message_id", ""))
        msg_part = message_id or _message_fingerprint(data.get("message", data.get("raw_message", "")))
        dedup_key = f"{msg_part}_{user_id}_{data.get('time', '')}"
        if conn.dedup.is_duplicate(dedup_key):
            return True
        if conn.self_id and user_id == conn.self_id:
            return True
        return False
    async def _check_authorization_async(self, user_id: str, msg_type: str, data: dict, conn) -> bool:
        if conn.is_user_authorized(user_id, msg_type, data):
            return True
        if msg_type == "private":
            account_name = conn.name if self._multi_account else ""
            reject_key = _make_chat_id(data, account_name)
            last = self._reject_notified.get(reject_key, 0)
            if time.time() - last > REJECT_NOTIFY_TTL:
                await self._send_reply_async_conn(conn, data, "您好，当前bot仅对白名单用户开放，请联系管理员添加。")
                self._reject_notified[reject_key] = time.time()
        return False
    def _check_wake_trigger(self, msg_type: str, is_slash_cmd: bool,
                            text_for_cmd: str, raw_message, conn,
                            segments: Optional[List[Dict[str, Any]]] = None) -> bool:
        if msg_type == "group" and not is_slash_cmd:
            segments_for_wake = segments if segments is not None else _extract_segments(raw_message)
            if not conn.is_group_wake_triggered(raw_message, text_for_cmd, segments_for_wake):
                return False
        return True
    async def _parse_message_segments(self, data: dict, conn, raw_message, text_for_cmd: str = "",
                                      segments: Optional[List[Dict[str, Any]]] = None) -> Optional[dict]:
        segments = segments if segments is not None else _extract_segments(raw_message)
        parsed = {
            "segments": segments,
            "text": text_for_cmd or _segments_text(segments),
            "images": _extract_images(segments),
            "voice_url": _extract_voice(segments),
            "video_url": _extract_video(segments),
            "at_targets": _extract_at(segments),
            "reply_id": _extract_reply(segments),
            "face_id": _extract_face(segments),
            "forward_id": _extract_forward(segments),
            "json_card": _extract_json_card(segments),
            "xml_msg": _extract_xml(segments),
            **_extract_typed_segments(segments),
            "forward_content": "",
            "forward_images": [],
        }
        if not any(parsed[k] for k in ("text", "images", "voice_url", "video_url", "forward_id", "face_id", "json_card", "xml_msg")) \
                and not any(v for k, v in parsed.items() if k.endswith("_msg") or k.endswith("_seg")):
            return None
        if parsed["forward_id"]:
            try:
                parsed["forward_content"], parsed["forward_images"] = await self._resolve_forward_message(parsed["forward_id"], conn)
            except Exception as e:
                pass
        return parsed
    @staticmethod
    def _prune_oldest(d: dict, max_size: int, prune_count: int = None):
        if len(d) <= max_size:
            return
        prune_count = prune_count or (max_size // 2)
        oldest = sorted(d, key=d.get)[:prune_count]
        for k in oldest:
            del d[k]
    @staticmethod
    def _prune_arbitrary(d: dict, max_size: int, prune_count: int = 100):
        if len(d) <= max_size:
            return
        for k in list(d.keys())[:prune_count]:
            del d[k]
    def _cleanup_seq_dictionaries(self, chat_id: str):
        now = time.time()
        if now - self._last_seq_cleanup_time < 60:
            return
        self._last_seq_cleanup_time = now
        self._prune_oldest(self._msg_receive_seq, 200, 50)
        self._prune_oldest(self._chat_msg_seq, 500, 300)
        for d in (self._last_msg_id, self._active_input_status):
            self._prune_arbitrary(d, 500)
        if len(self._last_progress_msg) > 200:
            for k in list(self._last_progress_msg)[:len(self._last_progress_msg) - 200]:
                del self._last_progress_msg[k]
        if len(self._pending_approvals) > 50 and _HAS_APPROVAL:
            stale = [k for k, sk in self._pending_approvals.items() if not has_blocking_approval(sk)]
            for k in stale:
                self._pending_approvals.pop(k, None)
                self._pending_approval_admin.pop(k, None)
        if len(self._approval_locks) > 100:
            active_approvals = set(self._pending_approvals)
            self._approval_locks = {k: v for k, v in self._approval_locks.items() if k in active_approvals}
        self._bg_delete_tasks = {t for t in self._bg_delete_tasks if not t.done()}
        for k in [k for k, t in self._active_tasks.items() if t.done()]:
            del self._active_tasks[k]
        for d, ttl in ((self._reject_notified, REJECT_NOTIFY_TTL), (self._pending_update_chats, PENDING_UPDATE_TTL)):
            for k in [k for k, v in d.items() if now - v > ttl]:
                del d[k]
    def _strip_at_mentions(self, text: str, raw_message, conn, msg_type: str) -> str:
        if msg_type == "group" and conn.self_id:
            text = re.sub(r'\[CQ:at,qq=' + re.escape(conn.self_id) + r'\]', '', text).strip()
            if isinstance(raw_message, list) and any(
                seg.get("type") == "at" and str((seg.get("data") or {}).get("qq", "")) == conn.self_id
                for seg in raw_message
            ):
                text = text.lstrip()
        return text
    async def _build_display_text(self, parsed: dict, text: str, msg_type: str,
                                   is_slash_cmd: bool, sender_name: str, conn) -> tuple:
        display_text = text
        if msg_type == "group" and not is_slash_cmd:
            display_text = f"[{sender_name}] {text}" if text else f"[{sender_name}]"
        if parsed["voice_url"] and not text:
            display_text = (display_text or "") + " [语音消息]"
        if parsed["video_url"]:
            display_text = (display_text or "") + " [视频消息]"
        if parsed["face_id"]:
            display_text = (display_text or "") + f" [表情{parsed['face_id']}]"
        typed_parts = [v for k, v in parsed.items() if k.endswith("_msg") or k.endswith("_seg")]
        for _seg_text in (parsed.get("json_card"), parsed.get("xml_msg"), *typed_parts):
            if _seg_text:
                display_text = (display_text or "") + " " + _seg_text
        if parsed["forward_content"]:
            display_text = (display_text or "") + parsed["forward_content"]
        if parsed["reply_id"]:
            display_text, quoted_images = await self._append_reply_context(
                display_text, parsed["reply_id"], conn, parsed.get("segments", [])
            )
        else:
            quoted_images = []
        forward_images = parsed.get("forward_images", [])
        if forward_images:
            quoted_images = (quoted_images or []) + forward_images
        return display_text, text, quoted_images
    async def _download_images_parallel(self, urls: List[str]) -> List[str]:
        if not urls:
            return []
        sem = asyncio.Semaphore(IMAGE_DOWNLOAD_CONCURRENCY)
        async def _download_one(img_url):
            async with sem:
                return await self._media_cache.download(img_url, self._http_client, "image")
        results = await asyncio.gather(*[_download_one(u) for u in urls], return_exceptions=True)
        return [r for r in results if isinstance(r, str) and r]
    async def _inject_file_content(self, segments: List[Dict], text: str, conn) -> str:
        injected = text
        for seg in segments:
            if seg.get("type") != "file":
                continue
            data = seg.get("data") or {}
            file_name = data.get("name") or data.get("file") or ""
            ext = Path(file_name).suffix.lower() if file_name else ""
            if ext not in _TEXT_EXTS:
                continue
            local_path = None
            file_url = data.get("url") or data.get("file_url") or ""
            if not file_url:
                file_url = await self._resolve_file_url(data, conn)
            # C3: Only allow files from media cache directory (allowlist approach)
            cache_dir = str(self._media_cache._dir.resolve())
            if file_url.startswith("file://"):
                decoded = url_unquote(file_url[7:])
                resolved = str(Path(decoded).resolve())
                if resolved.startswith(cache_dir + os.sep) and os.path.isfile(resolved) and not os.path.islink(resolved):
                    local_path = resolved
            elif file_url.startswith(("http://", "https://")):
                try:
                    local_path = await self._media_cache.download(file_url, self._http_client, "file")
                except Exception as e:
                    logger.debug("File download failed: %s", e)
                    continue
            if not local_path or not os.path.isfile(local_path):
                continue
            # M7: File size limit
            if os.path.getsize(local_path) > FILE_INJECTION_MAX_BYTES:
                logger.debug("File too large for injection: %s", local_path)
                continue
            try:
                with open(local_path, "r", errors="replace") as f:
                    file_content = f.read(FILE_INJECTION_MAX_BYTES)
                display_name = os.path.basename(local_path)
                injection = f"[Content of {display_name}]:\n{file_content}"
                injected = f"{injection}\n\n{injected}" if injected.strip() else injection
            except Exception as e:
                pass
        return injected
    async def _resolve_file_url(self, data: dict, conn) -> str:
        file_id = data.get("file_id") or data.get("id") or data.get("file")
        if not file_id:
            return ""
        params_list = [{"file_id": file_id}, {"file": file_id}, {"id": file_id}]
        busid = data.get("busid") or data.get("bus_id")
        if busid is not None:
            params_list.insert(0, {"file_id": file_id, "busid": busid})
        for params in params_list:
            try:
                result = await self._send_action_conn(conn, "get_file", params, timeout=10.0)
            except Exception:
                continue
            if result.get("retcode") != 0:
                continue
            rdata = result.get("data") or {}
            url = rdata.get("url") or rdata.get("file_url") or rdata.get("path") or rdata.get("file") or ""
            if url:
                if os.path.isabs(str(url)):
                    return f"file://{url}"
                return str(url)
        return ""
    async def _build_and_dispatch_event(self, parsed: dict, display_text: str, text: str,
                                         chat_id: str, user_id: str, sender_name: str,
                                         message_id: str, msg_type: str, *,
                                         conn, data: dict,
                                         quoted_images: Optional[List[str]] = None):
        images = parsed["images"]
        voice_url = parsed["voice_url"]
        video_url = parsed["video_url"]
        reply_id = parsed["reply_id"]
        all_images = list(images) if images else []
        if quoted_images:
            all_images.extend(quoted_images)
        local_image_paths = await self._download_images_parallel(all_images)
        display_text = await self._inject_file_content(parsed.get("segments", []), display_text, conn)
        source = self.build_source(
            chat_id=chat_id, user_id=user_id, user_name=sender_name,
            message_id=message_id, chat_type="dm" if msg_type == "private" else "group",
        )
        msg_type_enum = MessageType.TEXT
        for key, mtype in _MSG_TYPE_MAP.items():
            if parsed.get(key):
                msg_type_enum = mtype
                break
        event = MessageEvent(
            source=source, text=display_text, message_type=msg_type_enum,
            raw_message=data, message_id=message_id,
        )
        all_media, all_types = [], []
        img_src = local_image_paths or all_images
        if img_src:
            all_media.extend(img_src)
            all_types.extend(["image"] * len(img_src))
        for url, mtype in ((voice_url, "voice"), (video_url, "video")):
            if url:
                all_media.append(url)
                all_types.append(mtype)
        if all_media:
            event.media_urls = all_media
            event.media_types = all_types
        if reply_id:
            event.reply_to_message_id = reply_id
        await self.handle_message(event)
    _FORWARD_MAX_DEPTH = 3
    _FORWARD_MAX_FETCHES = 8
    _FORWARD_MAX_IMAGES = 10
    async def _resolve_forward_message(self, forward_id: str, conn, *, depth: int = 0,
                                        _seen: Optional[set] = None,
                                        _fetch_count: List[int] = None) -> Tuple[str, List[str]]:
        if _seen is None:
            _seen = set()
        if _fetch_count is None:
            _fetch_count = [0]
        if forward_id in _seen or depth > self._FORWARD_MAX_DEPTH:
            return "", []
        _seen.add(forward_id)
        _fetch_count[0] += 1
        if _fetch_count[0] > self._FORWARD_MAX_FETCHES:
            return "", []
        fwd_lines: List[str] = []
        fwd_images: List[str] = []
        try:
            forward_msgs = await self.get_forward_msg(forward_id, conn=conn)
        except Exception as e:
            return "", []
        if not forward_msgs:
            return "", []
        for fmsg in forward_msgs:
            f_name = (fmsg.get("sender", {}).get("nickname")
                      or fmsg.get("sender", {}).get("card") or "未知")
            raw_content = fmsg.get("content") or fmsg.get("message") or ""
            f_segments = _extract_segments(raw_content)
            f_text = _extract_text_from_message(raw_content)
            f_images = _extract_images(f_segments)
            nested_forward_id = _extract_forward(f_segments)
            json_card = _extract_json_card(f_segments)
            xml_msg = _extract_xml(f_segments)
            typed = _extract_typed_segments(f_segments)
            line_parts = []
            if f_text:
                line_parts.append(f_text)
            if f_images and len(fwd_images) < self._FORWARD_MAX_IMAGES:
                fwd_images.extend(f_images[:self._FORWARD_MAX_IMAGES - len(fwd_images)])
                line_parts.append("[图片]")
            for _seg_text in (json_card, xml_msg, *typed.values()):
                if _seg_text:
                    line_parts.append(_seg_text)
            try:
                injected = await self._inject_file_content(f_segments, "", conn)
                if injected:
                    line_parts.append(injected[:MAX_QUOTE_TEXT] + "…" if len(injected) > MAX_QUOTE_TEXT else injected)
            except Exception:
                pass
            if nested_forward_id:
                nested_text, nested_imgs = await self._resolve_forward_message(
                    nested_forward_id, conn, depth=depth + 1, _seen=_seen, _fetch_count=_fetch_count)
                if nested_text:
                    line_parts.append(nested_text)
                if nested_imgs and len(fwd_images) < self._FORWARD_MAX_IMAGES:
                    fwd_images.extend(nested_imgs[:self._FORWARD_MAX_IMAGES - len(fwd_images)])
            if line_parts:
                fwd_lines.append(f"{'  ' * depth}{f_name}: {' '.join(line_parts)}")
        text_block = ""
        if fwd_lines:
            text_block = "\n[合并转发消息]\n" + "\n".join(fwd_lines) + "\n[转发结束]"
        return text_block, fwd_images
    def _reply_segment_fallback(self, segments: List[Dict]) -> str:
        for seg in segments or []:
            if seg.get("type") != "reply":
                continue
            data = seg.get("data") or {}
            sender = data.get("qq") or data.get("user_id") or data.get("sender_id") or "?"
            body = ""
            for key in ("text", "content", "message", "summary"):
                if data.get(key):
                    body = str(data.get(key))
                    break
            body = body or "无法从NapCat取回原文"
            return f"\n[引用 {sender}: {body[:MAX_QUOTE_TEXT]}]"
        return ""
    async def _append_reply_context(self, display_text: str, reply_id: str, conn, source_segments: Optional[List[Dict]] = None) -> tuple:
        quoted_images: List[str] = []
        _fallback = self._reply_segment_fallback(source_segments or []) or "\n[引用了一条消息，但无法获取内容]"
        try:
            quoted_obj = await asyncio.wait_for(self.get_msg(reply_id, conn=conn), timeout=10.0)
            if not quoted_obj:
                return (display_text or "") + _fallback, quoted_images
            quoted_message = quoted_obj.get("message", "")
            quoted_text = _extract_text_from_message(quoted_message)
            quoted_name = (quoted_obj.get("sender", {}).get("nickname") or quoted_obj.get("real_id", "?"))
            quoted_segments = _extract_segments(quoted_message)
            quoted_images = _extract_images(quoted_segments)
            quoted_forward_id = _extract_forward(quoted_segments)
            quoted_json_card = _extract_json_card(quoted_segments)
            quoted_typed = _extract_typed_segments(quoted_segments)
            quote_parts = []
            if quoted_text:
                quote_parts.append(quoted_text[:MAX_MULTIMSG_PREVIEW] + "…" if len(quoted_text) > MAX_MULTIMSG_PREVIEW else quoted_text)
            elif quoted_images:
                quote_parts.append("[图片]")
            for _seg_text in quoted_typed.values():
                if _seg_text:
                    quote_parts.append(_seg_text)
            quoted_file_text = await self._inject_file_content(quoted_segments, "", conn)
            if quoted_file_text:
                quote_parts.append(quoted_file_text[:MAX_QUOTE_TEXT] + "…" if len(quoted_file_text) > MAX_QUOTE_TEXT else quoted_file_text)
            if quoted_forward_id:
                try:
                    fwd_text, fwd_imgs = await self._resolve_forward_message(quoted_forward_id, conn)
                    if fwd_text:
                        quote_parts.append(fwd_text)
                    if fwd_imgs:
                        quoted_images.extend(fwd_imgs)
                except Exception:
                    pass
            elif quoted_json_card:
                quote_parts.append(quoted_json_card)
            if quote_parts:
                combined = " ".join(quote_parts)
                combined = combined[:MAX_QUOTE_TEXT] + "…" if len(combined) > MAX_QUOTE_TEXT else combined
                display_text = (display_text or "") + f"\n[引用 {quoted_name}: {combined}]"
            else:
                display_text = (display_text or "") + _fallback
        except Exception:
            display_text = (display_text or "") + _fallback
        return display_text, quoted_images
    async def _try_action_formats(self, action: str, params_list: List[dict], conn=None) -> dict:
        for params in params_list:
            if conn is not None:
                result = await self._send_action_conn(conn, action, params)
            else:
                result = await self._send_action(action, params)
            if result.get("retcode") == 0:
                data = result.get("data", {})
                if data:
                    return data
        return {}
    async def get_msg(self, message_id: str, conn=None) -> Dict:
        mid_str = str(message_id).strip()
        if not mid_str:
            return {}
        params_list: List[dict] = []
        try:
            mid_int = int(mid_str)
            params_list.extend([{"message_id": mid_int}, {"id": mid_int}])
        except (ValueError, TypeError):
            pass
        params_list.extend([{"message_id": mid_str}, {"id": mid_str}])
        cached = getattr(conn, '_working_msg_params', None) if conn else None
        if cached:
            key_name = cached.get("_key", "message_id")
            for p in [{key_name: mid_str}] + ([{key_name: int(mid_str)}] if mid_str.isdigit() else []):
                result = await self._send_action_conn(conn, "get_msg", p)
                if result.get("retcode") == 0 and result.get("data"):
                    return result["data"]
            params_list = [p for p in params_list if p != {key_name: mid_str}]
        data = await self._try_action_formats("get_msg", params_list, conn)
        if data and conn is not None:
            conn._working_msg_params = {"_key": "message_id"}
        return data
    async def get_forward_msg(self, forward_id: str, conn=None) -> List[Dict]:
        fid_str = str(forward_id).strip()
        if not fid_str:
            return []
        params_list: List[dict] = [{"id": fid_str}, {"forward_id": fid_str}]
        try:
            fid_int = int(fid_str)
            params_list.insert(0, {"id": fid_int})
        except (ValueError, TypeError):
            pass
        data = await self._try_action_formats("get_forward_msg", params_list, conn)
        if isinstance(data, dict):
            msgs = data.get("messages") or data.get("message") or data.get("items") or []
            return msgs if msgs else []
        if isinstance(data, list):
            return data
        return []
