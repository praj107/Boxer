"""libvirt storage pool + qcow2 overlay management."""
from __future__ import annotations

import asyncio
import logging
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

    def ensure_pool(self) -> None:
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
        vm_dir = self.vm_dir(project_id, vm_id)
        vm_dir.mkdir(parents=True, exist_ok=True)
        overlay = vm_dir / "disk.qcow2"
        if overlay.exists():
            return overlay

        loop = asyncio.get_running_loop()
        await loop.run_in_executor(
            None,
            self._create_overlay_sync,
            str(base_path),
            str(overlay),
            disk_gb,
        )
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

    async def create_blank_disk(self, project_id: str, vm_id: str, disk_gb: int) -> Path:
        """Create an empty qcow2 target disk (no backing file) for an ISO install."""
        vm_dir = self.vm_dir(project_id, vm_id)
        vm_dir.mkdir(parents=True, exist_ok=True)
        disk = vm_dir / "disk.qcow2"
        if disk.exists():
            return disk

        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, self._create_blank_sync, str(disk), disk_gb)
        logger.info("Created blank install disk %s (size=%dG)", disk, disk_gb)
        return disk

    @staticmethod
    def _create_blank_sync(dest: str, size_gb: int) -> None:
        subprocess.run(
            ["qemu-img", "create", "-f", "qcow2", dest, f"{size_gb}G"],
            check=True,
            capture_output=True,
        )

    def delete_vm_storage(self, project_id: str, vm_id: str) -> None:
        import shutil
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
