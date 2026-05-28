"""Admission control and queue runner."""
from __future__ import annotations

import asyncio
import logging
import os
from typing import TYPE_CHECKING, Any, Callable, Coroutine, Optional

import libvirt

from boxer.config import BoxerConfig, get_config
from boxer.ipc import ERR_RESOURCE_EXHAUSTED, IPCError

if TYPE_CHECKING:
    from boxerd.db import Database

logger = logging.getLogger(__name__)


def _host_total_ram_gib() -> float:
    try:
        with open("/proc/meminfo") as f:
            for line in f:
                if line.startswith("MemTotal:"):
                    kb = int(line.split()[1])
                    return kb / (1024 * 1024)
    except Exception:
        pass
    return 8.0


def _host_cpu_threads() -> int:
    return os.cpu_count() or 4


class HostCapacity:
    def __init__(self, conn: libvirt.virConnect, cfg: Optional[BoxerConfig] = None):
        self._conn = conn
        self._cfg = cfg or get_config()

    def get_status(self) -> dict[str, Any]:
        total_ram = _host_total_ram_gib()
        usable_ram = total_ram - self._cfg.host_reserved_ram_gib
        host_threads = _host_cpu_threads()
        max_vcpus = self._cfg.max_total_vcpus or max(1, host_threads - 2)

        running_vms = self._count_running_boxer_vms()
        used_ram_gib = self._used_ram_gib()
        used_vcpus = self._used_vcpus()

        return {
            "total_ram_gib": round(total_ram, 2),
            "usable_ram_gib": round(usable_ram, 2),
            "free_ram_gib": round(max(0.0, usable_ram - used_ram_gib), 2),
            "host_threads": host_threads,
            "max_vcpus": max_vcpus,
            "used_vcpus": used_vcpus,
            "free_vcpus": max(0, max_vcpus - used_vcpus),
            "running_vms": running_vms,
        }

    def _count_running_boxer_vms(self) -> int:
        try:
            doms = self._conn.listAllDomains(libvirt.VIR_CONNECT_LIST_DOMAINS_ACTIVE)
            return sum(1 for d in doms if d.name().startswith("Boxer--"))
        except libvirt.libvirtError:
            return 0

    def _used_ram_gib(self) -> float:
        total = 0
        try:
            doms = self._conn.listAllDomains(libvirt.VIR_CONNECT_LIST_DOMAINS_ACTIVE)
            for dom in doms:
                if not dom.name().startswith("Boxer--"):
                    continue
                info = dom.info()
                total += info[1]  # maxMem in KiB
        except libvirt.libvirtError:
            pass
        return total / (1024 * 1024)

    def _used_vcpus(self) -> int:
        total = 0
        try:
            doms = self._conn.listAllDomains(libvirt.VIR_CONNECT_LIST_DOMAINS_ACTIVE)
            for dom in doms:
                if not dom.name().startswith("Boxer--"):
                    continue
                info = dom.info()
                total += info[3]  # nrVirtCpu
        except libvirt.libvirtError:
            pass
        return total


class Scheduler:
    def __init__(
        self,
        db: "Database",
        capacity: HostCapacity,
        vm_create_fn: Callable[[dict[str, Any]], Coroutine[Any, Any, Any]],
        cfg: Optional[BoxerConfig] = None,
    ):
        self._db = db
        self._capacity = capacity
        self._vm_create_fn = vm_create_fn
        self._cfg = cfg or get_config()
        self._queue_task: Optional[asyncio.Task] = None

    async def check_admission(
        self,
        project_id: str,
        cpu: int,
        ram_mb: int,
        disk_gb: int,
    ) -> Optional[str]:
        """Return None if admitted, or a human-readable reason string if denied."""
        status = self._capacity.get_status()

        ram_gib = ram_mb / 1024
        if ram_gib > status["free_ram_gib"]:
            return f"Insufficient host RAM: need {ram_gib:.1f}G, free {status['free_ram_gib']:.1f}G"

        if cpu > status["free_vcpus"]:
            return f"Insufficient vCPUs: need {cpu}, free {status['free_vcpus']}"

        project_vms = await self._db.list_vms(project_id)
        running = [v for v in project_vms if v.state == "running"]
        if len(running) >= self._cfg.max_running_per_project:
            return (
                f"Project has {len(running)} running VMs "
                f"(limit {self._cfg.max_running_per_project})"
            )

        used_disk = sum(v.disk_gb for v in project_vms)
        if used_disk + disk_gb > self._cfg.max_disk_per_project_gib:
            return (
                f"Project disk limit exceeded: using {used_disk}G + {disk_gb}G "
                f"> {self._cfg.max_disk_per_project_gib}G"
            )

        return None

    async def enqueue_request(
        self, project_id: str, request: dict[str, Any], reason: str
    ) -> str:
        return await self._db.enqueue(project_id, request, reason)

    def start_queue_runner(self) -> None:
        self._queue_task = asyncio.create_task(self._queue_loop())

    async def stop(self) -> None:
        if self._queue_task:
            self._queue_task.cancel()
            try:
                await self._queue_task
            except asyncio.CancelledError:
                pass

    async def _queue_loop(self) -> None:
        while True:
            try:
                await self._process_queue()
            except Exception:
                logger.exception("Error in queue loop")
            await asyncio.sleep(30)

    async def _process_queue(self) -> None:
        entries = await self._db.list_pending_queue()
        for entry in entries:
            import json
            request = json.loads(entry.request_json)
            cpu = request.get("cpu", 2)
            ram_mb = request.get("ram_mb", 2048)
            disk_gb = request.get("disk_gb", 20)

            reason = await self.check_admission(entry.project_id, cpu, ram_mb, disk_gb)
            if reason:
                continue

            await self._db.update_queue_status(entry.id, "processing")
            try:
                await self._vm_create_fn(request)
                await self._db.update_queue_status(entry.id, "done")
                logger.info("Queue entry %s promoted to VM", entry.id)
            except Exception as exc:
                logger.exception("Failed to create VM from queue entry %s", entry.id)
                await self._db.update_queue_status(entry.id, "expired")
