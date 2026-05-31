"""Unattended-install seed generation for ISO installer VMs.

Each supported distro family has its own automation format and its own
auto-detection convention for where the installer looks for that file:

- ``ubuntu``  → cloud-init NoCloud autoinstall (``user-data`` on a volume
  labelled ``cidata``); subiquity consumes the ``autoinstall:`` document.
- ``debian``  → preseed (``preseed.cfg`` on a labelled volume).
- ``fedora`` / ``rhel`` → Kickstart (``ks.cfg`` on a volume labelled
  ``OEMDRV``, which anaconda auto-loads without a kernel argument).
- ``manual``  → no seed; the installer is driven interactively over SPICE.

The rendered text is the testable contract here; building the tiny seed ISO is
delegated to genisoimage/mkisofs exactly as :mod:`boxerd.cloud_init` does.
"""
from __future__ import annotations

import asyncio
import logging
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import yaml

logger = logging.getLogger(__name__)

# install_method → ISO volume label the installer auto-detects.
_VOLID = {
    "ubuntu": "cidata",
    "debian": "PRESEED",
    "fedora": "OEMDRV",
    "rhel": "OEMDRV",
}

SUPPORTED_METHODS = frozenset({"ubuntu", "debian", "fedora", "rhel", "manual"})

# A locked password: no console/password login is possible, SSH-key auth only.
# Installers that require an identity password accept the locked sentinel.
_LOCKED_PASSWORD = "!"


@dataclass(frozen=True)
class SeedOptions:
    method: str
    hostname: str = "boxer"
    username: str = "boxer"
    ssh_authorized_keys: list[str] = field(default_factory=list)
    packages: list[str] = field(default_factory=list)
    password_hash: Optional[str] = None  # crypt(5) hash; locked if omitted


def render_seed(opts: SeedOptions) -> Optional[tuple[str, str]]:
    """Return ``(filename, contents)`` for the seed, or ``None`` for manual installs."""
    method = opts.method.lower()
    if method not in SUPPORTED_METHODS:
        raise ValueError(f"Unsupported install method '{opts.method}'")
    if method == "manual":
        return None
    if method == "ubuntu":
        return "user-data", _render_autoinstall(opts)
    if method == "debian":
        return "preseed.cfg", _render_preseed(opts)
    return "ks.cfg", _render_kickstart(opts)


def _render_autoinstall(opts: SeedOptions) -> str:
    autoinstall: dict[str, object] = {
        "version": 1,
        "identity": {
            "hostname": opts.hostname,
            "username": opts.username,
            "password": opts.password_hash or _LOCKED_PASSWORD,
        },
        "ssh": {
            "install-server": True,
            "allow-pw": opts.password_hash is not None,
            "authorized-keys": list(opts.ssh_authorized_keys),
        },
        "packages": _dedupe(["qemu-guest-agent", *opts.packages]),
        "late-commands": [
            "curtin in-target --target=/target -- systemctl enable qemu-guest-agent",
        ],
    }
    return "#cloud-config\n" + yaml.safe_dump({"autoinstall": autoinstall}, sort_keys=False)


def _render_preseed(opts: SeedOptions) -> str:
    keys = "\n".join(opts.ssh_authorized_keys)
    lines = [
        "d-i debian-installer/locale string en_US",
        "d-i keyboard-configuration/xkb-keymap select us",
        f"d-i netcfg/get_hostname string {opts.hostname}",
        "d-i netcfg/get_domain string local",
        "d-i passwd/root-login boolean false",
        f"d-i passwd/username string {opts.username}",
        "d-i passwd/user-uid string 1000",
        "d-i user-setup/allow-password-weak boolean false",
        "d-i pkgsel/include string openssh-server qemu-guest-agent sudo "
        + " ".join(opts.packages),
        "d-i grub-installer/only_debian boolean true",
        "d-i finish-install/reboot_in_progress note",
        f"d-i preseed/late_command string "
        f"in-target mkdir -p /home/{opts.username}/.ssh; "
        f"echo '{keys}' > /target/home/{opts.username}/.ssh/authorized_keys; "
        f"in-target chown -R {opts.username}:{opts.username} /home/{opts.username}/.ssh",
    ]
    return "\n".join(lines) + "\n"


def _render_kickstart(opts: SeedOptions) -> str:
    keys_block = "\n".join(
        f'sshkey --username={opts.username} "{key}"' for key in opts.ssh_authorized_keys
    )
    pkgs = "\n".join(["@^minimal-environment", "openssh-server", "qemu-guest-agent", *opts.packages])
    pw_line = (
        f"rootpw --iscrypted --lock {opts.password_hash}"
        if opts.password_hash
        else "rootpw --lock"
    )
    return (
        "text\n"
        "reboot\n"
        f"network --bootproto=dhcp --hostname={opts.hostname}\n"
        f"{pw_line}\n"
        f"user --name={opts.username} --groups=wheel --lock\n"
        f"{keys_block}\n"
        "bootloader --location=mbr\n"
        "clearpart --all --initlabel\n"
        "autopart\n"
        "%packages\n"
        f"{pkgs}\n"
        "%end\n"
        "%post\n"
        f"systemctl enable qemu-guest-agent sshd\n"
        "%end\n"
    )


def _dedupe(items: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for item in items:
        if item and item not in seen:
            seen.add(item)
            out.append(item)
    return out


class InstallerSeedBuilder:
    async def build(self, opts: SeedOptions, dest_dir: Path) -> Optional[Path]:
        """Render the seed and pack it into a labelled ISO, or ``None`` for manual."""
        rendered = render_seed(opts)
        if rendered is None:
            return None
        filename, contents = rendered
        method = opts.method.lower()
        volid = _VOLID[method]
        iso_path = dest_dir / "seed.iso"
        if iso_path.exists():
            return iso_path
        dest_dir.mkdir(parents=True, exist_ok=True)

        with tempfile.TemporaryDirectory() as tmpdir:
            files = [Path(tmpdir) / filename]
            files[0].write_text(contents)
            # NoCloud requires an (empty) meta-data file alongside user-data.
            if method == "ubuntu":
                meta = Path(tmpdir) / "meta-data"
                meta.write_text(f"instance-id: {opts.hostname}\nlocal-hostname: {opts.hostname}\n")
                files.append(meta)
            await self._run_genisoimage(volid, files, iso_path)

        logger.info("Installer seed ISO (%s) created at %s", method, iso_path)
        return iso_path

    @staticmethod
    async def _run_genisoimage(volid: str, files: list[Path], dest: Path) -> None:
        for cmd in ("genisoimage", "mkisofs"):
            try:
                loop = asyncio.get_running_loop()
                await loop.run_in_executor(
                    None,
                    lambda c=cmd: subprocess.run(
                        [c, "-output", str(dest), "-volid", volid, "-joliet", "-rock",
                         *[str(f) for f in files]],
                        check=True,
                        capture_output=True,
                    ),
                )
                return
            except FileNotFoundError:
                continue
            except subprocess.CalledProcessError as exc:
                raise RuntimeError(f"{cmd} failed: {exc.stderr.decode()}") from exc
        raise RuntimeError("Neither genisoimage nor mkisofs found; install one to build seed ISOs")
