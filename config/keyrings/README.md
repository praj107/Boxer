# Boxer trusted keyrings

This directory holds the **pinned PGP keyrings** Boxer uses to verify the
authenticity of image checksum manifests (Roadmap Milestone 2).

`boxerd` verifies signatures with `gpgv`, which reads *only* the keyring file
you place here — it never consults your user GnuPG trust database and cannot
import keys. The keyring is therefore the single trust anchor; curate it
deliberately.

On install, `scripts/setup.sh` creates `/etc/boxer/keyrings` and copies any
`*.gpg` / `*.kbx` files found in this directory into it. The keyring filename in
`images.yaml` (`verification.signature.keyring`) is resolved relative to
`keyrings_dir` (default `/etc/boxer/keyrings`) unless it is an absolute path.

## Expected keyrings

| Filename                        | Distro signing key it must contain                       |
| ------------------------------- | -------------------------------------------------------- |
| `ubuntu-cloudimage-keyring.gpg` | Ubuntu Cloud Image signing key (signs `SHA256SUMS.gpg`)  |
| `debian-cloud-keyring.gpg`      | Debian Cloud Images signing key (signs `SHA512SUMS.sign`)|
| `fedora-44-keyring.gpg`         | Fedora 44 release key (clearsigns `*-CHECKSUM`)          |
| `almalinux-9-keyring.gpg`       | AlmaLinux 9 release key (clearsigns `CHECKSUM`)          |

These public keys are **not bundled** with Boxer: they should be obtained and
cross-checked through each distro's own out-of-band channel, then exported into
a keyring here. For example, to build the Ubuntu keyring from a key you have
already verified:

```bash
gpg --no-default-keyring --keyring ./ubuntu-cloudimage-keyring.gpg \
    --import ubuntu-cloud-image-signing-key.asc
```

## Enabling fail-closed verification

Catalog entries ship with `signature.required: false`, so a fresh install still
verifies images by checksum manifest. Once the matching keyring is installed,
set `required: true` on that entry (and optionally pin
`signature.fingerprints`) so Boxer refuses to cache an image whose manifest
signature cannot be confirmed.

Run `boxer image-trust` to preflight every configured trust chain without
downloading the artifacts.
