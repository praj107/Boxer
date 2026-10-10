"""Tests for file transfer (vm.push_files / vm.pull), detached exec, and the SSH key default."""
from __future__ import annotations

import base64
import io
import tarfile
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

import boxer.config as config_mod
from boxer.config import BoxerConfig
from boxer.ipc import IPCError
from boxer.transfer import pack_local, save_png, write_pull
from boxer.types import VMRecord
from boxerd.daemon import BoxerDaemon
from boxerd.guest_agent import GuestAgent
from boxerd.policy import PolicyEngine

CALLER = {"caller_project_id": "p_aaa111", "caller_user": "alice"}


def _vm() -> VMRecord:
    now = datetime.now(timezone.utc)
    return VMRecord(
        id="vm_abc123", libvirt_name="Boxer--p_aaa111--t--vm_abc123", project_id="p_aaa111",
        display_name="t", state="running", owner_user="alice", template="ubuntu-24.04",
        cpu=1, ram_mb=512, disk_gb=2, headless=True, created_at=now, last_touched=now,
        lease_until=now + timedelta(minutes=30), ip_address="10.200.1.5",
    )


def _daemon(tmp_path: Path, vm: VMRecord) -> Any:
    daemon = object.__new__(BoxerDaemon)
    daemon._db = AsyncMock()
    daemon._db.get_vm.return_value = vm
    daemon._policy = PolicyEngine()
    daemon._storage = SimpleNamespace(vm_dir=lambda project_id, vm_id: tmp_path)
    daemon._ssh_keys = SimpleNamespace(has_key=lambda d: False, key_path=lambda d: None)
    daemon._guest = AsyncMock()
    return daemon


class FakeConn:
    """Stands in for an asyncssh connection: records commands and stdin bytes."""

    def __init__(self, stdout: Any = "", exit_status: int = 0) -> None:
        self.calls: list[dict[str, Any]] = []
        self._stdout = stdout
        self._exit = exit_status

    async def __aenter__(self) -> "FakeConn":
        return self

    async def __aexit__(self, *exc: Any) -> None:
        return None

    async def run(self, cmd: str, check: bool = False, input: Any = None, encoding: Any = "utf-8") -> Any:
        self.calls.append({"cmd": cmd, "input": input, "encoding": encoding})
        return SimpleNamespace(stdout=self._stdout, stderr=b"" if encoding is None else "", exit_status=self._exit)


# ── client-side packing ──────────────────────────────────────────────────────

def test_pack_directory_roundtrips_and_skips_caches(tmp_path: Path) -> None:
    src = tmp_path / "src"
    (src / "lib").mkdir(parents=True)
    (src / "lib" / "a.js").write_text("module.exports = 1;")
    (src / "__pycache__").mkdir()
    (src / "__pycache__" / "x.pyc").write_bytes(b"junk")
    (src / "top.txt").write_text("hi")

    item = pack_local(str(src), "/home/boxer/app")
    assert item["extract"] is True and item["path"] == "/home/boxer/app"

    out = write_pull({"is_archive": True, "data_b64": item["data_b64"]}, str(tmp_path / "out"))
    assert out["is_archive"] is True
    assert (tmp_path / "out" / "lib" / "a.js").read_text() == "module.exports = 1;"
    assert (tmp_path / "out" / "top.txt").read_text() == "hi"
    assert not (tmp_path / "out" / "__pycache__").exists()


def test_pack_file_keeps_mode(tmp_path: Path) -> None:
    f = tmp_path / "run.sh"
    f.write_text("#!/bin/sh\n")
    f.chmod(0o755)
    item = pack_local(str(f), "/tmp/run.sh")
    assert item["mode"] == 0o755
    assert base64.b64decode(item["data_b64"]) == b"#!/bin/sh\n"


def test_write_pull_refuses_to_clobber(tmp_path: Path) -> None:
    dest = tmp_path / "log.txt"
    dest.write_text("old")
    payload = {"is_archive": False, "data_b64": base64.b64encode(b"new").decode()}
    with pytest.raises(FileExistsError):
        write_pull(payload, str(dest))
    write_pull(payload, str(dest), overwrite=True)
    assert dest.read_text() == "new"


def test_write_pull_blocks_path_traversal(tmp_path: Path) -> None:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        data = b"pwned"
        info = tarfile.TarInfo("../escape.txt")
        info.size = len(data)
        tar.addfile(info, io.BytesIO(data))
    payload = {"is_archive": True, "data_b64": base64.b64encode(buf.getvalue()).decode()}
    with pytest.raises(tarfile.TarError):
        write_pull(payload, str(tmp_path / "out"))
    assert not (tmp_path / "escape.txt").exists()


def test_save_png_reports_dimensions(tmp_path: Path) -> None:
    png = b"\x89PNG\r\n\x1a\n" + b"\x00\x00\x00\rIHDR" + (1024).to_bytes(4, "big") + (768).to_bytes(4, "big") + b"\x08\x02"
    out = save_png(base64.b64encode(png).decode(), str(tmp_path / "s.png"))
    assert (out["width"], out["height"]) == (1024, 768)
    assert (tmp_path / "s.png").read_bytes() == png


# ── guest agent command construction ─────────────────────────────────────────

async def test_push_writes_files_and_extracts_archives() -> None:
    agent = GuestAgent(conn=None, cfg=BoxerConfig({}))
    fake = FakeConn()
    with patch.object(GuestAgent, "_connect", AsyncMock(return_value=fake)):
        written = await agent.push_files("10.0.0.2", [
            {"path": "/etc/x.conf", "data": b"k=v\n", "mode": 0o600, "extract": False},
            {"path": "/opt/app dir", "data": b"TGZ", "mode": None, "extract": True},
        ], sudo=True)
    assert [w["path"] for w in written] == ["/etc/x.conf", "/opt/app dir"]
    first, second = fake.calls
    assert "sudo -n tee /etc/x.conf" in first["cmd"] and "chmod 600" in first["cmd"]
    assert first["input"] == b"k=v\n" and first["encoding"] is None
    assert "tar -xzf" in second["cmd"] and "'/opt/app dir'" in second["cmd"]


async def test_push_failure_surfaces_stderr() -> None:
    agent = GuestAgent(conn=None, cfg=BoxerConfig({}))
    fake = FakeConn(exit_status=1)
    with patch.object(GuestAgent, "_connect", AsyncMock(return_value=fake)):
        with pytest.raises(IPCError, match="push to /root/x failed"):
            await agent.push_files("10.0.0.2", [{"path": "/root/x", "data": b"", "extract": False}])


async def test_pull_splits_kind_marker() -> None:
    agent = GuestAgent(conn=None, cfg=BoxerConfig({}))
    fake = FakeConn(stdout=b"F\nhello")
    with patch.object(GuestAgent, "_connect", AsyncMock(return_value=fake)):
        out = await agent.pull_path("10.0.0.2", "/var/log/x", max_bytes=100)
    assert out == {"path": "/var/log/x", "is_archive": False, "data": b"hello"}


async def test_pull_enforces_size_cap() -> None:
    agent = GuestAgent(conn=None, cfg=BoxerConfig({}))
    fake = FakeConn(stdout=b"F\n" + b"x" * 11)
    with patch.object(GuestAgent, "_connect", AsyncMock(return_value=fake)):
        with pytest.raises(IPCError, match="pull limit"):
            await agent.pull_path("10.0.0.2", "/big", max_bytes=10)


async def test_detached_exec_returns_pid_and_log() -> None:
    agent = GuestAgent(conn=None, cfg=BoxerConfig({}))
    fake = FakeConn(stdout="4242\n")
    with patch.object(GuestAgent, "_connect", AsyncMock(return_value=fake)):
        out = await agent.exec_ssh("10.0.0.2", "apt-get install -y foo && reboot", detach=True)
    assert out["detached"] is True and out["pid"] == "4242"
    assert out["log_path"].startswith("/tmp/boxer-job-")
    cmd = fake.calls[0]["cmd"]
    assert cmd.startswith("setsid nohup sh -c ") and "< /dev/null &" in cmd


# ── daemon validation ────────────────────────────────────────────────────────

async def test_push_handler_validates_and_forwards(tmp_path: Path) -> None:
    daemon = _daemon(tmp_path, _vm())
    daemon._guest.push_files.return_value = [{"path": "/tmp/a", "bytes": 2, "extracted": False}]
    result = await BoxerDaemon._h_vm_push_files(daemon, {
        **CALLER, "vm_id": "vm_abc123",
        "files": [{"path": "/tmp/a", "data_b64": base64.b64encode(b"hi").decode(), "mode": 0o644}],
    })
    assert result["total_bytes"] == 2
    sent = daemon._guest.push_files.call_args[0][1]
    assert sent == [{"path": "/tmp/a", "data": b"hi", "mode": 0o644, "extract": False}]


@pytest.mark.parametrize("bad", [
    {"path": "relative/x", "data_b64": ""},
    {"path": "/tmp/x", "data_b64": "@@not base64@@"},
    {"path": "/tmp/\x00x", "data_b64": ""},
])
async def test_push_handler_rejects_bad_items(tmp_path: Path, bad: dict[str, Any]) -> None:
    daemon = _daemon(tmp_path, _vm())
    with pytest.raises(IPCError):
        await BoxerDaemon._h_vm_push_files(daemon, {**CALLER, "vm_id": "vm_abc123", "files": [bad]})
    daemon._guest.push_files.assert_not_called()


async def test_push_and_pull_respect_ownership(tmp_path: Path) -> None:
    daemon = _daemon(tmp_path, _vm())
    other = {"caller_project_id": "p_other", "caller_user": "mallory"}
    with pytest.raises(IPCError):
        await BoxerDaemon._h_vm_push_files(daemon, {
            **other, "vm_id": "vm_abc123", "files": [{"path": "/tmp/a", "data_b64": ""}]})
    with pytest.raises(IPCError):
        await BoxerDaemon._h_vm_pull(daemon, {**other, "vm_id": "vm_abc123", "path": "/etc/shadow"})
    daemon._guest.push_files.assert_not_called()
    daemon._guest.pull_path.assert_not_called()


async def test_pull_handler_encodes_payload(tmp_path: Path) -> None:
    daemon = _daemon(tmp_path, _vm())
    daemon._guest.pull_path.return_value = {"path": "/tmp/a", "is_archive": False, "data": b"\x00\x01"}
    result = await BoxerDaemon._h_vm_pull(daemon, {**CALLER, "vm_id": "vm_abc123", "path": "/tmp/a"})
    assert base64.b64decode(result["data_b64"]) == b"\x00\x01" and result["bytes"] == 2


async def test_exec_handler_bounds_and_forwards_new_options(tmp_path: Path) -> None:
    daemon = _daemon(tmp_path, _vm())
    daemon._guest.exec_ssh.return_value = {"stdout": "", "stderr": "", "exit_code": 0}
    await BoxerDaemon._h_vm_exec(daemon, {
        **CALLER, "vm_id": "vm_abc123", "command": "true", "detach": True, "wait_ssh_seconds": 90})
    kwargs = daemon._guest.exec_ssh.call_args.kwargs
    assert kwargs["detach"] is True and kwargs["wait_ssh_seconds"] == 90
    with pytest.raises(IPCError):
        await BoxerDaemon._h_vm_exec(daemon, {
            **CALLER, "vm_id": "vm_abc123", "command": "true", "wait_ssh_seconds": 99999})


async def test_vm_without_ip_is_rejected(tmp_path: Path) -> None:
    daemon = _daemon(tmp_path, replace(_vm(), ip_address=None))
    with pytest.raises(IPCError, match="no IP address"):
        await BoxerDaemon._h_vm_pull(daemon, {**CALLER, "vm_id": "vm_abc123", "path": "/tmp/a"})


# ── SSH key default ──────────────────────────────────────────────────────────

def test_ssh_key_defaults_to_setup_generated_pair(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    key = tmp_path / "boxer_id_ed25519"
    key.write_text("PRIVATE")
    (tmp_path / "boxer_id_ed25519.pub").write_text("ssh-ed25519 AAAA boxer-vm-access\n")
    monkeypatch.setattr(config_mod, "DEFAULT_SSH_KEY_PATH", key)
    cfg = BoxerConfig({})
    assert cfg.boxer_ssh_privkey_path == key
    assert cfg.boxer_ssh_pubkey == "ssh-ed25519 AAAA boxer-vm-access"


def test_explicit_ssh_config_wins_and_missing_default_is_none(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(config_mod, "DEFAULT_SSH_KEY_PATH", tmp_path / "absent")
    assert BoxerConfig({}).boxer_ssh_privkey_path is None
    cfg = BoxerConfig({"boxer_ssh_privkey_path": "/x/key", "boxer_ssh_pubkey": "ssh-ed25519 B"})
    assert cfg.boxer_ssh_privkey_path == Path("/x/key")
    assert cfg.boxer_ssh_pubkey == "ssh-ed25519 B"
