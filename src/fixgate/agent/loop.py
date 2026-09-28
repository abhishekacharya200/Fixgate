"""The tool-use loop, with the failure handling most demos leave out.

Per step: check the budget, call the model (with retries), validate and run each tool call, and watch
for the ways an agent goes wrong: replies with no tool call, invalid calls, output cut off at the
length limit, the same call repeated, a context that keeps growing, a refusal. Each of those ends in a
specific Outcome instead of a crash or an endless loop.
"""

from __future__ import annotations

import json
from collections import Counter
from dataclasses import asdict, dataclass, field
from typing import Callable, List, Optional

from ..budget import Meter
from ..config import AgentConfig, RetryConfig
from ..llm.base import LLMBackend, LLMError, LLMResponse, Message, ToolCall, ToolResult, serialized_size
from ..llm.retry import call_with_retry, wall_sleep
from ..taxonomy import Category, Outcome, TaskStop
from ..tools.builtin import clip
from ..tools.registry import InvalidCall, ToolOutput, ToolRegistry
from ..tracing import Tracer


@dataclass
class Continue:
    """A phase tool's answer that keeps the loop going (e.g. 'reproduction verified, now fix it')."""

    message: str
    is_error: bool = False


@dataclass
class Finish:
    """A phase tool's answer that ends the loop."""

    name: str
    arguments: dict
    data: dict = field(default_factory=dict)


TerminalHook = Callable[[str, dict], "Continue | Finish"]

CLEARED = "[output cleared to keep the context small ({} characters). Run the tool again if you need it.]"


def _signature(call: ToolCall) -> str:
    return call.name + ":" + json.dumps(call.arguments, sort_keys=True)


def _trace_args(arguments: dict) -> dict:
    return {k: (clip(v, 2000) if isinstance(v, str) else v) for k, v in arguments.items()}


class AgentLoop:
    def __init__(self, *, llm: LLMBackend, model: str, system: str, registry: ToolRegistry, meter: Meter,
                 tracer: Tracer, retry: RetryConfig, agent: AgentConfig, max_tokens: int,
                 on_terminal: TerminalHook, phase: Callable[[], str],
                 sleep: Callable[[float], None] = wall_sleep):
        self.llm = llm
        self.model = model
        self.system = system
        self.registry = registry
        self.meter = meter
        self.tracer = tracer
        self.retry = retry
        self.agent = agent
        self.max_tokens = max_tokens
        self.on_terminal = on_terminal
        self.phase = phase
        self.sleep = sleep
        self.messages: List[Message] = []
        self.retries: Counter = Counter()
        self.stats: Counter = Counter()
        self._sent = 0  # messages already written to the payload log

    # ---------- the loop ----------

    def run(self, first_message: str) -> Finish:
        self.messages.append(Message("user", text=first_message))
        idle = invalid_steps = repeats = 0
        last_signature = None
        step = 0
        while True:
            step += 1
            phase = self.phase()
            with self.tracer.span(f"step {step}", "step", step=step, phase=phase) as span:
                self._maybe_clear_context()
                response = self._call_model(phase)
                self.meter.record_step()
                self.messages.append(Message("assistant", text=response.text, tool_calls=response.tool_calls,
                                             native=response.native, native_backend=getattr(self.llm, "name", None)))
                if response.text:
                    span.set(text=clip(response.text, 1500))
                if response.stop_reason == "refusal":
                    raise TaskStop(Outcome(Category.LLM_ERROR, "refusal", phase,
                                           "The model refused to continue.", span_id=span.span_id))

                notes: List[str] = []
                if not response.tool_calls:
                    idle += 1
                    self.stats["idle_turns"] += 1
                    if idle >= self.agent.max_idle_turns:
                        raise TaskStop(Outcome(Category.MALFORMED_OUTPUT, "no_tool_call", phase,
                                               f"{idle} replies in a row without a tool call",
                                               span_id=span.span_id))
                    if response.stop_reason == "max_tokens":
                        notes.append("Your reply was cut off at the output limit. Work in smaller steps.")
                    notes.append("You replied without calling a tool. Keep working with the tools, and finish "
                                 "by calling submit_fix (or give_up if the issue cannot be fixed).")
                    self.messages.append(Message("user", text="\n".join(notes)))
                    continue
                idle = 0

                results: List[ToolResult] = []
                finish: Optional[Finish] = None
                all_invalid = True
                for call in response.tool_calls:
                    if finish is not None:
                        results.append(ToolResult(call.id, call.name, "Not run: the task ended.", is_error=True))
                        continue
                    if response.stop_reason == "max_tokens":
                        results.append(ToolResult(call.id, call.name, "This call was cut off at the output length "
                                                  "limit, so it was not run. Send smaller pieces (for example several "
                                                  "edit_file calls instead of one large write_file).", is_error=True))
                        self.stats["truncated_calls"] += 1
                        continue
                    result, done, valid = self._run_tool(call, phase)
                    all_invalid = all_invalid and not valid
                    results.append(result)
                    if done is not None:
                        finish = done
                if finish is not None:
                    span.set(finished_by=finish.name)
                    return finish

                invalid_steps = invalid_steps + 1 if all_invalid else 0
                if invalid_steps >= self.agent.max_invalid_calls:
                    raise TaskStop(Outcome(Category.MALFORMED_OUTPUT, "invalid_tool_calls", phase,
                                           f"{invalid_steps} steps in a row with only invalid tool calls",
                                           span_id=span.span_id))

                signature = "|".join(_signature(c) for c in response.tool_calls)
                repeats = repeats + 1 if signature == last_signature else 0
                last_signature = signature
                if repeats + 1 >= self.agent.stuck_repeat_limit:
                    raise TaskStop(Outcome(Category.STUCK_LOOP, "repeated_call", phase,
                                           f"the same tool call was made {repeats + 1} times in a row",
                                           span_id=span.span_id, details={"call": signature[:500]}))
                if repeats + 2 == self.agent.stuck_repeat_limit:
                    notes.append("You have made exactly the same call several times with the same result. "
                                 "Try a different approach.")
                    self.stats["stuck_warnings"] += 1
                if self.meter.should_warn():
                    used = self.meter.snapshot()
                    notes.append(f"Budget warning: over {int(self.agent_warn_fraction * 100)}% of this task's budget "
                                 f"is used ({used['steps']} of {self.meter.limits.max_steps} steps, "
                                 f"${self.meter.budget_cost():.2f} of ${self.meter.limits.max_cost_usd:.2f}). Finish "
                                 "now: verify your change and call submit_fix, or give_up.")
                    self.stats["budget_warnings"] += 1
                    self.tracer.event("budget.warning", **used)
                self.messages.append(Message("user", text="\n\n".join(notes), tool_results=results))

    @property
    def agent_warn_fraction(self) -> float:
        return self.meter.limits.warn_fraction

    # ---------- model calls ----------

    def _call_model(self, phase: str) -> LLMResponse:
        specs = self.registry.specs()
        estimate = serialized_size(self.system, self.messages, specs)
        self.meter.check(phase, self.model, estimate)
        with self.tracer.span("llm", "llm", model=self.model, provider=getattr(self.llm, "name", "?"),
                              estimated_prompt_tokens=estimate, messages=len(self.messages)) as span:

            def on_retry(err: LLMError, attempt: int, wait: float) -> None:
                self.retries[err.kind] += 1
                span.event("retry", kind=err.kind, attempt=attempt, wait_s=round(wait, 2), message=err.message[:300])
                if err.kind == "rate_limit":
                    self.meter.pause(wait)

            def attempt() -> LLMResponse:
                return self.llm.complete(model=self.model, system=self.system, messages=self.messages, tools=specs,
                                         max_tokens=self.max_tokens)

            overflowed = False
            while True:
                try:
                    response = call_with_retry(attempt, self.retry, on_retry=on_retry, sleep=self.sleep)
                    break
                except LLMError as err:
                    if err.kind == "context_overflow" and not overflowed:
                        overflowed = True
                        span.event("context_overflow", message=err.message[:300])
                        self._clear_results(keep=2)
                        continue
                    span.fail(err, err.kind)
                    raise TaskStop(Outcome(Category.LLM_ERROR, err.kind, phase, err.message[:500],
                                           span_id=span.span_id, details={"status": err.status})) from err
            cost = self.meter.record_llm(self.model, response.usage)
            usage = response.usage
            span.set(input_tokens=usage.input_tokens, output_tokens=usage.output_tokens,
                     cache_read_tokens=usage.cache_read_tokens, cache_write_tokens=usage.cache_write_tokens,
                     usage_estimated=usage.estimated, cost_usd=cost, stop_reason=response.stop_reason,
                     tool_calls=[c.name for c in response.tool_calls], request_id=response.request_id,
                     response_model=response.model)
            new = [asdict(m) for m in self.messages[self._sent:]]
            for item in new:
                item.pop("native", None)
            self._sent = len(self.messages)
            span.set(payload=self.tracer.payload("llm_exchange", {"new_messages": new, "response": response.raw}))
            return response

    # ---------- tools ----------

    def _run_tool(self, call: ToolCall, phase: str):
        """(ToolResult, Finish or None, was the call valid)"""
        with self.tracer.span(f"tool {call.name}", "tool", tool=call.name, args=_trace_args(call.arguments)) as span:
            try:
                tool = self.registry.validate(call)
            except InvalidCall as err:
                self.stats["invalid_calls"] += 1
                message = str(err)
                if not call.arguments and call.name in self.registry.tools and not call.parse_error:
                    # Seen through proxies that flatten tool history: the arguments get lost on the way.
                    self.stats["empty_arguments"] += 1
                    message += (" The call arrived with no arguments at all. Send it again with every required "
                                "argument; for long file contents prefer several edit_file calls.")
                span.fail(message, "invalid_call")
                return ToolResult(call.id, call.name, message, is_error=True), None, False
            self.stats[f"tool:{call.name}"] += 1
            if call.coerced:
                self.stats["coerced_arguments"] += 1
            if tool.terminal:
                answer = self.on_terminal(call.name, call.arguments)
                if isinstance(answer, Finish):
                    span.set(terminal=True)
                    return ToolResult(call.id, call.name, "Done."), answer, True
                span.set(is_error=answer.is_error, output_chars=len(answer.message))
                span.set(payload=self.tracer.payload("tool_output", answer.message))
                return ToolResult(call.id, call.name, answer.message, is_error=answer.is_error), None, True
            try:
                output: ToolOutput = tool.handler(call.arguments)
            except (FileNotFoundError, PermissionError, OSError) as err:
                output = ToolOutput(f"{type(err).__name__}: {err}", is_error=True)
            span.set(is_error=output.is_error, output_chars=len(output.content), **output.data)
            if output.data.get("rejected"):
                self.stats[f"rejected_{output.data['rejected']}"] += 1
            if output.data.get("blocked"):
                self.stats[f"blocked:{output.data['blocked']}"] += 1
            if output.is_error:
                span.status = "error"
                span.error = {"type": "tool_error", "message": output.content[:300]}
            span.set(payload=self.tracer.payload("tool_output", output.content))
            return ToolResult(call.id, call.name, output.content, is_error=output.is_error), None, True

    # ---------- context ----------

    def _maybe_clear_context(self) -> None:
        estimate = serialized_size(self.system, self.messages, self.registry.specs())
        if estimate >= self.agent.context_clear_tokens:
            self._clear_results(keep=self.agent.keep_recent_results)
            after = serialized_size(self.system, self.messages, self.registry.specs())
            self.tracer.event("context.cleared", before_tokens=estimate, after_tokens=after)
            self.stats["context_clears"] += 1

    def _clear_results(self, keep: int) -> None:
        """Replace all but the last `keep` tool results (and long old tool inputs) with a short note."""
        positions = [(i, j) for i, m in enumerate(self.messages) for j in range(len(m.tool_results))]
        old = positions[:-keep] if keep else positions
        cutoff = old[-1][0] if old else -1
        for i, j in old:
            result = self.messages[i].tool_results[j]
            if not result.content.startswith("[output cleared"):
                result.content = CLEARED.format(len(result.content))
        for i, message in enumerate(self.messages):
            if i >= cutoff or message.role != "assistant":
                continue
            for call in message.tool_calls:
                for key, value in list(call.arguments.items()):
                    if isinstance(value, str) and len(value) > 1500:
                        call.arguments[key] = value[:300] + f"\n[... {len(value) - 300} characters cleared ...]"
                        message.native = None
