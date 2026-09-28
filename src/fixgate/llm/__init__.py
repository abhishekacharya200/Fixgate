"""LLM access: one neutral interface over the Anthropic, OpenAI and Gemini APIs."""

from __future__ import annotations

from ..config import LLMConfig
from .base import LLMBackend, LLMError, LLMResponse, Message, ToolCall, ToolResult, ToolSpec, Usage


def make_backend(cfg: LLMConfig) -> LLMBackend:
    if cfg.provider == "anthropic":
        from .anthropic_backend import AnthropicBackend

        return AnthropicBackend(cfg)
    if cfg.provider == "openai":
        from .openai_backend import OpenAIBackend

        return OpenAIBackend(cfg)
    if cfg.provider == "gemini":
        from .gemini_backend import GeminiBackend

        return GeminiBackend(cfg)
    raise ValueError(f"Unknown provider '{cfg.provider}'")


__all__ = ["LLMBackend", "LLMError", "LLMResponse", "Message", "ToolCall", "ToolResult", "ToolSpec", "Usage",
           "make_backend"]
