"""Unix socket JSON-RPC 2.0 server — dispatches to registered handlers."""
from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path
from typing import Any, Callable, Coroutine, Optional

from boxer.ipc import (
    ERR_INTERNAL,
    ERR_METHOD_NOT_FOUND,
    ERR_PARSE,
    IPCError,
    make_error_response,
    make_response,
    read_message,
    write_message,
)

logger = logging.getLogger(__name__)

Handler = Callable[[dict[str, Any]], Coroutine[Any, Any, Any]]


class IPCServer:
    def __init__(self, socket_path: Path, notify_socket_path: Optional[Path] = None):
        self._socket_path = socket_path
        self._notify_socket_path = notify_socket_path
        self._handlers: dict[str, Handler] = {}
        self._notify_handlers: dict[str, Handler] = {}
        self._server: Optional[asyncio.AbstractServer] = None
        self._notify_server: Optional[asyncio.AbstractServer] = None

    def register(self, method: str, handler: Handler) -> None:
        self._handlers[method] = handler

    def register_notify(self, method: str, handler: Handler) -> None:
        """Register a handler on the read-only notification socket."""
        self._notify_handlers[method] = handler

    async def start(self) -> None:
        self._socket_path.parent.mkdir(parents=True, exist_ok=True)
        if self._socket_path.exists():
            self._socket_path.unlink()
        self._server = await asyncio.start_unix_server(
            self._handle_connection, str(self._socket_path)
        )
        os.chmod(str(self._socket_path), 0o660)
        logger.info("IPC server listening on %s", self._socket_path)

        if self._notify_socket_path:
            if self._notify_socket_path.exists():
                self._notify_socket_path.unlink()
            self._notify_server = await asyncio.start_unix_server(
                self._handle_notify_connection, str(self._notify_socket_path)
            )
            os.chmod(str(self._notify_socket_path), 0o666)
            logger.info("Notify socket on %s", self._notify_socket_path)

    async def stop(self) -> None:
        for srv in (self._server, self._notify_server):
            if srv:
                srv.close()
                await srv.wait_closed()

    async def _handle_connection(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        await self._serve_loop(reader, writer, self._handlers)

    async def _handle_notify_connection(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        await self._serve_loop(reader, writer, self._notify_handlers)

    async def _serve_loop(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        handlers: dict[str, Handler],
    ) -> None:
        try:
            while True:
                try:
                    msg = await read_message(reader)
                except (ConnectionResetError, asyncio.IncompleteReadError):
                    break
                except Exception as exc:
                    await write_message(
                        writer, make_error_response(None, ERR_PARSE, str(exc))
                    )
                    break

                req_id = msg.get("id")
                method = msg.get("method")
                params = msg.get("params", {})

                if method not in handlers:
                    await write_message(
                        writer,
                        make_error_response(req_id, ERR_METHOD_NOT_FOUND, f"unknown method: {method}"),
                    )
                    continue

                try:
                    result = await handlers[method](params)
                    await write_message(writer, make_response(req_id, result))
                except IPCError as exc:
                    await write_message(
                        writer, make_error_response(req_id, exc.code, exc.message, exc.data)
                    )
                except Exception as exc:
                    logger.exception("Error in handler %s", method)
                    await write_message(
                        writer, make_error_response(req_id, ERR_INTERNAL, str(exc))
                    )
        finally:
            writer.close()
