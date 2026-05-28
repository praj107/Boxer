"""libvirt VM lifecycle: define, start, stop, undefine."""
from __future__ import annotations

import asyncio
import logging
import uuid
from pathlib import Path
from typing import Optional

import libvirt

from boxer.config import BoxerConfig, get_config

logger = logging.getLogger(__name__)


def _domain_xml(
    *,
    name: str,
    vm_id: str,
    cpu: int,
    ram_mb: int,
    disk_path: Path,
    cloud_init_iso: Optional[Path],
    net_name: str,
    headless: bool,
    serial_log: Path,
) -> str:
    display_section = ""
    if not headless:
        display_section = """
  <graphics type='spice' port='-1' autoport='yes' listen='127.0.0.1'>
    <listen type='address' address='127.0.0.1'/>
  </graphics>
  <video>
    <model type='qxl' ram='65536' vram='65536' vgamem='16384' heads='1'/>
  </video>"""

    cdrom_section = ""
    if cloud_init_iso:
        cdrom_section = f"""
    <disk type='file' device='cdrom'>
      <driver name='qemu' type='raw'/>
      <source file='{cloud_init_iso}'/>
      <target dev='sdb' bus='sata'/>
      <readonly/>
    </disk>"""

    return f"""<domain type='kvm'>
  <name>{name}</name>
  <uuid>{vm_id}</uuid>
  <memory unit='MiB'>{ram_mb}</memory>
  <currentMemory unit='MiB'>{ram_mb}</currentMemory>
  <vcpu placement='static'>{cpu}</vcpu>
  <os>
    <type arch='x86_64' machine='pc-q35-8.2'>hvm</type>
    <boot dev='hd'/>
  </os>
  <features>
    <acpi/>
    <apic/>
  </features>
  <cpu mode='host-passthrough' check='none' migratable='on'/>
  <clock offset='utc'>
    <timer name='rtc' tickpolicy='catchup'/>
    <timer name='pit' tickpolicy='delay'/>
    <timer name='hpet' present='no'/>
  </clock>
  <on_poweroff>destroy</on_poweroff>
  <on_reboot>restart</on_reboot>
  <on_crash>destroy</on_crash>
  <devices>
    <emulator>/usr/bin/qemu-system-x86_64</emulator>
    <disk type='file' device='disk'>
      <driver name='qemu' type='qcow2' cache='writeback' io='native'/>
      <source file='{disk_path}'/>
      <target dev='vda' bus='virtio'/>
    </disk>{cdrom_section}
    <controller type='usb' model='qemu-xhci'/>
    <interface type='network'>
      <source network='{net_name}'/>
      <model type='virtio'/>
    </interface>
    <serial type='file'>
      <source path='{serial_log}'/>
      <target type='isa-serial' port='0'/>
    </serial>
    <console type='file'>
      <source path='{serial_log}'/>
      <target type='serial' port='0'/>
    </console>
    <channel type='unix'>
      <target type='virtio' name='org.qemu.guest_agent.0'/>
    </channel>
    <rng model='virtio'>
      <backend model='random'>/dev/urandom</backend>
    </rng>
    <memballoon model='virtio'/>
  </devices>{display_section}
</domain>"""


class VMOperations:
    def __init__(self, conn: libvirt.virConnect, cfg: Optional[BoxerConfig] = None):
        self._conn = conn
        self._cfg = cfg or get_config()

    def define_and_start(
        self,
        *,
        libvirt_name: str,
        vm_id: str,
        cpu: int,
        ram_mb: int,
        disk_path: Path,
        cloud_init_iso: Optional[Path],
        net_name: str,
        headless: bool,
        serial_log: Path,
    ) -> libvirt.virDomain:
        xml = _domain_xml(
            name=libvirt_name,
            vm_id=vm_id,
            cpu=cpu,
            ram_mb=ram_mb,
            disk_path=disk_path,
            cloud_init_iso=cloud_init_iso,
            net_name=net_name,
            headless=headless,
            serial_log=serial_log,
        )
        dom = self._conn.defineXML(xml)
        dom.setAutostart(0)
        dom.create()
        logger.info("Defined and started domain %s", libvirt_name)
        return dom

    def stop(self, libvirt_name: str, graceful: bool = True) -> None:
        try:
            dom = self._conn.lookupByName(libvirt_name)
        except libvirt.libvirtError:
            return
        if graceful:
            try:
                dom.shutdown()
                return
            except libvirt.libvirtError:
                pass
        try:
            dom.destroy()
        except libvirt.libvirtError as exc:
            logger.debug("destroy failed for %s: %s", libvirt_name, exc)

    def start(self, libvirt_name: str) -> None:
        try:
            dom = self._conn.lookupByName(libvirt_name)
            if dom.isActive() == 0:
                dom.create()
        except libvirt.libvirtError as exc:
            raise RuntimeError(f"Failed to start {libvirt_name}: {exc}") from exc

    def undefine(self, libvirt_name: str) -> None:
        try:
            dom = self._conn.lookupByName(libvirt_name)
            if dom.isActive():
                dom.destroy()
            dom.undefineFlags(
                libvirt.VIR_DOMAIN_UNDEFINE_NVRAM
                | libvirt.VIR_DOMAIN_UNDEFINE_SNAPSHOTS_METADATA
            )
        except libvirt.libvirtError as exc:
            logger.debug("undefine failed for %s: %s", libvirt_name, exc)

    def get_state(self, libvirt_name: str) -> str:
        try:
            dom = self._conn.lookupByName(libvirt_name)
            state, _ = dom.state()
            return {
                libvirt.VIR_DOMAIN_RUNNING: "running",
                libvirt.VIR_DOMAIN_PAUSED: "paused",
                libvirt.VIR_DOMAIN_SHUTDOWN: "stopping",
                libvirt.VIR_DOMAIN_SHUTOFF: "stopped",
                libvirt.VIR_DOMAIN_CRASHED: "error",
                libvirt.VIR_DOMAIN_PMSUSPENDED: "stopped",
            }.get(state, "unknown")
        except libvirt.libvirtError:
            return "unknown"

    def snapshot(self, libvirt_name: str, label: str) -> str:
        try:
            dom = self._conn.lookupByName(libvirt_name)
            snap_name = f"boxer-snap-{label}-{uuid.uuid4().hex[:6]}"
            xml = f"<domainsnapshot><name>{snap_name}</name></domainsnapshot>"
            dom.snapshotCreateXML(xml, 0)
            logger.info("Snapshot %s created for %s", snap_name, libvirt_name)
            return snap_name
        except libvirt.libvirtError as exc:
            raise RuntimeError(f"Snapshot failed: {exc}") from exc

    async def get_ip_via_guest_agent(self, libvirt_name: str, timeout: float = 30.0) -> Optional[str]:
        """Poll QEMU guest agent for the VM's primary IP address."""
        import json as _json

        deadline = asyncio.get_running_loop().time() + timeout
        while asyncio.get_running_loop().time() < deadline:
            try:
                dom = self._conn.lookupByName(libvirt_name)
                result_str = dom.qemuAgentCommand(
                    '{"execute":"guest-network-get-interfaces"}', 5, 0
                )
                result = _json.loads(result_str)
                for iface in result.get("return", []):
                    if iface.get("name") in ("lo", "loopback"):
                        continue
                    for addr in iface.get("ip-addresses", []):
                        if addr.get("ip-address-type") == "ipv4":
                            ip = addr["ip-address"]
                            if not ip.startswith("127."):
                                return ip
            except Exception:
                pass
            await asyncio.sleep(2)
        return None
