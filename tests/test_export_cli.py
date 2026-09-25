"""The client export writes provenance sidecars that survive without a server."""

from __future__ import annotations

import argparse
import json

import httpx
import pytest

from lab_tracker_client import LabTracker
from lab_tracker_client import cli as lt_cli


def _json_response(payload: dict) -> httpx.Response:
    return httpx.Response(200, json=payload)


def _list_payload(items: list[dict]) -> dict:
    return {"data": items, "meta": {"limit": 200, "offset": 0, "total": len(items)}}


def _prov_doc(entity_id: str) -> dict:
    return {"@context": {"prov": "http://www.w3.org/ns/prov#"}, "@id": entity_id}


def _handler(request: httpx.Request) -> httpx.Response:
    path = request.url.path
    if path == "/datasets":
        return _json_response(
            _list_payload(
                [
                    {
                        "dataset_id": "ds-1",
                        "commit_manifest": {"files": [{"path": "raw/session001.nwb"}]},
                    }
                ]
            )
        )
    if path == "/analyses":
        return _json_response(_list_payload([{"analysis_id": "an-1"}]))
    if path == "/claims":
        return _json_response(_list_payload([{"claim_id": "cl-1"}]))
    if path.endswith("/provenance"):
        entity_id = path.split("/")[2]
        return _json_response(_prov_doc(entity_id))
    return httpx.Response(500, json={"error": {"message": f"unexpected {path}"}})


def _args(**overrides: object) -> argparse.Namespace:
    values = {
        "project": "project-1",
        "out": "",
        "since": None,
        "until": None,
        "data_root": None,
        "ara": False,
    }
    values.update(overrides)
    return argparse.Namespace(**values)


def test_export_writes_sidecars_readable_without_server(tmp_path) -> None:
    out_dir = tmp_path / "export"
    with LabTracker(base_url="http://testserver", transport=httpx.MockTransport(_handler)) as lt:
        summary = lt_cli._cmd_export(lt, _args(out=str(out_dir)))

    assert summary["counts"] == {"dataset": 1, "analysis": 1, "claim": 1}
    dataset_sidecar = out_dir / "dataset-ds-1.prov.jsonld"
    assert dataset_sidecar.is_file()
    # The sidecar is self-contained JSON-LD: readable with the server stopped.
    document = json.loads(dataset_sidecar.read_text(encoding="utf-8"))
    assert document["@id"] == "ds-1"
    assert (out_dir / "analysis-an-1.prov.jsonld").is_file()
    assert (out_dir / "claim-cl-1.prov.jsonld").is_file()


def test_export_co_locates_dataset_sidecar_next_to_data(tmp_path) -> None:
    data_root = tmp_path / "data"
    (data_root / "raw").mkdir(parents=True)
    (data_root / "raw" / "session001.nwb").write_bytes(b"fake-nwb")
    out_dir = tmp_path / "export"

    with LabTracker(base_url="http://testserver", transport=httpx.MockTransport(_handler)) as lt:
        lt_cli._cmd_export(
            lt,
            _args(out=str(out_dir), data_root=str(data_root)),
        )

    # The reasoning lands beside the .nwb file, not only in the export dir.
    co_located = data_root / "raw" / "dataset-ds-1.prov.jsonld"
    assert co_located.is_file()
    assert json.loads(co_located.read_text(encoding="utf-8"))["@id"] == "ds-1"


def _graph_prov_doc(base: str, route: str, entity_id: str) -> dict:
    return {
        "@context": {"prov": "http://www.w3.org/ns/prov#"},
        "@graph": [{"@id": f"{base}/{route}/{entity_id}", "@type": "prov:Entity"}],
    }


def _graph_handler(identifier_base: str):
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/datasets":
            return _json_response(_list_payload([{"dataset_id": "ds-1"}]))
        if path in ("/analyses", "/claims"):
            return _json_response(_list_payload([]))
        if path.endswith("/provenance"):
            route, entity_id = path.split("/")[1:3]
            return _json_response(_graph_prov_doc(identifier_base, route, entity_id))
        return httpx.Response(500, json={"error": {"message": f"unexpected {path}"}})

    return handler


def test_export_notes_host_relative_identifiers(tmp_path) -> None:
    from lab_tracker_client.export import export_project_provenance

    handler = _graph_handler("http://testserver")
    with LabTracker(base_url="http://testserver", transport=httpx.MockTransport(handler)) as lt:
        result = export_project_provenance(lt, project_id="p1", out_dir=str(tmp_path))

    assert result.identifier_root == "http://testserver"
    assert result.identifier_note is not None
    assert "LAB_TRACKER_BASE_URL" in result.identifier_note
    assert result.to_dict()["identifier_note"] == result.identifier_note


def test_export_stays_quiet_for_canonical_identifiers(tmp_path) -> None:
    from lab_tracker_client.export import export_project_provenance

    handler = _graph_handler("https://lab.example.org")
    with LabTracker(base_url="http://testserver", transport=httpx.MockTransport(handler)) as lt:
        result = export_project_provenance(lt, project_id="p1", out_dir=str(tmp_path))

    assert result.identifier_root == "https://lab.example.org"
    assert result.identifier_note is None
    assert "identifier_note" not in result.to_dict()


def _ara_doc(scope: str, entity_id: str) -> dict:
    return {
        "@context": {"prov": "http://www.w3.org/ns/prov#"},
        "@id": f"http://testserver/{scope}/{entity_id}/ara-artifact",
        "@type": "lab:AraArtifact",
        "layers": {},
    }


def _ara_handler(request: httpx.Request) -> httpx.Response:
    path = request.url.path
    if path == "/projects/project-1/goals":
        return _json_response(_list_payload([{"goal_id": "g1", "title": "Paper"}]))
    if path == "/questions":
        return _json_response(
            _list_payload(
                [
                    {"question_id": "q1", "text": "Root", "parent_question_ids": []},
                    {"question_id": "q2", "text": "Child", "parent_question_ids": ["q1"]},
                ]
            )
        )
    if path.endswith("/ara-artifact"):
        scope, entity_id = path.split("/")[1:3]
        return _json_response(_ara_doc(scope, entity_id))
    return _handler(request)


def test_export_ara_writes_goal_and_root_question_artifacts(tmp_path) -> None:
    out_dir = tmp_path / "export"
    with LabTracker(
        base_url="http://testserver", transport=httpx.MockTransport(_ara_handler)
    ) as lt:
        summary = lt_cli._cmd_export(lt, _args(out=str(out_dir), ara=True))

    assert summary["counts"] == {
        "dataset": 1,
        "analysis": 1,
        "claim": 1,
        "ara_goal": 1,
        "ara_question": 1,
    }
    goal_artifact = out_dir / "goal-g1.ara.jsonld"
    question_artifact = out_dir / "question-q1.ara.jsonld"
    assert goal_artifact.is_file()
    assert question_artifact.is_file()
    assert json.loads(goal_artifact.read_text(encoding="utf-8"))["@type"] == "lab:AraArtifact"
    assert json.loads(question_artifact.read_text(encoding="utf-8"))["@id"].endswith(
        "/questions/q1/ara-artifact"
    )
    # The child question's story lives inside its root's subtree: no file of its own.
    assert not (out_dir / "question-q2.ara.jsonld").exists()
    assert str(goal_artifact) in summary["files"]
    assert str(question_artifact) in summary["files"]


def test_export_without_ara_flag_never_calls_ara_routes(tmp_path) -> None:
    def strict_handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/ara-artifact") or path in ("/questions", "/projects/project-1/goals"):
            return httpx.Response(500, json={"error": {"message": f"unexpected {path}"}})
        return _handler(request)

    with LabTracker(
        base_url="http://testserver", transport=httpx.MockTransport(strict_handler)
    ) as lt:
        summary = lt_cli._cmd_export(lt, _args(out=str(tmp_path / "export")))

    assert summary["counts"] == {"dataset": 1, "analysis": 1, "claim": 1}
    assert not list((tmp_path / "export").glob("*.ara.jsonld"))


def test_ara_artifact_rejects_unknown_scope() -> None:
    from lab_tracker_client import LTValidationError

    with (
        LabTracker(base_url="http://testserver", transport=httpx.MockTransport(_handler)) as lt,
        pytest.raises(LTValidationError, match="scope must be one of"),
    ):
        lt.ara_artifact("datasets", "ds-1")


def test_export_parser_exposes_the_ara_flag() -> None:
    parser = lt_cli._build_parser()
    args = parser.parse_args(["export", "--project", "p1", "--ara"])
    assert args.ara is True
    assert parser.parse_args(["export", "--project", "p1"]).ara is False
