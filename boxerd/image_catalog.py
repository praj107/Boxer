"""Allowlisted image fetching with SHA256 verification."""
from __future__ import annotations

import asyncio
import hashlib
import ipaddress
import logging
import socket
import tempfile
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

import httpx

from boxer.config import BoxerConfig, ImageCatalog, get_catalog, get_config
from boxer.ipc import ERR_INVALID_PARAMS, ERR_INTERNAL, IPCError

logger = logging.getLogger(__name__)

_BLOCKED_NETWORKS = [
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("127.0.0.0/8"),
    ipaddress.ip_network("169.254.0.0/16"),
    ipaddress.ip_network("::1/128"),
    ipaddress.ip_network("fc00::/7"),
]


def _is_private_ip(ip: str) -> bool:
    try:
        addr = ipaddress.ip_address(ip)
        return any(addr in net for net in _BLOCKED_NETWORKS)
    except ValueError:
        return True


def _validate_url(url: str) -> None:
    parsed = urlparse(url)
    if parsed.scheme != "https":
        raise IPCError(ERR_INVALID_PARAMS, "Only https:// URLs are allowed")
    hostname = parsed.hostname
    if not hostname:
        raise IPCError(ERR_INVALID_PARAMS, "Invalid URL: no hostname")
    try:
        addrs = socket.getaddrinfo(hostname, None)
        for _, _, _, _, sockaddr in addrs:
            ip = sockaddr[0]
            if _is_private_ip(ip):
                raise IPCError(ERR_INVALID_PARAMS, f"URL resolves to private/reserved IP: {ip}")
    except IPCError:
        raise
    except Exception as exc:
        raise IPCError(ERR_INVALID_PARAMS, f"DNS resolution failed: {exc}")


async def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    loop = asyncio.get_running_loop()
    with open(path, "rb") as f:
        while True:
            chunk = await loop.run_in_executor(None, f.read, 1 << 20)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


class ImageManager:
    def __init__(self, cfg: Optional[BoxerConfig] = None, catalog: Optional[ImageCatalog] = None):
        self._cfg = cfg or get_config()
        self._catalog = catalog or get_catalog()

    def _base_path(self, name: str) -> Path:
        return self._cfg.images_dir / name / "base.qcow2"

    def _checksum_path(self, name: str) -> Path:
        return self._cfg.images_dir / name / "checksum.txt"

    def is_cached(self, name: str) -> bool:
        p = self._base_path(name)
        return p.exists() and self._checksum_path(name).exists()

    async def ensure_image(self, name: str) -> Path:
        entry = self._catalog.get(name)
        if entry is None:
            raise IPCError(ERR_INVALID_PARAMS, f"Unknown template: {name}. Available: {self._catalog.list_names()}")

        base_path = self._base_path(name)
        if self.is_cached(name):
            logger.debug("Image %s already cached at %s", name, base_path)
            return base_path

        url = entry["url"]
        _validate_url(url)
        expected_sha256: Optional[str] = entry.get("sha256")

        logger.info("Fetching image %s from %s", name, url)
        base_path.parent.mkdir(parents=True, exist_ok=True)

        tmp_path = Path(tempfile.mktemp(dir=base_path.parent, suffix=".tmp"))
        try:
            await self._download(url, tmp_path)
            actual_sha256 = await _sha256_file(tmp_path)

            if expected_sha256 and expected_sha256 != actual_sha256:
                raise IPCError(
                    ERR_INTERNAL,
                    f"SHA256 mismatch for {name}: expected {expected_sha256}, got {actual_sha256}",
                )

            tmp_path.rename(base_path)
            self._checksum_path(name).write_text(actual_sha256)
            logger.info("Image %s cached, sha256=%s", name, actual_sha256)
            return base_path
        except Exception:
            if tmp_path.exists():
                tmp_path.unlink(missing_ok=True)
            raise

    async def _download(self, url: str, dest: Path) -> None:
        async with httpx.AsyncClient(follow_redirects=True, timeout=3600) as client:
            async with client.stream("GET", url) as resp:
                resp.raise_for_status()
                with open(dest, "wb") as f:
                    async for chunk in resp.aiter_bytes(1 << 16):
                        f.write(chunk)
