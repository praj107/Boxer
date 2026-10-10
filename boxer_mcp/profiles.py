"""MCP operating profiles for different agent hosts and tool callers."""
from __future__ import annotations

from dataclasses import dataclass


FULL_TOOL_NAMES: tuple[str, ...] = (
    "box_resource_status",
    "box_list_labs",
    "box_request_lab",
    "box_lab_status",
    "box_list_images",
    "box_prewarm_image",
    "box_request_vm",
    "box_list_profiles",
    "box_request_installer",
    "box_list_vms",
    "box_get_vm",
    "box_start_vm",
    "box_stop_vm",
    "box_restart_vm",
    "box_get_ssh_access",
    "box_delete_vm",
    "box_extend_lease",
    "box_snapshot",
    "box_exec",
    "box_push",
    "box_pull",
    "box_screenshot",
    "box_input",
    "box_get_serial_log",
    "box_queue_status",
    "box_cleanup_plan",
    "box_scan_unmanaged",
    "box_import_vm",
    "box_adopt_vm",
    "box_purge_ghost",
    # Guest utils (Milestone 9): structured, token-efficient guest introspection.
    "box_sysinfo",
    "box_services",
    "box_packages",
    "box_logs",
)

COMPACT_TOOL_NAMES: tuple[str, ...] = (
    "box_resource_status",
    "box_list_labs",
    "box_request_lab",
    "box_lab_status",
    "box_list_images",
    "box_list_profiles",
    "box_request_vm",
    "box_list_vms",
    "box_get_vm",
    "box_exec",
    "box_push",
    "box_pull",
    "box_get_serial_log",
    "box_queue_status",
    "box_cleanup_plan",
    "box_stop_vm",
    "box_extend_lease",
    "box_delete_vm",
    # Curated guest-utils subset for low-context callers.
    "box_sysinfo",
    "box_services",
)

# Dedicated, opt-in Android app-testing surface (Milestone 8). Pairs core
# lifecycle/inspection tools with the emulator + ADB control tools.
ANDROID_TOOL_NAMES: tuple[str, ...] = (
    "box_resource_status",
    "box_list_android_images",
    "box_request_android",
    "box_list_vms",
    "box_get_vm",
    "box_start_vm",
    "box_stop_vm",
    "box_restart_vm",
    "box_delete_vm",
    "box_extend_lease",
    "box_queue_status",
    "box_android_ui",
    "box_android_app",
    "box_android_screenshot",
    "box_android_logcat",
    "box_run_instrumentation",
)

READONLY_TOOL_NAMES: tuple[str, ...] = (
    "box_resource_status",
    "box_list_labs",
    "box_lab_status",
    "box_list_images",
    "box_list_profiles",
    "box_list_vms",
    "box_get_vm",
    "box_get_serial_log",
    "box_queue_status",
    "box_cleanup_plan",
    "box_scan_unmanaged",
    "box_list_android_images",
    "box_sysinfo",
    "box_services",
    "box_logs",
)

RICH_INSTRUCTIONS = (
    "Boxer provides isolated KVM labs for coding work that needs real Linux fidelity: clean "
    "bug repros, distro smoke checks, Docker builds away from host Docker, browser tests, "
    "and headed desktop inspection. Prefer box_list_labs then box_request_lab over raw VM "
    "requests. Use exact vm_id values; box_delete_vm is permanent.\n\n"
    "Workflow: check box_resource_status, choose a lab recipe, request one lab per purpose, "
    "then use box_lab_status and box_exec for bounded validation commands. Fall back to "
    "box_request_vm only when no lab recipe fits. Use box_get_serial_log for boot or "
    "cloud-init failures. Use box_request_installer only for ISO installer workflows. Avoid "
    "returning private SSH keys unless direct SSH is explicitly required."
)

CODEX_INSTRUCTIONS = (
    "Boxer provides isolated KVM labs for coding work that needs real Linux fidelity: clean "
    "bug repros, distro smoke checks, Docker builds away from host Docker, browser tests, "
    "and headed desktop inspection. Prefer box_list_labs then box_request_lab over raw VM "
    "requests. Use exact vm_id values; box_delete_vm is permanent.\n\n"
    "This full Codex profile still exposes VM lifecycle escape hatches, but the product "
    "shape is lab-first rather than VM-ops-first. Keep calls scoped: IDs first, large blobs "
    "later, and small serial-log tails or command outputs. Use BOXER_PROJECT_DIR when the "
    "client does not launch stdio servers from the repository root."
)

LOW_CONTEXT_INSTRUCTIONS = (
    "Boxer provides compact KVM lab recipes for stricter coding tool callers such as Qwen, "
    "DeepSeek, and Kimi. Use it for clean repro labs, distro smoke labs, isolated Docker "
    "builds, and browser-test labs. Call box_resource_status, then box_list_labs, then "
    "box_request_lab. Use exact vm_id values for box_lab_status, box_exec, and cleanup. "
    "Keep outputs small; box_delete_vm is permanent.\n\n"
    "This profile keeps raw VM tools as fallback but intentionally hides installer, "
    "import/adopt, direct SSH key, screenshot, input, snapshot, start, restart, and "
    "image-prewarm tools. Its box_request_vm schema is reduced for agents that cannot use "
    "a lab recipe."
)

ANDROID_INSTRUCTIONS = (
    "Boxer Android app-testing profile. Boot an emulator with box_request_android "
    "(see box_list_android_images for versions/variants), then drive the app over "
    "ADB: box_android_ui to dump the UI tree as JSON and tap/swipe/type by "
    "coordinate, box_android_app to install/launch/stop apps, box_android_screenshot "
    "for visuals, box_android_logcat for structured telemetry, and "
    "box_run_instrumentation for prepared test suites. Devices appear in "
    "box_list_vms and are deleted with box_delete_vm (permanent). Keep outputs "
    "small; prefer UI dumps and filtered logcat over raw screenshots when possible."
)

READONLY_INSTRUCTIONS = (
    "Boxer read-only profile. Use this profile to inspect KVM lab recipes, VM capacity, "
    "images, bootstrap profiles, existing project VMs, serial logs, queues, cleanup plans, "
    "and unmanaged VM state. It cannot create, execute inside, mutate, import, adopt, purge, "
    "or delete VMs."
)


@dataclass(frozen=True)
class MCPProfile:
    name: str
    description: str
    instructions: str
    tool_names: tuple[str, ...]
    compact_request_vm: bool = False


MCP_PROFILES: dict[str, MCPProfile] = {
    "claude": MCPProfile(
        name="claude",
        description="Full lab-first Boxer surface tuned for Claude Code tool search.",
        instructions=RICH_INSTRUCTIONS,
        tool_names=FULL_TOOL_NAMES,
    ),
    "codex": MCPProfile(
        name="codex",
        description="Full lab-first Boxer surface tuned for Codex server instructions.",
        instructions=CODEX_INSTRUCTIONS,
        tool_names=FULL_TOOL_NAMES,
    ),
    "qwen": MCPProfile(
        name="qwen",
        description="Compact lab-first Boxer surface for Qwen Code and Qwen-backed agents.",
        instructions=LOW_CONTEXT_INSTRUCTIONS,
        tool_names=COMPACT_TOOL_NAMES,
        compact_request_vm=True,
    ),
    "deepseek": MCPProfile(
        name="deepseek",
        description="Compact lab-first Boxer surface for DeepSeek-backed coding agents.",
        instructions=LOW_CONTEXT_INSTRUCTIONS,
        tool_names=COMPACT_TOOL_NAMES,
        compact_request_vm=True,
    ),
    "kimi": MCPProfile(
        name="kimi",
        description="Compact lab-first Boxer surface for Kimi/Moonshot-backed coding agents.",
        instructions=LOW_CONTEXT_INSTRUCTIONS,
        tool_names=COMPACT_TOOL_NAMES,
        compact_request_vm=True,
    ),
    "oss": MCPProfile(
        name="oss",
        description="Generic compact lab-first Boxer surface for local/open-weight agents.",
        instructions=LOW_CONTEXT_INSTRUCTIONS,
        tool_names=COMPACT_TOOL_NAMES,
        compact_request_vm=True,
    ),
    "android": MCPProfile(
        name="android",
        description="Opt-in Android emulator app-testing surface (emulator + ADB control).",
        instructions=ANDROID_INSTRUCTIONS,
        tool_names=ANDROID_TOOL_NAMES,
    ),
    "readonly": MCPProfile(
        name="readonly",
        description="Read-only Boxer inspection surface.",
        instructions=READONLY_INSTRUCTIONS,
        tool_names=READONLY_TOOL_NAMES,
    ),
}

MCP_PROFILE_ALIASES: dict[str, str] = {
    "default": "codex",
    "full": "codex",
    "rich": "codex",
    "anthropic": "claude",
    "claude-code": "claude",
    "openai": "codex",
    "openai-codex": "codex",
    "low-context": "oss",
    "local": "oss",
    "open-weight": "oss",
    "moonshot": "kimi",
    "kimi-k2": "kimi",
    "read": "readonly",
    "read-only": "readonly",
}


def resolve_mcp_profile(name: str | None) -> MCPProfile:
    raw = (name or "codex").strip().lower()
    canonical = MCP_PROFILE_ALIASES.get(raw, raw)
    try:
        return MCP_PROFILES[canonical]
    except KeyError as exc:
        known = sorted({*MCP_PROFILES, *MCP_PROFILE_ALIASES})
        raise ValueError(f"Unknown Boxer MCP profile '{name}'. Known profiles: {known}") from exc


def profile_summary() -> list[dict[str, object]]:
    return [
        {
            "name": profile.name,
            "description": profile.description,
            "tools": len(profile.tool_names),
            "compact_request_vm": profile.compact_request_vm,
        }
        for profile in MCP_PROFILES.values()
    ]
