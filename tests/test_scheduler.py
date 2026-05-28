"""Tests for the scheduler / admission control."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from boxer.types import VMRecord
from boxerd.scheduler import HostCapacity, Scheduler


def _mock_capacity(free_ram_gib: float = 8.0, free_vcpus: int = 8) -> HostCapacity:
    cap = MagicMock(spec=HostCapacity)
    cap.get_status.return_value = {
        "total_ram_gib": 16.0,
        "usable_ram_gib": 12.0,
        "free_ram_gib": free_ram_gib,
        "host_threads": 8,
        "max_vcpus": 6,
        "used_vcpus": 6 - free_vcpus,
        "free_vcpus": free_vcpus,
        "running_vms": 0,
    }
    return cap


def _make_running_vm(project_id: str = "p_aaa111", disk_gb: int = 20) -> VMRecord:
    now = datetime.now(timezone.utc)
    return VMRecord(
        id="vm_xyz",
        libvirt_name="Boxer--test--vm_xyz",
        project_id=project_id,
        display_name="test",
        state="running",
        owner_user="alice",
        template="ubuntu-24.04",
        cpu=2,
        ram_mb=2048,
        disk_gb=disk_gb,
        headless=True,
        created_at=now,
        last_touched=now,
        lease_until=now + timedelta(hours=1),
    )


@pytest.mark.asyncio
async def test_admitted_when_resources_available(db, cfg) -> None:
    cap = _mock_capacity(free_ram_gib=8.0, free_vcpus=8)
    sched = Scheduler(db, cap, AsyncMock(), cfg)
    reason = await sched.check_admission("p_aaa111", cpu=2, ram_mb=2048, disk_gb=20)
    assert reason is None


@pytest.mark.asyncio
async def test_denied_when_insufficient_ram(db, cfg) -> None:
    cap = _mock_capacity(free_ram_gib=1.0)
    sched = Scheduler(db, cap, AsyncMock(), cfg)
    reason = await sched.check_admission("p_aaa111", cpu=2, ram_mb=4096, disk_gb=20)
    assert reason is not None
    assert "RAM" in reason


@pytest.mark.asyncio
async def test_denied_when_insufficient_vcpus(db, cfg) -> None:
    cap = _mock_capacity(free_vcpus=0)
    sched = Scheduler(db, cap, AsyncMock(), cfg)
    reason = await sched.check_admission("p_aaa111", cpu=2, ram_mb=512, disk_gb=20)
    assert reason is not None
    assert "vCPU" in reason


@pytest.mark.asyncio
async def test_denied_when_project_at_vm_limit(db, cfg) -> None:
    await db.upsert_project("p_aaa111", "/a")
    # Insert 2 running VMs (max_running_per_project=2 in test cfg)
    for i in range(2):
        vm = _make_running_vm()
        vm.id = f"vm_{i:03d}"
        vm.libvirt_name = f"Boxer--p_aaa111--test--vm_{i:03d}"
        await db.insert_vm(vm)

    cap = _mock_capacity()
    sched = Scheduler(db, cap, AsyncMock(), cfg)
    reason = await sched.check_admission("p_aaa111", cpu=2, ram_mb=512, disk_gb=20)
    assert reason is not None
    assert "running" in reason.lower()


@pytest.mark.asyncio
async def test_denied_when_project_over_disk_limit(db, cfg) -> None:
    await db.upsert_project("p_aaa111", "/a")
    vm = _make_running_vm(disk_gb=40)
    await db.insert_vm(vm)

    cap = _mock_capacity()
    sched = Scheduler(db, cap, AsyncMock(), cfg)
    reason = await sched.check_admission("p_aaa111", cpu=2, ram_mb=512, disk_gb=20)
    assert reason is not None
    assert "disk" in reason.lower()


@pytest.mark.asyncio
async def test_enqueue_returns_id(db, cfg) -> None:
    cap = _mock_capacity(free_ram_gib=0)
    sched = Scheduler(db, cap, AsyncMock(), cfg)
    queue_id = await sched.enqueue_request("p_aaa111", {"template": "ubuntu-24.04"}, "no RAM")
    assert queue_id.startswith("q_")

    pending = await db.list_pending_queue()
    assert len(pending) == 1
