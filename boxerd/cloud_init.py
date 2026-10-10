"""cloud-init ISO generation for VM first-boot configuration."""
from __future__ import annotations

import asyncio
import base64
import json
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
        # systemctl for systemd distros; rc-service fallback for Alpine/OpenRC.
        "systemctl enable --now qemu-guest-agent 2>/dev/null || rc-service qemu-guest-agent start 2>/dev/null || true",
        # Ensure host keys exist before starting sshd; Debian cloud images
        # occasionally miss this on first boot when the package postinst races
        # with cloud-init's runcmd phase.
        "ssh-keygen -A 2>/dev/null || true",
        "systemctl enable --now ssh 2>/dev/null || systemctl enable --now sshd 2>/dev/null || rc-service sshd start 2>/dev/null || true",
        *options.runcmd,
    ]

    user: dict[str, object] = {
        "name": "boxer",
        "groups": ["sudo", "wheel"],
        "shell": "/bin/sh",
        "sudo": "ALL=(ALL) NOPASSWD:ALL",
        # Use passwd: '*' (disabled, not locked) instead of lock_passwd:True.
        # Alpine's PAM rejects key-auth for locked accounts (! prefix in shadow);
        # '*' allows SSH key auth while keeping password login impossible.
        "passwd": "*",
        "lock_passwd": False,
    }
    if options.ssh_authorized_keys:
        user["ssh_authorized_keys"] = options.ssh_authorized_keys

    # Explicit network config using netplan v2 syntax.  Cloud-init translates
    # this to the renderer appropriate for the distro (NM on EL9, netplan on
    # Ubuntu, ENI on Debian, etc.).  Matching both eth* and en* covers:
    # - net.ifnames=0 kernels (Rocky 9 GenericCloud): NIC is "eth0"
    # - predictable-name kernels (Debian/Ubuntu/AlmaLinux): NIC is "ens*"/"enp*"
    network_config: dict[str, object] = {
        "version": 2,
        "ethernets": {
            "all-eth": {
                "match": {"name": "eth*"},
                "dhcp4": True,
                "dhcp6": False,
                "set-name": "eth0",
            },
            "all-en": {
                "match": {"name": "en*"},
                "dhcp4": True,
                "dhcp6": False,
            },
        },
    }
    data: dict[str, object] = {
        "hostname": hostname,
        "manage_etc_hosts": True,
        "users": [user],
        "network": network_config,
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


def _render_configdrive_files(
    vm_id: str,
    hostname: str,
    user_data: str,
    options: CloudInitOptions,
) -> dict[str, str]:
    """Return a mapping of path→content for an OpenStack ConfigDrive layout.

    ConfigDrive is the native cloud-init datasource on RHEL-derivative images
    (Rocky, AlmaLinux, CentOS Stream).  The disk must be labelled 'config-2'
    and contain an openstack/latest/ directory tree.
    """
    meta = {
        "uuid": vm_id,
        "hostname": hostname,
        "name": hostname,
        "launch_index": 0,
        "availability_zone": "nova",
        "project_id": "boxer",
        "public_keys": {
            f"key-{i}": k
            for i, k in enumerate(options.ssh_authorized_keys)
        },
    }
    return {
        "openstack/latest/meta_data.json": json.dumps(meta),
        "openstack/latest/user_data": user_data,
        "openstack/latest/vendor_data.json": "{}",
        "openstack/2012-08-10/meta_data.json": json.dumps(meta),
        "openstack/2012-08-10/user_data": user_data,
        "openstack/2012-08-10/vendor_data.json": "{}",
        "ec2/latest/meta-data.json": json.dumps({"instance-id": vm_id, "hostname": hostname}),
    }


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
    ) -> tuple[Path, Path]:
        """Build both seed ISOs and return (nocloud_iso, configdrive_iso).

        Two ISOs are always created:
        - cloud-init.iso   (label "cidata",    NoCloud format)   → /dev/vdb in VM
        - configdrive.iso  (label "config-2",  ConfigDrive format) → /dev/vdc in VM

        This dual-seed approach handles all major cloud-init distros:
        - Ubuntu, Debian, Alpine: prefer NoCloud (cidata)
        - RHEL, Rocky, AlmaLinux, CentOS: prefer ConfigDrive (config-2)
        """
        nocloud_iso = dest_dir / "cloud-init.iso"
        configdrive_iso = dest_dir / "configdrive.iso"

        keys = []
        if ssh_pubkey:
            keys.append(ssh_pubkey)
        keys.extend(ssh_authorized_keys or [])
        deduped_keys = _dedupe(keys)

        opts = CloudInitOptions(
            ssh_authorized_keys=deduped_keys,
            packages=packages or [],
            runcmd=runcmd or [],
            write_files=write_files or [],
        )
        user_data = _render_user_data(hostname, opts)
        meta_data = _META_DATA_TEMPLATE.format(vm_id=vm_id, hostname=hostname)

        if not nocloud_iso.exists():
            with tempfile.TemporaryDirectory() as tmpdir:
                ud = Path(tmpdir) / "user-data"
                md = Path(tmpdir) / "meta-data"
                ud.write_text(user_data)
                md.write_text(meta_data)
                await self._run_genisoimage_nocloud(ud, md, nocloud_iso)
            logger.info("NoCloud ISO created at %s", nocloud_iso)

        if not configdrive_iso.exists():
            cd_files = _render_configdrive_files(vm_id, hostname, user_data, opts)
            with tempfile.TemporaryDirectory() as tmpdir:
                tmppath = Path(tmpdir)
                for rel, content in cd_files.items():
                    dest = tmppath / rel
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    dest.write_text(content)
                await self._run_genisoimage_configdrive(tmppath, configdrive_iso)
            logger.info("ConfigDrive ISO created at %s", configdrive_iso)

        return nocloud_iso, configdrive_iso

    @staticmethod
    async def _run_genisoimage_nocloud(user_data: Path, meta_data: Path, dest: Path) -> None:
        for cmd in ("genisoimage", "mkisofs"):
            try:
                loop = asyncio.get_running_loop()
                await loop.run_in_executor(
                    None,
                    lambda c=cmd: subprocess.run(
                        [c, "-output", str(dest), "-volid", "cidata",
                         "-rock", str(user_data), str(meta_data)],
                        check=True,
                        capture_output=True,
                    ),
                )
                return
            except FileNotFoundError:
                continue
            except subprocess.CalledProcessError as exc:
                raise RuntimeError(f"{cmd} failed: {exc.stderr.decode()}") from exc
        raise RuntimeError("Neither genisoimage nor mkisofs found")

    @staticmethod
    async def _run_genisoimage_configdrive(src_dir: Path, dest: Path) -> None:
        for cmd in ("genisoimage", "mkisofs"):
            try:
                loop = asyncio.get_running_loop()
                await loop.run_in_executor(
                    None,
                    lambda c=cmd: subprocess.run(
                        [c, "-output", str(dest), "-volid", "config-2",
                         "-rock", "-joliet",
                         "-full-iso9660-filenames",
                         str(src_dir)],
                        check=True,
                        capture_output=True,
                    ),
                )
                return
            except FileNotFoundError:
                continue
            except subprocess.CalledProcessError as exc:
                raise RuntimeError(f"{cmd} failed: {exc.stderr.decode()}") from exc
        raise RuntimeError("Neither genisoimage nor mkisofs found")
