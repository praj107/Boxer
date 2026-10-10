"""ADB control plane for Android emulator devices.

Exposes the agent-facing primitives chosen in the consultation: ad-hoc
UIAutomator (dump the UI tree as compact JSON, tap/swipe/type/key by selector),
app control (install/launch/stop/clear), structured logcat telemetry,
screenshots, and an ``am instrument`` runner.

Parsing helpers (``parse_ui_xml``, ``parse_logcat``, ``parse_adb_devices``,
``parse_instrument_output``) are module-level pure functions so they can be unit
tested without an emulator.
"""
from __future__ import annotations

import base64
import logging
import re
import subprocess
import xml.etree.ElementTree as ET
from typing import Any, Optional

from boxer.ipc import ERR_INTERNAL, IPCError
from boxerd.android_sdk import AndroidSDK

logger = logging.getLogger(__name__)

_BOUNDS_RE = re.compile(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]")
# threadtime logcat:  MM-DD HH:MM:SS.mmm  PID  TID  LEVEL  TAG: message
_LOGCAT_RE = re.compile(
    r"^(?P<ts>\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2}\.\d+)\s+"
    r"(?P<pid>\d+)\s+(?P<tid>\d+)\s+(?P<level>[VDIWEF])\s+(?P<tag>[^:]*?):\s?(?P<msg>.*)$"
)
_LOG_LEVELS = {"V", "D", "I", "W", "E", "F"}


def parse_bounds(raw: str) -> Optional[dict[str, int]]:
    """Parse an android:bounds string ``[x1,y1][x2,y2]`` into a center + rect."""
    m = _BOUNDS_RE.search(raw or "")
    if not m:
        return None
    x1, y1, x2, y2 = (int(g) for g in m.groups())
    return {
        "x1": x1,
        "y1": y1,
        "x2": x2,
        "y2": y2,
        "cx": (x1 + x2) // 2,
        "cy": (y1 + y2) // 2,
    }


def parse_ui_xml(xml: str) -> list[dict[str, Any]]:
    """Flatten a UIAutomator XML dump into a compact list of interesting nodes.

    Only nodes that carry a resource-id, text, content-desc, or are clickable are
    kept, with their identifying attributes and tap center — far cheaper for a
    model to read than the raw nested XML.
    """
    try:
        root = ET.fromstring(xml)
    except ET.ParseError as exc:
        raise IPCError(ERR_INTERNAL, f"Could not parse UIAutomator dump: {exc}") from exc

    nodes: list[dict[str, Any]] = []
    for el in root.iter("node"):
        rid = el.get("resource-id", "")
        text = el.get("text", "")
        desc = el.get("content-desc", "")
        clickable = el.get("clickable") == "true"
        if not (rid or text or desc or clickable):
            continue
        node: dict[str, Any] = {}
        if rid:
            node["id"] = rid
        if text:
            node["text"] = text
        if desc:
            node["desc"] = desc
        cls = el.get("class", "")
        if cls:
            node["class"] = cls.rsplit(".", 1)[-1]
        if clickable:
            node["clickable"] = True
        bounds = parse_bounds(el.get("bounds", ""))
        if bounds:
            node["cx"] = bounds["cx"]
            node["cy"] = bounds["cy"]
        nodes.append(node)
    return nodes


def parse_logcat(text: str, limit: int = 500) -> list[dict[str, Any]]:
    """Parse ``logcat -v threadtime`` output into structured records."""
    records: list[dict[str, Any]] = []
    for line in text.splitlines():
        m = _LOGCAT_RE.match(line)
        if not m:
            continue
        records.append(
            {
                "ts": m.group("ts"),
                "pid": int(m.group("pid")),
                "tid": int(m.group("tid")),
                "level": m.group("level"),
                "tag": m.group("tag").strip(),
                "msg": m.group("msg"),
            }
        )
    if limit and len(records) > limit:
        records = records[-limit:]
    return records


def parse_adb_devices(text: str) -> list[str]:
    """Return the list of serials reported by ``adb devices`` (device state only)."""
    serials: list[str] = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("List of devices"):
            continue
        parts = line.split()
        if len(parts) >= 2 and parts[1] == "device":
            serials.append(parts[0])
    return serials


def parse_instrument_output(text: str) -> dict[str, Any]:
    """Summarize ``am instrument -r`` output into a structured pass/fail report."""
    summary: dict[str, Any] = {"raw_tail": text.strip().splitlines()[-20:]}
    tests = re.search(r"Tests run:\s*(\d+)", text)
    failures = re.search(r"Failures?:\s*(\d+)", text)
    ok = "OK (" in text or "INSTRUMENTATION_CODE: -1" in text
    if tests:
        summary["tests"] = int(tests.group(1))
    if failures:
        summary["failures"] = int(failures.group(1))
    failure_blocks = re.findall(r"INSTRUMENTATION_STATUS:\s*stack=(.+)", text)
    if failure_blocks:
        summary["stacks"] = [b.strip()[:1000] for b in failure_blocks[:5]]
    summary["passed"] = bool(ok) and not (failures and int(failures.group(1)) > 0)
    return summary


class AdbOperations:
    def __init__(self, sdk: AndroidSDK):
        self._sdk = sdk

    def _adb(self, serial: str, args: list[str], *, timeout: int = 30, binary: bool = False):
        adb = self._sdk.adb()
        if adb is None:
            raise IPCError(ERR_INTERNAL, "adb not found")
        cmd = [str(adb), "-s", serial, *args]
        result = subprocess.run(
            cmd,
            capture_output=True,
            timeout=timeout,
            text=not binary,
        )
        if result.returncode != 0:
            err = result.stderr if binary else (result.stderr or "")
            if binary and isinstance(err, bytes):
                err = err.decode(errors="replace")
            raise IPCError(ERR_INTERNAL, f"adb {' '.join(args[:2])} failed: {str(err).strip()[:400]}")
        return result.stdout

    # ------------------------------------------------------------------ UI tree

    def ui_dump(self, serial: str, timeout: int = 30) -> list[dict[str, Any]]:
        # exec-out streams the XML straight to stdout without a temp file on device.
        xml = self._adb(serial, ["exec-out", "uiautomator", "dump", "/dev/tty"], timeout=timeout)
        # uiautomator prints a trailing status line; keep only the XML document.
        start = xml.find("<?xml")
        if start == -1:
            start = xml.find("<hierarchy")
        end = xml.rfind("</hierarchy>")
        if start != -1 and end != -1:
            xml = xml[start : end + len("</hierarchy>")]
        return parse_ui_xml(xml)

    def tap(self, serial: str, x: int, y: int) -> None:
        self._adb(serial, ["shell", "input", "tap", str(int(x)), str(int(y))])

    def swipe(self, serial: str, x1: int, y1: int, x2: int, y2: int, ms: int = 300) -> None:
        self._adb(
            serial,
            ["shell", "input", "swipe", str(int(x1)), str(int(y1)), str(int(x2)), str(int(y2)), str(int(ms))],
        )

    def text(self, serial: str, value: str) -> None:
        # input text uses %s for spaces; escape minimally.
        escaped = value.replace(" ", "%s")
        self._adb(serial, ["shell", "input", "text", escaped])

    def key(self, serial: str, keycode: str) -> None:
        self._adb(serial, ["shell", "input", "keyevent", keycode])

    # ------------------------------------------------------------------ app control

    def install_apk(self, serial: str, apk_path: str, timeout: int = 300) -> None:
        self._adb(serial, ["install", "-r", "-g", apk_path], timeout=timeout)

    def launch(self, serial: str, package: str, activity: Optional[str] = None) -> str:
        if activity:
            comp = activity if "/" in activity else f"{package}/{activity}"
            return self._adb(serial, ["shell", "am", "start", "-n", comp])
        return self._adb(
            serial, ["shell", "monkey", "-p", package, "-c", "android.intent.category.LAUNCHER", "1"]
        )

    def stop_app(self, serial: str, package: str) -> None:
        self._adb(serial, ["shell", "am", "force-stop", package])

    def clear_app(self, serial: str, package: str) -> None:
        self._adb(serial, ["shell", "pm", "clear", package])

    def current_activity(self, serial: str) -> str:
        return self._adb(serial, ["shell", "dumpsys", "activity", "activities"], timeout=20)

    def pull(self, serial: str, remote: str, local: str, timeout: int = 120) -> None:
        self._adb(serial, ["pull", remote, local], timeout=timeout)

    # ------------------------------------------------------------------ telemetry

    def screenshot(self, serial: str, timeout: int = 30) -> str:
        png = self._adb(serial, ["exec-out", "screencap", "-p"], timeout=timeout, binary=True)
        return base64.b64encode(png).decode()

    def logcat(
        self,
        serial: str,
        *,
        tag: Optional[str] = None,
        priority: str = "V",
        package_pid: Optional[int] = None,
        limit: int = 500,
        timeout: int = 30,
    ) -> list[dict[str, Any]]:
        args = ["logcat", "-d", "-v", "threadtime"]
        if package_pid:
            args += ["--pid", str(package_pid)]
        if tag:
            args += [f"{tag}:{priority}", "*:S"]
        out = self._adb(serial, args, timeout=timeout)
        return parse_logcat(out, limit=limit)

    def pidof(self, serial: str, package: str) -> Optional[int]:
        out = self._adb(serial, ["shell", "pidof", package]).strip()
        return int(out.split()[0]) if out else None

    def instrument(
        self,
        serial: str,
        test_package: str,
        runner: str = "androidx.test.runner.AndroidJUnitRunner",
        class_or_method: Optional[str] = None,
        timeout: int = 1800,
    ) -> dict[str, Any]:
        args = ["shell", "am", "instrument", "-w", "-r"]
        if class_or_method:
            args += ["-e", "class", class_or_method]
        args.append(f"{test_package}/{runner}")
        out = self._adb(serial, args, timeout=timeout)
        return parse_instrument_output(out)
