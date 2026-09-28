"""The agent's working tools: bash, read_file, write_file, edit_file and search_code.

All of them act inside the sandbox. Writes are confined to the repository and a scratch folder, Python
edits that no longer parse are rejected (and the file is left unchanged), and command output is cut to
a fixed size before it reaches the model.
"""

from __future__ import annotations

import difflib
import posixpath
import shlex
from dataclasses import dataclass, field
from typing import Callable, List, Optional

from ..config import AgentConfig, PolicyConfig
from ..taxonomy import Category, Outcome, TaskStop
from .policy import check_command, write_denied
from .registry import Tool, ToolOutput

DEFAULT_LINES = 400
MAX_LINES = 800
MAX_READ_BYTES = 3_000_000


def clip(text: str, limit: int) -> str:
    """Keep the start and the end of a long text (errors are usually at the end)."""
    if len(text) <= limit:
        return text
    head = int(limit * 0.6)
    tail = limit - head
    omitted = len(text) - head - tail
    return f"{text[:head]}\n\n[... {omitted} characters omitted ...]\n\n{text[-tail:]}"


@dataclass
class ToolContext:
    sandbox: object
    policy: PolicyConfig
    agent: AgentConfig
    python: str = "python"
    blocked_commands: int = 0
    commands_run: int = 0
    edited_files: set = field(default_factory=set)
    phase_of: Optional[Callable[[], str]] = None   # the pipeline's current phase, for outcomes raised here

    @property
    def phase(self) -> str:
        return self.phase_of() if self.phase_of else "fix"

    @property
    def workdir(self) -> str:
        return self.sandbox.workdir

    @property
    def scratch(self) -> str:
        return self.sandbox.scratch_dir

    def absolute(self, path: str) -> str:
        path = path.strip()
        if not path.startswith("/"):
            path = posixpath.join(self.workdir, path)
        return posixpath.normpath(path)

    def display(self, path: str) -> str:
        if path.startswith(self.workdir.rstrip("/") + "/"):
            return path[len(self.workdir.rstrip("/")) + 1:]
        return path


def _near_matches(text: str, needle: str) -> str:
    first = next((line for line in needle.splitlines() if line.strip()), "")
    if not first:
        return ""
    lines = text.splitlines()
    stripped = [line.strip() for line in lines]
    close = difflib.get_close_matches(first.strip(), stripped, n=3, cutoff=0.6)
    hits = []
    for candidate in close:
        number = stripped.index(candidate) + 1
        hits.append(f"line {number}: {lines[number - 1][:120]}")
    if not hits:
        return "Read the file again: it may have changed."
    return "Closest lines: " + "; ".join(hits) + ". Check the exact whitespace and indentation."


def make_tools(ctx: ToolContext) -> List[Tool]:
    sandbox = ctx.sandbox
    limit = ctx.agent.tool_output_chars

    # ---------- bash ----------

    def bash(args: dict) -> ToolOutput:
        command = args["command"]
        rule = check_command(command)
        if rule is not None:
            ctx.blocked_commands += 1
            if ctx.blocked_commands > ctx.agent.max_blocked_commands:
                raise TaskStop(Outcome(Category.POLICY_BLOCKED, "blocked_command_limit", ctx.phase,
                                       f"{ctx.blocked_commands} commands were blocked by policy "
                                       f"(last: {rule.name})"))
            return ToolOutput(f"Blocked by policy rule '{rule.name}': {rule.reason}", is_error=True,
                              data={"blocked": rule.name})
        ctx.commands_run += 1
        result = sandbox.exec(command, timeout=args.get("timeout"))
        header = f"exit code: {result.exit_code}" + (" (timed out)" if result.timed_out else "")
        body = clip(result.output.rstrip("\n"), limit) or "(no output)"
        return ToolOutput(f"{header}\n{body}", data={"exit_code": result.exit_code, "timed_out": result.timed_out,
                                                    "output_bytes": result.total_bytes,
                                                    "duration_s": result.duration_s})

    # ---------- read_file ----------

    def read_file(args: dict) -> ToolOutput:
        path = ctx.absolute(args["path"])
        info = sandbox.file_info(path)
        if info is None:
            return ToolOutput(f"No such file: {ctx.display(path)}", is_error=True)
        size, kind = info
        if "directory" in kind:
            listing = sandbox.exec(f"ls -la {shlex.quote(path)} | head -300", timeout=60)
            return ToolOutput(f"{ctx.display(path)} is a directory:\n{clip(listing.output, limit)}")
        if size > MAX_READ_BYTES:
            return ToolOutput(f"{ctx.display(path)} is {size} bytes; use bash (grep, sed -n) to read parts of it.",
                              is_error=True)
        data = sandbox.read_file(path)
        if b"\0" in data[:8192]:
            return ToolOutput(f"{ctx.display(path)} is a binary file.", is_error=True)
        lines = data.decode("utf-8", "replace").splitlines()
        total = len(lines)
        start = max(1, int(args.get("start_line") or 1))
        end = int(args.get("end_line") or (start + DEFAULT_LINES - 1))
        end = min(end, total, start + MAX_LINES - 1)
        if total == 0:
            return ToolOutput(f"[{ctx.display(path)} is empty]")
        if start > total:
            return ToolOutput(f"{ctx.display(path)} has only {total} lines.", is_error=True)
        more = " Use start_line/end_line to read more." if end < total or start > 1 else ""
        note = f"[{ctx.display(path)}: lines {start}-{end} of {total}.{more}]"
        numbered = "\n".join(f"{n:>6}\t{lines[n - 1]}" for n in range(start, end + 1))
        return ToolOutput(note + "\n" + clip(numbered, limit), data={"path": ctx.display(path), "lines": [start, end]})

    # ---------- write helpers ----------

    def check_write(path: str) -> Optional[str]:
        real = sandbox.realpath(path)
        return write_denied(real, ctx.workdir, ctx.scratch, ctx.policy.forbidden_paths)

    def syntax_problem(path: str, new_text: str) -> Optional[str]:
        """A SyntaxError message if new_text does not parse but the current file does."""
        if not path.endswith(".py") or not path.startswith(ctx.workdir.rstrip("/") + "/"):
            return None
        probe = f"{ctx.scratch}/syntax-probe.py"
        sandbox.write_file(probe, new_text.encode("utf-8"))
        check = ("{py} -c \"import ast,sys; ast.parse(open(sys.argv[1], encoding='utf-8').read(), sys.argv[1])\" "
                 "{target}")
        after = sandbox.exec(check.format(py=ctx.python, target=shlex.quote(probe)), timeout=60)
        if after.ok:
            return None
        if sandbox.file_info(path) is None:
            return after.output.strip().splitlines()[-1] if after.output.strip() else "SyntaxError"
        before = sandbox.exec(check.format(py=ctx.python, target=shlex.quote(path)), timeout=60)
        if not before.ok:
            return None  # the file did not parse before either (e.g. a template); do not block
        lines = [line for line in after.output.strip().splitlines() if line.strip()]
        return "\n".join(lines[-4:]).replace(probe, ctx.display(path))

    def drop_bytecode(path: str) -> None:
        """Remove cached bytecode of an edited module: an edit that keeps the file size within the same
        second would otherwise keep running the old code."""
        if path.endswith(".py"):
            directory, name = posixpath.split(path)
            sandbox.exec(f"rm -f {shlex.quote(directory)}/__pycache__/{shlex.quote(name[:-3])}.*.pyc", timeout=30)

    # ---------- write_file ----------

    def write_file(args: dict) -> ToolOutput:
        path = ctx.absolute(args["path"])
        denied = check_write(path)
        if denied:
            return ToolOutput(f"Cannot write {ctx.display(path)}: {denied}.", is_error=True)
        content = args["content"]
        problem = syntax_problem(path, content)
        if problem:
            return ToolOutput(f"Not written: the new content of {ctx.display(path)} does not parse:\n{problem}",
                              is_error=True, data={"rejected": "syntax"})
        sandbox.write_file(path, content.encode("utf-8"))
        drop_bytecode(path)
        if path.startswith(ctx.workdir.rstrip("/") + "/"):
            ctx.edited_files.add(ctx.display(path))
        return ToolOutput(f"Wrote {len(content)} characters to {ctx.display(path)}.",
                          data={"path": ctx.display(path)})

    # ---------- edit_file ----------

    def edit_file(args: dict) -> ToolOutput:
        path = ctx.absolute(args["path"])
        denied = check_write(path)
        if denied:
            return ToolOutput(f"Cannot edit {ctx.display(path)}: {denied}.", is_error=True)
        if sandbox.file_info(path) is None:
            return ToolOutput(f"No such file: {ctx.display(path)}. Use write_file to create it.", is_error=True)
        text = sandbox.read_file(path).decode("utf-8", "surrogateescape")
        old, new = args["old_text"], args["new_text"]
        if old == new:
            return ToolOutput("old_text and new_text are identical; nothing to change.", is_error=True)
        count = text.count(old) if old else 0
        if count == 0:
            return ToolOutput(f"old_text was not found in {ctx.display(path)}. {_near_matches(text, old)}",
                              is_error=True)
        if count > 1 and not args.get("replace_all"):
            return ToolOutput(f"old_text appears {count} times in {ctx.display(path)}. Include more surrounding "
                              f"lines so it is unique, or set replace_all to true.", is_error=True)
        updated = text.replace(old, new) if args.get("replace_all") else text.replace(old, new, 1)
        problem = syntax_problem(path, updated)
        if problem:
            return ToolOutput(f"Edit rejected, {ctx.display(path)} is unchanged: the result does not parse:\n"
                              f"{problem}", is_error=True, data={"rejected": "syntax"})
        sandbox.write_file(path, updated.encode("utf-8", "surrogateescape"))
        drop_bytecode(path)
        ctx.edited_files.add(ctx.display(path))
        first = text[: text.index(old)].count("\n") + 1
        new_lines = updated.splitlines()
        start = max(1, first - 3)
        end = min(len(new_lines), first + new.count("\n") + 3)
        snippet = "\n".join(f"{n:>6}\t{new_lines[n - 1]}" for n in range(start, end + 1))
        replaced = f"{count} replacements" if args.get("replace_all") else "1 replacement"
        return ToolOutput(f"Edited {ctx.display(path)} ({replaced}). Lines {start}-{end} now read:\n"
                          f"{clip(snippet, limit)}", data={"path": ctx.display(path)})

    # ---------- search_code ----------

    def search_code(args: dict) -> ToolOutput:
        pattern = args["pattern"]
        where = (args.get("path") or ".").strip() or "."
        glob = (args.get("glob") or "").strip()
        most = max(1, min(int(args.get("max_results") or 60), 300))
        if glob:
            base = where.rstrip("/")
            spec = f":(glob){glob}" if base in (".", "") else f":(glob){base}/**/{glob}"
        else:
            spec = where
        command = (f"git grep -n -I -E -e {shlex.quote(pattern)} -- {shlex.quote(spec)} | head -n {most + 1}")
        result = sandbox.exec(command, timeout=120)
        output = result.output.rstrip("\n")
        if "not a git repository" in output:
            include = f"--include={shlex.quote(glob)} " if glob else ""
            result = sandbox.exec(f"grep -rnIE {include}-e {shlex.quote(pattern)} {shlex.quote(where)} | "
                                  f"head -n {most + 1}", timeout=120)
            output = result.output.rstrip("\n")
        if not output:
            return ToolOutput("No matches.")
        if output.startswith("fatal:") or "grep: " in output.splitlines()[0]:
            return ToolOutput(f"Search failed: {output[:500]}", is_error=True)
        lines = output.splitlines()
        note = f"\n[more than {most} matches; narrow the pattern, path or glob]" if len(lines) > most else ""
        return ToolOutput(clip("\n".join(line[:300] for line in lines[:most]), limit) + note)

    workdir = ctx.workdir
    return [
        Tool("bash",
             f"Run a shell command in the sandbox (bash, working directory {workdir}). There is no network. "
             f"The default time limit is {getattr(sandbox, 'exec_timeout', 180)}s; set timeout (up to "
             f"{getattr(sandbox, 'max_exec_timeout', 900)}s) for slow test runs. stdout and stderr are combined "
             "and long output is cut in the middle. Interactive programs and background processes are not allowed.",
             {"type": "object", "properties": {
                 "command": {"type": "string", "minLength": 1, "description": "The command to run."},
                 "timeout": {"type": "integer", "minimum": 1, "maximum": 900, "x-clamp": True,
                             "description": "Seconds before the command is stopped."}},
              "required": ["command"], "additionalProperties": False},
             bash),
        Tool("read_file",
             f"Read a text file with line numbers. Relative paths start at {workdir}. Shows {DEFAULT_LINES} lines "
             "by default; use start_line and end_line for other parts of long files.",
             {"type": "object", "properties": {
                 "path": {"type": "string", "minLength": 1},
                 "start_line": {"type": "integer", "minimum": 1},
                 "end_line": {"type": "integer", "minimum": 1}},
              "required": ["path"], "additionalProperties": False},
             read_file),
        Tool("write_file",
             f"Create a file or replace its whole content. Allowed inside {workdir} and {ctx.scratch}. Use it for "
             "the reproduction script and for new files; use edit_file to change part of an existing file.",
             {"type": "object", "properties": {
                 "path": {"type": "string", "minLength": 1},
                 "content": {"type": "string"}},
              "required": ["path", "content"], "additionalProperties": False},
             write_file),
        Tool("edit_file",
             "Replace an exact piece of text in a file. old_text must match the file exactly, including "
             "indentation, and must appear once unless replace_all is true. An edit that leaves a Python file "
             "unparseable is rejected and the file is left unchanged.",
             {"type": "object", "properties": {
                 "path": {"type": "string", "minLength": 1},
                 "old_text": {"type": "string", "minLength": 1},
                 "new_text": {"type": "string"},
                 "replace_all": {"type": "boolean"}},
              "required": ["path", "old_text", "new_text"], "additionalProperties": False},
             edit_file),
        Tool("search_code",
             "Search the repository's files with an extended regular expression (git grep -E). Optionally "
             "limit it to a directory (path) and a file name glob (glob, for example '*.py'). Returns "
             "file:line:text lines.",
             {"type": "object", "properties": {
                 "pattern": {"type": "string", "minLength": 1},
                 "path": {"type": "string"},
                 "glob": {"type": "string"},
                 "max_results": {"type": "integer", "minimum": 1, "maximum": 300}},
              "required": ["pattern"], "additionalProperties": False},
             search_code),
    ]
