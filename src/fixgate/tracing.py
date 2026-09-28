"""Trace logging: one JSON line per finished span, OpenTelemetry-style ids, secrets redacted.

A task's trace lives in <traces_dir>/<task_id>/: trace.jsonl holds the spans (task > phase > step > llm /
tool / verify), payloads.jsonl the full LLM requests and responses and full tool outputs, which are
too big to keep inside span attributes.
"""

from __future__ import annotations

import json
import os
import secrets
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator, List, Optional

from .redact import redact


def new_id(nbytes: int = 8) -> str:
    return secrets.token_hex(nbytes)


def _jsonable(value: Any) -> Any:
    try:
        json.dumps(value)
        return value
    except (TypeError, ValueError):
        if isinstance(value, dict):
            return {str(k): _jsonable(v) for k, v in value.items()}
        if isinstance(value, (list, tuple, set)):
            return [_jsonable(v) for v in value]
        return repr(value)


class Span:
    def __init__(self, tracer: "Tracer", name: str, kind: str, parent_id: Optional[str], attrs: dict):
        self.tracer = tracer
        self.span_id = new_id()
        self.parent_id = parent_id
        self.name = name
        self.kind = kind
        self.attrs = dict(attrs)
        self.events: List[dict] = []
        self.status = "ok"
        self.error: Optional[dict] = None
        self.start_ns = time.time_ns()
        self._t0 = time.perf_counter()
        self.duration_ms: Optional[float] = None

    def set(self, **attrs: Any) -> "Span":
        self.attrs.update(attrs)
        return self

    def add(self, key: str, amount: float) -> None:
        self.attrs[key] = self.attrs.get(key, 0) + amount

    def event(self, name: str, **attrs: Any) -> None:
        self.events.append({"name": name, "time_ns": time.time_ns(), "attrs": attrs})

    def fail(self, error: Any, kind: Optional[str] = None) -> None:
        self.status = "error"
        if isinstance(error, BaseException):
            self.error = {"type": kind or type(error).__name__, "message": str(error)[:2000]}
        else:
            self.error = {"type": kind or "error", "message": str(error)[:2000]}

    def record(self) -> dict:
        return {
            "trace_id": self.tracer.trace_id, "span_id": self.span_id, "parent_id": self.parent_id,
            "name": self.name, "kind": self.kind, "start_ns": self.start_ns,
            "end_ns": self.start_ns + int((self.duration_ms or 0) * 1e6), "duration_ms": self.duration_ms,
            "status": self.status, "error": self.error, "attrs": _jsonable(self.attrs),
            "events": _jsonable(self.events),
        }


class Tracer:
    def __init__(self, directory: Path, trace_id: Optional[str] = None, **attrs: Any):
        self.dir = Path(directory)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.trace_id = trace_id or new_id(16)
        self.attrs = attrs
        self._lock = threading.Lock()
        self._local = threading.local()
        self._spans = open(self.dir / "trace.jsonl", "a", encoding="utf-8")
        self._payloads = open(self.dir / "payloads.jsonl", "a", encoding="utf-8")

    # ---------- spans ----------

    def _stack(self) -> list:
        if not hasattr(self._local, "stack"):
            self._local.stack = []
        return self._local.stack

    @property
    def current(self) -> Optional[Span]:
        stack = self._stack()
        return stack[-1] if stack else None

    @contextmanager
    def span(self, name: str, kind: str = "internal", **attrs: Any) -> Iterator[Span]:
        parent = self.current
        span = Span(self, name, kind, parent.span_id if parent else None, attrs)
        stack = self._stack()
        stack.append(span)
        try:
            yield span
        except BaseException as exc:
            if span.status == "ok":
                span.fail(exc)
            raise
        finally:
            span.duration_ms = round((time.perf_counter() - span._t0) * 1000, 2)
            stack.pop()
            self._write(self._spans, span.record())

    def event(self, name: str, **attrs: Any) -> None:
        span = self.current
        if span is not None:
            span.event(name, **attrs)

    # ---------- payloads ----------

    def payload(self, kind: str, data: Any, span: Optional[Span] = None) -> str:
        """Store a large payload next to the spans; returns its id for a span attribute."""
        span = span or self.current
        pid = new_id()
        self._write(self._payloads, {"id": pid, "span_id": span.span_id if span else None, "kind": kind,
                                     "time_ns": time.time_ns(), "data": _jsonable(data)})
        return pid

    def _write(self, fh, record: dict) -> None:
        line = json.dumps(redact(record), ensure_ascii=False)
        with self._lock:
            fh.write(line + "\n")
            fh.flush()

    def close(self) -> None:
        with self._lock:
            for fh in (self._spans, self._payloads):
                try:
                    fh.close()
                except OSError:
                    pass


class NullTracer(Tracer):
    """A tracer that writes nothing (for unit tests and one-off calls)."""

    def __init__(self):  # noqa: D401 - no files
        self.dir = Path(os.devnull)
        self.trace_id = new_id(16)
        self.attrs = {}
        self._lock = threading.Lock()
        self._local = threading.local()
        self._spans = self._payloads = None

    def _write(self, fh, record: dict) -> None:
        return None

    def close(self) -> None:
        return None

    def payload(self, kind: str, data: Any, span: Optional[Span] = None) -> str:
        return new_id()


# ---------- reading traces back ----------

def load_spans(directory: Path) -> List[dict]:
    path = Path(directory) / "trace.jsonl"
    if not path.is_file():
        return []
    spans = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            spans.append(json.loads(line))
    return sorted(spans, key=lambda s: s["start_ns"])


def load_payload(directory: Path, payload_id: str) -> Optional[dict]:
    path = Path(directory) / "payloads.jsonl"
    if not path.is_file():
        return None
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            if payload_id in line:
                record = json.loads(line)
                if record.get("id") == payload_id:
                    return record
    return None


def render_tree(spans: List[dict], width: int = 110) -> str:
    """The spans as an indented tree with durations, tokens and cost."""
    children: dict = {}
    for span in spans:
        children.setdefault(span["parent_id"], []).append(span)
    lines: List[str] = []

    def walk(parent_id: Optional[str], depth: int) -> None:
        for span in children.get(parent_id, []):
            attrs = span.get("attrs") or {}
            bits = [f"{(span.get('duration_ms') or 0) / 1000:.1f}s"]
            if attrs.get("cost_usd") is not None:
                bits.append(f"${attrs['cost_usd']:.4f}")
            if attrs.get("input_tokens"):
                bits.append(f"in={attrs['input_tokens']} out={attrs.get('output_tokens', 0)}")
            if attrs.get("tool"):
                bits.append(str(attrs["tool"]))
            if attrs.get("exit_code") is not None:
                bits.append(f"exit={attrs['exit_code']}")
            if span.get("status") == "error":
                err = span.get("error") or {}
                bits.append(f"ERROR {err.get('type')}: {str(err.get('message'))[:60]}")
            label = f"{'  ' * depth}{span['name']}"
            lines.append(f"{label:<40} {' | '.join(bits)}"[:width])
            walk(span["span_id"], depth + 1)

    walk(None, 0)
    known = {s["span_id"] for s in spans}
    orphans = [s for s in spans if s["parent_id"] and s["parent_id"] not in known]
    for span in orphans:  # parents still running when the file was read
        children.setdefault("__orphan__", []).append(span)
    walk("__orphan__", 0)
    return "\n".join(lines)


# ---------- OpenTelemetry export ----------

def _otlp_value(value: Any) -> dict:
    if isinstance(value, bool):
        return {"boolValue": value}
    if isinstance(value, int):
        return {"intValue": str(value)}
    if isinstance(value, float):
        return {"doubleValue": value}
    if isinstance(value, str):
        return {"stringValue": value[:4000]}
    return {"stringValue": json.dumps(value, ensure_ascii=False)[:4000]}


def to_otlp(spans: List[dict], service_name: str = "fixgate") -> dict:
    """The spans as an OTLP/JSON ExportTraceServiceRequest (POST it to <collector>/v1/traces)."""
    out = []
    for span in spans:
        attrs = {f"fixgate.{k}": v for k, v in (span.get("attrs") or {}).items() if v is not None}
        attrs["fixgate.kind"] = span.get("kind")
        record = {
            "traceId": span["trace_id"], "spanId": span["span_id"], "name": span["name"], "kind": 1,
            "startTimeUnixNano": str(span["start_ns"]), "endTimeUnixNano": str(span["end_ns"]),
            "attributes": [{"key": k, "value": _otlp_value(v)} for k, v in attrs.items()],
            "events": [{"name": e.get("name", "event"), "timeUnixNano": str(e.get("time_ns", span["start_ns"])),
                        "attributes": [{"key": k, "value": _otlp_value(v)} for k, v in (e.get("attrs") or {}).items()]}
                       for e in span.get("events") or []],
            "status": ({"code": 2, "message": (span.get("error") or {}).get("message", "")}
                       if span.get("status") == "error" else {"code": 1}),
        }
        if span.get("parent_id"):
            record["parentSpanId"] = span["parent_id"]
        out.append(record)
    return {"resourceSpans": [{
        "resource": {"attributes": [{"key": "service.name", "value": {"stringValue": service_name}}]},
        "scopeSpans": [{"scope": {"name": "fixgate"}, "spans": out}],
    }]}
