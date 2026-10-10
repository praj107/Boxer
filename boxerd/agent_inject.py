"""Inject the optional in-guest boxerctl agent through cloud-init.

The ``boxerctl`` profile is special: instead of expanding into packages/runcmd
like the other named profiles, it ships the prebuilt static binary into the guest
(when ``agent/dist/boxerctl-linux-<arch>`` exists). The binary is delivered as a
base64 *text* file (valid UTF-8, so it rides the existing WriteFile path) and a
runcmd decodes it into /usr/local/bin. If the binary has not been built, the
profile is a graceful no-op and the daemon's shell-based guest_utils fallback
still works.
"""
from __future__ import annotations

import base64
import logging
from pathlib import Path
from typing import Optional

from boxerd.cloud_init import WriteFile

logger = logging.getLogger(__name__)

BOXERCTL_PROFILE = "boxerctl"
_DIST_DIR = Path(__file__).parent.parent / "agent" / "dist"
_STAGE_PATH = "/opt/boxer/boxerctl.b64"

BOXERCTL_RUNCMD = [
    "mkdir -p /usr/local/bin /opt/boxer",
    f"base64 -d {_STAGE_PATH} > /usr/local/bin/boxerctl",
    "chmod 0755 /usr/local/bin/boxerctl",
    f"rm -f {_STAGE_PATH}",
]


def boxerctl_binary_path(arch: str = "amd64") -> Optional[Path]:
    p = _DIST_DIR / f"boxerctl-linux-{arch}"
    return p if p.exists() else None


def boxerctl_injection(arch: str = "amd64") -> Optional[tuple[WriteFile, list[str]]]:
    """Return (write_file, runcmd) to install boxerctl, or None if not built."""
    binary = boxerctl_binary_path(arch)
    if binary is None:
        logger.info("boxerctl profile requested but no prebuilt binary for arch=%s; "
                    "guest will use daemon-side shell fallback", arch)
        return None
    encoded = base64.b64encode(binary.read_bytes()).decode("ascii")
    write_file = WriteFile(
        path=_STAGE_PATH,
        content=encoded,
        permissions="0644",
        owner="root:root",
    )
    return write_file, list(BOXERCTL_RUNCMD)
