# Daily-review email alerts

Lab Tracker can send a short cue when an assigned daily review is ready. The
alert is deliberately contentless:

> A Lab Tracker review is ready.
>
> Open Lab Tracker to review it: `<signed link>`
>
> Sign-in is required. This email contains no research details.

No project name, note text, proposal summary, count, attachment, or scientific
metadata leaves the application in email.

## Delivery guarantees

The final `GRAPH_BATCH → READY` update and its recipient-scoped outbox row are
written in one database transaction. A deterministic unique key prevents the
same review from being queued twice. A worker leases one row, commits the
lease, contacts the provider, and then records provider acceptance or a
sanitized failure in a new transaction. Expired leases can be reclaimed after
a worker crash; retryable failures use exponential backoff and a bounded
attempt count.

`accepted` means the provider accepted the submission. It does not prove inbox
delivery; that would require provider delivery webhooks.

## Recipient setup

On the daily-review schedule, select a per-user schedule, enter that user's
mailbox, and enable **Email me when my review is ready**. Project-level default
rows cannot receive email because they have no recipient identity. The app
rechecks the preference and address immediately before each provider attempt,
so opting out or changing the address cancels an already queued stale
delivery.

## Signed links

Every real review alert contains a recipient- and delivery-bound HMAC token
with a short expiry. The `/r/<token>` route verifies it and redirects to the
review page. The token is not a login token and never grants project access:
the browser must still sign in, and the ordinary batch API enforces project
authorization.

## Transport choices

With `LAB_TRACKER_REVIEW_EMAIL_TRANSPORT=smtp`, the app's background worker
sends through the configured SMTP account. TLS modes and network timeouts are
explicit, failures are redacted before persistence, and retries reuse a
deterministic `Message-ID`.

With `external`, Lab Tracker owns only the outbox. In Docker Compose, invoke
the bridge inside the already-running **primary** app container so it inherits
the live Postgres configuration and persisted signing secret:

```bash
docker compose \
  -p lab-tracker \
  -f /Users/samuelbrudner/Documents/GitHub/lab-tracker/docker-compose.yml \
  exec -T app \
  python -m lab_tracker.review_email_external_worker claim
```

A bare host invocation is safe only when it is explicitly configured with the
same production database URL, public URL, email settings, and authentication
signing secret. Otherwise it may open a local SQLite database or create links
that the live app cannot verify.

The JSON response contains fixed `subject` and `text_content` fields plus an
opaque `dedupe_marker`. Before sending, the mailbox worker searches Sent Items
for that marker; if it already exists, the worker records the existing message
as accepted instead of sending again. The marker is stable across retries and
contains no graph or project identifier.

After the mailbox provider accepts the message, the worker acknowledges it
with `accepted --delivery-id ... --claim-token ... --provider-message-id ...`.
On failure it calls `failed` with the same IDs and a small non-secret error
code. This mode keeps mailbox OAuth credentials out of the Lab Tracker
container.

The root Compose file also provides a default-off, primary-only
`review-email-external` control profile. It mounts the primary app's durable
runtime secret read-only, joins only the primary network, receives no OpenAI or
mailbox credentials, and can be used when a one-shot container is preferable:

```bash
docker compose \
  -p lab-tracker \
  -f /Users/samuelbrudner/Documents/GitHub/lab-tracker/docker-compose.yml \
  --profile review-email-external \
  run --rm --no-deps review-email-control claim
```

Replace `claim` with the full `accepted ...`, `failed ...`, or `test --to ...`
argument list as needed. The explicit project and root Compose path prevent
this helper from attaching to Marion's separate `lab-tracker-marion` database
and signing secret.

Admins can enqueue a fixed, non-graph diagnostic via
`POST /review-email/test`. The diagnostic is visibly labeled as a test and
creates no graph record. The local bridge exposes the same operation without
an application password. Under Docker, run it through the primary app:

```bash
docker compose \
  -p lab-tracker \
  -f /Users/samuelbrudner/Documents/GitHub/lab-tracker/docker-compose.yml \
  exec -T app \
  python -m lab_tracker.review_email_external_worker test --to user@example.org
```

## Unattended external delivery

Do **not** schedule `claim` by itself: a claim acquires a lease and must be
followed by a Sent Items lookup plus `accepted` or `failed`. Schedule the whole
mailbox transaction on a short cadence:

1. Run the primary `claim` command above.
2. Stop when its JSON contains `"delivery": null`.
3. Search the sender's Sent Items for the exact `dedupe_marker`.
4. If found, call `accepted` with that existing provider message ID.
5. Otherwise send the fixed `subject` and `text_content` to `to`, then call
   `accepted` with the new provider message ID.
6. On a provider failure, call `failed`; add `--retryable` only for a transient
   failure.
7. Repeat until the queue is empty, with a conservative per-run cap.

The application and Compose profile deliberately cannot grant mailbox access.
The remaining external input is a mailbox identity (for example the connected
Outlook account) whose scheduled automation may search/read its Sent Items and
send messages. The automation also needs local permission to run the primary
Compose command. No SMTP password, OAuth token, or model key belongs in Lab
Tracker's Compose file.
