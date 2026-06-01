"""Tests for installer domain XML generation and boot-order flip."""
from __future__ import annotations

import uuid
import xml.etree.ElementTree as ET
import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import libvirt

from boxer.config import BoxerConfig
from boxerd.vm_ops import VMOperations, _domain_xml, _installer_domain_xml, _xml_boot_to_disk


def _assert_valid_libvirt_uuid(root: ET.Element) -> None:
    value = root.findtext("uuid")
    assert value is not None
    assert str(uuid.UUID(value)) == value
    assert not value.startswith("vm_")


def test_headed_cloud_image_graphics_inside_devices() -> None:
    xml = _domain_xml(
        name="Boxer--p_x--vm--vm_1",
        vm_id="vm_1",
        cpu=2,
        ram_mb=2048,
        disk_path=Path("/var/lib/boxer/disk.qcow2"),
        cloud_init_iso=Path("/var/lib/boxer/cloud-init.iso"),
        net_name="boxer-net-x",
        headless=False,
        serial_log=Path("/var/lib/boxer/serial.log"),
    )
    root = ET.fromstring(xml)
    _assert_valid_libvirt_uuid(root)
    # graphics must be a child of <devices>, not a stray child of <domain>.
    assert root.find("./devices/graphics") is not None
    assert root.find("./graphics") is None


def test_cloud_image_primary_disk_uses_libvirt_compatible_io_defaults() -> None:
    root = ET.fromstring(
        _domain_xml(
            name="Boxer--p_x--vm--vm_1",
            vm_id="vm_1",
            cpu=2,
            ram_mb=2048,
            disk_path=Path("/var/lib/boxer/disk.qcow2"),
            cloud_init_iso=None,
            net_name="boxer-net-x",
            headless=True,
            serial_log=Path("/var/lib/boxer/serial.log"),
        )
    )
    driver = root.find("./devices/disk[@device='disk']/driver")
    assert driver is not None
    assert driver.get("cache") == "writeback"
    assert driver.get("io") is None


def _installer_xml(seed: bool = True, headless: bool = False) -> str:
    return _installer_domain_xml(
        name="Boxer--p_x--inst--vm_1",
        vm_id="vm_1",
        cpu=2,
        ram_mb=2048,
        disk_path=Path("/var/lib/boxer/disk.qcow2"),
        install_iso=Path("/var/lib/boxer/isos/arch/installer.iso"),
        seed_iso=Path("/var/lib/boxer/seed.iso") if seed else None,
        net_name="boxer-net-x",
        headless=headless,
        serial_log=Path("/var/lib/boxer/serial.log"),
    )


def test_installer_boots_iso_before_disk() -> None:
    root = ET.fromstring(_installer_xml())
    _assert_valid_libvirt_uuid(root)
    disks = root.findall("./devices/disk")
    by_dev = {d.find("target").get("dev"): d for d in disks}

    # Install ISO (sda, cdrom) boots first; target disk (vda) second.
    assert by_dev["sda"].get("device") == "cdrom"
    assert by_dev["sda"].find("boot").get("order") == "1"
    assert by_dev["vda"].get("device") == "disk"
    assert by_dev["vda"].find("boot").get("order") == "2"
    # No <os><boot> when per-device boot order is used.
    assert root.find("./os/boot") is None


def test_installer_primary_disk_uses_libvirt_compatible_io_defaults() -> None:
    root = ET.fromstring(_installer_xml())
    driver = root.find("./devices/disk[@device='disk']/driver")
    assert driver is not None
    assert driver.get("cache") == "writeback"
    assert driver.get("io") is None


def test_installer_nvme_emulation_uses_raw_shareable_disk_and_reserved_root_port() -> None:
    root = ET.fromstring(
        _installer_domain_xml(
            name="Boxer--p_x--inst--vm_1",
            vm_id="vm_1",
            cpu=2,
            ram_mb=2048,
            disk_path=Path("/var/lib/boxer/disk.raw"),
            install_iso=Path("/var/lib/boxer/install.iso"),
            seed_iso=None,
            net_name="boxer-net-x",
            headless=True,
            serial_log=Path("/var/lib/boxer/serial.log"),
            disk_format="raw",
            emulate_nvme=True,
        )
    )

    assert root.find("./metadata/{https://github.com/boxer-vm/boxer}nvme").get("enabled") == "true"
    disk = root.find("./devices/disk[@device='disk']")
    assert disk.find("driver").get("type") == "raw"
    assert disk.find("shareable") is not None
    root_port = root.find("./devices/controller[@type='pci'][@index='1']")
    assert root_port is not None
    assert root_port.get("model") == "pcie-root-port"


def test_installer_nvme_emulation_requires_raw_disk() -> None:
    try:
        _installer_domain_xml(
            name="Boxer--p_x--inst--vm_1",
            vm_id="vm_1",
            cpu=2,
            ram_mb=2048,
            disk_path=Path("/var/lib/boxer/disk.qcow2"),
            install_iso=Path("/var/lib/boxer/install.iso"),
            seed_iso=None,
            net_name="boxer-net-x",
            headless=True,
            serial_log=Path("/var/lib/boxer/serial.log"),
            disk_format="qcow2",
            emulate_nvme=True,
        )
    except ValueError:
        pass
    else:
        raise AssertionError("expected qcow2 NVMe emulation to be rejected")


def test_installer_includes_seed_cdrom() -> None:
    root = ET.fromstring(_installer_xml(seed=True))
    cdroms = [d for d in root.findall("./devices/disk") if d.get("device") == "cdrom"]
    devs = {d.find("target").get("dev") for d in cdroms}
    assert devs == {"sda", "sdb"}  # install ISO + seed ISO


def test_installer_without_seed_has_single_cdrom() -> None:
    root = ET.fromstring(_installer_xml(seed=False))
    cdroms = [d for d in root.findall("./devices/disk") if d.get("device") == "cdrom"]
    assert len(cdroms) == 1


def test_installer_headed_by_default_has_spice() -> None:
    root = ET.fromstring(_installer_xml(headless=False))
    gfx = root.find("./devices/graphics")
    assert gfx is not None
    assert gfx.get("type") == "spice"


def test_boot_to_disk_drops_cdrom_and_pins_hd() -> None:
    transformed = _xml_boot_to_disk(_installer_xml(seed=True))
    root = ET.fromstring(transformed)

    # All cdrom devices removed; only the virtio target disk remains.
    disks = root.findall("./devices/disk")
    assert all(d.get("device") == "disk" for d in disks)
    assert len(disks) == 1
    # Per-device boot orders gone; <os><boot dev='hd'/> set.
    assert disks[0].find("boot") is None
    os_boot = root.find("./os/boot")
    assert os_boot is not None and os_boot.get("dev") == "hd"
    # Reboot policy restored so the installed OS can reboot normally.
    assert root.find("on_reboot").text == "restart"


def test_define_and_start_discards_domain_when_create_fails() -> None:
    conn = MagicMock()
    dom = MagicMock()
    dom.create.side_effect = libvirt.libvirtError("create failed")
    dom.isActive.return_value = False
    conn.defineXML.return_value = dom
    ops = VMOperations(conn, BoxerConfig({}))

    try:
        ops.define_and_start_installer(
            libvirt_name="Boxer--p_x--inst--vm_1",
            vm_id="vm_1",
            cpu=2,
            ram_mb=2048,
            disk_path=Path("/var/lib/boxer/disk.qcow2"),
            install_iso=Path("/var/lib/boxer/install.iso"),
            seed_iso=None,
            net_name="boxer-net-x",
            headless=True,
            serial_log=Path("/var/lib/boxer/serial.log"),
        )
    except libvirt.libvirtError:
        pass
    else:
        raise AssertionError("expected create failure")

    dom.undefineFlags.assert_called_once()


def test_define_and_start_installer_adds_nvme_before_resume() -> None:
    conn = MagicMock()
    dom = MagicMock()
    conn.defineXML.return_value = dom
    ops = VMOperations(conn, BoxerConfig({}))
    disk_path = Path("/var/lib/boxer/disk.raw")
    qmp_commands = []

    def qmp_side_effect(_dom, raw_command: str, _flags: int) -> str:
        command = json.loads(raw_command)
        qmp_commands.append(command)
        if command["execute"] == "query-block":
            return json.dumps(
                {
                    "return": [
                        {
                            "inserted": {
                                "node-name": "libvirt-2-format",
                                "file": str(disk_path),
                                "image": {"filename": str(disk_path)},
                            }
                        }
                    ]
                }
            )
        if command["execute"] == "device_add":
            return json.dumps({"return": {}})
        raise AssertionError(command)

    with patch("boxerd.vm_ops.libvirt_qemu.qemuMonitorCommand", side_effect=qmp_side_effect):
        ops.define_and_start_installer(
            libvirt_name="Boxer--p_x--inst--vm_1",
            vm_id="vm_1",
            cpu=2,
            ram_mb=2048,
            disk_path=disk_path,
            install_iso=Path("/var/lib/boxer/install.iso"),
            seed_iso=None,
            net_name="boxer-net-x",
            headless=True,
            serial_log=Path("/var/lib/boxer/serial.log"),
            reboot_policy="restart",
            disk_format="raw",
            emulate_nvme=True,
        )

    dom.createWithFlags.assert_called_once_with(libvirt.VIR_DOMAIN_START_PAUSED)
    dom.create.assert_not_called()
    dom.resume.assert_called_once()
    device_add = [cmd for cmd in qmp_commands if cmd["execute"] == "device_add"][0]
    assert device_add["arguments"]["driver"] == "nvme"
    assert device_add["arguments"]["drive"] == "libvirt-2-format"
    assert device_add["arguments"]["bus"] == "pci.1"
    assert device_add["arguments"]["share-rw"] is True
