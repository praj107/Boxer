# Boxer VM Image and Bootstrap Roadmap

This roadmap tracks the work needed to make Boxer a low-boilerplate VM provider for agents while keeping image sourcing and credentials auditable.

## Current Baseline

Boxer already creates VMs from allowlisted catalog entries, caches base images, creates qcow2 overlays, provisions a `boxer` sudo user through cloud-init, installs OpenSSH and the QEMU guest agent, and exposes lifecycle operations through MCP and the CLI.

The main gaps are:

- catalog entries can set `sha256: null`, which accepts a download after hashing it locally but does not bind the artifact to an upstream checksum source;
- catalog `type: iso` entries are present but the VM creation path assumes a bootable cloud-image disk;
- guest bootstrap is fixed, so agents still spend setup tokens installing packages and creating shell access;
- direct SSH shell access needs to be explicit and short-lived rather than a default secret exposure path.

## Milestone 1: Trusted Cloud Images and Bootstrap QOL

Status: implemented in this branch.

Scope:

- Keep the static allowlist model in `config/images.yaml`.
- Add checksum-manifest verification metadata for cloud images.
- Continue supporting static `sha256` pins as a fallback.
- Reject traditional installer ISO templates in `vm.request` until the installer workflow exists.
- Add per-VM bootstrap options for packages, setup commands, and caller-provided public keys.
- Keep daemon-owned SSH as the default path for `box_exec`.
- Add explicit ephemeral SSH key generation for callers that request direct shell access.
- Add `vm.restart` / `box_restart_vm`.
- Add optional create-time IP waiting to reduce polling boilerplate.

Trust model for this milestone:

- HTTPS and private/reserved IP blocking remain mandatory for artifacts and manifests.
- Redirects are followed only after validating each redirect target.
- A catalog entry can trust a checksum manifest over HTTPS or a static pinned hash.
- PGP signature verification fields may be present in the catalog, but enforcement is deferred to Milestone 2.

Reference sources used for the catalog direction:

- Ubuntu cloud images and checksum artifacts: https://cloud-images.ubuntu.com/
- Ubuntu cloud image documentation: https://ubuntu.com/server/docs/explanation/clouds/find-cloud-images/
- Debian cloud image directory layout: https://cdimage.debian.org/cdimage/cloud/bookworm/latest/
- Debian image verification guidance: https://www.debian.org/CD/verify.en.html
- Fedora Cloud download and verification guidance: https://www.fedoraproject.org/cloud/download/
- Fedora checksum verification guidance: https://alt.fedoraproject.org/en/verify.html
- AlmaLinux cloud image verification pattern: https://wiki.almalinux.org/cloud/Generic-cloud
- Arch ISO signature guidance: https://wiki.archlinux.org/title/Getting_and_installing_Arch

## Milestone 2: PGP Keyring Verification

Status: implemented in this branch.

Add distro-aware signature verification for checksum manifests.

Scope:

- Catalog `verification.signature` block (with flat fallbacks) for `signature_url`,
  `keyring_path`, `signature_fingerprints`, `signature_mode`, and `signature_required`.
- Two signature shapes: `detached` (Ubuntu `SHA256SUMS.gpg`, Debian `SHA512SUMS.sign`)
  and `clearsigned` (Fedora/AlmaLinux `*-CHECKSUM`), with checksums read from the
  signed payload for clearsigned manifests.
- Verification uses `gpgv` against a pinned keyring under `keyrings_dir`
  (default `/etc/boxer/keyrings`); `gpgv` never touches the user trust DB or
  imports keys, so the keyring is the sole trust anchor.
- Optional signing-key fingerprint pinning enforced when configured.
- Fail closed when `signature_required: true` and the signature is missing,
  the keyring is absent, or verification fails; otherwise fall back to
  checksum-manifest verification with a warning.
- Verification provenance (`signature_verified`, `signature_provenance`) stored
  in image cache metadata.
- `boxer image-trust [templates…]` admin command (IPC `image.preflight`) checks
  every configured trust chain by fetching only manifests and signatures, never
  the multi-gigabyte artifacts.

Notes / deferred within this milestone:

- Distro public keyrings are not bundled; admins install them into
  `keyrings_dir` (see `config/keyrings/README.md`). Catalog entries ship with
  `signature_required: false` so a fresh install verifies by checksum manifest,
  and flip to fail-closed once the keyring is provisioned.
- Arch signs the ISO itself rather than a checksum manifest; its signature
  enforcement is wired up with the ISO installer workflow in Milestone 3.

## Milestone 3: ISO Installer Workflow

Status: implemented in this branch.

Traditional installer ISOs need a separate flow from cloud images.

Scope:

- Installer job type (`vm.request_installer` / `box_request_installer`) with its
  own concurrency cap (`max_concurrent_installs`); over-cap requests queue with
  `job_type: installer` and are promoted by the existing queue runner.
- Blank qcow2 target disks (`StorageManager.create_blank_disk`) instead of
  backing-file overlays.
- Installer domains boot the ISO first via per-device `<boot order>`; an install
  watcher detects the end-of-install power-off (domains use `on_reboot=destroy`),
  then rewrites the domain XML to boot from disk, detaches the cdrom, and starts
  the VM. `install_state` (`installing`/`installed`/`failed`) is tracked in the DB.
- Unattended seed generation (`boxerd/installer_seed.py`) for the methods named in
  the catalog entry's `install.method`:
  - `ubuntu`: cloud-init NoCloud autoinstall (`cidata`);
  - `debian`: preseed (`PRESEED`);
  - `fedora`/`rhel`: Kickstart on an `OEMDRV`-labelled seed ISO;
  - `manual`: no seed, headed SPICE console (Arch uses this; its cloud-image is
    preferred for headless automation).
- Serial logs captured to `serial.log`; installer state exposed via `vm.get`,
  `box_list_vms`, and `boxer ls`.
- ISO cache (`isos_dir`) kept separate from the cloud-image cache (`images_dir`),
  reusing the Milestone 2 checksum-manifest + PGP verification chain.

Notes / deferred within this milestone:

- Fully hands-off kernel-cmdline injection (e.g. Ubuntu `autoinstall ds=nocloud`)
  is not performed; seeds are placed on auto-detected volume labels and the headed
  console remains the fallback for installers that need a boot argument.
- Arch ISO-level signature enforcement is still tracked alongside its `.sig` flow,
  not the checksum-manifest path.

## Milestone 4: Image Families and Refresh Policy

Status: implemented in this branch.

Improve catalog ergonomics without widening the attack surface.

Scope:

- First-class `family` tagging for Ubuntu, Debian, Fedora, AlmaLinux, Rocky, and
  Arch; AlmaLinux 9 and Rocky 9 generic-cloud entries added to the catalog.
- `refresh_policy` support resolved per entry (explicit field wins; static
  `sha256` defaults to `pinned`, otherwise `latest`):
  - `pinned`: exact hash required (static sha256 or a manifest digest); never
    auto-refreshed, and rejected if no verifiable hash is configured;
  - `latest`: cached blob is refreshed when the upstream manifest digest changes;
  - `manual`: cached blob is served until an admin runs `boxer image-refresh`.
- Content-addressed cache: artifacts are stored as
  `<images|isos>/<template>/blobs/<algo>-<digest>.<ext>` with a `metadata.json`
  carrying the current pointer, provenance, and verified-digest history, so
  multiple verified versions can coexist and overlays keep their exact backing.
- `boxer prune` removes old cached base artifacts that are neither the current
  blob nor referenced as a backing file by any existing overlay (backing chains
  resolved via `qemu-img info --backing-chain`; falls back to protecting all
  digest blobs when that information is unavailable).
- Image listing/prewarm: `box_list_images` (offline catalog + cache status) and
  `box_prewarm_image` for agents; `boxer images`, `boxer image-refresh`, and
  admin IPC `image.list` / `image.refresh` / `image.prune` / `image.prewarm`.

Notes:

- The Rocky entry verifies by checksum manifest only; its signing keyring is
  admin-provided (add a `verification.signature` block once installed).
- Pruning ISO blobs protects the current blob; non-current installer ISOs are
  best-effort since cdrom references are not tracked in backing chains.

## Milestone 5: Rich Guest Profiles

Status: implemented in this branch.

Make common agent VM setups one request instead of a setup transcript.

Scope:

- Named bootstrap profiles (`boxerd/profiles.py`): `python`, `node`, `browser`,
  `docker`, `desktop`. A closed allowlist (max 5 per request) that expands into
  deduped cloud-init packages + setup commands, merged with any explicit
  `bootstrap_packages` / `bootstrap_commands`.
- Optional wait conditions on `vm.request`: `guest_agent`, `ip`, `ssh`,
  `cloud_init` / `package_install`, plus an arbitrary `wait_command` that must
  exit 0; bounded by `wait_timeout_seconds` (capped at 900). Per-condition
  results (`ready`/`timeout`/`skipped`/`error`) are returned in `wait_results`.
- File injection through cloud-init `write_files` (base64-encoded), bounded:
  absolute paths, ≤20 files, ≤256 KiB each, ≤1 MiB total, octal permissions.
- Secret handling: `secret_files` default to mode `0600` owned by `boxer`, their
  contents are never written to events or returned payloads (only a count), and
  a late cloud-init command wipes the persisted `user-data` copy from the
  instance cache after first boot to limit secret residue on disk.
- MCP schemas keep all of this bounded and explicit: `box_request_vm` gained
  `profiles`, `write_files`, `secret_files`, `wait_for`, `wait_command`, and
  `wait_timeout_seconds`; discovery via `box_list_profiles` (and `boxer profiles`).

Notes:

- Profiles target the Debian/Ubuntu package layer of the default cloud-image
  families; on other families the package step is best-effort.
- The seed-wipe is best-effort defence-in-depth; the cloud-init NoCloud ISO is
  still the delivery vector, so treat `secret_files` as one-time material.

## Milestone 6: Agent-Tuned MCP Profiles

Status: implemented in this branch.

Make the MCP surface adapt to the host/model harness without changing the daemon
security boundary.

Scope:

- Profile-aware MCP server construction (`boxer-mcp --profile NAME` or
  `BOXER_MCP_PROFILE`) with:
  - `codex` (default): full rich surface with Codex-front-loaded server
    instructions;
  - `claude`: full rich surface with Claude Code Tool Search-friendly
    instructions;
  - `qwen`, `deepseek`, `kimi`, and `oss`: compact low-context surfaces for
    stricter or low-context tool callers;
  - `readonly`: inspection tools and no create/execute/mutate/delete tools.
- Compact profiles keep the public `box_request_vm` tool name but expose a
  smaller schema: template, purpose, profiles, headless, TTL, readiness waits,
  and wait timeout.
- MCP `ToolAnnotations` added for read-only, destructive, idempotent, and
  open-world hints where the Python MCP SDK supports them.
- Client-neutral `BOXER_PROJECT_DIR` added ahead of `CLAUDE_PROJECT_DIR` for
  project identity, so non-Claude MCP hosts can preserve Boxer isolation even
  when stdio starts outside the repository root.
- README client snippets updated for Claude Code, Codex, Qwen Code,
  DeepSeek-backed agents, Kimi-backed agents, and generic local harnesses.
- Detailed profile design note added at `docs/MCP-Agent-Profiles.md`.

Reference sources used for this profile direction:

- Claude Code MCP and Tool Search: https://code.claude.com/docs/en/mcp
- Codex MCP server instructions: https://developers.openai.com/codex/codex-manual.md
- Qwen Code MCP configuration: https://qwenlm.github.io/qwen-code-docs/en/users/features/mcp/
- DeepSeek tool calls and strict mode: https://api-docs.deepseek.com/guides/tool_calls
- DeepSeek chat-completion tool schema: https://api-docs.deepseek.com/api/create-chat-completion
- Kimi K2 tool calling: https://github.com/MoonshotAI/Kimi-K2

## Milestone 7: Agent Lab Recipes

Status: implemented in this branch.

Reposition Boxer from generic VM operations toward a practical agent niche:
local, policy-enforced KVM labs for real Linux validation work.

Scope:

- New daemon lab recipe catalog (`boxerd/labs.py`) with five single-VM recipes:
  `linux-repro`, `distro-smoke`, `docker-build`, `web-ui`, and
  `headed-desktop`.
- New IPC methods:
  - `lab.list`: returns recipes and stack constraints;
  - `lab.request`: compiles a recipe into the existing audited `vm.request`
    path, preserving image catalog verification, scheduling, leases, queueing,
    per-project networks, and policy;
  - `lab.status`: returns lab-oriented VM status and next steps.
- New MCP tools:
  `box_list_labs`, `box_request_lab`, and `box_lab_status`.
  Rich profiles now expose 28 tools; compact profiles expose 16 tools with
  lab-first instructions; readonly exposes 11 inspection tools.
- New CLI commands:
  `boxer labs`, `boxer lab list`, `boxer lab request`, and
  `boxer lab status`.
- Documentation added at `docs/Agent-Labs.md` explaining the product niche,
  research consolidation, recipes, MCP flow, CLI flow, and explicit bounds.

Reference sources used for this direction:

- Claude Code dev containers: https://code.claude.com/docs/en/devcontainer
- Claude Code MCP workflows: https://code.claude.com/docs/en/mcp
- OpenAI local shell sandbox guidance: https://developers.openai.com/api/docs/guides/tools-local-shell
- VM-backed sandbox MCP example: https://github.com/bird/sandbox-mcp
- Container sandbox MCP example: https://protodex.io/servers/automata-labs-team-code-sandbox-mcp.html

## Milestone 8: Android End-to-End App Testing

Status: implemented in this branch (live emulator pass pending host SDK — see note).

Bring Android app testing into Boxer's lab model using the official SDK Android
Emulator, controlled agentically over ADB.

Decisions (consultation 2026-06-20):

- Backend: the **SDK Android Emulator** (`emulator` + `sdkmanager` system images),
  not android-x86, Cuttlefish, or Waydroid. Best app-test fidelity, official
  variants, ADB, headless ATD images.
- Execution: **host-direct managed process** supervised by boxerd (needs Android
  SDK + `/dev/kvm` on the host), not nested-in-libvirt-VM.
- Images: drive `sdkmanager` against an **allowlist of system-image package
  specs** (`config/android.yaml`); sdkmanager performs its own download +
  checksum verification + license acceptance. Android blobs are not added to the
  `images.yaml` URL allowlist.
- Control: **ad-hoc UIAutomator + structured logcat first** (dump the UI tree as
  compact JSON, tap/swipe/type by selector, screenshots), with an instrumentation
  runner (`am instrument`) as a secondary path. No in-app SDK.
- Data model: **unified with VMs** — Android devices reuse the `vms` record,
  leases, policy, per-project caps, and queue via a `kind` discriminator and a
  backend dispatch (`libvirt` vs `emulator`); they appear in `box_list_vms`,
  `box_get_vm`, `box_delete_vm`, `box_extend_lease`.

Catalog seed: API 28-30, 33/34, 35/36, `google_apis` + `aosp_atd`, x86_64 +
arm64-v8a. Automation defaults to `google_apis`/`aosp_atd` (never
`google_apis_playstore`, which blocks `adb root`).

Progress journal:

- [x] DB `kind` column + `VMRecord.kind` (backend dispatch wired in daemon/lease below).
- [x] `android_sdk.py` (SDK discovery + KVM/license preflight).
- [x] `emulator_ops.py` (AVD create/launch/boot-wait/state/stop/delete, port alloc).
- [x] `adb_ops.py` (UIAutomator dump→JSON, input, app control, logcat, instrument).
- [x] `config/android.yaml` catalog + loader + config keys.
- [x] Backend dispatch (`_ops_for`) wired in daemon + lease manager.
- [x] IPC handlers (`android.list_images`, `android.request`, `android.adb`) + queue job type.
- [x] MCP tools (`box_request_android`, `box_android_ui/app/screenshot/logcat`, `box_run_instrumentation`) + opt-in `android` profile.
- [x] CLI (`boxer android images|request|ui|app|logcat`).
- [x] Docs (`docs/Android-Testing.md`) + tests (catalog, kind roundtrip, ADB/emulator parsers).

Note: emulator/ADB code is unit-tested via pure parsers and dry-run import; a live
end-to-end pass needs the Android SDK + system images + `/dev/kvm` on the host
(multi-GB, one-time `sdkmanager --licenses`). `box_list_android_images` reports
SDK/KVM availability for that check.

Reference sources used for this direction:

- Android emulator command-line / headless: https://developer.android.com/studio/run/emulator-commandline
- AVD system images & ATD: https://developer.android.com/studio/run/managing-avds
- sdkmanager: https://developer.android.com/tools/sdkmanager
- adb (logcat, am instrument, uiautomator): https://developer.android.com/tools/adb
- UI Automator: https://developer.android.com/training/testing/other-components/ui-automator

## Milestone 9: Linux Guest Utils

Status: implemented in this branch.

Give agents structured, token-efficient system info and control inside Linux
guests instead of guessing shell commands over an SSH terminal.

Decisions (consultation 2026-06-20):

- Architecture: **hybrid**. Daemon-side curated JSON tools are the zero-install
  baseline (work on every distro, BYOISO, over the existing SSH channel); an
  optional **static Go binary** in-guest agent (`boxerctl`, installed via a
  cloud-init profile) provides richer single-round-trip telemetry. The MCP
  surface is identical either way; the daemon prefers `boxerctl` when present.
- Capabilities (all four): system snapshot, services & processes, packages
  (query/search/install — install is policy-gated and mutating), logs & health.

Progress journal:

- [x] `guest_utils.py` (daemon-side, family-aware parsers, boxerctl preferred).
- [x] `agent/boxerctl` Go source + build script + injection wiring + `boxerctl` profile.
- [x] IPC handlers (`guest.sysinfo`, `guest.services`, `guest.packages`, `guest.logs`) + ownership policy.
- [x] MCP tools (`box_sysinfo`, `box_services`, `box_packages`, `box_logs`) + rich/compact/readonly exposure.
- [x] CLI (`boxer guest info|services|packages|logs`).
- [x] Docs (`docs/Guest-Utils.md`) + tests (sysinfo/services/logs parsers, boxerctl-preferred dispatch).

## Milestone 10: Guest File Transfer & Robust Exec

Status: implemented (2026-10-10).

- `vm.push_files` / `vm.pull` IPC methods (ownership-checked, absolute guest paths, 32 MiB cap,
  64 files per push, base64 payloads), carried over the existing asyncssh channel: no scp/sftp.
- MCP `box_push` / `box_pull` (rich and compact profiles); packing and unpacking happen in the
  caller's process, so host paths use the user's own permissions. Directory pulls unpack with
  `tarfile` `filter="data"` (no path traversal).
- `vm.exec`: `detach` (setsid/nohup, pid + log path) and `wait_ssh_seconds` (connect retry).
- `box_screenshot(save_path=...)`; CLI `boxer exec | push | pull | screenshot`.
- IPC line limit raised from asyncio's 64 KiB default to 64 MiB on both ends.
- SSH key falls back to `/etc/boxer/boxer_id_ed25519` when `boxer.yaml` leaves it unset, so
  `scripts/reload.sh` re-syncing `boxer.yaml` no longer disables exec.

