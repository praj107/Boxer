"""Tests for image catalog URL validation and signature-aware digest resolution."""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from boxer.config import BoxerConfig, ImageCatalog
from boxer.ipc import IPCError, ERR_INVALID_PARAMS, ERR_INTERNAL
from boxerd.image_catalog import (
    ImageManager,
    _parse_checksum_manifest,
    _validate_url,
    _is_private_ip,
)

_IMAGES_YAML = Path(__file__).parent.parent / "config" / "images.yaml"


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


def test_parse_digest_only_sidecar_manifest() -> None:
    digest = (
        "05bd2071f54cb47307fd1f9ff99333b3426dafa7fea95253"
        "b1100119b4da537e5f415c369a7c74b8c4d11cc22db178e2"
        "6f39ecb27b7757edbe25d19a9944f061"
    )
    assert (
        _parse_checksum_manifest(digest + "\n", "nocloud_alpine.qcow2", "sha512")
        == digest
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


# ------------------------------------------------ new checksum manifest formats


def test_parse_centos_sha256sum_sidecar() -> None:
    """CentOS Stream ships a per-file .SHA256SUM; bare hash on a single line."""
    digest = "a" * 64
    assert _parse_checksum_manifest(digest + "\n", "CentOS-Stream-GenericCloud-9-latest.x86_64.qcow2", "sha256") == digest


def test_parse_opensuse_sha256_filename_pair() -> None:
    """openSUSE .sha256 files sometimes contain 'hash  filename'."""
    digest = "b" * 64
    text = f"{digest}  openSUSE-Leap-15.6-Minimal-VM.x86_64-Cloud.qcow2\n"
    assert _parse_checksum_manifest(text, "openSUSE-Leap-15.6-Minimal-VM.x86_64-Cloud.qcow2", "sha256") == digest


def test_parse_rocky_fedora_style_checksum() -> None:
    """Rocky and AlmaLinux CHECKSUM files use 'SHA256 (file) = hash' Fedora style."""
    digest = "c" * 64
    text = f"SHA256 (Rocky-8-GenericCloud.latest.x86_64.qcow2) = {digest}\n"
    assert _parse_checksum_manifest(text, "Rocky-8-GenericCloud.latest.x86_64.qcow2", "sha256") == digest


def test_parse_freebsd_multi_entry_checksum() -> None:
    """FreeBSD CHECKSUM files contain entries for multiple ISOs; match by filename."""
    disc1_digest = "d" * 64
    bootonly_digest = "e" * 64
    text = (
        f"SHA256 (FreeBSD-14.3-RELEASE-amd64-disc1.iso) = {disc1_digest}\n"
        f"SHA256 (FreeBSD-14.3-RELEASE-amd64-bootonly.iso) = {bootonly_digest}\n"
    )
    assert _parse_checksum_manifest(text, "FreeBSD-14.3-RELEASE-amd64-disc1.iso", "sha256") == disc1_digest
    assert _parse_checksum_manifest(text, "FreeBSD-14.3-RELEASE-amd64-bootonly.iso", "sha256") == bootonly_digest


# -------------------------------------------------------- catalog integrity


def test_images_yaml_loads_and_all_entries_have_required_fields() -> None:
    """Every active entry in images.yaml must have url, type, and default resources."""
    data = yaml.safe_load(_IMAGES_YAML.read_text())
    images: dict = data.get("images", {})
    assert images, "images.yaml contains no image entries"

    for name, entry in images.items():
        assert "url" in entry, f"{name}: missing url"
        entry_type = entry.get("type") or entry.get("artifact_type")
        assert entry_type in ("cloud-image", "iso"), f"{name}: unknown type {entry_type!r}"
        assert "default_cpu" in entry, f"{name}: missing default_cpu"
        assert "default_ram_mb" in entry, f"{name}: missing default_ram_mb"
        assert "default_disk_gb" in entry, f"{name}: missing default_disk_gb"
        assert "family" in entry, f"{name}: missing family"
        url = entry["url"]
        assert url.startswith("https://"), f"{name}: url must be https"


def test_images_yaml_iso_entries_have_install_method() -> None:
    data = yaml.safe_load(_IMAGES_YAML.read_text())
    for name, entry in data.get("images", {}).items():
        if entry.get("type") == "iso":
            assert "install" in entry, f"{name}: iso entry missing install block"
            assert "method" in entry["install"], f"{name}: iso install block missing method"


def test_images_yaml_verification_algorithms_are_supported() -> None:
    data = yaml.safe_load(_IMAGES_YAML.read_text())
    supported = {"sha256", "sha512"}
    for name, entry in data.get("images", {}).items():
        verification = entry.get("verification") or {}
        algo = verification.get("checksum_algorithm")
        if algo is not None:
            assert algo in supported, f"{name}: unsupported checksum_algorithm {algo!r}"


def test_images_yaml_expected_families_present() -> None:
    data = yaml.safe_load(_IMAGES_YAML.read_text())
    families = {e.get("family") for e in data.get("images", {}).values()}
    for expected in ("ubuntu", "debian", "fedora", "almalinux", "rocky", "alpine", "freebsd", "openbsd", "netbsd", "centos", "opensuse"):
        assert expected in families, f"family '{expected}' not represented in catalog"


# -------------------------------------------------------- DHCP lease fallback


def test_dhcp_lease_fallback_returns_ip(monkeypatch) -> None:
    """_get_ip_via_dhcp_lease extracts MAC/net from XML and queries DHCPLeases."""
    import xml.etree.ElementTree as ET
    from unittest.mock import MagicMock
    from boxerd.vm_ops import VMOperations

    domain_xml = """<domain>
      <devices>
        <interface type='network'>
          <mac address='52:54:00:ab:cd:ef'/>
          <source network='boxer-net-test'/>
        </interface>
      </devices>
    </domain>"""

    mock_dom = MagicMock()
    mock_dom.XMLDesc.return_value = domain_xml

    mock_net = MagicMock()
    mock_net.DHCPLeases.return_value = [
        {"type": 0, "ipaddr": "10.200.1.99", "mac": "52:54:00:ab:cd:ef"},
    ]

    mock_conn = MagicMock()
    mock_conn.lookupByName.return_value = mock_dom
    mock_conn.networkLookupByName.return_value = mock_net

    ops = VMOperations.__new__(VMOperations)
    ops._conn = mock_conn

    ip = ops._get_ip_via_dhcp_lease("boxer--proj--vm-test--vm_abc123")
    assert ip == "10.200.1.99"
    mock_net.DHCPLeases.assert_called_once_with("52:54:00:ab:cd:ef", 0)


def test_dhcp_lease_fallback_skips_ipv6(monkeypatch) -> None:
    """_get_ip_via_dhcp_lease ignores IPv6 (type=1) entries."""
    from unittest.mock import MagicMock
    from boxerd.vm_ops import VMOperations

    domain_xml = """<domain>
      <devices>
        <interface type='network'>
          <mac address='52:54:00:11:22:33'/>
          <source network='boxer-net'/>
        </interface>
      </devices>
    </domain>"""

    mock_dom = MagicMock()
    mock_dom.XMLDesc.return_value = domain_xml
    mock_net = MagicMock()
    mock_net.DHCPLeases.return_value = [
        {"type": 1, "ipaddr": "fe80::1", "mac": "52:54:00:11:22:33"},
    ]
    mock_conn = MagicMock()
    mock_conn.lookupByName.return_value = mock_dom
    mock_conn.networkLookupByName.return_value = mock_net

    ops = VMOperations.__new__(VMOperations)
    ops._conn = mock_conn

    assert ops._get_ip_via_dhcp_lease("test-vm") is None


def test_dhcp_lease_fallback_returns_none_on_exception() -> None:
    """_get_ip_via_dhcp_lease swallows all exceptions and returns None."""
    from unittest.mock import MagicMock
    from boxerd.vm_ops import VMOperations

    mock_conn = MagicMock()
    mock_conn.lookupByName.side_effect = RuntimeError("libvirt gone")

    ops = VMOperations.__new__(VMOperations)
    ops._conn = mock_conn

    assert ops._get_ip_via_dhcp_lease("test-vm") is None
