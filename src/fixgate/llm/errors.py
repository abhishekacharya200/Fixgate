"""Map HTTP errors from any provider to LLMError kinds."""

from __future__ import annotations

from typing import Optional

from .base import LLMError

_CONTEXT_WORDS = ("prompt is too long", "context length", "context_length", "maximum context", "too many tokens",
                  "input is too long", "exceeds the context window")


def error_from_status(status: Optional[int], message: str, retry_after: Optional[float] = None,
                      request_id: Optional[str] = None) -> LLMError:
    lowered = (message or "").lower()
    if status in (400, 413) and any(word in lowered for word in _CONTEXT_WORDS):
        kind = "context_overflow"
    elif status == 429:
        kind = "rate_limit"
    elif status == 529 or "overloaded" in lowered:
        kind = "overloaded"
    elif status in (401, 403):
        kind = "auth"
    elif status == 404:
        kind = "not_found"
    elif status == 408 or status == 504:
        kind = "timeout"
    elif status is not None and status >= 500:
        kind = "server"
    elif status in (400, 409, 413, 422):
        kind = "bad_request"
    else:
        kind = "unknown"
    return LLMError(kind, message or f"HTTP {status}", status=status, retry_after_s=retry_after,
                    request_id=request_id)
