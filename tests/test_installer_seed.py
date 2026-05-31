"""Tests for unattended-install seed rendering."""
from __future__ import annotations

import pytest
import yaml

from boxerd.installer_seed import SeedOptions, render_seed


def _opts(method: str, **kw) -> SeedOptions:
    return SeedOptions(
        method=method,
        hostname="ci",
        ssh_authorized_keys=["ssh-ed25519 AAAAKEY boxer"],
        **kw,
    )


def test_manual_method_has_no_seed() -> None:
    assert render_seed(_opts("manual")) is None


def test_unsupported_method_raises() -> None:
    with pytest.raises(ValueError):
        render_seed(_opts("gentoo"))


def test_ubuntu_autoinstall_is_valid_cloud_config() -> None:
    filename, contents = render_seed(_opts("ubuntu", packages=["git"]))
    assert filename == "user-data"
    assert contents.startswith("#cloud-config\n")
    doc = yaml.safe_load(contents)
    ai = doc["autoinstall"]
    assert ai["version"] == 1
    assert ai["identity"]["hostname"] == "ci"
    assert "ssh-ed25519 AAAAKEY boxer" in ai["ssh"]["authorized-keys"]
    assert "git" in ai["packages"]
    assert "qemu-guest-agent" in ai["packages"]
    # No password provided → locked, and password auth disabled.
    assert ai["ssh"]["allow-pw"] is False


def test_debian_preseed_contains_user_and_keys() -> None:
    filename, contents = render_seed(_opts("debian"))
    assert filename == "preseed.cfg"
    assert "d-i passwd/username string boxer" in contents
    assert "ssh-ed25519 AAAAKEY boxer" in contents
    assert "qemu-guest-agent" in contents


def test_fedora_kickstart_contains_sshkey_directive() -> None:
    filename, contents = render_seed(_opts("fedora", packages=["tmux"]))
    assert filename == "ks.cfg"
    assert 'sshkey --username=boxer "ssh-ed25519 AAAAKEY boxer"' in contents
    assert "qemu-guest-agent" in contents
    assert "tmux" in contents
    assert "rootpw --lock" in contents


def test_password_hash_enables_password_auth_for_ubuntu() -> None:
    _, contents = render_seed(_opts("ubuntu", password_hash="$6$abc$def"))
    doc = yaml.safe_load(contents)
    assert doc["autoinstall"]["identity"]["password"] == "$6$abc$def"
    assert doc["autoinstall"]["ssh"]["allow-pw"] is True
