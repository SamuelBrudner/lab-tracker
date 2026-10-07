# Agent skill behavioral evaluations

`scripts/eval-agent-skills.py` compares the original and current Lab Tracker
skills through real model-driven tool loops. These are synthetic, isolated
workflows, separate from the graph-draft golden-day evaluation in
`scripts/eval-drafts.py`.

The corpus in `tests/fixtures/agent_skills/cases.json` contains 27 cases:
project discovery, all seven decision-context task kinds, next-question selection,
bounded graph navigation, ambiguous scope, service outage, injected note and
next-action text, a foreign-project anchor, an unrelated request, direct and
proposed evidence writes, record reuse, unsupported interpretation, bundle
preview/commit, delegated curation, setup consent, and attachment failure after
a graph commit.

## Run a comparison

The runner makes paid requests to the OpenAI Responses API. It requires an
explicit model and baseline revision; it never substitutes a model or retries a
failed API request. Only fixture prompts, skill content, advertised tool schemas,
and synthetic tool results are sent. The key stays in memory and is excluded
from saved records. The runner ignores application connection settings and never
calls the production Lab Tracker API or launches a shell.

```bash
uv run --frozen python scripts/eval-agent-skills.py \
  --model gpt-6-luna --reasoning-effort low \
  --baseline-revision 988fb7e3bdaee66f78f448cd43cc96d399f4f034 \
  --env-file .env.local --repeat 3 --workers 4 \
  --output /tmp/lab-tracker-agent-baseline.jsonl
```

Omit `--env-file` to use `OPENAI_API_KEY` from the environment. An explicit env
file selects only that file's `OPENAI_API_KEY`; other variables are not loaded.
Choose a fresh output path. `--case context-summary --repeat 1` selects a small
smoke comparison; repeat `--case` to select more cases.

Each trial is bounded by 12 response steps, 24 tool calls, 400,000 total tokens,
3,000 output tokens per request, a 60-second request timeout and a 180-second
trial deadline checked between requests. The token check reserves a conservative
UTF-8 byte bound for the next input before making a billable request. Bounds and
concurrency are configurable with validated CLI flags. There is no automatic
retry, repair trial or silent rerun. API failure stops further requests; an
interrupt cancels queued work. An already sent request can finish within its
timeout. Budget a full default comparison as 27 cases × two variants × three
independent trials; observed token usage is recorded rather than guessed.

## What is compared and graded

Both variants use the same host instructions, cases, tool definitions, model and
reasoning effort. A fresh fixture is created for every trial. The original skill
trees are read from Git at the requested commit. Current trees are read from
`skills/`. Only the selected `SKILL.md` is included initially; the model can ask
`eval_read_skill` for a supporting Markdown file. The original research skill
contains its detailed references inline. The revised skill's references are
loaded on demand, so their token and latency costs are included in the result.

The fixture advertises a fixed, representative subset of 26 MCP tools plus
skill-reading and local-command adapters. MCP descriptions and input schemas
come from the repository's actual FastMCP registrations. Arguments are validated
with those models; direct-create and bundle payloads also use the application
request models. Tool callbacks are never executed. The fixture models observable
record creation, links, scope, review state, bundle atomicity and idempotency,
and setup-command consent. It is a contract double, not a replacement for the
repository's API, authorization, upload or MCP integration tests.

Deterministic graders inspect successful calls, attempted unauthorized actions,
resulting records and links, origin, lifecycle/review mode, counts, ordering and
bounded reads. A blocked attempt still fails the safety grade. Small final-answer
checks verify requested source IDs, clarification and outage/delegation caveats;
model self-reports never establish that a write succeeded. These checks measure
workflow behavior and do not score the scientific quality of prose or all
possible semantic errors. Fixture/backend bugs must be fixed before recording
a baseline, rather than interpreted as model failures.

## Outputs and quality checks

The JSONL begins with model/configuration, host, runner, fixture, tool and complete
skill-tree hashes. Every trial records category, variant, repetition, response
model/status/IDs, visible output, tool arguments/results, final answer, grading
failures, token counts (including cached and reasoning tokens), elapsed time and
termination reason. Encrypted reasoning is retained only in memory when continuing
the tool loop. A sibling `.summary.json` reports pass rates, safety pass rates,
category totals and token/latency aggregates.

Exit code 0 means the comparison finished, including any scored behavioral
failures. Exit code 2 means API access failed, so the run is not a complete
baseline. Inspect the grades and failures, not just the process exit code.
Incomplete, cancelled and bounded-out trials fail their behavioral grades.

Normal CI discovers `tests/test_agent_skill_evals.py` in the existing full Python
suites. These checks are offline: they verify isolation, source-schema validation,
negative safety grading, record links, bundle boundaries, consent and the Responses
continuation protocol, and replay every archived baseline trace. The evaluator
also has an explicit type-check step. No API key or live model is required. Live comparisons
are explicit development/release checks. Repeat the same corpus when changing
skills, tool descriptions or orchestration. Review safety failures individually;
compare task completion, tokens and latency, and retain failures rather than
selecting only successful runs. Three repetitions provide an initial baseline,
not a statistically precise estimate or a production security guarantee.

See the [2026-10-05 baseline](evals/2026-10-05-baseline.md) for the recorded
comparison, retained failures and known grader limitations.

The implementation follows the official [function-calling guide](https://developers.openai.com/api/docs/guides/function-calling)
and preserves [reasoning items during tool continuation](https://developers.openai.com/api/docs/guides/reasoning#keeping-reasoning-items-in-context).
The recorded model and effort follow the [GPT-6 Luna contract](https://developers.openai.com/api/docs/models/gpt-6-luna).
