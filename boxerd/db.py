"""aiosqlite database layer — schema + CRUD helpers."""
from __future__ import annotations

import json
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, AsyncIterator, Optional

import aiosqlite

from boxer.types import Event, QueueEntry, VMRecord

_SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS projects (
    id        TEXT PRIMARY KEY,
    path      TEXT UNIQUE NOT NULL,
    first_seen TEXT NOT NULL,
    last_seen  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS vms (
    id           TEXT PRIMARY KEY,
    libvirt_name TEXT UNIQUE NOT NULL,
    project_id   TEXT NOT NULL,
    display_name TEXT NOT NULL,
    state        TEXT NOT NULL,
    owner_user   TEXT NOT NULL,
    template     TEXT NOT NULL,
    cpu          INTEGER NOT NULL,
    ram_mb       INTEGER NOT NULL,
    disk_gb      INTEGER NOT NULL,
    headless     INTEGER NOT NULL,
    ip_address   TEXT,
    created_at   TEXT NOT NULL,
    last_touched TEXT NOT NULL,
    lease_until  TEXT NOT NULL,
    FOREIGN KEY (project_id) REFERENCES projects(id)
);

CREATE TABLE IF NOT EXISTS vm_tags (
    vm_id TEXT NOT NULL,
    key   TEXT NOT NULL,
    value TEXT NOT NULL,
    PRIMARY KEY (vm_id, key),
    FOREIGN KEY (vm_id) REFERENCES vms(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS queue (
    id           TEXT PRIMARY KEY,
    project_id   TEXT NOT NULL,
    request_json TEXT NOT NULL,
    priority     INTEGER NOT NULL DEFAULT 0,
    status       TEXT NOT NULL,
    reason       TEXT NOT NULL DEFAULT '',
    created_at   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS events (
    id         TEXT PRIMARY KEY,
    project_id TEXT,
    vm_id      TEXT,
    level      TEXT NOT NULL,
    message    TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_vms_project ON vms(project_id);
CREATE INDEX IF NOT EXISTS idx_events_created ON events(created_at);
CREATE INDEX IF NOT EXISTS idx_queue_status ON queue(status, priority);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse_dt(s: str) -> datetime:
    return datetime.fromisoformat(s)


def _vm_from_row(row: aiosqlite.Row, tags: dict[str, str]) -> VMRecord:
    return VMRecord(
        id=row["id"],
        libvirt_name=row["libvirt_name"],
        project_id=row["project_id"],
        display_name=row["display_name"],
        state=row["state"],
        owner_user=row["owner_user"],
        template=row["template"],
        cpu=row["cpu"],
        ram_mb=row["ram_mb"],
        disk_gb=row["disk_gb"],
        headless=bool(row["headless"]),
        ip_address=row["ip_address"],
        created_at=_parse_dt(row["created_at"]),
        last_touched=_parse_dt(row["last_touched"]),
        lease_until=_parse_dt(row["lease_until"]),
        tags=tags,
        origin=row["origin"] if "origin" in row.keys() else "boxer",
    )


class Database:
    def __init__(self, path: Path):
        self._path = path
        self._db: Optional[aiosqlite.Connection] = None

    async def open(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._db = await aiosqlite.connect(str(self._path))
        self._db.row_factory = aiosqlite.Row
        await self._db.executescript(_SCHEMA)
        await self._db.commit()
        # Idempotent migration: add 'origin' column for existing DBs
        try:
            await self._db.execute(
                "ALTER TABLE vms ADD COLUMN origin TEXT NOT NULL DEFAULT 'boxer'"
            )
            await self._db.commit()
        except Exception:
            pass  # Column already exists

    async def close(self) -> None:
        if self._db:
            await self._db.close()

    @asynccontextmanager
    async def _conn(self) -> AsyncIterator[aiosqlite.Connection]:
        assert self._db is not None, "Database not opened"
        yield self._db

    # --- Projects ---

    async def upsert_project(self, project_id: str, path: str) -> None:
        async with self._conn() as db:
            now = _now()
            await db.execute(
                """INSERT INTO projects(id, path, first_seen, last_seen) VALUES(?,?,?,?)
                   ON CONFLICT(id) DO UPDATE SET last_seen=excluded.last_seen, path=excluded.path""",
                (project_id, path, now, now),
            )
            await db.commit()

    # --- VMs ---

    async def insert_vm(self, vm: VMRecord) -> None:
        async with self._conn() as db:
            await db.execute(
                """INSERT INTO vms(id,libvirt_name,project_id,display_name,state,owner_user,
                   template,cpu,ram_mb,disk_gb,headless,ip_address,created_at,last_touched,
                   lease_until,origin)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    vm.id, vm.libvirt_name, vm.project_id, vm.display_name, vm.state,
                    vm.owner_user, vm.template, vm.cpu, vm.ram_mb, vm.disk_gb,
                    int(vm.headless), vm.ip_address,
                    vm.created_at.isoformat(), vm.last_touched.isoformat(),
                    vm.lease_until.isoformat(), vm.origin,
                ),
            )
            for k, v in vm.tags.items():
                await db.execute(
                    "INSERT OR REPLACE INTO vm_tags(vm_id,key,value) VALUES(?,?,?)",
                    (vm.id, k, v),
                )
            await db.commit()

    async def get_vm(self, vm_id: str) -> Optional[VMRecord]:
        async with self._conn() as db:
            async with db.execute("SELECT * FROM vms WHERE id=?", (vm_id,)) as cur:
                row = await cur.fetchone()
            if row is None:
                return None
            tags = await self._get_tags(db, vm_id)
            return _vm_from_row(row, tags)

    async def list_vms(self, project_id: Optional[str] = None) -> list[VMRecord]:
        async with self._conn() as db:
            if project_id:
                async with db.execute("SELECT * FROM vms WHERE project_id=?", (project_id,)) as cur:
                    rows = await cur.fetchall()
            else:
                async with db.execute("SELECT * FROM vms", ()) as cur:
                    rows = await cur.fetchall()
            result = []
            for row in rows:
                tags = await self._get_tags(db, row["id"])
                result.append(_vm_from_row(row, tags))
            return result

    async def update_vm_state(self, vm_id: str, state: str) -> None:
        async with self._conn() as db:
            await db.execute(
                "UPDATE vms SET state=?, last_touched=? WHERE id=?",
                (state, _now(), vm_id),
            )
            await db.commit()

    async def update_vm_ip(self, vm_id: str, ip: str) -> None:
        async with self._conn() as db:
            await db.execute(
                "UPDATE vms SET ip_address=?, last_touched=? WHERE id=?",
                (ip, _now(), vm_id),
            )
            await db.commit()

    async def update_vm_lease(self, vm_id: str, lease_until: datetime) -> None:
        async with self._conn() as db:
            await db.execute(
                "UPDATE vms SET lease_until=?, last_touched=? WHERE id=?",
                (lease_until.isoformat(), _now(), vm_id),
            )
            await db.commit()

    async def touch_vm(self, vm_id: str) -> None:
        async with self._conn() as db:
            await db.execute("UPDATE vms SET last_touched=? WHERE id=?", (_now(), vm_id))
            await db.commit()

    async def delete_vm(self, vm_id: str) -> None:
        async with self._conn() as db:
            await db.execute("DELETE FROM vms WHERE id=?", (vm_id,))
            await db.commit()

    async def get_vm_by_libvirt_name(self, libvirt_name: str) -> Optional[VMRecord]:
        async with self._conn() as db:
            async with db.execute(
                "SELECT * FROM vms WHERE libvirt_name=?", (libvirt_name,)
            ) as cur:
                row = await cur.fetchone()
            if row is None:
                return None
            tags = await self._get_tags(db, row["id"])
            return _vm_from_row(row, tags)

    async def purge_ghost_record(self, vm_id: str) -> None:
        """Delete a DB record for a VM whose libvirt domain is confirmed absent."""
        async with self._conn() as db:
            await db.execute("DELETE FROM vms WHERE id=?", (vm_id,))
            await db.commit()

    async def _get_tags(self, db: aiosqlite.Connection, vm_id: str) -> dict[str, str]:
        async with db.execute("SELECT key,value FROM vm_tags WHERE vm_id=?", (vm_id,)) as cur:
            rows = await cur.fetchall()
        return {r["key"]: r["value"] for r in rows}

    # --- Queue ---

    async def enqueue(self, project_id: str, request: dict[str, Any], reason: str = "") -> str:
        entry_id = "q_" + uuid.uuid4().hex[:12]
        async with self._conn() as db:
            await db.execute(
                "INSERT INTO queue(id,project_id,request_json,status,reason,created_at) VALUES(?,?,?,?,?,?)",
                (entry_id, project_id, json.dumps(request), "pending", reason, _now()),
            )
            await db.commit()
        return entry_id

    async def get_queue_entry(self, entry_id: str) -> Optional[QueueEntry]:
        async with self._conn() as db:
            async with db.execute("SELECT * FROM queue WHERE id=?", (entry_id,)) as cur:
                row = await cur.fetchone()
        if row is None:
            return None
        return QueueEntry(
            id=row["id"],
            project_id=row["project_id"],
            request_json=row["request_json"],
            priority=row["priority"],
            status=row["status"],
            reason=row["reason"],
            created_at=_parse_dt(row["created_at"]),
        )

    async def list_pending_queue(self) -> list[QueueEntry]:
        async with self._conn() as db:
            async with db.execute(
                "SELECT * FROM queue WHERE status='pending' ORDER BY priority DESC, created_at ASC"
            ) as cur:
                rows = await cur.fetchall()
        return [
            QueueEntry(
                id=r["id"], project_id=r["project_id"], request_json=r["request_json"],
                priority=r["priority"], status=r["status"], reason=r["reason"],
                created_at=_parse_dt(r["created_at"]),
            )
            for r in rows
        ]

    async def update_queue_status(self, entry_id: str, status: str) -> None:
        async with self._conn() as db:
            await db.execute("UPDATE queue SET status=? WHERE id=?", (status, entry_id))
            await db.commit()

    async def expire_old_queue_entries(self, older_than_hours: int = 2) -> int:
        async with self._conn() as db:
            cur = await db.execute(
                """UPDATE queue SET status='expired'
                   WHERE status='pending'
                   AND datetime(created_at) < datetime('now', ? || ' hours')""",
                (f"-{older_than_hours}",),
            )
            await db.commit()
            return cur.rowcount

    # --- Events ---

    async def add_event(
        self,
        level: str,
        message: str,
        project_id: Optional[str] = None,
        vm_id: Optional[str] = None,
    ) -> Event:
        event_id = "ev_" + uuid.uuid4().hex[:12]
        now = _now()
        async with self._conn() as db:
            await db.execute(
                "INSERT INTO events(id,project_id,vm_id,level,message,created_at) VALUES(?,?,?,?,?,?)",
                (event_id, project_id, vm_id, level, message, now),
            )
            await db.commit()
        return Event(
            id=event_id, project_id=project_id, vm_id=vm_id,
            level=level, message=message, created_at=_parse_dt(now),
        )

    async def list_events(
        self,
        since: Optional[datetime] = None,
        level: Optional[str] = None,
        limit: int = 100,
    ) -> list[Event]:
        clauses = []
        params: list[Any] = []
        if since:
            clauses.append("created_at > ?")
            params.append(since.isoformat())
        if level:
            clauses.append("level=?")
            params.append(level)
        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        params.append(limit)
        async with self._conn() as db:
            async with db.execute(
                f"SELECT * FROM events {where} ORDER BY created_at DESC LIMIT ?", params
            ) as cur:
                rows = await cur.fetchall()
        return [
            Event(
                id=r["id"], project_id=r["project_id"], vm_id=r["vm_id"],
                level=r["level"], message=r["message"], created_at=_parse_dt(r["created_at"]),
            )
            for r in rows
        ]
