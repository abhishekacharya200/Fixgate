"""OpenAI Chat Completions through the official SDK (the gateway's Codex path, or the real API).

Codex reports no token usage for a turn that ends in a tool call, so for those responses fixgate counts
the tokens itself with the o200k_base tokenizer (or a character estimate) and marks the usage estimated.
"""

from __future__ import annotations

import json
from typing import List, Optional

from ..config import LLMConfig
from .base import LLMError, LLMResponse, Message, ToolCall, ToolSpec, Usage, estimate_tokens, to_prose
from .errors import error_from_status
from .retry import seconds_until_reset

_encoder = None


def count_tokens(text: str) -> int:
    global _encoder
    if _encoder is None:
        try:
            import tiktoken

            _encoder = tiktoken.get_encoding("o200k_base")
        except Exception:  # not installed or no network for the first download
            _encoder = False
    if _encoder:
        return len(_encoder.encode(text, disallowed_special=()))
    return estimate_tokens(text)


class OpenAIBackend:
    name = "openai"

    def __init__(self, cfg: LLMConfig):
        import openai

        self._sdk = openai
        kwargs = {"api_key": cfg.api_key() or "missing-key", "timeout": cfg.request_timeout_s, "max_retries": 0}
        if cfg.uses_gateway:
            kwargs["default_headers"] = {"cache-control": "no-cache"}  # fresh samples, no replayed answers
        if cfg.base_url:
            kwargs["base_url"] = cfg.base_url.rstrip("/") + ("" if cfg.base_url.rstrip("/").endswith("/v1") else "/v1")
        self.client = openai.OpenAI(**kwargs)
        self.cfg = cfg

    @staticmethod
    def _messages(system: str, messages: List[Message]) -> List[dict]:
        out: List[dict] = [{"role": "system", "content": system}] if system else []
        for message in messages:
            if message.role == "assistant":
                item: dict = {"role": "assistant", "content": message.text or None}
                if message.tool_calls:
                    item["tool_calls"] = [{"id": c.id, "type": "function",
                                           "function": {"name": c.name, "arguments": json.dumps(c.arguments)}}
                                          for c in message.tool_calls]
                if item["content"] is None and not message.tool_calls:
                    item["content"] = "(no reply)"
                out.append(item)
            else:
                for result in message.tool_results:
                    content = result.content or "(no output)"
                    out.append({"role": "tool", "tool_call_id": result.call_id,
                                "content": ("ERROR: " + content) if result.is_error else content})
                if message.text:
                    out.append({"role": "user", "content": message.text})
        return out

    def request(self, *, model: str, system: str, messages: List[Message], tools: List[ToolSpec],
                max_tokens: int) -> dict:
        params = {
            "model": model,
            "messages": self._messages(system, messages),
            "tools": [{"type": "function", "function": {"name": t.name, "description": t.description,
                                                        "parameters": t.input_schema}} for t in tools],
            "max_completion_tokens": max_tokens,
        }
        if self.cfg.effort in ("low", "medium", "high"):
            params["reasoning_effort"] = self.cfg.effort
        return params

    def complete(self, *, model: str, system: str, messages: List[Message], tools: List[ToolSpec],
                 max_tokens: int) -> LLMResponse:
        sdk = self._sdk
        if self.cfg.prose_history:
            messages = to_prose(messages)
        params = self.request(model=model, system=system, messages=messages, tools=tools, max_tokens=max_tokens)
        try:
            response = self.client.chat.completions.create(**params)
        except sdk.APIStatusError as exc:
            message = _error_message(exc)
            retry_after = _retry_after(exc) or seconds_until_reset(message)
            raise error_from_status(exc.status_code, message, retry_after) from exc
        except sdk.APITimeoutError as exc:
            raise LLMError("timeout", str(exc)) from exc
        except sdk.APIConnectionError as exc:
            raise LLMError("connection", str(exc)) from exc
        if not response.choices:
            raise LLMError("server", "The response has no choices.")
        choice = response.choices[0]
        message = choice.message
        calls = []
        for i, call in enumerate(message.tool_calls or []):
            raw = call.function.arguments or "{}"
            try:
                arguments = json.loads(raw)
                error = None if isinstance(arguments, dict) else "arguments are not a JSON object"
            except ValueError as exc:
                arguments, error = {}, f"arguments are not valid JSON: {exc}"
            calls.append(ToolCall(call.id or f"call_{i}", call.function.name, arguments if isinstance(arguments, dict)
                                  else {}, error))
        usage = _usage(response.usage)
        if usage is None or (usage.input_tokens + usage.cache_read_tokens) == 0:
            prompt = json.dumps(params["messages"]) + json.dumps(params["tools"])
            completion = (message.content or "") + "".join(c.function.name + (c.function.arguments or "")
                                                           for c in message.tool_calls or [])
            usage = Usage(input_tokens=count_tokens(prompt), output_tokens=count_tokens(completion), estimated=True)
        finish = choice.finish_reason
        stop = "tool_use" if calls else {"stop": "end_turn", "length": "max_tokens",
                                         "content_filter": "refusal"}.get(finish or "", "other")
        return LLMResponse(text=message.content or "", tool_calls=calls, stop_reason=stop, usage=usage,
                           model=response.model or model, request_id=getattr(response, "_request_id", None),
                           raw={"params": params, "response": response.model_dump()})


def _usage(usage) -> Optional[Usage]:
    if usage is None:
        return None
    details = getattr(usage, "prompt_tokens_details", None)
    cached = getattr(details, "cached_tokens", 0) or 0 if details else 0
    prompt = usage.prompt_tokens or 0
    return Usage(input_tokens=max(0, prompt - cached), output_tokens=usage.completion_tokens or 0,
                 cache_read_tokens=cached)


def _error_message(exc) -> str:
    body = getattr(exc, "body", None)
    if isinstance(body, dict):
        err = body.get("error") if isinstance(body.get("error"), dict) else body
        if isinstance(err, dict) and err.get("message"):
            return str(err["message"])
    return str(getattr(exc, "message", "") or exc)


def _retry_after(exc) -> Optional[float]:
    response = getattr(exc, "response", None)
    value = response.headers.get("retry-after") if response is not None else None
    try:
        return float(value) if value else None
    except ValueError:
        return None
