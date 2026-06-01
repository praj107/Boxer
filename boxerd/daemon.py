"""Boxerd main daemon: wires all components and registers IPC handlers."""
from __future__ import annotations

import asyncio
import logging
import re
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

import libvirt

from boxer.config import BoxerConfig, get_config
from boxer.ipc import ERR_INVALID_PARAMS, ERR_NOT_FOUND, ERR_POLICY_VIOLATION, IPCError
from boxer.types import VMRecord
from boxerd.cloud_init import CloudInitBuilder, WriteFile
from boxerd.db import Database
from boxerd.guest_agent import GuestAgent
from boxerd.image_catalog import ImageManager
from boxerd.installer_seed import SUPPORTED_METHODS, InstallerSeedBuilder, SeedOptions
from boxerd.ipc_server import IPCServer
from boxerd.lease_manager import LeaseManager
from boxerd.network_ops import NetworkManager
from boxerd.policy import PolicyEngine, caller_from_params
from boxerd.profiles import MAX_PROFILES, available_profiles, resolve_profiles
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

    # Named profiles expand into extra packages + setup commands (bounded allowlist).
    profile_names = _string_list(
        params.get("profiles", bootstrap.get("profiles")), "profiles", max_items=MAX_PROFILES, max_len=32
    )
    resolved = resolve_profiles(profile_names)
    packages = _dedupe_keep_order([*resolved["packages"], *packages])
    commands = _dedupe_keep_order([*resolved["runcmd"], *commands])
    if len(packages) > 80:
        raise IPCError(ERR_INVALID_PARAMS, "Too many packages after profile expansion (max 80)")

    write_files, secret_count = _parse_file_injections(params, bootstrap)
    if secret_count:
        # Best-effort: drop cloud-init's persisted copy of user-data after first
        # boot so injected secret material is not readable from the instance cache.
        commands.append(
            "find /var/lib/cloud/instances -maxdepth 2 -name user-data.txt -delete 2>/dev/null || true"
        )

    return {
        "packages": packages,
        "commands": commands,
        "ssh_public_keys": ssh_public_keys,
        "write_files": write_files,
        "secret_count": secret_count,
        "profiles": profile_names,
    }


def _validate_local_iso_path(path_str: str, local_iso_dir) -> "Path":
    """Validate a caller-supplied local ISO path and return its resolved real path.

    When local_iso_dir is set, the path must resolve under that directory (prevents
    an agent from booting files outside the designated staging area on shared hosts).
    When local_iso_dir is None (the default), any accessible ISO path is permitted —
    appropriate for a single-developer workstation where all IPC callers are trusted.
    """
    if not path_str.startswith("/"):
        raise IPCError(ERR_INVALID_PARAMS, "iso_path must be an absolute path")
    path = Path(path_str)
    if not path.exists():
        raise IPCError(ERR_INVALID_PARAMS, f"iso_path does not exist: {path}")
    if not path.is_file():
        raise IPCError(ERR_INVALID_PARAMS, f"iso_path is not a regular file: {path}")
    real = path.resolve()
    if local_iso_dir is not None:
        real_dir = local_iso_dir.resolve()
        try:
            real.relative_to(real_dir)
        except ValueError:
            raise IPCError(
                ERR_INVALID_PARAMS,
                f"iso_path must be under local_iso_dir ({local_iso_dir}). "
                f"Resolved path: {real}",
            )
    return real


def _caller_project_path(params: dict[str, Any], project_id: str) -> str:
    path = params.get("caller_project_path")
    if isinstance(path, str) and path.strip():
        return str(Path(path).expanduser().resolve())
    return f"unknown:{project_id}"


def _dedupe_keep_order(items: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for item in items:
        if item not in seen:
            seen.add(item)
            out.append(item)
    return out


_WAIT_CONDITIONS = {"guest_agent", "ip", "ssh", "cloud_init", "package_install"}


def _parse_wait_conditions(value: Any) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise IPCError(ERR_INVALID_PARAMS, "wait_for must be a list of condition names")
    conditions: list[str] = []
    for item in value:
        if item not in _WAIT_CONDITIONS:
            raise IPCError(
                ERR_INVALID_PARAMS,
                f"Unknown wait condition '{item}'. Valid: {sorted(_WAIT_CONDITIONS)}",
            )
        if item not in conditions:
            conditions.append(item)
    return conditions


_PERMISSIONS_RE = re.compile(r"^0[0-7]{3}$")
_MAX_FILE_BYTES = 256 * 1024
_MAX_TOTAL_FILE_BYTES = 1024 * 1024


def _parse_file_injections(
    params: dict[str, Any], bootstrap: dict[str, Any]
) -> tuple[list[WriteFile], int]:
    """Parse write_files (regular) and secret_files (mode 0600, never logged)."""
    regular = _coerce_file_list(
        params.get("write_files", bootstrap.get("write_files")),
        "write_files",
        default_permissions="0644",
        default_owner="root:root",
    )
    secrets = _coerce_file_list(
        params.get("secret_files", bootstrap.get("secret_files")),
        "secret_files",
        default_permissions="0600",
        default_owner="boxer:boxer",
    )
    files = [*regular, *secrets]
    if len(files) > 20:
        raise IPCError(ERR_INVALID_PARAMS, "At most 20 injected files are allowed")
    total = sum(len(f.content.encode("utf-8")) for f in files)
    if total > _MAX_TOTAL_FILE_BYTES:
        raise IPCError(ERR_INVALID_PARAMS, "Injected files exceed the 1 MiB total limit")
    paths = [f.path for f in files]
    if len(set(paths)) != len(paths):
        raise IPCError(ERR_INVALID_PARAMS, "Duplicate injected file paths")
    return files, len(secrets)


def _coerce_file_list(
    value: Any, field: str, *, default_permissions: str, default_owner: str
) -> list[WriteFile]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise IPCError(ERR_INVALID_PARAMS, f"{field} must be a list of objects")
    result: list[WriteFile] = []
    for item in value:
        if not isinstance(item, dict):
            raise IPCError(ERR_INVALID_PARAMS, f"{field} entries must be objects with path and content")
        path = item.get("path")
        content = item.get("content")
        if not isinstance(path, str) or not path.startswith("/") or len(path) > 256:
            raise IPCError(ERR_INVALID_PARAMS, f"{field}: path must be an absolute path <= 256 chars")
        if not isinstance(content, str):
            raise IPCError(ERR_INVALID_PARAMS, f"{field}: content must be a string")
        if len(content.encode("utf-8")) > _MAX_FILE_BYTES:
            raise IPCError(ERR_INVALID_PARAMS, f"{field}: '{path}' exceeds the 256 KiB per-file limit")
        permissions = item.get("permissions", default_permissions)
        if not isinstance(permissions, str) or not _PERMISSIONS_RE.match(permissions):
            raise IPCError(ERR_INVALID_PARAMS, f"{field}: permissions must be an octal mode like 0644")
        owner = item.get("owner", default_owner)
        if not isinstance(owner, str) or len(owner) > 64:
            raise IPCError(ERR_INVALID_PARAMS, f"{field}: owner must be a string like 'user:group'")
        result.append(WriteFile(path=path, content=content, permissions=permissions, owner=owner))
    return result


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
        self._installer_seed = InstallerSeedBuilder()
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
        reg("vm.request_installer", self._h_vm_request_installer)
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
        reg("vm.serial_log", self._h_vm_serial_log)
        reg("image.preflight", self._h_image_preflight)
        reg("profile.list", self._h_profile_list)
        reg("image.list", self._h_image_list)
        reg("image.prewarm", self._h_image_prewarm)
        reg("image.refresh", self._h_image_refresh)
        reg("image.prune", self._h_image_prune)
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
        caller_project_path = _caller_project_path(params, caller.project_id)
        template = params.get("template", "ubuntu-24.04")
        purpose = params.get("purpose", "vm")
        headless = bool(params.get("headless", True))
        ttl_minutes = int(params.get("ttl_minutes", self._cfg.default_ttl_minutes))
        tags = params.get("tags") or {}
        bootstrap = _bootstrap_from_params(params)
        ssh_access = bool(params.get("ssh_access", False) or params.get("return_ssh_private_key", False))
        return_ssh_private_key = bool(params.get("return_ssh_private_key", False))
        wait_for_ip_seconds = int(params.get("wait_for_ip_seconds", 0) or 0)
        wait_for = _parse_wait_conditions(params.get("wait_for"))
        wait_command = params.get("wait_command")
        if wait_command is not None and not isinstance(wait_command, str):
            raise IPCError(ERR_INVALID_PARAMS, "wait_command must be a string")
        wait_timeout_seconds = max(0, min(int(params.get("wait_timeout_seconds", 0) or 0), 900))
        # Asking for a wait condition implies we must first obtain an IP.
        if (wait_for or wait_command) and wait_for_ip_seconds == 0:
            wait_for_ip_seconds = wait_timeout_seconds or 120

        catalog_entry = self._image_manager._catalog.get(template)
        if catalog_entry is None:
            # Give a targeted hint when the caller passes a local file path.
            if template.startswith("/") and template.lower().endswith(".iso"):
                raise IPCError(
                    ERR_INVALID_PARAMS,
                    f"'{template}' looks like a local ISO file path, not a catalog template. "
                    "To boot a custom ISO use box_request_installer with the iso_path parameter: "
                    f"box_request_installer(iso_path='{template}', purpose='<name>'). "
                    "box_request_vm only accepts cloud-image catalog names (e.g. ubuntu-24.04).",
                )
            raise IPCError(
                ERR_INVALID_PARAMS,
                f"Unknown template: {template}. "
                f"Available: {self._image_manager._catalog.list_names()}",
            )

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
            caller_project_path=caller_project_path,
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
        if wait_for or wait_command:
            result["wait_results"] = await self._wait_for_conditions(
                vm, wait_for, wait_command, wait_timeout_seconds or 300
            )
        return result

    async def _wait_for_conditions(
        self,
        vm: VMRecord,
        conditions: list[str],
        command: Optional[str],
        timeout_seconds: int,
    ) -> dict[str, str]:
        """Poll for optional readiness conditions, bounded by timeout_seconds.

        Returns a per-condition status: ready | timeout | error | skipped.
        Conditions needing in-guest access are skipped when no IP is available.
        """
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout_seconds
        report: dict[str, str] = {}

        def remaining() -> float:
            return max(1.0, deadline - loop.time())

        ip = vm.ip_address

        async def _poll_ssh(cmd: str) -> str:
            while loop.time() < deadline:
                try:
                    res = await self._guest.exec_ssh(ip, cmd, timeout_seconds=min(30, int(remaining())))
                    if res.get("exit_code", 1) == 0:
                        return "ready"
                except Exception:
                    pass
                await asyncio.sleep(5)
            return "timeout"

        for cond in conditions:
            if cond in ("guest_agent", "ip"):
                report[cond] = "ready" if ip else "timeout"
            elif cond == "ssh":
                report[cond] = "skipped" if not ip else await _poll_ssh("true")
            elif cond in ("cloud_init", "package_install"):
                report[cond] = (
                    "skipped"
                    if not ip
                    else await _poll_ssh("cloud-init status --wait >/dev/null 2>&1 || cloud-init status | grep -q done")
                )
            else:
                report[cond] = "error"

        if command:
            report["command"] = "skipped" if not ip else await _poll_ssh(command)
        return report

    async def _create_vm_from_request(self, request: dict[str, Any]) -> None:
        request = {**request, "return_ssh_private_key": False, "wait_install_seconds": 0}
        if request.get("job_type") == "installer":
            await self._h_vm_request_installer(request)
        else:
            await self._h_vm_request(request)

    # ------------------------------------------------------------------ vm.request_installer

    async def _h_vm_request_installer(self, params: dict[str, Any]) -> dict[str, Any]:
        caller = caller_from_params(params)
        caller_project_path = _caller_project_path(params, caller.project_id)
        template = params.get("template")
        iso_path_str = params.get("iso_path")
        wait_install_seconds = max(0, min(int(params.get("wait_install_seconds", 0) or 0), 1800))

        if not template and not iso_path_str:
            raise IPCError(ERR_INVALID_PARAMS, "Either template or iso_path is required for an ISO install")
        if template and iso_path_str:
            raise IPCError(ERR_INVALID_PARAMS, "template and iso_path are mutually exclusive")

        purpose = params.get("purpose", "install")
        ttl_minutes = int(params.get("ttl_minutes", max(self._cfg.default_ttl_minutes, 180)))
        tags = params.get("tags") or {}
        bootstrap = _bootstrap_from_params(params)

        if iso_path_str:
            # Local ISO path: validate and use directly, no catalog lookup.
            install_iso = _validate_local_iso_path(iso_path_str, self._cfg.local_iso_dir)
            method = str(params.get("install_method", "manual")).lower()
            if method not in SUPPORTED_METHODS:
                raise IPCError(
                    ERR_INVALID_PARAMS,
                    f"Unsupported install_method '{method}'. Supported: {sorted(SUPPORTED_METHODS)}",
                )
            # Local ISOs default headless=True so serial log is the primary interface.
            headless = bool(params.get("headless", True))
            display_template = f"local:{Path(iso_path_str).name}"
            cpu = int(params.get("cpu") or 2)
            ram_mb = int(params.get("ram_mb") or 2048)
            disk_gb = int(params.get("disk_gb") or 20)
            # manual method + local ISO = test-boot mode: no install expected, no watcher.
            test_boot = (method == "manual")
            stage_install_iso = True
        else:
            catalog_entry = self._image_manager._catalog.get(template)
            if catalog_entry is None:
                raise IPCError(ERR_INVALID_PARAMS, f"Unknown template: {template}")
            if str(catalog_entry.get("type") or catalog_entry.get("artifact_type")) != "iso":
                raise IPCError(
                    ERR_INVALID_PARAMS,
                    f"Template '{template}' is not an installer ISO. Use box_request_vm for cloud images.",
                )

            install_cfg = catalog_entry.get("install") or {}
            method = str(install_cfg.get("method", "manual")).lower()
            if method not in SUPPORTED_METHODS:
                raise IPCError(
                    ERR_INVALID_PARAMS,
                    f"Template '{template}' has unsupported install method '{method}'. "
                    f"Supported: {sorted(SUPPORTED_METHODS)}",
                )
            # Manual installs must be headed so the console is reachable over SPICE.
            headless = False if method == "manual" else bool(params.get("headless", False))
            display_template = template
            cpu = int(params.get("cpu") or catalog_entry.get("default_cpu", 2))
            ram_mb = int(params.get("ram_mb") or catalog_entry.get("default_ram_mb", 2048))
            disk_gb = int(params.get("disk_gb") or catalog_entry.get("default_disk_gb", 20))
            install_iso = await self._image_manager.ensure_iso(template)
            test_boot = False  # catalog ISOs always go through the install watcher
            stage_install_iso = False

        # Installer-specific admission: its own concurrency cap plus host caps.
        installing = await self._db.count_installing_vms()
        if installing >= self._cfg.max_concurrent_installs:
            reason = (
                f"Installer concurrency limit reached "
                f"({installing}/{self._cfg.max_concurrent_installs} installs running)"
            )
        else:
            reason = await self._scheduler.check_admission(caller.project_id, cpu, ram_mb, disk_gb)
        if reason:
            queue_id = await self._scheduler.enqueue_request(
                caller.project_id,
                {
                    **params,
                    "job_type": "installer",
                    "caller_project_id": caller.project_id,
                    "caller_user": caller.user,
                },
                reason,
            )
            return {
                "status": "queued",
                "queue_id": queue_id,
                "reason": reason,
                "retry_after_seconds": 120,
                "current_capacity": self._capacity.get_status(),
            }

        vm = await self._create_installer_vm(
            caller_project_id=caller.project_id,
            caller_project_path=caller_project_path,
            caller_user=caller.user,
            template=display_template,
            purpose=purpose,
            install_iso=install_iso,
            method=method,
            headless=headless,
            ttl_minutes=ttl_minutes,
            cpu=cpu,
            ram_mb=ram_mb,
            disk_gb=disk_gb,
            tags=tags,
            bootstrap=bootstrap,
            test_boot=test_boot,
            stage_install_iso=stage_install_iso,
        )
        result = _vm_to_dict(vm)
        result["install_method"] = method

        if test_boot:
            result["note"] = (
                "VM is booting from the local ISO. No install watcher — the VM runs "
                "until you stop or delete it. The blank target disk is exposed as NVMe. "
                "Use box_get_serial_log to read console output."
            )
        elif wait_install_seconds > 0:
            final_state = await self._wait_for_install(vm.id, wait_install_seconds)
            result["install_state"] = final_state
            if final_state == "installed":
                result["note"] = "Install completed. VM is now running from disk."
            elif final_state == "failed":
                result["note"] = "Install failed. Use box_get_serial_log to inspect the console output."
            else:
                result["note"] = (
                    f"Wait timeout ({wait_install_seconds}s) reached before install finished. "
                    "Poll box_get_vm for install_state or box_get_serial_log for progress."
                )
        else:
            result["note"] = (
                "Install in progress. Boxer will switch boot order to disk and start the VM "
                "once the installer powers off. Poll box_get_vm for install_state, or "
                "use box_get_serial_log to monitor console output."
            )
        return result

    async def _create_installer_vm(
        self,
        *,
        caller_project_id: str,
        caller_project_path: str,
        caller_user: str,
        template: str,
        purpose: str,
        install_iso: Path,
        method: str,
        headless: bool,
        ttl_minutes: int,
        cpu: int,
        ram_mb: int,
        disk_gb: int,
        tags: dict[str, str],
        bootstrap: dict[str, list[str]],
        test_boot: bool = False,
        stage_install_iso: bool = False,
    ) -> VMRecord:
        """Provision an installer or test-boot VM from an ISO.

        test_boot=True (local ISO + manual method): VM boots the ISO and runs
        indefinitely; no install watcher, on_reboot=restart, install_state=None.
        Appropriate for OS development test cycles where serial output is the
        acceptance criterion, not a successful install to disk.
        """
        vm_id = _make_vm_id()
        libvirt_name = _make_libvirt_name(caller_project_id, purpose, vm_id)
        now = datetime.now(timezone.utc)
        lease_until = now + timedelta(minutes=ttl_minutes)

        await self._db.upsert_project(caller_project_id, caller_project_path)

        vm_dir = self._storage.vm_dir(caller_project_id, vm_id)
        disk_format = "raw" if test_boot else "qcow2"
        disk_path = await self._storage.create_blank_disk(
            caller_project_id,
            vm_id,
            disk_gb,
            disk_format=disk_format,
        )
        if stage_install_iso:
            install_iso = await self._storage.stage_iso(caller_project_id, vm_id, install_iso)
        else:
            self._storage.prepare_readonly_file(install_iso)

        ssh_keys = list(bootstrap["ssh_public_keys"])
        if self._cfg.boxer_ssh_pubkey:
            ssh_keys.insert(0, self._cfg.boxer_ssh_pubkey)
        seed_iso = await self._installer_seed.build(
            SeedOptions(
                method=method,
                hostname=purpose[:20] or "boxer",
                ssh_authorized_keys=ssh_keys,
                packages=bootstrap["packages"],
            ),
            vm_dir,
        )
        self._storage.prepare_readonly_file(seed_iso)

        net_name = self._network.ensure_project_network(caller_project_id)
        serial_log = vm_dir / "serial.log"
        self._storage.prepare_mutable_file(serial_log, create=True)

        # test_boot: allow reboots (on_reboot=restart) instead of destroying the domain.
        # Installer flows use on_reboot=destroy so the end-of-install reboot is the
        # completion signal; test boots should survive reboots for iterative development.
        reboot_policy = "restart" if test_boot else "destroy"

        loop = asyncio.get_running_loop()
        try:
            await loop.run_in_executor(
                None,
                lambda: self._vm_ops.define_and_start_installer(
                    libvirt_name=libvirt_name,
                    vm_id=vm_id,
                    cpu=cpu,
                    ram_mb=ram_mb,
                    disk_path=disk_path,
                    install_iso=install_iso,
                    seed_iso=seed_iso,
                    net_name=net_name,
                    headless=headless,
                    serial_log=serial_log,
                    reboot_policy=reboot_policy,
                    disk_format=disk_format,
                    emulate_nvme=test_boot,
                ),
            )
        except Exception:
            self._storage.delete_vm_storage(caller_project_id, vm_id)
            raise

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
            artifact_type="iso",
            install_state=None if test_boot else "installing",
        )
        await self._db.insert_vm(vm)
        await self._db.add_event(
            "INFO",
            f"{'Test boot' if test_boot else 'ISO install'} '{purpose}' ({vm_id}) "
            f"started from {template} (method={method})",
            project_id=caller_project_id,
            vm_id=vm_id,
        )
        if not test_boot:
            asyncio.create_task(self._watch_install(vm_id, libvirt_name))
        return vm

    async def _watch_install(
        self, vm_id: str, libvirt_name: str, timeout_seconds: int = 7200
    ) -> None:
        """Wait for the installer to power off, then boot the installed disk.

        The installer domain is defined with ``on_reboot=destroy``, so a normal
        end-of-install reboot transitions the domain to ``stopped``. That edge is
        the completion signal: we flip boot order to disk and start the VM.
        """
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout_seconds
        # Let the domain come up before we start watching for the power-off edge.
        await asyncio.sleep(15)
        try:
            while loop.time() < deadline:
                state = self._vm_ops.get_state(libvirt_name)
                if state == "stopped":
                    break
                await asyncio.sleep(15)
            else:
                await self._db.update_vm_install_state(vm_id, "failed")
                await self._db.add_event(
                    "WARN",
                    f"ISO install for {vm_id} did not finish within {timeout_seconds}s",
                    vm_id=vm_id,
                )
                return

            await loop.run_in_executor(None, self._vm_ops.switch_boot_to_disk, libvirt_name)
            await loop.run_in_executor(None, self._vm_ops.start, libvirt_name)
            await self._db.update_vm_install_state(vm_id, "installed")
            await self._db.update_vm_state(vm_id, "running")
            await self._db.add_event(
                "INFO",
                f"ISO install for {vm_id} completed; booted from disk",
                vm_id=vm_id,
            )
            asyncio.create_task(self._poll_ip(vm_id, libvirt_name))
        except Exception:
            logger.exception("Install watcher failed for %s", vm_id)
            await self._db.update_vm_install_state(vm_id, "failed")

    async def _wait_for_install(self, vm_id: str, timeout_seconds: int) -> str:
        """Poll DB until install_state reaches a terminal value or timeout_seconds elapses."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout_seconds
        while loop.time() < deadline:
            vm = await self._db.get_vm(vm_id)
            if vm and vm.install_state in ("installed", "failed"):
                return vm.install_state
            await asyncio.sleep(10)
        return "timeout"

    async def _create_vm(
        self,
        *,
        caller_project_id: str,
        caller_project_path: str,
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
        bootstrap = bootstrap or {"packages": [], "commands": [], "ssh_public_keys": [], "write_files": []}

        await self._db.upsert_project(caller_project_id, caller_project_path)

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
            write_files=bootstrap.get("write_files") or [],
        )
        self._storage.prepare_readonly_file(cloud_init_iso)

        net_name = self._network.ensure_project_network(caller_project_id)
        serial_log = vm_dir / "serial.log"
        self._storage.prepare_mutable_file(serial_log, create=True)

        loop = asyncio.get_running_loop()
        try:
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
        except Exception:
            self._storage.delete_vm_storage(caller_project_id, vm_id)
            raise

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

    # ------------------------------------------------------------------ vm.serial_log

    async def _h_vm_serial_log(self, params: dict[str, Any]) -> dict[str, Any]:
        caller = caller_from_params(params)
        vm_id = params.get("vm_id")
        tail_lines = max(1, min(int(params.get("tail_lines", 200) or 200), 5000))
        if not vm_id:
            raise IPCError(ERR_INVALID_PARAMS, "vm_id required")
        vm = await self._db.get_vm(vm_id)
        self._policy.check("vm.serial_log", caller, vm)

        serial_log = self._storage.vm_dir(vm.project_id, vm_id) / "serial.log"
        if not serial_log.exists():
            return {
                "vm_id": vm_id,
                "lines": [],
                "total_lines": 0,
                "returned_lines": 0,
                "note": "No serial log yet",
            }

        loop = asyncio.get_running_loop()
        text = await loop.run_in_executor(None, lambda: serial_log.read_text(errors="replace"))
        all_lines = text.splitlines()
        total = len(all_lines)
        return {
            "vm_id": vm_id,
            "lines": all_lines[-tail_lines:],
            "total_lines": total,
            "returned_lines": min(tail_lines, total),
        }

    # ------------------------------------------------------------------ image.preflight

    async def _h_image_preflight(self, params: dict[str, Any]) -> dict[str, Any]:
        caller = caller_from_params(params)
        if not caller.is_admin:
            raise IPCError(ERR_POLICY_VIOLATION, "image.preflight requires admin privileges")
        templates = params.get("templates")
        if templates is not None and not isinstance(templates, list):
            raise IPCError(ERR_INVALID_PARAMS, "templates must be a list of template names")
        reports = await self._image_manager.preflight(templates)
        return {"reports": reports}

    # ------------------------------------------------------------------ profile.list

    async def _h_profile_list(self, params: dict[str, Any]) -> dict[str, Any]:
        return {"profiles": available_profiles()}

    # ------------------------------------------------------------------ image.list

    async def _h_image_list(self, params: dict[str, Any]) -> dict[str, Any]:
        return {"images": self._image_manager.list_images()}

    # ------------------------------------------------------------------ image.prewarm

    async def _h_image_prewarm(self, params: dict[str, Any]) -> dict[str, Any]:
        template = params.get("template")
        if not template:
            raise IPCError(ERR_INVALID_PARAMS, "template required")
        entry = self._image_manager._catalog.get(template)
        if entry is None:
            raise IPCError(ERR_INVALID_PARAMS, f"Unknown template: {template}")
        is_iso = str(entry.get("type") or entry.get("artifact_type")) == "iso"
        if is_iso:
            path = await self._image_manager.ensure_iso(template)
        else:
            path = await self._image_manager.ensure_image(template)
        meta = self._image_manager._read_metadata(template, iso=is_iso) or {}
        current = meta.get("current") or {}
        return {
            "template": template,
            "artifact_type": "iso" if is_iso else "cloud-image",
            "cached": True,
            "path": str(path),
            "current_digest": f"{current.get('algorithm')}:{current.get('digest')}",
            "signature_verified": bool(current.get("signature_verified")),
        }

    # ------------------------------------------------------------------ image.refresh

    async def _h_image_refresh(self, params: dict[str, Any]) -> dict[str, Any]:
        caller = caller_from_params(params)
        if not caller.is_admin:
            raise IPCError(ERR_POLICY_VIOLATION, "image.refresh requires admin privileges")
        template = params.get("template")
        if not template:
            raise IPCError(ERR_INVALID_PARAMS, "template required")
        return await self._image_manager.refresh_image(template)

    # ------------------------------------------------------------------ image.prune

    async def _h_image_prune(self, params: dict[str, Any]) -> dict[str, Any]:
        caller = caller_from_params(params)
        if not caller.is_admin:
            raise IPCError(ERR_POLICY_VIOLATION, "image.prune requires admin privileges")
        older_than_seconds = int(params.get("older_than_seconds", 0) or 0)
        dry_run = bool(params.get("dry_run", False))
        return await self._image_manager.prune(older_than_seconds, dry_run)

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
        "artifact_type": vm.artifact_type,
        "install_state": vm.install_state,
    }
