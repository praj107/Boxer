"""Tests for digest-addressed caching, refresh policies, listing, and pruning."""
from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from boxer.config import BoxerConfig, ImageCatalog
from boxer.ipc import ERR_INVALID_PARAMS, IPCError
from boxerd.image_catalog import ImageManager


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _manager(tmp_path: Path, entries: dict) -> ImageManager:
    cfg = BoxerConfig({"state_dir": str(tmp_path)})
    return ImageManager(cfg, ImageCatalog({"images": entries}))


@pytest.fixture(autouse=True)
def _no_url_validation(monkeypatch):
    monkeypatch.setattr("boxerd.image_catalog._validate_url", lambda url: None)


def _stub_downloads(monkeypatch, mgr: ImageManager, state: dict, manifest: bool = False):
    """Wire _download to emit state['content'] and count calls."""
    state.setdefault("downloads", 0)

    async def fake_download(url: str, dest: Path) -> None:
        state["downloads"] += 1
        Path(dest).write_bytes(state["content"])

    monkeypatch.setattr(mgr, "_download", fake_download)
    if manifest:
        async def fake_bytes(url: str) -> bytes:
            return f"{_sha(state['content'])}  base.qcow2\n".encode()
        monkeypatch.setattr(mgr, "_download_bytes", fake_bytes)


async def test_cache_is_digest_addressed(tmp_path, monkeypatch) -> None:
    content = b"FAKE QCOW2 v1"
    digest = _sha(content)
    entry = {"type": "cloud-image", "url": "https://x/base.qcow2", "sha256": digest}
    mgr = _manager(tmp_path, {"test": entry})
    state = {"content": content}
    _stub_downloads(monkeypatch, mgr, state)

    path = await mgr.ensure_image("test")
    assert path.name == f"sha256-{digest}.qcow2"
    assert path.read_bytes() == content
    assert state["downloads"] == 1
    assert mgr.is_cached("test")

    # Second call serves the cached blob without re-downloading.
    again = await mgr.ensure_image("test")
    assert again == path
    assert state["downloads"] == 1


async def test_digest_mismatch_fails_closed(tmp_path, monkeypatch) -> None:
    entry = {"type": "cloud-image", "url": "https://x/base.qcow2", "sha256": "deadbeef" * 8}
    mgr = _manager(tmp_path, {"test": entry})
    _stub_downloads(monkeypatch, mgr, {"content": b"actual bytes"})
    with pytest.raises(IPCError):
        await mgr.ensure_image("test")
    assert not mgr.is_cached("test")


async def test_latest_policy_refreshes_on_manifest_change(tmp_path, monkeypatch) -> None:
    entry = {
        "type": "cloud-image",
        "url": "https://x/base.qcow2",
        "refresh_policy": "latest",
        "verification": {"checksum_url": "https://x/SHA256SUMS", "checksum_filename": "base.qcow2"},
    }
    mgr = _manager(tmp_path, {"test": entry})
    state = {"content": b"v1"}
    _stub_downloads(monkeypatch, mgr, state, manifest=True)

    p1 = await mgr.ensure_image("test")
    assert p1.name == f"sha256-{_sha(b'v1')}.qcow2"

    # Upstream publishes a new image; latest policy refreshes to the new digest.
    state["content"] = b"v2"
    p2 = await mgr.ensure_image("test")
    assert p2.name == f"sha256-{_sha(b'v2')}.qcow2"
    assert p1.exists() and p2.exists()  # old blob retained until pruned
    assert mgr._read_metadata("test")["current"]["digest"] == _sha(b"v2")


async def test_manual_policy_does_not_auto_refresh(tmp_path, monkeypatch) -> None:
    entry = {
        "type": "cloud-image",
        "url": "https://x/base.qcow2",
        "refresh_policy": "manual",
        "verification": {"checksum_url": "https://x/SHA256SUMS", "checksum_filename": "base.qcow2"},
    }
    mgr = _manager(tmp_path, {"test": entry})
    state = {"content": b"v1"}
    _stub_downloads(monkeypatch, mgr, state, manifest=True)

    p1 = await mgr.ensure_image("test")
    state["content"] = b"v2"
    p2 = await mgr.ensure_image("test")
    assert p2 == p1  # still serving v1 despite upstream change
    assert state["downloads"] == 1

    # An explicit admin refresh picks up the new digest.
    refreshed = await mgr.refresh_image("test")
    assert refreshed["current_digest"] == f"sha256:{_sha(b'v2')}"


async def test_pinned_without_hash_is_rejected(tmp_path) -> None:
    entry = {"type": "cloud-image", "url": "https://x/base.qcow2", "refresh_policy": "pinned"}
    mgr = _manager(tmp_path, {"test": entry})
    with pytest.raises(IPCError) as exc:
        await mgr.ensure_image("test")
    assert exc.value.code == ERR_INVALID_PARAMS


def test_list_images_reports_family_and_cache(tmp_path) -> None:
    entries = {
        "ubuntu-24.04": {
            "type": "cloud-image",
            "family": "ubuntu",
            "description": "Ubuntu 24.04",
            "url": "https://x/u.qcow2",
            "default_cpu": 2,
        },
        "arch-latest": {"type": "iso", "family": "arch", "url": "https://x/a.iso", "install": {"method": "manual"}},
    }
    mgr = _manager(tmp_path, entries)
    rows = {r["template"]: r for r in mgr.list_images()}
    assert rows["ubuntu-24.04"]["family"] == "ubuntu"
    assert rows["ubuntu-24.04"]["refresh_policy"] == "latest"
    assert rows["ubuntu-24.04"]["cached"] is False
    assert rows["arch-latest"]["artifact_type"] == "iso"
    assert rows["arch-latest"]["install_method"] == "manual"


async def test_prune_removes_stale_noncurrent_blobs(tmp_path, monkeypatch) -> None:
    entry = {
        "type": "cloud-image",
        "url": "https://x/base.qcow2",
        "refresh_policy": "latest",
        "verification": {"checksum_url": "https://x/SHA256SUMS", "checksum_filename": "base.qcow2"},
    }
    mgr = _manager(tmp_path, {"test": entry})
    state = {"content": b"old"}
    _stub_downloads(monkeypatch, mgr, state, manifest=True)

    old_blob = await mgr.ensure_image("test")
    state["content"] = b"new"
    new_blob = await mgr.ensure_image("test")
    assert old_blob != new_blob

    dry = await mgr.prune(older_than_seconds=0, dry_run=True)
    assert dry["removed_count"] == 1
    assert old_blob.exists()  # dry run does not delete

    result = await mgr.prune(older_than_seconds=0)
    assert result["removed_count"] == 1
    assert not old_blob.exists()  # stale non-current blob removed
    assert new_blob.exists()  # current blob protected
