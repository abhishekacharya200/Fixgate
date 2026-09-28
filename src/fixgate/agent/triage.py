"""Triage: one structured LLM call that classifies the issue and decides whether the agent should work on it."""

from __future__ import annotations

from typing import Callable

from ..budget import Meter
from ..config import RetryConfig
from ..llm.base import LLMBackend, LLMError, Message, ToolResult, ToolSpec, serialized_size
from ..llm.retry import call_with_retry, wall_sleep
from ..models import Issue, TriageResult
from ..taxonomy import Category, Outcome, TaskStop
from ..tools.registry import InvalidCall, Tool, ToolOutput, ToolRegistry
from ..tracing import Tracer
from .prompts import TRIAGE_SYSTEM, triage_message

KINDS = ["bug", "feature", "documentation", "question", "duplicate", "invalid", "other"]
SEVERITIES = ["low", "medium", "high", "critical"]

TRIAGE_SCHEMA = {
    "type": "object",
    "properties": {
        "kind": {"type": "string", "enum": KINDS},
        "actionable": {"type": "boolean"},
        "needs_more_info": {"type": "boolean"},
        "severity": {"type": "string", "enum": SEVERITIES},
        "component": {"type": "string", "description": "Module, package or area of the code involved."},
        "summary": {"type": "string", "description": "One sentence: what is wrong or wanted."},
        "suggested_labels": {"type": "array", "items": {"type": "string"}, "maxItems": 6},
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
        "evidence": {"type": "string", "description": "Short quotes or facts from the issue that support "
                                                      "the assessment."},
    },
    "required": ["kind", "actionable", "needs_more_info", "severity", "component", "summary", "suggested_labels",
                 "confidence", "evidence"],
    "additionalProperties": False,
}

TRIAGE_TOOL = ToolSpec("record_triage", "Record the triage assessment of the issue.", TRIAGE_SCHEMA)


def triage_issue(issue: Issue, *, llm: LLMBackend, model: str, meter: Meter, tracer: Tracer, retry: RetryConfig,
                 max_tokens: int = 2048, attempts: int = 3, sleep: Callable[[float], None] = wall_sleep
                 ) -> TriageResult:
    registry = ToolRegistry([Tool("record_triage", TRIAGE_TOOL.description, TRIAGE_SCHEMA,
                                  lambda args: ToolOutput("ok"))])
    system = TRIAGE_SYSTEM.format(repo=issue.repo)
    messages = [Message("user", text=triage_message(issue))]
    with tracer.span("triage", "phase", model=model) as phase_span:
        for attempt in range(1, attempts + 1):
            estimate = serialized_size(system, messages, [TRIAGE_TOOL])
            meter.check("triage", model, estimate)
            with tracer.span("llm", "llm", model=model, attempt=attempt, estimated_prompt_tokens=estimate) as span:
                try:
                    response = call_with_retry(
                        lambda: llm.complete(model=model, system=system, messages=messages, tools=[TRIAGE_TOOL],
                                             max_tokens=max_tokens),
                        retry, sleep=sleep,
                        on_retry=lambda err, n, wait: (span.event("retry", kind=err.kind, attempt=n, wait_s=wait),
                                                       meter.pause(wait) if err.kind == "rate_limit" else None))
                except LLMError as err:
                    span.fail(err, err.kind)
                    raise TaskStop(Outcome(Category.LLM_ERROR, err.kind, "triage", err.message[:500],
                                           span_id=span.span_id)) from err
                cost = meter.record_llm(model, response.usage)
                meter.record_step()
                span.set(input_tokens=response.usage.input_tokens, output_tokens=response.usage.output_tokens,
                         cache_read_tokens=response.usage.cache_read_tokens,
                         cache_write_tokens=response.usage.cache_write_tokens, cost_usd=cost,
                         usage_estimated=response.usage.estimated, stop_reason=response.stop_reason,
                         payload=tracer.payload("llm_exchange", {"response": response.raw}))
            if response.stop_reason == "refusal":
                raise TaskStop(Outcome(Category.LLM_ERROR, "refusal", "triage", "The model refused to triage."))
            calls = [c for c in response.tool_calls if c.name == "record_triage"]
            messages.append(Message("assistant", text=response.text, tool_calls=response.tool_calls))
            if not calls:
                problem = "You did not call record_triage. Call it now with your assessment."
                if response.tool_calls:
                    results = [ToolResult(c.id, c.name, "Only record_triage is available.", True)
                               for c in response.tool_calls]
                    messages.append(Message("user", text=problem, tool_results=results))
                else:
                    messages.append(Message("user", text=problem))
                continue
            try:
                registry.validate(calls[0])
            except InvalidCall as err:
                results = [ToolResult(c.id, c.name, str(err) if c is calls[0] else "Not run.", True)
                           for c in response.tool_calls]
                messages.append(Message("user", text="Fix the arguments and call record_triage again.",
                                        tool_results=results))
                continue
            args = calls[0].arguments
            result = TriageResult(kind=args["kind"], actionable=args["actionable"],
                                  needs_more_info=args["needs_more_info"], severity=args["severity"],
                                  component=args["component"].strip(), summary=args["summary"].strip(),
                                  suggested_labels=[str(x) for x in args["suggested_labels"]][:6],
                                  confidence=float(args["confidence"]), evidence=args["evidence"].strip())
            phase_span.set(kind=result.kind, actionable=result.actionable, needs_more_info=result.needs_more_info,
                           severity=result.severity, proceed=result.proceed, confidence=result.confidence)
            return result
        raise TaskStop(Outcome(Category.MALFORMED_OUTPUT, "triage_no_valid_call", "triage",
                               f"no valid record_triage call after {attempts} attempts"))
