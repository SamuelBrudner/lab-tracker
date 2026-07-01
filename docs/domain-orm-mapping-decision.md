# Decision: Domain ↔ ORM mapping layer

**Status:** Proposed · **Date:** 2026-07-01 · **Tracking:** `bd` issue `lab-tracker-t4x0.2`
(epic `lab-tracker-t4x0`, "Reduce architectural complexity above functional need")

Related: [`internal-boundaries.md`](internal-boundaries.md) (layer diagram),
[`retained-v1-surface.md`](retained-v1-surface.md) ("prefer direct
repository-backed operations over speculative abstractions").

## Context

Every retained entity is represented three times:

| Layer | File | Size | Role |
| --- | --- | --- | --- |
| Domain model | `models.py` | 1260 | Pydantic types the app reasons about (nested, typed, `frozen`) |
| ORM row | `db_models.py` | 1649 | SQLAlchemy tables (flat columns) |
| Mapper | `sqlalchemy_mappers.py` | 1520 | ~76 hand-written translators bridging the two |
| (API request schemas) | `schemas.py` | 928 | request payloads; responses reuse domain models |

The complexity evaluation flagged this triple representation as the largest
structural multiplier on line count: adding one field to an entity touches
**~4 files**. Tracing `Dataset.terminal_reason` confirms it appears in
`models.py`, `db_models.py`, `sqlalchemy_mappers.py`, and `schemas.py`. The
question this ADR settles: **is the mapping layer worth keeping, and if so how
do we cut its cost?**

### What the mapper actually does (measured)

Reading the mapper (e.g. `dataset_to_model` / `dataset_from_model` /
`apply_dataset_to_model`, `sqlalchemy_mappers.py:406`) shows it is **not**
boilerplate that `from_attributes` could replace. It bridges a real
domain/persistence impedance mismatch:

- **Nested value objects ↔ flat columns** — the domain `Dataset` has a nested
  `commit_manifest`; the ORM flattens it into `manifest_files`,
  `manifest_metadata`, `manifest_note_ids`, … columns.
- **Child-row assembly** — the ORM defines **zero `relationship()`s**
  (verified), so the mapper assembles collections (question links, note
  targets, acquisition outputs) from separately-loaded rows.
- **Legacy compatibility** — e.g. `_legacy_external_artifacts_from_metadata`
  reads artifacts from old `metadata` shapes.

That part earns its keep. But the **bulk of the mapper is accidental**, driven
by primitive column-type choices rather than by shape divergence. Counts across
`sqlalchemy_mappers.py`:

| Manual conversion | Count | Root cause |
| --- | ---: | --- |
| `_uuid()` / `_uuid_str()` | **222** | all IDs are `String(36)` columns — `Uuid` columns: **0** |
| enum `.value` + `Status(...)` construction | **86** | enums stored as `String` — `Enum` columns: **0** |
| `_as_utc()` datetime coercion | **38** | naive/tz handling done by hand on every read |
| JSON (de)serialize helpers | **23** | 30 JSON columns hand-encoded to/from typed lists |

So **~300+ of the mapper's hand-written conversions exist only because the ORM
stores UUIDs, enums, and timestamps as raw primitives** and re-derives the typed
form on every crossing. That is not impedance mismatch — it is a missing
type-adaptation layer.

`models.py` already sets `ConfigDict(from_attributes=True)` on its domain base,
so the trivial 1:1 cases are already cheap; the mapper exists for the cases
`from_attributes` cannot express.

## Options considered

### A. Status quo — keep everything as-is
No cost, no benefit. Leaves ~300 error-prone manual conversions and the 4-file
fan-out. Rejected as the end state.

### B. Full unification (SQLModel / one class per entity)
Collapse domain + ORM into a single class so there is nothing to map.
**Rejected.** The domain and storage shapes genuinely diverge (nested
`commit_manifest` vs. flat `manifest_*` columns; typed lists vs. JSON; `frozen`
domain values vs. mutable rows). Unifying would either pollute the domain with
storage concerns or lose the nested/typed/immutable domain — the opposite of the
retained-v1 goal of keeping the semantic core clean. High effort, high risk,
negative architectural value.

### C. Rely on Pydantic `from_attributes` only
Delete the mapper, build domain models directly from ORM rows.
**Rejected.** `from_attributes` cannot assemble the nested manifest, decode JSON
columns into typed lists, synthesize child collections (no ORM relationships),
or apply legacy fallbacks. It would only remove the already-cheap trivial cases.

### D. Keep the separation; remove the *accidental* churn (recommended)
Keep domain / ORM / mapper as distinct layers, but delete the ~300 manual
conversions by pushing type adaptation into the storage layer:

1. **`TypeDecorator`s for UUID, enum, and UTC-datetime columns.** A `GUID`
   decorator backed by `String(36)` binds `UUID → str` and returns `str → UUID`;
   an enum decorator stores `.value` and returns the enum; a UTC decorator
   normalizes tz on read. Column **storage stays byte-identical** (still TEXT),
   so **no data migration is required** — only the Python-side type changes.
   This lets the ~300 `_uuid`/`.value`/`_as_utc` calls disappear and many
   `*_to_model` / `*_from_model` bodies collapse toward direct field copies.
2. **A mapping-completeness test.** Round-trip every entity through
   `to_model → from_model` and assert equality, plus assert every ORM column is
   referenced by its mapper. This makes the remaining (legitimate) fan-out
   *safe*: a field added to the ORM but forgotten in the mapper fails a test
   instead of silently dropping data.

## Decision

Adopt **Option D**. Keep the three-layer separation — it protects a clean,
persistence-independent domain, which `retained-v1-surface.md` names as the
long-term moat — and attack the *cost*, not the *structure*:

1. Introduce `TypeDecorator`s (`GUID`, enum, UTC-datetime) and migrate columns to
   them incrementally, entity by entity, with no schema/data migration.
2. Add the round-trip + column-coverage mapping-completeness test before the
   migration so each entity's conversion is proven equivalent as it changes.

Explicitly **do not** pursue SQLModel unification or a `from_attributes`-only
rewrite.

This is preferred over finishing the facade→services purity refactor: it removes
a real class of bugs (forgotten conversions) and ~300 conversions of noise,
whereas the facade work is a ~270-file change for edit-locality already largely
captured by the `api_parts/` mixin split.

## Consequences

**Positive**
- Removes ~300 manual conversions; `sqlalchemy_mappers.py` shrinks materially
  (estimate 30–40%), concentrating on the genuine nested/legacy assembly.
- Eliminates a bug class: UUID/enum type mismatches can no longer leak past a
  forgotten `_uuid()`/`.value` call — the type system and the completeness test
  catch them.
- No data migration; storage representation is unchanged, so rollout is
  reversible per entity.

**Negative / cost**
- Medium effort: 44 tables' worth of column-type swaps, done incrementally.
- Adding a *new* field still touches ~3–4 layers — the fan-out is inherent to
  keeping distinct domain/ORM/schema types. Option D makes that fan-out *safe*
  (completeness test) rather than eliminating it. A future generator could
  reduce it further but is out of scope here.
- `TypeDecorator` behavior must be validated on both SQLite (dev/test) and
  Postgres (prod) — covered by running the existing suite under both backends.

**Neutral**
- The API contract is unaffected; `schemas.py` and route envelopes do not change.

## Rollout sketch

1. Land the mapping-completeness test (green against today's code).
2. Add `lab_tracker/db_types.py` with `GUID`, `Enum`-string, and `UtcDateTime`
   `TypeDecorator`s + unit tests (bind/result round-trips on SQLite and Postgres).
3. Migrate one entity (e.g. `Session`, a simple one) end-to-end: swap its ORM
   column types, delete the now-redundant conversions from its mapper, confirm
   the completeness test + full suite stay green. Review the diff as the pattern.
4. Sweep the remaining entities, largest churn first (`Dataset`, `Note`,
   `Question`, `Analysis`, `Claim`, graph-draft rows).

## Open questions

- Postgres already has a native `UUID` type; do we want the `GUID` decorator to
  emit native `UUID` on Postgres and `String(36)` on SQLite (dialect-specific),
  or keep TEXT everywhere for simplicity? Native UUID on Postgres is a real
  schema migration and can be deferred to a later decision.
- Should enum columns adopt SQLAlchemy's native `Enum` (a DB-level constraint) or
  a plain string-backed decorator? String-backed avoids enum-alter migrations
  and matches current storage; recommended for the first pass.

## Revision — findings from the attempted sweep (2026-07-01)

Steps 1–3 of the rollout landed cleanly and are committed:

- **`t4x0.5`** mapping-completeness test (`tests/test_mapper_completeness.py`).
- **`t4x0.6`** `db_types.py` (`GUID`, `EnumType`, `UtcDateTime`) + unit tests.
- **`t4x0.7`** `Session` migrated end-to-end; full suite green.

Step 4 (the full sweep, `t4x0.8`) was attempted and then **reverted**, because it
falsified this ADR's central premise that the change is *storage-layer-local with
no call-site churn*. Migrating every entity's columns broke **~60+ call sites
across 20 files**, all because ORM id/enum attributes are consumed as **strings**
throughout the codebase, not only inside the mapper:

1. **`UUID(row.x)`** — 45 sites in `auth.py`, routes, and repository join-code.
   Once the column returns a `UUID`, `UUID(uuid)` raises. (A tolerant
   `ensure_uuid()` coercion fixes these, but they must be found.)
2. **Python-level `row.<id> == str(x)`** — 5 sites (e.g. `auth.py` PAT/device
   revoke). A loaded `UUID` never equals a `str`, so these silently invert
   (raising spurious `NotFound`). Note: SQLAlchemy `.where(Model.col == str(x))`
   query expressions are **safe** — the column type binds the value.
3. **Dicts/sets keyed by `row.<id>`** in repository join-processing
   (`analyses.py`, `exploration.py`, `graph_batches.py`, …). The fill side
   becomes `UUID`-keyed while the lookup side stays `str`-keyed, so counts/joins
   silently return empty (observed: `operation_count == 0`, default store lost).
4. **`UtcDateTime` shifts `onupdate` timing.** Returning *aware* datetimes makes a
   re-assigned `updated_at` compare equal to the loaded value, so SQLAlchemy drops
   it from the UPDATE and the `onupdate=_utc_now` fallback fires. Benign in
   production (services call `updated_at = utc_now()` before saving) but it
   changes behaviour for any caller that pins timestamps.

Enum migration itself proved **safe** — every enum is a `str, Enum`, so
`member == "value"` holds and query binds accept raw strings.

**Revised guidance:**

- This is a **coordinated cross-cutting migration**, not a mechanical column
  swap. Do it **per entity**: migrate the entity's columns, fix *its* consuming
  call sites (audit `UUID(<attr>)`, `<attr> == str(...)`, and id-keyed
  dict/set fills), clean its mapper, and confirm the full suite green — exactly
  as `Session` was done. `Session` worked precisely because nothing consumed its
  ids via those idioms in covered paths.
- Introduce a tolerant `ensure_uuid()` helper first to make the id call-sites
  safe during transition.
- **Reconsider priority.** The payoff is removing ~300 mapper conversions, but the
  cost is auditing id-consumption across auth/routes/repository. The string-id ORM
  is a working status quo; this cleanup is legitimately **lower priority** than the
  test-suite and facade wins already landed, and should only proceed entity-by-
  entity with the completeness test + full suite as the gate. A safer first target
  than a broad sweep may be to migrate only entities whose ids are *not* consumed
  outside the mapper.
