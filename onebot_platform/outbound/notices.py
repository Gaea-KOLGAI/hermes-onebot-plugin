from __future__ import annotations

import onebot_platform.adapter_runtime as _runtime
globals().update({k: v for k, v in vars(_runtime).items() if not k.startswith('__')})


def notice_sender_name(self, data: dict) -> str:
    return str(data.get("nickname") or data.get("card") or data.get("user_id") or "system")


async def dispatch_notice_text(self, data: dict, conn: _NapCatConnection, text: str, *, media_url: str = "", media_type: str = "") -> None:
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
        user_name=notice_sender_name(self, data),
        message_id=str(data.get("message_id") or data.get("file", {}).get("id") or data.get("flag") or ""),
        chat_type="group" if msg_type == "group" else "dm",
    )
    event = MessageEvent(source=source, text=text, message_type=MessageType.TEXT, raw_message=data, message_id=source.message_id)
    if media_url:
        event.media_urls = [media_url]
        event.media_types = [media_type or "file"]
    await self.handle_message(event)


async def handle_group_upload_notice(self, data: dict, conn: _NapCatConnection) -> None:
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
    await dispatch_notice_text(self, data, conn, text, media_url=file_url, media_type="file")


async def handle_notice(self, data: dict, conn: _NapCatConnection) -> None:
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
        await dispatch_notice_text(self, data, conn, f"[戳一戳: {poker_id}]")
