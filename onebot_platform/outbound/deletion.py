from __future__ import annotations

import onebot_platform.adapter_runtime as _runtime
globals().update({k: v for k, v in vars(_runtime).items() if not k.startswith('__')})


async def delete_message_with_status(self, chat_id: str, message_id: str, timeout: float = 15.0) -> Optional[bool]:
    conn = self._get_conn_for_chat(chat_id)
    try:
        mid = _safe_int(message_id, "message_id")
    except ValueError:
        return False
    try:
        result = await self._send_action_conn(conn, "delete_msg", {"message_id": mid}, timeout=timeout)
        retcode = result.get("retcode")
        if retcode in (0, 200):
            return True
        if retcode == -1:
            return None
        return False
    except Exception:
        return None


async def bg_delete(self, chat_id: str, message_id: str) -> None:
    try:
        status = await delete_message_with_status(self, chat_id, message_id, timeout=3.0)
        if status is None:
            self._delete_msg_supported = False
    except Exception:
        pass


def fire_and_forget_delete(self, chat_id: str, message_id: str) -> None:
    if not self._delete_msg_supported:
        return
    try:
        task = asyncio.ensure_future(self._bg_delete(chat_id, message_id))
        self._bg_delete_tasks.add(task)
        task.add_done_callback(self._bg_delete_tasks.discard)
    except Exception:
        pass
