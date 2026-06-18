from __future__ import annotations

import os
from pathlib import Path
from typing import Dict, List
from urllib.parse import unquote as url_unquote

import onebot_platform.adapter_runtime as _runtime
globals().update({k: v for k, v in vars(_runtime).items() if not k.startswith('__')})


async def inject_file_content(self, segments: List[Dict], text: str, conn) -> str:
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
        if os.path.getsize(local_path) > FILE_INJECTION_MAX_BYTES:
            logger.debug("File too large for injection: %s", local_path)
            continue
        try:
            with open(local_path, "r", errors="replace") as f:
                file_content = f.read(FILE_INJECTION_MAX_BYTES)
            injection = f"[Content of {os.path.basename(local_path)}]:\n{file_content}"
            injected = f"{injection}\n\n{injected}" if injected.strip() else injection
        except Exception:
            pass
    return injected


async def resolve_file_url(self, data: dict, conn) -> str:
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
            return f"file://{url}" if os.path.isabs(str(url)) else str(url)
    return ""
