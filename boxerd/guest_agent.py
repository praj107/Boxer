"""Guest operations: screenshot, exec via SSH, keyboard/mouse input."""
from __future__ import annotations

import asyncio
import base64
import logging
import shlex
import subprocess
import tempfile
import uuid
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

    # ------------------------------------------------------------------ SSH plumbing

    def _resolve_privkey(self, fallback_privkey_path: Optional[Path]) -> Path:
        privkey_path = self._cfg.boxer_ssh_privkey_path
        if (not privkey_path or not privkey_path.exists()) and fallback_privkey_path and fallback_privkey_path.exists():
            privkey_path = fallback_privkey_path
        if not privkey_path or not privkey_path.exists():
            raise IPCError(ERR_INVALID_PARAMS, "No SSH private key configured (boxer_ssh_privkey_path)")
        return privkey_path

    async def _connect(
        self,
        ip_address: str,
        timeout_seconds: int,
        fallback_privkey_path: Optional[Path] = None,
        wait_ssh_seconds: int = 0,
    ) -> Any:
        """Open an SSH connection, retrying for up to wait_ssh_seconds (e.g. across a reboot)."""
        privkey_path = self._resolve_privkey(fallback_privkey_path)
        try:
            import asyncssh
        except ImportError:
            raise IPCError(ERR_INTERNAL, "asyncssh not installed")

        loop = asyncio.get_running_loop()
        deadline = loop.time() + max(0, wait_ssh_seconds)
        while True:
            try:
                return await asyncssh.connect(
                    ip_address,
                    username="boxer",
                    client_keys=[str(privkey_path)],
                    known_hosts=None,
                    connect_timeout=timeout_seconds,
                )
            except (OSError, asyncssh.Error, asyncio.TimeoutError) as exc:
                if loop.time() >= deadline:
                    raise IPCError(ERR_INTERNAL, f"SSH connect failed: {exc}") from exc
                await asyncio.sleep(2)

    async def exec_ssh(
        self,
        ip_address: str,
        command: str,
        timeout_seconds: int = 30,
        fallback_privkey_path: Optional[Path] = None,
        detach: bool = False,
        wait_ssh_seconds: int = 0,
    ) -> dict[str, Any]:
        """Run a command. With detach=True the command keeps running in its own session after
        the call returns (stdin/stdout/stderr detached, output in log_path), so long jobs and
        commands that background children no longer hold the SSH channel open until timeout."""
        log_path = ""
        if detach:
            log_path = f"/tmp/boxer-job-{uuid.uuid4().hex[:12]}.log"
            command = (
                f"setsid nohup sh -c {shlex.quote(command)} > {log_path} 2>&1 < /dev/null & echo $!"
            )
        conn = await self._connect(ip_address, timeout_seconds, fallback_privkey_path, wait_ssh_seconds)
        try:
            async with conn:
                result = await asyncio.wait_for(conn.run(command, check=False), timeout=timeout_seconds)
        except asyncio.TimeoutError:
            raise IPCError(ERR_INTERNAL, f"SSH exec timed out after {timeout_seconds}s")
        except IPCError:
            raise
        except Exception as exc:
            raise IPCError(ERR_INTERNAL, f"SSH exec failed: {exc}") from exc
        out: dict[str, Any] = {
            "stdout": result.stdout or "",
            "stderr": result.stderr or "",
            "exit_code": result.exit_status or 0,
        }
        if detach:
            out["detached"] = True
            out["pid"] = (result.stdout or "").strip()
            out["log_path"] = log_path
        return out

    # ------------------------------------------------------------------ file transfer

    async def push_files(
        self,
        ip_address: str,
        files: list[dict[str, Any]],
        timeout_seconds: int = 120,
        fallback_privkey_path: Optional[Path] = None,
        sudo: bool = False,
    ) -> list[dict[str, Any]]:
        """Write files into the guest over the existing SSH channel (no scp/sftp dependency).

        Each item: {"path": absolute guest path, "data": bytes, "mode": int|None,
        "extract": bool}. With extract=True, data is a .tar.gz unpacked into path (a directory).
        """
        prefix = "sudo -n " if sudo else ""
        written: list[dict[str, Any]] = []
        conn = await self._connect(ip_address, timeout_seconds, fallback_privkey_path)
        try:
            async with conn:
                for item in files:
                    dest = item["path"]
                    q = shlex.quote(dest)
                    if item.get("extract"):
                        tmp = f"/tmp/boxer-push-{uuid.uuid4().hex[:12]}.tgz"
                        cmd = (
                            f"cat > {tmp} && {prefix}mkdir -p {q} && "
                            f"{prefix}tar -xzf {tmp} -C {q}; rc=$?; rm -f {tmp}; exit $rc"
                        )
                    else:
                        cmd = f"{prefix}mkdir -p $(dirname {q}) && {prefix}tee {q} > /dev/null"
                        if item.get("mode") is not None:
                            cmd += f" && {prefix}chmod {int(item['mode']):o} {q}"
                    result = await asyncio.wait_for(
                        conn.run(cmd, input=item["data"], encoding=None, check=False),
                        timeout=timeout_seconds,
                    )
                    if result.exit_status:
                        err = (result.stderr or b"").decode(errors="replace").strip()
                        raise IPCError(ERR_INTERNAL, f"push to {dest} failed: {err or result.exit_status}")
                    written.append({"path": dest, "bytes": len(item["data"]), "extracted": bool(item.get("extract"))})
        except asyncio.TimeoutError:
            raise IPCError(ERR_INTERNAL, f"push timed out after {timeout_seconds}s")
        except IPCError:
            raise
        except Exception as exc:
            raise IPCError(ERR_INTERNAL, f"push failed: {exc}") from exc
        return written

    async def pull_path(
        self,
        ip_address: str,
        path: str,
        max_bytes: int,
        timeout_seconds: int = 120,
        fallback_privkey_path: Optional[Path] = None,
        sudo: bool = False,
    ) -> dict[str, Any]:
        """Read a guest file, or a directory as .tar.gz, capped at max_bytes."""
        prefix = "sudo -n " if sudo else ""
        q = shlex.quote(path)
        cmd = (
            f"if {prefix}test -d {q}; then echo D; {prefix}tar -czf - -C {q} . | head -c {max_bytes + 1}; "
            f"elif {prefix}test -f {q}; then echo F; {prefix}head -c {max_bytes + 1} {q}; "
            f"else echo 'no such file or directory' >&2; exit 2; fi"
        )
        conn = await self._connect(ip_address, timeout_seconds, fallback_privkey_path)
        try:
            async with conn:
                result = await asyncio.wait_for(conn.run(cmd, encoding=None, check=False), timeout=timeout_seconds)
        except asyncio.TimeoutError:
            raise IPCError(ERR_INTERNAL, f"pull timed out after {timeout_seconds}s")
        except IPCError:
            raise
        except Exception as exc:
            raise IPCError(ERR_INTERNAL, f"pull failed: {exc}") from exc
        if result.exit_status:
            err = (result.stderr or b"").decode(errors="replace").strip()
            raise IPCError(ERR_INVALID_PARAMS, f"pull {path} failed: {err or result.exit_status}")
        raw: bytes = result.stdout or b""
        kind, _, data = raw.partition(b"\n")
        if len(data) > max_bytes:
            raise IPCError(ERR_INVALID_PARAMS, f"{path} exceeds the {max_bytes} byte pull limit")
        return {"path": path, "is_archive": kind == b"D", "data": data}

    async def install_ssh_public_key(
        self,
        ip_address: str,
        public_key: str,
        timeout_seconds: int = 30,
    ) -> dict[str, Any]:
        quoted_key = shlex.quote(public_key)
        command = (
            "mkdir -p ~/.ssh && chmod 700 ~/.ssh && "
            "touch ~/.ssh/authorized_keys && chmod 600 ~/.ssh/authorized_keys && "
            f"grep -qxF {quoted_key} ~/.ssh/authorized_keys || "
            f"printf '%s\\n' {quoted_key} >> ~/.ssh/authorized_keys"
        )
        return await self.exec_ssh(ip_address, command, timeout_seconds)

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
