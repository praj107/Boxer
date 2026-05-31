"""Tests for MCP tool layer with mocked IPC client."""
from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest


@pytest.mark.asyncio
async def test_box_resource_status_calls_ipc() -> None:
    with patch("boxer_mcp.tools.ipc", new_callable=AsyncMock) as mock_ipc:
        mock_ipc.return_value = {"free_ram_gib": 8.0, "running_vms": 0}
        from boxer_mcp.tools import box_resource_status
        result = await box_resource_status()
        mock_ipc.assert_called_once_with("resource.status", {})
        assert result["free_ram_gib"] == 8.0


@pytest.mark.asyncio
async def test_box_request_vm_passes_params() -> None:
    with patch("boxer_mcp.tools.ipc", new_callable=AsyncMock) as mock_ipc:
        mock_ipc.return_value = {"status": "running", "vm_id": "vm_abc123"}
        from boxer_mcp.tools import box_request_vm
        result = await box_request_vm(
            template="ubuntu-24.04",
            purpose="ci-runner",
            headless=True,
            ttl_minutes=30,
            bootstrap_packages=["git"],
            bootstrap_commands=["echo ready"],
            ssh_access=True,
            wait_for_ip_seconds=10,
        )
        call_args = mock_ipc.call_args
        assert call_args[0][0] == "vm.request"
        params = call_args[0][1]
        assert params["template"] == "ubuntu-24.04"
        assert params["purpose"] == "ci-runner"
        assert params["ttl_minutes"] == 30
        assert params["bootstrap_packages"] == ["git"]
        assert params["bootstrap_commands"] == ["echo ready"]
        assert params["ssh_access"] is True
        assert params["wait_for_ip_seconds"] == 10


@pytest.mark.asyncio
async def test_box_request_vm_passes_profiles_and_wait() -> None:
    with patch("boxer_mcp.tools.ipc", new_callable=AsyncMock) as mock_ipc:
        mock_ipc.return_value = {"vm_id": "vm_abc"}
        from boxer_mcp.tools import box_request_vm
        await box_request_vm(
            template="ubuntu-24.04",
            purpose="dev",
            profiles=["python", "docker"],
            write_files=[{"path": "/etc/app.conf", "content": "k=v"}],
            secret_files=[{"path": "/run/token", "content": "s3cr3t"}],
            wait_for=["ssh", "cloud_init"],
            wait_timeout_seconds=300,
        )
        params = mock_ipc.call_args[0][1]
        assert params["profiles"] == ["python", "docker"]
        assert params["write_files"][0]["path"] == "/etc/app.conf"
        assert params["secret_files"][0]["content"] == "s3cr3t"
        assert params["wait_for"] == ["ssh", "cloud_init"]
        assert params["wait_timeout_seconds"] == 300


@pytest.mark.asyncio
async def test_box_list_profiles_unwraps_result() -> None:
    with patch("boxer_mcp.tools.ipc", new_callable=AsyncMock) as mock_ipc:
        mock_ipc.return_value = {"profiles": [{"name": "python", "description": "Py", "packages": ["python3"]}]}
        from boxer_mcp.tools import box_list_profiles
        rows = await box_list_profiles()
        mock_ipc.assert_called_once_with("profile.list", {})
        assert rows[0]["name"] == "python"


@pytest.mark.asyncio
async def test_box_list_images_unwraps_result() -> None:
    with patch("boxer_mcp.tools.ipc", new_callable=AsyncMock) as mock_ipc:
        mock_ipc.return_value = {"images": [{"template": "ubuntu-24.04", "family": "ubuntu"}]}
        from boxer_mcp.tools import box_list_images
        rows = await box_list_images()
        mock_ipc.assert_called_once_with("image.list", {})
        assert rows[0]["family"] == "ubuntu"


@pytest.mark.asyncio
async def test_box_prewarm_image_passes_template() -> None:
    with patch("boxer_mcp.tools.ipc", new_callable=AsyncMock) as mock_ipc:
        mock_ipc.return_value = {"template": "fedora-44", "cached": True}
        from boxer_mcp.tools import box_prewarm_image
        result = await box_prewarm_image(template="fedora-44")
        assert mock_ipc.call_args[0] == ("image.prewarm", {"template": "fedora-44"})
        assert result["cached"] is True


@pytest.mark.asyncio
async def test_box_request_installer_passes_params() -> None:
    with patch("boxer_mcp.tools.ipc", new_callable=AsyncMock) as mock_ipc:
        mock_ipc.return_value = {"vm_id": "vm_iso01", "install_state": "installing"}
        from boxer_mcp.tools import box_request_installer
        result = await box_request_installer(
            template="arch-latest",
            purpose="arch-box",
            disk_gb=40,
            ssh_public_keys=["ssh-ed25519 AAAA boxer"],
        )
        assert mock_ipc.call_args[0][0] == "vm.request_installer"
        params = mock_ipc.call_args[0][1]
        assert params["template"] == "arch-latest"
        assert params["purpose"] == "arch-box"
        assert params["disk_gb"] == 40
        assert params["ssh_public_keys"] == ["ssh-ed25519 AAAA boxer"]
        assert result["install_state"] == "installing"


@pytest.mark.asyncio
async def test_box_delete_vm_requires_id() -> None:
    with patch("boxer_mcp.tools.ipc", new_callable=AsyncMock) as mock_ipc:
        mock_ipc.return_value = {"deleted": True}
        from boxer_mcp.tools import box_delete_vm
        await box_delete_vm(vm_id="vm_abc123")
        call_args = mock_ipc.call_args
        assert call_args[0][1]["vm_id"] == "vm_abc123"


@pytest.mark.asyncio
async def test_box_exec_passes_command() -> None:
    with patch("boxer_mcp.tools.ipc", new_callable=AsyncMock) as mock_ipc:
        mock_ipc.return_value = {"stdout": "Linux\n", "stderr": "", "exit_code": 0}
        from boxer_mcp.tools import box_exec
        result = await box_exec(vm_id="vm_abc123", command="uname -s")
        assert result["stdout"] == "Linux\n"
        params = mock_ipc.call_args[0][1]
        assert params["command"] == "uname -s"
        assert params["vm_id"] == "vm_abc123"


@pytest.mark.asyncio
async def test_box_list_vms() -> None:
    with patch("boxer_mcp.tools.ipc", new_callable=AsyncMock) as mock_ipc:
        mock_ipc.return_value = [{"vm_id": "vm_abc", "display_name": "test"}]
        from boxer_mcp.tools import box_list_vms
        vms = await box_list_vms()
        assert len(vms) == 1
        assert vms[0]["vm_id"] == "vm_abc"


@pytest.mark.asyncio
async def test_box_restart_vm_calls_ipc() -> None:
    with patch("boxer_mcp.tools.ipc", new_callable=AsyncMock) as mock_ipc:
        mock_ipc.return_value = {"vm_id": "vm_abc", "state": "running"}
        from boxer_mcp.tools import box_restart_vm
        await box_restart_vm(vm_id="vm_abc", graceful=False, wait_for_ip_seconds=5)
        mock_ipc.assert_called_once_with(
            "vm.restart",
            {"vm_id": "vm_abc", "graceful": False, "wait_for_ip_seconds": 5},
        )


@pytest.mark.asyncio
async def test_box_get_ssh_access_calls_ipc() -> None:
    with patch("boxer_mcp.tools.ipc", new_callable=AsyncMock) as mock_ipc:
        mock_ipc.return_value = {"username": "boxer"}
        from boxer_mcp.tools import box_get_ssh_access
        await box_get_ssh_access(vm_id="vm_abc", include_private_key=True, create=False)
        params = mock_ipc.call_args[0][1]
        assert mock_ipc.call_args[0][0] == "vm.ssh_access"
        assert params["vm_id"] == "vm_abc"
        assert params["include_private_key"] is True
        assert params["create"] is False
