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


_VALID_REFRESH_POLICIES = {"pinned", "latest", "manual"}


def _refresh_policy(entry: dict) -> str:
    """Resolve a catalog entry's refresh policy.

    Explicit ``refresh_policy`` wins. Otherwise an entry with a static ``sha256``
    pin defaults to ``pinned``; everything else defaults to ``latest`` (refresh
    when the upstream manifest digest changes).
    """
    explicit = entry.get("refresh_policy")
    if explicit is not None:
        policy = str(explicit).lower()
        if policy not in _VALID_REFRESH_POLICIES:
            raise IPCError(
                ERR_INVALID_PARAMS,
                f"Invalid refresh_policy '{explicit}'. Valid: {sorted(_VALID_REFRESH_POLICIES)}",
            )
        return policy
    return "pinned" if entry.get("sha256") else "latest"


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

    def _blob_dir(self, name: str, *, iso: bool = False) -> Path:
        return self._cache_dir(name, iso=iso) / "blobs"

    def _blob_path(self, name: str, algorithm: str, digest: str, *, iso: bool = False) -> Path:
        ext = "iso" if iso else "qcow2"
        return self._blob_dir(name, iso=iso) / f"{algorithm}-{digest}.{ext}"

    def _metadata_path(self, name: str, *, iso: bool = False) -> Path:
        return self._cache_dir(name, iso=iso) / "metadata.json"

    def is_cached(self, name: str, *, iso: bool = False) -> bool:
        return self._current_blob(self._read_metadata(name, iso=iso), name, iso=iso) is not None

    async def ensure_image(self, name: str, *, force: bool = False) -> Path:
        """Fetch and verify a cloud-image base disk. Rejects installer ISO templates."""
        entry = self._require_entry(name)
        artifact_type = _artifact_type(entry)
        if artifact_type != "cloud-image":
            raise IPCError(
                ERR_INVALID_PARAMS,
                f"Template '{name}' is type '{artifact_type}'. Use the ISO installer workflow "
                "(vm.request_installer / box_request_installer) for installer ISO templates.",
            )
        return await self._ensure_artifact(name, entry, iso=False, force=force)

    async def ensure_iso(self, name: str, *, force: bool = False) -> Path:
        """Fetch and verify an installer ISO into the ISO cache (separate from images)."""
        entry = self._require_entry(name)
        artifact_type = _artifact_type(entry)
        if artifact_type != "iso":
            raise IPCError(
                ERR_INVALID_PARAMS,
                f"Template '{name}' is type '{artifact_type}', not an installer ISO.",
            )
        return await self._ensure_artifact(name, entry, iso=True, force=force)

    def _require_entry(self, name: str) -> dict:
        entry = self._catalog.get(name)
        if entry is None:
            raise IPCError(
                ERR_INVALID_PARAMS,
                f"Unknown template: {name}. Available: {self._catalog.list_names()}",
            )
        return entry

    async def _ensure_artifact(self, name: str, entry: dict, *, iso: bool, force: bool = False) -> Path:
        kind = "ISO" if iso else "image"
        url = entry["url"]
        _validate_url(url)
        policy = _refresh_policy(entry)
        meta = self._read_metadata(name, iso=iso)

        # manual: serve the cached current artifact without contacting upstream,
        # unless an admin explicitly forces a refresh or nothing is cached yet.
        if policy == "manual" and not force:
            current = self._current_blob(meta, name, iso=iso)
            if current is not None:
                logger.debug("%s %s served from manual-pinned cache %s", kind, name, current)
                return current

        expected = await self._expected_digest(entry, url)

        if expected is not None:
            blob = self._blob_path(name, expected.algorithm, expected.digest, iso=iso)
            if blob.exists():
                # Verified content already on disk; (re)point current at it.
                self._set_current(name, iso, policy, url, expected)
                if not force:
                    logger.debug("%s %s already cached at %s", kind, name, blob)
                    return blob
                logger.info("%s %s already at requested digest; refresh is a no-op", kind, name)
                return blob
            return await self._download_to_blob(name, iso, url, policy, expected)

        # No static pin and no checksum manifest to resolve a digest.
        if policy == "pinned":
            raise IPCError(
                ERR_INVALID_PARAMS,
                f"refresh_policy 'pinned' for {name} requires a static sha256 or a checksum manifest",
            )
        current = self._current_blob(meta, name, iso=iso)
        if current is not None and not force:
            return current
        return await self._download_to_blob(name, iso, url, policy, None)

    async def _download_to_blob(
        self,
        name: str,
        iso: bool,
        url: str,
        policy: str,
        expected: Optional[ExpectedDigest],
    ) -> Path:
        kind = "ISO" if iso else "image"
        blob_dir = self._blob_dir(name, iso=iso)
        blob_dir.mkdir(parents=True, exist_ok=True)
        logger.info("Fetching %s %s from %s", kind, name, url)

        tmp_path = Path(tempfile.mktemp(dir=blob_dir, suffix=".tmp"))
        try:
            await self._download(url, tmp_path)
            algorithm = expected.algorithm if expected else "sha256"
            actual_digest = await _hash_file(tmp_path, algorithm)

            if expected and expected.digest != actual_digest:
                raise IPCError(
                    ERR_INTERNAL,
                    f"{algorithm.upper()} mismatch for {name}: expected {expected.digest}, got {actual_digest}",
                )

            blob = self._blob_path(name, algorithm, actual_digest, iso=iso)
            tmp_path.rename(blob)
            resolved = expected or ExpectedDigest(algorithm, actual_digest, "download hash only")
            self._set_current(name, iso, policy, url, resolved)
            logger.info("%s %s cached, %s=%s", kind, name, algorithm, actual_digest)
            return blob
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

    def list_images(self) -> list[dict]:
        """List catalog entries with family, policy, and local cache status.

        Pure local read — no network — so agents and admins can browse what is
        available and what is already warmed without triggering a download.
        """
        rows: list[dict] = []
        for name in self._catalog.list_names():
            entry = self._catalog.get(name) or {}
            artifact_type = _artifact_type(entry)
            iso = artifact_type == "iso"
            meta = self._read_metadata(name, iso=iso)
            current = (meta or {}).get("current") or {}
            cached = self._current_blob(meta, name, iso=iso) is not None
            rows.append(
                {
                    "template": name,
                    "family": entry.get("family"),
                    "artifact_type": artifact_type,
                    "description": entry.get("description"),
                    "refresh_policy": _refresh_policy(entry),
                    "default_cpu": entry.get("default_cpu"),
                    "default_ram_mb": entry.get("default_ram_mb"),
                    "default_disk_gb": entry.get("default_disk_gb"),
                    "install_method": (entry.get("install") or {}).get("method") if iso else None,
                    "cached": cached,
                    "current_digest": (
                        f"{current['algorithm']}:{current['digest']}" if cached and current else None
                    ),
                    "verified_at": current.get("verified_at") if cached else None,
                    "signature_verified": bool(current.get("signature_verified")) if cached else False,
                }
            )
        return rows

    async def refresh_image(self, name: str) -> dict:
        """Force re-evaluation of a template's cache per its refresh policy (admin)."""
        entry = self._require_entry(name)
        iso = _artifact_type(entry) == "iso"
        if iso:
            await self.ensure_iso(name, force=True)
        else:
            await self.ensure_image(name, force=True)
        meta = self._read_metadata(name, iso=iso) or {}
        current = meta.get("current") or {}
        return {
            "template": name,
            "artifact_type": meta.get("artifact_type"),
            "refresh_policy": meta.get("refresh_policy"),
            "current_digest": f"{current.get('algorithm')}:{current.get('digest')}",
            "verified_at": current.get("verified_at"),
            "signature_verified": bool(current.get("signature_verified")),
        }

    async def prune(self, older_than_seconds: int = 0, dry_run: bool = False) -> dict:
        """Remove cached base artifacts that are not current and not in use.

        Protected from removal: the current blob of every template, and any blob
        referenced as a backing file by an existing VM overlay. Legacy
        single-file caches (``base.qcow2`` / ``installer.iso``) are also removed
        when unreferenced.
        """
        loop = asyncio.get_running_loop()
        protected = self._collect_current_blobs()
        in_use, backing_known = await loop.run_in_executor(None, self._collect_backing_files)
        protected |= in_use
        cutoff = datetime.now(timezone.utc).timestamp() - max(0, older_than_seconds)

        removed: list[dict] = []
        freed = 0
        for blob in self._iter_cached_artifacts():
            real = blob.resolve()
            if real in protected:
                continue
            # Without backing-chain info we cannot prove a blob is unused; only
            # prune the loose legacy files, never digest blobs, to stay safe.
            if not backing_known and blob.parent.name == "blobs":
                continue
            try:
                st = blob.stat()
            except OSError:
                continue
            if st.st_mtime > cutoff:
                continue
            removed.append({"path": str(blob), "bytes": st.st_size})
            freed += st.st_size
            if not dry_run:
                blob.unlink(missing_ok=True)

        return {
            "dry_run": dry_run,
            "removed_count": len(removed),
            "freed_bytes": freed,
            "backing_chain_known": backing_known,
            "removed": removed,
        }

    def _collect_current_blobs(self) -> set[Path]:
        protected: set[Path] = set()
        for root, iso in ((self._cfg.images_dir, False), (self._cfg.isos_dir, True)):
            if not root.exists():
                continue
            for tmpl_dir in root.iterdir():
                if not tmpl_dir.is_dir():
                    continue
                blob = self._current_blob(self._read_metadata(tmpl_dir.name, iso=iso), tmpl_dir.name, iso=iso)
                if blob is not None:
                    protected.add(blob.resolve())
        return protected

    def _iter_cached_artifacts(self):
        for root in (self._cfg.images_dir, self._cfg.isos_dir):
            if not root.exists():
                continue
            for tmpl_dir in root.iterdir():
                if not tmpl_dir.is_dir():
                    continue
                blob_dir = tmpl_dir / "blobs"
                if blob_dir.is_dir():
                    for blob in blob_dir.iterdir():
                        if blob.is_file() and blob.suffix in (".qcow2", ".iso"):
                            yield blob
                # Legacy single-file caches from before digest-addressing.
                for legacy in ("base.qcow2", "installer.iso"):
                    p = tmpl_dir / legacy
                    if p.is_file():
                        yield p

    def _collect_backing_files(self) -> tuple[set[Path], bool]:
        """Return (backing files referenced by overlays, whether detection worked)."""
        import subprocess

        projects_dir = self._cfg.projects_dir
        if not projects_dir.exists():
            return set(), True
        in_use: set[Path] = set()
        known = True
        for overlay in projects_dir.rglob("disk.qcow2"):
            try:
                out = subprocess.run(
                    ["qemu-img", "info", "--output=json", "--backing-chain", str(overlay)],
                    check=True,
                    capture_output=True,
                    text=True,
                )
            except FileNotFoundError:
                return set(), False  # qemu-img missing: cannot prove anything unused
            except subprocess.CalledProcessError:
                known = False
                continue
            try:
                chain = json.loads(out.stdout)
            except json.JSONDecodeError:
                known = False
                continue
            for node in chain:
                for key in ("full-backing-filename", "backing-filename"):
                    backing = node.get(key)
                    if backing:
                        in_use.add(Path(backing).resolve())
        return in_use, known

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

    # --- digest-addressed cache metadata ---

    def _read_metadata(self, name: str, *, iso: bool = False) -> Optional[dict]:
        path = self._metadata_path(name, iso=iso)
        if not path.exists():
            return None
        try:
            return json.loads(path.read_text())
        except (json.JSONDecodeError, OSError):
            return None

    def _current_blob(self, meta: Optional[dict], name: str, *, iso: bool = False) -> Optional[Path]:
        """Resolve the metadata 'current' pointer to an existing blob, or None."""
        if not meta:
            return None
        current = meta.get("current") or {}
        algorithm, digest = current.get("algorithm"), current.get("digest")
        if not algorithm or not digest:
            return None
        blob = self._blob_path(name, algorithm, digest, iso=iso)
        return blob if blob.exists() else None

    def _set_current(
        self,
        name: str,
        iso: bool,
        policy: str,
        url: str,
        expected: ExpectedDigest,
    ) -> None:
        """Write the metadata pointer + provenance for the active verified digest."""
        meta = self._read_metadata(name, iso=iso) or {}
        now = datetime.now(timezone.utc).isoformat()
        current = {
            "algorithm": expected.algorithm,
            "digest": expected.digest,
            "blob": self._blob_path(name, expected.algorithm, expected.digest, iso=iso).name,
            "url": url,
            "verified_at": now,
            "verification_source": expected.source,
            "signature_verified": bool(expected.signature),
            "signature_provenance": expected.signature,
        }
        history = meta.get("history") or []
        ident = f"{expected.algorithm}:{expected.digest}"
        history = [h for h in history if f"{h.get('algorithm')}:{h.get('digest')}" != ident]
        history.insert(0, {"algorithm": expected.algorithm, "digest": expected.digest, "verified_at": now})
        meta.update(
            {
                "template": name,
                "artifact_type": "iso" if iso else "cloud-image",
                "refresh_policy": policy,
                "current": current,
                "history": history[:10],
            }
        )
        self._metadata_path(name, iso=iso).write_text(json.dumps(meta, indent=2) + "\n")

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
