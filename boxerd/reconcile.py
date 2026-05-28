"""Cross-reference libvirt domains against DB records to detect divergence."""
from __future__ import annotations

import asyncio
import json
import logging
import subprocess
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Optional

import libvirt

if TYPE_CHECKING:
    from boxerd.db import Database

logger = logging.getLogger(__name__)

BOXER_PREFIX = "Boxer--"


@dataclass
class OrphanedBoxerDomain:
    """Boxer-named domain with no DB record (crash recovery / DB wipe)."""
    libvirt_name: str
    parsed_project_id: str
    parsed_display_name: str
    parsed_vm_id: str
    disk_path: Optional[str]
    disk_gb: int
    is_active: bool
    suggested_action: str = "adopt"


@dataclass
class ForeignDomain:
    """Non-Boxer libvirt domain not tracked by Boxer."""
    libvirt_name: str
    disk_path: Optional[str]
    disk_gb: int
    is_active: bool
    suggested_action: str = "import"


@dataclass
class GhostRecord:
    """DB record whose libvirt domain no longer exists."""
    vm_id: str
    libvirt_name: str
    project_id: str
    display_name: str
    suggested_action: str = "purge"


@dataclass
class ReconcileReport:
    orphaned_boxer: list[OrphanedBoxerDomain] = field(default_factory=list)
    foreign_vms: list[ForeignDomain] = field(default_factory=list)
    ghost_records: list[GhostRecord] = field(default_factory=list)


def parse_boxer_name(libvirt_name: str) -> Optional[tuple[str, str, str]]:
    """
    Parse Boxer--<project_id>--<display_name>--<vm_id> into a 3-tuple.

    Returns (project_id, display_name, vm_id) or None if the name is
    malformed or the vm_id segment does not look like a Boxer-generated ID.
    """
    if not libvirt_name.startswith(BOXER_PREFIX):
        return None
    rest = libvirt_name[len(BOXER_PREFIX):]
    # maxsplit=2 so that display_name itself may contain '--'
    parts = rest.split("--", maxsplit=2)
    if len(parts) != 3:
        return None
    project_id, display_name, vm_id = parts
    if not vm_id.startswith("vm_") or len(vm_id) < 6:
        return None
    return project_id, display_name, vm_id


def _extract_disk_path(xml_str: str) -> Optional[str]:
    """Return the primary disk path from a libvirt domain XML string."""
    try:
        root = ET.fromstring(xml_str)
        for disk in root.findall(".//disk[@device='disk']"):
            source = disk.find("source")
            if source is not None:
                path = source.get("file") or source.get("dev")
                if path:
                    return path
    except ET.ParseError:
        logger.debug("Failed to parse domain XML for disk path extraction")
    return None


def _get_disk_size_gb(path: str) -> int:
    """Query actual virtual disk size via qemu-img info. Returns 0 on failure."""
    try:
        result = subprocess.run(
            ["qemu-img", "info", "--output=json", path],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode == 0:
            info = json.loads(result.stdout)
            virtual_size = info.get("virtual-size", 0)
            return max(1, int(virtual_size) // (1024 ** 3))
    except Exception:
        pass
    return 0


async def build_reconcile_report(
    conn: libvirt.virConnect,
    db: "Database",
) -> ReconcileReport:
    """
    Cross-reference all libvirt domains against all DB records.

    Blocking libvirt calls run in the executor so the event loop is not blocked.
    """
    loop = asyncio.get_running_loop()

    def _list_domains() -> list[tuple[str, str, bool]]:
        """Return (name, xml, is_active) for every defined domain."""
        results = []
        try:
            for dom in conn.listAllDomains(0):
                try:
                    results.append((dom.name(), dom.XMLDesc(0), bool(dom.isActive())))
                except libvirt.libvirtError:
                    pass
        except libvirt.libvirtError:
            pass
        return results

    all_domains = await loop.run_in_executor(None, _list_domains)
    libvirt_names: set[str] = {name for name, _, _ in all_domains}

    db_vms = await db.list_vms()
    db_libvirt_names: set[str] = {vm.libvirt_name for vm in db_vms}

    report = ReconcileReport()

    for name, xml, is_active in all_domains:
        if name in db_libvirt_names:
            continue

        disk_path = _extract_disk_path(xml)
        disk_gb = 0
        if disk_path:
            disk_gb = await loop.run_in_executor(None, _get_disk_size_gb, disk_path)

        parsed = parse_boxer_name(name)
        if parsed is not None:
            project_id, display_name, vm_id = parsed
            report.orphaned_boxer.append(OrphanedBoxerDomain(
                libvirt_name=name,
                parsed_project_id=project_id,
                parsed_display_name=display_name,
                parsed_vm_id=vm_id,
                disk_path=disk_path,
                disk_gb=disk_gb,
                is_active=is_active,
            ))
        else:
            report.foreign_vms.append(ForeignDomain(
                libvirt_name=name,
                disk_path=disk_path,
                disk_gb=disk_gb,
                is_active=is_active,
            ))

    # Ghost records: in DB but absent from libvirt
    for vm in db_vms:
        if vm.libvirt_name not in libvirt_names:
            report.ghost_records.append(GhostRecord(
                vm_id=vm.id,
                libvirt_name=vm.libvirt_name,
                project_id=vm.project_id,
                display_name=vm.display_name,
            ))

    return report
