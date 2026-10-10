# Android End-to-End App Testing (Milestone 8)

Boxer can boot official **SDK Android Emulator** devices and drive apps over ADB,
so an agent can run real end-to-end app tests next to its Linux VMs. This note
explains the design, the host requirements, and the control surface.

## Why the SDK emulator (and not the alternatives)

The consultation compared four backends:

| Backend | Fit | App fidelity | Notes |
|---|---|---|---|
| **SDK Android Emulator** (chosen) | host-direct process | best, official | all API levels & variants, ADB, headless ATD images |
| android-x86 / Bliss OS | libvirt VM | low | flaky ARM-app translation, unofficial |
| Cuttlefish | crosvm + own launcher | high (platform) | heavy host setup, overkill for app E2E |
| Waydroid / Redroid | host-kernel container | medium | breaks Boxer's clean-VM isolation |

The SDK emulator wins on app-test fidelity, variant coverage, and ADB tooling.
Its cost is that it is **not a libvirt domain** — it is a host process Boxer
supervises through a small device backend.

## Host requirements

- Android command-line SDK with `cmdline-tools`, `platform-tools`, and `emulator`.
- `/dev/kvm` accessible to the `boxerd` user (KVM acceleration).
- Set in `boxer.yaml`:
  ```yaml
  android_enabled: true
  android_sdk_root: /opt/android-sdk    # or export ANDROID_SDK_ROOT
  max_concurrent_emulators: 1
  ```
- One-time license acceptance: `sdkmanager --licenses`.

`box_list_android_images` reports an `availability` block so you can confirm the
SDK, KVM, and each tool are detected before requesting a device.

## Data model: unified with VMs

Android devices reuse the `vms` table via a `kind = "android"` discriminator and a
backend dispatch (`BoxerDaemon._ops_for`): the **libvirt backend** for normal VMs,
the **emulator backend** (`boxerd/emulator_ops.py`) for Android. This means
Android devices share leases, per-project caps, the admission/queue path, policy,
and the standard lifecycle tools (`box_list_vms`, `box_get_vm`, `box_start_vm`,
`box_stop_vm`, `box_restart_vm`, `box_delete_vm`, `box_extend_lease`). For an
Android row, `libvirt_name` holds the AVD name (`boxer_<vm_id>`), `template` the
catalog image name, and `ip_address`/tags hold the adb serial (`emulator-<port>`).

A dedicated `max_concurrent_emulators` cap (checked in `android.request`) bounds
how many emulators run at once; over-cap requests queue with `job_type: android`
and are promoted by the existing queue runner.

## Image catalog (`config/android.yaml`)

An allowlist of `sdkmanager` system-image package specs. Boxer drives
`sdkmanager <package>` (which performs its own download, checksum verification,
and license acceptance) — Android blobs are deliberately not fetched through the
`images.yaml` URL allowlist. Seeded coverage:

- API 28–30, 33/34, 35/36
- variants `google_apis` and `aosp_atd`
- ABIs `x86_64` (KVM-accelerated) and `arm64-v8a`

`google_apis_playstore` is intentionally excluded: it blocks `adb root` and needs
a signed-in account, which breaks automation.

## Control surface

Primary style: **ad-hoc UIAutomator + structured logcat** (no test APK required).

MCP tools (opt-in `android` profile, or the rich profiles' `box_list_android_images`):

- `box_request_android(template, purpose, …, wait_boot_seconds)` — boot a device.
- `box_android_ui(vm_id, action=dump|tap|swipe|text|key, …)` — `dump` returns the
  UI tree as compact JSON (resource-id/text/desc/class + tap center); the rest
  drive input by coordinate.
- `box_android_app(vm_id, action=install|launch|stop|clear|current_activity|pull, …)`.
- `box_android_screenshot(vm_id)` — base64 PNG.
- `box_android_logcat(vm_id, tag?, priority?, package?, limit)` — structured
  `{ts, pid, tid, level, tag, msg}` records.
- `box_run_instrumentation(vm_id, test_package, runner?, test_class?)` — secondary
  path for prepared Espresso/UIAutomator suites; returns structured pass/fail.

CLI: `boxer android images|request|ui|app|logcat`.

## Bounds

- Android support is off until `android_enabled: true` and the SDK is present.
- Emulators are host processes; they are discovered across daemon restarts by
  scanning `/proc` for the `-avd <name>` command line, and cleaned up by the lease
  manager via the emulator backend.
- The host CPU/RAM admission check reads libvirt usage and does not see other
  emulators' usage directly; `max_concurrent_emulators` is the primary guard.
