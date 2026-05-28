"""Tests for boxerd/reconcile.py and the DB origin/ghost helpers."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import pytest

from boxer.types import VMRecord
from boxerd.db import Database
from boxerd.reconcile import (
    ForeignDomain,
    GhostRecord,
    OrphanedBoxerDomain,
    _extract_disk_path,
    _get_disk_size_gb,
    build_reconcile_report,
    parse_boxer_name,
)


# ── helpers ────────────────────────────────────────────────────────────────────

def _make_vm(
    vm_id: str,
    libvirt_name: str,
    project_id: str = "p_aaa111",
    origin: str = "boxer",
) -> VMRecord:
    now = datetime.now(timezone.utc)
    return VMRecord(
        id=vm_id,
        libvirt_name=libvirt_name,
        project_id=project_id,
        display_name="test",
        state="running",
        owner_user="alice",
        template="ubuntu-24.04",
        cpu=2,
        ram_mb=2048,
        disk_gb=20,
        headless=True,
        created_at=now,
        last_touched=now,
        lease_until=now + timedelta(hours=1),
        origin=origin,
    )


def _make_domain_mock(name: str, xml: str, is_active: bool = False) -> MagicMock:
    dom = MagicMock()
    dom.name.return_value = name
    dom.XMLDesc.return_value = xml
    dom.isActive.return_value = 1 if is_active else 0
    return dom


def _disk_xml(path: str = "/var/lib/boxer/disk.qcow2") -> str:
    return f"""<domain>
      <devices>
        <disk type='file' device='disk'>
          <source file='{path}'/>
          <target dev='vda' bus='virtio'/>
        </disk>
      </devices>
    </domain>"""


# ── parse_boxer_name ───────────────────────────────────────────────────────────

def test_parse_valid_boxer_name() -> None:
    result = parse_boxer_name("Boxer--p_abc123--myvm--vm_def456")
    assert result == ("p_abc123", "myvm", "vm_def456")


def test_parse_boxer_name_display_contains_dashes() -> None:
    result = parse_boxer_name("Boxer--p_abc123--my-long-name--vm_abc123")
    assert result is not None
    assert result[1] == "my-long-name"


def test_parse_boxer_name_no_prefix_returns_none() -> None:
    assert parse_boxer_name("ubuntu-server") is None


def test_parse_boxer_name_too_few_parts_returns_none() -> None:
    assert parse_boxer_name("Boxer--p_abc123--vm_def456") is None


def test_parse_boxer_name_bad_vm_id_returns_none() -> None:
    assert parse_boxer_name("Boxer--p_abc--myvm--notavm") is None


# ── _extract_disk_path ─────────────────────────────────────────────────────────

def test_extract_disk_path_file_disk() -> None:
    xml = _disk_xml("/data/disk.qcow2")
    assert _extract_disk_path(xml) == "/data/disk.qcow2"


def test_extract_disk_path_block_device() -> None:
    xml = """<domain><devices>
      <disk type='block' device='disk'><source dev='/dev/vg0/lv-myvm'/></disk>
    </devices></domain>"""
    assert _extract_disk_path(xml) == "/dev/vg0/lv-myvm"


def test_extract_disk_path_cdrom_is_skipped() -> None:
    xml = """<domain><devices>
      <disk type='file' device='cdrom'><source file='/tmp/cloud-init.iso'/></disk>
      <disk type='file' device='disk'><source file='/data/disk.qcow2'/></disk>
    </devices></domain>"""
    assert _extract_disk_path(xml) == "/data/disk.qcow2"


def test_extract_disk_path_no_disk_returns_none() -> None:
    assert _extract_disk_path("<domain><devices></devices></domain>") is None


def test_extract_disk_path_malformed_xml_returns_none() -> None:
    assert _extract_disk_path("not xml at all") is None


# ── _get_disk_size_gb ──────────────────────────────────────────────────────────

def test_get_disk_size_gb_parses_virtual_size() -> None:
    fake_output = '{"virtual-size": 21474836480}'  # 20 GiB
    with patch("subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=0, stdout=fake_output)
        assert _get_disk_size_gb("/fake/path.qcow2") == 20


def test_get_disk_size_gb_returns_zero_on_failure() -> None:
    with patch("subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=1, stdout="")
        assert _get_disk_size_gb("/fake/path.qcow2") == 0


def test_get_disk_size_gb_returns_zero_on_exception() -> None:
    with patch("subprocess.run", side_effect=FileNotFoundError("qemu-img not found")):
        assert _get_disk_size_gb("/fake/path.qcow2") == 0


# ── build_reconcile_report ─────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_all_accounted_for(db: Database) -> None:
    """DB and libvirt in sync → empty report."""
    libvirt_name = "Boxer--p_aaa111--myvm--vm_abc123"
    await db.upsert_project("p_aaa111", "/a")
    await db.insert_vm(_make_vm("vm_abc123", libvirt_name))

    conn = MagicMock()
    conn.listAllDomains.return_value = [_make_domain_mock(libvirt_name, _disk_xml())]

    with patch("boxerd.reconcile._get_disk_size_gb", return_value=20):
        report = await build_reconcile_report(conn, db)

    assert report.orphaned_boxer == []
    assert report.foreign_vms == []
    assert report.ghost_records == []


@pytest.mark.asyncio
async def test_detects_orphaned_boxer_domain(db: Database) -> None:
    """Boxer-- domain in libvirt but not in DB → orphaned_boxer."""
    conn = MagicMock()
    conn.listAllDomains.return_value = [
        _make_domain_mock("Boxer--p_abc123--myvm--vm_def456", _disk_xml())
    ]

    with patch("boxerd.reconcile._get_disk_size_gb", return_value=20):
        report = await build_reconcile_report(conn, db)

    assert len(report.orphaned_boxer) == 1
    o = report.orphaned_boxer[0]
    assert o.libvirt_name == "Boxer--p_abc123--myvm--vm_def456"
    assert o.parsed_project_id == "p_abc123"
    assert o.parsed_vm_id == "vm_def456"
    assert o.suggested_action == "adopt"
    assert report.foreign_vms == []
    assert report.ghost_records == []


@pytest.mark.asyncio
async def test_detects_foreign_domain(db: Database) -> None:
    """Non-Boxer domain in libvirt but not in DB → foreign_vms."""
    conn = MagicMock()
    conn.listAllDomains.return_value = [
        _make_domain_mock("ubuntu-server-01", _disk_xml(), is_active=True)
    ]

    with patch("boxerd.reconcile._get_disk_size_gb", return_value=50):
        report = await build_reconcile_report(conn, db)

    assert len(report.foreign_vms) == 1
    f = report.foreign_vms[0]
    assert f.libvirt_name == "ubuntu-server-01"
    assert f.is_active is True
    assert f.suggested_action == "import"
    assert report.orphaned_boxer == []
    assert report.ghost_records == []


@pytest.mark.asyncio
async def test_detects_ghost_db_record(db: Database) -> None:
    """DB record with no corresponding libvirt domain → ghost_records."""
    await db.upsert_project("p_aaa111", "/a")
    await db.insert_vm(_make_vm("vm_ghost01", "Boxer--p_aaa111--test--vm_ghost01"))

    conn = MagicMock()
    conn.listAllDomains.return_value = []  # libvirt is empty

    report = await build_reconcile_report(conn, db)

    assert len(report.ghost_records) == 1
    g = report.ghost_records[0]
    assert g.vm_id == "vm_ghost01"
    assert g.suggested_action == "purge"
    assert report.orphaned_boxer == []
    assert report.foreign_vms == []


@pytest.mark.asyncio
async def test_mixed_report(db: Database) -> None:
    """Healthy + orphan + foreign + ghost all detected correctly."""
    await db.upsert_project("p_aaa111", "/a")
    healthy_name = "Boxer--p_aaa111--healthy--vm_healthy1"
    ghost_name = "Boxer--p_aaa111--ghost--vm_ghost01"
    await db.insert_vm(_make_vm("vm_healthy1", healthy_name))
    await db.insert_vm(_make_vm("vm_ghost01", ghost_name))

    conn = MagicMock()
    conn.listAllDomains.return_value = [
        _make_domain_mock(healthy_name, _disk_xml()),
        _make_domain_mock("Boxer--p_xyz--orphan--vm_orphan1", _disk_xml()),
        _make_domain_mock("foreign-vm", _disk_xml(), is_active=True),
    ]

    with patch("boxerd.reconcile._get_disk_size_gb", return_value=20):
        report = await build_reconcile_report(conn, db)

    assert len(report.orphaned_boxer) == 1
    assert report.orphaned_boxer[0].libvirt_name == "Boxer--p_xyz--orphan--vm_orphan1"
    assert len(report.foreign_vms) == 1
    assert report.foreign_vms[0].libvirt_name == "foreign-vm"
    assert len(report.ghost_records) == 1
    assert report.ghost_records[0].vm_id == "vm_ghost01"


# ── DB: origin field round-trips ───────────────────────────────────────────────

@pytest.mark.asyncio
async def test_origin_defaults_to_boxer(db: Database) -> None:
    await db.upsert_project("p_aaa111", "/a")
    vm = _make_vm("vm_orig01", "Boxer--p_aaa111--t--vm_orig01")
    await db.insert_vm(vm)
    fetched = await db.get_vm("vm_orig01")
    assert fetched is not None
    assert fetched.origin == "boxer"


@pytest.mark.asyncio
async def test_origin_imported_round_trips(db: Database) -> None:
    await db.upsert_project("p_aaa111", "/a")
    vm = _make_vm("vm_imp01", "foreign-vm", origin="imported")
    await db.insert_vm(vm)
    fetched = await db.get_vm("vm_imp01")
    assert fetched is not None
    assert fetched.origin == "imported"


@pytest.mark.asyncio
async def test_origin_adopted_round_trips(db: Database) -> None:
    await db.upsert_project("p_aaa111", "/a")
    vm = _make_vm("vm_adp01", "Boxer--p_aaa111--t--vm_adp01", origin="adopted")
    await db.insert_vm(vm)
    fetched = await db.get_vm("vm_adp01")
    assert fetched is not None
    assert fetched.origin == "adopted"


# ── DB: get_vm_by_libvirt_name ─────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_get_vm_by_libvirt_name_found(db: Database) -> None:
    await db.upsert_project("p_aaa111", "/a")
    vm = _make_vm("vm_lv01", "Boxer--p_aaa111--t--vm_lv01")
    await db.insert_vm(vm)
    result = await db.get_vm_by_libvirt_name("Boxer--p_aaa111--t--vm_lv01")
    assert result is not None
    assert result.id == "vm_lv01"


@pytest.mark.asyncio
async def test_get_vm_by_libvirt_name_not_found(db: Database) -> None:
    result = await db.get_vm_by_libvirt_name("nonexistent-domain")
    assert result is None


# ── DB: purge_ghost_record ─────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_purge_ghost_record_removes_row(db: Database) -> None:
    await db.upsert_project("p_aaa111", "/a")
    vm = _make_vm("vm_purge01", "Boxer--p_aaa111--t--vm_purge01")
    await db.insert_vm(vm)
    await db.purge_ghost_record("vm_purge01")
    assert await db.get_vm("vm_purge01") is None


@pytest.mark.asyncio
async def test_purge_ghost_record_nonexistent_is_noop(db: Database) -> None:
    # Should not raise even if the ID doesn't exist
    await db.purge_ghost_record("vm_does_not_exist")
