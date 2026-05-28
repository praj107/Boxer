"""Async IPC client connecting to boxerd Unix socket."""
from __future__ import annotations

import asyncio
import uuid
from pathlib import Path
from typing import Any, Optional

from boxer.ipc import IPCError, make_request, read_message


class IPCClient:
    def __init__(self, socket_path: Path):
        self._socket_path = socket_path
        self._reader: Optional[asyncio.StreamReader] = None
        self._writer: Optional[asyncio.StreamWriter] = None
        self._lock = asyncio.Lock()

    async def connect(self) -> None:
        self._reader, self._writer = await asyncio.open_unix_connection(str(self._socket_path))

    async def close(self) -> None:
        if self._writer:
            self._writer.close()
            try:
                await self._writer.wait_closed()
            except Exception:
                pass

    async def call(self, method: str, params: dict[str, Any]) -> Any:
        req_id = str(uuid.uuid4())
        async with self._lock:
            assert self._writer and self._reader, "Not connected"
            self._writer.write(make_request(method, params, req_id))
            await self._writer.drain()
            msg = await read_message(self._reader)

        if "error" in msg:
            err = msg["error"]
            raise IPCError(err["code"], err["message"], err.get("data"))
        return msg.get("result")

    async def __aenter__(self) -> "IPCClient":
        await self.connect()
        return self

    async def __aexit__(self, *_: Any) -> None:
        await self.close()


async def make_call(socket_path: Path, method: str, params: dict[str, Any]) -> Any:
    """Single-call helper that opens a fresh connection."""
    async with IPCClient(socket_path) as client:
        return await client.call(method, params)
