# AI model maintenance

Lab Tracker keeps its supported model choices in
[`ai_model_catalog.py`](../src/lab_tracker/ai_model_catalog.py). Each entry names
the workloads, model setting, reviewed recommendation, official source,
selection reason, review date, and next review date. Recommendations need review
every 30 days. This date is a maintenance deadline, not a provider retirement date.

The choices reviewed on October 8, 2026 are:

| Provider and use | Reviewed choice | Source |
| --- | --- | --- |
| OpenAI graph drafting, daily reviews, analysis drafts, member alignment | `gpt-6.1-sol` | [OpenAI model specification](https://developers.openai.com/api/docs/models/gpt-6.1-sol) |
| OpenAI voice transcription | `gpt-4o-mini-transcribe` | [OpenAI transcription specification](https://developers.openai.com/api/docs/models/gpt-4o-mini-transcribe) |
| Anthropic graph drafting and alignment | `claude-sonnet-5-5` | [Claude Sonnet specification](https://platform.claude.com/docs/en/models/sonnet-5-5/overview) |
| Google graph drafting, alignment, voice transcription | `gemini-3.8-flash` | [Gemini Flash specification](https://ai.google.dev/gemini-api/docs/models/gemini-3.8-flash) |

Graph drafting prioritizes reasoning quality. OpenAI's dedicated transcription
model remains a low-cost audio choice; `gpt-6.1-sol` does not replace an audio
transcription model. OpenAI drafting uses Responses structured outputs, with the
model's default reasoning effort when no effort is configured. GPT-6.1 Sol
supports `low`, `medium`, `high`, `xhigh`, and `max`, and rejects `none`. Its default
effort is `medium`. The graph request timeout defaults to 300 seconds.

## Inspect the actual configuration

Run the server utility in the environment that serves the instance:

```bash
lab-tracker models
lab-tracker models --json
lab-tracker models --strict --json
```

These commands read configuration and make no network requests. They list every
retained AI setting, including inactive alternatives, and identify the active
provider's graph and transcription choices. Statuses mean:

- `recommended`: matches the last reviewed choice or an explicitly accepted
  snapshot. Check `review_overdue` before treating the recommendation as current.
- `superseded`: a known older choice has a reviewed replacement. An intentional
  older pin still receives this advisory status.
- `unreviewed`: the model is outside the curated choices. Review it against the
  source and the workload; its name does not establish its quality or age.
- `custom_endpoint`: an institutional gateway or local endpoint needs its own
  model review. Official API recommendations do not establish what it serves.

`--strict` exits with code 1 if an active choice needs attention, its review is
overdue, a known incompatible setting is configured, or an explicitly requested
availability check fails. Inactive provider drift stays visible but does not
fail this check. The default command remains advisory and exits successfully.
Neither mode changes settings, graph records, drafts, or credentials.

The authenticated Setup page also displays the active model, the last review
date and the next review date. A known older model shows **Upgrade available**;
an expired recommendation shows **Review overdue**. Ordinary Setup reads make
no external provider requests.

## Check account access explicitly

```bash
lab-tracker models --check-availability --json
lab-tracker models --check-availability --strict --json
```

This opt-in check queries the configured provider's model metadata using its
configured credential. It checks the configured model and, for official API
endpoints, the recommended replacement. It resolves aliases where the provider
supports that. Each request has a five-second deadline and a 64 KiB response
limit; redirects are refused. No inference request or research content is sent.
Keys, provider response bodies and exception messages are excluded from output.

`available` means model metadata was accessible to that credential at the
reported time. It does not prove billing quota, a successful inference request,
or graph quality. HTTP 404 reports `unavailable`; other HTTP, connection, timeout,
or malformed-response failures report `error`. Missing credentials are reported
without making a request. Custom endpoints are queried only for their configured
model, since their model names need not correspond to official API names.

Run the audit inside a deployed container to see that container's configuration.
A local checkout's audit cannot establish what a running server uses. In
particular, existing `LAB_TRACKER_OPENAI_MODEL` or `DEDICATED_OPENAI_MODEL` pins
override an upgraded package default. Update the instance's model configuration
and redeploy or restart through its normal release procedure to apply a change.

## Review new releases

At each review, open the entry's official source and check the provider's model
catalog and deprecation guidance. Verify the workload's input modalities,
endpoint, output contract, reasoning controls, cost, and latency. Do not treat a
provider's model list or the largest version number as a quality ranking.

Before changing a recommendation, exercise the provider request contract with
synthetic fixtures and compare representative draft quality, latency, and usage
under the project's research-data policy. Preserve explicit deployment pins
until that environment is deliberately updated. Update the relevant registry
entry's review date, accepted snapshots and known older choices, and keep the
configuration reference and deployment examples in step. Do not advance another
entry's date unless its source was also reviewed.

Use `lab-tracker models --strict --json` in an operator's scheduled check or
release process to detect an overdue review or an older active pin. The registry
does not automatically rank future releases or silently switch a deployment.

The [supervised maintenance coordinator](maintenance-coordinator.md) schedules
these audits against deployed containers, tracks official OpenAI retirement and
model release information, and prepares persistent review packets. Its explicit
candidate evaluation reuses the synthetic graph-quality fixture.
