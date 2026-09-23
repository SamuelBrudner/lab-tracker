# Advisory: SQLite upgrades could delete AI-draft provenance (September 2026)

**Affected:** Lab Tracker deployments that use **SQLite** and ran `alembic upgrade head`
(directly, via `lab-tracker serve`, `lab-tracker seed`, or a container entrypoint) with code
from commit `1fc76cc` (26 July 2026) up to the release containing the fix for review
finding C1 (docs/runs/deep-review-2026-09-21.md).

**Not affected:** PostgreSQL deployments. PostgreSQL applies these schema changes with
`ALTER TABLE` and never drops and recreates the parent tables.

## What happened

SQLite cannot alter most constraints in place, so Alembic "batch" migrations rebuild a
table: create `_alembic_tmp_<table>`, copy the rows, `DROP TABLE <table>`, then rename the
copy. From commit `1fc76cc` on, the migration connection ran with `PRAGMA foreign_keys=ON`.
With foreign keys enforced, SQLite's `DROP TABLE` deletes every row first. That delete
fired `ON DELETE CASCADE` and `ON DELETE SET NULL` on every table that references the one
being rebuilt. The copied parent rows survived, but their children did not.

Some older migrations switched foreign keys off for their own duration. SQLite ignores
that pragma inside a transaction, so the switch back on never took effect. As a result,
the first such migration in a run left enforcement off for the rest of that run. So
whether you lost data depends on the revision your database was at when you upgraded.

| Database revision before the upgrade | Rebuild that ran with enforcement on | Data lost |
| --- | --- | --- |
| `0056_claim_confidence_bounds` to `0060_acquisition_collections` (servers last started between about 25 July and 13 August 2026), upgraded to `0061` or later | `0061` rebuilds `graph_change_sets` | **All `graph_change_operations` rows** (every AI-draft proposal with its `acceptance_mode` / `accepted_by` provenance). **`change_set_id` set to NULL** on `questions`, `notes`, `datasets`, `sessions`, `analyses`, `claims`, `goals`, `visualizations`, `experiments`, `exploration_nodes`, `entity_versions`, `graph_draft_batch_runs` and `review_email_outbox` |
| `0026` to `0029` | `0030` rebuilds `goals` | `goal_links` |
| `0026` to `0034` | `0035` rebuilds `claims`, `visualizations` and the origin tables | `question_parents`, `claim_analyses`, `claim_questions`, `visualizations`, `visualization_claims`, `note_targets`, `graph_change_sets` and other children of the rebuilt tables. If a dataset referenced a question, the upgrade instead failed with `FOREIGN KEY constraint failed` and left a `_alembic_tmp_claims` table behind |
| `0001` to `0007` | `0008` rebuilds `projects` | `questions`, `notes` and other project children |

We reproduced the losses from `0008`, `0030`, `0035` and `0061`. By the same mechanism,
`0013` and `0016` (databases that started between `0008` and `0015`) and `0027`
(databases at `0026`) probably also lost data. Databases at `0055` or earlier that
upgraded straight to head lost nothing to `0061`, because `0056` switched enforcement off
for the rest of the run.
Those runs could still hit the older rows above if they started low enough.

The only upgrades that matter are those run with code from 26 July 2026 onward. Earlier
code ran migrations with foreign keys off. That was SQLite's default at the time.

## The fix

SQLite migrations now:

- switch foreign keys off on the migration connection before any transaction begins, and
  check that the pragma actually took effect;
- run the whole upgrade inside a single `BEGIN IMMEDIATE` transaction, so a failing
  migration rolls back its schema changes, any `_alembic_tmp_*` table and the version
  stamp together;
- run `PRAGMA foreign_key_check` before committing, and refuse to commit (rolling back
  the entire run) if the run left a row pointing at a missing parent that was not
  already orphaned before it started;
- log a warning, but still commit, when rows were already orphaned before the run. See
  [If an upgrade warns about foreign-key violations](#if-an-upgrade-warns-about-foreign-key-violations);
- refuse to start if an `_alembic_tmp_*` table from an earlier failed run is still
  present.

The app's own database connections still enforce foreign keys.

Upgrade to the fixed release **before** you next restart a SQLite server that is
still at `0056`–`0060`.

## How to check whether your database lost rows

1. Stop Lab Tracker. Copy the live database file (plus any `-wal` / `-shm` files)
   somewhere safe before doing anything else.
2. Find the snapshot taken just before the damaging upgrade. `lab-tracker serve` takes
   one before every migration run. Snapshots are written to `LAB_TRACKER_BACKUP_PATH`
   (default `~/.lab-tracker/backups`) and named `<db-name>.backup-<UTC timestamp>.sqlite3`.
   Only the newest `LAB_TRACKER_BACKUP_KEEP` (default 10) are kept, so each restart since
   the upgrade pushed older snapshots out. Container deployments that run
   `alembic upgrade head` from the entrypoint take no automatic snapshot. For those, use
   your own volume backups. For each snapshot, check its revision:

   ```sh
   sqlite3 BACKUP.sqlite3 "SELECT version_num FROM alembic_version;"
   ```

   The newest snapshot still at `0056`–`0060` is your pre-upgrade copy for the `0061`
   case. For older cases, use the newest one at the revision in the table above.
3. Compare the tables at risk between that snapshot and the live database:

   ```sh
   for db in BACKUP.sqlite3 LIVE.db; do
     echo "== $db"
     sqlite3 "$db" "
       SELECT 'graph_change_operations', COUNT(*) FROM graph_change_operations;
       SELECT 'questions.change_set_id', COUNT(change_set_id) FROM questions;
       SELECT 'notes.change_set_id', COUNT(change_set_id) FROM notes;
       SELECT 'entity_versions.change_set_id', COUNT(change_set_id) FROM entity_versions;
       SELECT 'goal_links', COUNT(*) FROM goal_links;
       SELECT 'claim_questions', COUNT(*) FROM claim_questions;
       SELECT 'note_targets', COUNT(*) FROM note_targets;"
   done
   ```

   A live count lower than the snapshot's (after allowing for deliberate deletions since
   then) means rows were lost. Without a snapshot, one strong sign is change sets that have
   no operations:

   ```sql
   SELECT COUNT(*) FROM graph_change_sets
   WHERE change_set_id NOT IN (SELECT change_set_id FROM graph_change_operations);
   ```

## How to restore

Install the fixed release first, so that re-running the upgrade is safe.

**If nothing important was recorded since the upgrade**, restore the pre-upgrade snapshot
and let the fixed release migrate it:

```sh
lab-tracker restore /path/to/BACKUP.sqlite3 --force   # with the server stopped
lab-tracker serve                                     # snapshots, then upgrades safely
```

**If you have work to keep from after the upgrade**, recover the lost rows into the live
database instead:

1. Restore the snapshot into a scratch database and upgrade it with the fixed release:

   ```sh
   lab-tracker restore /path/to/BACKUP.sqlite3 --database-url sqlite:////tmp/recovered.db
   LAB_TRACKER_DATABASE_URL=sqlite:////tmp/recovered.db alembic upgrade head
   ```

   Run `alembic` from a checkout of the fixed release, because it needs the repository's
   `alembic.ini`. Alternatively, start `lab-tracker serve` with
   `LAB_TRACKER_DATABASE_URL=sqlite:////tmp/recovered.db`, `--no-browser` and a spare
   `--port`, and stop it once it has started.
2. With the server stopped and the live database copied aside, copy the missing rows
   back. Both databases are now at the same head revision, so their column layouts match.
   Confirm with `PRAGMA table_info(graph_change_operations);` on each before using
   `SELECT *`.

   ```sql
   -- sqlite3 LIVE.db
   PRAGMA foreign_keys = ON;
   ATTACH '/tmp/recovered.db' AS recovered;
   BEGIN;
   INSERT INTO graph_change_operations
   SELECT * FROM recovered.graph_change_operations AS r
   WHERE r.operation_id NOT IN (SELECT operation_id FROM main.graph_change_operations)
     AND r.change_set_id IN (SELECT change_set_id FROM main.graph_change_sets);

   -- Repeat for each backlink table:
   -- analyses(analysis_id), claims(claim_id), datasets(dataset_id),
   -- entity_versions(version_id), experiments(experiment_id),
   -- exploration_nodes(node_id), goals(goal_id), graph_draft_batch_runs(run_id),
   -- notes(note_id), review_email_outbox(delivery_id), sessions(session_id),
   -- visualizations(viz_id).
   UPDATE questions SET change_set_id = (
       SELECT r.change_set_id FROM recovered.questions AS r
       WHERE r.question_id = questions.question_id)
   WHERE change_set_id IS NULL
     AND question_id IN (
       SELECT r.question_id FROM recovered.questions AS r
       WHERE r.change_set_id IN (SELECT change_set_id FROM main.graph_change_sets));

   PRAGMA foreign_key_check;   -- must return no rows before you commit
   COMMIT;
   ```

   The `WHERE change_set_id IS NULL` guard leaves alone any backlink set since the
   upgrade. Recover the older cases (`goal_links`, `claim_*`, `note_targets`, and so on)
   the same way: insert rows that are missing by primary key, and only where their
   parents still exist.
3. Start the fixed release and spot-check draft provenance in the review UI.

If `alembic upgrade head` now refuses to run because an `_alembic_tmp_*` table is present,
an earlier upgrade failed part-way. The safest fix is to stop Lab Tracker, restore the
snapshot taken before that upgrade, and upgrade again:

```sh
lab-tracker restore /path/to/BACKUP.sqlite3 --force   # with the server stopped
lab-tracker serve
```

If instead you have confirmed that the original table (the name without the
`_alembic_tmp_` prefix) still exists with all of its rows, drop the leftover table and
re-run the upgrade.

## If an upgrade warns about foreign-key violations

Some rows may already point at a parent that does not exist when an upgrade starts. The
upgrade still commits, because it did not create those rows. It logs a warning that names
each child → parent table pair and its row count, for example:

```text
SQLite database has 2 foreign-key violation(s) that predate this migration run
(goal_links -> goals: 2 row(s)). ...
```

Such rows date from before Lab Tracker enforced SQLite foreign keys, or from one of the
damaging upgrades above. They stay in place, but any write that touches them can fail with
`FOREIGN KEY constraint failed`. To list them, run this against the live database:

```sql
PRAGMA foreign_key_check;   -- child table, rowid, parent table, constraint index
```

If the parents were lost in a damaging upgrade, recover them as described in
[How to restore](#how-to-restore). Otherwise, stop the server and copy the database aside.
Then delete each orphan, or point it at a valid parent. Where the column allows NULL, you
can instead set it to NULL.

Foreign-key violations stop an upgrade only when the run itself would leave new ones. The
upgrade then rolls back and leaves the database at its previous revision, still usable. The
error names the affected tables. The cause is a migration that lost parent rows or wrote a
dangling reference, or a new constraint that rows already in the database do not satisfy.
