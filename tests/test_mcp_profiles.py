"""Tests for Boxer MCP operating profiles and client-neutral project identity."""
from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from boxer_mcp.profiles import resolve_mcp_profile
from boxer_mcp.server import _derive_project_id, get_caller_params
from boxer_mcp.tools import build_mcp, box_request_vm_compact


@pytest.mark.asyncio
async def test_full_profiles_expose_all_tools() -> None:
    for profile_name in ("codex", "claude", "full", "rich"):
        tools = await build_mcp(profile_name).list_tools()
        names = {tool.name for tool in tools}
        assert len(names) == 34
        assert "box_request_lab" in names
        assert "box_request_installer" in names
        assert "box_get_ssh_access" in names
        assert "box_purge_ghost" in names
        # Guest-utils (Milestone 9) are part of the rich surface.
        assert "box_sysinfo" in names
        assert "box_packages" in names


@pytest.mark.asyncio
async def test_low_context_profiles_expose_compact_toolset() -> None:
    for profile_name in ("qwen", "deepseek", "kimi", "oss", "low-context"):
        tools = await build_mcp(profile_name).list_tools()
        names = {tool.name for tool in tools}
        assert len(names) == 20
        assert "box_request_lab" in names
        assert "box_request_vm" in names
        assert "box_exec" in names
        assert "box_push" in names and "box_pull" in names
        # Curated guest-utils subset for low-context callers.
        assert "box_sysinfo" in names
        assert "box_services" in names
        assert "box_request_installer" not in names
        assert "box_get_ssh_access" not in names
        assert "box_screenshot" not in names


@pytest.mark.asyncio
async def test_low_context_request_vm_schema_is_reduced() -> None:
    tools = await build_mcp("deepseek").list_tools()
    request_vm = next(tool for tool in tools if tool.name == "box_request_vm")
    assert set(request_vm.inputSchema["properties"]) == {
        "template",
        "purpose",
        "profiles",
        "headless",
        "ttl_minutes",
        "wait_for",
        "wait_timeout_seconds",
    }


@pytest.mark.asyncio
async def test_readonly_profile_exposes_no_mutating_tools() -> None:
    tools = await build_mcp("readonly").list_tools()
    names = {tool.name for tool in tools}
    assert len(names) == 15
    assert "box_list_labs" in names
    assert "box_lab_status" in names
    assert "box_resource_status" in names
    # Read-only guest-utils are allowed; mutating package ops are not.
    assert "box_sysinfo" in names
    assert "box_logs" in names
    assert "box_packages" not in names
    assert "box_exec" not in names
    assert "box_request_vm" not in names
    assert "box_request_lab" not in names
    assert "box_delete_vm" not in names


@pytest.mark.asyncio
async def test_tool_annotations_mark_read_and_destructive_tools() -> None:
    tools = {tool.name: tool for tool in await build_mcp("codex").list_tools()}
    assert tools["box_resource_status"].annotations.readOnlyHint is True
    assert tools["box_request_lab"].annotations.readOnlyHint is False
    assert tools["box_delete_vm"].annotations.destructiveHint is True
    assert tools["box_exec"].annotations.readOnlyHint is False


@pytest.mark.asyncio
async def test_compact_request_vm_passes_only_compact_params() -> None:
    with patch("boxer_mcp.tools.ipc", new_callable=AsyncMock) as mock_ipc:
        mock_ipc.return_value = {"vm_id": "vm_abc"}
        await box_request_vm_compact(
            template="ubuntu-24.04",
            purpose="agent",
            profiles=["python"],
            wait_for=["ssh"],
            wait_timeout_seconds=300,
        )
        assert mock_ipc.call_args[0] == (
            "vm.request",
            {
                "template": "ubuntu-24.04",
                "purpose": "agent",
                "headless": True,
                "ttl_minutes": 60,
                "profiles": ["python"],
                "wait_for": ["ssh"],
                "wait_timeout_seconds": 300,
            },
        )


def test_profile_aliases_and_unknown_profile() -> None:
    assert resolve_mcp_profile("moonshot").name == "kimi"
    assert resolve_mcp_profile("anthropic").name == "claude"
    with pytest.raises(ValueError):
        resolve_mcp_profile("giant-omnibus")


def test_list_profiles_cli_ignores_bad_default_profile(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from boxer_mcp.__main__ import main

    monkeypatch.setenv("BOXER_MCP_PROFILE", "not-a-profile")
    main(["--list-profiles"])
    out = capsys.readouterr().out
    assert '"name": "codex"' in out
    assert '"name": "qwen"' in out
    assert '"name": "readonly"' in out


def test_boxer_project_dir_overrides_claude_project_dir(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    boxer_dir = tmp_path / "boxer-project"
    claude_dir = tmp_path / "claude-project"
    monkeypatch.setenv("BOXER_PROJECT_DIR", str(boxer_dir))
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(claude_dir))

    params = get_caller_params()

    expected_path = str(boxer_dir.resolve())
    assert params["caller_project_path"] == expected_path
    assert params["caller_project_id"] == _derive_project_id(expected_path)


def test_claude_project_dir_remains_supported(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    claude_dir = tmp_path / "claude-project"
    monkeypatch.delenv("BOXER_PROJECT_DIR", raising=False)
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(claude_dir))

    params = get_caller_params()

    expected_path = str(claude_dir.resolve())
    assert params["caller_project_path"] == expected_path
    assert params["caller_project_id"] == _derive_project_id(expected_path)
