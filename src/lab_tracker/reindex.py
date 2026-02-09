"""One-time search index backfill utility.

Run with:

  python -m lab_tracker.reindex

This command is intended to (re)populate a persistent search backend (for example,
ChromaDB) from the primary SQL database.
"""

from __future__ import annotations

import argparse
from collections.abc import Iterable, Sequence
import sys
from typing import TypeVar

from sqlalchemy.exc import SQLAlchemyError

from lab_tracker.config import get_settings
from lab_tracker.db import get_engine, get_session_factory
from lab_tracker.sqlalchemy_repository import SQLAlchemyLabTrackerRepository
from lab_tracker.services.search_backend_factory import build_search_backend


T = TypeVar("T")


def _chunk(values: Sequence[T], size: int) -> Iterable[list[T]]:
    if size <= 0:
        raise ValueError("size must be > 0")
    for start in range(0, len(values), size):
        yield list(values[start : start + size])


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Backfill the configured search backend.")
    parser.add_argument(
        "--backend",
        default=None,
        help="Override LAB_TRACKER_SEARCH_BACKEND for this run.",
    )
    parser.add_argument(
        "--chromadb-persist-path",
        default=None,
        help="Override LAB_TRACKER_CHROMADB_PERSIST_PATH for this run.",
    )
    parser.add_argument(
        "--embedding-provider",
        default=None,
        help="Override LAB_TRACKER_EMBEDDING_PROVIDER for this run.",
    )
    parser.add_argument(
        "--reset",
        action="store_true",
        help="If using ChromaDB, drop existing collections before reindexing.",
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=256,
        help="Batch size for upserts (default: 256).",
    )
    return parser.parse_args(list(argv) if argv is not None else None)


def _reset_chromadb(persist_path: str) -> None:
    try:
        import chromadb  # type: ignore[import-not-found]
    except ModuleNotFoundError as exc:  # pragma: no cover
        raise RuntimeError(
            "chromadb is not installed. Install it (pip install chromadb) or disable --reset."
        ) from exc

    client = chromadb.PersistentClient(path=persist_path)
    for name in ("questions", "notes"):
        try:
            client.delete_collection(name=name)
        except Exception:
            # Older/newer chromadb versions vary; ignore missing collections.
            pass


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv)
    settings = get_settings()
    overrides: dict[str, object] = {}
    if args.backend:
        overrides["search_backend"] = args.backend
    if args.chromadb_persist_path:
        overrides["chromadb_persist_path"] = args.chromadb_persist_path
    if args.embedding_provider:
        overrides["embedding_provider"] = args.embedding_provider
    if overrides:
        settings = settings.model_copy(update=overrides)

    if args.reset and (settings.search_backend or "").strip().casefold() in {
        "chromadb",
        "chroma",
        "chroma_db",
    }:
        _reset_chromadb(str(settings.chromadb_persist_path))

    search_backend = build_search_backend(settings)

    engine = get_engine(settings)
    session_factory = get_session_factory(settings, engine=engine)
    try:
        with session_factory() as session:
            repository = SQLAlchemyLabTrackerRepository(session)
            questions = repository.questions.list()
            notes = repository.notes.list()
    except SQLAlchemyError as exc:
        raise RuntimeError(f"Failed to load data from database: {exc}") from exc

    batch_size = int(args.batch_size or 0)
    if batch_size <= 0:
        raise ValueError("batch-size must be > 0")

    for batch in _chunk(questions, batch_size):
        search_backend.upsert_questions(batch)
    for batch in _chunk(notes, batch_size):
        search_backend.upsert_notes(batch)

    print(
        f"Reindexed {len(questions)} questions and {len(notes)} notes into "
        f"search backend '{search_backend.backend_name}'."
    )
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main(sys.argv[1:]))
