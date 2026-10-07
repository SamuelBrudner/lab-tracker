# Evidence authoring and question staging

Before creating evidence records, read the existing questions, datasets,
analyses, claims, visualizations, and notes for the project. Reuse existing graph
records when they already represent the source or result.

Author evidence in this order:

1. Create or reuse datasets before creating analyses.
2. Create analyses before creating supported claims or visualizations.
3. Attach source notes to the most specific relevant entity, such as a claim or
   visualization rather than only the project.
4. Use `supported` claim status only when `supported_by_dataset_ids` or
   `supported_by_analysis_ids` is present. Use `proposed` for human
   interpretation without a concrete supporting record.
5. Prefer managed visualization uploads for plot or figure assets that exist on
   disk. Keep `file_path` as a source locator when useful, then call
   `lab_tracker_upload_visualization_file` so the graph node exposes an API
   download path and checksum. For retrospective paper figures, use DOI or PDF
   locators such as `doi:10.1371/journal.pcbi.1011051#fig5` only when no local
   plot file exists.
6. Declare `origin="ai_executed"` on every record whose text you wrote; leave
   the default `user` for content the person dictated verbatim.
   `lab_tracker_record_evidence_bundle` applies its `origin` to every component
   it creates. A `stage_evidence` token can only preview a bundle
   (`dry_run=true`); committing one needs an all-scope writable token.
7. Verify the final graph with list tools.

For retrospective literature evidence, staged datasets are acceptable
placeholders for source collections such as dissertation analyses or
published-recording sets. Prefer real method hashes and code versions when
available; otherwise use stable publication labels such as
`publication:eLife-2021-vae-feature-space` and
`published-pdf:elife-67855-v2`.

Before research-facing decisions, use `lab_tracker_get_decision_context` when
available. This includes choosing variables to plot, analyses to run, figures or
slides to make, experimental controls to prioritize, summaries to write, and
research writing such as manuscripts, grants, abstracts, results, discussion
text, and figure legends. If Lab Tracker is unavailable or ambiguous, state that
explicitly before proceeding.

For MCP clients on other computers, point `LAB_TRACKER_BASE_URL` at the serving
machine, preferably its durable HTTPS origin. Same-tailnet or
LAN-only clients can also use `http://<host-ip>:8000` when the server is
explicitly bound for LAN serving. Use `docs/lan-shared-graph.md` for the
current serving modes.

## Question Staging Workflow

Use Lab Tracker as the question/reasoning layer, not the execution task tracker.
For new projects or newly imported repo context:

1. Create or find the project.
2. Draft candidate question hierarchies with `status: "staged"`.
3. Read back the staged queue with `lab_tracker_list_questions status="staged"`
   so the user can review wording, hierarchy, type, and hypothesis.
4. Activate only approved questions through the app or API
   `PATCH /questions/{question_id}` with `{"status": "active"}`.
5. Keep concrete execution tasks in the repo issue tracker when the repo has one;
   link their results back as notes, analyses, datasets, or conclusions.

Question status transitions are one-way for review: `staged` can become
`active` or `abandoned`, but `active` cannot return to `staged`. A question
becomes `superseded` only through `POST /questions/{question_id}/refactor`, and
a new question starts as `staged`, `active`, or `abandoned`.
