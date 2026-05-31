"""Per-VM ephemeral SSH key management."""
from __future__ import annotations

import asyncio
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path

from boxer.ipc import ERR_INTERNAL, IPCError


@dataclass(frozen=True)
class SSHKeyMaterial:
    private_key_path: Path
    public_key_path: Path
    private_key: str
    public_key: str


class SSHKeyManager:
    def key_path(self, vm_dir: Path) -> Path:
        return vm_dir / "ssh" / "ephemeral_ed25519"

    def has_key(self, vm_dir: Path) -> bool:
        path = self.key_path(vm_dir)
        return path.exists() and path.with_suffix(path.suffix + ".pub").exists()

    async def ensure_keypair(self, vm_dir: Path, vm_id: str) -> SSHKeyMaterial:
        key_path = self.key_path(vm_dir)
        pub_path = key_path.with_suffix(key_path.suffix + ".pub")
        if not key_path.exists() or not pub_path.exists():
            key_path.parent.mkdir(parents=True, exist_ok=True)
            loop = asyncio.get_running_loop()
            try:
                await loop.run_in_executor(
                    None,
                    lambda: subprocess.run(
                        [
                            "ssh-keygen",
                            "-t",
                            "ed25519",
                            "-N",
                            "",
                            "-C",
                            f"boxer-{vm_id}-ephemeral",
                            "-f",
                            str(key_path),
                            "-q",
                        ],
                        check=True,
                        capture_output=True,
                    ),
                )
            except FileNotFoundError as exc:
                raise IPCError(ERR_INTERNAL, "ssh-keygen is required for ephemeral SSH access") from exc
            except subprocess.CalledProcessError as exc:
                stderr = exc.stderr.decode(errors="replace")
                raise IPCError(ERR_INTERNAL, f"ssh-keygen failed: {stderr}") from exc
            os.chmod(key_path, 0o600)
            os.chmod(pub_path, 0o644)

        return SSHKeyMaterial(
            private_key_path=key_path,
            public_key_path=pub_path,
            private_key=key_path.read_text(),
            public_key=pub_path.read_text().strip(),
        )
