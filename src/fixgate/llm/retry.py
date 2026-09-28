"""Retries for LLM calls: exponential backoff with full jitter for transient errors, and a long, bounded
wait for rate limits (a subscription window can take hours to reset).

Non-retryable errors (bad request, auth, context overflow, refusal) are raised at once. The SDKs' own
retries are switched off, so every attempt is visible in the trace.
"""

from __future__ import annotations

import random
import re
import time
from typing import Callable, Optional

from ..config import RetryConfig
from .base import LLMError, LLMResponse

_RESET_AT = re.compile(r"resets at (\d{4}-\d{2}-\d{2} \d{2}:\d{2})", re.I)
_RESET_IN = re.compile(r"resets in ((?:\d+\s*[dhms]\s*)+)", re.I)
_UNIT = {"d": 86400, "h": 3600, "m": 60, "s": 1}
# Codex: "try again at Oct 26th, 2026 11:22 PM."
_TRY_AGAIN_AT = re.compile(r"try again at ([A-Z][a-z]{2})[a-z]* (\d{1,2})(?:st|nd|rd|th)?,? (\d{4}) (\d{1,2}):(\d{2})\s*([AP]M)",
                           re.I)
_MONTHS = {m: i for i, m in enumerate(["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov",
                                       "dec"], start=1)}


def seconds_until_reset(message: str, now: Optional[float] = None) -> Optional[float]:
    """Subscription limits say when they reset: 'It resets at 2026-09-27 14:00 +0545.' (Claude Code
    through the gateway, local time), 'Resets in 6h25m27s.' (Antigravity) or 'try again at Oct 26th, 2026
    11:22 PM.' (Codex). A reset further away than rate_limit_max_wait_s fails the call at once."""
    absolute = _TRY_AGAIN_AT.search(message or "")
    if absolute:
        month, day, year, hour, minute, half = absolute.groups()
        hour = int(hour) % 12 + (12 if half.upper() == "PM" else 0)
        stamp = time.mktime((int(year), _MONTHS[month.lower()], int(day), hour, int(minute), 0, 0, 0, -1))
        return max(0.0, stamp - (now if now is not None else time.time()))
    relative = _RESET_IN.search(message or "")
    if relative:
        return float(sum(int(n) * _UNIT[u.lower()] for n, u in re.findall(r"(\d+)\s*([dhms])", relative.group(1),
                                                                         re.I)))
    match = _RESET_AT.search(message or "")
    if not match:
        return None
    try:
        reset = time.mktime(time.strptime(match.group(1), "%Y-%m-%d %H:%M"))
    except ValueError:
        return None
    return max(0.0, reset - (now if now is not None else time.time()))


def wall_sleep(seconds: float, step: float = 30.0) -> None:
    """Sleep until `seconds` of wall-clock time have passed.

    time.sleep alone stops counting while the machine is suspended, so a wait for a quota reset started
    before a laptop went to sleep would run hours late after it wakes. Checking the wall clock every `step`
    seconds bounds that delay."""
    end = time.time() + seconds
    while True:
        remaining = end - time.time()
        if remaining <= 0:
            return
        time.sleep(min(step, remaining))


def backoff(attempt: int, base: float, cap: float, rng: Callable[[float, float], float] = random.uniform) -> float:
    """Full jitter: a random wait between 0 and min(cap, base * 2^(attempt-1))."""
    return rng(0, min(cap, base * 2 ** (attempt - 1)))


def call_with_retry(fn: Callable[[], LLMResponse], policy: RetryConfig, *,
                    on_retry: Optional[Callable[[LLMError, int, float], None]] = None,
                    sleep: Callable[[float], None] = wall_sleep,
                    rng: Callable[[float, float], float] = random.uniform) -> LLMResponse:
    """Call fn until it succeeds, a non-retryable error occurs, or the retry budget is spent.

    on_retry(error, attempt, wait_seconds) is called before each wait (for traces and for pausing the
    task's wall-clock budget during rate-limit waits).
    """
    attempt = 0            # transient failures so far
    limited = 0            # rate-limit waits so far
    waited_for_limits = 0.0
    while True:
        try:
            return fn()
        except LLMError as err:
            if not err.retryable:
                raise
            if err.kind == "rate_limit":
                limited += 1
                wait = err.retry_after_s if err.retry_after_s is not None else min(900.0, 30.0 * 2 ** (limited - 1))
                wait = max(wait, 1.0) + rng(0, min(10.0, wait * 0.1))
                if waited_for_limits + wait > policy.rate_limit_max_wait_s:
                    raise
                waited_for_limits += wait
                number = limited
            else:
                attempt += 1
                if attempt >= policy.max_attempts:
                    raise
                # A refused connection usually means the endpoint is restarting: wait longer between tries.
                base = max(policy.base_delay_s, 5.0) if err.kind == "connection" else policy.base_delay_s
                wait = backoff(attempt, base, policy.max_delay_s, rng)
                if err.kind == "connection":
                    wait = max(wait, min(policy.max_delay_s, base * attempt))
                if err.retry_after_s:
                    wait = max(wait, min(err.retry_after_s, policy.max_delay_s * 5))
                number = attempt
            if on_retry is not None:
                on_retry(err, number, wait)
            sleep(wait)
