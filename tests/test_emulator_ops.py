"""Tests for emulator port allocation and AVD name validation (no emulator)."""
from __future__ import annotations

import pytest

from boxer.ipc import IPCError
from boxerd.emulator_ops import allocate_console_port, serial_for_port, validate_avd_name


def test_allocate_first_even_port():
    assert allocate_console_port(set()) == 5554


def test_allocate_skips_used_pairs():
    # 5554/5555 used -> next pair 5556/5557
    assert allocate_console_port({5554, 5555}) == 5556


def test_allocate_skips_when_adb_port_taken():
    # 5555 is the adb port of the 5554 pair, so that pair is unusable -> 5556.
    assert allocate_console_port({5555}) == 5556


def test_allocate_exhausted_raises():
    # Every console port in the range is taken -> no free pair.
    with pytest.raises(IPCError):
        allocate_console_port({5554, 5556}, lo=5554, hi=5556)


def test_serial_for_port():
    assert serial_for_port(5554) == "emulator-5554"


def test_validate_avd_name_ok():
    assert validate_avd_name("boxer_vm_abc123") == "boxer_vm_abc123"


@pytest.mark.parametrize("bad", ["", "has space", "x" * 65, "semi;colon", "slash/name"])
def test_validate_avd_name_rejects(bad):
    with pytest.raises(IPCError):
        validate_avd_name(bad)
