"""Ownership and action policy enforcement."""
from __future__ import annotations

import logging
from typing import Optional

from boxer.ipc import ERR_NOT_FOUND, ERR_POLICY_VIOLATION, IPCError
from boxer.types import CallerIdentity, VMRecord

logger = logging.getLogger(__name__)

# Actions that require the caller to own the VM
_OWNERSHIP_REQUIRED_ACTIONS = {
    "vm.start",
    "vm.stop",
    "vm.delete",
    "vm.extend_lease",
    "vm.snapshot",
    "vm.exec",
    "vm.screenshot",
    "vm.input",
    "vm.purge_ghost",
}

# Actions that are always blocked unless admin
_ADMIN_ONLY_ACTIONS: set[str] = set()


class PolicyEngine:
    def check(
        self,
        action: str,
        caller: CallerIdentity,
        vm: Optional[VMRecord] = None,
    ) -> None:
        if action in _ADMIN_ONLY_ACTIONS and not caller.is_admin:
            raise IPCError(ERR_POLICY_VIOLATION, f"Action '{action}' requires admin privileges")

        if action in _OWNERSHIP_REQUIRED_ACTIONS:
            if vm is None:
                raise IPCError(ERR_NOT_FOUND, "VM not found")
            if caller.is_admin:
                return
            if vm.project_id != caller.project_id:
                raise IPCError(
                    ERR_POLICY_VIOLATION,
                    f"VM belongs to project {vm.project_id}, caller is in {caller.project_id}",
                )
            if vm.owner_user != caller.user:
                raise IPCError(
                    ERR_POLICY_VIOLATION,
                    f"VM is owned by '{vm.owner_user}', caller is '{caller.user}'",
                )

    def check_vm_list_visibility(self, caller: CallerIdentity, vm: VMRecord) -> bool:
        """Return True if the caller should see this VM in listings."""
        if caller.is_admin:
            return True
        return vm.project_id == caller.project_id


def caller_from_params(params: dict) -> CallerIdentity:
    return CallerIdentity(
        project_id=params.get("caller_project_id", "p_unknown"),
        user=params.get("caller_user", "unknown"),
        is_admin=params.get("caller_is_admin", False),
    )
