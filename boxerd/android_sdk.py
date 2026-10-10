"""Android SDK discovery and host preflight for the emulator subsystem.

Boxer drives the official command-line SDK tools (``sdkmanager``, ``avdmanager``,
``emulator``, ``adb``) as host-direct managed processes. This module locates a
usable SDK, surfaces actionable errors when it is missing, installs allowlisted
system-image packages via ``sdkmanager``, and checks for ``/dev/kvm`` so that the
emulator can use hardware acceleration.
"""
from __future__ import annotations

import logging
import os
import subprocess
from pathlib import Path
from typing import Optional

from boxer.config import BoxerConfig, get_config
from boxer.ipc import ERR_INTERNAL, ERR_INVALID_PARAMS, IPCError

logger = logging.getLogger(__name__)


class AndroidSDK:
    """Resolve SDK tool paths and run sdkmanager/avdmanager preflight."""

    def __init__(self, cfg: Optional[BoxerConfig] = None):
        self._cfg = cfg or get_config()

    # ----------------------------------------------------------------- discovery

    def sdk_root(self) -> Optional[Path]:
        """Best-effort SDK root from config or the standard env vars."""
        raw = (
            self._cfg.android_sdk_root
            or os.environ.get("ANDROID_SDK_ROOT")
            or os.environ.get("ANDROID_HOME")
        )
        return Path(raw).expanduser() if raw else None

    def _tool(self, *candidates: str) -> Optional[Path]:
        """Return the first existing tool path among SDK-relative candidates."""
        root = self.sdk_root()
        if root is not None:
            for rel in candidates:
                p = root / rel
                if p.exists():
                    return p
        # Fall back to PATH (developer machines often export the tools).
        from shutil import which

        name = Path(candidates[0]).name
        found = which(name)
        return Path(found) if found else None

    def sdkmanager(self) -> Optional[Path]:
        return self._tool(
            "cmdline-tools/latest/bin/sdkmanager",
            "cmdline-tools/bin/sdkmanager",
            "tools/bin/sdkmanager",
        )

    def avdmanager(self) -> Optional[Path]:
        return self._tool(
            "cmdline-tools/latest/bin/avdmanager",
            "cmdline-tools/bin/avdmanager",
            "tools/bin/avdmanager",
        )

    def emulator(self) -> Optional[Path]:
        return self._tool("emulator/emulator")

    def adb(self) -> Optional[Path]:
        return self._tool("platform-tools/adb")

    # ----------------------------------------------------------------- preflight

    @staticmethod
    def kvm_available() -> bool:
        """True when /dev/kvm exists and is read/writable by this process."""
        return os.access("/dev/kvm", os.R_OK | os.W_OK)

    def availability(self) -> dict[str, object]:
        """Return a structured readiness report (never raises)."""
        return {
            "enabled": self._cfg.android_enabled,
            "sdk_root": str(self.sdk_root()) if self.sdk_root() else None,
            "sdkmanager": str(self.sdkmanager()) if self.sdkmanager() else None,
            "avdmanager": str(self.avdmanager()) if self.avdmanager() else None,
            "emulator": str(self.emulator()) if self.emulator() else None,
            "adb": str(self.adb()) if self.adb() else None,
            "kvm": self.kvm_available(),
        }

    def require_ready(self) -> None:
        """Raise IPCError with an actionable message if the SDK is not usable."""
        if not self._cfg.android_enabled:
            raise IPCError(
                ERR_INVALID_PARAMS,
                "Android support is disabled. Set android_enabled: true in boxer.yaml "
                "after installing the Android command-line SDK.",
            )
        missing = [
            name
            for name, path in (
                ("sdkmanager", self.sdkmanager()),
                ("avdmanager", self.avdmanager()),
                ("emulator", self.emulator()),
                ("adb", self.adb()),
            )
            if path is None
        ]
        if missing:
            raise IPCError(
                ERR_INTERNAL,
                f"Android SDK tools not found: {', '.join(missing)}. Set android_sdk_root "
                "in boxer.yaml (or ANDROID_SDK_ROOT) to a SDK with cmdline-tools, "
                "platform-tools, and emulator installed.",
            )
        if not self.kvm_available():
            raise IPCError(
                ERR_INTERNAL,
                "/dev/kvm is not accessible; the Android emulator needs KVM acceleration. "
                "Ensure the boxerd user is in the kvm group and virtualization is enabled.",
            )

    # ----------------------------------------------------------------- sdkmanager

    def ensure_system_image(self, package_spec: str, timeout: int = 1800) -> None:
        """Install an allowlisted system-image package via sdkmanager (idempotent)."""
        sdkmanager = self.sdkmanager()
        if sdkmanager is None:
            raise IPCError(ERR_INTERNAL, "sdkmanager not found")
        env = self._sdk_env()
        # Accept the license non-interactively, then install. sdkmanager is a
        # no-op when the package is already present.
        result = subprocess.run(
            [str(sdkmanager), package_spec],
            input="y\n" * 16,
            capture_output=True,
            text=True,
            timeout=timeout,
            env=env,
        )
        if result.returncode != 0:
            raise IPCError(
                ERR_INTERNAL,
                f"sdkmanager failed to install '{package_spec}': "
                f"{(result.stderr or result.stdout or '').strip()[:500]}",
            )

    def accept_licenses(self, timeout: int = 300) -> None:
        sdkmanager = self.sdkmanager()
        if sdkmanager is None:
            raise IPCError(ERR_INTERNAL, "sdkmanager not found")
        subprocess.run(
            [str(sdkmanager), "--licenses"],
            input="y\n" * 64,
            capture_output=True,
            text=True,
            timeout=timeout,
            env=self._sdk_env(),
        )

    def _sdk_env(self) -> dict[str, str]:
        env = dict(os.environ)
        root = self.sdk_root()
        if root is not None:
            env["ANDROID_SDK_ROOT"] = str(root)
            env["ANDROID_HOME"] = str(root)
        return env
