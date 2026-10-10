"""Background lease expiry scanner."""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Optional

from boxer.config import BoxerConfig, get_config

if TYPE_CHECKING:
    from boxerd.db import Database
    from boxerd.emulator_ops import EmulatorOperations
    from boxerd.guest_agent import GuestAgent
    from boxerd.ssh_keys import SSHKeyManager
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
        guest: Optional["GuestAgent"] = None,
        ssh_keys: Optional["SSHKeyManager"] = None,
        emulator_ops: Optional["EmulatorOperations"] = None,
    ):
        self._db = db
        self._vm_ops = vm_ops
        self._storage = storage
        self._cfg = cfg or get_config()
        self._guest = guest
        self._ssh_keys = ssh_keys
        self._emulator_ops = emulator_ops
        self._task: Optional[asyncio.Task] = None

    def _ops_for(self, vm: VMRecord):
        """Backend dispatch matching the daemon: emulator backend for Android."""
        if vm.kind == "android" and self._emulator_ops is not None:
            return self._emulator_ops
        return self._vm_ops

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
            await asyncio.sleep(max(1, self._cfg.stale_cleanup_interval_minutes * 60))

    async def _scan(self) -> None:
        now = datetime.now(timezone.utc)
        warn_threshold = now - timedelta(minutes=self._cfg.stale_warn_grace_minutes)
        delete_threshold = now - timedelta(hours=self._cfg.stale_delete_grace_hours)
        activity_since = now - timedelta(minutes=self._cfg.inactive_call_window_minutes)
        call_log_retention = now - timedelta(hours=self._cfg.project_call_log_retention_hours)

        vms = await self._db.list_vms()
        for vm in vms:
            lease_expired = vm.lease_until < now
            live_state = self._ops_for(vm).get_state(vm.libvirt_name)
            effective_state = live_state if live_state != "unknown" else vm.state
            if live_state != "unknown" and live_state != vm.state:
                await self._db.update_vm_state(vm.id, live_state)
                vm.state = live_state

            if not lease_expired:
                await self._db.delete_cleanup_action(vm.id)
                continue

            if effective_state in ("running", "paused", "stopping"):
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
                await self._cleanup_running_stale_vm(vm, now, activity_since)
            elif effective_state in ("stopped", "error", "unknown"):
                action = await self._db.get_cleanup_action(vm.id)
                if action:
                    await self._db.delete_cleanup_action(vm.id)
                    await self._db.add_event(
                        "INFO",
                        f"VM '{vm.display_name}' ({vm.id}) stopped after stale cleanup requested graceful shutdown.",
                        project_id=vm.project_id,
                        vm_id=vm.id,
                    )
                # Stopped + past delete grace: auto-clean
                if vm.lease_until < delete_threshold:
                    logger.info("Auto-cleaning stale VM %s (%s)", vm.display_name, vm.id)
                    await self._auto_delete(vm)

        # Expire old queue entries
        expired = await self._db.expire_old_queue_entries(older_than_hours=2)
        if expired:
            logger.info("Expired %d stale queue entries", expired)
        pruned_calls = await self._db.prune_project_call_log(call_log_retention)
        if pruned_calls:
            logger.debug("Pruned %d old project call records", pruned_calls)

    async def _cleanup_running_stale_vm(
        self,
        vm: VMRecord,
        now: datetime,
        activity_since: datetime,
    ) -> None:
        recent_calls = await self._db.count_project_calls_since(vm.project_id, activity_since)
        if recent_calls >= self._cfg.inactive_call_threshold:
            return

        action = await self._db.get_cleanup_action(vm.id)
        if action is None:
            await self._request_graceful_shutdown(vm, now, recent_calls)
            return

        if now >= action.force_after:
            await self._force_shutdown(vm, now, recent_calls)

    async def _request_graceful_shutdown(
        self,
        vm: VMRecord,
        now: datetime,
        recent_calls: int,
    ) -> None:
        force_after = now + timedelta(minutes=self._cfg.stale_force_shutdown_after_minutes)
        loop = asyncio.get_running_loop()
        error = ""
        try:
            await loop.run_in_executor(None, self._ops_for(vm).stop, vm.libvirt_name, True)
        except Exception as exc:
            error = str(exc)
            logger.exception("Failed to request graceful shutdown for stale VM %s", vm.id)

        await self._db.update_vm_state(vm.id, "stopping")
        await self._db.upsert_cleanup_action(
            vm.id,
            vm.project_id,
            "graceful_requested",
            now,
            force_after,
            error,
        )
        await self._db.add_event(
            "WARN",
            (
                f"Stale VM '{vm.display_name}' ({vm.id}) belongs to an inactive repo "
                f"({recent_calls} call(s) in the last {self._cfg.inactive_call_window_minutes} minute(s)); "
                f"requested graceful shutdown. Boxer will force power off after "
                f"{force_after.isoformat()} if it is still running."
            ),
            project_id=vm.project_id,
            vm_id=vm.id,
        )

    async def _force_shutdown(
        self,
        vm: VMRecord,
        now: datetime,
        recent_calls: int,
    ) -> None:
        process_snapshot = await self._collect_process_snapshot(vm, now)
        loop = asyncio.get_running_loop()
        force_error = ""
        try:
            await loop.run_in_executor(None, self._ops_for(vm).stop, vm.libvirt_name, False)
        except Exception as exc:
            force_error = str(exc)
            logger.exception("Failed to force shutdown stale VM %s", vm.id)

        live_state = self._ops_for(vm).get_state(vm.libvirt_name)
        if force_error or live_state in ("running", "paused", "stopping"):
            retry_after = now + timedelta(minutes=self._cfg.stale_force_shutdown_after_minutes)
            last_error = force_error or f"domain still {live_state}"
            await self._db.upsert_cleanup_action(
                vm.id,
                vm.project_id,
                "graceful_requested",
                now,
                retry_after,
                last_error,
            )
            await self._db.add_event(
                "ERROR",
                f"Failed to force stale VM '{vm.display_name}' ({vm.id}) off: {last_error}. "
                f"Will retry after {retry_after.isoformat()}.",
                project_id=vm.project_id,
                vm_id=vm.id,
            )
            return

        final_state = "unknown" if live_state == "unknown" else "stopped"
        await self._db.update_vm_state(vm.id, final_state)
        await self._db.delete_cleanup_action(vm.id)

        message = (
            f"Boxer forced off stale VM '{vm.display_name}' ({vm.id}) after graceful shutdown did not stop it. "
            f"Repo activity was {recent_calls} call(s) in the last "
            f"{self._cfg.inactive_call_window_minutes} minute(s). "
            f"{self._snapshot_notice_line(process_snapshot)}"
        )
        details = {
            "vm_id": vm.id,
            "display_name": vm.display_name,
            "libvirt_name": vm.libvirt_name,
            "forced_at": now.isoformat(),
            "live_state_after": live_state,
            "recent_call_count": recent_calls,
            "call_window_minutes": self._cfg.inactive_call_window_minutes,
            "process_snapshot": process_snapshot,
        }
        await self._db.add_event("WARN", message, project_id=vm.project_id, vm_id=vm.id)
        await self._db.add_project_notification(
            vm.project_id,
            "WARN",
            message,
            vm_id=vm.id,
            details=details,
        )

    async def _collect_process_snapshot(self, vm: VMRecord, now: datetime) -> dict[str, object]:
        if vm.kind != "vm":
            return {
                "available": False,
                "captured_at": now.isoformat(),
                "reason": "process snapshot unavailable for non-VM device kind",
                "lines": [],
            }
        if self._guest is None:
            return {
                "available": False,
                "captured_at": now.isoformat(),
                "reason": "guest process snapshot unavailable: guest operations are not configured",
                "lines": [],
            }

        ip_address = vm.ip_address
        if not ip_address:
            try:
                ip_address = await self._vm_ops.get_ip_via_guest_agent(vm.libvirt_name, timeout=5)
            except Exception as exc:
                logger.debug("IP lookup failed for process snapshot on %s: %s", vm.id, exc)
            if ip_address:
                await self._db.update_vm_ip(vm.id, ip_address)

        if not ip_address:
            return {
                "available": False,
                "captured_at": now.isoformat(),
                "reason": "guest process snapshot unavailable: VM has no known IP address",
                "lines": [],
            }

        fallback_key = None
        if self._ssh_keys is not None:
            vm_dir = self._storage.vm_dir(vm.project_id, vm.id)
            if self._ssh_keys.has_key(vm_dir):
                fallback_key = self._ssh_keys.key_path(vm_dir)

        command = (
            "printf 'top cpu\\n'; "
            "ps -eo pid,ppid,stat,pcpu,pmem,comm --sort=-pcpu | head -n 8; "
            "printf '\\ntop mem\\n'; "
            "ps -eo pid,ppid,stat,pcpu,pmem,comm --sort=-pmem | head -n 8"
        )
        try:
            result = await self._guest.exec_ssh(
                ip_address,
                command,
                timeout_seconds=10,
                fallback_privkey_path=fallback_key,
            )
        except Exception as exc:
            return {
                "available": False,
                "captured_at": now.isoformat(),
                "ip_address": ip_address,
                "reason": f"guest process snapshot unavailable: {exc}",
                "lines": [],
            }

        lines = (result.get("stdout") or "").strip().splitlines()
        max_lines = max(1, min(self._cfg.cleanup_process_snapshot_lines, 100))
        snapshot = {
            "available": result.get("exit_code", 1) == 0,
            "captured_at": now.isoformat(),
            "ip_address": ip_address,
            "exit_code": result.get("exit_code", 1),
            "lines": lines[:max_lines],
        }
        stderr = (result.get("stderr") or "").strip()
        if stderr:
            snapshot["stderr"] = stderr[:500]
        return snapshot

    @staticmethod
    def _snapshot_notice_line(snapshot: dict[str, object]) -> str:
        if not snapshot.get("available"):
            reason = str(snapshot.get("reason") or "process snapshot unavailable")
            return f"Process snapshot: {reason}."
        lines = [str(line).strip() for line in snapshot.get("lines", []) if str(line).strip()]
        process_lines = [
            line
            for line in lines
            if not line.lower().startswith("top ") and not line.lstrip().startswith("PID")
        ]
        summary = "; ".join(process_lines[:3])
        if not summary:
            summary = "captured, but no process rows were returned"
        return f"Process snapshot top rows: {summary}."

    async def _auto_delete(self, vm: VMRecord) -> None:
        try:
            self._ops_for(vm).undefine(vm.libvirt_name)
            if vm.kind == "vm" and vm.origin == "boxer":
                self._storage.delete_vm_storage(vm.project_id, vm.id)
            await self._db.delete_cleanup_action(vm.id)
            await self._db.delete_vm(vm.id)
            await self._db.add_event(
                "INFO",
                f"Auto-deleted stale VM {vm.id} ({vm.libvirt_name})",
                project_id=vm.project_id,
                vm_id=vm.id,
            )
        except Exception as exc:
            logger.exception("Failed to auto-delete VM %s: %s", vm.id, exc)
