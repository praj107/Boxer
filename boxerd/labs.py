"""Opinionated Boxer lab recipes built on the existing VM request path."""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Literal

from boxer.ipc import ERR_INVALID_PARAMS, IPCError

LabSize = Literal["small", "default", "large"]

_LAB_NAME_RE = re.compile(r"^[a-z][a-z0-9-]{0,40}$")
_PURPOSE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,48}$")

SIZE_OVERRIDES: dict[LabSize, dict[str, int]] = {
    "small": {"cpu": 1, "ram_mb": 1536, "disk_gb": 12},
    "default": {},
    "large": {"cpu": 4, "ram_mb": 6144, "disk_gb": 40},
}


@dataclass(frozen=True)
class LabRecipe:
    name: str
    title: str
    niche: str
    description: str
    default_template: str
    profiles: tuple[str, ...] = ()
    headless: bool = True
    ttl_minutes: int = 90
    wait_for: tuple[str, ...] = ("ssh", "cloud_init")
    wait_timeout_seconds: int = 300
    wait_command: str | None = None
    cpu: int | None = None
    ram_mb: int | None = None
    disk_gb: int | None = None
    recommended_templates: tuple[str, ...] = ()
    next_steps: tuple[str, ...] = field(default_factory=tuple)


LAB_RECIPES: dict[str, LabRecipe] = {
    "linux-repro": LabRecipe(
        name="linux-repro",
        title="Clean Linux Reproduction Lab",
        niche="reproduce a bug or run untrusted project commands away from the host",
        description=(
            "Creates one clean cloud-image VM with Python tooling and SSH/cloud-init readiness waits. "
            "Use for reproducing issues, running project tests, or inspecting generated artifacts without "
            "touching the host environment."
        ),
        default_template="ubuntu-24.04",
        profiles=("python",),
        recommended_templates=("ubuntu-24.04", "debian-12", "fedora-44", "almalinux-9", "rocky-9"),
        next_steps=(
            "Run minimal repro commands with box_exec.",
            "Use box_get_serial_log if cloud-init or boot readiness times out.",
            "Delete the lab VM when the repro is captured.",
        ),
    ),
    "distro-smoke": LabRecipe(
        name="distro-smoke",
        title="Distro Portability Smoke Lab",
        niche="check whether scripts or packages assume one Linux family",
        description=(
            "Creates a single VM on the requested distro template with minimal tooling and readiness waits. "
            "Run the same command across multiple lab VMs when you need a small distro matrix."
        ),
        default_template="debian-12",
        profiles=("python",),
        ttl_minutes=75,
        recommended_templates=("ubuntu-24.04", "debian-12", "fedora-44", "almalinux-9", "rocky-9", "alpine-3.23-cloud"),
        next_steps=(
            "Run the same smoke command on each distro lab.",
            "Compare stdout/stderr and package availability.",
            "Keep output snippets small and delete completed labs.",
        ),
    ),
    "docker-build": LabRecipe(
        name="docker-build",
        title="Isolated Docker Build Lab",
        niche="build or test containers without sharing the host Docker daemon",
        description=(
            "Creates one VM with Docker installed and the boxer user added to the docker group. "
            "Use when an agent needs container builds but should not touch host Docker state."
        ),
        default_template="ubuntu-24.04",
        profiles=("docker",),
        ttl_minutes=120,
        wait_command="docker version >/dev/null 2>&1",
        wait_timeout_seconds=480,
        ram_mb=4096,
        disk_gb=30,
        recommended_templates=("ubuntu-24.04", "debian-12"),
        next_steps=(
            "Copy or fetch only the build inputs needed for the container test.",
            "Run docker build/test commands with box_exec.",
            "Use box_delete_vm when the image cache is no longer useful.",
        ),
    ),
    "web-ui": LabRecipe(
        name="web-ui",
        title="Headless Web UI Test Lab",
        niche="run browser automation away from the host browser profile",
        description=(
            "Creates one VM with Chromium, Xvfb, fonts, and Python tooling. Use for Playwright, "
            "Selenium, or headless browser checks where host browser state should stay private."
        ),
        default_template="ubuntu-24.04",
        profiles=("python", "browser"),
        ttl_minutes=120,
        ram_mb=4096,
        disk_gb=25,
        recommended_templates=("ubuntu-24.04", "debian-12"),
        next_steps=(
            "Start the app or test server inside the VM.",
            "Run browser tests through box_exec.",
            "Use boxer forward only when a human needs to inspect a local port.",
        ),
    ),
    "headed-desktop": LabRecipe(
        name="headed-desktop",
        title="Headed Desktop Inspection Lab",
        niche="inspect GUI behavior or desktop installers through SPICE screenshots/input",
        description=(
            "Creates one headed VM with a minimal XFCE desktop. Use when the task truly needs a graphical "
            "session rather than a shell-only repro."
        ),
        default_template="ubuntu-24.04",
        profiles=("desktop",),
        headless=False,
        ttl_minutes=120,
        wait_for=("cloud_init",),
        wait_timeout_seconds=600,
        ram_mb=4096,
        disk_gb=30,
        recommended_templates=("ubuntu-24.04",),
        next_steps=(
            "Use box_screenshot and box_input from a rich MCP profile for GUI inspection.",
            "Prefer web-ui for browser automation that does not need a full desktop.",
            "Delete the lab VM after capturing the visual evidence.",
        ),
    ),
}


def list_labs() -> list[dict[str, Any]]:
    return [
        {
            "name": lab.name,
            "title": lab.title,
            "niche": lab.niche,
            "description": lab.description,
            "default_template": lab.default_template,
            "recommended_templates": list(lab.recommended_templates),
            "profiles": list(lab.profiles),
            "headless": lab.headless,
            "ttl_minutes": lab.ttl_minutes,
            "wait_for": list(lab.wait_for),
            "wait_timeout_seconds": lab.wait_timeout_seconds,
            "next_steps": list(lab.next_steps),
        }
        for lab in LAB_RECIPES.values()
    ]


def get_lab(name: str) -> LabRecipe:
    if not isinstance(name, str) or not _LAB_NAME_RE.match(name):
        raise IPCError(ERR_INVALID_PARAMS, "lab must be a lab recipe name")
    try:
        return LAB_RECIPES[name]
    except KeyError as exc:
        raise IPCError(
            ERR_INVALID_PARAMS,
            f"Unknown lab '{name}'. Available labs: {sorted(LAB_RECIPES)}",
        ) from exc


def build_lab_request(params: dict[str, Any]) -> tuple[LabRecipe, dict[str, Any]]:
    lab = get_lab(params.get("lab", ""))
    purpose = params.get("purpose") or lab.name
    if not isinstance(purpose, str) or not _PURPOSE_RE.match(purpose):
        raise IPCError(ERR_INVALID_PARAMS, "purpose must be a short slug using letters, numbers, dot, dash, or underscore")

    template = params.get("template") or lab.default_template
    if not isinstance(template, str) or not template:
        raise IPCError(ERR_INVALID_PARAMS, "template must be a catalog template name")

    size = str(params.get("size", "default")).lower()
    if size not in SIZE_OVERRIDES:
        raise IPCError(ERR_INVALID_PARAMS, f"size must be one of {sorted(SIZE_OVERRIDES)}")

    wait = bool(params.get("wait", True))
    ttl_minutes = int(params.get("ttl_minutes", lab.ttl_minutes) or lab.ttl_minutes)
    if ttl_minutes < 10 or ttl_minutes > 24 * 60:
        raise IPCError(ERR_INVALID_PARAMS, "ttl_minutes must be between 10 and 1440")

    extra_tags = params.get("tags") or {}
    if not isinstance(extra_tags, dict):
        raise IPCError(ERR_INVALID_PARAMS, "tags must be an object")
    tags: dict[str, str] = {
        "boxer.lab": lab.name,
        "boxer.niche": lab.niche,
        "boxer.interface": "lab",
    }
    for key, value in extra_tags.items():
        if not isinstance(key, str) or not isinstance(value, str):
            raise IPCError(ERR_INVALID_PARAMS, "tags must be string key/value pairs")
        if key.startswith("boxer."):
            raise IPCError(ERR_INVALID_PARAMS, "tags may not override boxer.* lab metadata")
        tags[key[:64]] = value[:256]

    request: dict[str, Any] = {
        "template": template,
        "purpose": purpose,
        "headless": lab.headless,
        "ttl_minutes": ttl_minutes,
        "profiles": list(lab.profiles),
        "tags": tags,
    }
    if wait:
        request["wait_for"] = list(lab.wait_for)
        request["wait_timeout_seconds"] = lab.wait_timeout_seconds
        if lab.wait_command:
            request["wait_command"] = lab.wait_command
    if lab.cpu is not None:
        request["cpu"] = lab.cpu
    if lab.ram_mb is not None:
        request["ram_mb"] = lab.ram_mb
    if lab.disk_gb is not None:
        request["disk_gb"] = lab.disk_gb

    for key, value in SIZE_OVERRIDES[size].items():
        request[key] = value

    return lab, request


def lab_payload(lab: LabRecipe, vm_result: dict[str, Any]) -> dict[str, Any]:
    payload = dict(vm_result)
    payload["lab"] = {
        "name": lab.name,
        "title": lab.title,
        "niche": lab.niche,
        "vm_id": vm_result.get("vm_id"),
        "status": vm_result.get("status", vm_result.get("state")),
        "next_steps": list(lab.next_steps),
    }
    if vm_result.get("status") == "queued":
        payload["lab"]["next_steps"] = [
            "Poll box_queue_status until the lab is promoted.",
            "After promotion, use box_list_vms to find the lab VM tagged with boxer.lab.",
            "Keep the lab count small; Boxer enforces host and project capacity.",
        ]
    return payload


def lab_status_payload(vm: dict[str, Any]) -> dict[str, Any]:
    tags = vm.get("tags") or {}
    lab_name = tags.get("boxer.lab")
    lab = LAB_RECIPES.get(lab_name) if isinstance(lab_name, str) else None
    ready = vm.get("live_state", vm.get("state")) == "running" and bool(vm.get("ip_address"))
    return {
        "vm": vm,
        "lab": {
            "name": lab_name,
            "title": lab.title if lab else None,
            "niche": lab.niche if lab else None,
            "ready": ready,
            "next_steps": list(lab.next_steps) if lab else [],
        },
    }
