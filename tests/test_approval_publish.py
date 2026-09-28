"""The human approval step and publishing, with a fake GitHub API and a local git remote."""

import json
import subprocess
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from fixgate.approval import ApprovalError, Approvals
from fixgate.github import GitHub, parse_issue_ref
from fixgate.publish import publish
from fixgate.store import Store

from .conftest import git


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "db.sqlite")
    yield s
    s.close()


def pr_payload(**extra):
    payload = {"repo": "acme/calc", "issue_number": 7, "issue_url": "https://github.com/acme/calc/issues/7",
               "base_branch": "main", "base_commit": "0" * 40, "branch": "fixgate/issue-7-add",
               "title": "Fix #7: add", "body": "Fixes #7", "patch": "", "patch_sha256": "",
               "commit_message": "Fix #7: add\n\nFixes #7"}
    payload.update(extra)
    return payload


def test_approve_then_execute_checks(store):
    approvals = Approvals(store)
    a = approvals.request("t1", "open_pr", pr_payload())
    assert a.status == "pending"
    with pytest.raises(ApprovalError):
        approvals.check_executable(a)            # not approved yet
    with pytest.raises(ApprovalError):
        approvals.approve(a.id, " ")             # a decision needs a name
    a = approvals.approve(a.id, "reviewer", "looks right")
    approvals.check_executable(a)
    with pytest.raises(ApprovalError, match="already approved"):
        approvals.approve(a.id, "someone else")
    a.payload["patch"] = "tampered"
    with pytest.raises(ApprovalError, match="payload changed"):
        approvals.check_executable(a)
    actions = [row["action"] for row in store.audit_log(a.id)]
    assert actions == ["approval.approved", "approval.requested"]


def test_reject_marks_the_task(store, tmp_path):
    from fixgate.models import Issue, TaskResult
    from fixgate.taxonomy import Category, Outcome

    result = TaskResult(task_id="t2", issue=Issue(repo="acme/calc", title="x", body="y", number=7),
                        outcome=Outcome(Category.AWAITING_APPROVAL))
    store.save_task(result, mode="live")
    approvals = Approvals(store)
    a = approvals.request("t2", "open_pr", pr_payload())
    approvals.reject(a.id, "reviewer", "wrong approach")
    assert store.task("t2")["category"] == "approval_rejected"
    assert store.task("t2")["blame"] == "human"


def test_requests_expire(store):
    approvals = Approvals(store, expires_hours=1)
    a = approvals.request("t3", "open_pr", pr_payload())
    past = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat(timespec="seconds")
    store.execute("UPDATE approvals SET expires_at = ? WHERE id = ?", (past, a.id))
    with pytest.raises(ApprovalError, match="expired"):
        approvals.approve(a.id, "reviewer")
    assert approvals.get(a.id).status == "expired"


def test_issue_refs():
    assert parse_issue_ref("acme/calc#7") == ("acme", "calc", 7)
    assert parse_issue_ref("https://github.com/acme/calc/issues/12") == ("acme", "calc", 12)
    with pytest.raises(ValueError):
        parse_issue_ref("acme/calc")


class FakeGitHub:
    """Enough of the REST API for publishing: /user, forks, pulls."""

    def __init__(self, login="me"):
        self.login = login
        self.pulls = []
        self.forks = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        path, method = request.url.path, request.method
        if path == "/user":
            return httpx.Response(200, json={"login": self.login})
        if path == "/repos/acme/calc/forks" and method == "POST":
            self.forks.append(1)
            return httpx.Response(202, json={"owner": {"login": self.login}, "name": "calc"})
        if path == f"/repos/{self.login}/calc":
            return httpx.Response(200, json={"name": "calc", "owner": {"login": self.login}})
        if path == "/repos/acme/calc/pulls" and method == "GET":
            head = request.url.params.get("head")
            return httpx.Response(200, json=[p for p in self.pulls if p["head"] == head])
        if path == "/repos/acme/calc/pulls" and method == "POST":
            body = json.loads(request.content)
            pull = {**body, "html_url": f"https://github.com/acme/calc/pull/{len(self.pulls) + 1}"}
            self.pulls.append(pull)
            return httpx.Response(201, json=pull)
        return httpx.Response(404, json={"message": f"no route {method} {path}"})


def make_remote(tmp_path):
    """A bare 'upstream' acme/calc with one commit, and an empty bare 'fork' me/calc."""
    work = tmp_path / "upstream-work"
    work.mkdir()
    (work / "calc.py").write_text("def add(a, b):\n    return a - b\n")
    git(work, "init", "-q", "-b", "main")
    git(work, "add", "-A")
    git(work, "commit", "-q", "-m", "initial")
    base = git(work, "rev-parse", "HEAD").strip()
    remotes = tmp_path / "remotes"
    (remotes / "acme").mkdir(parents=True)
    (remotes / "me").mkdir(parents=True)
    subprocess.run(["git", "clone", "-q", "--bare", str(work), str(remotes / "acme" / "calc.git")], check=True)
    subprocess.run(["git", "init", "-q", "--bare", str(remotes / "me" / "calc.git")], check=True)
    git(work, "checkout", "-q", "-b", "fix")
    (work / "calc.py").write_text("def add(a, b):\n    return a + b\n")
    patch = subprocess.run(["git", "diff", "main"], cwd=work, capture_output=True, text=True, check=True).stdout
    return remotes, base, patch


def test_publish_opens_one_draft_pr_on_a_fork(store, tmp_path, cfg):
    remotes, base, patch = make_remote(tmp_path)
    fake = FakeGitHub()
    gh = GitHub("token", "https://api.github.test", transport=httpx.MockTransport(fake.handler), sleep=lambda s: None)
    cfg.github.commit_name, cfg.github.commit_email = "Reviewer", "reviewer@example.com"
    approvals = Approvals(store)
    a = approvals.request("t4", "open_pr", pr_payload(base_commit=base, patch=patch))
    with pytest.raises(Exception, match="pending"):
        publish(cfg, store, a.id, gh=gh, remote_base=str(remotes))   # never without approval
    approvals.approve(a.id, "reviewer")
    url = publish(cfg, store, a.id, gh=gh, remote_base=str(remotes))
    assert url == "https://github.com/acme/calc/pull/1"
    assert fake.pulls[0]["draft"] is True and fake.pulls[0]["head"] == "me:fixgate/issue-7-add"
    assert "Approved for publishing by reviewer" in fake.pulls[0]["body"]
    shown = subprocess.run(["git", "--git-dir", str(remotes / "me" / "calc.git"), "show", "fixgate/issue-7-add"],
                           capture_output=True, text=True, check=True).stdout
    assert "+    return a + b" in shown and "Author: Reviewer <reviewer@example.com>" in shown
    assert approvals.get(a.id).status == "executed"
    # a second call returns the same PR instead of opening another
    assert publish(cfg, store, a.id, gh=gh, remote_base=str(remotes)) == url
    assert len(fake.pulls) == 1


def test_github_client_retries_rate_limits():
    calls = []

    def handler(request):
        calls.append(1)
        if len(calls) == 1:
            return httpx.Response(403, headers={"x-ratelimit-remaining": "0", "x-ratelimit-reset": "0"},
                                  json={"message": "rate limited"})
        return httpx.Response(200, json={"login": "me"})

    waits = []
    gh = GitHub("t", "https://api.github.test", transport=httpx.MockTransport(handler), sleep=waits.append)
    assert gh.user() == "me" and len(waits) == 1


def test_new_requests_notify_a_webhook(store, monkeypatch):
    sent = []

    def fake_post(url, json=None, timeout=None):
        sent.append((url, json))
        return httpx.Response(200, request=httpx.Request("POST", url))

    monkeypatch.setattr(httpx, "post", fake_post)
    approvals = Approvals(store, notify_webhook="https://hooks.example/abc", page_url="http://127.0.0.1:8790")
    a = approvals.request("t9", "open_pr", pr_payload())
    assert sent and sent[0][0] == "https://hooks.example/abc"
    assert f"http://127.0.0.1:8790/approvals/{a.id}" in sent[0][1]["text"]
    assert [r["action"] for r in store.audit_log(a.id)][0] == "approval.notified"


def test_failed_notification_does_not_block(store, monkeypatch):
    def broken(url, json=None, timeout=None):
        raise httpx.ConnectError("down")

    monkeypatch.setattr(httpx, "post", broken)
    a = Approvals(store, notify_webhook="https://hooks.example/abc").request("t10", "open_pr", pr_payload())
    assert a.status == "pending"
    assert [r["action"] for r in store.audit_log(a.id)][0] == "approval.notify_failed"
