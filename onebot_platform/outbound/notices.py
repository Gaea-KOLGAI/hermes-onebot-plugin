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


def _notice_approval_candidate_chat_ids(self, data: dict, conn: _NapCatConnection, actor_id: str) -> list:
    candidate_chat_ids = []
    if data.get("group_id"):
        candidate_chat_ids.append(f"group_{data.get('group_id')}")
    elif actor_id:
        candidate_chat_ids.append(f"private_{actor_id}")
    if self._multi_account:
        candidate_chat_ids = [f"{conn.name}:{cid}" for cid in candidate_chat_ids] + candidate_chat_ids
    return candidate_chat_ids


def _reaction_target_message_id(data: dict) -> str:
    for key in (
        "target_message_id", "message_id", "msg_id", "source_msg_id", "message_seq",
        "target_msg_id", "target_id", "msg_seq", "seq",
    ):
        value = data.get(key)
        if value not in (None, ""):
            return str(value)
    message = data.get("message")
    if isinstance(message, dict):
        for key in ("message_id", "msg_id", "id", "seq", "message_seq", "msg_seq"):
            value = message.get(key)
            if value not in (None, ""):
                return str(value)
    for container_key in ("target", "source", "data"):
        obj = data.get(container_key)
        if isinstance(obj, dict):
            for key in ("message_id", "msg_id", "id", "seq", "message_seq", "msg_seq"):
                value = obj.get(key)
                if value not in (None, ""):
                    return str(value)
    return ""


def _is_reaction_notice(notice_type: str, sub_type: str) -> bool:
    return notice_type in {
        "group_reaction", "message_reaction", "message_reactions_updated", "reaction",
        "group_msg_emoji_like",
    } or (
        notice_type == "notify" and sub_type in {"reaction", "emoji_like", "message_reaction", "group_reaction", "group_msg_emoji_like"}
    )


def _is_reaction_add_notice(data: dict) -> bool:
    """Return False only when the reaction notice explicitly describes removal."""
    is_add = data.get("is_add")
    if is_add is False or str(is_add).strip().lower() in {"false", "0", "no"}:
        return False
    for key in ("action", "operation", "event_type", "reaction_type", "sub_type"):
        value = data.get(key)
        if value is None:
            continue
        text = str(value).strip().lower()
        if text in {"remove", "removed", "delete", "deleted", "cancel", "cancelled", "unset", "unlike"}:
            return False
    return True


def _reaction_has_approval_emoji(data: dict, approval_emoji_id: str = "66") -> bool:
    def _matches(obj) -> bool:
        if not isinstance(obj, dict):
            return False
        emoji = obj.get("emoji_id") or obj.get("emojiId") or obj.get("qface_id") or obj.get("face_id") or obj.get("id")
        if str(emoji) != str(approval_emoji_id):
            return False
        count = obj.get("count")
        if count is not None:
            try:
                return int(count) > 0
            except (TypeError, ValueError):
                return str(count).strip().lower() not in {"", "0", "false", "no"}
        return True
    if _matches(data):
        return True
    for key in ("likes", "current_reactions", "reactions", "reaction", "emoji_like"):
        value = data.get(key)
        if isinstance(value, list) and any(_matches(item) for item in value):
            return True
        if _matches(value):
            return True
    return False


async def _resolve_notice_approval(self, data: dict, conn: _NapCatConnection, actor_id: str, choice: str, *, target_message_id: str = "") -> bool:
    if not _HAS_APPROVAL:
        return False
    admin_qq = (conn.admin_qq or os.getenv("ONEBOT_ADMIN_QQ", "")).strip()
    for chat_id in _notice_approval_candidate_chat_ids(self, data, conn, actor_id):
        if target_message_id:
            expected_message_id = str(self._pending_approval_messages.get(chat_id, "") or "")
            if not expected_message_id or expected_message_id != str(target_message_id):
                continue
        is_admin_approval = self._pending_approval_admin.get(chat_id, False)
        if is_admin_approval and (not admin_qq or str(actor_id) != str(admin_qq)):
            continue
        if chat_id in self._pending_approvals:
            if await self._resolve_approval_shortcut(chat_id, choice, actor_id, admin_qq, from_notice=True):
                return True
    return False


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
    if _is_reaction_notice(notice_type, sub_type):
        actor_id = str(data.get("user_id") or data.get("operator_id") or "")
        target_message_id = _reaction_target_message_id(data)
        if (
            _is_reaction_add_notice(data)
            and _reaction_has_approval_emoji(data)
            and actor_id
            and target_message_id
            and await _resolve_notice_approval(self, data, conn, actor_id, "2", target_message_id=target_message_id)
        ):
            return
        return
    if notice_type == "notify" and sub_type == "poke":
        poker_id = str(data.get("user_id", ""))
        target_id = data.get("target_id", "")
        self_id = data.get("self_id", "")
        if str(target_id) != str(self_id):
            return
        if data.get("group_id"):
            return
        if await _resolve_notice_approval(self, data, conn, poker_id, "1"):
            return
        await dispatch_notice_text(self, data, conn, f"[戳一戳: {poker_id}]")
