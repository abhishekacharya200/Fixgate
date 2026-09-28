"""Remove secrets from everything fixgate writes: traces, logs, the database and PR bodies."""

from __future__ import annotations

import re
import threading
from typing import Any, Iterable

MASK = "[REDACTED]"

# (name, pattern, replacement)
PATTERNS = [
    ("github_token", re.compile(r"gh[pousr]_[A-Za-z0-9]{30,}"), MASK),
    ("github_pat", re.compile(r"github_pat_[A-Za-z0-9_]{40,}"), MASK),
    ("anthropic_key", re.compile(r"sk-ant-[A-Za-z0-9_\-]{20,}"), MASK),
    ("openai_key", re.compile(r"sk-(?:proj-)?[A-Za-z0-9_\-]{32,}"), MASK),
    ("google_api_key", re.compile(r"AIza[0-9A-Za-z_\-]{35}"), MASK),
    ("aws_access_key", re.compile(r"(?:AKIA|ASIA)[0-9A-Z]{16}"), MASK),
    ("slack_token", re.compile(r"xox[baprs]-[A-Za-z0-9\-]{10,}"), MASK),
    ("private_key", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z ]*PRIVATE KEY-----"), MASK),
    ("auth_header", re.compile(r"(?i)(authorization:\s*(?:bearer|token)\s+)[^\s'\"]+"), r"\1" + MASK),
    ("url_credentials", re.compile(r"(?i)(https?://[^/\s:@]+:)[^@\s/]+(@)"), r"\1" + MASK + r"\2"),
]

_known: set = set()
_lock = threading.Lock()


def register_secret(value: str) -> None:
    """Mask this exact value everywhere from now on (for tokens read at startup)."""
    if value and len(value) >= 8:
        with _lock:
            _known.add(value)


def register_secrets(values: Iterable[str]) -> None:
    for value in values:
        register_secret(value)


def redact_text(text: str) -> str:
    if not text:
        return text
    for value in list(_known):
        if value in text:
            text = text.replace(value, MASK)
    for _, pattern, replacement in PATTERNS:
        text = pattern.sub(replacement, text)
    return text


def redact(value: Any) -> Any:
    """Redact strings inside any JSON-like value."""
    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, dict):
        return {k: redact(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [redact(v) for v in value]
    return value


def find_secrets(text: str) -> list:
    """Names of the kinds of secret found in text (used to block a patch that contains one)."""
    found = []
    if any(value in text for value in list(_known)):
        found.append("known_secret")
    for name, pattern, _ in PATTERNS:
        if pattern.search(text):
            found.append(name)
    return found
