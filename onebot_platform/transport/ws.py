from __future__ import annotations

import asyncio
from typing import Optional

try:
    import websockets
    WEBSOCKETS_AVAILABLE = True
except ImportError:
    WEBSOCKETS_AVAILABLE = False
    websockets = None

WS_CONNECT_KWARGS = dict(
    ping_interval=None,
    ping_timeout=None,
    close_timeout=5,
    max_size=10 * 1024 * 1024,
)


async def _websockets_connect(uri: str, *, headers: Optional[dict] = None, timeout: float = 15.0, **kwargs):
    connect_kwargs = {**kwargs}
    if headers:
        connect_kwargs["additional_headers"] = headers
    try:
        return await asyncio.wait_for(websockets.connect(uri, **connect_kwargs), timeout=timeout)
    except TypeError as e:
        if headers and "additional_headers" in str(e):
            connect_kwargs.pop("additional_headers", None)
            connect_kwargs["extra_headers"] = headers
            return await asyncio.wait_for(websockets.connect(uri, **connect_kwargs), timeout=timeout)
        raise


def _ws_authorization(websocket) -> str:
    for source in (
        getattr(getattr(websocket, "request", None), "headers", None),
        getattr(websocket, "request_headers", None),
    ):
        try:
            if source:
                return source.get("Authorization", "")
        except Exception:
            pass
    return ""
