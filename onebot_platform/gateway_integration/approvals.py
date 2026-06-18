from __future__ import annotations

import onebot_platform.adapter_runtime as _runtime
globals().update({k: v for k, v in vars(_runtime).items() if not k.startswith('__')})

_APPROVAL_CHOICES = {
    "1": "once", "approve": "once", "批准": "once", "批准一次": "once", "单次批准": "once",
    "once": "once", "y": "once", "yes": "once",
    "2": "session", "session": "session", "approve session": "session",
    "会话批准": "session", "本会话批准": "session",
    "3": "always", "always": "always", "approve always": "always",
    "永久批准": "always",
    "4": "deny", "deny": "deny", "拒绝": "deny", "n": "deny", "no": "deny",
}
_UPDATE_CHOICES = {
    "1": "y", "y": "y", "yes": "y", "是": "y", "确认": "y",
    "2": "n", "n": "n", "no": "n", "否": "n", "取消": "n",
}
_APPROVAL_REACTION_EMOJI_ID = "66"


<<<<<<< HEAD
def _approval_notify_metadata(chat_id: str, metadata: Optional[Dict[str, Any]], last_msg_user_id: str = "", auto_at_enabled: bool = True) -> Optional[Dict[str, Any]]:
    notify_metadata = dict(metadata or {})
    msg_type, _target_id = _parse_chat_id(chat_id)
    if not auto_at_enabled:
        return notify_metadata or None
    originator = str(notify_metadata.get("originator_user_id") or "").strip()
    if not originator and last_msg_user_id:
        originator = str(last_msg_user_id).strip()
=======
def _approval_notify_metadata(chat_id: str, metadata: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    notify_metadata = dict(metadata or {})
    msg_type, _target_id = _parse_chat_id(chat_id)
    originator = str(notify_metadata.get("originator_user_id") or "").strip()
>>>>>>> aaad7b1a70ed13c15c707e04b5f2cd4a3a169130
    if msg_type == "group" and originator and originator.isdigit():
        notify_metadata.setdefault("mention_originator_user_id", originator)
        notify_metadata.setdefault("mention_reason", "approval_prompt")
    return notify_metadata or None


class ApprovalMixin:
    async def send_exec_approval(
        self,
        chat_id: str,
        command: str,
        session_key: str,
        description: str = "dangerous command",
        metadata: Optional[Dict[str, Any]] = None,
    ) -> SendResult:
        self._pending_approvals[chat_id] = session_key
        if metadata and metadata.get("admin_only"):
            self._pending_approval_admin[chat_id] = True
        else:
            self._pending_approval_admin.pop(chat_id, None)
        cmd_preview = command[:300] + "..." if len(command) > 300 else command
        msg = (
            f"⚠️ 危险命令审批:\n"
            f"命令: {cmd_preview}\n"
            f"原因: {description}\n"
            f"戳一戳我批准一次\n"
            f"点下方表情=允许该会话\n"
            f"回复1 单次批准\n"
            f"回复2 会话批准\n"
            f"回复3 永久批准\n"
            f"回复4 拒绝"
        )
        reply_to = self._last_msg_id.get(chat_id)
<<<<<<< HEAD
        last_user = self._last_msg_user_id.get(chat_id, "")
        _auto_at = True
        _ps = getattr(self, "_plugin_settings", None)
        if _ps:
            _gs = _ps.get_chat("_global")
            _v = _gs.get("auto_at_originator")
            _auto_at = True if _v is None else bool(_v)
        approval_metadata = _approval_notify_metadata(chat_id, metadata, last_user, _auto_at)
=======
        approval_metadata = _approval_notify_metadata(chat_id, metadata)
>>>>>>> aaad7b1a70ed13c15c707e04b5f2cd4a3a169130
        result = await self.send(chat_id, msg, reply_to=reply_to, metadata=approval_metadata)
        approval_msg_id = str(getattr(result, "message_id", "") or "")
        if approval_msg_id:
            self._pending_approval_messages[chat_id] = approval_msg_id
            try:
                await self._send_action_conn(
                    self._get_conn_for_chat(chat_id),
                    "set_msg_emoji_like",
                    {"message_id": int(approval_msg_id) if approval_msg_id.isdigit() else approval_msg_id, "emoji_id": _APPROVAL_REACTION_EMOJI_ID},
                    timeout=10.0,
                )
            except Exception as e:
                logger.debug("Failed to add approval reaction hint to %s: %s", approval_msg_id, e)
        else:
            self._pending_approval_messages.pop(chat_id, None)
        return result
    async def send_update_prompt(
        self,
        chat_id: str,
        prompt: str,
        default: str = "",
        session_key: str = "",
        metadata: Optional[Dict[str, Any]] = None,
    ) -> SendResult:
        msg = (
            f"🔄 更新确认:\n"
            f"{prompt}\n\n"
            f"回复 1 或 y → 确认\n"
            f"回复 2 或 n → 取消"
        )
        reply_to = self._last_msg_id.get(chat_id)
        self._pending_update_chats[chat_id] = time.time()
<<<<<<< HEAD
        last_user = self._last_msg_user_id.get(chat_id, "")
        _auto_at = True
        _ps = getattr(self, "_plugin_settings", None)
        if _ps:
            _gs = _ps.get_chat("_global")
            _v = _gs.get("auto_at_originator")
            _auto_at = True if _v is None else bool(_v)
        update_metadata = _approval_notify_metadata(chat_id, metadata, last_user, _auto_at)
        return await self.send(chat_id, msg, reply_to=reply_to, metadata=update_metadata)
=======
        return await self.send(chat_id, msg, reply_to=reply_to, metadata=metadata)
>>>>>>> aaad7b1a70ed13c15c707e04b5f2cd4a3a169130
    async def _resolve_approval_shortcut(
        self,
        chat_id: str,
        user_text: str,
        user_id: str = "",
        admin_qq: str = "",
        *,
        reply_to_message_id: str = "",
        from_notice: bool = False,
    ) -> bool:
        if not _HAS_APPROVAL:
            return False
        text = _strip_slash(user_text.strip().lower())
<<<<<<< HEAD
        # Strip leading @mention: @name(QQ:xxx) or @xxx
        if conn_self := getattr(self._get_conn_for_chat(chat_id), "self_id", "") or "":
            import re
            text = re.sub(r'@\S*\(QQ:' + re.escape(conn_self) + r'\)\s*', '', text)
            text = re.sub(r'@' + re.escape(conn_self) + r'\s*', '', text)
            text = text.strip()
=======
>>>>>>> aaad7b1a70ed13c15c707e04b5f2cd4a3a169130
        choice = _APPROVAL_CHOICES.get(text)
        if choice is None:
            return False
        lock = self._approval_locks.setdefault(chat_id, asyncio.Lock())
        async with lock:
            session_key = self._pending_approvals.get(chat_id)
            is_admin_approval = self._pending_approval_admin.get(chat_id, False)
            if not session_key:
                return False
            try:
                from tools.approval import has_blocking_approval
                if not has_blocking_approval(session_key):
                    self._pending_approvals.pop(chat_id, None)
                    self._pending_approval_admin.pop(chat_id, None)
                    self._pending_approval_messages.pop(chat_id, None)
                    return False
            except ImportError:
                pass
            msg_type, target_id = _parse_chat_id(chat_id)
            if msg_type == "group" and not from_notice:
                expected_message_id = str(self._pending_approval_messages.get(chat_id, "") or "")
                if not expected_message_id or str(reply_to_message_id or "") != expected_message_id:
                    return False
            if user_id:
                auth_data = {"message_type": msg_type, "user_id": str(user_id)}
                if msg_type == "group":
                    auth_data["group_id"] = target_id
                conn = self._get_conn_for_chat(chat_id)
                effective_admin_qq = str(admin_qq or getattr(conn, "admin_qq", "") or os.getenv("ONEBOT_ADMIN_QQ", "")).strip()
                if not effective_admin_qq or str(user_id) != effective_admin_qq:
                    if not conn.is_user_authorized(str(user_id), msg_type, auth_data):
                        return False
            if is_admin_approval:
                if not admin_qq:
                    logger.warning("approval admin is not configured; rejecting admin_only shortcut for %s", chat_id)
                    await self.send(chat_id, "✗ 管理员未配置，无法批准此操作")
                    return True
                if user_id and str(user_id) != str(admin_qq):
                    return False
            try:
                from tools.approval import resolve_gateway_approval
                resolve_gateway_approval(session_key, choice)
            except Exception as e:
                logger.warning("Failed to resolve gateway approval %s: %s", session_key, e)
                return False
            self._pending_approvals.pop(chat_id, None)
            self._pending_approval_admin.pop(chat_id, None)
            self._pending_approval_messages.pop(chat_id, None)
        choice_text = {
            "once": "单次批准",
            "session": "会话批准",
            "always": "永久批准",
            "deny": "已拒绝",
        }
        await self.send(chat_id, f"✓ {choice_text.get(choice, choice)}")
        return True
    async def _handle_update_shortcut(self, chat_id: str, user_text: str) -> bool:
        if chat_id not in self._pending_update_chats:
            return False
        text = _strip_slash(user_text.strip().lower())
<<<<<<< HEAD
        # Strip leading @mention: @name(QQ:xxx) or @xxx
        if conn_self := getattr(self._get_conn_for_chat(chat_id), "self_id", "") or "":
            import re
            text = re.sub(r'@\S*\(QQ:' + re.escape(conn_self) + r'\)\s*', '', text)
            text = re.sub(r'@' + re.escape(conn_self) + r'\s*', '', text)
            text = text.strip()
=======
>>>>>>> aaad7b1a70ed13c15c707e04b5f2cd4a3a169130
        answer = _UPDATE_CHOICES.get(text)
        if answer is None:
            return False
        self._pending_update_chats.pop(chat_id, None)
        try:
            from hermes_constants import get_hermes_home
            home = get_hermes_home()
            response_path = home / ".update_response"
            tmp = response_path.with_suffix(".tmp")
            tmp.write_text(answer)
            tmp.replace(response_path)
            await self.send(chat_id, f"✓ 已{'确认' if answer == 'y' else '取消'}更新")
            return True
        except Exception as e:
            return False
