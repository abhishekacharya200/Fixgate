"""The cost cap: per-task limits on dollars, tokens, steps and wall-clock time.

Every LLM call is checked before it is made (with an estimate of its prompt) and recorded after it
returns. A limit that would be crossed stops the task with budget_exceeded before the money is spent.
Time spent waiting for a rate-limit window to reset does not count against the wall-clock limit.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Callable, Optional

from .config import BudgetConfig
from .llm.base import Usage
from .llm.pricing import PriceBook
from .taxonomy import Category, Outcome, TaskStop


class BudgetExceeded(TaskStop):
    def __init__(self, kind: str, used: float, limit: float, phase: str = ""):
        self.kind = kind
        self.used = used
        self.limit = limit
        super().__init__(Outcome(Category.BUDGET_EXCEEDED, kind, phase,
                                 f"{kind} limit reached: {used:.4g} of {limit:.4g}",
                                 details={"used": used, "limit": limit}))


@dataclass
class Totals:
    llm_calls: int = 0
    steps: int = 0
    input_tokens: int = 0        # uncached input
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    prompt_tokens: int = 0       # all input, cached or not
    cost_usd: float = 0.0
    cost_known: bool = True      # False once a call used a model with no price
    cost_cached_estimate_usd: float = 0.0  # what the calls would cost with prompt caching on the real API
    usage_estimated_calls: int = 0
    paused_s: float = 0.0


class Meter:
    def __init__(self, limits: BudgetConfig, prices: PriceBook, clock: Callable[[], float] = time.monotonic,
                 basis: str = "measured"):
        self.limits = limits
        self.prices = prices
        self.clock = clock
        self.basis = basis   # measured | cached_estimate: which cost max_cost_usd applies to
        self.started = clock()
        self.totals = Totals()
        self._last_prompt_tokens = 0
        self.warned = False

    # ---------- time ----------

    def elapsed(self) -> float:
        return self.clock() - self.started - self.totals.paused_s

    def pause(self, seconds: float) -> None:
        """Exclude a wait (e.g. for a rate-limit reset) from the wall-clock limit."""
        self.totals.paused_s += max(0.0, seconds)

    # ---------- checks ----------

    def check(self, phase: str = "", model: Optional[str] = None, estimated_prompt_tokens: int = 0) -> None:
        """Raise BudgetExceeded if the next LLM call would cross any limit."""
        t, lim = self.totals, self.limits
        if t.steps >= lim.max_steps:
            raise BudgetExceeded("steps", t.steps, lim.max_steps, phase)
        if self.elapsed() >= lim.max_wall_s:
            raise BudgetExceeded("wall_clock", self.elapsed(), lim.max_wall_s, phase)
        if t.prompt_tokens + estimated_prompt_tokens > lim.max_input_tokens:
            raise BudgetExceeded("input_tokens", t.prompt_tokens + estimated_prompt_tokens, lim.max_input_tokens,
                                 phase)
        if t.output_tokens >= lim.max_output_tokens:
            raise BudgetExceeded("output_tokens", t.output_tokens, lim.max_output_tokens, phase)
        if model is not None:
            price = self.prices.get(model)
            projected = self.budget_cost()
            if price and self.basis == "cached_estimate":
                prefix = min(estimated_prompt_tokens, self._last_prompt_tokens)
                projected += ((estimated_prompt_tokens - prefix) * price.cache_write + prefix * price.cache_read) / 1e6
            elif price:
                projected += estimated_prompt_tokens * price.input / 1e6
            if projected > lim.max_cost_usd:
                raise BudgetExceeded("cost_usd", projected, lim.max_cost_usd, phase)

    def budget_cost(self) -> float:
        """The dollar figure the cost cap applies to."""
        t = self.totals
        return t.cost_cached_estimate_usd if self.basis == "cached_estimate" else t.cost_usd

    def fraction_used(self) -> float:
        t, lim = self.totals, self.limits
        return max(t.steps / lim.max_steps, self.elapsed() / lim.max_wall_s, t.prompt_tokens / lim.max_input_tokens,
                   t.output_tokens / lim.max_output_tokens, self.budget_cost() / lim.max_cost_usd if lim.max_cost_usd else 0)

    def should_warn(self) -> bool:
        """True once, when the task first passes warn_fraction of any limit."""
        if not self.warned and self.fraction_used() >= self.limits.warn_fraction:
            self.warned = True
            return True
        return False

    # ---------- recording ----------

    def record_llm(self, model: str, usage: Usage) -> Optional[float]:
        t = self.totals
        t.llm_calls += 1
        t.input_tokens += usage.input_tokens
        t.output_tokens += usage.output_tokens
        t.cache_read_tokens += usage.cache_read_tokens
        t.cache_write_tokens += usage.cache_write_tokens
        t.prompt_tokens += usage.prompt_tokens
        if usage.estimated:
            t.usage_estimated_calls += 1
        cost = self.prices.cost(model, usage)
        if cost is None:
            t.cost_known = False
        else:
            t.cost_usd += cost
            cached = self.prices.cost_with_caching(model, usage.prompt_tokens, self._last_prompt_tokens,
                                                   usage.output_tokens)
            t.cost_cached_estimate_usd += cached or 0.0
        self._last_prompt_tokens = usage.prompt_tokens
        return cost

    def record_step(self) -> None:
        self.totals.steps += 1

    def snapshot(self) -> dict:
        t = self.totals
        return {
            "llm_calls": t.llm_calls, "steps": t.steps, "input_tokens": t.input_tokens,
            "output_tokens": t.output_tokens, "cache_read_tokens": t.cache_read_tokens,
            "cache_write_tokens": t.cache_write_tokens, "prompt_tokens": t.prompt_tokens,
            "cost_usd": round(t.cost_usd, 6) if t.cost_known else None,
            "cost_cached_estimate_usd": round(t.cost_cached_estimate_usd, 6) if t.cost_known else None,
            "cost_basis": self.basis,
            "usage_estimated_calls": t.usage_estimated_calls, "wall_s": round(self.elapsed(), 2),
            "paused_s": round(t.paused_s, 2),
        }


def make_meter(cfg, prices: PriceBook) -> Meter:
    """A meter for one task, with the cost basis resolved from the configuration."""
    basis = cfg.budget.cost_basis
    if basis == "auto":
        basis = "cached_estimate" if cfg.llm.uses_gateway else "measured"
    return Meter(cfg.budget, prices, basis=basis)
