"""Boxerd main daemon: wires all components and registers IPC handlers."""
from __future__ import annotations

import asyncio
import logging
import re
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

import libvirt

from boxer.config import BoxerConfig, get_config
from boxer.ipc import ERR_INVALID_PARAMS, ERR_NOT_FOUND, IPCError
from boxer.types import VMRecord
from boxerd.cloud_init import CloudInitBuilder
from boxerd.db import Database
from boxerd.guest_agent import GuestAgent
from boxerd.image_catalog import ImageManager
from boxerd.ipc_server import IPCServer
from boxerd.lease_manager import LeaseManager
from boxerd.network_ops import NetworkManager
from boxerd.policy import PolicyEngine, caller_from_params
from boxerd.reconcile import build_reconcile_report, parse_boxer_name, _extract_disk_path, _get_disk_size_gb
from boxerd.scheduler import HostCapacity, Scheduler
from boxerd.ssh_keys import SSHKeyManager, SSHKeyMaterial
from boxerd.storage_ops import StorageManager
from boxerd.vm_ops import VMOperations

logger = logging.getLogger(__name__)


def _make_vm_id() -> str:
    return "vm_" + uuid.uuid4().hex[:6]


def _make_libvirt_name(project_id: str, display_name: str, vm_id: str) -> str:
    safe = "".join(c if c.isalnum() or c == "-" else "-" for c in display_name)[:30]
    return f"Boxer--{project_id}--{safe}--{vm_id}"


_PACKAGE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9+_.:-]{0,127}$")


def _string_list(
    value: Any,
    field: str,
    *,
    max_items: int,
    max_len: int,
) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise IPCError(ERR_INVALID_PARAMS, f"{field} must be a list of strings")
    result: list[str] = []
    for item in value:
        if not isinstance(item, str):
            raise IPCError(ERR_INVALID_PARAMS, f"{field} must be a list of strings")
        item = item.strip()
        if not item:
            continue
        if len(item) > max_len:
            raise IPCError(ERR_INVALID_PARAMS, f"{field} entries must be <= {max_len} characters")
        result.append(item)
    if len(result) > max_items:
        raise IPCError(ERR_INVALID_PARAMS, f"{field} accepts at most {max_items} entries")
    return result


def _bootstrap_from_params(params: dict[str, Any]) -> dict[str, list[str]]:
    bootstrap = params.get("bootstrap") or {}
    if bootstrap and not isinstance(bootstrap, dict):
        raise IPCError(ERR_INVALID_PARAMS, "bootstrap must be an object")

    packages = _string_list(
        params.get("bootstrap_packages", bootstrap.get("packages")),
        "bootstrap_packages",
        max_items=50,
        max_len=128,
    )
    invalid_packages = [pkg for pkg in packages if not _PACKAGE_RE.match(pkg)]
    if invalid_packages:
        raise IPCError(ERR_INVALID_PARAMS, f"Invalid package names: {invalid_packages}")

    commands = _string_list(
        params.get("bootstrap_commands", bootstrap.get("commands")),
        "bootstrap_commands",
        max_items=20,
        max_len=2000,
    )
    ssh_public_keys = _string_list(
        params.get("ssh_public_keys", bootstrap.get("ssh_public_keys")),
        "ssh_public_keys",
        max_items=10,
        max_len=4096,
    )
    valid_prefixes = (
        "ssh-ed25519 ",
        "ssh-rsa ",
        "ecdsa-sha2-",
        "sk-ssh-ed25519@",
        "sk-ecdsa-sha2-",
    )
    invalid_keys = [key for key in ssh_public_keys if not key.startswith(valid_prefixes)]
    if invalid_keys:
        raise IPCError(ERR_INVALID_PARAMS, "ssh_public_keys entries must be OpenSSH public keys")

    return {
        "packages": packages,
        "commands": commands,
        "ssh_public_keys": ssh_public_keys,
    }


class BoxerDaemon:
    def __init__(self, cfg: Optional[BoxerConfig] = None):
        self._cfg = cfg or get_config()
        self._conn: Optional[libvirt.virConnect] = None
        self._db = Database(self._cfg.db_path)
        self._server = IPCServer(self._cfg.socket_path, self._cfg.notify_socket_path)
        self._policy = PolicyEngine()
        self._image_manager: Optional[ImageManager] = None
        self._storage: Optional[StorageManager] = None
        self._network: Optional[NetworkManager] = None
        self._vm_ops: Optional[VMOperations] = None
        self._guest: Optional[GuestAgent] = None
        self._capacity: Optional[HostCapacity] = None
        self._scheduler: Optional[Scheduler] = None
        self._lease_manager: Optional[LeaseManager] = None
        self._cloud_init = CloudInitBuilder()
        self._ssh_keys = SSHKeyManager()

    async def start(self) -> None:
        logger.info("BoxerD starting")
        self._conn = libvirt.open(self._cfg.libvirt_uri)
        if self._conn is None:
            raise RuntimeError(f"Failed to connect to libvirt at {self._cfg.libvirt_uri}")

        await self._db.open()

        self._image_manager = ImageManager(self._cfg)
        self._storage = StorageManager(self._conn, self._cfg)
        self._network = NetworkManager(self._conn, self._cfg)
        self._vm_ops = VMOperations(self._conn, self._cfg)
        self._guest = GuestAgent(self._conn, self._cfg)
        self._capacity = HostCapacity(self._conn, self._cfg)
        self._scheduler = Scheduler(
            self._db, self._capacity, self._create_vm_from_request, self._cfg
        )
        self._lease_manager = LeaseManager(self._db, self._vm_ops, self._storage, self._cfg)

        self._storage.ensure_pool()
        self._register_handlers()

        await self._server.start()
        self._scheduler.start_queue_runner()
        self._lease_manager.start()

        logger.info("BoxerD ready")

    async def stop(self) -> None:
        if self._lease_manager:
            await self._lease_manager.stop()
        if self._scheduler:
            await self._scheduler.stop()
        await self._server.stop()
        await self._db.close()
        if self._conn:
            self._conn.close()
        logger.info("BoxerD stopped")

    def _register_handlers(self) -> None:
        reg = self._server.register
        reg("vm.request", self._h_vm_request)
        reg("vm.list", self._h_vm_list)
        reg("vm.get", self._h_vm_get)
        reg("vm.start", self._h_vm_start)
        reg("vm.stop", self._h_vm_stop)
        reg("vm.restart", self._h_vm_restart)
        reg("vm.ssh_access", self._h_vm_ssh_access)
        reg("vm.delete", self._h_vm_delete)
        reg("vm.extend_lease", self._h_vm_extend_lease)
        reg("vm.snapshot", self._h_vm_snapshot)
        reg("vm.exec", self._h_vm_exec)
        reg("vm.screenshot", self._h_vm_screenshot)
        reg("vm.input", self._h_vm_input)
        reg("resource.status", self._h_resource_status)
        reg("queue.status", self._h_queue_status)
        reg("cleanup.plan", self._h_cleanup_plan)
        reg("event.poll", self._h_event_poll)
        reg("vm.scan", self._h_vm_scan)
        reg("vm.import", self._h_vm_import)
        reg("vm.adopt", self._h_vm_adopt)
        reg("vm.purge_ghost", self._h_vm_purge_ghost)

        self._server.register_notify("event.poll", self._h_event_poll)

    # ------------------------------------------------------------------ vm.request

    async def _h_vm_request(self, params: dict[str, Any]) -> dict[str, Any]:
        caller = caller_from_params(params)
        template = params.get("template", "ubuntu-24.04")
        purpose = params.get("purpose", "vm")
        headless = bool(params.get("headless", True))
        ttl_minutes = int(params.get("ttl_minutes", self._cfg.default_ttl_minutes))
        tags = params.get("tags") or {}
        bootstrap = _bootstrap_from_params(params)
        ssh_access = bool(params.get("ssh_access", False) or params.get("return_ssh_private_key", False))
        return_ssh_private_key = bool(params.get("return_ssh_private_key", False))
        wait_for_ip_seconds = int(params.get("wait_for_ip_seconds", 0) or 0)

        catalog_entry = self._image_manager._catalog.get(template)
        if catalog_entry is None:
            raise IPCError(ERR_INVALID_PARAMS, f"Unknown template: {template}")

        cpu = int(params.get("cpu") or catalog_entry.get("default_cpu", 2))
        ram_mb = int(params.get("ram_mb") or catalog_entry.get("default_ram_mb", 2048))
        disk_gb = int(params.get("disk_gb") or catalog_entry.get("default_disk_gb", 20))

        reason = await self._scheduler.check_admission(caller.project_id, cpu, ram_mb, disk_gb)
        if reason:
            queue_id = await self._scheduler.enqueue_request(
                caller.project_id,
                {**params, "caller_project_id": caller.project_id, "caller_user": caller.user},
                reason,
            )
            return {
                "status": "queued",
                "queue_id": queue_id,
                "reason": reason,
                "retry_after_seconds": 60,
                "current_capacity": self._capacity.get_status(),
                "note": "If ssh_access was requested, retrieve it after promotion with box_get_ssh_access.",
            }

        vm, ssh_access_info = await self._create_vm(
            caller_project_id=caller.project_id,
            caller_user=caller.user,
            template=template,
            purpose=purpose,
            headless=headless,
            ttl_minutes=ttl_minutes,
            cpu=cpu,
            ram_mb=ram_mb,
            disk_gb=disk_gb,
            tags=tags,
            bootstrap=bootstrap,
            ssh_access=ssh_access,
            include_private_key=return_ssh_private_key,
            wait_for_ip_seconds=wait_for_ip_seconds,
        )
        result = _vm_to_dict(vm)
        if ssh_access_info:
            result["ssh_access"] = ssh_access_info
        return result

    async def _create_vm_from_request(self, request: dict[str, Any]) -> None:
        request = {**request, "return_ssh_private_key": False}
        await self._h_vm_request(request)

    async def _create_vm(
        self,
        *,
        caller_project_id: str,
        caller_user: str,
        template: str,
        purpose: str,
        headless: bool,
        ttl_minutes: int,
        cpu: int,
        ram_mb: int,
        disk_gb: int,
        tags: dict[str, str],
        bootstrap: Optional[dict[str, list[str]]] = None,
        ssh_access: bool = False,
        include_private_key: bool = False,
        wait_for_ip_seconds: int = 0,
    ) -> tuple[VMRecord, Optional[dict[str, Any]]]:
        vm_id = _make_vm_id()
        libvirt_name = _make_libvirt_name(caller_project_id, purpose, vm_id)
        now = datetime.now(timezone.utc)
        lease_until = now + timedelta(minutes=ttl_minutes)
        bootstrap = bootstrap or {"packages": [], "commands": [], "ssh_public_keys": []}

        await self._db.upsert_project(caller_project_id, "unknown")

        base_path = await self._image_manager.ensure_image(template)
        vm_dir = self._storage.vm_dir(caller_project_id, vm_id)
        overlay_path = await self._storage.create_overlay(caller_project_id, vm_id, base_path, disk_gb)

        key_material: Optional[SSHKeyMaterial] = None
        ssh_keys = list(bootstrap["ssh_public_keys"])
        if ssh_access:
            key_material = await self._ssh_keys.ensure_keypair(vm_dir, vm_id)
            ssh_keys.append(key_material.public_key)

        ssh_pubkey = self._cfg.boxer_ssh_pubkey
        cloud_init_iso = await self._cloud_init.build(
            vm_id=vm_id,
            dest_dir=vm_dir,
            hostname=purpose[:20],
            ssh_pubkey=ssh_pubkey,
            ssh_authorized_keys=ssh_keys,
            packages=bootstrap["packages"],
            runcmd=bootstrap["commands"],
        )

        net_name = self._network.ensure_project_network(caller_project_id)
        serial_log = vm_dir / "serial.log"

        loop = asyncio.get_running_loop()
        await loop.run_in_executor(
            None,
            lambda: self._vm_ops.define_and_start(
                libvirt_name=libvirt_name,
                vm_id=vm_id,
                cpu=cpu,
                ram_mb=ram_mb,
                disk_path=overlay_path,
                cloud_init_iso=cloud_init_iso,
                net_name=net_name,
                headless=headless,
                serial_log=serial_log,
            ),
        )

        vm = VMRecord(
            id=vm_id,
            libvirt_name=libvirt_name,
            project_id=caller_project_id,
            display_name=purpose,
            state="running",
            owner_user=caller_user,
            template=template,
            cpu=cpu,
            ram_mb=ram_mb,
            disk_gb=disk_gb,
            headless=headless,
            created_at=now,
            last_touched=now,
            lease_until=lease_until,
            tags=tags,
        )
        await self._db.insert_vm(vm)

        if wait_for_ip_seconds > 0:
            ip = await self._vm_ops.get_ip_via_guest_agent(
                libvirt_name,
                timeout=max(1, min(wait_for_ip_seconds, 300)),
            )
            if ip:
                vm.ip_address = ip
                await self._db.update_vm_ip(vm_id, ip)
                logger.info("VM %s got IP %s", vm_id, ip)
            else:
                asyncio.create_task(self._poll_ip(vm_id, libvirt_name))
        else:
            asyncio.create_task(self._poll_ip(vm_id, libvirt_name))

        await self._db.add_event(
            "INFO",
            f"VM '{purpose}' ({vm_id}) created by {caller_user}",
            project_id=caller_project_id,
            vm_id=vm_id,
        )
        ssh_access_info = (
            self._ssh_access_payload(vm, key_material, include_private_key)
            if key_material
            else None
        )
        return vm, ssh_access_info

    async def _poll_ip(self, vm_id: str, libvirt_name: str) -> None:
        ip = await self._vm_ops.get_ip_via_guest_agent(libvirt_name, timeout=120)
        if ip:
            await self._db.update_vm_ip(vm_id, ip)
            logger.info("VM %s got IP %s", vm_id, ip)

    @staticmethod
    def _ssh_access_payload(
        vm: VMRecord,
        key_material: SSHKeyMaterial,
        include_private_key: bool,
    ) -> dict[str, Any]:
        host = vm.ip_address or "<pending-ip>"
        payload: dict[str, Any] = {
            "vm_id": vm.id,
            "username": "boxer",
            "host": vm.ip_address,
            "public_key": key_material.public_key,
            "private_key_path": str(key_material.private_key_path),
            "ssh_command": (
                f"ssh -i {key_material.private_key_path} "
                "-o IdentitiesOnly=yes "
                "-o StrictHostKeyChecking=accept-new "
                f"boxer@{host}"
            ),
        }
        if include_private_key:
            payload["private_key"] = key_material.private_key
        return payload

    # ------------------------------------------------------------------ vm.list

    async def _h_vm_list(self, params: dict[str, Any]) -> list[dict[str, Any]]:
        caller = caller_from_params(params)
        include_stale = bool(params.get("include_stale", False))
        vms = await self._db.list_vms(None if caller.is_admin else caller.project_id)
        result = []
        for vm in vms:
            if not self._policy.check_vm_list_visibility(caller, vm):
                continue
            d = _vm_to_dict(vm)
            state = self._vm_ops.get_state(vm.libvirt_name)
            d["live_state"] = state
            d["is_stale"] = vm.lease_until < datetime.now(timezone.utc)
            if not include_stale and d["is_stale"] and state == "stopped":
                continue
            result.append(d)
        return result

    # ------------------------------------------------------------------ vm.get

    async def _h_vm_get(self, params: dict[str, Any]) -> dict[str, Any]:
        caller = caller_from_params(params)
        vm_id = params.get("vm_id")
        if not vm_id:
            raise IPCError(ERR_INVALID_PARAMS, "vm_id required")
        vm = await self._db.get_vm(vm_id)
        self._policy.check("vm.get", caller, vm)
        await self._db.touch_vm(vm_id)
        d = _vm_to_dict(vm)
        d["live_state"] = self._vm_ops.get_state(vm.libvirt_name)
        return d

    # ------------------------------------------------------------------ vm.start

    async def _h_vm_start(self, params: dict[str, Any]) -> dict[str, Any]:
        caller = caller_from_params(params)
        vm_id = params.get("vm_id")
        wait_for_ip_seconds = int(params.get("wait_for_ip_seconds", 0) or 0)
        if not vm_id:
            raise IPCError(ERR_INVALID_PARAMS, "vm_id required")
        vm = await self._db.get_vm(vm_id)
        self._policy.check("vm.start", caller, vm)

        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, self._vm_ops.start, vm.libvirt_name)
        await self._db.update_vm_state(vm_id, "running")
        vm.state = "running"
        if wait_for_ip_seconds > 0:
            ip = await self._vm_ops.get_ip_via_guest_agent(
                vm.libvirt_name,
                timeout=max(1, min(wait_for_ip_seconds, 300)),
            )
            if ip:
                vm.ip_address = ip
                await self._db.update_vm_ip(vm_id, ip)
            else:
                asyncio.create_task(self._poll_ip(vm_id, vm.libvirt_name))
        else:
            asyncio.create_task(self._poll_ip(vm_id, vm.libvirt_name))
        return {"vm_id": vm_id, "state": "running", "ip_address": vm.ip_address}

    # ------------------------------------------------------------------ vm.stop

    async def _h_vm_stop(self, params: dict[str, Any]) -> dict[str, Any]:
        caller = caller_from_params(params)
        vm_id = params.get("vm_id")
        graceful = bool(params.get("graceful", True))
        if not vm_id:
            raise IPCError(ERR_INVALID_PARAMS, "vm_id required")
        vm = await self._db.get_vm(vm_id)
        self._policy.check("vm.stop", caller, vm)

        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, self._vm_ops.stop, vm.libvirt_name, graceful)
        await self._db.update_vm_state(vm_id, "stopped")
        return {"vm_id": vm_id, "state": "stopped"}

    # ------------------------------------------------------------------ vm.restart

    async def _h_vm_restart(self, params: dict[str, Any]) -> dict[str, Any]:
        caller = caller_from_params(params)
        vm_id = params.get("vm_id")
        graceful = bool(params.get("graceful", True))
        wait_for_ip_seconds = int(params.get("wait_for_ip_seconds", 0) or 0)
        if not vm_id:
            raise IPCError(ERR_INVALID_PARAMS, "vm_id required")
        vm = await self._db.get_vm(vm_id)
        self._policy.check("vm.restart", caller, vm)

        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, self._vm_ops.stop, vm.libvirt_name, graceful)
        await loop.run_in_executor(None, self._vm_ops.start, vm.libvirt_name)
        await self._db.update_vm_state(vm_id, "running")
        vm.state = "running"

        if wait_for_ip_seconds > 0:
            ip = await self._vm_ops.get_ip_via_guest_agent(
                vm.libvirt_name,
                timeout=max(1, min(wait_for_ip_seconds, 300)),
            )
            if ip:
                vm.ip_address = ip
                await self._db.update_vm_ip(vm_id, ip)
            else:
                asyncio.create_task(self._poll_ip(vm_id, vm.libvirt_name))
        else:
            asyncio.create_task(self._poll_ip(vm_id, vm.libvirt_name))

        return {"vm_id": vm_id, "state": "running", "ip_address": vm.ip_address}

    # ------------------------------------------------------------------ vm.ssh_access

    async def _h_vm_ssh_access(self, params: dict[str, Any]) -> dict[str, Any]:
        caller = caller_from_params(params)
        vm_id = params.get("vm_id")
        include_private_key = bool(params.get("include_private_key", False))
        create = bool(params.get("create", True))
        timeout = int(params.get("timeout_seconds", 30) or 30)
        if not vm_id:
            raise IPCError(ERR_INVALID_PARAMS, "vm_id required")
        vm = await self._db.get_vm(vm_id)
        self._policy.check("vm.ssh_access", caller, vm)

        vm_dir = self._storage.vm_dir(vm.project_id, vm_id)
        key_exists = self._ssh_keys.has_key(vm_dir)
        if not key_exists and not create:
            raise IPCError(
                ERR_NOT_FOUND,
                "No ephemeral SSH key exists for this VM. Re-run with create=true.",
            )
        if not key_exists and not vm.ip_address:
            raise IPCError(ERR_INVALID_PARAMS, "VM has no IP address yet; wait for boot before creating SSH access")

        key_material = await self._ssh_keys.ensure_keypair(vm_dir, vm_id)
        if not key_exists:
            try:
                await self._guest.install_ssh_public_key(
                    vm.ip_address,
                    key_material.public_key,
                    timeout_seconds=timeout,
                )
            except Exception:
                key_material.private_key_path.unlink(missing_ok=True)
                key_material.public_key_path.unlink(missing_ok=True)
                raise
            await self._db.add_event(
                "INFO",
                f"Ephemeral SSH access created for '{vm.display_name}' ({vm_id})",
                project_id=vm.project_id,
                vm_id=vm_id,
            )

        await self._db.touch_vm(vm_id)
        return self._ssh_access_payload(vm, key_material, include_private_key)

    # ------------------------------------------------------------------ vm.delete

    async def _h_vm_delete(self, params: dict[str, Any]) -> dict[str, Any]:
        caller = caller_from_params(params)
        vm_id = params.get("vm_id")
        if not vm_id:
            raise IPCError(ERR_INVALID_PARAMS, "vm_id is required for delete (name alone is not accepted)")
        vm = await self._db.get_vm(vm_id)
        self._policy.check("vm.delete", caller, vm)

        await self._db.update_vm_state(vm_id, "deleting")
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, self._vm_ops.undefine, vm.libvirt_name)
        if vm.origin == "boxer":
            self._storage.delete_vm_storage(vm.project_id, vm_id)
        await self._db.delete_vm(vm_id)

        if not self._network.has_active_vms(vm.project_id):
            vms_left = await self._db.list_vms(vm.project_id)
            if not vms_left:
                self._network.teardown_project_network(vm.project_id)

        await self._db.add_event(
            "INFO", f"VM '{vm.display_name}' ({vm_id}) deleted by {caller.user}",
            project_id=vm.project_id, vm_id=vm_id,
        )
        return {"vm_id": vm_id, "deleted": True}

    # ------------------------------------------------------------------ vm.extend_lease

    async def _h_vm_extend_lease(self, params: dict[str, Any]) -> dict[str, Any]:
        caller = caller_from_params(params)
        vm_id = params.get("vm_id")
        ttl_minutes = int(params.get("ttl_minutes", self._cfg.default_ttl_minutes))
        if not vm_id:
            raise IPCError(ERR_INVALID_PARAMS, "vm_id required")
        vm = await self._db.get_vm(vm_id)
        self._policy.check("vm.extend_lease", caller, vm)

        now = datetime.now(timezone.utc)
        new_lease = max(vm.lease_until, now) + timedelta(minutes=ttl_minutes)
        await self._db.update_vm_lease(vm_id, new_lease)
        return {"vm_id": vm_id, "lease_until": new_lease.isoformat()}

    # ------------------------------------------------------------------ vm.snapshot

    async def _h_vm_snapshot(self, params: dict[str, Any]) -> dict[str, Any]:
        caller = caller_from_params(params)
        vm_id = params.get("vm_id")
        label = params.get("label", "snap")
        if not vm_id:
            raise IPCError(ERR_INVALID_PARAMS, "vm_id required")
        vm = await self._db.get_vm(vm_id)
        self._policy.check("vm.snapshot", caller, vm)

        loop = asyncio.get_running_loop()
        snap_name = await loop.run_in_executor(
            None, self._vm_ops.snapshot, vm.libvirt_name, label
        )
        return {"vm_id": vm_id, "snapshot_name": snap_name}

    # ------------------------------------------------------------------ vm.exec

    async def _h_vm_exec(self, params: dict[str, Any]) -> dict[str, Any]:
        caller = caller_from_params(params)
        vm_id = params.get("vm_id")
        command = params.get("command")
        timeout = int(params.get("timeout_seconds", 30))
        if not vm_id or not command:
            raise IPCError(ERR_INVALID_PARAMS, "vm_id and command required")
        vm = await self._db.get_vm(vm_id)
        self._policy.check("vm.exec", caller, vm)
        if not vm.ip_address:
            raise IPCError(ERR_INVALID_PARAMS, "VM has no IP address yet; wait for boot to complete")

        result = await self._guest.exec_ssh(vm.ip_address, command, timeout)
        await self._db.touch_vm(vm_id)
        return result

    # ------------------------------------------------------------------ vm.screenshot

    async def _h_vm_screenshot(self, params: dict[str, Any]) -> dict[str, Any]:
        caller = caller_from_params(params)
        vm_id = params.get("vm_id")
        if not vm_id:
            raise IPCError(ERR_INVALID_PARAMS, "vm_id required")
        vm = await self._db.get_vm(vm_id)
        self._policy.check("vm.screenshot", caller, vm)

        png_b64 = await self._guest.screenshot(vm.libvirt_name)
        await self._db.touch_vm(vm_id)
        return {"vm_id": vm_id, "format": "png", "data": png_b64}

    # ------------------------------------------------------------------ vm.input

    async def _h_vm_input(self, params: dict[str, Any]) -> dict[str, Any]:
        caller = caller_from_params(params)
        vm_id = params.get("vm_id")
        actions = params.get("actions", [])
        if not vm_id:
            raise IPCError(ERR_INVALID_PARAMS, "vm_id required")
        vm = await self._db.get_vm(vm_id)
        self._policy.check("vm.input", caller, vm)

        await self._guest.send_input(vm.libvirt_name, actions)
        await self._db.touch_vm(vm_id)
        return {"vm_id": vm_id, "actions_sent": len(actions)}

    # ------------------------------------------------------------------ resource.status

    async def _h_resource_status(self, params: dict[str, Any]) -> dict[str, Any]:
        return self._capacity.get_status()

    # ------------------------------------------------------------------ queue.status

    async def _h_queue_status(self, params: dict[str, Any]) -> dict[str, Any]:
        caller = caller_from_params(params)
        entries = await self._db.list_pending_queue()
        filtered = [e for e in entries if caller.is_admin or e.project_id == caller.project_id]
        return {
            "pending": len(filtered),
            "entries": [
                {
                    "queue_id": e.id,
                    "project_id": e.project_id,
                    "status": e.status,
                    "reason": e.reason,
                    "created_at": e.created_at.isoformat(),
                }
                for e in filtered
            ],
        }

    # ------------------------------------------------------------------ cleanup.plan

    async def _h_cleanup_plan(self, params: dict[str, Any]) -> dict[str, Any]:
        caller = caller_from_params(params)
        now = datetime.now(timezone.utc)
        vms = await self._db.list_vms(None if caller.is_admin else caller.project_id)
        stale = [v for v in vms if v.lease_until < now]
        return {
            "stale_count": len(stale),
            "stale_vms": [
                {
                    "vm_id": v.id,
                    "display_name": v.display_name,
                    "state": v.state,
                    "lease_expired": v.lease_until.isoformat(),
                    "last_touched": v.last_touched.isoformat(),
                }
                for v in stale
            ],
        }

    # ------------------------------------------------------------------ event.poll

    async def _h_event_poll(self, params: dict[str, Any]) -> list[dict[str, Any]]:
        since_str = params.get("since")
        since = datetime.fromisoformat(since_str) if since_str else None
        level = params.get("level")
        limit = int(params.get("limit", 50))

        events = await self._db.list_events(since=since, level=level, limit=limit)
        return [
            {
                "event_id": e.id,
                "project_id": e.project_id,
                "vm_id": e.vm_id,
                "level": e.level,
                "message": e.message,
                "created_at": e.created_at.isoformat(),
            }
            for e in events
        ]


    # ------------------------------------------------------------------ vm.scan

    async def _h_vm_scan(self, params: dict[str, Any]) -> dict[str, Any]:
        report = await build_reconcile_report(self._conn, self._db)
        return {
            "orphaned_boxer": [
                {
                    "libvirt_name": o.libvirt_name,
                    "parsed_project_id": o.parsed_project_id,
                    "parsed_display_name": o.parsed_display_name,
                    "parsed_vm_id": o.parsed_vm_id,
                    "disk_path": o.disk_path,
                    "disk_gb": o.disk_gb,
                    "is_active": o.is_active,
                    "suggested_action": o.suggested_action,
                }
                for o in report.orphaned_boxer
            ],
            "foreign_vms": [
                {
                    "libvirt_name": f.libvirt_name,
                    "disk_path": f.disk_path,
                    "disk_gb": f.disk_gb,
                    "is_active": f.is_active,
                    "suggested_action": f.suggested_action,
                }
                for f in report.foreign_vms
            ],
            "ghost_records": [
                {
                    "vm_id": g.vm_id,
                    "libvirt_name": g.libvirt_name,
                    "project_id": g.project_id,
                    "display_name": g.display_name,
                    "suggested_action": g.suggested_action,
                }
                for g in report.ghost_records
            ],
        }

    # ------------------------------------------------------------------ vm.import

    async def _h_vm_import(self, params: dict[str, Any]) -> dict[str, Any]:
        caller = caller_from_params(params)
        libvirt_name = params.get("libvirt_name")
        if not libvirt_name:
            raise IPCError(ERR_INVALID_PARAMS, "libvirt_name required")

        display_name = params.get("display_name") or libvirt_name
        ttl_minutes = int(params.get("ttl_minutes", self._cfg.default_ttl_minutes))
        project_id = params.get("project_id") or caller.project_id

        loop = asyncio.get_running_loop()
        try:
            xml = await loop.run_in_executor(
                None, lambda: self._conn.lookupByName(libvirt_name).XMLDesc(0)
            )
        except libvirt.libvirtError:
            raise IPCError(ERR_NOT_FOUND, f"Domain '{libvirt_name}' not found in libvirt")

        existing = await self._db.get_vm_by_libvirt_name(libvirt_name)
        if existing is not None:
            raise IPCError(
                ERR_INVALID_PARAMS,
                f"Domain '{libvirt_name}' is already tracked as vm_id={existing.id}",
            )

        disk_path = _extract_disk_path(xml)
        disk_gb = 0
        if disk_path:
            disk_gb = await loop.run_in_executor(None, _get_disk_size_gb, disk_path)

        vm_id = _make_vm_id()
        now = datetime.now(timezone.utc)
        vm = VMRecord(
            id=vm_id,
            libvirt_name=libvirt_name,
            project_id=project_id,
            display_name=display_name,
            state="running",
            owner_user=caller.user,
            template="imported",
            cpu=0,
            ram_mb=0,
            disk_gb=disk_gb,
            headless=True,
            created_at=now,
            last_touched=now,
            lease_until=now + timedelta(minutes=ttl_minutes),
            origin="imported",
        )
        await self._db.upsert_project(project_id, "unknown")
        await self._db.insert_vm(vm)
        await self._db.add_event(
            "INFO",
            f"Imported foreign VM '{libvirt_name}' as '{display_name}' ({vm_id})",
            project_id=project_id,
            vm_id=vm_id,
        )
        return _vm_to_dict(vm)

    # ------------------------------------------------------------------ vm.adopt

    async def _h_vm_adopt(self, params: dict[str, Any]) -> dict[str, Any]:
        caller = caller_from_params(params)
        libvirt_name = params.get("libvirt_name")
        if not libvirt_name:
            raise IPCError(ERR_INVALID_PARAMS, "libvirt_name required")

        parsed = parse_boxer_name(libvirt_name)
        if parsed is None:
            raise IPCError(
                ERR_INVALID_PARAMS,
                f"'{libvirt_name}' does not match Boxer--<project>--<name>--<vm_id> pattern",
            )
        project_id, display_name, vm_id = parsed

        loop = asyncio.get_running_loop()
        try:
            xml = await loop.run_in_executor(
                None, lambda: self._conn.lookupByName(libvirt_name).XMLDesc(0)
            )
        except libvirt.libvirtError:
            raise IPCError(ERR_NOT_FOUND, f"Domain '{libvirt_name}' not found in libvirt")

        existing = await self._db.get_vm(vm_id)
        if existing is not None:
            raise IPCError(
                ERR_INVALID_PARAMS,
                f"vm_id '{vm_id}' is already tracked in DB",
            )

        disk_path = _extract_disk_path(xml)
        disk_gb = 0
        if disk_path:
            disk_gb = await loop.run_in_executor(None, _get_disk_size_gb, disk_path)

        now = datetime.now(timezone.utc)
        vm = VMRecord(
            id=vm_id,
            libvirt_name=libvirt_name,
            project_id=project_id,
            display_name=display_name,
            state="running",
            owner_user=caller.user,
            template="adopted",
            cpu=0,
            ram_mb=0,
            disk_gb=disk_gb,
            headless=True,
            created_at=now,
            last_touched=now,
            lease_until=now + timedelta(minutes=self._cfg.default_ttl_minutes),
            origin="adopted",
        )
        await self._db.upsert_project(project_id, "unknown")
        await self._db.insert_vm(vm)
        await self._db.add_event(
            "INFO",
            f"Adopted orphaned Boxer domain '{libvirt_name}' (vm_id={vm_id})",
            project_id=project_id,
            vm_id=vm_id,
        )
        return _vm_to_dict(vm)

    # ------------------------------------------------------------------ vm.purge_ghost

    async def _h_vm_purge_ghost(self, params: dict[str, Any]) -> dict[str, Any]:
        caller = caller_from_params(params)
        vm_id = params.get("vm_id")
        if not vm_id:
            raise IPCError(ERR_INVALID_PARAMS, "vm_id required")

        vm = await self._db.get_vm(vm_id)
        if vm is None:
            raise IPCError(ERR_NOT_FOUND, f"vm_id '{vm_id}' not found in DB")

        self._policy.check("vm.purge_ghost", caller, vm)

        loop = asyncio.get_running_loop()
        def _domain_exists(name: str) -> bool:
            try:
                self._conn.lookupByName(name)
                return True
            except libvirt.libvirtError:
                return False

        still_alive = await loop.run_in_executor(None, _domain_exists, vm.libvirt_name)
        if still_alive:
            raise IPCError(
                ERR_INVALID_PARAMS,
                f"Domain '{vm.libvirt_name}' still exists in libvirt — use vm.delete instead",
            )

        await self._db.purge_ghost_record(vm_id)
        await self._db.add_event(
            "INFO",
            f"Purged ghost DB record for '{vm.display_name}' ({vm_id})",
            project_id=vm.project_id,
            vm_id=vm_id,
        )
        return {"vm_id": vm_id, "purged": True}


def _vm_to_dict(vm: VMRecord) -> dict[str, Any]:
    return {
        "vm_id": vm.id,
        "libvirt_name": vm.libvirt_name,
        "display_name": vm.display_name,
        "project_id": vm.project_id,
        "state": vm.state,
        "owner_user": vm.owner_user,
        "template": vm.template,
        "cpu": vm.cpu,
        "ram_mb": vm.ram_mb,
        "disk_gb": vm.disk_gb,
        "headless": vm.headless,
        "ip_address": vm.ip_address,
        "created_at": vm.created_at.isoformat(),
        "last_touched": vm.last_touched.isoformat(),
        "lease_until": vm.lease_until.isoformat(),
        "tags": vm.tags,
        "origin": vm.origin,
    }
