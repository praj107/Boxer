"""libvirt VM lifecycle: define, start, stop, undefine."""
from __future__ import annotations

import asyncio
import json
import logging
import uuid
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any, Optional

import libvirt
import libvirt_qemu

from boxer.config import BoxerConfig, get_config

logger = logging.getLogger(__name__)

_BOXER_METADATA_NS = "https://github.com/boxer-vm/boxer"
_NVME_DEVICE_ID = "boxer-nvme0"
_NVME_BUS_CANDIDATES = ("pci.1",) + tuple(f"pci.{idx}" for idx in range(16, 1, -1))


def _libvirt_uuid(name: str, vm_id: str) -> str:
    """Return a stable canonical UUID for libvirt domain XML."""
    try:
        return str(uuid.UUID(vm_id))
    except ValueError:
        return str(uuid.uuid5(uuid.NAMESPACE_URL, f"boxer://{name}/{vm_id}"))


def _qmp_error_desc(response: dict[str, Any]) -> str:
    error = response.get("error")
    if isinstance(error, dict):
        return str(error.get("desc") or error.get("class") or error)
    return str(error or response)


def _xml_nvme_emulation_enabled(xml: str) -> bool:
    root = ET.fromstring(xml)
    for child in root.findall("metadata/{%s}nvme" % _BOXER_METADATA_NS):
        if child.get("enabled") == "true":
            return True
    return False


def _xml_primary_disk_path(xml: str) -> Path:
    root = ET.fromstring(xml)
    for disk in root.findall("./devices/disk"):
        if disk.get("device") != "disk":
            continue
        source = disk.find("source")
        path = source.get("file") if source is not None else None
        if path:
            return Path(path)
    raise RuntimeError("Domain XML does not contain a primary file-backed disk")


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
    libvirt_uuid = _libvirt_uuid(name, vm_id)
    display_section = """
  <video>
    <model type='none'/>
  </video>"""
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
  <uuid>{libvirt_uuid}</uuid>
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
      <driver name='qemu' type='qcow2' cache='writeback'/>
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
    <memballoon model='virtio'/>{display_section}
  </devices>
</domain>"""


def _installer_domain_xml(
    *,
    name: str,
    vm_id: str,
    cpu: int,
    ram_mb: int,
    disk_path: Path,
    install_iso: Path,
    seed_iso: Optional[Path],
    net_name: str,
    headless: bool,
    serial_log: Path,
    reboot_policy: str = "destroy",
    disk_format: str = "qcow2",
    emulate_nvme: bool = False,
) -> str:
    """Domain XML for an ISO installer: blank target disk, ISO booted first.

    Per-device ``<boot order>`` is used (order 1 = install ISO, order 2 = target
    disk) so that after the install completes the ISO can be detached and the
    disk becomes the only bootable device. SPICE is on by default for installs.

    reboot_policy controls ``<on_reboot>``:
    - "destroy" (default): end-of-install reboot transitions domain to stopped,
      which is the completion signal used by the install watcher.
    - "restart": domain survives reboots — appropriate for test-boot VMs where
      the ISO is the final runtime, not a transient installer.
    """
    libvirt_uuid = _libvirt_uuid(name, vm_id)
    if disk_format not in {"qcow2", "raw"}:
        raise ValueError(f"Unsupported installer disk format: {disk_format}")
    if emulate_nvme and disk_format != "raw":
        raise ValueError("NVMe emulation requires a raw installer disk")

    metadata_section = ""
    if emulate_nvme:
        metadata_section = f"""
  <metadata>
    <boxer:nvme xmlns:boxer='{_BOXER_METADATA_NS}' enabled='true'/>
  </metadata>"""

    nvme_root_port_section = ""
    if emulate_nvme:
        # QEMU's nvme device must be hotplugged onto a PCIe root port. Reserving
        # index 1 gives the QMP attach path a deterministic free bus: pci.1.
        nvme_root_port_section = """
    <controller type='pci' index='1' model='pcie-root-port'/>"""

    disk_shareable = ""
    if emulate_nvme:
        disk_shareable = "      <shareable/>\n"

    display_section = """
  <video>
    <model type='none'/>
  </video>"""
    if not headless:
        display_section = """
  <graphics type='spice' port='-1' autoport='yes' listen='127.0.0.1'>
    <listen type='address' address='127.0.0.1'/>
  </graphics>
  <video>
    <model type='qxl' ram='65536' vram='65536' vgamem='16384' heads='1'/>
  </video>"""

    seed_section = ""
    if seed_iso:
        seed_section = f"""
    <disk type='file' device='cdrom'>
      <driver name='qemu' type='raw'/>
      <source file='{seed_iso}'/>
      <target dev='sdb' bus='sata'/>
      <readonly/>
    </disk>"""

    return f"""<domain type='kvm'>
  <name>{name}</name>
  <uuid>{libvirt_uuid}</uuid>{metadata_section}
  <memory unit='MiB'>{ram_mb}</memory>
  <currentMemory unit='MiB'>{ram_mb}</currentMemory>
  <vcpu placement='static'>{cpu}</vcpu>
  <os>
    <type arch='x86_64' machine='pc-q35-8.2'>hvm</type>
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
  <on_reboot>{reboot_policy}</on_reboot>
  <on_crash>destroy</on_crash>
  <devices>
    <emulator>/usr/bin/qemu-system-x86_64</emulator>{nvme_root_port_section}
    <disk type='file' device='disk'>
      <driver name='qemu' type='{disk_format}' cache='writeback'/>
      <source file='{disk_path}'/>
      <target dev='vda' bus='virtio'/>
{disk_shareable}      <boot order='2'/>
    </disk>
    <disk type='file' device='cdrom'>
      <driver name='qemu' type='raw'/>
      <source file='{install_iso}'/>
      <target dev='sda' bus='sata'/>
      <readonly/>
      <boot order='1'/>
    </disk>{seed_section}
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
    <memballoon model='virtio'/>{display_section}
  </devices>
</domain>"""


def _xml_boot_to_disk(xml: str) -> str:
    """Rewrite installer domain XML so the VM boots from its target disk only.

    Detaches every cdrom device and removes per-device ``<boot order>`` entries,
    then pins ``<os><boot dev='hd'/>``. Pure function for testability.
    """
    root = ET.fromstring(xml)
    devices = root.find("devices")
    if devices is not None:
        for disk in list(devices.findall("disk")):
            if disk.get("device") == "cdrom":
                devices.remove(disk)
                continue
            for boot in disk.findall("boot"):
                disk.remove(boot)

    os_el = root.find("os")
    if os_el is not None:
        for boot in os_el.findall("boot"):
            os_el.remove(boot)
        boot = ET.SubElement(os_el, "boot")
        boot.set("dev", "hd")
        # Keep <boot> directly after <type> for a conventional, stable ordering.
        type_el = os_el.find("type")
        if type_el is not None:
            os_el.remove(boot)
            os_el.insert(list(os_el).index(type_el) + 1, boot)

    # on_reboot=destroy is needed during install; restore restart for normal use.
    reboot_el = root.find("on_reboot")
    if reboot_el is not None:
        reboot_el.text = "restart"

    return ET.tostring(root, encoding="unicode")


class VMOperations:
    def __init__(self, conn: libvirt.virConnect, cfg: Optional[BoxerConfig] = None):
        self._conn = conn
        self._cfg = cfg or get_config()

    @staticmethod
    def _discard_failed_define(dom: libvirt.virDomain) -> None:
        try:
            if dom.isActive():
                dom.destroy()
        except libvirt.libvirtError:
            pass
        try:
            dom.undefineFlags(
                libvirt.VIR_DOMAIN_UNDEFINE_NVRAM
                | libvirt.VIR_DOMAIN_UNDEFINE_SNAPSHOTS_METADATA
            )
        except libvirt.libvirtError:
            try:
                dom.undefine()
            except libvirt.libvirtError:
                pass

    @staticmethod
    def _safe_destroy(dom: libvirt.virDomain) -> None:
        try:
            if dom.isActive():
                dom.destroy()
        except libvirt.libvirtError:
            pass

    def _qmp(self, dom: libvirt.virDomain, command: dict[str, Any]) -> dict[str, Any]:
        raw = libvirt_qemu.qemuMonitorCommand(
            dom,
            json.dumps(command),
            libvirt_qemu.VIR_DOMAIN_QEMU_MONITOR_COMMAND_DEFAULT,
        )
        return json.loads(raw)

    def _qmp_return(self, dom: libvirt.virDomain, command: dict[str, Any]) -> Any:
        response = self._qmp(dom, command)
        if "error" in response:
            raise RuntimeError(
                f"QMP {command.get('execute')} failed: {_qmp_error_desc(response)}"
            )
        return response.get("return")

    def _block_node_for_disk(self, dom: libvirt.virDomain, disk_path: Path) -> str:
        blocks = self._qmp_return(dom, {"execute": "query-block"})
        candidates = {str(disk_path)}
        try:
            candidates.add(str(disk_path.resolve()))
        except OSError:
            pass

        for block in blocks or []:
            inserted = block.get("inserted") or {}
            paths = {
                inserted.get("file"),
                (inserted.get("image") or {}).get("filename"),
            }
            if not any(path in candidates for path in paths if path):
                continue
            node = inserted.get("node-name")
            if node:
                return str(node)
        raise RuntimeError(f"QMP block node not found for disk {disk_path}")

    def _attach_nvme_disk(
        self,
        dom: libvirt.virDomain,
        *,
        disk_path: Path,
        vm_id: str,
    ) -> None:
        node = self._block_node_for_disk(dom, disk_path)
        serial_suffix = "".join(ch for ch in vm_id if ch.isalnum())[:20] or uuid.uuid4().hex[:12]
        base_args: dict[str, Any] = {
            "driver": "nvme",
            "id": _NVME_DEVICE_ID,
            "drive": node,
            "serial": f"boxer-{serial_suffix}",
            "share-rw": True,
        }

        last_error = "no PCIe root port candidates tried"
        for bus in _NVME_BUS_CANDIDATES:
            response = self._qmp(
                dom,
                {
                    "execute": "device_add",
                    "arguments": {**base_args, "bus": bus},
                },
            )
            if "return" in response:
                logger.info("Attached NVMe device %s on %s", _NVME_DEVICE_ID, bus)
                return

            last_error = _qmp_error_desc(response)
            if any(
                fragment in last_error
                for fragment in (
                    "not found",
                    "not available",
                    "does not support hotplugging",
                )
            ):
                continue
            raise RuntimeError(f"QMP device_add nvme failed: {last_error}")

        raise RuntimeError(f"QMP device_add nvme failed: {last_error}")

    def _create_with_nvme(
        self,
        dom: libvirt.virDomain,
        *,
        disk_path: Path,
        vm_id: str,
    ) -> None:
        dom.createWithFlags(libvirt.VIR_DOMAIN_START_PAUSED)
        try:
            self._attach_nvme_disk(dom, disk_path=disk_path, vm_id=vm_id)
            dom.resume()
        except Exception:
            self._safe_destroy(dom)
            raise

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
        try:
            dom.setAutostart(0)
            dom.create()
        except libvirt.libvirtError:
            self._discard_failed_define(dom)
            raise
        logger.info("Defined and started domain %s", libvirt_name)
        return dom

    def define_and_start_installer(
        self,
        *,
        libvirt_name: str,
        vm_id: str,
        cpu: int,
        ram_mb: int,
        disk_path: Path,
        install_iso: Path,
        seed_iso: Optional[Path],
        net_name: str,
        headless: bool,
        serial_log: Path,
        reboot_policy: str = "destroy",
        disk_format: str = "qcow2",
        emulate_nvme: bool = False,
    ) -> libvirt.virDomain:
        xml = _installer_domain_xml(
            name=libvirt_name,
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
            emulate_nvme=emulate_nvme,
        )
        dom = self._conn.defineXML(xml)
        try:
            dom.setAutostart(0)
            if emulate_nvme:
                self._create_with_nvme(dom, disk_path=disk_path, vm_id=vm_id)
            else:
                dom.create()
        except Exception:
            self._discard_failed_define(dom)
            raise
        logger.info("Defined and started installer domain %s", libvirt_name)
        return dom

    def switch_boot_to_disk(self, libvirt_name: str) -> None:
        """Redefine a finished installer domain to boot from disk and drop the ISO."""
        try:
            dom = self._conn.lookupByName(libvirt_name)
        except libvirt.libvirtError as exc:
            raise RuntimeError(f"Domain {libvirt_name} not found: {exc}") from exc
        if dom.isActive():
            try:
                dom.destroy()
            except libvirt.libvirtError:
                pass
        new_xml = _xml_boot_to_disk(dom.XMLDesc(0))
        self._conn.defineXML(new_xml)
        logger.info("Switched %s boot order to disk", libvirt_name)

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
                xml = dom.XMLDesc(0)
                if _xml_nvme_emulation_enabled(xml):
                    self._create_with_nvme(
                        dom,
                        disk_path=_xml_primary_disk_path(xml),
                        vm_id=libvirt_name,
                    )
                else:
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

    def _get_ip_via_dhcp_lease(self, libvirt_name: str) -> Optional[str]:
        """Return the VM's IPv4 address from the libvirt network DHCP lease table.

        This is a synchronous, non-blocking read; it returns None if the lease
        is not present yet or if anything in the libvirt call chain fails.
        """
        try:
            dom = self._conn.lookupByName(libvirt_name)
            xml = dom.XMLDesc(0)
            root = ET.fromstring(xml)
            iface_el = root.find("./devices/interface[@type='network']")
            if iface_el is None:
                return None
            mac_el = iface_el.find("mac")
            src_el = iface_el.find("source")
            if mac_el is None or src_el is None:
                return None
            mac = mac_el.get("address")
            net_name = src_el.get("network")
            if not mac or not net_name:
                return None
            net = self._conn.networkLookupByName(net_name)
            for lease in net.DHCPLeases(mac, 0) or []:
                if lease.get("type") == 0:  # AF_INET / IPv4
                    ip = lease.get("ipaddr")
                    if ip and not ip.startswith("127."):
                        return ip
        except Exception:
            pass
        return None

    async def get_ip_via_guest_agent(self, libvirt_name: str, timeout: float = 30.0) -> Optional[str]:
        """Poll for the VM's primary IPv4 address.

        Tries the QEMU guest agent first on each iteration (authoritative,
        works behind NAT); falls back to the libvirt DHCP lease table when the
        guest agent is not yet ready.  The DHCP fallback lets IP discovery
        succeed even on distros where qemu-guest-agent starts late (e.g. SELinux
        policy races on Fedora) or is unavailable.
        """
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
            ip = self._get_ip_via_dhcp_lease(libvirt_name)
            if ip:
                return ip
            await asyncio.sleep(2)
        return None
