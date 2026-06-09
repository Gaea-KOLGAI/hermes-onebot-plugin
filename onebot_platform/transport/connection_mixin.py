from __future__ import annotations

import onebot_platform.adapter_runtime as _runtime
globals().update({k: v for k, v in vars(_runtime).items() if not k.startswith('__')})
from onebot_platform.transport import lifecycle as _lifecycle

class ConnectionMixin:
    def _set_fatal_if_default(self, conn, error_type: str, msg: str, retryable: bool = False):
        if conn is self._default_conn:
            self._set_fatal_error(error_type, msg, retryable=retryable)
    async def _connect_conn_by_mode(self, conn: _NapCatConnection) -> bool:
        if conn.ws_mode == "reverse":
            return await self._connect_reverse_conn(conn)
        return await self._connect_forward_conn(conn)
    async def connect(self) -> bool:
        await self._ensure_settings_loaded()
        self._shutting_down = False
        if len(self._connections) == 1 and not self._multi_account:
            return await self._connect_conn_by_mode(self._default_conn)
        any_connected = False
        for name, conn in self._connections.items():
            ok = await self._connect_conn_by_mode(conn)
            if ok:
                any_connected = True
        if any_connected:
            self._mark_connected()
        return any_connected
    def _check_ws_prereqs(self, conn: _NapCatConnection) -> bool:
        if not conn.ws_url:
            self._set_fatal_if_default(conn, "config_missing", "ONEBOT_WS_URL must be set", retryable=False)
            return False
        if not WEBSOCKETS_AVAILABLE:
            self._set_fatal_if_default(conn, "missing_dependency", "pip install websockets", retryable=False)
            return False
        return True
    async def _connect_forward_conn(self, conn: _NapCatConnection) -> bool:
        if conn.ws and conn.ws.close_code is None:
            return True
        if not self._check_ws_prereqs(conn):
            return False
        headers = {"Authorization": f"Bearer {conn.access_token}"} if conn.access_token else None
        try:
            conn.ws = await _websockets_connect(
                conn.ws_url, headers=headers, timeout=15.0, **_WS_CONNECT_KWARGS
            )
        except Exception as e:
            self._set_fatal_if_default(conn, "connect_failed", str(e), retryable=True)
            return False
        conn.connected_since = conn.last_heartbeat = time.time()
        conn.recv_task = asyncio.create_task(self._receive_loop_conn(conn))
        conn.heartbeat_task = asyncio.create_task(self._heartbeat_monitor_conn(conn))
        self._mark_connected()
        asyncio.create_task(self._fetch_self_info_conn(conn))
        return True
    async def _connect_reverse_conn(self, conn: _NapCatConnection) -> bool:
        if not self._check_ws_prereqs(conn):
            return False
        parsed = urlparse(conn.ws_url)
        host = parsed.hostname or "0.0.0.0"
        port = parsed.port or 8082
        async def handler(websocket, path=None):
            await self._handle_reverse_ws_client(conn, websocket)
        try:
            conn.ws_server = await websockets.serve(
                handler, host, port,
                ping_interval=30,
                ping_timeout=10,
                max_size=10 * 1024 * 1024,
            )
            return True
        except Exception as e:
            self._set_fatal_if_default(conn, "connect_failed", str(e), retryable=True)
            return False
    async def _handle_reverse_ws_client(self, conn: _NapCatConnection, websocket) -> None:
        if conn.access_token:
            if not hmac.compare_digest(_ws_authorization(websocket), f"Bearer {conn.access_token}"):
                try:
                    await websocket.close(4001, "Unauthorized")
                except Exception:
                    pass
                return
        else:
            if "reverse_ws_no_token" not in conn._warnings:
                logger.info("OneBot reverse WebSocket has no access token; accepting local NapCat connections")
                conn._warnings.add("reverse_ws_no_token")
        conn.ws = websocket
        conn.reverse_ws_clients.add(websocket)
        conn.connected_since = time.time()
        conn.last_heartbeat = time.time()
        self._mark_connected()
        try:
            await self._cancel_conn_tasks(conn, "recv_task", "heartbeat_task")
            conn.recv_task = asyncio.create_task(self._receive_loop_conn(conn))
            conn.heartbeat_task = asyncio.create_task(self._heartbeat_monitor_conn(conn))
            asyncio.create_task(self._fetch_self_info_conn(conn))
            await websocket.wait_closed()
        finally:
            conn.reverse_ws_clients.discard(websocket)
            if conn.ws is websocket:
                if conn.reverse_ws_clients:
                    conn.ws = next(iter(conn.reverse_ws_clients))
                    await self._cancel_conn_tasks(conn, "recv_task", "heartbeat_task")
                    conn.recv_task = asyncio.create_task(self._receive_loop_conn(conn))
                    conn.heartbeat_task = asyncio.create_task(self._heartbeat_monitor_conn(conn))
                else:
                    conn.ws = None
            if not conn.reverse_ws_clients and conn is self._default_conn:
                self._mark_disconnected()
    async def disconnect(self) -> None:
        self._shutting_down = True
        self._running = False
        bg_tasks = list(getattr(self, "_bg_delete_tasks", set()) or [])
        for task in bg_tasks:
            if task and not task.done():
                task.cancel()
        for task in bg_tasks:
            try:
                await task
            except asyncio.CancelledError:
                pass
            except Exception:
                pass
        if hasattr(self, "_bg_delete_tasks"):
            self._bg_delete_tasks.clear()
        for name, conn in self._connections.items():
            await self._disconnect_conn(conn)
        if self._http_client:
            try:
                await self._http_client.aclose()
            except Exception:
                pass
            self._http_client = None
        self._mark_disconnected()
    _cancel_conn_tasks = _lifecycle.cancel_conn_tasks
    _disconnect_conn = _lifecycle.disconnect_conn
    _fetch_self_info_conn = _lifecycle.fetch_self_info_conn
    _force_close_ws = _lifecycle.force_close_ws
    async def _heartbeat_monitor_conn(self, conn: _NapCatConnection) -> None:
        while self._running:
            await asyncio.sleep(15)
            if not self._running:
                return
            ws = conn.ws
            if ws is None or ws.close_code is not None:
                break
            elapsed = time.time() - conn.last_heartbeat
            if elapsed > HEARTBEAT_TIMEOUT:
                if conn.recv_task and not conn.recv_task.done():
                    conn.recv_task.cancel()
                await self._force_close_ws(conn)
                if conn.ws_mode == "forward":
                    if not conn.reconnect_task or conn.reconnect_task.done():
                        conn.reconnect_task = asyncio.create_task(self._reconnect_loop_conn(conn))
                break
    async def _reconnect_loop_conn(self, conn: _NapCatConnection) -> None:
        attempt = 0
        while self._running:
            delay = min(RECONNECT_BASE_DELAY * (2 ** attempt), RECONNECT_MAX_DELAY)
            jitter = delay * 0.2 * (_random.random() - 0.5)
            wait = max(1.0, delay + jitter)
            await asyncio.sleep(wait)
            if not self._running:
                return
            await self._cancel_conn_tasks(conn, "recv_task", "heartbeat_task", clear=True)
            if conn.ws:
                try:
                    await asyncio.wait_for(conn.ws.close(), timeout=3.0)
                except Exception:
                    pass
                conn.ws = None
            try:
                ok = await self._connect_forward_conn(conn)
                if ok:
                    return
            except Exception as e:
                pass
            attempt += 1
    def _dispatch_for_chat(self, chat_id: str, coro, *, notice: bool = False) -> None:
        key = (chat_id + ":notice") if notice else chat_id
        # Limit concurrent tasks per chat to prevent unbounded growth
        chat_tasks = [(k, t) for k, t in self._active_tasks.items()
                      if k.startswith(chat_id) and not t.done()]
        while len(chat_tasks) >= MAX_TASKS_PER_CHAT:
            # Cancel the oldest task (dict preserves insertion order)
            oldest_key, oldest_task = chat_tasks.pop(0)
            self._active_tasks.pop(oldest_key, None)
            oldest_task.cancel()
        async def _wrapper():
            try:
                await coro
            except asyncio.CancelledError:
                pass
            except Exception as e:
                logger.debug("Task exception in %s: %s", key, e)
            finally:
                if self._active_tasks.get(key) is asyncio.current_task():
                    self._active_tasks.pop(key, None)
        task = asyncio.create_task(_wrapper())
        self._active_tasks[key] = task
    async def _receive_loop_conn(self, conn: _NapCatConnection) -> None:
        while self._running:
            ws = conn.ws
            if ws is None or ws.close_code is not None:
                break
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=RECV_TIMEOUT)
            except asyncio.TimeoutError:
                now = time.time()
                stale_keys = [k for k, ts in conn._echo_timestamps.items() if now - ts > ECHO_STALE_TIMEOUT]
                for k in stale_keys:
                    fut = conn.echo_futures.pop(k, None)
                    conn._echo_timestamps.pop(k, None)
                    if fut and not fut.done():
                        fut.set_exception(asyncio.TimeoutError("echo stale"))
                continue
            except websockets.ConnectionClosed as e:
                break
            except asyncio.CancelledError:
                return
            except Exception as e:
                if self._running:
                    await asyncio.sleep(1)
                continue
            try:
                data = json.loads(raw)
            except (json.JSONDecodeError, TypeError):
                continue
            try:
                await self._process_event_conn(conn, data)
            except asyncio.CancelledError:
                return
            except Exception as e:
                logger.debug("Event processing error: %s", e)
        if self._running and conn.ws_mode == "forward":
            if not conn.reconnect_task or conn.reconnect_task.done():
                if not getattr(self, '_shutting_down', False):
                    self._running = True
                conn.reconnect_task = asyncio.create_task(self._reconnect_loop_conn(conn))
    async def _process_event_conn(self, conn: _NapCatConnection, data: dict) -> None:
        if "self_id" in data and not conn.self_id:
            conn.self_id = str(data["self_id"])
        if self._resolve_echo_response(conn, data):
            return
        handlers = {
            "meta_event": self._handle_meta_event_conn,
            "message": self._handle_message_event_conn,
            "notice": self._handle_notice_event_conn,
            "request": self._handle_request_event_conn,
        }
        handler = handlers.get(data.get("post_type", ""))
        if handler:
            await handler(conn, data)
    def _resolve_echo_response(self, conn: _NapCatConnection, data: dict) -> bool:
        echo = data.get("echo")
        if not echo or echo not in conn.echo_futures:
            return False
        fut = conn.echo_futures.pop(echo)
        conn._echo_timestamps.pop(echo, None)
        if not fut.done():
            fut.set_result(data)
        return True
    async def _handle_meta_event_conn(self, conn: _NapCatConnection, data: dict) -> None:
        sub = data.get("meta_event_type", "")
        if sub == "heartbeat":
            conn.last_heartbeat = time.time()
        elif sub == "lifecycle" and data.get("sub_type", "") == "connect":
            asyncio.create_task(self._fetch_self_info_conn(conn))
    async def _handle_message_event_conn(self, conn: _NapCatConnection, data: dict) -> None:
        account_name = conn.name if self._multi_account else ""
        chat_id = _make_chat_id(data, account_name)
        self._dispatch_for_chat(chat_id, self._handle_message(data, conn=conn))
    async def _handle_notice_event_conn(self, conn: _NapCatConnection, data: dict) -> None:
        self._dispatch_for_chat(f"notice:{data.get('notice_type', '')}", self._handle_notice(data, conn), notice=True)
    async def _handle_request_event_conn(self, conn: _NapCatConnection, data: dict) -> None:
        self._dispatch_for_chat(f"request:{data.get('request_type', '')}", self._handle_request(data, conn), notice=True)
