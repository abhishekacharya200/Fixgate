# fixgate benchmark run `mini50-gpt6-luna`

Model `gpt-6-luna` (openai), 31 issues.

## Headline

| metric | value |
|---|---|
| resolved (hidden tests pass) | 22 / 31 = 71.0% (95% CI 53.4% to 83.9%) |
| cost per task (mean / median / p90) | $0.020 / $0.009 / $0.044 |
| cost per resolved issue | $0.028 |
| total cost | $0.613 |
| cost per task with prompt caching (estimate) | $0.004 |
| steps per task (mean / p90) | 19.2 / 31 |
| wall time per task (median / p90) | 356s / 881s |

Token counts for 591 model calls were counted locally with a tokenizer because the endpoint reported no usage for them; their costs are estimates at the model's API list price.

## Failure breakdown

| outcome | blame | count | share | meaning |
|---|---|---|---|---|
| resolved | none | 22 | 71% | The patch makes the benchmark's hidden tests pass. |
| wrong_fix (target_tests_fail: 8) | agent | 8 | 26% | The patch passed fixgate's checks but the benchmark's hidden tests fail. |
| stuck_loop (repeated_call: 1) | agent | 1 | 3% | The agent repeated the same action without progress. |

By blame: none 22, agent 9

## By repository

| repo | n | resolved | rate |
|---|---|---|---|
| django/django | 25 | 19 | 76% |
| sphinx-doc/sphinx | 6 | 3 | 50% |

## By difficulty (SWE-bench Verified annotation)

| difficulty | n | resolved | rate |
|---|---|---|---|
| 1-4 hours | 4 | 3 | 75% |
| 15 min - 1 hour | 15 | 11 | 73% |
| <15 min fix | 11 | 8 | 73% |
| >4 hours | 1 | 0 | 0% |

## Reproduction

Status: verified 29, not_reproduced 1, none 1
Checked against the maintainers' fix: 20 of 29 verified reproductions fail before it and pass after it (69%).
In 4 tasks the agent edited its reproduction script after fixgate verified it.
Resolve rate with a verified reproduction: 72% (n=29); without: 50% (n=2).

## fixgate's gate versus the hidden tests

| | hidden tests pass | hidden tests fail |
|---|---|---|
| gate accepted | 22 | 8 |
| gate rejected (graded anyway) | 0 | 0 |

Precision of the gate (accepted patches that are correct): 73%.

## Guardrail activity (all tasks)

- invalid_calls: 54
- fix_rounds: 32
- idle_turns: 5
- stuck_warnings: 2
- blocked_commands: 1
- blocked:interactive: 1

## Per issue

| instance | outcome | repro | steps | cost | time |
|---|---|---|---|---|---|
| django__django-11790 | resolved | verified | 15 | $0.008 | 426.86s |
| django__django-11815 | wrong_fix/target_tests_fail | verified | 31 | $0.040 | 881.45s |
| django__django-11848 | resolved | verified | 13 | $0.006 | 353.31s |
| django__django-11880 | resolved | verified | 15 | $0.006 | 355.58s |
| django__django-11885 | resolved | verified | 46 | $0.108 | 1322.45s |
| django__django-11951 | resolved | verified | 11 | $0.008 | 211.0s |
| django__django-11964 | resolved | verified | 24 | $0.018 | 545.41s |
| django__django-11999 | resolved | verified | 12 | $0.008 | 233.46s |
| django__django-12039 | wrong_fix/target_tests_fail | verified | 9 | $0.005 | 184.1s |
| django__django-12050 | resolved | verified | 10 | $0.003 | 170.3s |
| django__django-12143 | resolved | verified | 12 | $0.008 | 256.7s |
| django__django-12155 | resolved | verified | 12 | $0.007 | 438.85s |
| django__django-12193 | wrong_fix/target_tests_fail | verified | 15 | $0.010 | 333.57s |
| django__django-12209 | resolved | verified | 31 | $0.039 | 919.07s |
| django__django-12262 | resolved | verified | 19 | $0.013 | 432.7s |
| django__django-12273 | wrong_fix/target_tests_fail | verified | 18 | $0.009 | 376.32s |
| django__django-12276 | resolved | verified | 12 | $0.006 | 271.91s |
| django__django-12304 | resolved | verified | 22 | $0.013 | 631.76s |
| django__django-12308 | wrong_fix/target_tests_fail | verified | 16 | $0.017 | 305.88s |
| django__django-12325 | resolved | verified | 13 | $0.007 | 290.22s |
| django__django-12406 | wrong_fix/target_tests_fail | verified | 45 | $0.073 | 1076.38s |
| django__django-12708 | resolved | verified | 12 | $0.012 | 256.05s |
| django__django-12713 | resolved | verified | 15 | $0.007 | 341.37s |
| django__django-12774 | resolved | verified | 11 | $0.006 | 241.3s |
| django__django-9296 | resolved | verified | 14 | $0.007 | 236.15s |
| sphinx-doc__sphinx-10323 | resolved | not_reproduced | 31 | $0.045 | 680.94s |
| sphinx-doc__sphinx-10435 | wrong_fix/target_tests_fail | verified | 26 | $0.026 | 634.25s |
| sphinx-doc__sphinx-10466 | resolved | verified | 8 | $0.005 | 141.86s |
| sphinx-doc__sphinx-10673 | resolved | verified | 28 | $0.044 | 769.75s |
| sphinx-doc__sphinx-11510 | stuck_loop/repeated_call | none | 23 | $0.019 | 440.66s |
| sphinx-doc__sphinx-7590 | wrong_fix/target_tests_fail | verified | 27 | $0.031 | 636.49s |
