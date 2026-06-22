# Lab Tracker — Research Group Presentation

**Length:** ~15 min content + Q&A. Slides-first.
**Structure:** the problem, one resolution that addresses it, how it works (by example), and how to adopt it.

> This is the condensed 15-minute version for a research-group slot. It preserves
> the original talk's structure and argument at one to two points per idea. The
> fuller ~25-minute treatment (a separate slide per failure mode, a live demo,
> and an architecture overview) can be restored if the slot allows.

---

## 1. Title — *0.5 min*

- Title, name, date. Tagline: preserving the reasoning behind experiments, not only the resulting files.

## 2. The record loses experimental rationale — *2 min*

- Lead concretely: data files record when, where, and what was acquired, but not why. `2025_12_10_Rig2_session001.nwb` conveys nothing about what was expected, why it was collected, or what was observed at the bench. That context resides in notebooks, on whiteboards, and in individual memory, and it is lost when people leave the group.
- Two further problems follow from the same gap; state each concisely:
  - **AI assistants inherit the missing context.** Without grounding, an assistant begins each session without the lab's context — proposing analyses the group has already excluded, asserting unsupported claims, or producing work that is plausible but unrelated to the question under study. The last failure is the most costly, because it appears productive.
  - **The structured record that would resolve this is too costly to maintain.** Shared spreadsheets, wikis, ELNs, and lab-meeting slides become outdated within weeks. The more useful the structure, the greater the manual effort it requires, and manually maintained structure is not sustained.

## 3. One AI-maintained graph — *2 min*

- A single structured graph of questions, with attached data and analyses, is precisely the record the third problem identifies as desirable. The same graph addresses all three problems:
  - Researchers read it to recover rationale, onboard, and retain context after colleagues leave; the reasoning is preserved.
  - The agent reads it through its harness to produce grounded, on-target output.
  - The agent also writes to it through human-gated capture, so most maintenance no longer falls to individuals.
- These effects reinforce one another: AI-assisted capture keeps the graph current, and a current graph keeps the agent grounded. It is a research-context graph — human-readable, agent-readable, and AI-maintained — **not** a file manager or document store. The benefit to researchers does not depend on using AI; the benefit to AI-assisted work scales with adoption.

## 4. The graph, by example — *4 min*

- Three design principles, shown through one concrete thread rather than stated abstractly:
  - **Questions are first-class** and linked from broad to atomic: a motivating question sits above the specific experimental, control, and analysis questions beneath it. This records rationale before result.
  - **Everything hangs off a question.** A dataset cannot be committed without naming the question it addresses (it may tag secondary ones, marking whether the data supported or refuted each); notes, sessions, and claims attach to questions directly, and analyses and visualizations inherit theirs from the data and claims they are built on. Evidence is never orphaned from its rationale.
  - **Claims are "supported" only when backed by a dataset or analysis**; otherwise they remain "proposed." The model enforces this evidence discipline for human- and AI-authored claims alike.
- Trace one result from end to end:
  - Activate the question *"Does lateral inhibition normalize PN output?"*; run a Rig2 session and capture a bench note; promote it into the committed dataset `2025_12_10_Rig2_session001.nwb` with a provenance manifest; record the *divisive-normalization fit* linked to that dataset and question; assert the supported claim *"background odor scales PN gain ~0.6× (n = 18)"*; and attach **Figure 3b**.
  - Then traverse backward: from Figure 3b to every dataset, note, and decision behind it. The figure is not merely an output; it is evidence for a claim about a question.

## 5. Capture all day, confirm once — *2 min*

- This is the mechanism that reduces the maintenance cost, and it splits into two moments:
  - **At the bench — capture (beat 3).** A researcher captures a photo, voice note, photo-and-voice pair, or text at `/app/capture`, and figures register themselves from analysis code (`savefig`). Capture is frictionless: it asks nothing about which question the note belongs to, and it works offline. Each capture lands *staged*, not yet in the graph.
  - **End of day — the daily review (beat 4).** On a cadence the project sets (evening by default, or on demand), the system gathers the day's staged captures, assembles a project-scoped context packet, and the model returns reviewable draft operations: link this capture to a question, draft a note, suggest a sub-question, or flag uncertainty. The researcher works one review queue — edit, accept, reject, or defer — and the result commits through the same validation as any manual entry.
- Maintenance becomes the model drafting the structured entries from the day's photos and voice notes, with the researcher confirming them in one sitting.
- Guarantees: nothing is committed automatically; drafts that reference unknown entities are rejected; and human approval is always required.

## 6. The AI-harness role — *1.5 min*

- **Retrieval:** before research-facing work — selecting variables to plot, analyses to run, figure legends, or manuscript text — agents retrieve bounded decision context through the MCP server, grounded in the lab's actual questions, datasets, and claims.
- **Relevance:** because the graph encodes the broad-to-atomic question hierarchy, the harness keeps suggestions tied to the actual scientific goal; generic retrieval over files does not.
- **Write-back** is the same human-gated capture described in §5 — proposals only, validated and human-approved — and it is also what keeps the graph current.

## 7. Adoption — *1 min*

- One concrete request: select one active project and record its questions. Application URL (`/app`); point of contact.
- *Not in v1* (briefly): OCR on uploads, automatic question extraction, semantic/vector search, and PI approval gates — deferred in order to deliver the durable core first.

## 8. Discussion / Q&A — *remaining time*

- Prompts: which project to pilot; what capture friction would impede adoption; and, for those who use AI, which context they most often re-enter.
- Anticipated objections:
  - *Relative to an ELN, Benchling, or a shared drive* — Lab Tracker is questions-first, with evidence-linked claims and an agent-readable graph, not file storage.
  - *Whether it will become outdated, as past attempts did* — that is precisely the maintenance problem; AI-assisted capture is the specific mechanism that reduces it.
  - *Reservations about using or trusting AI* — the benefit to researchers is unconditional; AI assistance is optional and always human-gated.

---

**Timing:** ~13–14 min of core content leaves a buffer to finish at 15. Condense §6 to a single sentence if time is short; do not cut §4 (the worked example) or §7 (the request).
