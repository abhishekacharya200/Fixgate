"""Provider-neutral messages, tool calls, usage and errors.

The agent loop speaks only these types. Each backend converts them to one API format (Anthropic
Messages, OpenAI Chat/Responses, Gemini) and back, so the same loop runs on any provider, through the
local gateway during development or against the real API in production.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional, Protocol


@dataclass
class ToolSpec:
    name: str
    description: str
    input_schema: dict


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: dict
    parse_error: Optional[str] = None  # the model sent arguments that are not a JSON object
    coerced: bool = False              # fixgate fixed an argument's type (e.g. "12" for 12)


@dataclass
class ToolResult:
    call_id: str
    name: str
    content: str
    is_error: bool = False


@dataclass
class Message:
    role: str                                   # "user" or "assistant"
    text: str = ""
    tool_calls: List[ToolCall] = field(default_factory=list)       # assistant
    tool_results: List[ToolResult] = field(default_factory=list)   # user
    # The assistant turn exactly as the API returned it (e.g. with thinking blocks), replayed verbatim by
    # the backend that produced it.
    native: Optional[list] = None
    native_backend: Optional[str] = None


@dataclass
class Usage:
    input_tokens: int = 0         # input not read from or written to a cache
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    estimated: bool = False       # the provider reported nothing and fixgate counted locally

    @property
    def prompt_tokens(self) -> int:
        return self.input_tokens + self.cache_read_tokens + self.cache_write_tokens

    def __add__(self, other: "Usage") -> "Usage":
        return Usage(self.input_tokens + other.input_tokens, self.output_tokens + other.output_tokens,
                     self.cache_read_tokens + other.cache_read_tokens,
                     self.cache_write_tokens + other.cache_write_tokens, self.estimated or other.estimated)


@dataclass
class LLMResponse:
    text: str
    tool_calls: List[ToolCall]
    stop_reason: str              # end_turn | tool_use | max_tokens | refusal | other
    usage: Usage
    model: str
    request_id: Optional[str] = None
    raw: Optional[dict] = None
    native: Optional[list] = None   # the assistant content blocks in the provider's own format


class LLMError(Exception):
    """kind: rate_limit, overloaded, server, timeout, connection, auth, bad_request, context_overflow,
    not_found, refusal, unknown."""

    RETRYABLE = {"rate_limit", "overloaded", "server", "timeout", "connection"}

    def __init__(self, kind: str, message: str, status: Optional[int] = None,
                 retry_after_s: Optional[float] = None, request_id: Optional[str] = None):
        super().__init__(f"{kind}: {message}")
        self.kind = kind
        self.message = message
        self.status = status
        self.retry_after_s = retry_after_s
        self.request_id = request_id

    @property
    def retryable(self) -> bool:
        return self.kind in self.RETRYABLE


class LLMBackend(Protocol):
    name: str

    def complete(self, *, model: str, system: str, messages: List[Message], tools: List[ToolSpec],
                 max_tokens: int) -> LLMResponse:
        ...


def estimate_tokens(text: str) -> int:
    """A fast, deliberately generous estimate (about 3.5 characters per token for code and prose)."""
    return int(len(text) / 3.5) + 1


def serialized_size(system: str, messages: List[Message], tools: List[ToolSpec]) -> int:
    """Estimated prompt tokens of a request, for budget checks before the call."""
    import json

    total = estimate_tokens(system)
    for tool in tools:
        total += estimate_tokens(tool.name + tool.description + json.dumps(tool.input_schema))
    for message in messages:
        total += estimate_tokens(message.text) + 4
        for call in message.tool_calls:
            total += estimate_tokens(json.dumps(call.arguments)) + 10
        for result in message.tool_results:
            total += estimate_tokens(result.content) + 10
    return total


def _prose_value(key: str, value) -> str:
    if isinstance(value, str) and "\n" in value:
        return f"{key}:\n{value}"
    return f"{key}: {value}"


def to_prose(messages: List[Message]) -> List[Message]:
    """Earlier tool calls and results as plain text turns, for endpoints that flatten tool blocks into
    JSON-in-tags text (see LLMConfig.history_format)."""
    out: List[Message] = []
    for message in messages:
        if message.role == "assistant":
            parts = [message.text] if message.text else []
            for call in message.tool_calls:
                lines = "\n".join(_prose_value(k, v) for k, v in call.arguments.items())
                parts.append(f"(I used the {call.name} tool.)\n{lines}".rstrip())
            out.append(Message("assistant", text="\n\n".join(parts) or "(no reply)"))
        else:
            parts = [f"Result of {r.name}{' (error)' if r.is_error else ''}:\n{r.content}" for r in message.tool_results]
            if message.text:
                parts.append(message.text)
            out.append(Message("user", text="\n\n".join(parts) or "(empty)"))
    return out
