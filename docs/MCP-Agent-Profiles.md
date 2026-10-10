# Boxer MCP Agent Profiles

This plan applies the MCP client/model tuning primer to Boxer specifically.
Boxer is a stateful KVM lab provider, so the right adaptation is not a generic
`search`/`fetch` research surface or a VM-ops panel. It is a set of compatible
operating profiles that expose high-level lab recipes first and lower-level VM
controls only where the host/model can use them safely.

## Design Targets

The implementation optimizes for five core agent groupings:

| Group | Boxer profile | Reason |
| --- | --- | --- |
| Codex | `codex` | Full 28-tool surface with lab-first server instructions whose first paragraph is self-contained for Codex initialization. |
| Claude Code | `claude` | Full 28-tool surface with concise instructions for Claude Code Tool Search and deferred tool loading. |
| Qwen Code / Qwen-backed agents | `qwen` | Compact 16-tool surface to keep `mcpServers` discovery and function schemas small while exposing lab recipes. |
| DeepSeek-backed agents | `deepseek` | Compact 16-tool surface with lab recipes and a reduced `box_request_vm` fallback schema for stricter OpenAI-compatible tool callers. |
| Kimi/Moonshot-backed agents | `kimi` | Compact 16-tool surface for harnesses that pass the tool list to Kimi-style OpenAI/Anthropic-compatible APIs. |

`oss` is the generic compact profile for local/open-weight harnesses. `readonly`
is an inspection-only profile for cautious environments.

## Boxer-Specific Decisions

1. Keep the daemon IPC API stable.
   The profiles live in `boxer_mcp`; every tool still calls the same audited
   daemon methods. No raw libvirt, shell, XML, or host filesystem passthrough is
   introduced.

2. Preserve the rich default.
   `codex` is the default and exposes all 28 tools, matching the historical
   Boxer surface plus lab recipes while improving server instructions and tool
   annotations.

3. Shrink the low-context profile by hiding uncommon workflows.
   The `qwen`, `deepseek`, `kimi`, and `oss` profiles expose only the common
   coding loop: capacity, lab discovery/request/status, image/profile discovery,
   fallback VM creation, VM lookup, command execution, serial-log reading,
   queue/cleanup inspection, stop, extend-lease, and delete.

4. Reduce the riskiest schema instead of weakening validation.
   Compact profiles should use `box_request_lab` first. When no lab recipe fits,
   `box_request_vm` keeps the same public tool name but uses a smaller schema:
   template, purpose, profiles, headless, TTL, readiness waits, and wait timeout.
   Advanced bootstrap files, secret files, direct SSH key return, manual tags,
   and custom wait commands remain available in the rich profiles.

5. Add MCP annotations.
   Tools now advertise read-only, destructive, idempotent, and open-world hints
   where the MCP SDK supports them. This helps clients that use annotations for
   approval policy or UI presentation.

6. Add `BOXER_PROJECT_DIR`.
   Boxer still supports `CLAUDE_PROJECT_DIR`, but non-Claude clients can now pass
   `BOXER_PROJECT_DIR` to keep project isolation stable even when the MCP stdio
   process starts outside the repository root.

## Profile Matrix

| Profile | Tool count | Request VM schema | Hidden by design |
| --- | ---: | --- | --- |
| `codex` | 28 | Full | Nothing. |
| `claude` | 28 | Full | Nothing. |
| `qwen` | 16 | Compact | Installer, import/adopt, purge ghost, direct SSH key, screenshot/input, snapshot, start/restart, image prewarm. |
| `deepseek` | 16 | Compact | Same as `qwen`. |
| `kimi` | 16 | Compact | Same as `qwen`. |
| `oss` | 16 | Compact | Same as `qwen`. |
| `readonly` | 11 | None | All create, exec, lifecycle mutation, import/adopt/purge, and delete tools. |

## Host Notes

Claude Code:
Claude Code supports MCP resources, prompts, dynamic `list_changed`
notifications, and MCP Tool Search. Its docs say server instructions are useful
for Tool Search and that tool descriptions and server instructions are truncated
at 2 KB. Boxer keeps the `claude` instructions short and front-loaded.
Reference: https://code.claude.com/docs/en/mcp

Codex:
Codex supports stdio and Streamable HTTP MCP servers and reads server
instructions during initialization. Its manual recommends keeping the first 512
characters of `instructions` self-contained. Boxer makes the first paragraph of
the `codex` instructions independently useful.
Reference: https://developers.openai.com/codex/codex-manual.md

Qwen Code:
Qwen Code loads MCP servers from `mcpServers` in `settings.json`, supports stdio,
HTTP, and SSE entries, and exposes timeout and trust controls. Boxer maps Qwen to
the compact profile by default in docs because Qwen-facing harnesses benefit from
fewer tools and narrower schemas.
Reference: https://qwenlm.github.io/qwen-code-docs/en/users/features/mcp/

DeepSeek:
DeepSeek's API supports OpenAI-compatible tools and strict function-calling beta
mode. Its API reference also warns applications to validate generated arguments
before executing functions. Boxer keeps the DeepSeek profile compact and leaves
daemon-side validation authoritative.
References:
- https://api-docs.deepseek.com/guides/tool_calls
- https://api-docs.deepseek.com/api/create-chat-completion

Kimi:
Kimi K2 is documented as an OpenAI/Anthropic-compatible model with strong
tool-calling capabilities where the application passes available tools and then
executes returned calls. Boxer maps Kimi/Moonshot-backed agents to the compact
profile to reduce per-request tool inventory pressure.
Reference: https://github.com/MoonshotAI/Kimi-K2

## Verification Plan

Profile behavior is verified at the MCP SDK layer:

```bash
.venv/bin/python -m pytest tests/test_labs.py tests/test_mcp_profiles.py tests/test_tools.py tests/test_mcp_stdio_e2e.py -q
```

Full repository regression should also pass:

```bash
.venv/bin/python -m pytest -q
```

After reinstalling Boxer on a host, restart MCP client sessions so stdio child
processes load the updated profile code:

```bash
sudo bash scripts/reload.sh --restart-mcp
```
