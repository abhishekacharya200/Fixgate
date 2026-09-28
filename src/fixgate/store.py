"""SQLite storage: tasks, approval requests, publications and an append-only audit log."""

from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional

from .models import TaskResult
from .redact import redact

SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (
    id TEXT PRIMARY KEY,
    run_id TEXT,
    issue_ref TEXT NOT NULL,
    mode TEXT NOT NULL,
    category TEXT,
    subcategory TEXT,
    blame TEXT,
    phase TEXT,
    model TEXT,
    provider TEXT,
    cost_usd REAL,
    prompt_tokens INTEGER,
    output_tokens INTEGER,
    steps INTEGER,
    wall_s REAL,
    repro_status TEXT,
    gate_passed INTEGER,
    patch_sha256 TEXT,
    trace_dir TEXT,
    approval_id TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    result_json TEXT
);
CREATE INDEX IF NOT EXISTS tasks_run ON tasks (run_id);
CREATE TABLE IF NOT EXISTS approvals (
    id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL,
    action TEXT NOT NULL,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    decided_at TEXT,
    decided_by TEXT,
    note TEXT,
    payload_json TEXT NOT NULL,
    payload_sha256 TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS approvals_status ON approvals (status);
CREATE TABLE IF NOT EXISTS publications (
    approval_id TEXT PRIMARY KEY,
    url TEXT,
    branch TEXT,
    created_at TEXT NOT NULL,
    details_json TEXT
);
CREATE TABLE IF NOT EXISTS audit (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    at TEXT NOT NULL,
    actor TEXT NOT NULL,
    action TEXT NOT NULL,
    target TEXT,
    details_json TEXT
);
"""


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Store:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._db = sqlite3.connect(str(self.path), check_same_thread=False, timeout=30)
        self._db.row_factory = sqlite3.Row
        with self._lock:
            self._db.execute("PRAGMA journal_mode=WAL")
            self._db.executescript(SCHEMA)
            self._db.commit()

    def execute(self, sql: str, args: tuple = ()) -> sqlite3.Cursor:
        with self._lock:
            cursor = self._db.execute(sql, args)
            self._db.commit()
            return cursor

    def query(self, sql: str, args: tuple = ()) -> List[dict]:
        with self._lock:
            return [dict(row) for row in self._db.execute(sql, args).fetchall()]

    def close(self) -> None:
        with self._lock:
            self._db.close()

    # ---------- tasks ----------

    def save_task(self, result: TaskResult, mode: str, run_id: Optional[str] = None) -> None:
        o, u = result.outcome, result.usage
        data = redact(result.to_json())
        stamp = now_iso()
        self.execute(
            """INSERT INTO tasks (id, run_id, issue_ref, mode, category, subcategory, blame, phase, model, provider,
                   cost_usd, prompt_tokens, output_tokens, steps, wall_s, repro_status, gate_passed, patch_sha256,
                   trace_dir, approval_id, created_at, updated_at, result_json)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(id) DO UPDATE SET category=excluded.category, subcategory=excluded.subcategory,
                   blame=excluded.blame, phase=excluded.phase, cost_usd=excluded.cost_usd,
                   prompt_tokens=excluded.prompt_tokens, output_tokens=excluded.output_tokens, steps=excluded.steps,
                   wall_s=excluded.wall_s, repro_status=excluded.repro_status, gate_passed=excluded.gate_passed,
                   patch_sha256=excluded.patch_sha256, approval_id=excluded.approval_id,
                   trace_dir=excluded.trace_dir, run_id=excluded.run_id, model=excluded.model,
                   updated_at=excluded.updated_at, result_json=excluded.result_json""",
            (result.task_id, run_id, result.issue.ref, mode, o.category.value, o.subcategory, o.blame.value, o.phase,
             result.model, result.provider, u.get("cost_usd"), u.get("prompt_tokens"), u.get("output_tokens"),
             u.get("steps"), u.get("wall_s"), result.reproduction.status if result.reproduction else None,
             int(result.gate_passed), result.patch_sha256, result.trace_dir, result.approval_id, stamp, stamp,
             json.dumps(data)))

    def set_task_outcome(self, task_id: str, category: str, subcategory: str, blame: str, phase: str) -> None:
        row = self.task(task_id)
        if row is None:
            return
        data = json.loads(row["result_json"])
        data["outcome"].update({"category": category, "subcategory": subcategory, "blame": blame, "phase": phase})
        self.execute("UPDATE tasks SET category=?, subcategory=?, blame=?, phase=?, updated_at=?, result_json=? "
                     "WHERE id=?", (category, subcategory, blame, phase, now_iso(), json.dumps(data), task_id))

    def task(self, task_id: str) -> Optional[dict]:
        rows = self.query("SELECT * FROM tasks WHERE id = ?", (task_id,))
        if not rows:
            rows = self.query("SELECT * FROM tasks WHERE id LIKE ? ORDER BY created_at DESC LIMIT 2", (task_id + "%",))
            if len(rows) != 1:
                return None
        return rows[0]

    def tasks(self, limit: int = 50, run_id: Optional[str] = None) -> List[dict]:
        if run_id:
            return self.query("SELECT * FROM tasks WHERE run_id = ? ORDER BY created_at DESC LIMIT ?", (run_id, limit))
        return self.query("SELECT * FROM tasks ORDER BY created_at DESC LIMIT ?", (limit,))

    # ---------- audit ----------

    def audit(self, actor: str, action: str, target: str, **details) -> None:
        self.execute("INSERT INTO audit (at, actor, action, target, details_json) VALUES (?, ?, ?, ?, ?)",
                     (now_iso(), actor, action, target, json.dumps(redact(details))))

    def audit_log(self, target: Optional[str] = None, limit: int = 100) -> List[dict]:
        if target:
            return self.query("SELECT * FROM audit WHERE target = ? ORDER BY id DESC LIMIT ?", (target, limit))
        return self.query("SELECT * FROM audit ORDER BY id DESC LIMIT ?", (limit,))
