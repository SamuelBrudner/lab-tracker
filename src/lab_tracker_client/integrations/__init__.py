"""Thin, optional pipeline-framework hooks for ``lt pipeline`` capture.

Each module turns what one framework already declares about a finished run
into a :class:`lab_tracker_client.pipeline_capture.PipelineRun` and records it
as a single staged note:

* :mod:`~lab_tracker_client.integrations.snakemake` — ``report(log, ...)`` for
  a Snakefile's ``onsuccess:``/``onerror:`` blocks;
* :mod:`~lab_tracker_client.integrations.nextflow` — the trace-file parser
  behind ``lt pipeline nextflow --trace trace.txt``;
* :mod:`~lab_tracker_client.integrations.kedro` — ``LabTrackerHooks`` for a
  Kedro project's ``settings.HOOKS``;
* :mod:`~lab_tracker_client.integrations.dvc` — the ``dvc.lock`` reader behind
  ``lt pipeline dvc``.

None of them imports its framework at module import time; the frameworks are
optional and never Lab Tracker dependencies.
"""
