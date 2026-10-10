"""Tests for ADB parsing helpers (pure functions, no emulator required)."""
from __future__ import annotations

from boxerd.adb_ops import (
    parse_adb_devices,
    parse_bounds,
    parse_instrument_output,
    parse_logcat,
    parse_ui_xml,
)


def test_parse_bounds_center():
    b = parse_bounds("[10,20][110,120]")
    assert b == {"x1": 10, "y1": 20, "x2": 110, "y2": 120, "cx": 60, "cy": 70}


def test_parse_bounds_invalid():
    assert parse_bounds("garbage") is None


def test_parse_ui_xml_keeps_interesting_nodes():
    xml = (
        "<hierarchy>"
        '<node class="android.widget.FrameLayout" bounds="[0,0][1080,2400]"/>'
        '<node resource-id="com.app:id/ok" text="OK" clickable="true" '
        'class="android.widget.Button" bounds="[0,0][200,100]"/>'
        '<node content-desc="Menu" clickable="true" bounds="[0,0][50,50]"/>'
        "</hierarchy>"
    )
    nodes = parse_ui_xml(xml)
    # The bare FrameLayout (no id/text/desc, not clickable) is dropped.
    assert len(nodes) == 2
    btn = nodes[0]
    assert btn["id"] == "com.app:id/ok"
    assert btn["text"] == "OK"
    assert btn["class"] == "Button"
    assert btn["clickable"] is True
    assert btn["cx"] == 100 and btn["cy"] == 50
    assert nodes[1]["desc"] == "Menu"


def test_parse_logcat_threadtime():
    text = (
        "--------- beginning of main\n"
        "06-20 10:00:00.123  1234  1300 I ActivityManager: Start proc\n"
        "06-20 10:00:01.000  1234  1234 E AndroidRuntime: FATAL EXCEPTION\n"
        "not a log line\n"
    )
    rows = parse_logcat(text)
    assert len(rows) == 2
    assert rows[0]["level"] == "I" and rows[0]["tag"] == "ActivityManager"
    assert rows[1]["level"] == "E" and rows[1]["pid"] == 1234


def test_parse_logcat_limit():
    text = "\n".join(
        f"06-20 10:00:0{i}.000  1  1 I T: m{i}" for i in range(9)
    )
    rows = parse_logcat(text, limit=3)
    assert len(rows) == 3
    assert rows[-1]["msg"] == "m8"


def test_parse_adb_devices_filters_state():
    out = "List of devices attached\nemulator-5554\tdevice\nemulator-5556\toffline\n"
    assert parse_adb_devices(out) == ["emulator-5554"]


def test_parse_instrument_output_pass():
    text = "INSTRUMENTATION_RESULT: stream=\nTests run: 5\n\nOK (5 tests)\nINSTRUMENTATION_CODE: -1\n"
    report = parse_instrument_output(text)
    assert report["tests"] == 5
    assert report["passed"] is True


def test_parse_instrument_output_fail():
    text = "Tests run: 3\nFailures: 1\nINSTRUMENTATION_STATUS: stack=java.lang.AssertionError\n"
    report = parse_instrument_output(text)
    assert report["failures"] == 1
    assert report["passed"] is False
    assert report["stacks"]
