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

Traditional installer ISOs need a separate flow from cloud images.

Planned work:

- Introduce an installer job type with its own queue and concurrency controls.
- Create blank target disks instead of qcow2 overlays.
- Boot ISO first, then switch boot order to disk after install.
- Support unattended install methods where practical:
  - Ubuntu autoinstall;
  - Debian preseed or autoinstall-equivalent workflows;
  - Fedora/RHEL-family Kickstart;
  - Arch cloud-image preferred, manual ISO only for headed workflows.
- Capture serial logs and installer state.
- Keep ISO caches separate from cloud-image caches.

## Milestone 4: Image Families and Refresh Policy

Improve catalog ergonomics without widening the attack surface.

Planned work:

- First-class image families for Ubuntu, Debian, Fedora, AlmaLinux, Rocky, and Arch.
- `refresh_policy` support:
  - `pinned`: exact hash required;
  - `latest`: refresh when upstream manifest hash changes;
  - `manual`: admin-triggered refresh only.
- Cache by verified digest instead of only template name.
- Add prune commands for old verified base artifacts.
- Add image listing/prewarm tools for agents and admins.

## Milestone 5: Rich Guest Profiles

Make common agent VM setups one request instead of a setup transcript.

Planned work:

- Named bootstrap profiles such as `python`, `node`, `browser`, `docker`, and `desktop`.
- Optional wait conditions: SSH ready, package install complete, command success, guest-agent ready.
- File injection through cloud-init `write_files`.
- Secret handling rules for one-time credentials and private material.
- MCP schemas that keep setup inputs bounded and explicit.
