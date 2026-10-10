"""Android catalog, device-kind persistence, and boxerctl injection tests."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from boxer.config import get_android_catalog
from boxer.types import VMRecord
from boxerd.agent_inject import BOXERCTL_PROFILE, boxerctl_binary_path, boxerctl_injection
from boxerd.db import Database
from boxerd.profiles import available_profiles


def test_android_catalog_has_seed_variants():
    cat = get_android_catalog()
    names = cat.list_names()
    assert any("android-35" in n for n in names)
    assert any("aosp_atd" in n for n in names)
    assert any("arm64-v8a" in n for n in names)
    # Playstore variant is intentionally excluded (blocks adb root).
    assert not any("playstore" in n for n in names)
    entry = cat.get(names[0])
    assert entry["package"].startswith("system-images;")
    assert "api" in entry and "abi" in entry


def test_android_catalog_entries_carry_name():
    for e in get_android_catalog().list_entries():
        assert "name" in e and "package" in e


@pytest.mark.asyncio
async def test_vm_kind_roundtrip(db: Database) -> None:
    await db.upsert_project("p_aaa111", "/home/alice/proj")
    now = datetime.now(timezone.utc)
    vm = VMRecord(
        id="vm_and001",
        libvirt_name="boxer_vm_and001",
        project_id="p_aaa111",
        display_name="droid",
        state="running",
        owner_user="alice",
        template="android-35-google_apis-x86_64",
        cpu=4,
        ram_mb=3072,
        disk_gb=6,
        headless=True,
        created_at=now,
        last_touched=now,
        lease_until=now + timedelta(hours=1),
        ip_address="emulator-5554",
        tags={"boxer.android.adb_serial": "emulator-5554"},
        artifact_type="android-emulator",
        kind="android",
    )
    await db.insert_vm(vm)
    got = await db.get_vm("vm_and001")
    assert got is not None
    assert got.kind == "android"
    assert got.artifact_type == "android-emulator"
    assert got.tags["boxer.android.adb_serial"] == "emulator-5554"
    # Default kind remains 'vm' for normal records.
    assert VMRecord.__dataclass_fields__["kind"].default == "vm"


def test_boxerctl_profile_discoverable():
    names = [p["name"] for p in available_profiles()]
    assert BOXERCTL_PROFILE in names


def test_boxerctl_injection_noop_without_binary():
    # No prebuilt binary in agent/dist by default -> graceful no-op.
    if boxerctl_binary_path("amd64") is None:
        assert boxerctl_injection("amd64") is None
