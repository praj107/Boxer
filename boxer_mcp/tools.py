"""MCP tool definitions for Boxer VM control."""
from __future__ import annotations

from typing import Annotated, Any, Optional

from mcp.server.fastmcp import FastMCP

from boxer_mcp.server import ipc

mcp = FastMCP("boxer", instructions="Boxer QEMU/KVM VM control plane. Use box_resource_status first to check capacity before requesting VMs.")


@mcp.tool()
async def box_resource_status() -> dict[str, Any]:
    """Return current host CPU, RAM capacity and running VM counts."""
    return await ipc("resource.status", {})


@mcp.tool()
async def box_request_vm(
    template: Annotated[str, "OS template name (e.g. ubuntu-24.04, debian-12, fedora-40)"],
    purpose: Annotated[str, "Short name/purpose for the VM, used as hostname prefix"],
    headless: Annotated[bool, "True for no display (CLI-only), False for SPICE display"] = True,
    ttl_minutes: Annotated[int, "Lease duration in minutes before VM is marked stale"] = 60,
    cpu: Annotated[Optional[int], "Number of vCPUs (default from template)"] = None,
    ram_mb: Annotated[Optional[int], "RAM in MiB (default from template)"] = None,
    disk_gb: Annotated[Optional[int], "Disk size in GiB (default from template)"] = None,
    tags: Annotated[Optional[dict[str, str]], "Optional key-value tags"] = None,
) -> dict[str, Any]:
    """Request a new VM. Returns vm_id when created, or queued status if host is at capacity."""
    params: dict[str, Any] = {"template": template, "purpose": purpose, "headless": headless, "ttl_minutes": ttl_minutes}
    if cpu is not None:
        params["cpu"] = cpu
    if ram_mb is not None:
        params["ram_mb"] = ram_mb
    if disk_gb is not None:
        params["disk_gb"] = disk_gb
    if tags:
        params["tags"] = tags
    return await ipc("vm.request", params)


@mcp.tool()
async def box_list_vms(
    include_stale: Annotated[bool, "Include stopped VMs with expired leases"] = False,
) -> list[dict[str, Any]]:
    """List VMs belonging to the current project."""
    return await ipc("vm.list", {"include_stale": include_stale})


@mcp.tool()
async def box_get_vm(
    vm_id: Annotated[str, "VM ID (e.g. vm_6d92e4)"],
) -> dict[str, Any]:
    """Get details of a specific VM including live state and IP address."""
    return await ipc("vm.get", {"vm_id": vm_id})


@mcp.tool()
async def box_start_vm(
    vm_id: Annotated[str, "VM ID"],
) -> dict[str, Any]:
    """Start a stopped VM."""
    return await ipc("vm.start", {"vm_id": vm_id})


@mcp.tool()
async def box_stop_vm(
    vm_id: Annotated[str, "VM ID"],
    graceful: Annotated[bool, "True to send shutdown signal, False to force off"] = True,
) -> dict[str, Any]:
    """Stop a running VM."""
    return await ipc("vm.stop", {"vm_id": vm_id, "graceful": graceful})


@mcp.tool()
async def box_delete_vm(
    vm_id: Annotated[str, "VM ID — must be exact ID, not display name"],
) -> dict[str, Any]:
    """Permanently delete a VM and its storage. Requires the exact vm_id."""
    return await ipc("vm.delete", {"vm_id": vm_id})


@mcp.tool()
async def box_extend_lease(
    vm_id: Annotated[str, "VM ID"],
    ttl_minutes: Annotated[int, "Additional minutes to add to the lease"] = 60,
) -> dict[str, Any]:
    """Extend the lease of a VM to prevent it from being marked stale."""
    return await ipc("vm.extend_lease", {"vm_id": vm_id, "ttl_minutes": ttl_minutes})


@mcp.tool()
async def box_snapshot(
    vm_id: Annotated[str, "VM ID"],
    label: Annotated[str, "Human-readable snapshot label"] = "snap",
) -> dict[str, Any]:
    """Create a snapshot of a VM's current disk state."""
    return await ipc("vm.snapshot", {"vm_id": vm_id, "label": label})


@mcp.tool()
async def box_exec(
    vm_id: Annotated[str, "VM ID"],
    command: Annotated[str, "Shell command to run inside the VM"],
    timeout_seconds: Annotated[int, "Max execution time in seconds"] = 30,
) -> dict[str, Any]:
    """Run a command inside a VM via SSH. Returns stdout, stderr, exit_code."""
    return await ipc("vm.exec", {"vm_id": vm_id, "command": command, "timeout_seconds": timeout_seconds})


@mcp.tool()
async def box_screenshot(
    vm_id: Annotated[str, "VM ID"],
) -> dict[str, Any]:
    """Capture a screenshot of a headed VM. Returns base64-encoded PNG."""
    return await ipc("vm.screenshot", {"vm_id": vm_id})


@mcp.tool()
async def box_input(
    vm_id: Annotated[str, "VM ID"],
    actions: Annotated[list[dict[str, Any]], "List of input actions: {type: 'key'|'type', keys: [...]} or {type: 'type', text: '...'}"],
) -> dict[str, Any]:
    """Send keyboard or mouse input to a headed VM."""
    return await ipc("vm.input", {"vm_id": vm_id, "actions": actions})


@mcp.tool()
async def box_queue_status() -> dict[str, Any]:
    """Check the VM creation queue for the current project."""
    return await ipc("queue.status", {})


@mcp.tool()
async def box_cleanup_plan() -> dict[str, Any]:
    """List stale VMs that can be cleaned up in the current project."""
    return await ipc("cleanup.plan", {})
