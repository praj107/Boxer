"""libvirt storage pool + qcow2 overlay management."""
from __future__ import annotations

import asyncio
import grp
import logging
import os
import shutil
import subprocess
from pathlib import Path
from typing import Optional

import libvirt

from boxer.config import BoxerConfig, get_config

logger = logging.getLogger(__name__)

_POOL_NAME = "boxer-storage"


def _pool_xml(state_dir: Path) -> str:
    return f"""<pool type='dir'>
  <name>{_POOL_NAME}</name>
  <target>
    <path>{state_dir}</path>
  </target>
</pool>"""


class StorageManager:
    def __init__(self, conn: libvirt.virConnect, cfg: Optional[BoxerConfig] = None):
        self._conn = conn
        self._cfg = cfg or get_config()
        self._qemu_gid = self._lookup_qemu_gid()

    def _lookup_qemu_gid(self) -> Optional[int]:
        try:
            return grp.getgrnam(self._cfg.qemu_group).gr_gid
        except KeyError:
            logger.warning("Configured qemu_group '%s' does not exist", self._cfg.qemu_group)
            return None

    def _set_access(self, path: Path, mode: int) -> None:
        try:
            if self._qemu_gid is not None:
                os.chown(path, -1, self._qemu_gid)
        except PermissionError:
            logger.debug("Could not chgrp %s to %s", path, self._cfg.qemu_group)
        try:
            os.chmod(path, mode)
        except PermissionError:
            logger.debug("Could not chmod %s to %o", path, mode)

    def _ensure_vm_dir(self, project_id: str, vm_id: str) -> Path:
        project_dir = self._cfg.projects_dir / project_id
        vms_dir = project_dir / "vms"
        vm_dir = self.vm_dir(project_id, vm_id)
        for path in (self._cfg.state_dir, self._cfg.projects_dir, project_dir, vms_dir, vm_dir):
            path.mkdir(parents=True, exist_ok=True)
            self._set_access(path, 0o750)
        return vm_dir

    def prepare_readonly_file(self, path: Optional[Path]) -> None:
        if path is not None and path.exists():
            self._set_access(path, 0o440)

    def prepare_mutable_file(self, path: Path, *, create: bool = False) -> None:
        if create:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.touch(exist_ok=True)
        if path.exists():
            self._set_access(path, 0o660)

    def ensure_pool(self) -> None:
        self._cfg.state_dir.mkdir(parents=True, exist_ok=True)
        self._set_access(self._cfg.state_dir, 0o750)
        try:
            pool = self._conn.storagePoolLookupByName(_POOL_NAME)
            if pool.isActive() == 0:
                pool.create()
        except libvirt.libvirtError:
            xml = _pool_xml(self._cfg.state_dir)
            pool = self._conn.storagePoolDefineXML(xml, 0)
            pool.setAutostart(1)
            pool.create(0)
        logger.debug("Storage pool %s ready", _POOL_NAME)

    def vm_dir(self, project_id: str, vm_id: str) -> Path:
        return self._cfg.projects_dir / project_id / "vms" / vm_id

    async def create_overlay(self, project_id: str, vm_id: str, base_path: Path, disk_gb: int) -> Path:
        self.prepare_readonly_file(base_path)
        vm_dir = self._ensure_vm_dir(project_id, vm_id)
        overlay = vm_dir / "disk.qcow2"
        if overlay.exists():
            self.prepare_mutable_file(overlay)
            return overlay

        loop = asyncio.get_running_loop()
        await loop.run_in_executor(
            None,
            self._create_overlay_sync,
            str(base_path),
            str(overlay),
            disk_gb,
        )
        self.prepare_mutable_file(overlay)
        logger.info("Created overlay %s (base=%s, size=%dG)", overlay, base_path, disk_gb)
        return overlay

    @staticmethod
    def _create_overlay_sync(base: str, dest: str, size_gb: int) -> None:
        subprocess.run(
            ["qemu-img", "create", "-f", "qcow2", "-b", base, "-F", "qcow2",
             dest, f"{size_gb}G"],
            check=True,
            capture_output=True,
        )

    async def create_blank_disk(
        self,
        project_id: str,
        vm_id: str,
        disk_gb: int,
        *,
        disk_format: str = "qcow2",
    ) -> Path:
        """Create an empty target disk (no backing file) for an ISO install."""
        if disk_format not in {"qcow2", "raw"}:
            raise ValueError(f"Unsupported blank disk format: {disk_format}")
        vm_dir = self._ensure_vm_dir(project_id, vm_id)
        disk = vm_dir / ("disk.raw" if disk_format == "raw" else "disk.qcow2")
        if disk.exists():
            self.prepare_mutable_file(disk)
            return disk

        loop = asyncio.get_running_loop()
        await loop.run_in_executor(
            None,
            self._create_blank_sync,
            str(disk),
            disk_gb,
            disk_format,
        )
        self.prepare_mutable_file(disk)
        logger.info(
            "Created blank install disk %s (size=%dG, format=%s)",
            disk,
            disk_gb,
            disk_format,
        )
        return disk

    @staticmethod
    def _create_blank_sync(dest: str, size_gb: int, disk_format: str) -> None:
        subprocess.run(
            ["qemu-img", "create", "-f", disk_format, dest, f"{size_gb}G"],
            check=True,
            capture_output=True,
        )

    async def stage_iso(self, project_id: str, vm_id: str, source: Path) -> Path:
        """Copy a caller-provided ISO into VM storage so QEMU can read it."""
        vm_dir = self._ensure_vm_dir(project_id, vm_id)
        dest = vm_dir / "install.iso"
        src = source.resolve()
        if not dest.exists() or dest.resolve() != src:
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(None, shutil.copy2, src, dest)
        self.prepare_readonly_file(dest)
        logger.info("Staged local ISO %s for VM %s", src, vm_id)
        return dest

    def delete_vm_storage(self, project_id: str, vm_id: str) -> None:
        vm_dir = self.vm_dir(project_id, vm_id)
        if vm_dir.exists():
            shutil.rmtree(vm_dir)
            logger.info("Deleted storage for VM %s", vm_id)

    def project_disk_usage_gib(self, project_id: str) -> float:
        project_dir = self._cfg.projects_dir / project_id
        if not project_dir.exists():
            return 0.0
        total = sum(
            f.stat().st_size
            for f in project_dir.rglob("*")
            if f.is_file()
        )
        return total / (1024 ** 3)
