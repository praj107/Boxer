"""Structured, token-efficient guest introspection over the SSH channel.

This is the daemon-side baseline of the hybrid guest-utils design: curated
commands run over the existing ``GuestAgent.exec_ssh`` channel and parsed into
compact JSON, so an agent gets one structured result instead of guessing a
sequence of shell commands. When the optional in-guest ``boxerctl`` binary is
present it is preferred (one round trip, JSON straight from the guest); otherwise
the daemon falls back to family-aware shell snippets parsed here.

Parsing helpers are module-level pure functions for unit testing without a guest.
"""
from __future__ import annotations

import json
import logging
import re
import shlex
from pathlib import Path
from typing import Any, Optional

from boxer.ipc import ERR_INVALID_PARAMS, IPCError
from boxerd.guest_agent import GuestAgent

logger = logging.getLogger(__name__)

# --------------------------------------------------------------------------- shell

_SYSINFO_SH = r"""
{
. /etc/os-release 2>/dev/null || true
echo "OS_ID=${ID:-}"
echo "OS_NAME=${PRETTY_NAME:-}"
echo "OS_VERSION=${VERSION_ID:-}"
echo "OS_LIKE=${ID_LIKE:-}"
echo "KERNEL=$(uname -sr)"
echo "ARCH=$(uname -m)"
echo "NPROC=$(nproc 2>/dev/null || echo 1)"
read l1 l5 l15 rest < /proc/loadavg 2>/dev/null; echo "LOAD1=$l1"; echo "LOAD5=$l5"; echo "LOAD15=$l15"
awk '/^MemTotal:/{print "MEM_TOTAL_KB="$2} /^MemAvailable:/{print "MEM_AVAIL_KB="$2}' /proc/meminfo 2>/dev/null
echo "UPTIME_SEC=$(cut -d. -f1 /proc/uptime 2>/dev/null)"
echo "VIRT=$(systemd-detect-virt 2>/dev/null || echo unknown)"
df -Pk / 2>/dev/null | awk 'NR==2{print "DISK_TOTAL_KB="$2; print "DISK_USED_KB="$3; print "DISK_AVAIL_KB="$4; print "DISK_PCT="$5; print "DISK_MOUNT="$6}'
}
"""

_SERVICES_SH = r"""
if command -v systemctl >/dev/null 2>&1; then
  echo "@@INIT systemd"
  echo "@@RUNNING"
  systemctl list-units --type=service --state=running --no-legend --no-pager 2>/dev/null | awk '{print $1}'
  echo "@@FAILED"
  systemctl list-units --type=service --state=failed --no-legend --no-pager 2>/dev/null | awk '{print $1}'
elif command -v rc-status >/dev/null 2>&1; then
  echo "@@INIT openrc"
  echo "@@RUNNING"
  rc-status -s 2>/dev/null | awk '/started/{print $1}'
  echo "@@FAILED"
  rc-status -s 2>/dev/null | awk '/crashed|stopped/{print $1}'
else
  echo "@@INIT unknown"
  echo "@@RUNNING"
  echo "@@FAILED"
fi
echo "@@PORTS"
( ss -tlnH 2>/dev/null || ss -tln 2>/dev/null || netstat -tln 2>/dev/null ) | awk '{print $4}' | grep -E ':[0-9]+$' | sed 's/.*://' | sort -un
echo "@@PROCS"
ps -eo pid,pcpu,pmem,comm --sort=-pcpu 2>/dev/null | head -n 7
"""

_LOGS_SH_TEMPLATE = r"""
echo "@@FAILED_UNITS"
( systemctl list-units --type=service --state=failed --no-legend --no-pager 2>/dev/null | awk '{print $1}' ) || true
echo "@@JOURNAL"
( journalctl -n {n} --no-pager -p warning 2>/dev/null || tail -n {n} /var/log/messages 2>/dev/null || tail -n {n} /var/log/syslog 2>/dev/null ) || true
echo "@@DMESG"
( dmesg --level=err,warn 2>/dev/null | tail -n {n} || dmesg 2>/dev/null | tail -n {n} ) || true
"""

# pkg manager probe shared by query/search/install
_PKG_DETECT = (
    "if command -v apt-get >/dev/null 2>&1; then echo apt; "
    "elif command -v dnf >/dev/null 2>&1; then echo dnf; "
    "elif command -v apk >/dev/null 2>&1; then echo apk; "
    "elif command -v zypper >/dev/null 2>&1; then echo zypper; "
    "elif command -v pacman >/dev/null 2>&1; then echo pacman; "
    "else echo unknown; fi"
)

_VALID_PKG = re.compile(r"^[A-Za-z0-9][A-Za-z0-9+._-]{0,100}$")


def _boxerctl_or(fallback: str, subcommand: str) -> str:
    """Prefer the in-guest boxerctl agent (JSON), else run the fallback script."""
    return (
        f"if command -v boxerctl >/dev/null 2>&1; then boxerctl {subcommand} --json; "
        f"else\n{fallback}\nfi"
    )


# --------------------------------------------------------------------------- parsers


def parse_kv(raw: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for line in raw.splitlines():
        if "=" in line:
            k, _, v = line.partition("=")
            out[k.strip()] = v.strip()
    return out


def _int(d: dict[str, str], key: str) -> Optional[int]:
    try:
        return int(d[key])
    except (KeyError, ValueError):
        return None


def parse_sysinfo(raw: str) -> dict[str, Any]:
    kv = parse_kv(raw)
    total_kb = _int(kv, "MEM_TOTAL_KB")
    avail_kb = _int(kv, "MEM_AVAIL_KB")
    used_mb = None
    if total_kb is not None and avail_kb is not None:
        used_mb = (total_kb - avail_kb) // 1024
    return {
        "os": {
            "id": kv.get("OS_ID") or None,
            "name": kv.get("OS_NAME") or None,
            "version": kv.get("OS_VERSION") or None,
            "like": kv.get("OS_LIKE") or None,
        },
        "kernel": kv.get("KERNEL") or None,
        "arch": kv.get("ARCH") or None,
        "virtualization": kv.get("VIRT") or None,
        "cpu": {
            "count": _int(kv, "NPROC"),
            "load": [kv.get("LOAD1"), kv.get("LOAD5"), kv.get("LOAD15")],
        },
        "memory": {
            "total_mb": (total_kb // 1024) if total_kb is not None else None,
            "available_mb": (avail_kb // 1024) if avail_kb is not None else None,
            "used_mb": used_mb,
        },
        "disk_root": {
            "total_kb": _int(kv, "DISK_TOTAL_KB"),
            "used_kb": _int(kv, "DISK_USED_KB"),
            "available_kb": _int(kv, "DISK_AVAIL_KB"),
            "used_pct": kv.get("DISK_PCT"),
            "mount": kv.get("DISK_MOUNT"),
        },
        "uptime_seconds": _int(kv, "UPTIME_SEC"),
    }


def _split_sections(raw: str) -> dict[str, list[str]]:
    sections: dict[str, list[str]] = {}
    current: Optional[str] = None
    inline: dict[str, str] = {}
    for line in raw.splitlines():
        if line.startswith("@@"):
            parts = line[2:].split(None, 1)
            name = parts[0]
            if len(parts) > 1:
                inline[name] = parts[1].strip()
                current = None
            else:
                current = name
                sections[name] = []
            continue
        if current is not None and line.strip():
            sections[current].append(line.rstrip())
    sections["_inline"] = [f"{k}={v}" for k, v in inline.items()]
    return sections


def parse_services(raw: str) -> dict[str, Any]:
    sec = _split_sections(raw)
    inline = parse_kv("\n".join(sec.get("_inline", [])))
    ports = []
    for p in sec.get("PORTS", []):
        p = p.strip()
        if p.isdigit():
            ports.append(int(p))
    procs = []
    for line in sec.get("PROCS", []):
        fields = line.split(None, 3)
        if len(fields) == 4 and fields[0].isdigit():
            procs.append(
                {"pid": int(fields[0]), "cpu": fields[1], "mem": fields[2], "command": fields[3]}
            )
    return {
        "init": inline.get("INIT", "unknown"),
        "running": [s for s in sec.get("RUNNING", []) if s.strip()],
        "failed": [s for s in sec.get("FAILED", []) if s.strip()],
        "listening_ports": sorted(set(ports)),
        "top_processes": procs,
    }


def parse_logs(raw: str) -> dict[str, Any]:
    sec = _split_sections(raw)
    return {
        "failed_units": [s for s in sec.get("FAILED_UNITS", []) if s.strip()],
        "journal": sec.get("JOURNAL", []),
        "dmesg": sec.get("DMESG", []),
    }


# --------------------------------------------------------------------------- class


class GuestUtils:
    def __init__(self, guest: GuestAgent):
        self._guest = guest

    async def _run(self, ip: str, key: Optional[Path], command: str, timeout: int = 30) -> str:
        res = await self._guest.exec_ssh(ip, command, timeout, fallback_privkey_path=key)
        return res.get("stdout") or ""

    @staticmethod
    def _maybe_json(raw: str) -> Optional[dict[str, Any]]:
        s = raw.strip()
        if s.startswith("{"):
            try:
                return json.loads(s)
            except json.JSONDecodeError:
                return None
        return None

    async def sysinfo(self, ip: str, key: Optional[Path]) -> dict[str, Any]:
        raw = await self._run(ip, key, _boxerctl_or(_SYSINFO_SH, "sysinfo"))
        return self._maybe_json(raw) or parse_sysinfo(raw)

    async def services(self, ip: str, key: Optional[Path]) -> dict[str, Any]:
        raw = await self._run(ip, key, _boxerctl_or(_SERVICES_SH, "services"))
        return self._maybe_json(raw) or parse_services(raw)

    async def logs(self, ip: str, key: Optional[Path], lines: int = 50) -> dict[str, Any]:
        lines = max(1, min(int(lines), 500))
        fallback = _LOGS_SH_TEMPLATE.replace("{n}", str(lines))
        raw = await self._run(ip, key, _boxerctl_or(fallback, f"logs --lines {lines}"))
        return self._maybe_json(raw) or parse_logs(raw)

    async def packages(
        self,
        ip: str,
        key: Optional[Path],
        action: str = "query",
        name: Optional[str] = None,
    ) -> dict[str, Any]:
        if action not in ("query", "search", "install"):
            raise IPCError(ERR_INVALID_PARAMS, "action must be query, search, or install")
        if action in ("search", "install"):
            if not name or not _VALID_PKG.match(name):
                raise IPCError(ERR_INVALID_PARAMS, "a valid package name is required for search/install")
        mgr = (await self._run(ip, key, _PKG_DETECT, timeout=15)).strip().splitlines()
        manager = mgr[-1].strip() if mgr else "unknown"
        if manager == "unknown":
            raise IPCError(ERR_INVALID_PARAMS, "No supported package manager detected in guest")

        quoted = shlex.quote(name) if name else ""
        if action == "query":
            cmd = {
                "apt": "dpkg-query -W -f='${Package}\\n' 2>/dev/null | wc -l; dpkg-query -W -f='${Package}\\n' 2>/dev/null | head -n 50",
                "dnf": "rpm -qa 2>/dev/null | wc -l; rpm -qa --qf '%{NAME}\\n' 2>/dev/null | sort | head -n 50",
                "apk": "apk info 2>/dev/null | wc -l; apk info 2>/dev/null | head -n 50",
                "zypper": "rpm -qa 2>/dev/null | wc -l; rpm -qa --qf '%{NAME}\\n' 2>/dev/null | sort | head -n 50",
                "pacman": "pacman -Qq 2>/dev/null | wc -l; pacman -Qq 2>/dev/null | head -n 50",
            }[manager]
            raw = await self._run(ip, key, cmd, timeout=30)
            out = raw.splitlines()
            count = int(out[0]) if out and out[0].strip().isdigit() else None
            sample = [l.strip() for l in out[1:] if l.strip()]
            return {"manager": manager, "installed_count": count, "sample": sample}

        if action == "search":
            cmd = {
                "apt": f"apt-cache search {quoted} 2>/dev/null | head -n 30",
                "dnf": f"dnf -q search {quoted} 2>/dev/null | head -n 30",
                "apk": f"apk search {quoted} 2>/dev/null | head -n 30",
                "zypper": f"zypper -q search {quoted} 2>/dev/null | head -n 30",
                "pacman": f"pacman -Ss {quoted} 2>/dev/null | head -n 30",
            }[manager]
            raw = await self._run(ip, key, cmd, timeout=60)
            return {"manager": manager, "query": name, "results": [l for l in raw.splitlines() if l.strip()]}

        # install (mutating; policy-gated upstream)
        cmd = {
            "apt": f"sudo DEBIAN_FRONTEND=noninteractive apt-get install -y {quoted}",
            "dnf": f"sudo dnf install -y {quoted}",
            "apk": f"sudo apk add {quoted}",
            "zypper": f"sudo zypper --non-interactive install {quoted}",
            "pacman": f"sudo pacman -S --noconfirm {quoted}",
        }[manager]
        res = await self._guest.exec_ssh(ip, cmd, 600, fallback_privkey_path=key)
        return {
            "manager": manager,
            "package": name,
            "exit_code": res.get("exit_code", 1),
            "installed": res.get("exit_code", 1) == 0,
            "output_tail": (res.get("stdout") or "").splitlines()[-15:],
            "error_tail": (res.get("stderr") or "").splitlines()[-10:],
        }
