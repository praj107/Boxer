"""Tests for guest_utils parsers and the boxerctl-preferred dispatch."""
from __future__ import annotations

import pytest

from boxer.ipc import IPCError
from boxerd.guest_utils import (
    GuestUtils,
    parse_logs,
    parse_services,
    parse_sysinfo,
)


def test_parse_sysinfo_full():
    raw = (
        "OS_ID=ubuntu\nOS_NAME=Ubuntu 24.04\nOS_VERSION=24.04\nOS_LIKE=debian\n"
        "KERNEL=Linux 6.8.0\nARCH=x86_64\nNPROC=4\n"
        "LOAD1=0.10\nLOAD5=0.20\nLOAD15=0.30\n"
        "MEM_TOTAL_KB=4096000\nMEM_AVAIL_KB=1024000\n"
        "UPTIME_SEC=12345\nVIRT=kvm\n"
        "DISK_TOTAL_KB=20000000\nDISK_USED_KB=5000000\nDISK_AVAIL_KB=15000000\n"
        "DISK_PCT=25%\nDISK_MOUNT=/\n"
    )
    s = parse_sysinfo(raw)
    assert s["os"]["id"] == "ubuntu"
    assert s["os"]["like"] == "debian"
    assert s["kernel"] == "Linux 6.8.0"
    assert s["cpu"]["count"] == 4
    assert s["cpu"]["load"] == ["0.10", "0.20", "0.30"]
    assert s["memory"]["total_mb"] == 4000
    assert s["memory"]["available_mb"] == 1000
    assert s["memory"]["used_mb"] == 3000
    assert s["disk_root"]["used_pct"] == "25%"
    assert s["uptime_seconds"] == 12345


def test_parse_sysinfo_missing_fields():
    s = parse_sysinfo("OS_ID=alpine\n")
    assert s["os"]["id"] == "alpine"
    assert s["memory"]["total_mb"] is None
    assert s["cpu"]["count"] is None


def test_parse_services_systemd():
    raw = (
        "@@INIT systemd\n"
        "@@RUNNING\nsshd.service\ncron.service\n"
        "@@FAILED\nfoo.service\n"
        "@@PORTS\n22\n80\n22\n"
        "@@PROCS\n"
        "  PID %CPU %MEM COMMAND\n"
        "  1 0.1 0.5 systemd\n"
        "  42 2.0 1.0 sshd\n"
    )
    s = parse_services(raw)
    assert s["init"] == "systemd"
    assert s["running"] == ["sshd.service", "cron.service"]
    assert s["failed"] == ["foo.service"]
    assert s["listening_ports"] == [22, 80]
    assert {"pid": 42, "cpu": "2.0", "mem": "1.0", "command": "sshd"} in s["top_processes"]


def test_parse_logs_sections():
    raw = (
        "@@FAILED_UNITS\nbar.service\n"
        "@@JOURNAL\nwarn: something\nerr: other\n"
        "@@DMESG\n[1.0] usb disconnect\n"
    )
    logs = parse_logs(raw)
    assert logs["failed_units"] == ["bar.service"]
    assert logs["journal"] == ["warn: something", "err: other"]
    assert logs["dmesg"] == ["[1.0] usb disconnect"]


class _FakeGuest:
    def __init__(self, stdout: str):
        self._stdout = stdout
        self.last_command = None

    async def exec_ssh(self, ip, command, timeout, fallback_privkey_path=None):
        self.last_command = command
        return {"stdout": self._stdout, "stderr": "", "exit_code": 0}


@pytest.mark.asyncio
async def test_sysinfo_prefers_boxerctl_json():
    # When the guest returns JSON (boxerctl), it is used verbatim.
    guest = _FakeGuest('{"os": {"id": "fedora"}, "kernel": "Linux"}')
    data = await GuestUtils(guest).sysinfo("1.2.3.4", None)
    assert data == {"os": {"id": "fedora"}, "kernel": "Linux"}
    assert "boxerctl sysinfo --json" in guest.last_command


@pytest.mark.asyncio
async def test_sysinfo_falls_back_to_shell_parse():
    guest = _FakeGuest("OS_ID=debian\nNPROC=2\n")
    data = await GuestUtils(guest).sysinfo("1.2.3.4", None)
    assert data["os"]["id"] == "debian"
    assert data["cpu"]["count"] == 2


@pytest.mark.asyncio
async def test_packages_rejects_bad_name():
    guest = _FakeGuest("apt\n")
    with pytest.raises(IPCError):
        await GuestUtils(guest).packages("1.2.3.4", None, action="install", name="bad;name")
