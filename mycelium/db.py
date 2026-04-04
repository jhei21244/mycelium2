"""SQLite persistence layer — async, single-file, zero config."""

import aiosqlite
import time
import uuid

SCHEMA = """
CREATE TABLE IF NOT EXISTS goals (
    id          TEXT PRIMARY KEY,
    description TEXT NOT NULL,
    status      TEXT NOT NULL DEFAULT 'planning',
    created_at  REAL NOT NULL,
    completed_at REAL
);

CREATE TABLE IF NOT EXISTS tasks (
    id          TEXT PRIMARY KEY,
    goal_id     TEXT NOT NULL REFERENCES goals(id),
    name        TEXT NOT NULL,
    description TEXT NOT NULL,
    code        TEXT NOT NULL,
    status      TEXT NOT NULL DEFAULT 'pending',
    priority    REAL NOT NULL DEFAULT 1.0,
    failures    INTEGER NOT NULL DEFAULT 0,
    max_retries INTEGER NOT NULL DEFAULT 3,
    agent_id    TEXT,
    result      TEXT,
    error       TEXT,
    created_at  REAL NOT NULL,
    available_at REAL,
    claimed_at  REAL,
    completed_at REAL
);

CREATE TABLE IF NOT EXISTS task_deps (
    task_id    TEXT NOT NULL REFERENCES tasks(id),
    depends_on TEXT NOT NULL REFERENCES tasks(id),
    PRIMARY KEY (task_id, depends_on)
);

CREATE TABLE IF NOT EXISTS agents (
    id              TEXT PRIMARY KEY,
    name            TEXT NOT NULL,
    status          TEXT NOT NULL DEFAULT 'idle',
    current_task_id TEXT,
    tasks_completed INTEGER NOT NULL DEFAULT 0,
    last_heartbeat  REAL
);

CREATE TABLE IF NOT EXISTS events (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp  REAL NOT NULL,
    event_type TEXT NOT NULL,
    entity_id  TEXT,
    detail     TEXT
);
"""


class DB:
    def __init__(self, path: str = "mycelium.db"):
        self.path = path
        self._db: aiosqlite.Connection | None = None

    async def connect(self):
        self._db = await aiosqlite.connect(self.path)
        self._db.row_factory = aiosqlite.Row
        await self._db.executescript(SCHEMA)
        await self._db.execute("PRAGMA journal_mode=WAL")
        await self._db.execute("PRAGMA busy_timeout=5000")
        await self._db.commit()

    async def close(self):
        if self._db:
            await self._db.close()

    # ── Goals ──────────────────────────────────────────────

    async def create_goal(self, description: str) -> str:
        gid = uuid.uuid4().hex[:12]
        now = time.time()
        await self._db.execute(
            "INSERT INTO goals (id, description, status, created_at) VALUES (?,?,?,?)",
            (gid, description, "planning", now),
        )
        await self._log("goal_created", gid, description)
        await self._db.commit()
        return gid

    async def update_goal_status(self, gid: str, status: str):
        now = time.time() if status in ("completed", "failed") else None
        await self._db.execute(
            "UPDATE goals SET status=?, completed_at=COALESCE(?, completed_at) WHERE id=?",
            (status, now, gid),
        )
        await self._log("goal_status", gid, status)
        await self._db.commit()

    async def get_goal(self, gid: str) -> dict | None:
        cur = await self._db.execute("SELECT * FROM goals WHERE id=?", (gid,))
        row = await cur.fetchone()
        return dict(row) if row else None

    async def list_goals(self) -> list[dict]:
        cur = await self._db.execute("SELECT * FROM goals ORDER BY created_at DESC")
        return [dict(r) for r in await cur.fetchall()]

    # ── Tasks ──────────────────────────────────────────────

    async def create_task(
        self, goal_id: str, name: str, description: str, code: str,
        priority: float = 1.0, depends_on: list[str] | None = None,
    ) -> str:
        tid = uuid.uuid4().hex[:12]
        now = time.time()
        has_deps = bool(depends_on)
        status = "pending" if has_deps else "ready"
        available_at = None if has_deps else now
        await self._db.execute(
            """INSERT INTO tasks
               (id, goal_id, name, description, code, status, priority, created_at, available_at)
               VALUES (?,?,?,?,?,?,?,?,?)""",
            (tid, goal_id, name, description, code, status, priority, now, available_at),
        )
        if depends_on:
            for dep in depends_on:
                await self._db.execute(
                    "INSERT INTO task_deps (task_id, depends_on) VALUES (?,?)",
                    (tid, dep),
                )
        await self._log("task_created", tid, f"{name} [{status}]")
        await self._db.commit()
        return tid

    async def claim_task(self, tid: str, agent_id: str) -> bool:
        """Atomic claim — returns True if this agent won the race."""
        now = time.time()
        cur = await self._db.execute(
            """UPDATE tasks SET status='running', agent_id=?, claimed_at=?
               WHERE id=? AND status='ready'""",
            (agent_id, now, tid),
        )
        await self._db.commit()
        return cur.rowcount > 0

    async def complete_task(self, tid: str, result: str):
        now = time.time()
        await self._db.execute(
            "UPDATE tasks SET status='done', result=?, completed_at=? WHERE id=?",
            (result, now, tid),
        )
        await self._log("task_done", tid, result[:200] if result else "ok")
        await self._db.commit()

    async def fail_task(self, tid: str, error: str):
        cur = await self._db.execute("SELECT failures, max_retries FROM tasks WHERE id=?", (tid,))
        row = await cur.fetchone()
        failures = row["failures"] + 1
        if failures >= row["max_retries"]:
            new_status = "failed"
        else:
            new_status = "ready"  # back into the field with amplified signal
        now = time.time()
        await self._db.execute(
            """UPDATE tasks SET status=?, failures=?, error=?, agent_id=NULL,
               available_at=? WHERE id=?""",
            (new_status, failures, error, now, tid),
        )
        await self._log("task_failed", tid, f"attempt {failures}: {error[:200]}")
        await self._db.commit()
        return new_status

    async def get_ready_tasks(self) -> list[dict]:
        cur = await self._db.execute("SELECT * FROM tasks WHERE status='ready'")
        return [dict(r) for r in await cur.fetchall()]

    async def get_tasks_for_goal(self, goal_id: str) -> list[dict]:
        cur = await self._db.execute(
            "SELECT * FROM tasks WHERE goal_id=? ORDER BY created_at", (goal_id,)
        )
        return [dict(r) for r in await cur.fetchall()]

    async def get_task(self, tid: str) -> dict | None:
        cur = await self._db.execute("SELECT * FROM tasks WHERE id=?", (tid,))
        row = await cur.fetchone()
        return dict(row) if row else None

    async def get_task_deps(self, tid: str) -> list[str]:
        cur = await self._db.execute(
            "SELECT depends_on FROM task_deps WHERE task_id=?", (tid,)
        )
        return [r["depends_on"] for r in await cur.fetchall()]

    async def promote_pending_tasks(self, goal_id: str) -> list[str]:
        """Check pending tasks whose deps are now all satisfied → ready."""
        promoted = []
        cur = await self._db.execute(
            "SELECT id FROM tasks WHERE goal_id=? AND status='pending'", (goal_id,)
        )
        pending = [dict(r) for r in await cur.fetchall()]
        now = time.time()
        for t in pending:
            deps = await self.get_task_deps(t["id"])
            if not deps:
                continue
            cur2 = await self._db.execute(
                f"SELECT COUNT(*) as c FROM tasks WHERE id IN ({','.join('?' for _ in deps)}) AND status='done'",
                deps,
            )
            row = await cur2.fetchone()
            if row["c"] == len(deps):
                await self._db.execute(
                    "UPDATE tasks SET status='ready', available_at=? WHERE id=?",
                    (now, t["id"]),
                )
                await self._log("task_promoted", t["id"], "deps satisfied → ready")
                promoted.append(t["id"])
        if promoted:
            await self._db.commit()
        return promoted

    async def check_goal_completion(self, goal_id: str) -> str | None:
        """Returns 'completed' or 'failed' if all tasks are terminal, else None."""
        cur = await self._db.execute(
            "SELECT status, COUNT(*) as c FROM tasks WHERE goal_id=? GROUP BY status",
            (goal_id,),
        )
        statuses = {r["status"]: r["c"] for r in await cur.fetchall()}
        non_terminal = sum(v for k, v in statuses.items() if k not in ("done", "failed"))
        if non_terminal > 0:
            return None
        if statuses.get("failed", 0) > 0:
            return "failed"
        return "completed"

    # ── Agents ─────────────────────────────────────────────

    async def register_agent(self, agent_id: str, name: str):
        now = time.time()
        await self._db.execute(
            """INSERT INTO agents (id, name, status, tasks_completed, last_heartbeat)
               VALUES (?,?,?,?,?)
               ON CONFLICT(id) DO UPDATE SET status='idle', last_heartbeat=?""",
            (agent_id, name, "idle", 0, now, now),
        )
        await self._log("agent_registered", agent_id, name)
        await self._db.commit()

    async def update_agent(self, agent_id: str, status: str, task_id: str | None = None):
        now = time.time()
        await self._db.execute(
            "UPDATE agents SET status=?, current_task_id=?, last_heartbeat=? WHERE id=?",
            (status, task_id, now, agent_id),
        )
        if status == "idle" and task_id is None:
            await self._db.execute(
                "UPDATE agents SET tasks_completed = tasks_completed + 1 WHERE id=?",
                (agent_id,),
            )
        await self._db.commit()

    async def list_agents(self) -> list[dict]:
        cur = await self._db.execute("SELECT * FROM agents ORDER BY name")
        return [dict(r) for r in await cur.fetchall()]

    # ── Events ─────────────────────────────────────────────

    async def _log(self, event_type: str, entity_id: str, detail: str):
        await self._db.execute(
            "INSERT INTO events (timestamp, event_type, entity_id, detail) VALUES (?,?,?,?)",
            (time.time(), event_type, entity_id, detail),
        )

    async def recent_events(self, limit: int = 50) -> list[dict]:
        cur = await self._db.execute(
            "SELECT * FROM events ORDER BY id DESC LIMIT ?", (limit,)
        )
        return [dict(r) for r in await cur.fetchall()]
