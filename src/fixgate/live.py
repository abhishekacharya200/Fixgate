"""Live mode: fetch a GitHub issue, run the pipeline on a fresh clone in a sandbox, and ask a human to
approve the draft PR.

The clone happens on the host. The sandbox gets the checkout as a mount, runs the repository's setup
commands with network access, and then loses its network before the agent's first command. The GitHub
token is never written to the checkout and never enters the container.
"""

from __future__ import annotations

import base64
import os
import re
import secrets
import shutil
import subprocess
from pathlib import Path
from typing import Optional

from .approval import Approval, Approvals
from .budget import make_meter
from .config import Config, RepoProfile
from .github import GitHub, parse_issue_ref, resolve_token
from .llm import make_backend
from .llm.pricing import PriceBook
from .models import Issue, TaskResult
from .redact import redact_text
from .sandbox.base import SandboxError
from .sandbox.docker import DockerSandbox, image_present, pull_image
from .store import Store
from .taxonomy import Category, Outcome
from .tracing import Tracer

# pip only warns about an extra a project does not define, so every common name is requested at once, and
# requirement files and dependency groups are installed when they exist.
DEFAULT_SETUP = [
    "python -m venv /tmp/fixgate-venv && . /tmp/fixgate-venv/bin/activate && pip install -q --upgrade pip && "
    "(pip install -q -e '.[test,tests,testing]' || pip install -q -e .)",
    ". /tmp/fixgate-venv/bin/activate && for f in requirements-test.txt requirements_test.txt test-requirements.txt "
    "requirements-dev.txt requirements/test.txt requirements/tests.txt requirements/testing.txt tests/requirements.txt; "
    "do if [ -f \"$f\" ]; then pip install -q -r \"$f\" || exit 1; fi; done; "
    "for g in test tests testing dev; do pip install -q --group \"$g\" >/dev/null 2>&1 || true; done; "
    "pip install -q pytest",
]
DEFAULT_ENV = ". /tmp/fixgate-venv/bin/activate"


def git_auth_args(token: Optional[str]) -> list:
    """`-c http.extraHeader=...` for one git command: the token is never stored in .git/config."""
    if not token:
        return []
    basic = base64.b64encode(f"x-access-token:{token}".encode()).decode()
    return ["-c", f"http.extraHeader=Authorization: Basic {basic}"]


def run_git(args: list, cwd: Optional[Path] = None, token: Optional[str] = None, timeout: int = 600) -> str:
    done = subprocess.run(["git", *git_auth_args(token), *args], cwd=cwd, capture_output=True, text=True,
                          timeout=timeout)
    if done.returncode != 0:
        raise RuntimeError(redact_text(f"git {args[0]} failed: {(done.stderr or done.stdout).strip()[-800:]}"))
    return done.stdout


def clone(repo: str, dest: Path, token: Optional[str], depth: int = 50) -> str:
    """Clone the default branch; returns the checked-out commit."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    run_git(["clone", "--quiet", "--depth", str(depth), f"https://github.com/{repo}.git", str(dest)], token=token)
    return run_git(["rev-parse", "HEAD"], cwd=dest).strip()


def slug(text: str, limit: int = 40) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:limit].strip("-") or "fix"


def pr_title(issue: Issue, result: TaskResult) -> str:
    return f"Fix #{issue.number}: {issue.title.strip()}"[:120]


def pr_body(issue: Issue, result: TaskResult) -> str:
    fix, repro, triage = result.fix, result.reproduction, result.triage
    parts = [f"Fixes #{issue.number}", "", "## Summary", fix.summary if fix else "", "", "## Root cause",
             fix.root_cause if fix else ""]
    parts += ["", "## Reproduction"]
    if repro and repro.status == "verified":
        parts.append(f"Before the fix, `{repro.command}` exits with code {repro.exit_code}. After the fix it exits 0.")
        if repro.script:
            parts += ["", "<details><summary>Reproduction script</summary>", "", "```python",
                      repro.script.strip()[:6000], "```", "", "</details>"]
    else:
        reason = repro.explanation if repro else "not attempted"
        parts.append(f"Not reproduced: {reason}")
    tests = result.verification.tests if result.verification else None
    if tests:
        parts += ["", "## Tests", f"`{tests.command}`: {tests.verdict.replace('_', ' ')}"]
    warnings = (result.verification.warnings if result.verification else [])
    if warnings:
        parts += ["", "## Notes for the reviewer"] + [f"- {w}" for w in warnings]
    if triage:
        parts += ["", "## Triage", f"{triage.kind}, severity {triage.severity}, component `{triage.component}`"]
    usage = result.usage or {}
    key = "cost_cached_estimate_usd" if usage.get("cost_basis") == "cached_estimate" else "cost_usd"
    cost = f"${usage[key]:.2f}" if usage.get(key) is not None else "n/a"
    parts += ["", "---", f"Draft opened by fixgate (task `{result.task_id}`, {usage.get('steps', 0)} agent steps, "
              f"model cost {cost} at API prices) after a human review of this exact patch."]
    return "\n".join(parts)


def request_pr_approval(approvals: Approvals, issue: Issue, result: TaskResult, base_branch: str,
                        branch_prefix: str) -> Approval:
    payload = {
        "repo": issue.repo, "issue_number": issue.number, "issue_url": issue.url, "base_branch": base_branch,
        "base_commit": result.base_commit, "branch": f"{branch_prefix}issue-{issue.number}-{slug(issue.title)}",
        "title": pr_title(issue, result), "body": pr_body(issue, result), "patch": result.patch,
        "patch_sha256": result.patch_sha256,
        "commit_message": f"Fix #{issue.number}: {issue.title.strip()}"[:72] + "\n\n"
                          + (result.fix.summary if result.fix else "") + f"\n\nFixes #{issue.number}",
    }
    return approvals.request(result.task_id, "open_pr", payload)


def run_issue(cfg: Config, ref: str, *, store: Store, keep_workspace: bool = False,
              model: Optional[str] = None) -> TaskResult:
    if model:
        cfg.llm.model = model
    owner, name, number = parse_issue_ref(ref)
    token = resolve_token(cfg.github)
    gh = GitHub(token, cfg.github.api_url)
    issue = gh.issue(owner, name, number)
    repo_info = gh.repo(owner, name)
    task_id = "t_" + secrets.token_hex(6)
    tracer = Tracer(cfg.traces_dir / task_id, task_id=task_id, issue=issue.ref)
    prices = PriceBook(cfg.pricing)
    meter = make_meter(cfg, prices)
    profile: RepoProfile = cfg.repo_profile(issue.repo)
    workspace = cfg.workspaces_dir / task_id / "repo"
    sandbox: Optional[DockerSandbox] = None
    try:
        with tracer.span("prepare", "phase", repo=issue.repo) as span:
            clone(issue.repo, workspace, token)
            image = profile.image or cfg.sandbox.image
            if not image_present(image):
                pull_image(image)
            setup = profile.setup if profile.setup else DEFAULT_SETUP
            env_setup = profile.env_setup or (DEFAULT_ENV if not profile.setup else "")
            user = f"{os.getuid()}:{os.getgid()}" if cfg.sandbox.run_as_host_user else None
            sandbox = DockerSandbox(image, workdir=cfg.sandbox.workdir, env_setup="", memory=cfg.sandbox.memory,
                                    cpus=cfg.sandbox.cpus, pids_limit=cfg.sandbox.pids_limit, network="bridge",
                                    mounts=[(str(workspace), cfg.sandbox.workdir, "rw")], user=user,
                                    exec_timeout=cfg.sandbox.exec_timeout_s,
                                    max_exec_timeout=cfg.sandbox.max_exec_timeout_s,
                                    capture_bytes=cfg.sandbox.output_capture_bytes,
                                    labels={"fixgate.task": task_id}).start()
            for command in setup:
                done = sandbox.exec(command, timeout=cfg.sandbox.max_exec_timeout_s)
                span.event("setup", command=command, exit_code=done.exit_code)
                if not done.ok:
                    raise SandboxError("env_setup", f"`{command}` failed: {done.output[-800:]}")
            sandbox.disconnect_network()
            if sandbox.networks():
                raise SandboxError("network", "the sandbox still has a network after disconnecting")
            sandbox.env_setup = env_setup
            span.set(image=image, network="none")
        llm = make_backend(cfg.llm)
        from .agent.pipeline import Pipeline

        pipeline = Pipeline(cfg, llm=llm, sandbox=sandbox, tracer=tracer, meter=meter, test_hint=profile.test_hint,
                            environment="The project's Python environment is active in every command.")
        result = pipeline.run(issue, task_id)
    except SandboxError as err:
        result = TaskResult(task_id=task_id, issue=issue, outcome=Outcome(Category.SANDBOX_ERROR, err.kind, "setup",
                                                                          err.message[:500]),
                            model=cfg.llm.model, provider=cfg.llm.provider, trace_dir=str(tracer.dir),
                            usage=meter.snapshot())
    finally:
        if sandbox is not None:
            sandbox.close()
        if not keep_workspace:
            shutil.rmtree(workspace.parent, ignore_errors=True)
        tracer.close()
    if result.outcome.category == Category.PATCH_READY:
        approval = request_pr_approval(Approvals.from_config(store, cfg), issue, result,
                                       repo_info.get("default_branch", "main"), cfg.github.branch_prefix)
        result.approval_id = approval.id
        result.outcome = Outcome(Category.AWAITING_APPROVAL, result.outcome.subcategory, "approval",
                                 f"waiting for a decision on {approval.id}", details=result.outcome.details)
    store.save_task(result, mode="live")
    return result
