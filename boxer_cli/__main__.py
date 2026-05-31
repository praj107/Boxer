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
        install_s = ""
        if v.get("install_state") in ("installing", "failed"):
            install_s = f" [install:{v['install_state']}]"
        click.echo(
            f"{v['vm_id']:<14} {v['display_name']:<25} "
            f"{v.get('live_state', v['state']):<10} "
            f"{(v.get('ip_address') or '-'):<16} "
            f"{_fmt_dt(v['lease_until'])}{stale_s}{install_s}"
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
@click.option("--wait-ip", "wait_for_ip_seconds", default=0, type=int,
              help="Wait up to N seconds for guest-agent IP discovery")
def start(vm_id: str, wait_for_ip_seconds: int) -> None:
    """Start a stopped VM."""
    result = _run(_call("vm.start", {"vm_id": vm_id, "wait_for_ip_seconds": wait_for_ip_seconds}))
    ip = result.get("ip_address") or "-"
    click.echo(f"VM {result['vm_id']}: {result['state']}  ip={ip}")


@cli.command("restart")
@click.argument("vm_id")
@click.option("--force", is_flag=True, help="Force power off before starting")
@click.option("--wait-ip", "wait_for_ip_seconds", default=0, type=int,
              help="Wait up to N seconds for guest-agent IP discovery")
def restart(vm_id: str, force: bool, wait_for_ip_seconds: int) -> None:
    """Restart a VM."""
    result = _run(_call(
        "vm.restart",
        {
            "vm_id": vm_id,
            "graceful": not force,
            "wait_for_ip_seconds": wait_for_ip_seconds,
        },
    ))
    ip = result.get("ip_address") or "-"
    click.echo(f"VM {result['vm_id']}: {result['state']}  ip={ip}")


@cli.command("ssh-access")
@click.argument("vm_id")
@click.option("--show-private-key", is_flag=True, help="Print private key material")
@click.option("--no-create", is_flag=True, help="Do not create a key if none exists")
@click.option("--timeout", "timeout_seconds", default=30, show_default=True)
def ssh_access(vm_id: str, show_private_key: bool, no_create: bool, timeout_seconds: int) -> None:
    """Create or show direct SSH access details for a VM."""
    result = _run(_call(
        "vm.ssh_access",
        {
            "vm_id": vm_id,
            "include_private_key": show_private_key,
            "create": not no_create,
            "timeout_seconds": timeout_seconds,
        },
    ))
    click.echo(f"user: {result['username']}")
    click.echo(f"host: {result.get('host') or '-'}")
    click.echo(f"key:  {result['private_key_path']}")
    click.echo(f"cmd:  {result['ssh_command']}")
    if show_private_key:
        click.echo("\nprivate_key:")
        click.echo(result["private_key"])


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


@cli.command("scan")
@click.option("--json", "as_json", is_flag=True, help="Output JSON")
def scan(as_json: bool) -> None:
    """List all unmanaged libvirt domains and ghost DB records (read-only)."""
    report = _run(_call("vm.scan", {}))
    if as_json:
        click.echo(json.dumps(report, indent=2))
        return

    orphaned = report.get("orphaned_boxer", [])
    foreign = report.get("foreign_vms", [])
    ghosts = report.get("ghost_records", [])

    if not orphaned and not foreign and not ghosts:
        click.echo("All domains are accounted for — nothing to reconcile.")
        return

    if orphaned:
        click.echo(f"\nOrphaned Boxer domains ({len(orphaned)}) — run 'boxer adopt <name>' to recover:")
        for o in orphaned:
            active = "running" if o.get("is_active") else "stopped"
            click.echo(f"  {o['libvirt_name']}  project={o['parsed_project_id']}  {active}  disk={o['disk_gb']}G")

    if foreign:
        click.echo(f"\nForeign (unmanaged) domains ({len(foreign)}) — run 'boxer import <name>' to manage:")
        for f in foreign:
            active = "running" if f.get("is_active") else "stopped"
            click.echo(f"  {f['libvirt_name']}  {active}  disk={f['disk_gb']}G")

    if ghosts:
        click.echo(f"\nGhost DB records ({len(ghosts)}) — run 'boxer reconcile --fix' to purge:")
        for g in ghosts:
            click.echo(f"  {g['vm_id']}  libvirt_name={g['libvirt_name']}  project={g['project_id']}")


@cli.command("reconcile")
@click.option("--fix", is_flag=True, help="Auto-adopt Boxer orphans and purge ghost DB records")
@click.option("--json", "as_json", is_flag=True, help="Output JSON (implies dry-run)")
def reconcile(fix: bool, as_json: bool) -> None:
    """Bidirectional reconciliation report. Use --fix to apply safe automated changes."""
    report = _run(_call("vm.scan", {}))

    if as_json:
        click.echo(json.dumps(report, indent=2))
        return

    orphaned = report.get("orphaned_boxer", [])
    foreign = report.get("foreign_vms", [])
    ghosts = report.get("ghost_records", [])

    click.echo(f"Orphaned Boxer domains : {len(orphaned)}")
    click.echo(f"Foreign domains        : {len(foreign)}")
    click.echo(f"Ghost DB records       : {len(ghosts)}")

    if not fix:
        if orphaned:
            click.echo("\n[DRY RUN] Would auto-adopt:")
            for o in orphaned:
                click.echo(f"  boxer adopt {o['libvirt_name']}")
        if ghosts:
            click.echo("\n[DRY RUN] Would purge ghost records:")
            for g in ghosts:
                click.echo(f"  boxer purge-ghost {g['vm_id']}  ({g['libvirt_name']})")
        if foreign:
            click.echo("\n[INFO] Foreign domains require manual import:")
            for f in foreign:
                click.echo(f"  boxer import {f['libvirt_name']}")
        if not orphaned and not ghosts and not foreign:
            click.echo("\nNothing to reconcile.")
        return

    # --fix: adopt orphans and purge ghosts; foreign domains always require manual import
    for o in orphaned:
        try:
            result = _run(_call("vm.adopt", {"libvirt_name": o["libvirt_name"]}))
            click.echo(f"Adopted  {o['libvirt_name']} → vm_id={result['vm_id']}")
        except Exception as exc:
            click.echo(f"Failed to adopt {o['libvirt_name']}: {exc}", err=True)

    for g in ghosts:
        try:
            _run(_call("vm.purge_ghost", {"vm_id": g["vm_id"]}))
            click.echo(f"Purged   {g['vm_id']}  ({g['libvirt_name']})")
        except Exception as exc:
            click.echo(f"Failed to purge {g['vm_id']}: {exc}", err=True)

    if foreign:
        click.echo(f"\n{len(foreign)} foreign domain(s) still require manual 'boxer import <name>'.")


@cli.command("image-trust")
@click.argument("templates", nargs=-1)
@click.option("--json", "as_json", is_flag=True, help="Output JSON")
def image_trust(templates: tuple[str, ...], as_json: bool) -> None:
    """Preflight the trust chain (checksum manifest + PGP signature) of catalog images.

    Downloads only the small checksum manifests and signatures — never the
    multi-gigabyte artifacts. With no arguments, checks every catalog entry.
    """
    params: dict[str, Any] = {}
    if templates:
        params["templates"] = list(templates)
    result = _run(_call("image.preflight", params))
    reports = result.get("reports", [])

    if as_json:
        click.echo(json.dumps(reports, indent=2))
        return

    if not reports:
        click.echo("No catalog entries to check.")
        return

    symbols = {
        "signed": "✓ signed",
        "checksum-only": "~ checksum",
        "unverified": "! unverified",
        "error": "✗ error",
    }
    click.echo(f"{'TEMPLATE':<16} {'STATUS':<14} {'REQ':<4} DETAIL")
    click.echo("-" * 80)
    for r in reports:
        status = symbols.get(r.get("status", ""), r.get("status", "?"))
        req = "yes" if r.get("signature_required") else "-"
        click.echo(f"{r['template']:<16} {status:<14} {req:<4} {r.get('detail', '')}")


@cli.command("import")
@click.argument("domain")
@click.option("--project", "project_id", default="p_imported", show_default=True,
              help="Target project ID")
@click.option("--display", "display_name", default=None, help="Human-readable VM name")
@click.option("--ttl", "ttl_minutes", default=None, type=int, help="Lease duration in minutes")
@click.option("--yes", is_flag=True, help="Skip confirmation prompt")
def import_vm(domain: str, project_id: str, display_name: Optional[str],
              ttl_minutes: Optional[int], yes: bool) -> None:
    """Import a foreign libvirt domain into Boxer management (non-destructive)."""
    if not yes:
        click.confirm(
            f"Import '{domain}' into project '{project_id}'?\n"
            "  Storage will NOT be moved or deleted by Boxer.",
            abort=True,
        )
    params: dict[str, Any] = {"libvirt_name": domain, "project_id": project_id}
    if display_name:
        params["display_name"] = display_name
    if ttl_minutes is not None:
        params["ttl_minutes"] = ttl_minutes
    result = _run(_call("vm.import", params))
    click.echo(
        f"Imported '{domain}' as vm_id={result['vm_id']}  "
        f"display='{result['display_name']}'  project={result['project_id']}"
    )


@cli.command("adopt")
@click.argument("domain")
@click.option("--yes", is_flag=True, help="Skip confirmation prompt")
def adopt(domain: str, yes: bool) -> None:
    """Re-adopt an orphaned Boxer-- domain whose DB record was lost."""
    if not yes:
        click.confirm(f"Re-adopt orphaned domain '{domain}'?", abort=True)
    result = _run(_call("vm.adopt", {"libvirt_name": domain}))
    click.echo(f"Adopted '{domain}' → vm_id={result['vm_id']}")


if __name__ == "__main__":
    cli()
