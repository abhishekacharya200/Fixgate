"""Carry out an approved action: open the draft PR, or apply triage labels and a comment.

Publishing uses only the approved payload. The patch is applied to a fresh clone at the recorded base
commit, pushed to a branch (on your fork unless you configured pushing to the repository itself), and
opened as a draft PR. Running it twice does not open a second PR.
"""

from __future__ import annotations

import json
import subprocess
import tempfile
from pathlib import Path
from typing import Optional

from .approval import Approval, ApprovalError, Approvals
from .config import Config
from .github import GitHub, GitHubError, resolve_token
from .live import run_git
from .store import Store, now_iso
from .taxonomy import BLAME, Category


class PublishError(Exception):
    pass


def _commit_identity(cfg: Config) -> list:
    name = cfg.github.commit_name
    email = cfg.github.commit_email
    args = []
    if name:
        args += ["-c", f"user.name={name}"]
    if email:
        args += ["-c", f"user.email={email}"]
    return args


def publish(cfg: Config, store: Store, approval_id: str, *, gh: Optional[GitHub] = None,
            token: Optional[str] = None, remote_base: str = "https://github.com") -> str:
    approvals = Approvals.from_config(store, cfg)
    approval = approvals.get(approval_id)
    existing = store.query("SELECT url FROM publications WHERE approval_id = ?", (approval.id,))
    if existing:
        return existing[0]["url"]
    approvals.check_executable(approval)
    if gh is None:
        token = token or resolve_token(cfg.github)
        if not token:
            raise PublishError("no GitHub token: set GITHUB_TOKEN or log in with `gh auth login`")
        gh = GitHub(token, cfg.github.api_url)
    try:
        if approval.action == "open_pr":
            url = _open_pr(cfg, gh, approval, token, remote_base)
        elif approval.action == "apply_triage":
            url = _apply_triage(gh, approval)
        else:
            raise PublishError(f"unknown action {approval.action}")
    except (GitHubError, RuntimeError, subprocess.SubprocessError) as err:
        store.audit("fixgate", "publish.failed", approval.id, error=str(err)[:500])
        store.set_task_outcome(approval.task_id, Category.GITHUB_ERROR.value, type(err).__name__,
                               BLAME[Category.GITHUB_ERROR].value, "publish")
        raise PublishError(str(err)) from err
    store.execute("INSERT INTO publications (approval_id, url, branch, created_at, details_json) VALUES (?, ?, ?, ?, ?)",
                  (approval.id, url, approval.payload.get("branch"), now_iso(),
                   json.dumps({"by": approval.decided_by})))
    approvals.mark_executed(approval, url)
    if approval.action == "open_pr":
        store.set_task_outcome(approval.task_id, Category.PR_OPENED.value, "draft", BLAME[Category.PR_OPENED].value,
                               "publish")
    return url


def _open_pr(cfg: Config, gh: GitHub, approval: Approval, token: Optional[str], remote_base: str) -> str:
    p = approval.payload
    owner, name = p["repo"].split("/", 1)
    me = gh.user()
    if cfg.github.push_to == "origin":
        push_owner, push_name = owner, name
    elif me.lower() == owner.lower():
        push_owner, push_name = owner, name
    else:
        fork = gh.create_fork(owner, name)
        push_owner, push_name = fork["owner"]["login"], fork["name"]
    head = f"{push_owner}:{p['branch']}"
    found = gh.find_pull(owner, name, head)
    if found:
        return found["html_url"]
    with tempfile.TemporaryDirectory(prefix="fixgate-publish-") as tmp:
        repo = Path(tmp) / "repo"
        run_git(["clone", "--quiet", "--depth", "50", f"{remote_base}/{p['repo']}.git", str(repo)], token=token)
        have = subprocess.run(["git", "cat-file", "-e", f"{p['base_commit']}^{{commit}}"], cwd=repo,
                              capture_output=True).returncode == 0
        if not have:
            run_git(["fetch", "--quiet", "--depth", "50", "origin", p["base_commit"]], cwd=repo, token=token)
        run_git(["checkout", "--quiet", "-b", p["branch"], p["base_commit"]], cwd=repo)
        patch_file = Path(tmp) / "change.patch"
        patch_file.write_text(p["patch"], encoding="utf-8")
        run_git(["apply", "--index", "--whitespace=nowarn", str(patch_file)], cwd=repo)
        run_git([*_commit_identity(cfg), "commit", "--quiet", "-m", p["commit_message"]], cwd=repo)
        run_git(["push", "--quiet", f"{remote_base}/{push_owner}/{push_name}.git",
                 f"HEAD:refs/heads/{p['branch']}"], cwd=repo, token=token)
    body = p["body"] + f"\n\nApproved for publishing by {approval.decided_by}."
    pull = gh.create_pull(owner, name, title=p["title"], body=body, head=head, base=p["base_branch"],
                          draft=cfg.github.draft)
    return pull["html_url"]


def _apply_triage(gh: GitHub, approval: Approval) -> str:
    p = approval.payload
    owner, name = p["repo"].split("/", 1)
    if p.get("labels"):
        gh.add_labels(owner, name, p["issue_number"], p["labels"])
    if p.get("comment"):
        comment = gh.comment(owner, name, p["issue_number"], p["comment"])
        return comment.get("html_url", f"{p['repo']}#{p['issue_number']}")
    return f"https://github.com/{p['repo']}/issues/{p['issue_number']}"


def ensure_approved(approvals: Approvals, approval_id: str) -> Approval:
    approval = approvals.get(approval_id)
    try:
        approvals.check_executable(approval)
    except ApprovalError as err:
        raise PublishError(str(err)) from err
    return approval
