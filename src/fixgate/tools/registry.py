"""Tool definitions, argument validation and dispatch.

Every tool call is validated against its JSON Schema before anything runs. An invalid call is answered
with an error the model can correct; it never reaches the sandbox.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple

from jsonschema import Draft202012Validator

from ..llm.base import ToolCall, ToolSpec


@dataclass
class ToolOutput:
    content: str
    is_error: bool = False
    data: dict = field(default_factory=dict)   # structured facts for traces (exit code, path...)


Handler = Callable[[dict], ToolOutput]


@dataclass
class Tool:
    name: str
    description: str
    schema: dict
    handler: Handler
    terminal: bool = False   # a phase tool: report_reproduction, submit_fix, give_up

    def spec(self) -> ToolSpec:
        return ToolSpec(self.name, self.description, self.schema)


class InvalidCall(Exception):
    pass


_INT = re.compile(r"^-?\d+$")
_NUM = re.compile(r"^-?\d+(\.\d+)?$")


def coerce_arguments(schema: dict, arguments: dict) -> Tuple[dict, bool]:
    """Fix the harmless type slips models make through some endpoints: "12" or [12] for an integer,
    "true" for a boolean. Anything else is left for validation to reject."""
    properties = schema.get("properties") or {}
    fixed, changed = dict(arguments), False
    for key, value in arguments.items():
        kind = (properties.get(key) or {}).get("type")
        if kind not in ("integer", "number", "boolean"):
            continue
        if isinstance(value, list) and len(value) == 1:
            value = value[0]
        if kind == "integer" and isinstance(value, str) and _INT.match(value.strip()):
            value = int(value.strip())
        elif kind == "number" and isinstance(value, str) and _NUM.match(value.strip()):
            value = float(value.strip())
        elif kind == "boolean" and isinstance(value, str) and value.strip().lower() in ("true", "false"):
            value = value.strip().lower() == "true"
        # Properties marked "x-clamp" take the nearest allowed value instead of an error (e.g. a timeout the model
        # gave in milliseconds).
        spec = properties.get(key) or {}
        if spec.get("x-clamp") and isinstance(value, (int, float)) and not isinstance(value, bool):
            if "maximum" in spec and value > spec["maximum"]:
                value = spec["maximum"]
            if "minimum" in spec and value < spec["minimum"]:
                value = spec["minimum"]
        if type(value) is not type(arguments[key]) or value != arguments[key]:
            fixed[key] = value
            changed = True
    return fixed, changed


class ToolRegistry:
    def __init__(self, tools: Optional[List[Tool]] = None):
        self.tools: Dict[str, Tool] = {}
        self._validators: Dict[str, Draft202012Validator] = {}
        for tool in tools or []:
            self.add(tool)

    def add(self, tool: Tool) -> None:
        Draft202012Validator.check_schema(tool.schema)
        self.tools[tool.name] = tool
        self._validators[tool.name] = Draft202012Validator(tool.schema)

    def specs(self) -> List[ToolSpec]:
        return [tool.spec() for tool in self.tools.values()]

    def validate(self, call: ToolCall) -> Tool:
        """The tool for this call, or InvalidCall with a message the model can act on."""
        tool = self.tools.get(call.name)
        if tool is None:
            raise InvalidCall(f"Unknown tool '{call.name}'. Available tools: {', '.join(self.tools)}.")
        if call.parse_error:
            raise InvalidCall(f"The arguments for {call.name} could not be parsed ({call.parse_error}). "
                              "Send a JSON object that matches the tool's schema.")
        call.arguments, call.coerced = coerce_arguments(tool.schema, call.arguments)
        errors = sorted(self._validators[call.name].iter_errors(call.arguments), key=lambda e: list(e.path))
        if errors:
            details = "; ".join(_describe(e) for e in errors[:5])
            raise InvalidCall(f"Invalid arguments for {call.name}: {details}.")
        return tool

    def run(self, call: ToolCall) -> ToolOutput:
        tool = self.validate(call)
        return tool.handler(call.arguments)


def _describe(error: Any) -> str:
    where = ".".join(str(p) for p in error.path)
    return f"{where + ': ' if where else ''}{error.message}"
