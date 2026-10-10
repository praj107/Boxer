"""Host-direct Android emulator lifecycle (the 'emulator' device backend).

Mirrors the subset of the ``VMOperations`` lifecycle surface that the daemon and
lease manager dispatch on (``get_state`` / ``start`` / ``stop`` / ``undefine``),
selected by ``VMRecord.kind == 'android'``, plus AVD creation and boot waiting.

Emulators are supervised as host processes, not libvirt domains. AVDs persist on
disk (so ``start`` can relaunch from the AVD definition), and running instances
are discovered by scanning ``/proc`` for the ``-avd <name>`` command line, which
keeps state correct across daemon restarts.
"""
from __future__ import annotations

import logging
import os
import re
import signal
import subprocess
import time
from pathlib import Path
from typing import Optional

from boxer.config import BoxerConfig, get_config
from boxer.ipc import ERR_INTERNAL, ERR_INVALID_PARAMS, IPCError
from boxerd.android_sdk import AndroidSDK

logger = logging.getLogger(__name__)

_AVD_NAME_RE = re.compile(r"^[A-Za-z0-9._-]{1,64}$")


def allocate_console_port(in_use: set[int], lo: int = 5554, hi: int = 5680) -> int:
    """Return a free even console port in [lo, hi]; adb port is always port+1."""
    port = lo if lo % 2 == 0 else lo + 1
    while port <= hi:
        if port not in in_use and (port + 1) not in in_use:
            return port
        port += 2
    raise IPCError(ERR_INTERNAL, "No free emulator console ports available")


def serial_for_port(console_port: int) -> str:
    return f"emulator-{console_port}"


def validate_avd_name(name: str) -> str:
    if not isinstance(name, str) or not _AVD_NAME_RE.match(name):
        raise IPCError(ERR_INVALID_PARAMS, "Invalid AVD name (allowed: letters, digits, . _ -, max 64)")
    return name


class EmulatorOperations:
    def __init__(self, sdk: AndroidSDK, cfg: Optional[BoxerConfig] = None):
        self._sdk = sdk
        self._cfg = cfg or get_config()

    # ------------------------------------------------------------ process discovery

    @staticmethod
    def _proc_cmdlines() -> list[tuple[int, str]]:
        """Return (pid, cmdline) for all processes (Linux /proc)."""
        out: list[tuple[int, str]] = []
        proc = Path("/proc")
        if not proc.exists():
            return out
        for entry in proc.iterdir():
            if not entry.name.isdigit():
                continue
            try:
                raw = (entry / "cmdline").read_bytes()
            except OSError:
                continue
            cmd = raw.replace(b"\x00", b" ").decode(errors="replace")
            out.append((int(entry.name), cmd))
        return out

    def _pids_for_avd(self, avd_name: str) -> list[int]:
        token = f"-avd {avd_name} "
        pids = []
        for pid, cmd in self._proc_cmdlines():
            if "emulator" in cmd and (token in cmd + " " or cmd.endswith(f"-avd {avd_name}")):
                pids.append(pid)
        return pids

    def _ports_in_use(self) -> set[int]:
        ports: set[int] = set()
        for _, cmd in self._proc_cmdlines():
            if "emulator" not in cmd:
                continue
            m = re.search(r"-ports\s+(\d+),(\d+)", cmd)
            if m:
                ports.add(int(m.group(1)))
                ports.add(int(m.group(2)))
        return ports

    # ------------------------------------------------------------ lifecycle (dispatch surface)

    def get_state(self, avd_name: str) -> str:
        """running | stopped — duck-typed to match VMOperations.get_state."""
        return "running" if self._pids_for_avd(avd_name) else "stopped"

    def start(self, avd_name: str) -> None:
        """Relaunch an existing AVD from its on-disk definition."""
        if self._pids_for_avd(avd_name):
            return
        self.launch(avd_name)

    def stop(self, avd_name: str, graceful: bool = True) -> None:
        pids = self._pids_for_avd(avd_name)
        if not pids:
            return
        adb = self._sdk.adb()
        if graceful and adb is not None:
            # Best-effort clean shutdown via the emulator console.
            for port in self._ports_in_use():
                try:
                    subprocess.run(
                        [str(adb), "-s", serial_for_port(port), "emu", "kill"],
                        capture_output=True,
                        timeout=15,
                    )
                except Exception:
                    pass
        for pid in self._pids_for_avd(avd_name):
            try:
                os.kill(pid, signal.SIGTERM if graceful else signal.SIGKILL)
            except OSError:
                pass

    def undefine(self, avd_name: str) -> None:
        """Kill any running instance and delete the AVD definition."""
        self.stop(avd_name, graceful=False)
        avdmanager = self._sdk.avdmanager()
        if avdmanager is None:
            return
        try:
            subprocess.run(
                [str(avdmanager), "delete", "avd", "-n", avd_name],
                capture_output=True,
                text=True,
                timeout=120,
                env=self._sdk._sdk_env(),
            )
        except Exception as exc:
            logger.debug("avdmanager delete failed for %s: %s", avd_name, exc)

    # ------------------------------------------------------------ creation

    def create_avd(
        self,
        avd_name: str,
        package_spec: str,
        *,
        device: str = "pixel_6",
        data_gb: int = 6,
    ) -> None:
        avdmanager = self._sdk.avdmanager()
        if avdmanager is None:
            raise IPCError(ERR_INTERNAL, "avdmanager not found")
        cmd = [
            str(avdmanager),
            "create",
            "avd",
            "-n",
            avd_name,
            "-k",
            package_spec,
            "-d",
            device,
            "--force",
        ]
        result = subprocess.run(
            cmd,
            input="no\n",  # decline the custom-hardware-profile prompt
            capture_output=True,
            text=True,
            timeout=300,
            env=self._sdk._sdk_env(),
        )
        if result.returncode != 0:
            raise IPCError(
                ERR_INTERNAL,
                f"avdmanager create failed for '{avd_name}': "
                f"{(result.stderr or result.stdout or '').strip()[:500]}",
            )

    def launch(
        self,
        avd_name: str,
        *,
        ram_mb: Optional[int] = None,
        cores: Optional[int] = None,
    ) -> dict[str, object]:
        """Spawn a headless emulator process; return serial + ports + pid."""
        emulator = self._sdk.emulator()
        if emulator is None:
            raise IPCError(ERR_INTERNAL, "emulator not found")
        console_port = allocate_console_port(
            self._ports_in_use(),
            self._cfg.emulator_console_port_min,
            self._cfg.emulator_console_port_max,
        )
        adb_port = console_port + 1
        cmd = [
            str(emulator),
            "-avd",
            avd_name,
            "-ports",
            f"{console_port},{adb_port}",
            "-no-window",
            "-no-audio",
            "-no-boot-anim",
            "-no-snapshot",
            "-gpu",
            "swiftshader_indirect",
            "-accel",
            "on",
        ]
        if ram_mb:
            cmd += ["-memory", str(ram_mb)]
        if cores:
            cmd += ["-cores", str(cores)]
        log_dir = self._cfg.state_dir / "android" / avd_name
        log_dir.mkdir(parents=True, exist_ok=True)
        log_file = open(log_dir / "emulator.log", "ab")  # noqa: SIM115 (held by child)
        proc = subprocess.Popen(  # noqa: S603
            cmd,
            stdout=log_file,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            env=self._sdk._sdk_env(),
            start_new_session=True,
        )
        return {
            "pid": proc.pid,
            "console_port": console_port,
            "adb_port": adb_port,
            "serial": serial_for_port(console_port),
        }

    def wait_boot(self, serial: str, timeout: int = 300) -> bool:
        """Poll until sys.boot_completed=1; return True on boot, False on timeout."""
        adb = self._sdk.adb()
        if adb is None:
            raise IPCError(ERR_INTERNAL, "adb not found")
        deadline = time.time() + timeout
        # Wait for the device to register first.
        try:
            subprocess.run(
                [str(adb), "-s", serial, "wait-for-device"],
                capture_output=True,
                timeout=max(5, min(timeout, 120)),
            )
        except Exception:
            pass
        while time.time() < deadline:
            try:
                res = subprocess.run(
                    [str(adb), "-s", serial, "shell", "getprop", "sys.boot_completed"],
                    capture_output=True,
                    text=True,
                    timeout=10,
                )
                if res.stdout.strip() == "1":
                    return True
            except Exception:
                pass
            time.sleep(3)
        return False
