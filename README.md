# Boxer

A policy-enforcing QEMU/KVM control plane that lets AI coding assistants (and humans) create, manage, and destroy virtual machines safely — without ever touching raw `virsh`, libvirt XML, or the host hypervisor directly.

## Features

- **Project-scoped isolation** — VM ownership is derived from `CLAUDE_PROJECT_DIR` (SHA-256 of the real path). An agent in project A cannot list, stop, or delete VMs belonging to project B.
- **Narrow MCP surface** — 13 audited tools (`box_request_vm`, `box_exec`, `box_screenshot`, `box_delete_vm`, …). No raw shell, no libvirt passthrough, no XML.
- **Admission control & queueing** — configurable caps on RAM, vCPUs, running VMs per project, and disk per project; over-capacity requests queue automatically and are promoted when resources free.
- **Per-project NAT networks** — each project gets its own isolated libvirt network (`boxer-net-<id>`); VMs are not visible to each other across projects.
- **Lease management** — every VM has a TTL; expired running VMs emit warnings, expired stopped VMs are auto-deleted after a grace period.
- **Allowlisted image catalog** — only HTTPS URLs in `images.yaml` can be fetched; all downloads are checksum-verified against a manifest or static pin; private/RFC-1918 IPs are blocked.
- **PGP-verified checksums** — catalog entries can require a `gpgv`-checked signature (detached or clearsigned) on the checksum manifest, validated against a pinned local keyring; fails closed when `signature_required: true`. Preflight every trust chain with `boxer image-trust`.
- **cloud-init provisioning** — headless VMs boot with SSH ready, QEMU guest agent running, and Boxer's key injected.
- **Headed VMs** — SPICE display bound to `127.0.0.1`; screenshot and keyboard input tools available.
- **Human CLI** — `boxer ls / start / stop / delete / cleanup / prune / events / status / image-trust`
- **Desktop notifications** — `boxer-notifier` (user systemd service) calls `notify-send` when stale VMs need attention.

## Setup

Requires a Debian/Ubuntu host with QEMU/KVM already supported by the kernel (`/dev/kvm` exists).

```bash
sudo bash scripts/setup.sh
# optionally: sudo bash scripts/setup.sh --user YOUR_USERNAME
```

The script installs system packages, enables `libvirtd`, creates the `boxer-admin` group, generates an SSH keypair, installs the Python package to `/opt/boxer/venv`, and registers systemd services.

**After setup, log out and back in** (or reboot) before starting the daemon or registering the MCP server. Group membership changes (`libvirt`, `kvm`, `boxer-admin`) are not visible to your running session until you do, and MCP clients will receive `EACCES` when trying to access `/opt/boxer/` without them.

Start the daemon:

```bash
sudo systemctl start boxerd
journalctl -u boxerd -f
```

## Adding to MCP clients

All clients use the stdio transport. The server binary is `/opt/boxer/venv/bin/boxer-mcp` (or wherever the venv lives).

### Claude Code

```bash
claude mcp add --transport stdio --scope local boxer-vm -- /opt/boxer/venv/bin/boxer-mcp
```

Use `--scope project` to restrict the server to a single repository, or `--scope user` to share it across all projects for your user.

### Cursor

Create or edit `.cursor/mcp.json` in your home directory or project root:

```json
{
  "mcpServers": {
    "boxer-vm": {
      "command": "/opt/boxer/venv/bin/boxer-mcp"
    }
  }
}
```

### Windsurf

Edit `~/.codeium/windsurf/mcp_config.json`:

```json
{
  "mcpServers": {
    "boxer-vm": {
      "command": "/opt/boxer/venv/bin/boxer-mcp"
    }
  }
}
```

### Continue

Edit `~/.continue/config.json`:

```json
{
  "mcpServers": [
    {
      "name": "boxer-vm",
      "command": "/opt/boxer/venv/bin/boxer-mcp"
    }
  ]
}
```

### Zed

Edit Zed's `settings.json` (`cmd+,`):

```json
{
  "context_servers": {
    "boxer-vm": {
      "command": {
        "path": "/opt/boxer/venv/bin/boxer-mcp",
        "args": []
      }
    }
  }
}
```

## Emergency cleanup

If the daemon is unavailable, Boxer domains are identifiable by their name prefix:

```bash
virsh -c qemu:///system list --all | grep 'Boxer--'
virsh -c qemu:///system destroy  Boxer--<name>
virsh -c qemu:///system undefine Boxer--<name> --remove-all-storage
```

Only touch domains with the `Boxer--` prefix unless you know exactly what else is running.
