"""FastMCP server setup — reads CLAUDE_PROJECT_DIR and derives caller identity."""
from __future__ import annotations

import hashlib
import os
from pathlib import Path
from typing import Any

from boxer.config import get_config
from boxer_mcp.ipc_client import make_call


def _derive_project_id(project_dir: str) -> str:
    real = os.path.realpath(project_dir)
    return "p_" + hashlib.sha256(real.encode()).hexdigest()[:16]


def get_caller_params() -> dict[str, Any]:
    project_dir = os.environ.get("CLAUDE_PROJECT_DIR", os.getcwd())
    return {
        "caller_project_id": _derive_project_id(project_dir),
        "caller_user": os.environ.get("USER", "unknown"),
        "caller_is_admin": False,
    }


async def ipc(method: str, params: dict[str, Any]) -> Any:
    cfg = get_config()
    merged = {**get_caller_params(), **params}
    return await make_call(cfg.socket_path, method, merged)
