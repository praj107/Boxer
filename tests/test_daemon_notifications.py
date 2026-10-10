"""Tests for daemon-side project activity and notification delivery."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

import pytest

from boxer.ipc import ERR_INVALID_PARAMS, IPCError
from boxerd.daemon import BoxerDaemon
from boxerd.db import Database


@pytest.mark.asyncio
async def test_project_activity_wrapper_records_call_and_attaches_notice(db: Database) -> None:
    daemon = object.__new__(BoxerDaemon)
    daemon._db = db
    await db.add_project_notification(
        "p_aaa111",
        "WARN",
        "Boxer forced off stale VM vm_deadbe",
        vm_id="vm_deadbe",
        details={"process_snapshot": {"lines": ["42 python"]}},
    )

    async def handler(params: dict[str, Any]) -> dict[str, Any]:
        return {"ok": True}

    wrapped = BoxerDaemon._with_project_activity(daemon, "vm.get", handler)
    result = await wrapped({"caller_project_id": "p_aaa111", "caller_user": "alice"})

    assert result["ok"] is True
    assert result["boxer_notices"][0]["vm_id"] == "vm_deadbe"
    assert result["boxer_notices"][0]["details"]["process_snapshot"]["lines"] == ["42 python"]
    assert await db.list_pending_project_notifications("p_aaa111") == []
    assert await db.count_project_calls_since(
        "p_aaa111",
        datetime.now(timezone.utc) - timedelta(minutes=1),
    ) == 1


@pytest.mark.asyncio
async def test_project_activity_wrapper_leaves_notice_pending_for_list_result(db: Database) -> None:
    daemon = object.__new__(BoxerDaemon)
    daemon._db = db
    note = await db.add_project_notification(
        "p_aaa111",
        "WARN",
        "Boxer forced off stale VM vm_deadbe",
        vm_id="vm_deadbe",
    )

    async def handler(params: dict[str, Any]) -> list[dict[str, Any]]:
        return [{"vm_id": "vm_live"}]

    wrapped = BoxerDaemon._with_project_activity(daemon, "vm.list", handler)
    result = await wrapped({"caller_project_id": "p_aaa111", "caller_user": "alice"})

    assert result == [{"vm_id": "vm_live"}]
    pending = await db.list_pending_project_notifications("p_aaa111")
    assert [n.id for n in pending] == [note.id]


@pytest.mark.asyncio
async def test_project_activity_wrapper_attaches_notice_to_ipc_error(db: Database) -> None:
    daemon = object.__new__(BoxerDaemon)
    daemon._db = db
    note = await db.add_project_notification(
        "p_aaa111",
        "WARN",
        "Boxer forced off stale VM vm_deadbe",
        vm_id="vm_deadbe",
    )

    async def handler(params: dict[str, Any]) -> dict[str, Any]:
        raise IPCError(ERR_INVALID_PARAMS, "VM is stopped")

    wrapped = BoxerDaemon._with_project_activity(daemon, "vm.exec", handler)
    with pytest.raises(IPCError) as excinfo:
        await wrapped({"caller_project_id": "p_aaa111", "caller_user": "alice"})

    assert excinfo.value.data["boxer_notices"][0]["notification_id"] == note.id
    assert await db.list_pending_project_notifications("p_aaa111") == []
