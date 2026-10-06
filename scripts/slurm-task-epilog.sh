#!/bin/sh
# Lab Tracker Slurm TaskEpilog: finish `lt hpc submit` runs with no job-script edits.
#
# Install (cluster administrators), once:
#
#     install -m 0755 slurm-task-epilog.sh /etc/slurm/lab-tracker-task-epilog.sh
#     # slurm.conf on every node:
#     TaskEpilog=/etc/slurm/lab-tracker-task-epilog.sh
#     scontrol reconfigure
#
# (If the site already has a TaskEpilog, call this script from it instead.)
#
# slurmstepd runs a TaskEpilog as the *job user*, in the job's environment,
# after each task of each job step ends. This script acts only for the batch
# step of jobs submitted through `lt hpc submit` -- their run manifest sits in
# the submit directory -- and runs `lt hpc epilog --fail-silent`, which writes
# the run's finish event to the user's own outbox unless the job already
# finished the run itself. It never contacts Lab Tracker, never reads or runs
# anything as root, never changes the job's outcome, and always exits 0.
#
# Knobs (environment):
#   LAB_TRACKER_HPC_EPILOG_ENABLED=0  turn the epilog off for a job or site
#   LAB_TRACKER_LT=/path/to/lt         the lt to run (default: the lt that
#                                      submitted the job, then `lt` on PATH)
# Edit EPILOG_TIMEOUT_SECONDS below to bound how long a job's cleanup may wait.

EPILOG_TIMEOUT_SECONDS=60

case "${LAB_TRACKER_HPC_EPILOG_ENABLED:-1}" in
  0 | false | FALSE | False | no | NO | No | off | OFF | Off) exit 0 ;;
esac

# The batch step only: srun steps (0, 1, ...) and the extern step end with
# their own task epilogs, one per task, and must not each start Python.
case "${SLURM_STEP_ID:-batch}" in
  batch | 4294967294) ;;
  *) exit 0 ;;
esac
case "${SLURM_PROCID:-0}" in
  0) ;;
  *) exit 0 ;;
esac

[ -n "${SLURM_JOB_ID:-}" ] || exit 0
job="${SLURM_ARRAY_JOB_ID:-$SLURM_JOB_ID}"

# Cheap pre-check: only jobs `lt hpc submit` recorded have a run manifest.
manifest=""
for dir in "${SLURM_SUBMIT_DIR:-}" "${SLURM_JOB_WORK_DIR:-}"; do
  [ -n "$dir" ] || continue
  for candidate in \
    "$dir/.lab-tracker/hpc-runs/job-$job.${SLURM_CLUSTER_NAME:-}.json" \
    "$dir/.lab-tracker/hpc-runs/job-$job.json"; do
    if [ -f "$candidate" ]; then
      manifest="$candidate"
      break 2
    fi
  done
done
[ -n "$manifest" ] || exit 0

lt_cmd="${LAB_TRACKER_LT:-}"
if [ -z "$lt_cmd" ]; then
  # The manifest records the lt that submitted the job; it runs as the same user.
  lt_cmd="$(sed -n 's/^[[:space:]]*"lt_command":[[:space:]]*"\(.*\)",\{0,1\}[[:space:]]*$/\1/p' \
    "$manifest" 2>/dev/null | head -n 1)"
fi
if [ -z "$lt_cmd" ] || [ ! -x "$lt_cmd" ]; then
  lt_cmd="$(command -v lt 2>/dev/null || true)"
fi
[ -n "$lt_cmd" ] || exit 0

if command -v timeout >/dev/null 2>&1; then
  timeout "$EPILOG_TIMEOUT_SECONDS" "$lt_cmd" hpc epilog --fail-silent \
    >/dev/null 2>&1 </dev/null
else
  "$lt_cmd" hpc epilog --fail-silent >/dev/null 2>&1 </dev/null
fi
exit 0
