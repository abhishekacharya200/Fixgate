# Results

All numbers below come from files in [`docs/results/`](results/). Nothing was rerun or filtered to improve
them; where a run is incomplete, the text says so.

## 1. Fixing real issues: SWE-bench Verified mini

**Setup.** The 50-issue [Verified mini](https://huggingface.co/datasets/MariusHobbhahn/swe-bench-verified-mini)
subset of SWE-bench Verified (25 Django, 25 Sphinx). Each issue runs once in the official SWE-bench Docker
image for that instance, with only the issue text as input. Grading is the official
`swebench.harness.run_evaluation` (swebench 5.0.2), unchanged. Model: OpenAI `gpt-6-luna`, called through the
Chat Completions format of the local [RAG gateway](https://github.com/abhishekacharya200/RAG-Gateway) (Codex
CLI on a ChatGPT login). Configuration: [`config.toml`](results/mini50-gpt6-luna/config.toml); prompts as they
were at the start of the run: [`prompts_at_start.py`](results/mini50-gpt6-luna/prompts_at_start.py).

**The run is incomplete: 31 of 50 issues.** After 31 issues the ChatGPT account reached its Codex usage
limit, which resets on 2026-10-26. The 31 finished issues are the first 31 in dataset order (all 25 Django
issues, then 6 Sphinx issues); none was skipped or retried. The two issues in flight when the limit hit
never finished and are not counted.

| | |
|---|---|
| Resolved (the maintainers' hidden tests pass) | **22 of 31 = 71.0%** (95% Wilson interval 53% to 84%) |
| Django | 19 of 25 (76%) |
| Sphinx | 3 of 6 (50%) |
| Model cost per issue at API list prices | mean $0.020, median $0.009, p90 $0.044 (token counts estimated locally: Codex reports no usage for tool-call turns) |
| Same, with the provider's prompt caching (estimate) | mean $0.004 |
| Cost per resolved issue | $0.028 |
| Agent steps per issue | mean 19, p90 31 |
| Time per issue | median 6 minutes, p90 15 minutes (through the gateway; each model call took 15 to 65 s) |

**Where the other 9 went.**

| Outcome | Count | Meaning |
|---|---|---|
| `wrong_fix` / `target_tests_fail` | 8 | passed fixgate's own checks, but the hidden tests for the issue still fail |
| `stuck_loop` / `repeated_call` | 1 | the agent repeated one call 4 times; stopped by the loop detector |

No task ended in a budget, policy, sandbox or LLM-API failure before the quota ran out.

**fixgate's gate.** 30 patches passed fixgate's checks; 22 of them are correct by the hidden tests, so the
gate's precision was 73%. In 32 submissions, 2 were sent back by the gate (reproduction still failing or
tests regressing) and fixed by the agent on the next round.

**Reproductions.** The agent produced a verified reproduction (a script that fails before its fix) for 29
of 31 issues. Checked against the maintainers' real fix, 20 of those 29 fail before it and pass after it.
The strict check undercounts: some of the 9 are correct reproductions of the bug that assert a different
output than the maintainers chose, or call a function the maintainers removed. But it also found two
reproductions that proved nothing (one copied the buggy code into the script instead of calling Django,
one failed with its own `TypeError`), and in 4 issues the agent edited its reproduction after fixgate
had verified it. Both loopholes are closed in the current code (see the README); this run used the
earlier version.

**Agent noise the guardrails absorbed.** 54 invalid tool calls were answered with an error instead of being
executed (52 were `bash` calls whose command arrived empty from the Codex endpoint), 5 replies without a tool
call were nudged, 1 command was blocked by policy (an interactive program), and 2 repeated-call warnings were
sent. None of these ended a task except the one stuck loop.

Per-issue results, patches and grades: [`results/mini50-gpt6-luna/`](results/mini50-gpt6-luna/)
([`report.md`](results/mini50-gpt6-luna/report.md)). One full trace:
[`example-trace/trace.jsonl`](results/mini50-gpt6-luna/example-trace/trace.jsonl).

### Pilots (not part of the headline)

Single-issue pilots used while building fixgate, reported for completeness:

| Model (endpoint) | Issue | Result |
|---|---|---|
| Claude Haiku 4.5 (Claude Code) | django-11815 | resolved, 46 steps, 3 minutes |
| Claude Haiku 4.5 (Claude Code) | sphinx-8035 | stopped by the cost cap; the patch it had would have resolved the issue (graded afterwards). This led to the cost-basis fix described in the design notes |
| gpt-6-luna (Codex) | django-11815 | wrong fix (handled only lazy translation values) |
| Gemini 3.8 Flash (Antigravity) | django-11815 | stuck loop after 6 steps |

## 2. Triage accuracy on maintainer-labelled issues

SWE-bench cannot test triage because all its issues are fixable. This set has 136 closed issues from
pytest, black, poetry and pandas, sampled with a fixed seed from issues that carry exactly one of each
project's type labels (bug, feature, documentation, question/user support). The model sees the title and
original body only, never labels or comments. Model: Claude Haiku 4.5. References and predictions:
[`results/triage-haiku/`](results/triage-haiku/).

| | |
|---|---|
| Accuracy (4 classes) | **69.9%** (macro F1 0.68) |
| Bug: precision / recall | 0.54 / 0.94 |
| Feature | 0.80 / 0.80 |
| Documentation | 1.00 / 0.75 |
| Question | 0.64 / 0.28 |
| Bugs that triage sends to the agent | 84% |
| Usage questions that triage stops | 53% |
| Cost per issue | $0.004 |

The weak spot is usage questions: most of them read as bug reports ("pytest hangs on GitHub Actions",
"IndexError on all files in my project") and only turned out to be questions after a maintainer
investigated. Text-only triage cannot know that. The current behaviour errs toward working on possible
bugs: a question that slips through costs one agent run, which usually ends in `give_up`, while a real bug
triaged as a question would be dropped.

## 3. Live mode on a real open issue

`fixgate run python-humanize/humanize#379` ("precisedelta() silently drops the sign of negative
timedeltas"), Claude Haiku 4.5:

- **First run:** reproduced and fixed, but the patch added a `conftest.py` that mocked `freezegun` out of
  `sys.modules` (the sandbox setup had not installed the project's `tests` extra). fixgate's gate passed
  it; the human review rejected it. That rejection led to three changes: setup installs every common test
  extra and requirement file, the patch policy blocks module stubs in test code, and the prompt forbids
  faking dependencies.
- **Second run:** a source-only patch (sign detected before the absolute value is taken, `-` prefix on the
  result), reproduction verified before and after, the project's `precisedelta` tests passing, 35 steps,
  2.7 minutes. It is waiting for a decision as an approval request; no pull request has been opened.
