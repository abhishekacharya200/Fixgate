# Design

This document explains how fixgate is put together and why. The README covers usage.

## The shape of a task

```
issue ──► triage ──► reproduce ──► fix ──► verify ──► approval ──► draft PR
            │            │           │        │           │
            ▼            ▼           ▼        ▼           ▼
     not actionable   repro must  agent's  fixgate re-  a person approves
     (stop, no cost)  fail before tools in  runs repro,  one exact payload
                      the fix     sandbox   tests, policy (sha256-bound)
```

Every task ends in exactly one `Outcome` (see `src/fixgate/taxonomy.py`). An outcome has a category, a
subcategory, the phase it ended in, and who or what it is attributed to (`blame`: agent, guardrail, infra,
human, or none). The category is always assigned by code. The model's own claims ("I reproduced it",
"the tests pass") are never used as evidence.

## Components

| Module | Responsibility |
|---|---|
| `llm/` | One provider-neutral interface (`complete(system, messages, tools)`) over the Anthropic, OpenAI and Gemini SDKs; retries; prices |
| `budget.py` | Per-task caps on dollars, tokens, steps and wall-clock time; checked before every call |
| `tracing.py` | Spans (task > phase > step > llm / tool / verify) as JSON lines, payloads in a side file, secrets redacted |
| `sandbox/` | Docker container per task: no network, dropped capabilities, resource limits, command timeouts, head/tail output capture |
| `tools/` | The agent's tools, JSON Schema validation, argument coercion, command and patch policy |
| `agent/loop.py` | The tool loop and its failure handling |
| `agent/pipeline.py` | The triage/reproduce/fix/verify state machine |
| `approval.py`, `publish.py` | Approval requests bound to a payload hash; publishing after approval only |
| `github.py`, `live.py` | GitHub REST client and the live-mode runner (clone, sandbox setup, network cut) |
| `eval/` | SWE-bench runner, official grading, reproduction check, report |
| `web/` | Local page for approvals, traces and reports |

## Decisions

**The phase tools are the only way forward.** The agent moves from reproduction to fix by calling
`report_reproduction`, and finishes by calling `submit_fix` or `give_up`. fixgate answers these calls
itself. A reproduction only counts if fixgate runs the command and it exits non-zero for a reason other
than a broken script. A fix only counts if the reproduction then exits 0, the agent's test command passes
(or fails only in ways it already failed before the change), and the diff passes the policy. A failed
check goes back to the agent as a tool error, up to `max_fix_rounds` times.

**Baseline comparison for tests.** Large projects often have tests that already fail. When the agent's
test command fails after the change, fixgate stashes exactly the changed files, runs the same command on
the original code, restores the changes, and compares the sets of failing tests. Only new failures block
a fix. Cached bytecode of the swapped files is removed before and after, because a stash can swap a
file's content within the same second and Python would then run stale `.pyc` files. (This was a real
false result during development, not a hypothetical.)

**Untrusted input stays in the user turn.** The issue text is wrapped in `<issue>` tags in the first user
message and the system prompt says to ignore instructions inside it. The real defence is structural: the
sandbox has no network and no secrets, the GitHub token never enters it, and nothing leaves it without a
human decision on the exact patch.

**A patch is approved, not a task.** The approval payload holds the title, body, branch and patch, and
its SHA-256. `publish` refuses to run unless the request is approved, unexpired and the payload still
hashes to the approved value. It applies the approved patch to a fresh clone at the recorded base commit,
so nothing the agent did after verification can slip in. Running `publish` twice returns the existing PR.

**Budgets are checked before spending.** `Meter.check` runs before every model call with an estimate of
the prompt size. A call that would pass the dollar cap is not made. Because the estimate is made before
the call, the overshoot is bounded by one call. Waiting for a rate-limit window to reset does not count
against the wall-clock limit.

**Retries are classified.** Rate limits wait (for the reset time when the endpoint says it, bounded by
`rate_limit_max_wait_s`); overloaded, server, timeout and connection errors back off with jitter
(connection errors wait longer, since they usually mean the endpoint is restarting); bad requests, auth
errors and refusals fail at once. A context overflow clears old tool results once and retries. The SDKs'
own retries are off so every attempt is visible in the trace.

**Context is cleared, not summarised.** When the prompt estimate passes `context_clear_tokens`, all but
the most recent tool results are replaced by a one-line note, and long arguments of old calls are cut.
This happens in batches rather than every turn, so a provider-side prompt cache is invalidated rarely.

**Benchmark numbers are not cherry-picked.** A benchmark instance runs once. `--retry-infra` reruns only
instances that ended in an infrastructure failure (LLM API, sandbox, internal error). Agent failures are
final. The reported success rate counts only patches that passed fixgate's own gate, which is what a
production deployment would propose; patches the gate stopped are graded separately ("shadow") only to
measure the gate.

## Development through the RAG gateway

During development fixgate talks to a local gateway that serves the Anthropic, OpenAI and Gemini APIs
through the Claude Code, Codex and Antigravity CLIs on subscription logins. Three things differ from the
real APIs and are handled explicitly:

1. **Tool history.** The gateway sends earlier turns to the CLI as one text block, rendering tool calls as
   `<tool_call name="...">{json}</tool_call>`. In a long session the model starts imitating that format
   inside its next real tool call, and the call arrives with no arguments. Replaying one failing request:
   0 of 2 attempts kept their arguments with native tool history, 3 of 3 with the history rendered as
   plain prose. `llm.history_format = "auto"` therefore sends prose history through the gateway and native
   tool blocks to real APIs.
2. **Response cache.** The gateway caches identical requests; fixgate sends `cache-control: no-cache` so
   that every benchmark attempt is a fresh sample.
3. **Cost.** Through Claude Code every call writes its whole prompt to the prompt cache (the conversation
   is one growing block), so the measured token cost is far above what the same run costs on the real
   API with prompt caching. Reports show both: the measured figure, and an estimate that bills each
   call's shared prefix at the cache-read price.
