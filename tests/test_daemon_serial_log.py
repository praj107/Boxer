"""Tests for serial-log handler response shape."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from boxer.types import VMRecord
from boxerd.daemon import BoxerDaemon
from boxerd.policy import PolicyEngine


def _make_vm(tmp_path: Path) -> VMRecord:
    now = datetime.now(timezone.utc)
    return VMRecord(
        id="vm_abc123",
        libvirt_name="Boxer--p_aaa111--test--vm_abc123",
        project_id="p_aaa111",
        display_name="test",
        state="running",
        owner_user="alice",
        template="local:image.iso",
        cpu=2,
        ram_mb=512,
        disk_gb=2,
        headless=True,
        created_at=now,
        last_touched=now,
        lease_until=now + timedelta(minutes=30),
        artifact_type="iso",
    )


@pytest.mark.asyncio
async def test_serial_log_missing_file_has_stable_shape(tmp_path: Path) -> None:
    vm = _make_vm(tmp_path)
    daemon = object.__new__(BoxerDaemon)
    daemon._db = AsyncMock()
    daemon._db.get_vm.return_value = vm
    daemon._policy = PolicyEngine()
    daemon._storage = SimpleNamespace(vm_dir=lambda project_id, vm_id: tmp_path)

    result = await BoxerDaemon._h_vm_serial_log(
        daemon,
        {
            "caller_project_id": "p_aaa111",
            "caller_user": "alice",
            "vm_id": "vm_abc123",
            "tail_lines": 500,
        },
    )

    assert result == {
        "vm_id": "vm_abc123",
        "lines": [],
        "total_lines": 0,
        "returned_lines": 0,
        "note": "No serial log yet",
    }
