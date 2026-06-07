"""cloud-init ISO generation for VM first-boot configuration."""
from __future__ import annotations

import asyncio
import base64
import logging
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import yaml

logger = logging.getLogger(__name__)

_META_DATA_TEMPLATE = """\
instance-id: {vm_id}
local-hostname: {hostname}
"""


@dataclass(frozen=True)
class WriteFile:
    """A file to inject via cloud-init write_files. Content is base64-encoded."""
    path: str
    content: str  # raw text; base64-encoded at render time
    permissions: str = "0644"
    owner: str = "root:root"


@dataclass(frozen=True)
class CloudInitOptions:
    ssh_authorized_keys: list[str] = field(default_factory=list)
    packages: list[str] = field(default_factory=list)
    runcmd: list[str] = field(default_factory=list)
    write_files: list[WriteFile] = field(default_factory=list)


def _dedupe(items: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for item in items:
        if item in seen:
            continue
        seen.add(item)
        result.append(item)
    return result


def _render_user_data(hostname: str, options: CloudInitOptions) -> str:
    packages = _dedupe(["qemu-guest-agent", "openssh-server", *options.packages])
    runcmd = [
        "systemctl enable --now qemu-guest-agent || true",
        # Ensure host keys exist before starting sshd; Debian cloud images
        # occasionally miss this on first boot when the package postinst races
        # with cloud-init's runcmd phase.
        "ssh-keygen -A 2>/dev/null || true",
        "systemctl enable --now ssh || systemctl enable --now sshd || true",
        *options.runcmd,
    ]

    user: dict[str, object] = {
        "name": "boxer",
        "groups": ["sudo"],
        "shell": "/bin/bash",
        "sudo": "ALL=(ALL) NOPASSWD:ALL",
        "lock_passwd": True,
    }
    if options.ssh_authorized_keys:
        user["ssh_authorized_keys"] = options.ssh_authorized_keys

    data: dict[str, object] = {
        "hostname": hostname,
        "manage_etc_hosts": True,
        "users": [user],
        "package_update": True,
        "packages": packages,
        "runcmd": runcmd,
    }
    if options.write_files:
        data["write_files"] = [
            {
                "path": wf.path,
                "encoding": "b64",
                "content": base64.b64encode(wf.content.encode("utf-8")).decode("ascii"),
                "permissions": wf.permissions,
                "owner": wf.owner,
            }
            for wf in options.write_files
        ]
    return "#cloud-config\n" + yaml.safe_dump(data, sort_keys=False)


class CloudInitBuilder:
    async def build(
        self,
        vm_id: str,
        dest_dir: Path,
        hostname: str,
        ssh_pubkey: Optional[str] = None,
        ssh_authorized_keys: Optional[list[str]] = None,
        packages: Optional[list[str]] = None,
        runcmd: Optional[list[str]] = None,
        write_files: Optional[list[WriteFile]] = None,
    ) -> Path:
        iso_path = dest_dir / "cloud-init.iso"
        if iso_path.exists():
            return iso_path

        keys = []
        if ssh_pubkey:
            keys.append(ssh_pubkey)
        keys.extend(ssh_authorized_keys or [])
        user_data = _render_user_data(
            hostname,
            CloudInitOptions(
                ssh_authorized_keys=_dedupe(keys),
                packages=packages or [],
                runcmd=runcmd or [],
                write_files=write_files or [],
            ),
        )
        meta_data = _META_DATA_TEMPLATE.format(vm_id=vm_id, hostname=hostname)

        with tempfile.TemporaryDirectory() as tmpdir:
            ud = Path(tmpdir) / "user-data"
            md = Path(tmpdir) / "meta-data"
            ud.write_text(user_data)
            md.write_text(meta_data)
            await self._run_genisoimage(ud, md, iso_path)

        logger.info("cloud-init ISO created at %s", iso_path)
        return iso_path

    @staticmethod
    async def _run_genisoimage(user_data: Path, meta_data: Path, dest: Path) -> None:
        # Try genisoimage first, fall back to mkisofs
        for cmd in ("genisoimage", "mkisofs"):
            try:
                loop = asyncio.get_running_loop()
                await loop.run_in_executor(
                    None,
                    lambda c=cmd: subprocess.run(
                        [c, "-output", str(dest), "-volid", "cidata",
                         "-joliet", "-rock", str(user_data), str(meta_data)],
                        check=True,
                        capture_output=True,
                    ),
                )
                return
            except FileNotFoundError:
                continue
            except subprocess.CalledProcessError as exc:
                raise RuntimeError(f"{cmd} failed: {exc.stderr.decode()}") from exc
        raise RuntimeError("Neither genisoimage nor mkisofs found; install one to use cloud-init")
