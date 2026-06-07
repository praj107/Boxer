"""Tests for cloud-init user-data rendering."""
from __future__ import annotations

import yaml

from boxerd.cloud_init import CloudInitOptions, _render_user_data


def _rendered(options: CloudInitOptions) -> dict:
    text = _render_user_data("agent-vm", options)
    assert text.startswith("#cloud-config\n")
    return yaml.safe_load(text.removeprefix("#cloud-config\n"))


def test_render_includes_default_ssh_and_guest_agent_packages() -> None:
    data = _rendered(CloudInitOptions())
    assert data["packages"] == ["qemu-guest-agent", "openssh-server"]
    assert any("qemu-guest-agent" in cmd for cmd in data["runcmd"])
    assert any("rc-service qemu-guest-agent start" in cmd for cmd in data["runcmd"])
    assert any("rc-service sshd start" in cmd for cmd in data["runcmd"])
    assert any("systemctl enable --now ssh" in cmd for cmd in data["runcmd"])


def test_render_includes_authorized_keys_and_bootstrap_inputs() -> None:
    data = _rendered(
        CloudInitOptions(
            ssh_authorized_keys=["ssh-ed25519 AAAA test"],
            packages=["git", "python3-pip"],
            runcmd=["echo ready >/tmp/ready"],
        )
    )
    user = data["users"][0]
    assert user["ssh_authorized_keys"] == ["ssh-ed25519 AAAA test"]
    assert data["packages"] == ["qemu-guest-agent", "openssh-server", "git", "python3-pip"]
    assert data["runcmd"][-1] == "echo ready >/tmp/ready"


def test_render_dedupes_packages() -> None:
    data = _rendered(CloudInitOptions(packages=["git", "git", "openssh-server"]))
    assert data["packages"] == ["qemu-guest-agent", "openssh-server", "git"]
