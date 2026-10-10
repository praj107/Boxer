# Linux Guest Utils (Milestone 9)

Guest utils give agents **structured, token-efficient** system information and
control inside Linux guests, instead of guessing a sequence of shell commands over
an SSH terminal. One tool call returns compact JSON for what would otherwise be
five-plus probes (`cat /etc/os-release`, `free -m`, `df -h`, `systemctl …`, `ss`).

## Hybrid architecture

1. **Daemon-side baseline (zero install).** `boxerd/guest_utils.py` runs curated,
   family-aware shell snippets over the existing `GuestAgent.exec_ssh` channel and
   parses them into compact JSON. This works on every SSH-reachable distro,
   including BYOISO guests, with nothing pre-installed.
2. **Optional in-guest agent.** A single static Go binary, `boxerctl`
   (`agent/boxerctl`), computes the same JSON in-guest in one round trip. It is
   installed by requesting the `boxerctl` profile, which injects the prebuilt
   binary via cloud-init (delivered as a base64 text file and decoded into
   `/usr/local/bin`). When `boxerctl` is present the daemon prefers it; otherwise
   it falls back to the shell parsers. **The MCP surface is identical either way.**

Build the agent with `agent/build.sh` (outputs `agent/dist/boxerctl-linux-{amd64,
arm64}`). If it has not been built, the `boxerctl` profile is a graceful no-op and
the daemon-side baseline still works.

## Capabilities

| Tool | IPC | Returns |
|---|---|---|
| `box_sysinfo` | `guest.sysinfo` | os/kernel/arch, cpu count + load, memory, root disk, uptime, virtualization |
| `box_services` | `guest.services` | init system, running & failed units, listening ports, top processes |
| `box_packages` | `guest.packages` | distro-aware query / search / **install** (apt/dnf/apk/zypper/pacman) |
| `box_logs` | `guest.logs` | failed units, journal/syslog tail, dmesg tail |

`box_packages` with `action=install` is **mutating** and ownership/policy-gated;
`query`/`search` are read-only. CLI: `boxer guest info|services|packages|logs`.

## Exposure

- Rich profiles (`codex`, `claude`): all four tools.
- Compact profiles (`qwen`, `deepseek`, `kimi`, `oss`): curated subset
  (`box_sysinfo`, `box_services`).
- `readonly`: read-only tools (`box_sysinfo`, `box_services`, `box_logs`); the
  mutating `box_packages` is excluded.

## Bounds

- Guest utils target **Linux VMs** (`kind = "vm"`) reachable over SSH; they are
  rejected for Android devices and for VMs without an IP yet. Serial-only guests
  (e.g. EncoreOS without SSH) remain served by `box_get_serial_log`.
- Package-manager coverage is best-effort on exotic distros; the `boxerctl`
  agent gives the most consistent output where installed.
