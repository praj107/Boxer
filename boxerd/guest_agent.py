"""Guest operations: screenshot, exec via SSH, keyboard/mouse input."""
from __future__ import annotations

import asyncio
import base64
import logging
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Optional

import libvirt

from boxer.config import BoxerConfig, get_config
from boxer.ipc import ERR_INTERNAL, ERR_INVALID_PARAMS, IPCError

logger = logging.getLogger(__name__)


class GuestAgent:
    def __init__(self, conn: libvirt.virConnect, cfg: Optional[BoxerConfig] = None):
        self._conn = conn
        self._cfg = cfg or get_config()

    async def screenshot(self, libvirt_name: str) -> str:
        """Return PNG screenshot as base64 string."""
        try:
            dom = self._conn.lookupByName(libvirt_name)
            if not dom.isActive():
                raise IPCError(ERR_INVALID_PARAMS, "VM is not running")
        except libvirt.libvirtError as exc:
            raise IPCError(ERR_INVALID_PARAMS, f"Domain not found: {exc}") from exc

        with tempfile.TemporaryDirectory() as tmp:
            ppm_path = Path(tmp) / "screen.ppm"
            png_path = Path(tmp) / "screen.png"

            loop = asyncio.get_running_loop()
            await loop.run_in_executor(None, self._virsh_screenshot, libvirt_name, str(ppm_path))
            await loop.run_in_executor(None, self._convert_to_png, str(ppm_path), str(png_path))

            return base64.b64encode(png_path.read_bytes()).decode()

    def _virsh_screenshot(self, name: str, dest: str) -> None:
        uri = self._cfg.libvirt_uri
        result = subprocess.run(
            ["virsh", "-c", uri, "screenshot", name, dest],
            capture_output=True,
        )
        if result.returncode != 0:
            raise IPCError(ERR_INTERNAL, f"virsh screenshot failed: {result.stderr.decode()}")

    @staticmethod
    def _convert_to_png(src: str, dest: str) -> None:
        try:
            # Try convert (ImageMagick) first
            result = subprocess.run(["convert", src, dest], capture_output=True)
            if result.returncode == 0:
                return
        except FileNotFoundError:
            pass
        # Fall back to Python PIL
        try:
            from PIL import Image
            img = Image.open(src)
            img.save(dest, "PNG")
        except Exception as exc:
            raise IPCError(ERR_INTERNAL, f"Failed to convert screenshot: {exc}") from exc

    async def exec_ssh(
        self,
        ip_address: str,
        command: str,
        timeout_seconds: int = 30,
    ) -> dict[str, Any]:
        privkey_path = self._cfg.boxer_ssh_privkey_path
        if not privkey_path or not privkey_path.exists():
            raise IPCError(ERR_INVALID_PARAMS, "No SSH private key configured (boxer_ssh_privkey_path)")

        try:
            import asyncssh
        except ImportError:
            raise IPCError(ERR_INTERNAL, "asyncssh not installed")

        try:
            async with asyncssh.connect(
                ip_address,
                username="boxer",
                client_keys=[str(privkey_path)],
                known_hosts=None,
                connect_timeout=timeout_seconds,
            ) as conn:
                result = await asyncio.wait_for(
                    conn.run(command, check=False),
                    timeout=timeout_seconds,
                )
                return {
                    "stdout": result.stdout or "",
                    "stderr": result.stderr or "",
                    "exit_code": result.exit_status or 0,
                }
        except asyncio.TimeoutError:
            raise IPCError(ERR_INTERNAL, f"SSH exec timed out after {timeout_seconds}s")
        except Exception as exc:
            raise IPCError(ERR_INTERNAL, f"SSH exec failed: {exc}") from exc

    async def send_keys(self, libvirt_name: str, keys: list[str]) -> None:
        uri = self._cfg.libvirt_uri
        loop = asyncio.get_running_loop()
        for key in keys:
            await loop.run_in_executor(
                None,
                lambda k=key: subprocess.run(
                    ["virsh", "-c", uri, "send-key", libvirt_name, "KEY_" + k.upper()],
                    check=True,
                    capture_output=True,
                ),
            )

    async def send_input(self, libvirt_name: str, actions: list[dict[str, Any]]) -> None:
        """Process a list of input actions: {type: 'key'|'type'|'mouse', ...}"""
        for action in actions:
            atype = action.get("type")
            if atype == "key":
                await self.send_keys(libvirt_name, action.get("keys", []))
            elif atype == "type":
                text = action.get("text", "")
                keys = list(text)
                await self.send_keys(libvirt_name, keys)
            elif atype == "mouse":
                # Mouse input via virsh inject-nmi is limited; log and skip
                logger.debug("Mouse input not fully supported yet: %s", action)
            else:
                logger.warning("Unknown input action type: %s", atype)
