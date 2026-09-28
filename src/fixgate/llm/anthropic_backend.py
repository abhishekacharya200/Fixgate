"""Anthropic Messages API through the official SDK (the gateway's Claude Code path, or the real API)."""

from __future__ import annotations

from typing import List, Optional

from ..config import LLMConfig
from .base import LLMError, LLMResponse, Message, ToolCall, ToolSpec, Usage, to_prose
from .errors import error_from_status
from .retry import seconds_until_reset

# Models whose thinking cannot be switched off: an explicit {"type": "disabled"} is a 400 there.
ALWAYS_THINKING = ("claude-opus-5-5", "claude-fable-5")


class AnthropicBackend:
    name = "anthropic"

    def __init__(self, cfg: LLMConfig):
        import anthropic

        self._sdk = anthropic
        kwargs = {"api_key": cfg.api_key(), "timeout": cfg.request_timeout_s, "max_retries": 0}
        if cfg.uses_gateway:
            kwargs["default_headers"] = {"cache-control": "no-cache"}  # fresh samples, no replayed answers
        if cfg.base_url:
            kwargs["base_url"] = cfg.base_url
        self.client = anthropic.Anthropic(**kwargs)
        self.cfg = cfg
        self.caching = cfg.prompt_caching if cfg.prompt_caching is not None else not cfg.uses_gateway

    # ---------- request ----------

    def _thinking(self, model: str) -> Optional[dict]:
        setting = (self.cfg.thinking or "off").strip().lower()
        haiku = "haiku" in model
        if setting == "off":
            return None if model.startswith(ALWAYS_THINKING) else {"type": "disabled"}
        if setting == "adaptive":
            return {"type": "enabled", "budget_tokens": 4000} if haiku else {"type": "adaptive"}
        if setting.isdigit():
            return {"type": "enabled", "budget_tokens": int(setting)} if haiku else {"type": "adaptive"}
        raise ValueError(f"llm.thinking: '{self.cfg.thinking}' is not off, adaptive or a number")

    @staticmethod
    def _messages(messages: List[Message]) -> List[dict]:
        out = []
        for message in messages:
            if message.role == "assistant" and message.native and message.native_backend == "anthropic":
                out.append({"role": "assistant", "content": message.native})
            elif message.role == "assistant":
                content: list = []
                if message.text:
                    content.append({"type": "text", "text": message.text})
                for call in message.tool_calls:
                    content.append({"type": "tool_use", "id": call.id, "name": call.name, "input": call.arguments})
                out.append({"role": "assistant", "content": content or [{"type": "text", "text": "(no reply)"}]})
            else:
                content = [{"type": "tool_result", "tool_use_id": r.call_id, "content": r.content or "(no output)",
                            "is_error": r.is_error} for r in message.tool_results]
                if message.text:
                    content.append({"type": "text", "text": message.text})
                out.append({"role": "user", "content": content})
        return out

    def request(self, *, model: str, system: str, messages: List[Message], tools: List[ToolSpec],
                max_tokens: int) -> dict:
        params: dict = {
            "model": model,
            "max_tokens": max_tokens,
            "messages": self._messages(messages),
            "tools": [{"name": t.name, "description": t.description, "input_schema": t.input_schema} for t in tools],
        }
        if system:
            params["system"] = [{"type": "text", "text": system}]
        thinking = self._thinking(model)
        if thinking is not None:
            params["thinking"] = thinking
        if self.cfg.effort:
            params["output_config"] = {"effort": self.cfg.effort}
        if self.caching:
            # Two breakpoints: the fixed prefix (tools + system) and the end of the conversation so far.
            if system:
                params["system"][-1]["cache_control"] = {"type": "ephemeral"}
            last = params["messages"][-1]["content"]
            if last:
                last[-1]["cache_control"] = {"type": "ephemeral"}
        return params

    # ---------- call ----------

    def complete(self, *, model: str, system: str, messages: List[Message], tools: List[ToolSpec],
                 max_tokens: int) -> LLMResponse:
        sdk = self._sdk
        if self.cfg.prose_history:
            messages = to_prose(messages)
        params = self.request(model=model, system=system, messages=messages, tools=tools, max_tokens=max_tokens)
        try:
            response = self.client.messages.create(**params)
        except sdk.APIStatusError as exc:
            message = _error_message(exc)
            retry_after = _retry_after(exc) or seconds_until_reset(message)
            raise error_from_status(exc.status_code, message, retry_after,
                                    exc.response.headers.get("request-id") if exc.response is not None
                                    else None) from exc
        except sdk.APITimeoutError as exc:
            raise LLMError("timeout", str(exc)) from exc
        except sdk.APIConnectionError as exc:
            raise LLMError("connection", str(exc)) from exc
        text_parts, calls = [], []
        for block in response.content:
            if block.type == "text":
                text_parts.append(block.text)
            elif block.type == "tool_use":
                arguments = block.input if isinstance(block.input, dict) else {}
                error = None if isinstance(block.input, dict) else "tool input is not a JSON object"
                calls.append(ToolCall(block.id, block.name, arguments, error))
        usage = response.usage
        stop = {"end_turn": "end_turn", "tool_use": "tool_use", "max_tokens": "max_tokens",
                "refusal": "refusal", "stop_sequence": "end_turn", "pause_turn": "other"}.get(
            response.stop_reason or "", "other")
        return LLMResponse(
            text="".join(text_parts),
            tool_calls=calls,
            stop_reason=stop,
            usage=Usage(input_tokens=usage.input_tokens or 0, output_tokens=usage.output_tokens or 0,
                        cache_read_tokens=getattr(usage, "cache_read_input_tokens", 0) or 0,
                        cache_write_tokens=getattr(usage, "cache_creation_input_tokens", 0) or 0),
            model=response.model or model,
            request_id=getattr(response, "_request_id", None),
            raw={"params": params, "response": response.to_dict()},
            native=[_replayable(block.to_dict()) for block in response.content],
        )


def _replayable(block: dict) -> dict:
    """A response block as request input: drop fields the request schema does not accept."""
    if block.get("type") == "text":
        return {"type": "text", "text": block.get("text", "")}
    if block.get("type") == "tool_use":
        return {"type": "tool_use", "id": block["id"], "name": block["name"], "input": block.get("input") or {}}
    return block


def _error_message(exc) -> str:
    body = getattr(exc, "body", None)
    if isinstance(body, dict):
        err = body.get("error") if isinstance(body.get("error"), dict) else body
        if isinstance(err, dict) and err.get("message"):
            return str(err["message"])
    return str(getattr(exc, "message", "") or exc)


def _retry_after(exc) -> Optional[float]:
    response = getattr(exc, "response", None)
    if response is None:
        return None
    value = response.headers.get("retry-after")
    try:
        return float(value) if value else None
    except ValueError:
        return None
