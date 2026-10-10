"""CLI tests for lab recipe commands."""
from __future__ import annotations

from unittest.mock import AsyncMock, patch

from click.testing import CliRunner

from boxer_cli.__main__ import cli


def test_labs_command_lists_recipes() -> None:
    runner = CliRunner()
    with patch("boxer_cli.__main__._call", new_callable=AsyncMock) as mock_call:
        mock_call.return_value = {
            "labs": [
                {
                    "name": "linux-repro",
                    "default_template": "ubuntu-24.04",
                    "ttl_minutes": 90,
                    "niche": "reproduce a bug",
                    "profiles": ["python"],
                    "recommended_templates": ["ubuntu-24.04"],
                }
            ]
        }
        result = runner.invoke(cli, ["labs"])

    assert result.exit_code == 0
    assert "linux-repro" in result.output
    mock_call.assert_awaited_once_with("lab.list", {})


def test_lab_request_command_passes_recipe_params() -> None:
    runner = CliRunner()
    with patch("boxer_cli.__main__._call", new_callable=AsyncMock) as mock_call:
        mock_call.return_value = {
            "vm_id": "vm_lab01",
            "state": "running",
            "lab": {
                "title": "Clean Linux Reproduction Lab",
                "name": "linux-repro",
                "next_steps": ["Run minimal repro commands with box_exec."],
            },
        }
        result = runner.invoke(
            cli,
            [
                "lab",
                "request",
                "linux-repro",
                "--purpose",
                "bug-123",
                "--size",
                "small",
                "--no-wait",
                "--tag",
                "ticket=ENG-42",
            ],
        )

    assert result.exit_code == 0
    mock_call.assert_awaited_once_with(
        "lab.request",
        {
            "lab": "linux-repro",
            "size": "small",
            "wait": False,
            "purpose": "bug-123",
            "tags": {"ticket": "ENG-42"},
        },
    )
    assert "vm_lab01" in result.output


def test_lab_status_command_passes_vm_id() -> None:
    runner = CliRunner()
    with patch("boxer_cli.__main__._call", new_callable=AsyncMock) as mock_call:
        mock_call.return_value = {
            "vm": {"vm_id": "vm_lab01", "live_state": "running", "ip_address": "10.0.0.5"},
            "lab": {"name": "linux-repro", "ready": True, "next_steps": []},
        }
        result = runner.invoke(cli, ["lab", "status", "vm_lab01"])

    assert result.exit_code == 0
    mock_call.assert_awaited_once_with("lab.status", {"vm_id": "vm_lab01"})
    assert "ready=True" in result.output
