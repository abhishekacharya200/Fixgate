"""API list prices, used to put a dollar figure on every call.

Through the local gateway the calls run on subscriptions, so these numbers are what the same tokens
would cost on the real API. Prices are USD per million tokens. Models not listed get no cost (None);
add them under [pricing."model-id"] in fixgate.toml.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Optional

from .base import Usage

PRICES_AS_OF = "2026-09-27"
PRICES_SOURCE = ("Anthropic API pricing (claude-api reference, cached 2026-06-24); OpenAI API pricing page "
                 "(developers.openai.com/api/docs/pricing, read 2026-09-27)")


@dataclass(frozen=True)
class Price:
    input: float
    output: float
    cache_write: float
    cache_read: float


PRICES: Dict[str, Price] = {
    "claude-haiku-4-5": Price(1.00, 5.00, 1.25, 0.10),
    "claude-sonnet-5": Price(2.00, 10.00, 2.50, 0.20),
    "claude-opus-5": Price(5.00, 25.00, 6.25, 0.50),
    "claude-opus-5-5": Price(4.00, 20.00, 5.00, 0.20),
    "claude-fable-5-1": Price(10.00, 50.00, 12.50, 0.25),
    # OpenAI has no cache-write premium: new input is billed at the input price.
    "gpt-6-luna": Price(0.10, 0.50, 0.10, 0.01),
    "gpt-6-sol": Price(2.00, 10.00, 2.00, 0.20),
    "gpt-5.6-terra": Price(2.00, 12.00, 2.00, 0.20),
    "gpt-5.6-luna": Price(0.20, 1.20, 0.20, 0.02),
    "gpt-5.5": Price(5.00, 30.00, 5.00, 0.50),
}

ALIASES = {"claude-haiku-4-5-20251001": "claude-haiku-4-5"}


class PriceBook:
    def __init__(self, overrides: Optional[Dict[str, Dict[str, float]]] = None):
        self.prices = dict(PRICES)
        for model, fields in (overrides or {}).items():
            base = self.prices.get(model)
            values = {"input": fields.get("input", base.input if base else 0.0),
                      "output": fields.get("output", base.output if base else 0.0)}
            values["cache_write"] = fields.get("cache_write", values["input"] * 1.25)
            values["cache_read"] = fields.get("cache_read", values["input"] * 0.1)
            self.prices[model] = Price(**values)

    def get(self, model: str) -> Optional[Price]:
        model = model.split("/", 1)[-1]  # "provider/model" names used by the gateway
        return self.prices.get(model) or self.prices.get(ALIASES.get(model, ""))

    def cost(self, model: str, usage: Usage) -> Optional[float]:
        """Dollar cost of one call. input_tokens counts uncached input only (Anthropic's convention)."""
        price = self.get(model)
        if price is None:
            return None
        return (usage.input_tokens * price.input + usage.output_tokens * price.output
                + usage.cache_write_tokens * price.cache_write + usage.cache_read_tokens * price.cache_read) / 1e6

    def cost_with_caching(self, model: str, prompt_tokens: int, cached_prefix_tokens: int,
                          output_tokens: int) -> Optional[float]:
        """Estimated cost if the shared prefix had been served from the prompt cache (real API behaviour
        that the gateway cannot reproduce): the prefix at the cache-read price, the rest as a cache write."""
        price = self.get(model)
        if price is None:
            return None
        cached = min(cached_prefix_tokens, prompt_tokens)
        return (cached * price.cache_read + (prompt_tokens - cached) * price.cache_write
                + output_tokens * price.output) / 1e6
