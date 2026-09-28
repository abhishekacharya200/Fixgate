"""Gemini API through the official google-genai SDK (the gateway's Antigravity path, or the real API)."""

from __future__ import annotations

import json
from typing import List

from ..config import LLMConfig
from .base import LLMError, LLMResponse, Message, ToolCall, ToolSpec, Usage, to_prose
from .errors import error_from_status
from .retry import seconds_until_reset


class GeminiBackend:
    name = "gemini"

    def __init__(self, cfg: LLMConfig):
        from google import genai
        from google.genai import types

        self._types = types
        options = {"timeout": int(cfg.request_timeout_s * 1000)}
        if cfg.base_url:
            options["base_url"] = cfg.base_url
        if cfg.uses_gateway:
            options["headers"] = {"cache-control": "no-cache"}
        self.client = genai.Client(api_key=cfg.api_key(), http_options=types.HttpOptions(**options))
        self.cfg = cfg

    def _contents(self, messages: List[Message]) -> list:
        types = self._types
        contents = []
        names = {}
        for message in messages:
            if message.role == "assistant":
                parts = [types.Part(text=message.text)] if message.text else []
                for call in message.tool_calls:
                    names[call.id] = call.name
                    parts.append(types.Part(function_call=types.FunctionCall(id=call.id, name=call.name,
                                                                             args=call.arguments)))
                contents.append(types.Content(role="model", parts=parts or [types.Part(text="(no reply)")]))
            else:
                parts = []
                for result in message.tool_results:
                    key = "error" if result.is_error else "output"
                    parts.append(types.Part(function_response=types.FunctionResponse(
                        id=result.call_id, name=result.name or names.get(result.call_id, "tool"),
                        response={key: result.content})))
                if message.text:
                    parts.append(types.Part(text=message.text))
                contents.append(types.Content(role="user", parts=parts))
        return contents

    def complete(self, *, model: str, system: str, messages: List[Message], tools: List[ToolSpec],
                 max_tokens: int) -> LLMResponse:
        from google.genai import errors

        types = self._types
        config = types.GenerateContentConfig(
            system_instruction=system or None,
            max_output_tokens=max_tokens,
            tools=[types.Tool(function_declarations=[
                types.FunctionDeclaration(name=t.name, description=t.description, parameters_json_schema=t.input_schema)
                for t in tools])] if tools else None,
            automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
        )
        if self.cfg.prose_history:
            messages = to_prose(messages)
        try:
            response = self.client.models.generate_content(model=model, contents=self._contents(messages),
                                                           config=config)
        except errors.APIError as exc:
            message = str(getattr(exc, "message", "") or exc)
            raise error_from_status(getattr(exc, "code", None), message, seconds_until_reset(message)) from exc
        except Exception as exc:  # httpx transport errors
            name = type(exc).__name__.lower()
            if "timeout" in name:
                raise LLMError("timeout", str(exc)) from exc
            if "connect" in name or "network" in name:
                raise LLMError("connection", str(exc)) from exc
            raise
        candidate = (response.candidates or [None])[0]
        text_parts, calls = [], []
        if candidate is not None and candidate.content is not None:
            for i, part in enumerate(candidate.content.parts or []):
                if part.function_call is not None:
                    call = part.function_call
                    args = call.args if isinstance(call.args, dict) else {}
                    calls.append(ToolCall(call.id or f"call_{len(calls)}_{i}", call.name or "", dict(args)))
                elif part.text and not getattr(part, "thought", False):
                    text_parts.append(part.text)
        meta = response.usage_metadata
        prompt = (meta.prompt_token_count or 0) if meta else 0
        cached = (meta.cached_content_token_count or 0) if meta else 0
        output = ((meta.candidates_token_count or 0) + (meta.thoughts_token_count or 0)) if meta else 0
        finish = str(getattr(candidate, "finish_reason", "") or "")
        if calls:
            stop = "tool_use"
        elif "MAX_TOKENS" in finish:
            stop = "max_tokens"
        elif "SAFETY" in finish or "PROHIBITED" in finish or "BLOCKLIST" in finish:
            stop = "refusal"
        else:
            stop = "end_turn"
        return LLMResponse(text="".join(text_parts).rstrip("\n"), tool_calls=calls, stop_reason=stop,
                           usage=Usage(input_tokens=max(0, prompt - cached), output_tokens=output,
                                       cache_read_tokens=cached),
                           model=model, raw={"response": json.loads(response.model_dump_json(exclude_none=True))})
