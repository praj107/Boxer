"""Tests for named bootstrap profiles and first-boot file/secret injection."""
from __future__ import annotations

import base64

import pytest
import yaml

from boxer.ipc import ERR_INVALID_PARAMS, IPCError
from boxerd.cloud_init import CloudInitOptions, WriteFile, _render_user_data
from boxerd.daemon import _bootstrap_from_params, _parse_wait_conditions
from boxerd.profiles import MAX_PROFILES, available_profiles, resolve_profiles


# ---------------------------------------------------------------- profiles

def test_resolve_single_profile() -> None:
    merged = resolve_profiles(["python"])
    assert "python3" in merged["packages"]
    assert "git" in merged["packages"]


def test_resolve_merges_and_dedupes() -> None:
    merged = resolve_profiles(["python", "node"])
    # git is in both profiles but should appear once.
    assert merged["packages"].count("git") == 1
    assert "python3" in merged["packages"]
    assert "nodejs" in merged["packages"]


def test_unknown_profile_rejected() -> None:
    with pytest.raises(IPCError) as exc:
        resolve_profiles(["python", "rust"])
    assert exc.value.code == ERR_INVALID_PARAMS


def test_too_many_profiles_rejected() -> None:
    with pytest.raises(IPCError):
        resolve_profiles(["python"] * (MAX_PROFILES + 1))


def test_available_profiles_shape() -> None:
    profiles = {p["name"] for p in available_profiles()}
    assert {"python", "node", "docker", "browser", "desktop"} <= profiles


# ---------------------------------------------------------------- bootstrap wiring

def test_bootstrap_expands_profiles_into_packages_and_commands() -> None:
    bootstrap = _bootstrap_from_params({"profiles": ["docker"], "bootstrap_packages": ["htop"]})
    assert "docker.io" in bootstrap["packages"]
    assert "htop" in bootstrap["packages"]
    assert any("usermod -aG docker" in c for c in bootstrap["commands"])
    assert bootstrap["profiles"] == ["docker"]


def test_bootstrap_parses_write_files() -> None:
    bootstrap = _bootstrap_from_params(
        {"write_files": [{"path": "/etc/app.conf", "content": "k=v", "permissions": "0640"}]}
    )
    assert len(bootstrap["write_files"]) == 1
    wf = bootstrap["write_files"][0]
    assert wf.path == "/etc/app.conf"
    assert wf.permissions == "0640"
    assert bootstrap["secret_count"] == 0


def test_bootstrap_secret_files_default_mode_and_wipe_command() -> None:
    bootstrap = _bootstrap_from_params(
        {"secret_files": [{"path": "/run/token", "content": "s3cr3t"}]}
    )
    assert bootstrap["secret_count"] == 1
    secret = bootstrap["write_files"][0]
    assert secret.permissions == "0600"
    assert secret.owner == "boxer:boxer"
    # A late command wipes the persisted cloud-init copy of the secret.
    assert any("var/lib/cloud/instances" in c for c in bootstrap["commands"])


def test_relative_path_rejected() -> None:
    with pytest.raises(IPCError):
        _bootstrap_from_params({"write_files": [{"path": "etc/app.conf", "content": "x"}]})


def test_bad_permissions_rejected() -> None:
    with pytest.raises(IPCError):
        _bootstrap_from_params({"write_files": [{"path": "/a", "content": "x", "permissions": "rwx"}]})


def test_oversize_file_rejected() -> None:
    big = "x" * (256 * 1024 + 1)
    with pytest.raises(IPCError):
        _bootstrap_from_params({"write_files": [{"path": "/big", "content": big}]})


# ---------------------------------------------------------------- cloud-init rendering

def test_write_files_rendered_base64() -> None:
    user_data = _render_user_data(
        "host",
        CloudInitOptions(write_files=[WriteFile(path="/etc/x", content="hello", permissions="0600")]),
    )
    assert user_data.startswith("#cloud-config\n")
    doc = yaml.safe_load(user_data)
    wf = doc["write_files"][0]
    assert wf["path"] == "/etc/x"
    assert wf["encoding"] == "b64"
    assert base64.b64decode(wf["content"]).decode() == "hello"
    assert wf["permissions"] == "0600"


# ---------------------------------------------------------------- wait conditions

def test_parse_wait_conditions_dedupes() -> None:
    assert _parse_wait_conditions(["ssh", "ssh", "cloud_init"]) == ["ssh", "cloud_init"]


def test_parse_wait_conditions_rejects_unknown() -> None:
    with pytest.raises(IPCError) as exc:
        _parse_wait_conditions(["teleport"])
    assert exc.value.code == ERR_INVALID_PARAMS


def test_parse_wait_conditions_empty() -> None:
    assert _parse_wait_conditions(None) == []
