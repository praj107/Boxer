"""Newline-delimited JSON-RPC 2.0 framing over a Unix domain socket."""
from __future__ import annotations

import asyncio
import json
import uuid
from typing import Any, Optional


class IPCError(Exception):
    def __init__(self, code: int, message: str, data: Any = None):
        self.code = code
        self.message = message
        self.data = data
        super().__init__(f"[{code}] {message}")


# JSON-RPC error codes
ERR_PARSE = -32700
ERR_INVALID_REQUEST = -32600
ERR_METHOD_NOT_FOUND = -32601
ERR_INVALID_PARAMS = -32602
ERR_INTERNAL = -32603
ERR_PERMISSION_DENIED = -32001
ERR_NOT_FOUND = -32002
ERR_RESOURCE_EXHAUSTED = -32003
ERR_POLICY_VIOLATION = -32004


def make_request(method: str, params: dict[str, Any], req_id: Optional[str] = None) -> bytes:
    msg = {
        "jsonrpc": "2.0",
        "id": req_id or str(uuid.uuid4()),
        "method": method,
        "params": params,
    }
    return (json.dumps(msg) + "\n").encode()


def make_response(req_id: str, result: Any) -> bytes:
    msg = {"jsonrpc": "2.0", "id": req_id, "result": result}
    return (json.dumps(msg) + "\n").encode()


def make_error_response(req_id: Optional[str], code: int, message: str, data: Any = None) -> bytes:
    err: dict[str, Any] = {"code": code, "message": message}
    if data is not None:
        err["data"] = data
    msg = {"jsonrpc": "2.0", "id": req_id, "error": err}
    return (json.dumps(msg) + "\n").encode()


async def read_message(reader: asyncio.StreamReader) -> dict[str, Any]:
    line = await reader.readline()
    if not line:
        raise ConnectionResetError("connection closed")
    return json.loads(line.decode())


async def write_message(writer: asyncio.StreamWriter, data: bytes) -> None:
    writer.write(data)
    await writer.drain()
