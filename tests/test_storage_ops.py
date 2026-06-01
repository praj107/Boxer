"""Tests for storage artifact staging and permission normalization."""
from __future__ import annotations

import stat
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from boxer.config import BoxerConfig
from boxerd.storage_ops import StorageManager


@pytest.fixture
def storage_cfg(tmp_path: Path) -> BoxerConfig:
    return BoxerConfig(
        {
            "state_dir": str(tmp_path / "state"),
            "qemu_group": "definitely-missing-qemu-group",
        }
    )


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


@pytest.mark.asyncio
async def test_create_blank_disk_prepares_qemu_writable_artifacts(
    storage_cfg: BoxerConfig,
) -> None:
    storage = StorageManager(MagicMock(), storage_cfg)

    disk = await storage.create_blank_disk("p_aaa111", "vm_abc123", 1)

    assert disk.name == "disk.qcow2"
    assert _mode(storage_cfg.state_dir) == 0o750
    assert _mode(disk.parent) == 0o750
    assert _mode(disk) == 0o660


@pytest.mark.asyncio
async def test_create_blank_raw_disk_for_nvme_test_boot(storage_cfg: BoxerConfig) -> None:
    storage = StorageManager(MagicMock(), storage_cfg)

    disk = await storage.create_blank_disk(
        "p_aaa111",
        "vm_abc123",
        1,
        disk_format="raw",
    )

    assert disk.name == "disk.raw"
    assert _mode(disk) == 0o660


@pytest.mark.asyncio
async def test_stage_iso_copies_local_iso_into_vm_dir(storage_cfg: BoxerConfig, tmp_path: Path) -> None:
    source = tmp_path / "image.iso"
    source.write_bytes(b"iso bytes")
    storage = StorageManager(MagicMock(), storage_cfg)

    staged = await storage.stage_iso("p_aaa111", "vm_abc123", source)

    assert staged == storage.vm_dir("p_aaa111", "vm_abc123") / "install.iso"
    assert staged.read_bytes() == b"iso bytes"
    assert _mode(staged) == 0o440
