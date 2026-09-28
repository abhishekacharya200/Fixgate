"""A scripted LLM for tests and dry runs: returns prepared responses in order, or raises prepared errors."""

from __future__ import annotations

from typing import Callable, List, Optional, Union

from .base import LLMError, LLMResponse, Message, ToolCall, ToolSpec, Usage

Step = Union[LLMResponse, LLMError, Callable[[List[Message]], Union[LLMResponse, LLMError]]]


def tool_call(name: str, call_id: Optional[str] = None, text: str = "", usage: Optional[Usage] = None,
              **arguments) -> LLMResponse:
    return LLMResponse(text=text, tool_calls=[ToolCall(call_id or f"call_{name}_{id(arguments)}", name, arguments)],
                       stop_reason="tool_use", usage=usage or Usage(input_tokens=1000, output_tokens=100),
                       model="scripted")


def reply(text: str, usage: Optional[Usage] = None) -> LLMResponse:
    return LLMResponse(text=text, tool_calls=[], stop_reason="end_turn",
                       usage=usage or Usage(input_tokens=1000, output_tokens=50), model="scripted")


class ScriptedBackend:
    name = "scripted"

    def __init__(self, steps: List[Step]):
        self.steps = list(steps)
        self.calls: List[dict] = []

    def complete(self, *, model: str, system: str, messages: List[Message], tools: List[ToolSpec],
                 max_tokens: int) -> LLMResponse:
        self.calls.append({"model": model, "system": system, "messages": list(messages),
                           "tools": [t.name for t in tools]})
        if not self.steps:
            raise LLMError("unknown", "The script has no more responses.")
        step = self.steps.pop(0)
        if callable(step) and not isinstance(step, (LLMResponse, LLMError)):
            step = step(messages)
        if isinstance(step, LLMError):
            raise step
        return step
