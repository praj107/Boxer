"""cloud-init ISO generation for VM first-boot configuration."""
from __future__ import annotations

import asyncio
import logging
import subprocess
import tempfile
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

_USER_DATA_TEMPLATE = """\
#cloud-config
hostname: {hostname}
manage_etc_hosts: true

users:
  - name: boxer
    groups: [sudo]
    shell: /bin/bash
    sudo: ALL=(ALL) NOPASSWD:ALL
    lock_passwd: true
{ssh_keys_block}

package_update: true
packages:
  - qemu-guest-agent
  - openssh-server

runcmd:
  - systemctl enable --now qemu-guest-agent
  - systemctl enable --now ssh
"""

_META_DATA_TEMPLATE = """\
instance-id: {vm_id}
local-hostname: {hostname}
"""


def _ssh_keys_block(pubkey: Optional[str]) -> str:
    if not pubkey:
        return ""
    return f"    ssh_authorized_keys:\n      - {pubkey}\n"


class CloudInitBuilder:
    async def build(
        self,
        vm_id: str,
        dest_dir: Path,
        hostname: str,
        ssh_pubkey: Optional[str] = None,
    ) -> Path:
        iso_path = dest_dir / "cloud-init.iso"
        if iso_path.exists():
            return iso_path

        user_data = _USER_DATA_TEMPLATE.format(
            hostname=hostname,
            ssh_keys_block=_ssh_keys_block(ssh_pubkey),
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
