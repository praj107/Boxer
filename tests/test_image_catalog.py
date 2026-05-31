"""Tests for image catalog URL validation and signature-aware digest resolution."""
from __future__ import annotations

from pathlib import Path

import pytest

from boxer.config import BoxerConfig, ImageCatalog
from boxer.ipc import IPCError, ERR_INVALID_PARAMS, ERR_INTERNAL
from boxerd.image_catalog import (
    ImageManager,
    _parse_checksum_manifest,
    _validate_url,
    _is_private_ip,
)


def test_private_ip_detection() -> None:
    assert _is_private_ip("10.0.0.1") is True
    assert _is_private_ip("172.16.5.1") is True
    assert _is_private_ip("192.168.1.1") is True
    assert _is_private_ip("127.0.0.1") is True
    assert _is_private_ip("::1") is True
    assert _is_private_ip("8.8.8.8") is False
    assert _is_private_ip("1.1.1.1") is False


def test_http_url_rejected() -> None:
    with pytest.raises(IPCError) as exc_info:
        _validate_url("http://example.com/image.qcow2")
    assert exc_info.value.code == ERR_INVALID_PARAMS
    assert "https" in exc_info.value.message


def test_no_scheme_rejected() -> None:
    with pytest.raises(IPCError):
        _validate_url("ftp://example.com/image.qcow2")


def test_valid_https_public_url_passes(monkeypatch) -> None:
    import socket

    def mock_getaddrinfo(host, port, *args, **kwargs):
        return [(None, None, None, None, ("8.8.8.8", 443))]

    monkeypatch.setattr(socket, "getaddrinfo", mock_getaddrinfo)
    _validate_url("https://cloud-images.ubuntu.com/noble/current/noble-server-cloudimg-amd64.img")


def test_private_ip_url_rejected(monkeypatch) -> None:
    import socket

    def mock_getaddrinfo(host, port, *args, **kwargs):
        return [(None, None, None, None, ("192.168.1.1", 443))]

    monkeypatch.setattr(socket, "getaddrinfo", mock_getaddrinfo)
    with pytest.raises(IPCError) as exc_info:
        _validate_url("https://internal.corp/image.qcow2")
    assert "private" in exc_info.value.message.lower()


def test_parse_sha256sums_manifest() -> None:
    text = """
abc123  other.img
4d967bd40fdef4c43a3d0a8d54e45efdbf55f4ebf1b7fcb13b6f70e27f20bc90 *noble-server-cloudimg-amd64.img
"""
    assert (
        _parse_checksum_manifest(text, "noble-server-cloudimg-amd64.img", "sha256")
        == "4d967bd40fdef4c43a3d0a8d54e45efdbf55f4ebf1b7fcb13b6f70e27f20bc90"
    )


def test_parse_fedora_checksum_manifest() -> None:
    text = """
# Fedora-Cloud-44-1.7-x86_64-CHECKSUM
SHA256 (Fedora-Cloud-Base-Generic-44-1.7.x86_64.qcow2) = 8f6e5d4c3b2a19081726354433221100ffeeddccbbaa99887766554433221100
"""
    assert (
        _parse_checksum_manifest(
            text,
            "Fedora-Cloud-Base-Generic-44-1.7.x86_64.qcow2",
            "sha256",
        )
        == "8f6e5d4c3b2a19081726354433221100ffeeddccbbaa99887766554433221100"
    )


def test_parse_checksum_manifest_missing_file_rejected() -> None:
    with pytest.raises(IPCError) as exc_info:
        _parse_checksum_manifest("abc123  other.img\n", "missing.img", "sha256")
    assert exc_info.value.code == ERR_INVALID_PARAMS


# ---------------------------------------------- signature-aware digest resolution

_DIGEST = "4d967bd40fdef4c43a3d0a8d54e45efdbf55f4ebf1b7fcb13b6f70e27f20bc90"
_MANIFEST = f"{_DIGEST}  image.qcow2\n".encode()


def _manager(tmp_path: Path, entry: dict) -> ImageManager:
    cfg = BoxerConfig({"state_dir": str(tmp_path), "keyrings_dir": str(tmp_path / "keyrings")})
    catalog = ImageCatalog({"images": {"test": entry}})
    return ImageManager(cfg, catalog)


async def test_required_signature_missing_keyring_fails_closed(tmp_path, monkeypatch) -> None:
    entry = {
        "url": "https://x/image.qcow2",
        "verification": {
            "checksum_url": "https://x/SHA256SUMS",
            "checksum_filename": "image.qcow2",
            "signature": {
                "mode": "detached",
                "signature_url": "https://x/SHA256SUMS.gpg",
                "keyring": "absent.gpg",
                "required": True,
            },
        },
    }
    mgr = _manager(tmp_path, entry)
    monkeypatch.setattr(mgr, "_download_bytes", _stub_download(_MANIFEST))
    monkeypatch.setattr("boxerd.image_catalog._validate_url", lambda url: None)

    with pytest.raises(IPCError) as exc:
        await mgr._expected_digest(entry, entry["url"])
    assert exc.value.code == ERR_INTERNAL


async def test_optional_signature_missing_keyring_falls_back(tmp_path, monkeypatch) -> None:
    entry = {
        "url": "https://x/image.qcow2",
        "verification": {
            "checksum_url": "https://x/SHA256SUMS",
            "checksum_filename": "image.qcow2",
            "signature": {
                "mode": "detached",
                "signature_url": "https://x/SHA256SUMS.gpg",
                "keyring": "absent.gpg",
                "required": False,
            },
        },
    }
    mgr = _manager(tmp_path, entry)
    monkeypatch.setattr(mgr, "_download_bytes", _stub_download(_MANIFEST))
    monkeypatch.setattr("boxerd.image_catalog._validate_url", lambda url: None)

    expected = await mgr._expected_digest(entry, entry["url"])
    assert expected is not None
    assert expected.digest == _DIGEST
    assert expected.signature is None  # checksum-only fallback


async def test_required_without_checksum_url_fails_closed(tmp_path) -> None:
    entry = {
        "url": "https://x/image.qcow2",
        "verification": {"signature": {"required": True, "keyring": "k.gpg"}},
    }
    mgr = _manager(tmp_path, entry)
    with pytest.raises(IPCError) as exc:
        await mgr._expected_digest(entry, entry["url"])
    assert exc.value.code == ERR_INTERNAL


def _stub_download(payload: bytes):
    async def _dl(url: str) -> bytes:
        return payload
    return _dl


# ----------------------------------------------------- ISO cache (Milestone 3)


def test_ensure_image_rejects_iso_template(tmp_path) -> None:
    import asyncio

    entry = {"type": "iso", "url": "https://x/installer.iso", "sha256": None}
    mgr = _manager(tmp_path, entry)
    with pytest.raises(IPCError) as exc:
        asyncio.run(mgr.ensure_image("test"))
    assert exc.value.code == ERR_INVALID_PARAMS
    assert "installer" in exc.value.message.lower()


async def test_ensure_iso_rejects_cloud_image_template(tmp_path) -> None:
    entry = {"type": "cloud-image", "url": "https://x/base.qcow2", "sha256": None}
    mgr = _manager(tmp_path, entry)
    with pytest.raises(IPCError) as exc:
        await mgr.ensure_iso("test")
    assert exc.value.code == ERR_INVALID_PARAMS


def test_iso_and_image_caches_are_separate(tmp_path) -> None:
    cfg = BoxerConfig({"state_dir": str(tmp_path)})
    mgr = ImageManager(cfg, ImageCatalog({"images": {}}))
    image_path = mgr._blob_path("ubuntu", "sha256", "abc", iso=False)
    iso_path = mgr._blob_path("arch", "sha256", "abc", iso=True)
    assert cfg.images_dir in image_path.parents
    assert cfg.isos_dir in iso_path.parents
    # Content-addressed: digest in the filename, distinct extensions per kind.
    assert image_path.name == "sha256-abc.qcow2"
    assert iso_path.name == "sha256-abc.iso"
