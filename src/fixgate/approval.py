"""The human approval step.

Nothing leaves the sandbox without a person's decision. An approval request stores the exact payload
(for a PR: title, body and patch) with its SHA-256. Approving it approves that payload only: publishing
recomputes the hash and refuses if anything changed. The model has no tool that can approve, and the
GitHub token never enters the sandbox. Every decision goes to the audit log.
"""

from __future__ import annotations

import hashlib
import json
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import List, Optional

from .store import Store, now_iso
from .taxonomy import BLAME, Category

ACTIONS = ("open_pr", "apply_triage")


class ApprovalError(Exception):
    pass


def payload_digest(payload: dict) -> str:
    return hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()


@dataclass
class Approval:
    id: str
    task_id: str
    action: str
    status: str               # pending | approved | rejected | expired | executed
    created_at: str
    expires_at: str
    payload: dict
    payload_sha256: str
    decided_at: Optional[str] = None
    decided_by: Optional[str] = None
    note: Optional[str] = None

    @classmethod
    def from_row(cls, row: dict) -> "Approval":
        return cls(id=row["id"], task_id=row["task_id"], action=row["action"], status=row["status"],
                   created_at=row["created_at"], expires_at=row["expires_at"], payload=json.loads(row["payload_json"]),
                   payload_sha256=row["payload_sha256"], decided_at=row.get("decided_at"),
                   decided_by=row.get("decided_by"), note=row.get("note"))

    @property
    def expired(self) -> bool:
        return datetime.fromisoformat(self.expires_at) < datetime.now(timezone.utc)


class Approvals:
    def __init__(self, store: Store, expires_hours: float = 72.0, notify_webhook: str = "",
                 page_url: str = "http://127.0.0.1:8790"):
        self.store = store
        self.expires_hours = expires_hours
        self.notify_webhook = notify_webhook
        self.page_url = page_url.rstrip("/")

    @classmethod
    def from_config(cls, store: Store, cfg) -> "Approvals":
        return cls(store, cfg.approval.expires_hours, cfg.approval.notify_webhook, cfg.approval.page_url)

    def _notify(self, approval: "Approval") -> None:
        """Best effort: a failed notification is recorded in the audit log and never blocks the request."""
        if not self.notify_webhook:
            return
        import httpx

        p = approval.payload
        text = (f"fixgate needs a decision: {approval.action.replace('_', ' ')} for {p.get('repo')}#"
                f"{p.get('issue_number')} ({p.get('title', '')}). Review: {self.page_url}/approvals/{approval.id} "
                f"or `fixgate show {approval.id}`. Expires {approval.expires_at}.")
        try:
            response = httpx.post(self.notify_webhook, json={"text": text}, timeout=10)
            response.raise_for_status()
            self.store.audit("fixgate", "approval.notified", approval.id)
        except httpx.HTTPError as err:
            self.store.audit("fixgate", "approval.notify_failed", approval.id, error=str(err)[:300])

    def request(self, task_id: str, action: str, payload: dict) -> Approval:
        if action not in ACTIONS:
            raise ApprovalError(f"unknown action {action}")
        approval_id = "apr_" + secrets.token_hex(6)
        created = datetime.now(timezone.utc)
        expires = created + timedelta(hours=self.expires_hours)
        digest = payload_digest(payload)
        self.store.execute(
            "INSERT INTO approvals (id, task_id, action, status, created_at, expires_at, payload_json, payload_sha256) "
            "VALUES (?, ?, ?, 'pending', ?, ?, ?, ?)",
            (approval_id, task_id, action, created.isoformat(timespec="seconds"),
             expires.isoformat(timespec="seconds"), json.dumps(payload), digest))
        self.store.audit("fixgate", "approval.requested", approval_id, task_id=task_id, requested=action,
                         payload_sha256=digest)
        approval = self.get(approval_id)
        self._notify(approval)
        return approval

    def get(self, approval_id: str) -> Approval:
        rows = self.store.query("SELECT * FROM approvals WHERE id = ?", (approval_id,))
        if not rows:
            rows = self.store.query("SELECT * FROM approvals WHERE id LIKE ?", (approval_id + "%",))
        if len(rows) != 1:
            raise ApprovalError(f"no approval request {approval_id}" if not rows else f"{approval_id} is ambiguous")
        return Approval.from_row(rows[0])

    def list(self, status: Optional[str] = None, limit: int = 50) -> List[Approval]:
        self.expire_stale()
        if status:
            rows = self.store.query("SELECT * FROM approvals WHERE status = ? ORDER BY created_at DESC LIMIT ?",
                                    (status, limit))
        else:
            rows = self.store.query("SELECT * FROM approvals ORDER BY created_at DESC LIMIT ?", (limit,))
        return [Approval.from_row(r) for r in rows]

    def expire_stale(self) -> int:
        stale = self.store.query("SELECT id, task_id FROM approvals WHERE status = 'pending' AND expires_at < ?",
                                 (now_iso(),))
        for row in stale:
            self.store.execute("UPDATE approvals SET status = 'expired' WHERE id = ? AND status = 'pending'",
                               (row["id"],))
            self.store.audit("fixgate", "approval.expired", row["id"])
            self._task_outcome(row["task_id"], Category.APPROVAL_EXPIRED, "timeout")
        return len(stale)

    def _decide(self, approval_id: str, status: str, by: str, note: str) -> Approval:
        approval = self.get(approval_id)
        if approval.status == "pending" and approval.expired:
            self.expire_stale()
            raise ApprovalError(f"{approval.id} expired at {approval.expires_at}")
        if approval.status != "pending":
            raise ApprovalError(f"{approval.id} is already {approval.status}")
        if not by.strip():
            raise ApprovalError("a decision needs the reviewer's name")
        cursor = self.store.execute(
            "UPDATE approvals SET status = ?, decided_at = ?, decided_by = ?, note = ? WHERE id = ? AND status = "
            "'pending'", (status, now_iso(), by, note, approval.id))
        if cursor.rowcount != 1:
            raise ApprovalError(f"{approval.id} was decided by someone else in the meantime")
        self.store.audit(by, f"approval.{status}", approval.id, note=note, payload_sha256=approval.payload_sha256)
        return self.get(approval.id)

    def approve(self, approval_id: str, by: str, note: str = "") -> Approval:
        return self._decide(approval_id, "approved", by, note)

    def reject(self, approval_id: str, by: str, reason: str) -> Approval:
        approval = self._decide(approval_id, "rejected", by, reason)
        self._task_outcome(approval.task_id, Category.APPROVAL_REJECTED, "reviewer")
        return approval

    def check_executable(self, approval: Approval) -> None:
        """Refuse to act unless the request is approved, unexpired and its payload is unchanged."""
        if approval.status != "approved":
            raise ApprovalError(f"{approval.id} is {approval.status}, not approved")
        if payload_digest(approval.payload) != approval.payload_sha256:
            raise ApprovalError(f"{approval.id}: the payload changed after it was approved")

    def mark_executed(self, approval: Approval, result: str) -> None:
        self.store.execute("UPDATE approvals SET status = 'executed' WHERE id = ?", (approval.id,))
        self.store.audit("fixgate", "approval.executed", approval.id, result=result)

    def _task_outcome(self, task_id: str, category: Category, subcategory: str) -> None:
        self.store.set_task_outcome(task_id, category.value, subcategory, BLAME[category].value, "approval")
