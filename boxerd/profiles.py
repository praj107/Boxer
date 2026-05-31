"""Named bootstrap profiles — common agent VM setups as one bounded input.

A profile is a small, vetted bundle of cloud-init `packages` + `runcmd` (and
optionally `write_files`) so a caller can ask for `["python", "docker"]` instead
of spelling out a setup transcript. The set of names is a closed allowlist; a
request naming an unknown profile is rejected, keeping the MCP surface bounded.

Profiles target Debian/Ubuntu package names (the default cloud-image families);
on other families the package layer is best-effort.
"""
from __future__ import annotations

from boxer.ipc import ERR_INVALID_PARAMS, IPCError

# name -> {"packages": [...], "runcmd": [...], "description": str}
PROFILES: dict[str, dict] = {
    "python": {
        "description": "Python 3 toolchain (pip, venv, build tools, git)",
        "packages": ["python3", "python3-pip", "python3-venv", "build-essential", "git"],
        "runcmd": [],
    },
    "node": {
        "description": "Node.js runtime with npm and git",
        "packages": ["nodejs", "npm", "git"],
        "runcmd": ["corepack enable || true"],
    },
    "docker": {
        "description": "Docker engine with the boxer user in the docker group",
        "packages": ["docker.io"],
        "runcmd": [
            "systemctl enable --now docker || true",
            "usermod -aG docker boxer || true",
        ],
    },
    "browser": {
        "description": "Headless Chromium for browser automation (Xvfb + fonts)",
        "packages": ["chromium", "xvfb", "fonts-liberation", "ca-certificates"],
        "runcmd": [],
    },
    "desktop": {
        "description": "Minimal XFCE desktop for headed VMs",
        "packages": ["xfce4", "xfce4-goodies", "lightdm"],
        "runcmd": ["systemctl set-default graphical.target || true"],
    },
}

MAX_PROFILES = 5


def available_profiles() -> list[dict]:
    """Return the catalog of profiles for discovery (name + description + packages)."""
    return [
        {"name": name, "description": p["description"], "packages": list(p["packages"])}
        for name, p in PROFILES.items()
    ]


def resolve_profiles(names: list[str]) -> dict[str, list[str]]:
    """Merge the named profiles into deduped packages + runcmd.

    Raises ``ERR_INVALID_PARAMS`` for unknown or too many profile names.
    """
    if not names:
        return {"packages": [], "runcmd": []}
    if len(names) > MAX_PROFILES:
        raise IPCError(ERR_INVALID_PARAMS, f"At most {MAX_PROFILES} profiles may be combined")

    unknown = [n for n in names if n not in PROFILES]
    if unknown:
        raise IPCError(
            ERR_INVALID_PARAMS,
            f"Unknown profile(s): {unknown}. Available: {sorted(PROFILES)}",
        )

    packages: list[str] = []
    runcmd: list[str] = []
    seen_pkg: set[str] = set()
    seen_cmd: set[str] = set()
    for name in names:
        profile = PROFILES[name]
        for pkg in profile["packages"]:
            if pkg not in seen_pkg:
                seen_pkg.add(pkg)
                packages.append(pkg)
        for cmd in profile["runcmd"]:
            if cmd not in seen_cmd:
                seen_cmd.add(cmd)
                runcmd.append(cmd)
    return {"packages": packages, "runcmd": runcmd}
