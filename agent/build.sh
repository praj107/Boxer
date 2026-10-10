#!/usr/bin/env bash
# Build the static boxerctl in-guest agent for amd64 and arm64.
#
# Output goes to agent/dist/boxerctl-linux-<arch>. When present, boxerd injects
# the arch-matched binary into guests that request the `boxerctl` profile (via
# cloud-init write_files); guests without it fall back to daemon-side shell
# parsing, so this build is optional.
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
dist="${here}/dist"
mkdir -p "${dist}"

build() {
  local arch="$1"
  echo "building boxerctl-linux-${arch}"
  ( cd "${here}" && CGO_ENABLED=0 GOOS=linux GOARCH="${arch}" \
      go build -trimpath -ldflags='-s -w' \
      -o "${dist}/boxerctl-linux-${arch}" ./boxerctl )
}

build amd64
build arm64
echo "done -> ${dist}"
