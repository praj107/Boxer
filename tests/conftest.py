"""Pytest fixtures: in-memory SQLite database and mock libvirt connection."""
from __future__ import annotations

import asyncio
from pathlib import Path
from typing import AsyncGenerator
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio

from boxer.config import BoxerConfig
from boxer.types import CallerIdentity
from boxerd.db import Database


@pytest.fixture
def cfg(tmp_path: Path) -> BoxerConfig:
    data = {
        "state_dir": str(tmp_path),
        "socket_path": str(tmp_path / "boxer.sock"),
        "notify_socket_path": str(tmp_path / "boxer-notify.sock"),
        "libvirt_uri": "test:///default",
        "host_reserved_ram_gib": 1,
        "max_running_per_project": 2,
        "max_disk_per_project_gib": 50,
        "default_ttl_minutes": 60,
        "stale_warn_grace_minutes": 5,
        "stale_delete_grace_hours": 1,
        "network_base_cidr": "10.200.0.0/16",
        "network_prefix_len": 24,
        "admin_group": "boxer-admin",
    }
    return BoxerConfig(data)


@pytest_asyncio.fixture
async def db(cfg: BoxerConfig) -> AsyncGenerator[Database, None]:
    database = Database(cfg.db_path)
    await database.open()
    yield database
    await database.close()


@pytest.fixture
def mock_libvirt_conn() -> MagicMock:
    conn = MagicMock()
    conn.listAllDomains.return_value = []
    conn.storagePoolLookupByName.side_effect = Exception("not found")
    conn.networkLookupByName.side_effect = Exception("not found")
    return conn


@pytest.fixture
def caller_a() -> CallerIdentity:
    return CallerIdentity(project_id="p_aaa111", user="alice")


@pytest.fixture
def caller_b() -> CallerIdentity:
    return CallerIdentity(project_id="p_bbb222", user="bob")


@pytest.fixture
def admin_caller() -> CallerIdentity:
    return CallerIdentity(project_id="admin", user="root", is_admin=True)
