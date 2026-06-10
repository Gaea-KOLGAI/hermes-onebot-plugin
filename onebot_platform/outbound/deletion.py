from __future__ import annotations

import onebot_platform.adapter_runtime as _runtime
globals().update({k: v for k, v in vars(_runtime).items() if not k.startswith('__')})


def delete_circuit_is_open(self) -> bool:
    circuit = getattr(self, "_delete_msg_circuit", None) or {}
    return time.monotonic() < float(circuit.get("opened_until", 0.0) or 0.0)


def record_delete_failure(self, *, threshold: int = 3, cooldown: float = 300.0) -> None:
    circuit = getattr(self, "_delete_msg_circuit", None)
    if not isinstance(circuit, dict):
        circuit = self._delete_msg_circuit = {"failures": 0, "opened_until": 0.0}
    failures = int(circuit.get("failures", 0) or 0) + 1
    circuit["failures"] = failures
    if failures >= threshold:
        circuit["opened_until"] = time.monotonic() + cooldown


def record_delete_success(self) -> None:
    circuit = getattr(self, "_delete_msg_circuit", None)
    if isinstance(circuit, dict):
        circuit["failures"] = 0
        circuit["opened_until"] = 0.0


async def delete_message_with_status(self, chat_id: str, message_id: str, timeout: float = 15.0) -> Optional[bool]:
    if delete_circuit_is_open(self):
        return None
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
            record_delete_failure(self)
        elif status is True:
            record_delete_success(self)
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
