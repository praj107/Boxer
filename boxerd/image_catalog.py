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
from boxerd.signature import (
    SignaturePolicy,
    VerificationResult,
    check_fingerprints,
    extract_clearsigned_payload,
    parse_signature_policy,
    resolve_keyring_path,
    verify_clearsigned,
    verify_detached,
)

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
    signature: Optional[str] = None


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
        self._keyrings_dir = self._cfg.keyrings_dir

    def _cache_dir(self, name: str, *, iso: bool = False) -> Path:
        root = self._cfg.isos_dir if iso else self._cfg.images_dir
        return root / name

    def _base_path(self, name: str, *, iso: bool = False) -> Path:
        return self._cache_dir(name, iso=iso) / ("installer.iso" if iso else "base.qcow2")

    def _checksum_path(self, name: str, *, iso: bool = False) -> Path:
        return self._cache_dir(name, iso=iso) / "checksum.txt"

    def _metadata_path(self, name: str, *, iso: bool = False) -> Path:
        return self._cache_dir(name, iso=iso) / "metadata.json"

    def is_cached(self, name: str, *, iso: bool = False) -> bool:
        p = self._base_path(name, iso=iso)
        return p.exists() and self._checksum_path(name, iso=iso).exists()

    async def ensure_image(self, name: str) -> Path:
        """Fetch and verify a cloud-image base disk. Rejects installer ISO templates."""
        entry = self._require_entry(name)
        artifact_type = _artifact_type(entry)
        if artifact_type != "cloud-image":
            raise IPCError(
                ERR_INVALID_PARAMS,
                f"Template '{name}' is type '{artifact_type}'. Use the ISO installer workflow "
                "(vm.request_installer / box_request_installer) for installer ISO templates.",
            )
        return await self._ensure_artifact(name, entry, iso=False)

    async def ensure_iso(self, name: str) -> Path:
        """Fetch and verify an installer ISO into the ISO cache (separate from images)."""
        entry = self._require_entry(name)
        artifact_type = _artifact_type(entry)
        if artifact_type != "iso":
            raise IPCError(
                ERR_INVALID_PARAMS,
                f"Template '{name}' is type '{artifact_type}', not an installer ISO.",
            )
        return await self._ensure_artifact(name, entry, iso=True)

    def _require_entry(self, name: str) -> dict:
        entry = self._catalog.get(name)
        if entry is None:
            raise IPCError(
                ERR_INVALID_PARAMS,
                f"Unknown template: {name}. Available: {self._catalog.list_names()}",
            )
        return entry

    async def _ensure_artifact(self, name: str, entry: dict, *, iso: bool) -> Path:
        kind = "ISO" if iso else "image"
        base_path = self._base_path(name, iso=iso)
        url = entry["url"]
        _validate_url(url)
        expected = await self._expected_digest(entry, url)

        cached = self._read_cached_digest(name, iso=iso)
        if self.is_cached(name, iso=iso) and self._cache_matches_expected(cached, expected):
            logger.debug("%s %s already cached at %s", kind, name, base_path)
            return base_path
        if self.is_cached(name, iso=iso) and expected is not None:
            logger.info("Cached %s %s no longer matches expected digest; refreshing", kind, name)
            base_path.unlink(missing_ok=True)
            self._checksum_path(name, iso=iso).unlink(missing_ok=True)

        logger.info("Fetching %s %s from %s", kind, name, url)
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
            self._write_cache_metadata(name, url, algorithm, actual_digest, expected, iso=iso)
            logger.info("%s %s cached, %s=%s", kind, name, algorithm, actual_digest)
            return base_path
        except Exception:
            if tmp_path.exists():
                tmp_path.unlink(missing_ok=True)
            raise

    async def preflight(self, names: Optional[list[str]] = None) -> list[dict]:
        """Check the trust chain of catalog entries without fetching artifacts.

        For each entry this resolves the expected digest exactly as a real fetch
        would (downloading the small checksum manifest and, where configured,
        verifying its PGP signature against the pinned keyring) but never
        downloads the multi-gigabyte image. Returns one report row per template.
        """
        targets = names if names is not None else self._catalog.list_names()
        reports: list[dict] = []
        for name in targets:
            entry = self._catalog.get(name)
            if entry is None:
                reports.append({"template": name, "status": "error", "detail": "unknown template"})
                continue

            verification = entry.get("verification") or {}
            policy = parse_signature_policy(verification, entry)
            row: dict = {
                "template": name,
                "artifact_type": _artifact_type(entry),
                "signature_required": bool(policy and policy.required),
            }
            try:
                expected = await self._expected_digest(entry, entry["url"])
            except IPCError as exc:
                row.update(status="error", detail=exc.message)
                reports.append(row)
                continue

            if expected is None:
                row.update(status="unverified", detail="no checksum manifest or static pin configured")
            elif expected.signature:
                row.update(status="signed", detail=expected.signature, algorithm=expected.algorithm)
            elif policy is not None:
                row.update(
                    status="checksum-only",
                    detail="signature configured but not verified (keyring missing or not required)",
                    algorithm=expected.algorithm,
                )
            else:
                row.update(status="checksum-only", detail=expected.source, algorithm=expected.algorithm)
            reports.append(row)
        return reports

    async def _expected_digest(self, entry: dict, url: str) -> Optional[ExpectedDigest]:
        sha256 = entry.get("sha256")
        if sha256:
            return ExpectedDigest("sha256", str(sha256).lower(), "catalog sha256")

        verification = entry.get("verification") or {}
        checksum_url = verification.get("checksum_url") or entry.get("checksum_url")
        policy = parse_signature_policy(verification, entry)
        if not checksum_url:
            if policy and policy.required:
                raise IPCError(
                    ERR_INTERNAL,
                    "signature_required is set but no checksum_url is configured to verify",
                )
            return None

        algorithm = _validate_hash_algorithm(
            verification.get("checksum_algorithm") or entry.get("checksum_algorithm") or "sha256"
        )
        filename = _manifest_filename(
            url,
            verification.get("checksum_filename") or entry.get("checksum_filename"),
        )
        _validate_url(checksum_url)
        manifest_bytes = await self._download_bytes(checksum_url)

        signature_source: Optional[str] = None
        if policy is not None:
            signature_source = await self._verify_manifest_signature(
                policy, checksum_url, manifest_bytes
            )

        manifest_text = manifest_bytes.decode("utf-8", "replace")
        if policy is not None and policy.mode == "clearsigned":
            manifest_text = extract_clearsigned_payload(manifest_text)

        digest = _parse_checksum_manifest(manifest_text, filename, algorithm)
        source = f"checksum manifest {checksum_url}"
        if signature_source:
            source = f"{source}; {signature_source}"
        return ExpectedDigest(algorithm, digest, source, signature=signature_source)

    async def _verify_manifest_signature(
        self,
        policy: SignaturePolicy,
        checksum_url: str,
        manifest_bytes: bytes,
    ) -> Optional[str]:
        """Verify a checksum manifest's PGP signature against a pinned keyring.

        Returns a provenance string on success, ``None`` when verification is
        unavailable but not required. Raises (fail-closed) whenever
        ``signature_required`` is set and the signature cannot be confirmed.
        """

        def _fail(message: str) -> Optional[str]:
            if policy.required:
                raise IPCError(ERR_INTERNAL, f"PGP signature verification failed: {message}")
            logger.warning("PGP signature not verified (%s); proceeding on checksum only", message)
            return None

        try:
            keyring = resolve_keyring_path(policy.keyring, self._keyrings_dir)
        except IPCError as exc:
            if policy.required:
                raise
            return _fail(exc.message)

        if policy.mode == "clearsigned":
            result: VerificationResult = await verify_clearsigned(keyring, manifest_bytes)
        else:
            if not policy.signature_url:
                return _fail("detached signature mode requires signature_url")
            _validate_url(policy.signature_url)
            signature_bytes = await self._download_bytes(policy.signature_url)
            result = await verify_detached(keyring, signature_bytes, manifest_bytes)

        if not result.valid:
            return _fail(f"no valid signature from {keyring.name}")
        if not check_fingerprints(result, policy.fingerprints):
            return _fail(
                f"signing key {result.fingerprints or '(unknown)'} not in pinned fingerprints"
            )

        signer = result.fingerprints[0] if result.fingerprints else "verified"
        return f"pgp signature verified via {keyring.name} (key {signer})"

    def _read_cached_digest(self, name: str, *, iso: bool = False) -> Optional[ExpectedDigest]:
        path = self._checksum_path(name, iso=iso)
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
        *,
        iso: bool = False,
    ) -> None:
        self._checksum_path(name, iso=iso).write_text(f"{algorithm}:{digest}\n")
        metadata = {
            "template": name,
            "artifact_type": "iso" if iso else "cloud-image",
            "url": url,
            "algorithm": algorithm,
            "digest": digest,
            "verified_at": datetime.now(timezone.utc).isoformat(),
            "verification_source": expected.source if expected else "download hash only",
            "signature_verified": bool(expected and expected.signature),
            "signature_provenance": expected.signature if expected else None,
        }
        self._metadata_path(name, iso=iso).write_text(json.dumps(metadata, indent=2) + "\n")

    async def _download(self, url: str, dest: Path) -> None:
        async with httpx.AsyncClient(follow_redirects=False, timeout=3600) as client:
            resp = await self._stream_validated(client, url)
            try:
                with open(dest, "wb") as f:
                    async for chunk in resp.aiter_bytes(1 << 16):
                        f.write(chunk)
            finally:
                await resp.aclose()

    async def _download_bytes(self, url: str) -> bytes:
        async with httpx.AsyncClient(follow_redirects=False, timeout=120) as client:
            resp = await self._stream_validated(client, url)
            try:
                chunks = []
                async for chunk in resp.aiter_bytes(1 << 16):
                    chunks.append(chunk)
                return b"".join(chunks)
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
