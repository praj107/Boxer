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
async def test_unknown_project_paths_are_disambiguated(db: Database) -> None:
    await db.upsert_project("p_aaa111", "unknown")
    await db.upsert_project("p_bbb222", "unknown")

    async with db._conn() as conn:
        async with conn.execute("SELECT id,path FROM projects ORDER BY id") as cur:
            rows = await cur.fetchall()

    assert [(row["id"], row["path"]) for row in rows] == [
        ("p_aaa111", "unknown:p_aaa111"),
        ("p_bbb222", "unknown:p_bbb222"),
    ]


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
async def test_install_state_roundtrip_and_count(db: Database) -> None:
    await db.upsert_project("p_aaa111", "/a")
    vm = _make_vm("vm_iso01", "p_aaa111")
    vm.artifact_type = "iso"
    vm.install_state = "installing"
    await db.insert_vm(vm)

    fetched = await db.get_vm("vm_iso01")
    assert fetched.artifact_type == "iso"
    assert fetched.install_state == "installing"
    assert await db.count_installing_vms() == 1

    await db.update_vm_install_state("vm_iso01", "installed")
    assert (await db.get_vm("vm_iso01")).install_state == "installed"
    assert await db.count_installing_vms() == 0


@pytest.mark.asyncio
async def test_cloud_image_vm_has_no_install_state(db: Database) -> None:
    await db.upsert_project("p_aaa111", "/a")
    await db.insert_vm(_make_vm("vm_cloud1", "p_aaa111"))
    fetched = await db.get_vm("vm_cloud1")
    assert fetched.artifact_type == "cloud-image"
    assert fetched.install_state is None


@pytest.mark.asyncio
async def test_events(db: Database) -> None:
    evt = await db.add_event("INFO", "VM created", project_id="p_aaa111", vm_id="vm_abc")
    assert evt.level == "INFO"

    events = await db.list_events()
    assert len(events) >= 1
    assert events[0].message == "VM created"


@pytest.mark.asyncio
async def test_project_call_log_counts_and_prunes(db: Database) -> None:
    now = datetime.now(timezone.utc)
    await db.record_project_call("p_aaa111", "vm.get", "alice")
    await db.record_project_call("p_aaa111", "vm.exec", "alice")
    await db.record_project_call("p_bbb222", "vm.get", "bob")

    assert await db.count_project_calls_since("p_aaa111", now - timedelta(minutes=1)) == 2
    assert await db.count_project_calls_since("p_bbb222", now - timedelta(minutes=1)) == 1

    pruned = await db.prune_project_call_log(now + timedelta(seconds=1))
    assert pruned == 3
    assert await db.count_project_calls_since("p_aaa111", now - timedelta(minutes=1)) == 0


@pytest.mark.asyncio
async def test_cleanup_action_roundtrip(db: Database) -> None:
    await db.upsert_project("p_aaa111", "/a")
    await db.insert_vm(_make_vm("vm_cleanup", "p_aaa111"))
    now = datetime.now(timezone.utc)
    force_after = now + timedelta(minutes=10)

    await db.upsert_cleanup_action(
        "vm_cleanup",
        "p_aaa111",
        "graceful_requested",
        now,
        force_after,
        "shutdown signal failed",
    )

    action = await db.get_cleanup_action("vm_cleanup")
    assert action is not None
    assert action.vm_id == "vm_cleanup"
    assert action.force_after == force_after
    assert action.last_error == "shutdown signal failed"

    await db.delete_cleanup_action("vm_cleanup")
    assert await db.get_cleanup_action("vm_cleanup") is None


@pytest.mark.asyncio
async def test_project_notifications_are_delivered_explicitly(db: Database) -> None:
    note = await db.add_project_notification(
        "p_aaa111",
        "WARN",
        "forced off vm_abc",
        vm_id="vm_abc",
        details={"process_snapshot": {"lines": ["PID COMMAND", "1 init"]}},
    )

    pending = await db.list_pending_project_notifications("p_aaa111")
    assert [n.id for n in pending] == [note.id]
    assert pending[0].details["process_snapshot"]["lines"][1] == "1 init"

    await db.mark_project_notifications_delivered([note.id])
    assert await db.list_pending_project_notifications("p_aaa111") == []
