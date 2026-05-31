"""Tests for PGP signature policy parsing and gpgv-based verification."""
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

from boxer.ipc import ERR_INTERNAL, ERR_INVALID_PARAMS, IPCError
from boxerd.signature import (
    SignaturePolicy,
    VerificationResult,
    check_fingerprints,
    extract_clearsigned_payload,
    normalize_fingerprint,
    parse_signature_policy,
    resolve_keyring_path,
    verify_clearsigned,
    verify_detached,
)


# ---------------------------------------------------------------- policy parsing


def test_no_signature_config_returns_none() -> None:
    assert parse_signature_policy({"checksum_url": "https://x/SHA256SUMS"}, {}) is None


def test_nested_detached_policy() -> None:
    verification = {
        "signature": {
            "mode": "detached",
            "signature_url": "https://x/SHA256SUMS.gpg",
            "keyring": "ubuntu.gpg",
            "required": True,
        }
    }
    policy = parse_signature_policy(verification, {})
    assert policy == SignaturePolicy(
        mode="detached",
        keyring="ubuntu.gpg",
        signature_url="https://x/SHA256SUMS.gpg",
        fingerprints=(),
        required=True,
    )


def test_flat_fallback_keys() -> None:
    verification = {
        "signature_url": "https://x/SHA256SUMS.gpg",
        "keyring_path": "/abs/ubuntu.gpg",
        "signature_required": True,
        "signature_fingerprint": "AB CD ef 12",
    }
    policy = parse_signature_policy(verification, {})
    assert policy is not None
    assert policy.mode == "detached"
    assert policy.keyring == "/abs/ubuntu.gpg"
    assert policy.required is True
    assert policy.fingerprints == ("ABCDEF12",)


def test_mode_inferred_clearsigned_when_no_signature_url() -> None:
    policy = parse_signature_policy({"signature": {"keyring": "fedora.gpg"}}, {})
    assert policy is not None
    assert policy.mode == "clearsigned"


def test_required_with_no_other_config_still_builds_policy() -> None:
    policy = parse_signature_policy({"signature": {"required": True}}, {})
    assert policy is not None
    assert policy.required is True
    # No signature_url → inferred clearsigned, no keyring → will fail closed later.
    assert policy.keyring is None


def test_invalid_mode_rejected() -> None:
    with pytest.raises(IPCError) as exc:
        parse_signature_policy({"signature": {"mode": "smime", "keyring": "k"}}, {})
    assert exc.value.code == ERR_INVALID_PARAMS


def test_fingerprint_list_normalized() -> None:
    policy = parse_signature_policy(
        {"signature": {"keyring": "k", "fingerprints": ["0xabc123", "DE F4 56"]}}, {}
    )
    assert policy is not None
    assert policy.fingerprints == ("ABC123", "DEF456")


def test_normalize_fingerprint() -> None:
    assert normalize_fingerprint("  0xab cd EF  ") == "ABCDEF"


# ---------------------------------------------------------------- keyring resolution


def test_resolve_keyring_absolute(tmp_path: Path) -> None:
    kr = tmp_path / "k.gpg"
    kr.write_bytes(b"x")
    assert resolve_keyring_path(str(kr), tmp_path / "other") == kr


def test_resolve_keyring_relative_to_dir(tmp_path: Path) -> None:
    kr = tmp_path / "ubuntu.gpg"
    kr.write_bytes(b"x")
    assert resolve_keyring_path("ubuntu.gpg", tmp_path) == kr


def test_resolve_keyring_missing_raises(tmp_path: Path) -> None:
    with pytest.raises(IPCError) as exc:
        resolve_keyring_path("nope.gpg", tmp_path)
    assert exc.value.code == ERR_INTERNAL


def test_resolve_keyring_none_raises(tmp_path: Path) -> None:
    with pytest.raises(IPCError) as exc:
        resolve_keyring_path(None, tmp_path)
    assert exc.value.code == ERR_INVALID_PARAMS


# ---------------------------------------------------------------- clearsign parsing


def test_extract_clearsigned_payload() -> None:
    clearsigned = (
        "-----BEGIN PGP SIGNED MESSAGE-----\n"
        "Hash: SHA256\n"
        "\n"
        "abc123  image.qcow2\n"
        "- -dashline stays\n"
        "-----BEGIN PGP SIGNATURE-----\n"
        "iQEzBA...\n"
        "-----END PGP SIGNATURE-----\n"
    )
    payload = extract_clearsigned_payload(clearsigned)
    assert "abc123  image.qcow2" in payload
    assert "-dashline stays" in payload
    assert "PGP" not in payload


def test_extract_clearsigned_payload_rejects_plain_text() -> None:
    with pytest.raises(IPCError):
        extract_clearsigned_payload("abc123  image.qcow2\n")


# ---------------------------------------------------------------- fingerprint match


def test_check_fingerprints_no_pins_passes() -> None:
    assert check_fingerprints(VerificationResult(True, ("ABC",)), ()) is True


def test_check_fingerprints_match() -> None:
    assert check_fingerprints(VerificationResult(True, ("ABC", "DEF")), ("DEF",)) is True


def test_check_fingerprints_no_match() -> None:
    assert check_fingerprints(VerificationResult(True, ("ABC",)), ("XYZ",)) is False


# ---------------------------------------------------------------- gpgv integration

_HAS_GPG = shutil.which("gpg") is not None and shutil.which("gpgv") is not None


@pytest.fixture
def gpg_signing_env(tmp_path: Path):
    """Generate an ephemeral GPG key, export a keyring, and expose a signer."""
    gnupghome = tmp_path / "gnupg"
    gnupghome.mkdir(mode=0o700)
    env = {**os.environ, "GNUPGHOME": str(gnupghome)}

    batch = tmp_path / "keyparams"
    batch.write_text(
        "%no-protection\n"
        "Key-Type: eddsa\n"
        "Key-Curve: ed25519\n"
        "Name-Real: Boxer Test\n"
        "Name-Email: test@boxer.invalid\n"
        "Expire-Date: 0\n"
        "%commit\n"
    )
    subprocess.run(
        ["gpg", "--batch", "--gen-key", str(batch)],
        env=env, check=True, capture_output=True,
    )
    fpr = subprocess.run(
        ["gpg", "--list-keys", "--with-colons"],
        env=env, check=True, capture_output=True, text=True,
    ).stdout
    fingerprint = next(
        line.split(":")[9] for line in fpr.splitlines() if line.startswith("fpr:")
    )

    keyring = tmp_path / "test-keyring.gpg"
    subprocess.run(
        ["gpg", "--batch", "--yes", "--output", str(keyring), "--export"],
        env=env, check=True, capture_output=True,
    )

    def sign_detached(data: bytes) -> bytes:
        return subprocess.run(
            ["gpg", "--batch", "--detach-sign", "--output", "-"],
            env=env, input=data, check=True, capture_output=True,
        ).stdout

    def clearsign(data: bytes) -> bytes:
        return subprocess.run(
            ["gpg", "--batch", "--clearsign", "--output", "-"],
            env=env, input=data, check=True, capture_output=True,
        ).stdout

    return type(
        "GpgEnv",
        (),
        {
            "keyring": keyring,
            "fingerprint": fingerprint,
            "sign_detached": staticmethod(sign_detached),
            "clearsign": staticmethod(clearsign),
        },
    )


@pytest.mark.skipif(not _HAS_GPG, reason="gpg/gpgv not installed")
async def test_verify_detached_good_signature(gpg_signing_env) -> None:
    manifest = b"abc123  image.qcow2\n"
    sig = gpg_signing_env.sign_detached(manifest)
    result = await verify_detached(gpg_signing_env.keyring, sig, manifest)
    assert result.valid is True
    assert gpg_signing_env.fingerprint in result.fingerprints


@pytest.mark.skipif(not _HAS_GPG, reason="gpg/gpgv not installed")
async def test_verify_detached_tampered_data_fails(gpg_signing_env) -> None:
    manifest = b"abc123  image.qcow2\n"
    sig = gpg_signing_env.sign_detached(manifest)
    result = await verify_detached(gpg_signing_env.keyring, sig, b"TAMPERED\n")
    assert result.valid is False


@pytest.mark.skipif(not _HAS_GPG, reason="gpg/gpgv not installed")
async def test_verify_clearsigned_good_signature(gpg_signing_env) -> None:
    manifest = b"abc123  image.qcow2\n"
    clearsigned = gpg_signing_env.clearsign(manifest)
    result = await verify_clearsigned(gpg_signing_env.keyring, clearsigned)
    assert result.valid is True
    # The signed payload must still parse back to the original checksum line.
    payload = extract_clearsigned_payload(clearsigned.decode())
    assert "abc123  image.qcow2" in payload


@pytest.mark.skipif(not _HAS_GPG, reason="gpg/gpgv not installed")
async def test_verify_detached_wrong_keyring_fails(gpg_signing_env, tmp_path: Path) -> None:
    manifest = b"abc123  image.qcow2\n"
    sig = gpg_signing_env.sign_detached(manifest)
    empty_keyring = tmp_path / "empty.gpg"
    empty_keyring.write_bytes(b"")
    result = await verify_detached(empty_keyring, sig, manifest)
    assert result.valid is False
