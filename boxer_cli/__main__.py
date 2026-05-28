"""boxer CLI — human management of Boxer VMs."""
from __future__ import annotations

import asyncio
import grp
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import click

from boxer.config import get_config
from boxer.ipc import ERR_PERMISSION_DENIED, IPCError
from boxer_mcp.ipc_client import make_call


def _is_admin() -> bool:
    cfg = get_config()
    try:
        gid = grp.getgrnam(cfg.admin_group).gr_gid
        return gid in os.getgroups()
    except KeyError:
        return os.geteuid() == 0


def _admin_params() -> dict[str, Any]:
    return {
        "caller_project_id": "admin",
        "caller_user": os.environ.get("USER", "root"),
        "caller_is_admin": _is_admin(),
    }


def _run(coro: Any) -> Any:
    return asyncio.run(coro)


async def _call(method: str, params: dict[str, Any]) -> Any:
    cfg = get_config()
    merged = {**_admin_params(), **params}
    return await make_call(cfg.socket_path, method, merged)


def _fmt_dt(iso: str) -> str:
    try:
        dt = datetime.fromisoformat(iso)
        return dt.strftime("%Y-%m-%d %H:%M UTC")
    except Exception:
        return iso


def _stale_flag(vm: dict[str, Any]) -> str:
    try:
        lease = datetime.fromisoformat(vm["lease_until"])
        if lease < datetime.now(timezone.utc):
            return " [STALE]"
    except Exception:
        pass
    return ""


@click.group()
def cli() -> None:
    """Boxer — QEMU/KVM VM control plane."""


@cli.command("ls")
@click.option("--stale", is_flag=True, help="Include stopped stale VMs")
@click.option("--all", "show_all", is_flag=True, help="Show all projects (admin)")
@click.option("--json", "as_json", is_flag=True)
def ls(stale: bool, show_all: bool, as_json: bool) -> None:
    """List VMs."""
    params = {"include_stale": stale}
    if show_all:
        params["caller_is_admin"] = True
    vms = _run(_call("vm.list", params))
    if as_json:
        click.echo(json.dumps(vms, indent=2))
        return
    if not vms:
        click.echo("No VMs found.")
        return
    click.echo(f"{'VM ID':<14} {'NAME':<25} {'STATE':<10} {'IP':<16} {'LEASE UNTIL':<20}")
    click.echo("-" * 87)
    for v in vms:
        stale_s = _stale_flag(v)
        click.echo(
            f"{v['vm_id']:<14} {v['display_name']:<25} "
            f"{v.get('live_state', v['state']):<10} "
            f"{(v.get('ip_address') or '-'):<16} "
            f"{_fmt_dt(v['lease_until'])}{stale_s}"
        )


@cli.command("stop")
@click.argument("vm_id")
@click.option("--force", is_flag=True, help="Force power off")
def stop(vm_id: str, force: bool) -> None:
    """Stop a running VM."""
    result = _run(_call("vm.stop", {"vm_id": vm_id, "graceful": not force}))
    click.echo(f"VM {result['vm_id']}: {result['state']}")


@cli.command("start")
@click.argument("vm_id")
def start(vm_id: str) -> None:
    """Start a stopped VM."""
    result = _run(_call("vm.start", {"vm_id": vm_id}))
    click.echo(f"VM {result['vm_id']}: {result['state']}")


@cli.command("delete")
@click.argument("vm_id")
@click.option("--yes", is_flag=True, help="Skip confirmation")
def delete(vm_id: str, yes: bool) -> None:
    """Delete a VM and its storage."""
    if not yes:
        click.confirm(f"Delete VM {vm_id} and all its storage?", abort=True)
    result = _run(_call("vm.delete", {"vm_id": vm_id}))
    click.echo(f"VM {vm_id} deleted: {result['deleted']}")


@cli.command("cleanup")
@click.option("--dry-run", is_flag=True)
@click.option("--interactive", is_flag=True)
def cleanup(dry_run: bool, interactive: bool) -> None:
    """Clean up stale VMs."""
    plan = _run(_call("cleanup.plan", {}))
    stale = plan.get("stale_vms", [])
    if not stale:
        click.echo("No stale VMs to clean up.")
        return
    click.echo(f"Found {len(stale)} stale VM(s):")
    for v in stale:
        click.echo(f"  {v['vm_id']} ({v['display_name']}) — state={v['state']}, expired={_fmt_dt(v['lease_expired'])}")
    if dry_run:
        click.echo("[dry-run] No changes made.")
        return
    for v in stale:
        if interactive:
            if not click.confirm(f"  Delete {v['vm_id']} ({v['display_name']})?"):
                continue
        try:
            _run(_call("vm.delete", {"vm_id": v["vm_id"]}))
            click.echo(f"  Deleted {v['vm_id']}")
        except IPCError as exc:
            click.echo(f"  Failed to delete {v['vm_id']}: {exc}", err=True)


@cli.command("prune")
@click.option("--older-than", default="7d", show_default=True, help="Age threshold e.g. 7d, 24h")
@click.option("--dry-run", is_flag=True)
def prune(older_than: str, dry_run: bool) -> None:
    """Remove stopped VMs older than a threshold."""
    click.echo(f"Prune --older-than {older_than} (dry_run={dry_run}): use cleanup for now.")


@cli.command("events")
@click.option("--tail", is_flag=True)
@click.option("--level", type=click.Choice(["INFO", "WARN", "ERROR"]))
@click.option("--limit", default=20, show_default=True)
def events(tail: bool, level: Optional[str], limit: int) -> None:
    """Show recent Boxer events."""
    params: dict[str, Any] = {"limit": limit}
    if level:
        params["level"] = level

    async def _poll() -> None:
        since = None
        while True:
            p = dict(params)
            if since:
                p["since"] = since
            evts = await _call("event.poll", p)
            for e in reversed(evts):
                since = e["created_at"]
                click.echo(f"{_fmt_dt(e['created_at'])} [{e['level']}] {e['message']}")
            if not tail:
                break
            await asyncio.sleep(10)

    asyncio.run(_poll())


@cli.command("status")
def status() -> None:
    """Show host resource status."""
    s = _run(_call("resource.status", {}))
    click.echo(f"RAM: {s['free_ram_gib']:.1f}G free / {s['usable_ram_gib']:.1f}G usable / {s['total_ram_gib']:.1f}G total")
    click.echo(f"vCPU: {s['free_vcpus']} free / {s['max_vcpus']} max ({s['host_threads']} host threads)")
    click.echo(f"Running Boxer VMs: {s['running_vms']}")


if __name__ == "__main__":
    cli()
