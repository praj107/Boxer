# Boxer

A policy-enforcing QEMU/KVM control plane that lets AI coding assistants (and humans) create, manage, and destroy virtual machines safely — without ever touching raw `virsh`, libvirt XML, or the host hypervisor directly.

## Features

- **Project-scoped isolation** — VM ownership is derived from `BOXER_PROJECT_DIR`, `CLAUDE_PROJECT_DIR`, or the MCP process working directory (SHA-256 of the real path). An agent in project A cannot list, stop, or delete VMs belonging to project B.
- **Agent lab recipes** — high-level `box_request_lab` workflows for clean Linux repros, distro smoke checks, isolated Docker builds, headless browser tests, and headed desktop inspection. Labs compile down to Boxer's audited VM request path.
- **Profiled MCP surface** — 34 audited tools in the rich profile (`box_request_lab`, `box_request_vm`, `box_request_installer`, `box_list_images`, `box_exec`, `box_push`, `box_pull`, `box_screenshot`, `box_delete_vm`, ...), plus compact Qwen/DeepSeek/Kimi profiles with 20 tools, lab-first instructions, and a reduced VM-request fallback schema. No raw shell, no libvirt passthrough, no XML.
- **Admission control & queueing** — configurable caps on RAM, vCPUs, running VMs per project, and disk per project; over-capacity requests queue automatically and are promoted when resources free.
- **Per-project NAT networks** — each project gets its own isolated libvirt network (`boxer-net-<id>`); VMs are not visible to each other across projects.
- **Lease management** — every VM has a TTL; expired running VMs emit warnings, expired stopped VMs are auto-deleted after a grace period.
- **Allowlisted image catalog** — only HTTPS URLs in `images.yaml` can be fetched; all downloads are checksum-verified against a manifest or static pin; private/RFC-1918 IPs are blocked. Distro families (Ubuntu, Debian, Fedora, AlmaLinux, Rocky, Arch) with per-entry `refresh_policy` (`pinned`/`latest`/`manual`).
- **Content-addressed image cache** — verified artifacts are stored by digest, so multiple versions coexist and overlays keep their exact backing; `boxer prune` reclaims old unused blobs, `box_list_images`/`box_prewarm_image` browse and warm the cache.
- **PGP-verified checksums** — catalog entries can require a `gpgv`-checked signature (detached or clearsigned) on the checksum manifest, validated against a pinned local keyring; fails closed when `signature_required: true`. Preflight every trust chain with `boxer image-trust`.
- **cloud-init provisioning** — headless VMs boot with SSH ready, QEMU guest agent running, and Boxer's key injected.
- **Rich guest profiles** — one-request setup via named profiles (`python`, `node`, `docker`, `browser`, `desktop`), bounded `write_files`/`secret_files` injection, and optional wait conditions (`ssh`, `cloud_init`, …) so a request returns only once the VM is actually ready.
- **ISO installer workflow** — `box_request_installer` provisions VMs from installer ISOs onto blank disks, boots the ISO first, then flips boot order to disk once the install finishes; optional unattended seeds (Ubuntu autoinstall, Debian preseed, Fedora Kickstart) or a manual SPICE console. ISO cache is kept separate from cloud images.
- **Headed VMs** — SPICE display bound to `127.0.0.1`; screenshot and keyboard input tools available. `box_screenshot(save_path=...)` / `boxer screenshot` write the PNG to a host file instead of returning base64.
- **Guest file transfer & robust exec** — `box_push` / `box_pull` (CLI: `boxer push` / `boxer pull`) copy files or whole directories (as one tar.gz) over the existing SSH channel, up to 32 MiB, optionally as root. `box_exec(detach=True)` runs long or self-backgrounding jobs in their own session and returns a pid + log path; `wait_ssh_seconds` rides through guest reboots. `boxer exec VM -- cmd` exposes the same from the shell. If `boxer.yaml` leaves the SSH key unset, the daemon uses the pair `setup.sh` generated in `/etc/boxer`.
- **Human CLI** — `boxer labs / lab request / lab status / ls / start / stop / delete / cleanup / prune / events / status / images / profiles / image-trust / image-refresh`
- **Desktop notifications** — `boxer-notifier` (user systemd service) calls `notify-send` when stale VMs need attention.

## Setup

Requires a Debian/Ubuntu host with QEMU/KVM already supported by the kernel (`/dev/kvm` exists).

```bash
sudo bash scripts/setup.sh
# optionally: sudo bash scripts/setup.sh --user YOUR_USERNAME
```

The script installs system packages, enables `libvirtd`, creates the `boxer-admin` group, generates an SSH keypair, installs the Python package to `/opt/boxer/venv`, and registers systemd services.

**After setup, log out and back in** (or reboot) before starting the daemon or registering the MCP server. Group membership changes (`libvirt`, `kvm`, `boxer-admin`) are not visible to your running session until you do, and MCP clients will receive `EACCES` when trying to access `/opt/boxer/` without them.

Boxer stores VM disks, generated seed ISOs, and staged local ISOs under
`/var/lib/boxer` with access for the configured QEMU group (`qemu_group`, default
`kvm`). For `box_request_installer(iso_path=...)`, Boxer copies the caller's ISO
into the VM directory before booting so system libvirt can read it even when the
source ISO lives under a private home directory.

Local `install_method="manual"` ISO test boots use a raw blank target disk and
attach it as QEMU NVMe before the guest starts. The ISO still boots first; the
NVMe disk is present for custom OS storage tests.

Start the daemon:

```bash
sudo systemctl start boxerd
journalctl -u boxerd -f
```

## Adding to MCP clients

All clients use the stdio transport. The server binary is `/opt/boxer/venv/bin/boxer-mcp` (or wherever the venv lives).

`boxer-mcp` is a per-client stdio child process. If you reinstall Boxer while an
agent session is running, restart the MCP client/session before relying on new
MCP tool code. `scripts/reload.sh` leaves active MCP children alone by default;
use `sudo bash scripts/reload.sh --restart-mcp` only when you are ready for
existing MCP transports to close.

### MCP profiles

Boxer exposes multiple compatible MCP operating profiles behind the same server
binary. Pick the profile at startup with `--profile NAME` or the
`BOXER_MCP_PROFILE` environment variable.

| Profile | Tools | Use for |
| --- | ---: | --- |
| `codex` (default) | 28 | Codex CLI/IDE and other strong coding agents that can use the full Boxer surface. |
| `claude` | 28 | Claude Code, especially with MCP Tool Search enabled. |
| `qwen` | 16 | Qwen Code and Qwen-backed agents that benefit from fewer tools, lab recipes, and a compact fallback schema. |
| `deepseek` | 16 | DeepSeek-backed coding agents and OpenAI-compatible wrappers. |
| `kimi` | 16 | Kimi/Moonshot-backed coding agents and OpenAI/Anthropic-compatible wrappers. |
| `oss` | 16 | Generic local/open-weight coding agents. |
| `readonly` | 11 | Inspection-only access with no VM creation, execution, import, purge, or delete tools. |

The compact profiles expose the common coding loop only:
`box_resource_status`, `box_list_labs`, `box_request_lab`, `box_lab_status`,
`box_list_images`, `box_list_profiles`, `box_request_vm`, `box_list_vms`,
`box_get_vm`, `box_exec`, `box_get_serial_log`, `box_queue_status`,
`box_cleanup_plan`, `box_stop_vm`, `box_extend_lease`, and `box_delete_vm`.
Their `box_request_vm` schema is intentionally reduced to template, purpose,
profiles, headless, TTL, and readiness waits for cases where no lab recipe fits.

For project isolation, Boxer derives VM ownership from `BOXER_PROJECT_DIR`, then
`CLAUDE_PROJECT_DIR`, then the MCP process working directory. Set
`BOXER_PROJECT_DIR` in non-Claude clients when the stdio server might not launch
from the repository root.

List the installed profiles:

```bash
/opt/boxer/venv/bin/boxer-mcp --list-profiles
```

See `docs/MCP-Agent-Profiles.md` for the Boxer-specific design rationale and
source links behind these profiles.

### Lab recipes

The lab layer is the preferred interface for agents. It keeps Boxer focused on
practical KVM-backed development environments instead of generic VM operations.

```bash
boxer labs
boxer lab request linux-repro --purpose bug-123 --size small
boxer lab request docker-build --purpose image-build --ttl 180
boxer lab status vm_abc123
```

Available recipes:

- `linux-repro` — clean Linux repro box with Python tooling.
- `distro-smoke` — one distro VM for portability checks; request several labs for a small matrix.
- `docker-build` — Docker inside a VM, isolated from the host Docker daemon.
- `web-ui` — Chromium/Xvfb/fonts for headless browser automation.
- `headed-desktop` — headed XFCE VM for SPICE screenshot/input workflows.

See `docs/Agent-Labs.md` for the research-backed niche and implementation
constraints.

### Claude Code

**Option A — one-liner:**

```bash
claude mcp add --env BOXER_MCP_PROFILE=claude --transport stdio --scope local boxer-vm -- /opt/boxer/venv/bin/boxer-mcp
```

Two ordering rules matter here (both are bugs in Claude Code's CLI parser):

1. `--env` is variadic — put it **before** `--transport`/`--scope` so another flag terminates it before the server name. If `--env KEY=VALUE` is the last option before the server name, the CLI consumes the server name as a second env-var value and then reports `missing required argument 'name'`.
2. `--` **after** the server name and **before** the command is required for stdio servers. It tells the parser that everything following is the child process invocation, not a flag for `mcp add` itself.

Use `--scope project` to restrict the server to a single repository, or `--scope user` to share it across all projects for your user.

**Option B — `add-json` (avoids all parser quirks):**

```bash
claude mcp add-json --scope local boxer-vm \
  '{"type":"stdio","command":"/opt/boxer/venv/bin/boxer-mcp","env":{"BOXER_MCP_PROFILE":"claude"}}'
```

**Option C — direct JSON (if you prefer config-file management):**

For local scope, create or edit `.claude/settings.local.json` in your project root:

```json
{
  "mcpServers": {
    "boxer-vm": {
      "command": "/opt/boxer/venv/bin/boxer-mcp",
      "env": {
        "BOXER_MCP_PROFILE": "claude"
      }
    }
  }
}
```

For user scope, edit `~/.claude/settings.json` instead. For project scope (shared via git), edit `.claude/settings.json`.

### Codex

In `~/.codex/config.toml` or a trusted project `.codex/config.toml`:

```toml
[mcp_servers."boxer-vm"]
command = "/opt/boxer/venv/bin/boxer-mcp"
args = ["--profile", "codex"]
startup_timeout_sec = 10
tool_timeout_sec = 120

[mcp_servers."boxer-vm".env]
BOXER_PROJECT_DIR = "/absolute/path/to/your/project"
```

Codex also supports `enabled_tools` / `disabled_tools` if you want to narrow the
rich profile without switching to a compact model profile.

### Qwen Code

Use the compact profile in `.qwen/settings.json`:

```json
{
  "mcpServers": {
    "boxer-vm": {
      "command": "/opt/boxer/venv/bin/boxer-mcp",
      "args": ["--profile", "qwen"],
      "env": {
        "BOXER_PROJECT_DIR": "${PWD}"
      },
      "timeout": 15000
    }
  }
}
```

### DeepSeek and Kimi backed agents

DeepSeek and Kimi usually reach MCP through a coding-agent harness such as
OpenCode, Cline, Roo Code, Kilo Code, or another OpenAI/Anthropic-compatible
wrapper. Configure the Boxer server in that harness with the compact profile:

```json
{
  "mcpServers": {
    "boxer-vm": {
      "command": "/opt/boxer/venv/bin/boxer-mcp",
      "args": ["--profile", "deepseek"],
      "env": {
        "BOXER_PROJECT_DIR": "/absolute/path/to/your/project"
      }
    }
  }
}
```

Use `--profile kimi` for Kimi/Moonshot-backed runs. Use `--profile oss` for a
generic local model harness when you do not know the exact model family.

### Cursor

Create or edit `.cursor/mcp.json` in your home directory or project root:

```json
{
  "mcpServers": {
    "boxer-vm": {
      "command": "/opt/boxer/venv/bin/boxer-mcp",
      "args": ["--profile", "oss"]
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
      "command": "/opt/boxer/venv/bin/boxer-mcp",
      "args": ["--profile", "oss"]
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
      "command": "/opt/boxer/venv/bin/boxer-mcp",
      "args": ["--profile", "oss"]
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
        "args": ["--profile", "oss"]
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
