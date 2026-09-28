# Failure taxonomy

Every task ends in exactly one category. Categories are assigned by code from what happened, never from
the model's own description, and each has a single `blame`, so a report can separate "the model could not
do it" from "a guardrail stopped it on purpose" from "something broke".

| Category | Blame | Assigned when | Typical subcategories |
|---|---|---|---|
| `resolved` | none | benchmark only: the patch makes the hidden tests pass | |
| `pr_opened` | none | live: a human approved the patch and the draft PR exists | `draft` |
| `awaiting_approval` | none | live: a verified patch waits for a decision | `reproduced`, `not_reproduced` |
| `patch_ready` | none | a patch passed every fixgate check (before grading or approval) | `reproduced`, `not_reproduced` |
| `not_actionable` | none | triage or the agent decided no code change is needed | `question`, `needs_more_info`, `not_a_bug` |
| `triage_rejected` | agent | benchmark only: a real, fixable issue was triaged as not actionable | the triage kind |
| `no_patch` | agent | the agent gave up, or could not reproduce when the policy requires it | `cannot_fix`, `cannot_reproduce`, `not_reproduced` |
| `verification_failed` | agent | every submission failed fixgate's checks | `repro_still_fails`, `tests_regressed`, `empty_diff`, `tests_only`, `bad_test_command`, `stray_files` |
| `wrong_fix` | agent | benchmark only: passed fixgate's checks, failed the hidden tests | `target_tests_fail`, `broke_existing_tests`, `patch_did_not_apply` |
| `stuck_loop` | agent | the same tool call repeated `stuck_repeat_limit` times | `repeated_call` |
| `malformed_output` | agent | replies without tool calls, or only invalid calls, several steps in a row | `no_tool_call`, `invalid_tool_calls`, `triage_no_valid_call` |
| `budget_exceeded` | guardrail | a per-task limit was reached | `cost_usd`, `steps`, `input_tokens`, `output_tokens`, `wall_clock` |
| `policy_blocked` | guardrail | the patch or the agent's commands broke a policy rule | `test_tampering`, `forbidden_path`, `diff_too_large`, `secret_detected`, `binary_file`, `blocked_command_limit` |
| `llm_error` | infra | the model API failed after retries, or refused | `rate_limit`, `connection`, `server`, `auth`, `bad_request`, `context_overflow`, `refusal` |
| `sandbox_error` | infra | Docker or the environment failed | `image_pull`, `container_start`, `env_setup`, `git`, `exec` |
| `github_error` | infra | the GitHub API or the push failed | |
| `internal_error` | infra | an exception in fixgate itself (the traceback is kept) | the exception type |
| `approval_rejected` | human | a reviewer rejected the patch | `reviewer` |
| `approval_expired` | human | nobody decided before `expires_hours` | `timeout` |

## Precedence

The first thing that ends the task decides the category. Some cases worth spelling out:

- A budget limit reached while the agent is still working is `budget_exceeded`, even if a patch exists.
  The partial diff is kept in the result for analysis but never proposed.
- A submission that fails verification is fed back to the agent. Only when `max_fix_rounds` submissions
  have failed does the task end, as `policy_blocked` if the last one broke a policy rule and
  `verification_failed` otherwise.
- In benchmark reports `not_actionable` becomes `triage_rejected`, because every benchmark issue was
  fixed by its maintainers.
- A benchmark patch that passed fixgate's gate but not the hidden tests is `wrong_fix`. The gate's
  precision (accepted patches that are correct) is reported separately.
