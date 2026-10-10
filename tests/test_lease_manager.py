"""Tests for stale running VM cleanup."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from boxer.config import BoxerConfig
from boxer.types import VMRecord
from boxerd.db import Database
from boxerd.lease_manager import LeaseManager


def _expired_vm(vm_id: str = "vm_stale", project_id: str = "p_aaa111") -> VMRecord:
    now = datetime.now(timezone.utc)
    return VMRecord(
        id=vm_id,
        libvirt_name=f"Boxer--{project_id}--stale--{vm_id}",
        project_id=project_id,
        display_name="stale",
        state="running",
        owner_user="alice",
        template="ubuntu-24.04",
        cpu=2,
        ram_mb=2048,
        disk_gb=20,
        headless=True,
        ip_address="10.200.0.10",
        created_at=now - timedelta(hours=2),
        last_touched=now - timedelta(hours=2),
        lease_until=now - timedelta(minutes=30),
    )


class FakeVMOps:
    def __init__(self, state: str = "running", force_state: str = "stopped"):
        self.state = state
        self.force_state = force_state
        self.stop_calls: list[tuple[str, bool]] = []
        self.undefined: list[str] = []

    def get_state(self, libvirt_name: str) -> str:
        return self.state

    def stop(self, libvirt_name: str, graceful: bool = True) -> None:
        self.stop_calls.append((libvirt_name, graceful))
        if not graceful:
            self.state = self.force_state

    def undefine(self, libvirt_name: str) -> None:
        self.undefined.append(libvirt_name)

    async def get_ip_via_guest_agent(self, libvirt_name: str, timeout: float = 30.0) -> str:
        return "10.200.0.10"


class FakeStorage:
    def __init__(self, root: Path):
        self.root = root
        self.deleted: list[tuple[str, str]] = []

    def vm_dir(self, project_id: str, vm_id: str) -> Path:
        return self.root / project_id / vm_id

    def delete_vm_storage(self, project_id: str, vm_id: str) -> None:
        self.deleted.append((project_id, vm_id))


class FakeGuest:
    def __init__(self) -> None:
        self.commands: list[str] = []

    async def exec_ssh(
        self,
        ip_address: str,
        command: str,
        timeout_seconds: int = 30,
        fallback_privkey_path: Path | None = None,
    ) -> dict[str, Any]:
        self.commands.append(command)
        return {
            "stdout": "top cpu\nPID PPID STAT %CPU %MEM COMMAND\n42 1 R 91.0 1.0 python\n",
            "stderr": "",
            "exit_code": 0,
        }


@pytest.fixture
def cleanup_cfg(tmp_path: Path) -> BoxerConfig:
    return BoxerConfig(
        {
            "state_dir": str(tmp_path),
            "stale_cleanup_interval_minutes": 10,
            "inactive_call_window_minutes": 10,
            "inactive_call_threshold": 3,
            "stale_force_shutdown_after_minutes": 10,
            "stale_warn_grace_minutes": 5,
            "stale_delete_grace_hours": 24,
        }
    )


@pytest.mark.asyncio
async def test_inactive_stale_running_vm_gets_graceful_shutdown(
    db: Database,
    cleanup_cfg: BoxerConfig,
    tmp_path: Path,
) -> None:
    vm_ops = FakeVMOps()
    vm = _expired_vm()
    await db.upsert_project(vm.project_id, "/repo")
    await db.insert_vm(vm)

    manager = LeaseManager(db, vm_ops, FakeStorage(tmp_path), cleanup_cfg)
    await manager._scan()

    assert vm_ops.stop_calls == [(vm.libvirt_name, True)]
    updated = await db.get_vm(vm.id)
    assert updated.state == "stopping"
    action = await db.get_cleanup_action(vm.id)
    assert action is not None
    assert action.stage == "graceful_requested"


@pytest.mark.asyncio
async def test_active_repo_stale_running_vm_is_not_stopped(
    db: Database,
    cleanup_cfg: BoxerConfig,
    tmp_path: Path,
) -> None:
    vm_ops = FakeVMOps()
    vm = _expired_vm()
    await db.upsert_project(vm.project_id, "/repo")
    await db.insert_vm(vm)
    for _ in range(cleanup_cfg.inactive_call_threshold):
        await db.record_project_call(vm.project_id, "vm.get", "alice")

    manager = LeaseManager(db, vm_ops, FakeStorage(tmp_path), cleanup_cfg)
    await manager._scan()

    assert vm_ops.stop_calls == []
    assert await db.get_cleanup_action(vm.id) is None


@pytest.mark.asyncio
async def test_force_shutdown_queues_notice_with_process_snapshot(
    db: Database,
    cleanup_cfg: BoxerConfig,
    tmp_path: Path,
) -> None:
    now = datetime.now(timezone.utc)
    vm_ops = FakeVMOps()
    guest = FakeGuest()
    vm = _expired_vm()
    await db.upsert_project(vm.project_id, "/repo")
    await db.insert_vm(vm)
    await db.upsert_cleanup_action(
        vm.id,
        vm.project_id,
        "graceful_requested",
        now - timedelta(minutes=20),
        now - timedelta(minutes=1),
    )

    manager = LeaseManager(db, vm_ops, FakeStorage(tmp_path), cleanup_cfg, guest=guest)
    await manager._scan()

    assert vm_ops.stop_calls == [(vm.libvirt_name, False)]
    updated = await db.get_vm(vm.id)
    assert updated.state == "stopped"
    assert await db.get_cleanup_action(vm.id) is None
    pending = await db.list_pending_project_notifications(vm.project_id)
    assert len(pending) == 1
    assert "forced off stale VM" in pending[0].message
    assert "42 1 R 91.0 1.0 python" in pending[0].message
    assert pending[0].details["process_snapshot"]["lines"][2] == "42 1 R 91.0 1.0 python"
