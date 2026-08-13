# Eval-gated hybrid GraphRAG

Lab Tracker uses one typed project graph. Retrieval first selects relevant seed
nodes, then expands those seeds through the same deterministic relationships
used by project graph reads. Semantic retrieval does not introduce a second
graph and never changes canonical records.

## Runtime policy

`LAB_TRACKER_SEMANTIC_SEARCH_MODE` is the single operational switch:

| Server mode | `auto` | Explicit `hybrid` | Indexing |
| --- | --- | --- | --- |
| `off` (default) | lexical | lexical fallback | off |
| `shadow` | compute both, serve lexical | serve lexical | on |
| `hybrid` | hybrid when ready | hybrid when ready | on |

Explicit `lexical` never computes an embedding. Shadow mode cannot be bypassed.
Hybrid serving requires a current injected adapter and at least 99% current
project coverage; every other state degrades to lexical results with stable
metadata. Query embedding happens only after project authorization.

This initiative ships no real adapter, credentials, provider SDK, or model
download. Runtime therefore remains lexical by default. A future adapter must
document data egress, pass the synthetic and private replay gates, demonstrate
99% coverage, p95 indexing lag under 60 seconds, and two-second query fallback
before an operator changes the server mode.

## Documents and indexing

`GraphNodeDocumentRenderer` produces versioned summaries, priority-ordered
lexical fields, normalized intrinsic semantic text, deterministic excerpts, and
hashes. Semantic text uses NFC, LF line endings, stable labels, and sorted
structured keys. It excludes neighboring records, resolved artifact bytes, raw
uploads, credentials, identifiers, checksums, paths, and unbounded manifests.
Every natural-language semantic field is also lexical-searchable.

Semantic text is paragraph-aware chunked at 4,000 characters with 400-character
overlap, capped at eight chunks while retaining the final tail. Portable
little-endian normalized float32 vectors are stored as rebuildable derivatives;
source text is not duplicated. Canonical transactions only coalesce jobs. The
worker renders and embeds outside long transactions and generation-checks before
replacement. Reconciliation supplies backfill, invalidation, orphan removal,
and stale-lease recovery.

## Ranking and graph context

Exact IDs and exact human titles remain pinned. Other lexical and semantic ranks
use equal-weight reciprocal-rank fusion with `k=60`; raw similarity values are
not exposed. Candidate pools use
`min(500, max(100, 5 * (offset + limit + 1)))` and report truncation.

Decision context orders explicit anchors, up to eight fused query seeds, one
shared depth-two typed traversal (50 nodes, 100 edges), then recency fill. It
records one deterministic shortest path per expanded node and caps content at
8,000 characters per anchor, 2,000 per seed, 1,200 per neighbor, and 40,000
overall.

## Evaluation gate

[`retrieval-eval/v1`](../retrieval-eval/v1/README.md) contains 40 synthetic,
deterministic cases with fixed seed and traversal budgets. It compares current
decision context, ranked lexical seeds, lexical plus graph expansion, semantic
plus the same expansion, and hybrid RRF plus the same expansion.

Production semantic work proceeds only if a non-shipped vector artifact shows:

- at least +10 percentage points macro typed-path recall over lexical + graph;
- at least +20 points for zero-token-overlap cases;
- 100% top-1 retention for exact IDs and titles;
- no greater than 0.02 overall nDCG@10 regression; and
- no new curated contradiction or hard-negative top-10 regression.

Private real-project replay stays local. Only aggregate reports containing the
corpus hash, renderer/chunker versions, descriptor, dimensions, and metrics may
be exported.
