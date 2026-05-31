"""PGP signature verification for checksum manifests using pinned local keyrings.

Verification is performed with ``gpgv`` rather than ``gpg``: ``gpgv`` only ever
reads a caller-supplied keyring, never touches the user trust database, and
cannot import keys. This makes it the right primitive for a fail-closed trust
chain — the keyring is the sole trust anchor and is curated by the admin.

Two signature shapes are supported:

- ``detached``: a separate signature file (binary ``.gpg`` or armored ``.sign`` /
  ``.asc``) signs the checksum manifest. Ubuntu ``SHA256SUMS.gpg`` and Debian
  ``SHA512SUMS.sign`` use this shape.
- ``clearsigned``: the checksum manifest is itself an inline PGP-signed message
  (``-----BEGIN PGP SIGNED MESSAGE-----``). Fedora ``*-CHECKSUM`` and
  AlmaLinux ``CHECKSUM`` use this shape; the checksums must be read from the
  signed payload, not the raw file.
"""
from __future__ import annotations

import asyncio
import logging
import re
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from boxer.ipc import ERR_INTERNAL, ERR_INVALID_PARAMS, IPCError

logger = logging.getLogger(__name__)

_SUPPORTED_SIGNATURE_MODES = {"detached", "clearsigned"}

# `[GNUPG:] VALIDSIG <primary-or-signing-fpr> <date> ...`
_VALIDSIG_RE = re.compile(r"^\[GNUPG:\]\s+VALIDSIG\s+([0-9A-Fa-f]{40,})")
# `[GNUPG:] GOODSIG <keyid> <user id>`
_GOODSIG_RE = re.compile(r"^\[GNUPG:\]\s+GOODSIG\s+([0-9A-Fa-f]+)")

_CLEARSIGN_HEADER = "-----BEGIN PGP SIGNED MESSAGE-----"
_CLEARSIGN_SIG = "-----BEGIN PGP SIGNATURE-----"


def normalize_fingerprint(value: str) -> str:
    """Uppercase a fingerprint and strip spaces / ``0x`` prefixes for comparison."""
    cleaned = value.strip().upper().replace(" ", "")
    if cleaned.startswith("0X"):
        cleaned = cleaned[2:]
    return cleaned


@dataclass(frozen=True)
class SignaturePolicy:
    """Catalog-derived rules for verifying a checksum manifest's PGP signature."""

    mode: str
    keyring: Optional[str]
    signature_url: Optional[str]
    fingerprints: tuple[str, ...] = ()
    required: bool = False


@dataclass(frozen=True)
class VerificationResult:
    valid: bool
    fingerprints: tuple[str, ...] = ()
    detail: str = ""


def parse_signature_policy(verification: dict, entry: dict) -> Optional[SignaturePolicy]:
    """Build a :class:`SignaturePolicy` from a catalog entry, or ``None`` if the
    entry asks for no signature verification at all.

    Accepts a nested ``verification.signature`` block as the primary form and
    falls back to flat keys on ``verification`` / the entry for ergonomics.
    """
    block = verification.get("signature")
    if block is not None and not isinstance(block, dict):
        raise IPCError(ERR_INVALID_PARAMS, "verification.signature must be an object")
    block = block or {}

    def _lookup(key: str):
        for src in (block, verification, entry):
            if key in src and src[key] is not None:
                return src[key]
        return None

    required = bool(_lookup("signature_required") or block.get("required"))
    signature_url = _lookup("signature_url")
    keyring = _lookup("keyring_path") or _lookup("keyring")
    mode = _lookup("signature_mode") or block.get("mode")

    fingerprints_raw = (
        _lookup("signature_fingerprints")
        or _lookup("signature_fingerprint")
        or block.get("fingerprints")
        or block.get("fingerprint")
    )

    # No signature configuration and not required → nothing to verify.
    if not any((required, signature_url, keyring, mode, fingerprints_raw)):
        return None

    if mode is None:
        mode = "clearsigned" if signature_url is None else "detached"
    mode = str(mode).lower()
    if mode not in _SUPPORTED_SIGNATURE_MODES:
        raise IPCError(
            ERR_INVALID_PARAMS,
            f"Unsupported signature mode '{mode}'. Supported: {sorted(_SUPPORTED_SIGNATURE_MODES)}",
        )

    if isinstance(fingerprints_raw, str):
        fingerprints = (normalize_fingerprint(fingerprints_raw),)
    elif isinstance(fingerprints_raw, (list, tuple)):
        fingerprints = tuple(normalize_fingerprint(str(f)) for f in fingerprints_raw if f)
    else:
        fingerprints = ()

    return SignaturePolicy(
        mode=mode,
        keyring=str(keyring) if keyring else None,
        signature_url=str(signature_url) if signature_url else None,
        fingerprints=fingerprints,
        required=required,
    )


def resolve_keyring_path(keyring: Optional[str], keyrings_dir: Path) -> Path:
    """Resolve a keyring reference to an existing file.

    Absolute paths are used verbatim; bare names are looked up under
    ``keyrings_dir``. Raises if no keyring is configured or the file is missing.
    """
    if not keyring:
        raise IPCError(ERR_INVALID_PARAMS, "signature verification requires a keyring")
    candidate = Path(keyring)
    if not candidate.is_absolute():
        candidate = keyrings_dir / candidate
    if not candidate.is_file():
        raise IPCError(
            ERR_INTERNAL,
            f"Keyring not found: {candidate}. Install the trusted keyring before enabling signature verification.",
        )
    return candidate


def extract_clearsigned_payload(text: str) -> str:
    """Return the signed body of a PGP clearsigned message.

    Strips the armor headers and undoes dash-escaping (``- `` → ``""``) as
    defined by RFC 4880 §7.1, so the result is byte-for-byte the checksum lines
    that were actually signed.
    """
    if _CLEARSIGN_HEADER not in text or _CLEARSIGN_SIG not in text:
        raise IPCError(ERR_INVALID_PARAMS, "Manifest is not a PGP clearsigned message")

    lines = text.splitlines()
    start = next(i for i, ln in enumerate(lines) if ln.strip() == _CLEARSIGN_HEADER)

    # Skip the armor header line and the Hash: headers, then one blank line.
    idx = start + 1
    while idx < len(lines) and lines[idx] != "":
        idx += 1
    idx += 1  # the blank separator line

    payload: list[str] = []
    while idx < len(lines) and lines[idx].strip() != _CLEARSIGN_SIG:
        line = lines[idx]
        if line.startswith("- "):
            line = line[2:]
        elif line == "-":
            line = ""
        payload.append(line)
        idx += 1

    return "\n".join(payload) + "\n"


def _parse_status(status_text: str) -> tuple[bool, tuple[str, ...]]:
    valid = False
    fingerprints: list[str] = []
    for raw in status_text.splitlines():
        line = raw.strip()
        m = _VALIDSIG_RE.match(line)
        if m:
            valid = True
            fingerprints.append(normalize_fingerprint(m.group(1)))
            continue
        if _GOODSIG_RE.match(line):
            valid = True
    return valid, tuple(fingerprints)


async def _run_gpgv(args: list[str]) -> tuple[int, str]:
    """Run gpgv, returning (returncode, status-fd text)."""
    gpgv = shutil.which("gpgv")
    if gpgv is None:
        raise IPCError(
            ERR_INTERNAL,
            "gpgv is not installed; cannot perform PGP signature verification",
        )
    proc = await asyncio.create_subprocess_exec(
        gpgv,
        "--status-fd",
        "1",
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate()
    status = stdout.decode("utf-8", "replace") + "\n" + stderr.decode("utf-8", "replace")
    return proc.returncode, status


async def verify_detached(keyring: Path, signature: bytes, signed: bytes) -> VerificationResult:
    """Verify a detached signature over ``signed`` against ``keyring``."""
    with tempfile.TemporaryDirectory(prefix="boxer-gpgv-") as tmp:
        sig_path = Path(tmp) / "manifest.sig"
        data_path = Path(tmp) / "manifest"
        sig_path.write_bytes(signature)
        data_path.write_bytes(signed)
        rc, status = await _run_gpgv(
            ["--keyring", str(keyring), str(sig_path), str(data_path)]
        )
    valid, fprs = _parse_status(status)
    return VerificationResult(valid=(rc == 0 and valid), fingerprints=fprs, detail=status.strip())


async def verify_clearsigned(keyring: Path, clearsigned: bytes) -> VerificationResult:
    """Verify an inline clearsigned message against ``keyring``."""
    with tempfile.TemporaryDirectory(prefix="boxer-gpgv-") as tmp:
        data_path = Path(tmp) / "manifest.asc"
        data_path.write_bytes(clearsigned)
        rc, status = await _run_gpgv(["--keyring", str(keyring), str(data_path)])
    valid, fprs = _parse_status(status)
    return VerificationResult(valid=(rc == 0 and valid), fingerprints=fprs, detail=status.strip())


def check_fingerprints(
    result: VerificationResult, expected: tuple[str, ...]
) -> bool:
    """Return True if no fingerprints are pinned, or a signing fingerprint matches a pin."""
    if not expected:
        return True
    return any(fpr in expected for fpr in result.fingerprints)
