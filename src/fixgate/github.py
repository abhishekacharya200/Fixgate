"""A small GitHub REST client with retries and rate-limit handling. The token stays on the host."""

from __future__ import annotations

import random
import re
import subprocess
import time
from typing import Callable, List, Optional, Tuple

import httpx

from .config import GitHubConfig
from .models import Issue
from .redact import register_secret

ISSUE_REF = re.compile(r"^(?:https://github\.com/)?([\w.-]+)/([\w.-]+)(?:#|/issues/)(\d+)/?$")


class GitHubError(Exception):
    def __init__(self, status: Optional[int], message: str):
        super().__init__(f"GitHub {status}: {message}" if status else f"GitHub: {message}")
        self.status = status
        self.message = message


def parse_issue_ref(ref: str) -> Tuple[str, str, int]:
    match = ISSUE_REF.match(ref.strip())
    if not match:
        raise ValueError(f"'{ref}' is not an issue reference like owner/repo#123 or an issue URL")
    return match.group(1), match.group(2), int(match.group(3))


def resolve_token(cfg: GitHubConfig) -> Optional[str]:
    import os

    token = os.environ.get(cfg.token_env)
    if not token and cfg.use_gh_cli:
        try:
            done = subprocess.run(["gh", "auth", "token"], capture_output=True, text=True, timeout=20)
            token = done.stdout.strip() if done.returncode == 0 else None
        except (OSError, subprocess.TimeoutExpired):
            token = None
    if token:
        register_secret(token)
    return token


class GitHub:
    def __init__(self, token: Optional[str], api_url: str = "https://api.github.com",
                 transport: Optional[httpx.BaseTransport] = None, sleep: Callable[[float], None] = time.sleep,
                 max_attempts: int = 5):
        headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28",
                   "User-Agent": "fixgate"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        self.http = httpx.Client(base_url=api_url, headers=headers, timeout=30, transport=transport)
        self.sleep = sleep
        self.max_attempts = max_attempts

    def request(self, method: str, path: str, **kwargs) -> httpx.Response:
        for attempt in range(1, self.max_attempts + 1):
            try:
                response = self.http.request(method, path, **kwargs)
            except httpx.TransportError as exc:
                if attempt == self.max_attempts:
                    raise GitHubError(None, f"network error: {exc}") from exc
                self.sleep(min(30, 2 ** attempt) + random.uniform(0, 1))
                continue
            wait = self._rate_limit_wait(response)
            if wait is not None and attempt < self.max_attempts:
                self.sleep(wait)
                continue
            if response.status_code >= 500 and attempt < self.max_attempts:
                self.sleep(min(30, 2 ** attempt) + random.uniform(0, 1))
                continue
            if response.status_code >= 400:
                try:
                    message = response.json().get("message", response.text)
                except ValueError:
                    message = response.text
                raise GitHubError(response.status_code, str(message)[:500])
            return response
        raise GitHubError(None, "retries exhausted")

    @staticmethod
    def _rate_limit_wait(response: httpx.Response) -> Optional[float]:
        if response.status_code not in (403, 429):
            return None
        if response.headers.get("retry-after"):
            return min(900.0, float(response.headers["retry-after"]))
        if response.headers.get("x-ratelimit-remaining") == "0" and response.headers.get("x-ratelimit-reset"):
            return min(900.0, max(1.0, float(response.headers["x-ratelimit-reset"]) - time.time() + 1))
        return None

    # ---------- reading ----------

    def user(self) -> str:
        return self.request("GET", "/user").json()["login"]

    def repo(self, owner: str, name: str) -> dict:
        return self.request("GET", f"/repos/{owner}/{name}").json()

    def issue(self, owner: str, name: str, number: int, max_comments: int = 20) -> Issue:
        data = self.request("GET", f"/repos/{owner}/{name}/issues/{number}").json()
        if data.get("pull_request"):
            raise GitHubError(None, f"{owner}/{name}#{number} is a pull request, not an issue")
        comments: List[str] = []
        if data.get("comments"):
            rows = self.request("GET", f"/repos/{owner}/{name}/issues/{number}/comments",
                                params={"per_page": max_comments}).json()
            comments = [f"{c['user']['login']}: {c.get('body') or ''}" for c in rows]
        return Issue(repo=f"{owner}/{name}", number=number, title=data.get("title") or "",
                      body=data.get("body") or "", url=data.get("html_url"),
                      labels=[label["name"] for label in data.get("labels") or []], comments=comments,
                      author=(data.get("user") or {}).get("login"))

    def issues_with_label(self, owner: str, name: str, label: str, limit: int = 20) -> List[int]:
        rows = self.request("GET", f"/repos/{owner}/{name}/issues",
                            params={"labels": label, "state": "open", "per_page": min(100, limit)}).json()
        return [row["number"] for row in rows if not row.get("pull_request")][:limit]

    def find_pull(self, owner: str, name: str, head: str) -> Optional[dict]:
        rows = self.request("GET", f"/repos/{owner}/{name}/pulls", params={"head": head, "state": "open"}).json()
        return rows[0] if rows else None

    # ---------- writing (only after an approval) ----------

    def create_fork(self, owner: str, name: str, wait_s: float = 60) -> dict:
        fork = self.request("POST", f"/repos/{owner}/{name}/forks").json()
        deadline = time.monotonic() + wait_s
        while time.monotonic() < deadline:
            try:
                return self.repo(fork["owner"]["login"], fork["name"])
            except GitHubError as err:
                if err.status != 404:
                    raise
                self.sleep(3)
        raise GitHubError(None, "the fork was not ready in time")

    def create_pull(self, owner: str, name: str, *, title: str, body: str, head: str, base: str,
                    draft: bool = True) -> dict:
        return self.request("POST", f"/repos/{owner}/{name}/pulls",
                            json={"title": title, "body": body, "head": head, "base": base, "draft": draft,
                                  "maintainer_can_modify": True}).json()

    def add_labels(self, owner: str, name: str, number: int, labels: List[str]) -> None:
        self.request("POST", f"/repos/{owner}/{name}/issues/{number}/labels", json={"labels": labels})

    def comment(self, owner: str, name: str, number: int, body: str) -> dict:
        return self.request("POST", f"/repos/{owner}/{name}/issues/{number}/comments", json={"body": body}).json()
