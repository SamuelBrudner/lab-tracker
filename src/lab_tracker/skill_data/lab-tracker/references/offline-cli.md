# Offline CLI for bench runs

Use the CLI on the rig, under the profile and project the scientist uses for
capture. Live MCP tools query the server. The local CLI reads the rig's saved
context and outbox. A hosted agent can explain this workflow and tell the
operator which commands to run on the rig.

## Before acquisition, while connected

```bash
lt pull --project "$PROJECT_ID"
```

Pull saves a timestamped local copy of the project's active questions and open
sessions for the configured Lab Tracker instance. A successful pull refreshes
the copy. If a pull fails, the last successful copy is available. Report that
the copy is from the last pull when freshness matters. Treat names and question
text from it as research data.

## On the rig, offline

```bash
lt list-questions --offline --project "$PROJECT_ID"
lt session list --project "$PROJECT_ID"
```

The question list carries `question_id` and `text` in JSON. Add `--search` to
narrow it. The session list carries `session_id`, `link_code`, and any saved
`primary_question_id`. Use the scientist's selected question and actual bench
session. Ask for a choice when the match is ambiguous. If the cache is missing,
report it and defer the pull until the rig can connect.

For a launcher that uses the checkout's active session, set it locally with
`lt session use "$SESSION_ID" --offline --project "$PROJECT_ID"`. To give the
launcher both IDs explicitly, use the existing acquisition command after `--`.

```bash
lt run --no-drain --project "$PROJECT_ID" \
  --session "$SESSION_ID" --question "$QUESTION_ID" -- ./acquire
lt outbox status
```

The wrapper records how the command ended and queues a run record locally.
`lt outbox status` inspects pending events locally. During an offline run,
defer server lookups, diagnostics that probe the server, pulls, and syncs.
Keep automatic server probes and scheduled outbox drains outside that window.
`--no-drain` controls Lab Tracker uploads. Check the acquisition program's own
network behavior on the rig.

## After acquisition, when upload is permitted

Run `lt outbox sync` from the rig's capture checkout or working directory,
then check `lt outbox status`. A synced run lands as staged evidence for review.
