"""Client-side helpers for vm.push_files / vm.pull, shared by the MCP tools and the CLI.

Packing and unpacking happen in the caller's process (the MCP server or CLI runs as the
user), so local paths are read and written with the user's own permissions; boxerd only
ever sees bytes and guest paths.
"""
from __future__ import annotations

import base64
import io
import os
import tarfile
from pathlib import Path
from typing import Any

# Never ship these when pushing a directory tree.
_SKIP_NAMES = {".git", "__pycache__", ".zig-cache", "zig-out", "node_modules", ".venv"}


def _tar_filter(info: tarfile.TarInfo) -> tarfile.TarInfo | None:
    if any(part in _SKIP_NAMES for part in Path(info.name).parts):
        return None
    info.uid = info.gid = 0
    info.uname = info.gname = ""
    return info


def pack_local(local_path: str, remote_path: str) -> dict[str, Any]:
    """Return one vm.push_files item for a local file or directory.

    A directory becomes a .tar.gz unpacked *into* remote_path (remote_path/<contents>).
    """
    src = Path(local_path).expanduser()
    if not src.exists():
        raise FileNotFoundError(f"local path not found: {src}")
    if src.is_dir():
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:gz") as tar:
            for child in sorted(src.iterdir()):
                tar.add(str(child), arcname=child.name, filter=_tar_filter)
        data = buf.getvalue()
        return {"path": remote_path, "data_b64": base64.b64encode(data).decode(), "extract": True}
    data = src.read_bytes()
    return {
        "path": remote_path,
        "data_b64": base64.b64encode(data).decode(),
        "mode": os.stat(src).st_mode & 0o777,
    }


def write_pull(result: dict[str, Any], local_path: str, overwrite: bool = False) -> dict[str, Any]:
    """Materialise a vm.pull result locally: a file, or a directory for archives."""
    dest = Path(local_path).expanduser()
    data = base64.b64decode(result["data_b64"])
    if result.get("is_archive"):
        if dest.exists() and any(dest.iterdir()) and not overwrite:
            raise FileExistsError(f"{dest} exists and is not empty (pass overwrite)")
        dest.mkdir(parents=True, exist_ok=True)
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tar:
            tar.extractall(dest, filter="data")
        return {"local_path": str(dest), "bytes": len(data), "is_archive": True}
    if dest.exists() and not overwrite:
        raise FileExistsError(f"{dest} exists (pass overwrite)")
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(data)
    return {"local_path": str(dest), "bytes": len(data), "is_archive": False}


def save_png(png_b64: str, local_path: str) -> dict[str, Any]:
    """Write a base64 PNG to disk; return its path, size and pixel dimensions."""
    dest = Path(local_path).expanduser()
    dest.parent.mkdir(parents=True, exist_ok=True)
    data = base64.b64decode(png_b64)
    dest.write_bytes(data)
    width = height = None
    if data[:8] == b"\x89PNG\r\n\x1a\n" and len(data) >= 24:
        width = int.from_bytes(data[16:20], "big")
        height = int.from_bytes(data[20:24], "big")
    return {"local_path": str(dest), "bytes": len(data), "width": width, "height": height}
