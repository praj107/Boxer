"""Allowlisted image fetching with SHA256 verification."""
from __future__ import annotations

import asyncio
import hashlib
import ipaddress
import json
import logging
import re
import socket
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
from urllib.parse import urljoin, urlparse

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

_SUPPORTED_HASHES = {"sha256", "sha512"}
_HASH_LENGTHS = {"sha256": 64, "sha512": 128}
_FEDORA_STYLE_RE = re.compile(
    r"^(?P<algorithm>SHA(?:256|512))\s*\((?P<filename>[^)]+)\)\s*=\s*(?P<digest>[0-9a-fA-F]+)$"
)


@dataclass(frozen=True)
class ExpectedDigest:
    algorithm: str
    digest: str
    source: str


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


def _artifact_type(entry: dict) -> str:
    return str(entry.get("artifact_type") or entry.get("type") or "cloud-image")


def _validate_hash_algorithm(algorithm: str) -> str:
    normalized = algorithm.lower()
    if normalized not in _SUPPORTED_HASHES:
        raise IPCError(
            ERR_INVALID_PARAMS,
            f"Unsupported checksum algorithm '{algorithm}'. Supported: {sorted(_SUPPORTED_HASHES)}",
        )
    return normalized


async def _hash_file(path: Path, algorithm: str) -> str:
    algorithm = _validate_hash_algorithm(algorithm)
    h = hashlib.new(algorithm)
    loop = asyncio.get_running_loop()
    with open(path, "rb") as f:
        while True:
            chunk = await loop.run_in_executor(None, f.read, 1 << 20)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


def _manifest_filename(url: str, override: Optional[str] = None) -> str:
    return override or Path(urlparse(url).path).name


def _parse_checksum_manifest(text: str, filename: str, algorithm: str = "sha256") -> str:
    algorithm = _validate_hash_algorithm(algorithm)
    wanted = Path(filename).name

    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue

        fedora_match = _FEDORA_STYLE_RE.match(line)
        if fedora_match:
            if fedora_match.group("algorithm").lower() != algorithm:
                continue
            candidate = Path(fedora_match.group("filename").strip()).name
            digest = fedora_match.group("digest").lower()
            if candidate == wanted and len(digest) == _HASH_LENGTHS[algorithm]:
                return digest
            continue

        parts = line.split()
        if len(parts) < 2:
            continue
        digest = parts[0].lower()
        candidate = Path(parts[-1].lstrip("*")).name
        if (
            candidate == wanted
            and len(digest) == _HASH_LENGTHS[algorithm]
            and re.fullmatch(r"[0-9a-f]+", digest)
        ):
            return digest

    raise IPCError(
        ERR_INVALID_PARAMS,
        f"Checksum manifest does not contain an entry for '{wanted}'",
    )


class ImageManager:
    def __init__(self, cfg: Optional[BoxerConfig] = None, catalog: Optional[ImageCatalog] = None):
        self._cfg = cfg or get_config()
        self._catalog = catalog or get_catalog()

    def _base_path(self, name: str) -> Path:
        return self._cfg.images_dir / name / "base.qcow2"

    def _checksum_path(self, name: str) -> Path:
        return self._cfg.images_dir / name / "checksum.txt"

    def _metadata_path(self, name: str) -> Path:
        return self._cfg.images_dir / name / "metadata.json"

    def is_cached(self, name: str) -> bool:
        p = self._base_path(name)
        return p.exists() and self._checksum_path(name).exists()

    async def ensure_image(self, name: str) -> Path:
        entry = self._catalog.get(name)
        if entry is None:
            raise IPCError(ERR_INVALID_PARAMS, f"Unknown template: {name}. Available: {self._catalog.list_names()}")

        artifact_type = _artifact_type(entry)
        if artifact_type != "cloud-image":
            raise IPCError(
                ERR_INVALID_PARAMS,
                f"Template '{name}' is type '{artifact_type}'. Boxer VM creation currently supports cloud-image templates only; installer ISO support is on the roadmap.",
            )

        base_path = self._base_path(name)
        url = entry["url"]
        _validate_url(url)
        expected = await self._expected_digest(entry, url)

        cached = self._read_cached_digest(name)
        if self.is_cached(name) and self._cache_matches_expected(cached, expected):
            logger.debug("Image %s already cached at %s", name, base_path)
            return base_path
        if self.is_cached(name) and expected is not None:
            logger.info("Cached image %s no longer matches expected digest; refreshing", name)
            base_path.unlink(missing_ok=True)
            self._checksum_path(name).unlink(missing_ok=True)

        logger.info("Fetching image %s from %s", name, url)
        base_path.parent.mkdir(parents=True, exist_ok=True)

        tmp_path = Path(tempfile.mktemp(dir=base_path.parent, suffix=".tmp"))
        try:
            await self._download(url, tmp_path)
            algorithm = expected.algorithm if expected else "sha256"
            actual_digest = await _hash_file(tmp_path, algorithm)

            if expected and expected.digest != actual_digest:
                raise IPCError(
                    ERR_INTERNAL,
                    f"{algorithm.upper()} mismatch for {name}: expected {expected.digest}, got {actual_digest}",
                )

            tmp_path.rename(base_path)
            self._write_cache_metadata(name, url, algorithm, actual_digest, expected)
            logger.info("Image %s cached, %s=%s", name, algorithm, actual_digest)
            return base_path
        except Exception:
            if tmp_path.exists():
                tmp_path.unlink(missing_ok=True)
            raise

    async def _expected_digest(self, entry: dict, url: str) -> Optional[ExpectedDigest]:
        sha256 = entry.get("sha256")
        if sha256:
            return ExpectedDigest("sha256", str(sha256).lower(), "catalog sha256")

        verification = entry.get("verification") or {}
        checksum_url = verification.get("checksum_url") or entry.get("checksum_url")
        if not checksum_url:
            return None

        algorithm = _validate_hash_algorithm(
            verification.get("checksum_algorithm") or entry.get("checksum_algorithm") or "sha256"
        )
        filename = _manifest_filename(
            url,
            verification.get("checksum_filename") or entry.get("checksum_filename"),
        )
        _validate_url(checksum_url)
        manifest = await self._download_text(checksum_url)
        digest = _parse_checksum_manifest(manifest, filename, algorithm)
        return ExpectedDigest(algorithm, digest, f"checksum manifest {checksum_url}")

    def _read_cached_digest(self, name: str) -> Optional[ExpectedDigest]:
        path = self._checksum_path(name)
        if not path.exists():
            return None
        raw = path.read_text().strip()
        if not raw:
            return None
        if ":" in raw:
            algorithm, digest = raw.split(":", 1)
            return ExpectedDigest(_validate_hash_algorithm(algorithm), digest.lower(), "cache")
        return ExpectedDigest("sha256", raw.lower(), "legacy cache")

    @staticmethod
    def _cache_matches_expected(
        cached: Optional[ExpectedDigest],
        expected: Optional[ExpectedDigest],
    ) -> bool:
        if cached is None:
            return expected is None
        if expected is None:
            return True
        return cached.algorithm == expected.algorithm and cached.digest == expected.digest

    def _write_cache_metadata(
        self,
        name: str,
        url: str,
        algorithm: str,
        digest: str,
        expected: Optional[ExpectedDigest],
    ) -> None:
        self._checksum_path(name).write_text(f"{algorithm}:{digest}\n")
        metadata = {
            "template": name,
            "url": url,
            "algorithm": algorithm,
            "digest": digest,
            "verified_at": datetime.now(timezone.utc).isoformat(),
            "verification_source": expected.source if expected else "download hash only",
        }
        self._metadata_path(name).write_text(json.dumps(metadata, indent=2) + "\n")

    async def _download(self, url: str, dest: Path) -> None:
        async with httpx.AsyncClient(follow_redirects=False, timeout=3600) as client:
            resp = await self._stream_validated(client, url)
            try:
                with open(dest, "wb") as f:
                    async for chunk in resp.aiter_bytes(1 << 16):
                        f.write(chunk)
            finally:
                await resp.aclose()

    async def _download_text(self, url: str) -> str:
        async with httpx.AsyncClient(follow_redirects=False, timeout=120) as client:
            resp = await self._stream_validated(client, url)
            try:
                chunks = []
                async for chunk in resp.aiter_bytes(1 << 16):
                    chunks.append(chunk)
                return b"".join(chunks).decode("utf-8")
            finally:
                await resp.aclose()

    async def _stream_validated(self, client: httpx.AsyncClient, url: str) -> httpx.Response:
        current_url = url
        for _ in range(6):
            _validate_url(current_url)
            request = client.build_request("GET", current_url)
            resp = await client.send(request, stream=True)
            if 300 <= resp.status_code < 400:
                location = resp.headers.get("location")
                await resp.aclose()
                if not location:
                    raise IPCError(ERR_INTERNAL, f"Redirect without Location from {current_url}")
                current_url = urljoin(current_url, location)
                continue
            try:
                resp.raise_for_status()
            except Exception:
                await resp.aclose()
                raise
            return resp
        raise IPCError(ERR_INTERNAL, f"Too many redirects while fetching {url}")
