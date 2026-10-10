"""End-to-end MCP stdio checks against a fake Boxer daemon socket."""
from __future__ import annotations

import asyncio
import json
import os
import sys
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client


@pytest.mark.asyncio
async def test_qwen_profile_stdio_discovery_and_daemon_call(tmp_path: Path) -> None:
    socket_path = tmp_path / "boxer.sock"
    config_path = tmp_path / "boxer.yaml"
    project_path = tmp_path / "project"
    config_path.write_text(f"state_dir: {tmp_path}\nsocket_path: {socket_path}\n")
    seen_requests: list[dict[str, Any]] = []

    async def handle_fake_daemon(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        line = await reader.readline()
        msg = json.loads(line.decode())
        seen_requests.append(msg)

        if msg["method"] == "resource.status":
            result: dict[str, Any] = {"free_ram_gib": 8.0, "running_vms": 0}
        elif msg["method"] == "lab.request":
            result = {
                "status": "running",
                "vm_id": "vm_lab01",
                "lab": {"name": msg["params"]["lab"], "vm_id": "vm_lab01"},
                "params": msg["params"],
            }
        elif msg["method"] == "vm.request":
            result = {"status": "running", "vm_id": "vm_e2e", "params": msg["params"]}
        else:
            result = {"ok": True, "method": msg["method"]}

        writer.write((json.dumps({"jsonrpc": "2.0", "id": msg["id"], "result": result}) + "\n").encode())
        await writer.drain()
        writer.close()
        await writer.wait_closed()

    daemon = await asyncio.start_unix_server(handle_fake_daemon, str(socket_path))
    repo_root = Path(__file__).resolve().parents[1]
    server = StdioServerParameters(
        command=sys.executable,
        args=["-m", "boxer_mcp", "--profile", "qwen"],
        cwd=repo_root,
        env={
            "BOXER_CONFIG_PATH": str(config_path),
            "BOXER_PROJECT_DIR": str(project_path),
            "USER": "e2e",
        },
    )

    async with daemon:
        with open(os.devnull, "w") as errlog:
            async with stdio_client(server, errlog=errlog) as (read, write):
                async with ClientSession(read, write, read_timeout_seconds=timedelta(seconds=5)) as session:
                    init = await session.initialize()
                    assert init.serverInfo.name == "boxer"
                    assert "compact KVM lab recipes" in init.instructions

                    tools = (await session.list_tools()).tools
                    tool_names = {tool.name for tool in tools}
                    assert len(tool_names) == 20
                    assert "box_request_lab" in tool_names
                    assert "box_request_vm" in tool_names
                    assert "box_request_installer" not in tool_names
                    request_vm = next(tool for tool in tools if tool.name == "box_request_vm")
                    assert "write_files" not in request_vm.inputSchema["properties"]

                    status = await session.call_tool("box_resource_status", {})
                    assert status.structuredContent == {"free_ram_gib": 8.0, "running_vms": 0}

                    lab = await session.call_tool(
                        "box_request_lab",
                        {"lab": "linux-repro", "purpose": "bug-123", "size": "small", "wait": False},
                    )
                    assert lab.structuredContent["vm_id"] == "vm_lab01"
                    assert lab.structuredContent["lab"]["name"] == "linux-repro"

                    created = await session.call_tool(
                        "box_request_vm",
                        {"template": "ubuntu-24.04", "purpose": "e2e", "profiles": ["python"]},
                    )
                    assert created.structuredContent["vm_id"] == "vm_e2e"

    assert [req["method"] for req in seen_requests] == ["resource.status", "lab.request", "vm.request"]
    lab_params = seen_requests[1]["params"]
    assert lab_params["lab"] == "linux-repro"
    assert lab_params["purpose"] == "bug-123"
    assert lab_params["size"] == "small"
    assert lab_params["wait"] is False

    create_params = seen_requests[2]["params"]
    assert create_params["caller_project_path"] == str(project_path)
    assert create_params["caller_user"] == "e2e"
    assert create_params["template"] == "ubuntu-24.04"
    assert create_params["profiles"] == ["python"]
    assert set(create_params) == {
        "caller_project_id",
        "caller_project_path",
        "caller_user",
        "caller_is_admin",
        "template",
        "purpose",
        "headless",
        "ttl_minutes",
        "profiles",
    }
