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
            f"回复1 单次批准\n"
            f"回复2 会话批准\n"
            f"回复3 永久批准\n"
            f"回复4 拒绝"
        )
        reply_to = self._last_msg_id.get(chat_id)
        result = await self.send(chat_id, msg, reply_to=reply_to, metadata=metadata)
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
        return await self.send(chat_id, msg, reply_to=reply_to, metadata=metadata)
    async def _resolve_approval_shortcut(
        self, chat_id: str, user_text: str, user_id: str = "", admin_qq: str = "",
    ) -> bool:
        if not _HAS_APPROVAL:
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
                    return False
            except ImportError:
                pass
            if is_admin_approval:
                if not admin_qq:
                    logger.warning("approval admin is not configured; rejecting admin_only shortcut for %s", chat_id)
                    await self.send(chat_id, "✗ 管理员未配置，无法批准此操作")
                    return True
                if user_id and str(user_id) != str(admin_qq):
                    return False
            text = _strip_slash(user_text.strip().lower())
            choice = _APPROVAL_CHOICES.get(text)
            if choice is None:
                return False
            try:
                from tools.approval import resolve_gateway_approval
                resolve_gateway_approval(session_key, choice)
            except Exception as e:
                logger.warning("Failed to resolve gateway approval %s: %s", session_key, e)
                return False
            self._pending_approvals.pop(chat_id, None)
            self._pending_approval_admin.pop(chat_id, None)
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
