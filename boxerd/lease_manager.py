"""Background lease expiry scanner."""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Optional

from boxer.config import BoxerConfig, get_config

if TYPE_CHECKING:
    from boxerd.db import Database
    from boxerd.vm_ops import VMOperations
    from boxerd.storage_ops import StorageManager

from boxer.types import VMRecord

logger = logging.getLogger(__name__)


class LeaseManager:
    def __init__(
        self,
        db: "Database",
        vm_ops: "VMOperations",
        storage: "StorageManager",
        cfg: Optional[BoxerConfig] = None,
    ):
        self._db = db
        self._vm_ops = vm_ops
        self._storage = storage
        self._cfg = cfg or get_config()
        self._task: Optional[asyncio.Task] = None

    def start(self) -> None:
        self._task = asyncio.create_task(self._scan_loop())

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    async def _scan_loop(self) -> None:
        while True:
            try:
                await self._scan()
            except Exception:
                logger.exception("Error in lease scan")
            await asyncio.sleep(60)

    async def _scan(self) -> None:
        now = datetime.now(timezone.utc)
        warn_threshold = now - timedelta(minutes=self._cfg.stale_warn_grace_minutes)
        delete_threshold = now - timedelta(hours=self._cfg.stale_delete_grace_hours)

        vms = await self._db.list_vms()
        for vm in vms:
            lease_expired = vm.lease_until < now

            if not lease_expired:
                continue

            if vm.state == "running":
                # Running past lease: warn
                await self._db.add_event(
                    "WARN",
                    f"VM '{vm.display_name}' ({vm.id}) lease expired at {vm.lease_until.isoformat()}. "
                    f"Use box_extend_lease or box_stop_vm.",
                    project_id=vm.project_id,
                    vm_id=vm.id,
                )
                # If running and idle for longer than warn grace: notify again
                if vm.last_touched < warn_threshold:
                    logger.warning("VM %s (%s) is running past lease", vm.display_name, vm.id)
            elif vm.state in ("stopped", "error", "unknown"):
                # Stopped + past delete grace: auto-clean
                if vm.lease_until < delete_threshold:
                    logger.info("Auto-cleaning stale VM %s (%s)", vm.display_name, vm.id)
                    await self._auto_delete(vm)

        # Expire old queue entries
        expired = await self._db.expire_old_queue_entries(older_than_hours=2)
        if expired:
            logger.info("Expired %d stale queue entries", expired)

    async def _auto_delete(self, vm: VMRecord) -> None:
        try:
            self._vm_ops.undefine(vm.libvirt_name)
            if vm.origin == "boxer":
                self._storage.delete_vm_storage(vm.project_id, vm.id)
            await self._db.delete_vm(vm.id)
            await self._db.add_event(
                "INFO",
                f"Auto-deleted stale VM {vm.id} ({vm.libvirt_name})",
                project_id=vm.project_id,
                vm_id=vm.id,
            )
        except Exception as exc:
            logger.exception("Failed to auto-delete VM %s: %s", vm.id, exc)
