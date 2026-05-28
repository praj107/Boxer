"""Tests for the database layer."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from boxer.types import VMRecord
from boxerd.db import Database


def _make_vm(
    vm_id: str = "vm_abc123",
    project_id: str = "p_aaa111",
    state: str = "running",
) -> VMRecord:
    now = datetime.now(timezone.utc)
    return VMRecord(
        id=vm_id,
        libvirt_name=f"Boxer--{project_id}--test--{vm_id}",
        project_id=project_id,
        display_name="test",
        state=state,
        owner_user="alice",
        template="ubuntu-24.04",
        cpu=2,
        ram_mb=2048,
        disk_gb=20,
        headless=True,
        created_at=now,
        last_touched=now,
        lease_until=now + timedelta(hours=1),
        tags={"env": "test"},
    )


@pytest.mark.asyncio
async def test_insert_and_get_vm(db: Database) -> None:
    await db.upsert_project("p_aaa111", "/home/alice/proj")
    vm = _make_vm()
    await db.insert_vm(vm)

    fetched = await db.get_vm("vm_abc123")
    assert fetched is not None
    assert fetched.display_name == "test"
    assert fetched.tags == {"env": "test"}


@pytest.mark.asyncio
async def test_list_vms_by_project(db: Database) -> None:
    await db.upsert_project("p_aaa111", "/a")
    await db.upsert_project("p_bbb222", "/b")
    await db.insert_vm(_make_vm("vm_001", "p_aaa111"))
    await db.insert_vm(_make_vm("vm_002", "p_aaa111"))
    await db.insert_vm(_make_vm("vm_003", "p_bbb222"))

    vms_a = await db.list_vms("p_aaa111")
    assert len(vms_a) == 2

    vms_b = await db.list_vms("p_bbb222")
    assert len(vms_b) == 1

    all_vms = await db.list_vms()
    assert len(all_vms) == 3


@pytest.mark.asyncio
async def test_update_vm_state(db: Database) -> None:
    await db.upsert_project("p_aaa111", "/a")
    await db.insert_vm(_make_vm())
    await db.update_vm_state("vm_abc123", "stopped")
    vm = await db.get_vm("vm_abc123")
    assert vm.state == "stopped"


@pytest.mark.asyncio
async def test_delete_vm(db: Database) -> None:
    await db.upsert_project("p_aaa111", "/a")
    await db.insert_vm(_make_vm())
    await db.delete_vm("vm_abc123")
    assert await db.get_vm("vm_abc123") is None


@pytest.mark.asyncio
async def test_queue_operations(db: Database) -> None:
    entry_id = await db.enqueue("p_aaa111", {"template": "ubuntu-24.04"}, "no RAM")
    assert entry_id.startswith("q_")

    pending = await db.list_pending_queue()
    assert len(pending) == 1
    assert pending[0].reason == "no RAM"

    await db.update_queue_status(entry_id, "done")
    pending = await db.list_pending_queue()
    assert len(pending) == 0


@pytest.mark.asyncio
async def test_events(db: Database) -> None:
    evt = await db.add_event("INFO", "VM created", project_id="p_aaa111", vm_id="vm_abc")
    assert evt.level == "INFO"

    events = await db.list_events()
    assert len(events) >= 1
    assert events[0].message == "VM created"
