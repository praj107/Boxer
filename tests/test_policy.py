"""Tests for the policy engine."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from boxer.ipc import IPCError, ERR_POLICY_VIOLATION, ERR_NOT_FOUND
from boxer.types import CallerIdentity, VMRecord
from boxerd.policy import PolicyEngine


def _make_vm(project_id: str = "p_aaa111", owner: str = "alice") -> VMRecord:
    now = datetime.now(timezone.utc)
    return VMRecord(
        id="vm_abc",
        libvirt_name=f"Boxer--{project_id}--test--vm_abc",
        project_id=project_id,
        display_name="test",
        state="running",
        owner_user=owner,
        template="ubuntu-24.04",
        cpu=2,
        ram_mb=2048,
        disk_gb=20,
        headless=True,
        created_at=now,
        last_touched=now,
        lease_until=now + timedelta(hours=1),
    )


def test_owner_can_stop_own_vm() -> None:
    policy = PolicyEngine()
    caller = CallerIdentity(project_id="p_aaa111", user="alice")
    vm = _make_vm("p_aaa111", "alice")
    policy.check("vm.stop", caller, vm)  # should not raise


def test_owner_can_restart_and_get_ssh_access() -> None:
    policy = PolicyEngine()
    caller = CallerIdentity(project_id="p_aaa111", user="alice")
    vm = _make_vm("p_aaa111", "alice")
    policy.check("vm.restart", caller, vm)
    policy.check("vm.ssh_access", caller, vm)


def test_wrong_project_is_denied() -> None:
    policy = PolicyEngine()
    caller = CallerIdentity(project_id="p_bbb222", user="alice")
    vm = _make_vm("p_aaa111", "alice")
    with pytest.raises(IPCError) as exc_info:
        policy.check("vm.stop", caller, vm)
    assert exc_info.value.code == ERR_POLICY_VIOLATION


def test_wrong_user_same_project_is_denied() -> None:
    policy = PolicyEngine()
    caller = CallerIdentity(project_id="p_aaa111", user="mallory")
    vm = _make_vm("p_aaa111", "alice")
    with pytest.raises(IPCError) as exc_info:
        policy.check("vm.delete", caller, vm)
    assert exc_info.value.code == ERR_POLICY_VIOLATION


def test_admin_can_touch_any_vm() -> None:
    policy = PolicyEngine()
    admin = CallerIdentity(project_id="admin", user="root", is_admin=True)
    vm = _make_vm("p_other", "bob")
    policy.check("vm.delete", admin, vm)  # should not raise


def test_missing_vm_for_ownership_action() -> None:
    policy = PolicyEngine()
    caller = CallerIdentity(project_id="p_aaa111", user="alice")
    with pytest.raises(IPCError) as exc_info:
        policy.check("vm.stop", caller, None)
    assert exc_info.value.code == ERR_NOT_FOUND


def test_list_visibility_owner_sees_own() -> None:
    policy = PolicyEngine()
    caller = CallerIdentity(project_id="p_aaa111", user="alice")
    vm = _make_vm("p_aaa111", "alice")
    assert policy.check_vm_list_visibility(caller, vm) is True


def test_list_visibility_other_project_hidden() -> None:
    policy = PolicyEngine()
    caller = CallerIdentity(project_id="p_aaa111", user="alice")
    vm = _make_vm("p_bbb222", "bob")
    assert policy.check_vm_list_visibility(caller, vm) is False


def test_list_visibility_admin_sees_all() -> None:
    policy = PolicyEngine()
    admin = CallerIdentity(project_id="admin", user="root", is_admin=True)
    vm = _make_vm("p_bbb222", "bob")
    assert policy.check_vm_list_visibility(admin, vm) is True
