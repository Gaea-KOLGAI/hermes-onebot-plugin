from __future__ import annotations

import asyncio

import onebot_platform.adapter_runtime as _runtime
globals().update({k: v for k, v in vars(_runtime).items() if not k.startswith('__')})


async def cancel_conn_tasks(self, conn: _NapCatConnection, *task_attrs: str, clear: bool = False):
    for attr in task_attrs:
        task = getattr(conn, attr, None)
        if task and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            except Exception:
                pass
        if clear:
            setattr(conn, attr, None)


async def disconnect_conn(self, conn: _NapCatConnection) -> None:
    await self._cancel_conn_tasks(conn, "recv_task", "heartbeat_task", "reconnect_task", clear=True)
    if conn.ws:
        try:
            await asyncio.wait_for(conn.ws.close(), timeout=5.0)
        except Exception:
            pass
        conn.ws = None
    if conn.ws_server:
        try:
            conn.ws_server.close()
            await asyncio.wait_for(conn.ws_server.wait_closed(), timeout=5.0)
        except Exception:
            pass
        conn.ws_server = None
    for fut in conn.echo_futures.values():
        if not fut.done():
            fut.set_exception(asyncio.TimeoutError("echo stale"))
    conn.echo_futures.clear()
    conn._echo_timestamps.clear()
    conn.reverse_ws_clients.clear()
    prefix = f"{conn.name}:" if self._multi_account else ""
    conn_names = tuple(f"{n}:" for n in self._connections.keys())
    def _is_ours(key):
        if prefix:
            return key.startswith(prefix)
        return not (conn_names and key.startswith(conn_names))
    for chat_id in [k for k in self._active_tasks if _is_ours(k)]:
        task = self._active_tasks.pop(chat_id, None)
        if task and not task.done():
            task.cancel()


async def fetch_self_info_conn(self, conn: _NapCatConnection):
    try:
        result = await self._send_action_conn(conn, "get_login_info", {})
        if result.get("retcode") == 0:
            data = result.get("data", {})
            conn.self_id = str(data.get("user_id", ""))
            conn.self_nickname = data.get("nickname", "")
    except Exception as e:
        logger.debug("Failed to fetch self info: %s", e)


async def force_close_ws(self, conn: _NapCatConnection) -> None:
    ws = conn.ws
    if ws is None:
        return
    try:
        await asyncio.wait_for(ws.close(), timeout=3.0)
        return
    except Exception:
        pass
    try:
        if hasattr(ws, 'transport') and ws.transport:
            ws.transport.close()
    except Exception:
        pass
