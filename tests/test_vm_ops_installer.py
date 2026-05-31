"""Tests for installer domain XML generation and boot-order flip."""
from __future__ import annotations

import xml.etree.ElementTree as ET
from pathlib import Path

from boxerd.vm_ops import _domain_xml, _installer_domain_xml, _xml_boot_to_disk


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
    # graphics must be a child of <devices>, not a stray child of <domain>.
    assert root.find("./devices/graphics") is not None
    assert root.find("./graphics") is None


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
    disks = root.findall("./devices/disk")
    by_dev = {d.find("target").get("dev"): d for d in disks}

    # Install ISO (sda, cdrom) boots first; target disk (vda) second.
    assert by_dev["sda"].get("device") == "cdrom"
    assert by_dev["sda"].find("boot").get("order") == "1"
    assert by_dev["vda"].get("device") == "disk"
    assert by_dev["vda"].find("boot").get("order") == "2"
    # No <os><boot> when per-device boot order is used.
    assert root.find("./os/boot") is None


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
