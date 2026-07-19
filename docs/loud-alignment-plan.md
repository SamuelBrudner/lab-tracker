# LOUD Alignment Plan — provenance that is Linked *and* Usable

This plan applies the LOUD principles ("Linked Open Usable Data", Rob
Sanderson's design principles behind Linked Art and Yale's LUX platform) to Lab
Tracker's provenance surface. The five principles, paraphrased:

1. The **right abstraction** for the audience.
2. **Few barriers** to entry.
3. **Comprehensible by introspection** — the data explains itself.
4. **Documentation with working examples.**
5. **Few exceptions**, many consistent patterns.

## Where we already stand

The premise of the export design ([`provenance-export.md`](provenance-export.md))
is LOUD's premise: the reasoning should ride next to the data as a plain,
self-contained file that outlives the app. Much of the surface is already
right:

- PROV-O in JSON-LD with typed entities, activities, and agents; supervision
  expressed as `prov:actedOnBehalfOf`; AI drafting attributed with provider,
  model, and prompt version (`src/lab_tracker/provenance.py`).
- Identifiers are HTTP URIs that double as API URLs (`_resource_iri`).
- A schema self-description endpoint for agents (`GET /schema/describe`).
- One envelope shape, one error shape, one pagination shape across the REST
  API (`src/lab_tracker/schemas.py`).
- A no-login read-only demo and one-click deploy — few barriers for humans.

The gaps below are polish on that foundation, not a rewrite. They are ordered
by leverage; each phase is independently shippable.

> **Status: implemented.** All six phases shipped (epic `lab-tracker-oh60`).
> The sections below are kept as the design rationale; the resulting surface
> is summarized in [retained-v1-surface.md](retained-v1-surface.md) and
> documented in [provenance-export.md](provenance-export.md).

## Phase 1 — Stable canonical identifiers

**Gap.** IRIs are minted from the incoming request's base URL
(`_request_base_url` in `src/lab_tracker/routes/provenance.py`) or whatever
base the CLI was pointed at. The same dataset is
`http://127.0.0.1:8000/datasets/…` locally, `http://192.168.1.20:8000/…` on
the LAN, and a third thing when hosted — and sidecar files freeze whichever
base was in effect at export time. A linked-data identifier is a *name* you
commit to, not the address you happened to fetch it from. Two exports of the
same project from different hosts currently describe formally different
entities.

**Plan.**

- Add a `LAB_TRACKER_CANONICAL_BASE_URL` setting (documented in
  [`configuration.md`](configuration.md)). When set, every `@id` in
  provenance documents and sidecars uses it, regardless of serving host.
- When unset, fall back to the current request-base behavior (zero-config
  local use keeps working), but `lt export` prints a one-line warning that
  identifiers are host-relative.
- Document the identifier policy in `provenance-export.md`: what is stable,
  what a lab should set before its first archived export.

**Done when** two exports of one project, taken through different hosts with
the setting configured, produce byte-identical `@id`s.

## Phase 2 — A vocabulary that dereferences

**Gap.** The `@context` maps ~100 custom terms into `lab:` =
`{base}/terms#`, but no `/terms` route exists — every custom term 404s.
Someone opening a sidecar in ten years cannot look up what
`lab:falsificationCriteria` means, which defeats "comprehensible by
introspection" at exactly the layer the export exists to serve.

**Plan.**

- Serve `GET /terms` from a single source of truth: a term registry (term →
  definition, range, which entity types emit it) that also *generates* the
  `@context` in `provenance.py`, so the two cannot drift.
- Content-negotiate: HTML for people, JSON-LD for machines.
- Add a test asserting every term used by any provenance builder appears in
  the registry with a non-empty definition.

**Done when** every `lab:` IRI in an exported sidecar resolves to a
human-readable definition.

## Phase 3 — Collapse duplicate terms; reuse standard vocabulary

**Gap.** Two "few exceptions" violations in the current context
(`src/lab_tracker/provenance.py`):

- Near-duplicate private terms: `fileName` / `filename` / `filePath`,
  `checksum` / `sha256`, `contentSize` / `sizeBytes`. Every consumer must
  check both spellings.
- Terms minted in the private namespace that standard vocabularies already
  define: `contentUrl`, `contentSize`, `encodingFormat`, `contentType` are
  schema.org terms; `lab:createdAt` / `lab:generatedAt` shadow
  `dcterms:created` / `prov:generatedAtTime`.

**Plan.**

- Pick one spelling per concept; keep the losing spellings in the `@context`
  as deprecated aliases mapping to the *same* IRI for one release, then drop
  them from emission (old sidecars stay interpretable because their frozen
  contexts still define the old keys).
- Re-map lookalike terms to `schema:` / `dcterms:` / `prov:` equivalents.
  Reserve `lab:` for genuinely novel research-record concepts —
  `falsificationCriteria`, `questionLink`, `refutingOutcome`,
  `curationState` are the vocabulary worth owning.

**Done when** the context contains one term per concept and no `lab:` term
duplicates a schema.org, Dublin Core, or PROV term.

## Phase 4 — One key style inside documents

**Gap.** The context aliases `wasGeneratedBy`, `wasAttributedTo`, `used`,
`wasDerivedFrom` to bare keys, but the builders emit the prefixed forms
(`"prov:wasGeneratedBy"`, `"prov:used"`). Documents mix prefixed and bare
keys; the Linked Art rule is that the JSON should read naturally *as JSON* —
no colons in keys.

**Plan.** Emit the aliased bare keys everywhere; keep the aliases in the
context (they already exist). Add a test that no emitted key contains `:`.
Mechanical change, low risk — JSON-LD expansion is unchanged.

## Phase 5 — JSON-LD where the identifier points

**Gap.** A sidecar's `@id` dereferences to `GET /datasets/{id}`, but that
returns the plain envelope shape — a different document, with no content
negotiation connecting the two. References in REST responses are naked UUIDs
with no type or link, so a client cannot follow its nose.

**Plan (incremental, not a REST rewrite).**

- Honor `Accept: application/ld+json` on the single-resource GETs for the
  entity types that have provenance builders (datasets, analyses, claims),
  returning the same document as the `/provenance` route. The `/provenance`
  paths remain as stable aliases.
- Add the canonical URI to single-resource envelope responses (an `iri`
  field alongside the UUID) so plain-JSON clients can cross the bridge too.
- Explicitly out of scope: converting list endpoints or write paths to
  JSON-LD; the envelope API remains the right abstraction for the app and
  its agents.

## Phase 6 — A committed, working example

**Gap.** No example sidecar exists anywhere in the repo; `provenance-export.md`
describes the format but never shows it. IIIF and Linked Art ship a complete
copy-pasteable instance for every format — it is the cheapest, highest-leverage
documentation there is.

**Plan.**

- Commit a real sidecar from the demo seed as
  `docs/examples/dataset.prov.jsonld`, plus an annotated walkthrough of its
  `@graph` in `provenance-export.md`.
- Add a test that regenerates the example from the demo seed and fails if it
  drifts, and that the document expands cleanly with a JSON-LD processor
  (`pyld`) — the example stays working by construction.

## Sequencing and effort

| Phase | Size | Depends on |
| --- | --- | --- |
| 1. Canonical identifiers | S | — |
| 2. `/terms` dereferences | M | — |
| 3. Term cleanup / reuse | M | 2 (registry is the natural place) |
| 4. One key style | S | — |
| 5. Content negotiation | M | 1 |
| 6. Committed example | S | best after 3–4, valuable even now |

Phases 1, 4, and 6 are each roughly a day and remove the sharpest critiques;
2–3 are the substantive vocabulary work; 5 can trail.

## Non-goals

- Adopting Linked Art or CIDOC-CRM wholesale — our domain is research
  reasoning, not cultural heritage objects; PROV-O plus a small documented
  vocabulary is the right abstraction.
- Making JSON-LD the primary API. The envelope API stays; LOUD applies to
  the provenance/export surface, where longevity is the point.
- Renaming identifiers in sidecars already exported by labs. Old exports
  remain valid; Phase 1 is about stability going forward.
