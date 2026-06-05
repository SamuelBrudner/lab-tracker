# Lab Tracker — Research Group Presentation

**Length:** ~15 min content + Q&A. Slides-first.
**Structure:** the problem, one resolution that addresses it, how it works (by example), how to try it.

> This is the tight 15-minute cut for a research-group slot. It preserves the
> original talk's spine and message at one-to-two beats per idea. The longer
> ~25-minute treatment (separate problem slides per failure mode, a live demo,
> an "under the hood" stack tour) can be restored if the slot grows.

---

## 1. Title — *0.5 min*

- Title, name, date. Tagline: keeping the *reasoning* behind experiments, not just the files.

## 2. The record loses the *why* — *2 min*

- Lead concretely: data files record *when / where / what*, not *why*. `2025_12_10_Rig2_session001.nwb` tells you nothing about what was expected, why it was collected, or what was observed at the bench. That context lives in notebooks, whiteboards, and memory — and is lost when people leave.
- Two problems compound from the same gap; name them, don't belabor them:
  - **AI assistants inherit the missing context.** Without grounding, the assistant starts from zero each session — proposing analyses the lab already ruled out, asserting unsupported claims, or producing work that is plausible but drifts from the question actually being asked (the costly one, because it looks productive).
  - **The structured record that would fix this is too costly to maintain.** Spreadsheets, wikis, ELNs, and lab-meeting slides go stale within weeks. The more useful the structure, the more there is to maintain by hand — and hand-maintained structure decays.

## 3. One AI-maintained graph — *2 min*

- A single structured graph of questions with attached data and analyses is exactly the record the third problem describes wanting. The same graph addresses all three:
  - Humans read it to recover rationale, onboard, and retain context after people leave → the *why* is preserved.
  - The agent reads it through its harness for grounded, on-target output.
  - The agent also writes to it through human-gated capture, so most upkeep shifts off people.
- These reinforce each other: AI-assisted capture keeps the graph current, and a current graph is what keeps the AI grounded. It is a research-context graph — human-readable, agent-readable, AI-maintained — **not** a file manager or document store. The human benefit does not depend on using AI; the AI benefit scales with how much each person uses it.

## 4. The graph, by example — *4 min*

- Two design points, shown through one real thread rather than stated abstractly:
  - **Questions are first-class**, linked broad → atomic: a motivating question sits above the specific experimental, control, and analysis questions under it. This records *why* before *what*.
  - **Claims are "supported" only when backed by a dataset or analysis**; otherwise "proposed." Evidence discipline is enforced by the model, for human- and AI-authored claims alike.
- Trace one result end to end:
  - Activate the question *"Does lateral inhibition normalize PN output?"* → run a Rig2 session and capture a bench note → promote it into the committed dataset `2025_12_10_Rig2_session001.nwb` with a provenance manifest → record the *divisive-norm fit* linked to that dataset and question → make the supported claim *"background odor scales PN gain ~0.6× (n=18)"* → attach **Fig 3b**.
  - Then traverse backward: from Fig 3b to every dataset, note, and decision behind it. The figure is not just an output — it is evidence for a claim about a question.

## 5. Capture keeps it current — *2 min*

- This is the mechanism that lowers the upkeep cost. At `/app/capture`: capture photo / voice / photo+voice / text → the system builds a project-scoped context packet → the model returns reviewable draft operations → the user edits, accepts, rejects, or defers → committed through the same validation as any manual write.
- Upkeep becomes the model drafting the structured entry from a photo or voice note; the person's job is to approve it.
- Guarantees: nothing auto-commits; drafts referencing unknown entities are rejected; the human is always the gate.

## 6. The AI-harness payoff — *1.5 min*

- **Retrieval:** agents pull bounded decision context through the MCP server before research-facing work — selecting variables to plot, analyses to run, figure legends, manuscript text — grounded in the lab's real questions, datasets, and claims.
- **On-target:** because the graph encodes the broad → atomic question hierarchy, the harness keeps suggestions tied to the actual scientific goal. Generic retrieval over files does not do this.
- **Write-back** is the same human-gated capture from §5 — proposals only, validated, human-approved — and it is also what maintains the graph.

## 7. Try it — *1 min*

- One concrete ask: pick one active project and log its questions. App URL (`/app`); who to contact.
- *Not in v1* (one line): OCR on uploads, automatic question extraction, semantic/vector search, PI approval gates — deferred to ship the durable core first.

## 8. Discussion / Q&A — *remaining time*

- Prompts: which project to pilot; what capture friction would block adoption; for AI users, what context they re-paste most.
- Objection prep:
  - *vs. an ELN / Benchling / shared drive* — questions-first, evidence-linked claims, and an agent-readable graph, not file storage.
  - *Won't it go stale like past attempts* — that is the upkeep problem; AI-assisted capture is the specific mechanism that lowers it.
  - *Don't use / trust AI* — the human benefit is unconditional; AI is opt-in and always human-gated.

---

**Timing:** ~13–14 min core leaves buffer to land at 15. Cut §6 to a single sentence if running long; never cut §4 (the worked example) or §7 (the ask).
