# Boxer Agent Labs

Boxer's practical niche is not "VM operations for agents". It is a local,
policy-enforced KVM lab provider for tasks where containers and raw shell access
are the wrong abstraction:

- clean Linux bug reproduction;
- distro portability smoke checks;
- Docker builds without touching the host Docker daemon;
- browser automation without using the host browser profile;
- headed GUI inspection when screenshots/input are necessary.

The lab layer is intentionally small. Each lab recipe creates one VM through the
existing `vm.request` daemon path, so catalog allowlisting, checksum
verification, per-project NAT networks, admission control, leases, queueing, and
project isolation all still apply.

## Research Consolidation

The agent sandbox market clusters around dev containers, Docker sandboxes, local
shell loops, and VM-backed sandboxes.

- Claude Code's dev-container guidance shows the mainstream container workflow:
  the editor connects to a container, and build tools run inside it rather than
  on the host. That is convenient, but it still shares the container model and
  does not provide distro/installer fidelity. Source:
  https://code.claude.com/docs/en/devcontainer
- Claude Code's MCP guidance frames MCP as a way to connect agents to external
  systems directly instead of pasting context manually. That supports a
  high-level lab surface better than a low-level VM control panel. Source:
  https://code.claude.com/docs/en/mcp
- OpenAI's local shell guide explicitly warns that arbitrary local command
  execution should be sandboxed or allowlisted. Boxer answers that need with
  VM-backed execution boundaries rather than passing raw host shell access
  through MCP. Source:
  https://developers.openai.com/api/docs/guides/tools-local-shell
- Existing sandbox MCPs are usually language/container sandboxes or fast local VM
  sandboxes. Boxer should not compete on sub-100ms command latency; it should
  compete on Linux distribution fidelity, host-Docker separation, image
  verification, lease cleanup, and per-project networks. Examples:
  https://github.com/bird/sandbox-mcp and
  https://protodex.io/servers/automata-labs-team-code-sandbox-mcp.html

That leads to a focused product shape: agents ask for a "lab" with an intended
validation job, then run bounded commands inside that lab. They should not start
by selecting CPU, RAM, disks, cloud-init files, screenshots, and lifecycle tools
unless the lab abstraction is insufficient.

## Recipes

`linux-repro`
: Clean Ubuntu/Debian/Fedora/etc. VM with Python tooling. Use for bug repros,
test runs, and suspicious command execution away from the host.

`distro-smoke`
: Single-distro VM for portability checks. Request more than one lab only when a
small matrix is required and host capacity permits it.

`docker-build`
: VM with Docker installed. Use for container builds while keeping the host
Docker daemon, images, volumes, and credentials out of the agent's reach.

`web-ui`
: VM with Chromium, Xvfb, fonts, and Python tooling. Use for headless browser
checks without exposing host browser state.

`headed-desktop`
: Headed XFCE VM. Use only when visual inspection through SPICE screenshot/input
is genuinely needed.

## MCP Flow

Preferred agent flow:

```text
box_resource_status
box_list_labs
box_request_lab
box_lab_status
box_exec / box_get_serial_log
box_delete_vm
```

Fallback flow:

```text
box_list_images
box_list_profiles
box_request_vm
```

Rich profiles still expose advanced VM operations because Claude Code, Codex,
and human operators can use them for edge cases. Compact Qwen, DeepSeek, Kimi,
and OSS profiles put lab tools first and keep raw VM creation as a reduced
fallback.

## CLI Flow

```bash
boxer labs
boxer lab request linux-repro --purpose bug-123 --size small
boxer lab request docker-build --purpose image-build --ttl 180
boxer lab status vm_abc123
boxer delete vm_abc123
```

## Bounds

Labs are deliberately constrained:

- one VM per lab request;
- no raw libvirt, XML, host shell, or host Docker passthrough;
- no arbitrary network topology construction;
- no bypass of Boxer image catalog verification;
- no automatic multi-VM matrix unless a future scheduler-aware batch interface is
  added;
- no secret return path unless the caller switches to a rich profile and
  explicitly requests direct SSH access.

This keeps Boxer useful for agents while preserving the safety model that made
the underlying VM stack worth using.
