"""Tests for opinionated lab recipes."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import pytest

from boxer.ipc import ERR_INVALID_PARAMS, IPCError
from boxer.types import VMRecord
from boxerd.daemon import BoxerDaemon
from boxerd.labs import build_lab_request, lab_status_payload, list_labs


def test_list_labs_exposes_practical_niches() -> None:
    labs = {lab["name"]: lab for lab in list_labs()}
    assert {"linux-repro", "distro-smoke", "docker-build", "web-ui", "headed-desktop"} <= set(labs)
    assert labs["docker-build"]["profiles"] == ["docker"]
    assert "host Docker" in labs["docker-build"]["description"]


def test_build_lab_request_defaults_to_recipe_shape() -> None:
    lab, request = build_lab_request({"lab": "docker-build", "purpose": "image-build"})
    assert lab.name == "docker-build"
    assert request["template"] == "ubuntu-24.04"
    assert request["purpose"] == "image-build"
    assert request["profiles"] == ["docker"]
    assert request["ram_mb"] == 4096
    assert request["disk_gb"] == 30
    assert request["wait_command"] == "docker version >/dev/null 2>&1"
    assert request["tags"]["boxer.lab"] == "docker-build"
    assert request["tags"]["boxer.interface"] == "lab"


def test_build_lab_request_size_and_template_override() -> None:
    _, request = build_lab_request(
        {
            "lab": "distro-smoke",
            "purpose": "fedora-check",
            "template": "fedora-44",
            "size": "small",
            "wait": False,
            "tags": {"ticket": "ENG-42"},
        }
    )
    assert request["template"] == "fedora-44"
    assert request["cpu"] == 1
    assert request["ram_mb"] == 1536
    assert request["disk_gb"] == 12
    assert "wait_for" not in request
    assert request["tags"]["ticket"] == "ENG-42"


def test_build_lab_request_rejects_unknown_and_reserved_tags() -> None:
    with pytest.raises(IPCError) as exc:
        build_lab_request({"lab": "vmops-panel"})
    assert exc.value.code == ERR_INVALID_PARAMS

    with pytest.raises(IPCError):
        build_lab_request({"lab": "linux-repro", "tags": {"boxer.lab": "override"}})


@pytest.mark.asyncio
async def test_daemon_lab_request_calls_vm_request_with_lab_metadata() -> None:
    daemon = object.__new__(BoxerDaemon)
    daemon._h_vm_request = AsyncMock(
        return_value={
            "vm_id": "vm_lab01",
            "state": "running",
            "tags": {"boxer.lab": "linux-repro"},
        }
    )

    result = await BoxerDaemon._h_lab_request(
        daemon,
        {
            "caller_project_id": "p_aaa111",
            "caller_user": "alice",
            "lab": "linux-repro",
            "purpose": "bug-123",
        },
    )

    vm_request = daemon._h_vm_request.call_args[0][0]
    assert vm_request["caller_project_id"] == "p_aaa111"
    assert vm_request["caller_user"] == "alice"
    assert vm_request["purpose"] == "bug-123"
    assert vm_request["profiles"] == ["python"]
    assert vm_request["tags"]["boxer.lab"] == "linux-repro"
    assert result["lab"]["name"] == "linux-repro"
    assert result["lab"]["vm_id"] == "vm_lab01"
    assert result["lab"]["next_steps"]


def test_lab_status_payload_reports_ready_and_next_steps() -> None:
    now = datetime.now(timezone.utc)
    vm = VMRecord(
        id="vm_lab01",
        libvirt_name="Boxer--p_aaa111--lab--vm_lab01",
        project_id="p_aaa111",
        display_name="bug-123",
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
        ip_address="10.0.0.5",
        tags={"boxer.lab": "linux-repro"},
    )
    payload = lab_status_payload(
        {
            "vm_id": vm.id,
            "state": vm.state,
            "live_state": "running",
            "ip_address": vm.ip_address,
            "tags": vm.tags,
        }
    )
    assert payload["lab"]["name"] == "linux-repro"
    assert payload["lab"]["ready"] is True
    assert payload["lab"]["next_steps"]
