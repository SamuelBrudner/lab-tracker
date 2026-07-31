# Provenance export: reasoning that outlives the app

The durable artifact in a lab is the data file — a `.nwb` will still open in ten
years. A running Lab Tracker instance is far less durable. So the reasoning
*about* the data should be able to ride next to the data as a readable file,
independent of any server.

`lt export` writes the PROV-O/JSON-LD provenance of a project's committed
records to self-contained sidecar files. Each sidecar is plain JSON-LD: open it
in any text editor or JSON-LD tool with no Lab Tracker instance running.

## Usage

```bash
lt export --project <PROJECT_ID> --out ./lab-tracker-export
```

This writes one `*.prov.jsonld` file per committed dataset, analysis, and
supported claim into the output directory, plus a printed summary of counts and
files.

### Co-locate sidecars next to the data

The server stores only *logical* file paths, not where your data physically
lives — the consumer machine knows that. Point `--data-root` at the directory
your dataset paths are relative to, and each dataset's sidecar is also written
next to the resolved data file:

```bash
lt export --project <PROJECT_ID> --data-root /data/rig2 --out ./lab-tracker-export
```

Given a dataset file logical path `raw/session001.nwb`, the reasoning lands at
`/data/rig2/raw/dataset-<id>.prov.jsonld`, beside the `.nwb` it explains.

### Window the analyses

For a progress-report-style slice, bound the analyses by commit time:

```bash
lt export --project <PROJECT_ID> --since 2025-07-01T00:00:00+00:00
```

`--since` is inclusive and `--until` is exclusive. Datasets and claims are
exported in full; the window applies to analyses.

## Identifier policy

Every node in a sidecar carries an `@id` — an HTTP URI that names the dataset,
analysis, claim, person, or activity it describes. A linked-data identifier is
a *name* your lab commits to, not the address a request happened to arrive on,
so it must not change when the serving host does.

By default, identifiers are rooted at whatever base URL served the request:
`http://127.0.0.1:8000/datasets/…` locally, your LAN IP over the LAN, and so
on. Two exports of the same project through different hosts would then name
the same records differently.

**Before your first archived export, set `LAB_TRACKER_BASE_URL`**
(see [configuration.md](configuration.md)) to the URL your lab commits to
long-term. From then on every provenance `@id` uses that root, byte-identical
regardless of which host, port, or proxy served the request. `lt export`
prints a note when the identifiers it writes are rooted at the URL it happened
to connect to, as a reminder to configure the canonical base before archiving.

## What's inside a sidecar

The documents are produced by the same builders that back the
`GET /datasets/{id}/provenance`, `GET /analyses/{id}/provenance`, and
`GET /claims/{id}/provenance` endpoints — PROV-O entities and activities in
JSON-LD, including content hashes, commit hashes, and the semantic edges back to
the questions each record answers.

The normative class, controlled-concept, and qualified-edge choices are fixed
by the [Lab Tracker public semantic profile](semantic-profile.md).

The identifiers dereference: `GET` on a dataset, analysis, or claim URI with
`Accept: application/ld+json` returns this same document, so a JSON-LD
consumer can follow any `@id` in a sidecar straight to the live record. The
plain-JSON envelope for those records carries the canonical URI as
`meta.iri`, bridging the two representations from the other side.

## A worked example

A complete dataset sidecar is committed at
[`examples/dataset.prov.jsonld`](examples/dataset.prov.jsonld) — paste it into
the [JSON-LD Playground](https://json-ld.org/playground/) as-is. It is
generated from a fixed fixture through the same builder as the live endpoints,
and a test regenerates it on every run, so it can never drift from what the
code produces. Reading its `@graph` top to bottom:

- **The dataset** (`…/datasets/6fce1866-…`) is a `prov:Entity`: its
  `commitHash` fixes the manifest, `wasGeneratedBy` points at the commit
  activity, and `wasAttributedTo` names the person who committed it. Because
  this record was AI-proposed and human-accepted, `origin` is
  `ai_suggested` and `wasGeneratedBy` *also* lists the drafting change set.
- **The commit activity** (`…/provenance/commit`) `used` the two raw files,
  carries the manifest metadata (including the NWB block), links the
  research `note` and the acquisition `sourceSession`, and holds a
  `questionLink` per question the data addressed.
- **The drafting activity and its software agent** record which provider,
  model, and prompt version proposed the record — `wasAssociatedWith`
  connects them. The proposal is provenance; the person is the one who
  committed.
- **The question links** say what the data *meant*: the primary question was
  `supports`, the secondary `inconclusive`.
- **The file entities** each carry `filePath`, `checksum`, and
  `contentSize` — enough to re-identify the bytes they describe.
- **The people** are `prov:Person` agents; `actedOnBehalfOf` states the
  supervision active when the work happened, with its start date.

Every key in a document reads as plain JSON (no prefixes) and is declared in
one vocabulary registry (`src/lab_tracker/vocabulary.py`): keys map to
standard PROV-O, schema.org, and Dublin Core IRIs where those exist, and to
the `lab:` namespace for research-record concepts of our own (falsification
criteria, question links, curation origin). Every `lab:` IRI dereferences:
`GET /terms` on the instance serves the vocabulary as a human-readable page,
or as JSON-LD with `Accept: application/ld+json`.
