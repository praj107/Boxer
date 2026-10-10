"""FastMCP server setup - derives caller identity for Boxer isolation."""
from __future__ import annotations

import hashlib
import os
from typing import Any

from boxer.config import get_config
from boxer_mcp.ipc_client import make_call


def _derive_project_id(project_dir: str) -> str:
    real = os.path.realpath(project_dir)
    return "p_" + hashlib.sha256(real.encode()).hexdigest()[:16]


def _caller_project_dir() -> str:
    """Return the project root used for Boxer isolation.

    BOXER_PROJECT_DIR is client-neutral and should be used for non-Claude hosts
    whose stdio working directory is not guaranteed to be the repository root.
    CLAUDE_PROJECT_DIR remains supported for existing Claude Code setups.
    """
    return os.environ.get("BOXER_PROJECT_DIR") or os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd()


def get_caller_params() -> dict[str, Any]:
    project_dir = _caller_project_dir()
    project_path = os.path.realpath(project_dir)
    return {
        "caller_project_id": _derive_project_id(project_path),
        "caller_project_path": project_path,
        "caller_user": os.environ.get("USER", "unknown"),
        "caller_is_admin": False,
    }


async def ipc(method: str, params: dict[str, Any]) -> Any:
    cfg = get_config()
    merged = {**get_caller_params(), **params}
    return await make_call(cfg.socket_path, method, merged)
