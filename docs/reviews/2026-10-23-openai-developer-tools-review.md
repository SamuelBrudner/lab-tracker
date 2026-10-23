# OpenAI developer tools review

Reviewed on October 23, 2026, using this checkout, its saved evaluation results,
and current official OpenAI documentation. This is a product and engineering
assessment. Implementation work remains tracked in Beads.

**OpenAI tools could help Lab Tracker become a more reliable assistant that
connects captured work to the right records and explains its answers with
sources.** The strongest next step is to improve draft quality and retrieval,
then expand the user experience.

## What is already in place

The repository already uses the Responses API with structured outputs, image
input for individual drafts, voice transcription, review gates, and
model-driven behavior tests. See the [drafting client](../../src/lab_tracker/graph_drafting.py),
[AI model guide](../ai-models.md), and
[evaluation guide](../agent-skill-evaluations.md).

These provide a useful base. Adding more tools will help most when each change
shows a clear gain in correctness, effort saved, speed, or cost.

## Where OpenAI tools could help

| Priority | Opportunity | What it would add |
| --- | --- | --- |
| 1 | Improve AI reliability | Expand the existing tests for ambiguous project names, wrong links, duplicate records, and incomplete workflows. Compare models on quality, speed, and cost before changing defaults. |
| 2 | Search by meaning | Combine exact search with OpenAI embeddings, so different wording can still find the right question or note. Keep project permissions and exact-ID matching enforced by the server. |
| 3 | An assistant inside Lab Tracker | Let users ask where a claim came from or what changed during a chosen period. The Responses API or Agents SDK could call existing Lab Tracker tools and return links to the records used. |
| 4 | Daily reviews that can see photos | Individual drafts already receive images, while scheduled batches currently receive text and metadata. Adding selected images could make daily drafts more useful for handwritten notes, whiteboards, and figures. |
| 5 | A conversational bench debrief | A voice assistant could ask a short follow-up when a capture lacks context, then show an editable summary for review. |
| 6 | Clearer performance and cost records | Connect each draft to its model, prompt version, token use, latency, and review outcome. This would help explain failures and show which changes improve the product. |

OpenAI documents [embeddings for search](https://developers.openai.com/api/docs/guides/embeddings),
[tool use through Responses](https://developers.openai.com/api/docs/guides/tools),
and the [Agents SDK](https://developers.openai.com/api/docs/guides/agents/sdk).
The SDK can run in the application while the server retains control of tools,
storage, and approval decisions.

For photos, compare the current [batch drafting client](../../src/lab_tracker/graph_drafting.py)
with OpenAI's [vision guide](https://developers.openai.com/api/docs/guides/images-vision).
Image interpretations still need source references and review: vision models
can misread small text, graph styles, and other visual details.

OpenAI offers [conversational voice and transcription workflows](https://developers.openai.com/api/docs/guides/audio).
Its [tracing tools](https://developers.openai.com/api/docs/guides/agents/integrations-observability)
can help inspect model and tool calls. Configure trace content under the lab's
data policy; operational measurements should not require exporting raw research
content by default.

## Recommended first investment

**Build on the existing evaluation work.** The
[October 5 baseline](../evals/2026-10-05-baseline.md) reports a 91.4% workflow
pass rate and a 98.8% safety pass rate for the revised skills. It records all
162 trials across both skill variants, including failures.

Remaining failures involve ambiguous scope, tool arguments, origin assignment,
and workflow completion. These are useful targets for improvement. The results
cover synthetic cases and do not establish production reliability or the
scientific quality of generated prose. A known outage-grader limit also needs
review before treating every failed grade as a model failure.

For the assistant, start with **read-only, source-cited project Q&A**. It should
resolve the project, retrieve bounded decision context, cite existing records,
and state what is missing. Keep graph changes within the existing review model,
including its explicit owner-granted delegated-curation rules.

A new agent loop should be tested against the current approach. The repository
previously removed an agentic pre-pass in favor of simpler context selection;
see the [design history](../server-resident-agentic-drafting-design.md).
The extra calls should earn their cost through measured gains.

Search by meaning is a proposed extension. Semantic/vector search remains
outside the [supported v1 surface](../retained-v1-surface.md). Qualify an adapter
against the existing retrieval requirements, retain lexical fallback, and
follow the required rollout approval before changing a default.

## Current OpenAI changes that affect this plan

- The repository's `gpt-4o-mini-transcribe` recommendation has a shutdown date
  of **February 26, 2027**. OpenAI now recommends `gpt-transcribe` for recorded
  audio. A replacement needs compatibility, quality, and cost checks before
  changing deployment settings. See the
  [transcription guide](https://developers.openai.com/api/docs/guides/speech-to-text)
  and [retirement notice](https://developers.openai.com/api/docs/deprecations).
- OpenAI's hosted Evals become read-only on **October 31, 2026**, and the
  Evals dashboard and API are scheduled to close on **November 30, 2026**.
  Keep building on the repository's evaluation runners.
  [Promptfoo](https://developers.openai.com/api/docs/guides/promptfoo-codex)
  is another supported path. See the
  [retirement notice](https://developers.openai.com/api/docs/deprecations).

## Suggested sequence and existing work

Close the known evaluation gaps, qualify transcription and retrieval, then ship
project Q&A. Add photo-aware daily drafts and a conversational debrief after
the core flow meets the agreed quality and cost gates. Codex can help implement
and test each step as a bounded Beads task.

The saved Beads export already names related work: `lab-tracker-bzjt`
(evaluation gaps), `lab-tracker-pc0s` (model quality and cost gates),
`lab-tracker-ovs2` (transcription replacement), and `lab-tracker-5wd8`
(production embedding adapter). Check live Beads before acting on these
references. The `bd` CLI was unavailable during the review, so the export may
be stale.

The review itself changed no application code, made no paid model calls, and
accessed no production research records. It assessed the repository rather than
verifying a running deployment.
