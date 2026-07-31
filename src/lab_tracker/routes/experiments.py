"""Experiment lifecycle and membership routes."""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter
from starlette import status as http_status
from starlette.requests import Request

from lab_tracker.api import LabTrackerAPI
from lab_tracker.models import (
    Dataset,
    Experiment,
    ExperimentStatus,
    Session,
    UsageEventResourceType,
)
from lab_tracker.schemas import (
    Envelope,
    ExperimentCreate,
    ExperimentUpdate,
    ListEnvelope,
)

from .shared import (
    accessible_project_ids_from_request,
    actor_from_request,
    api_from_request,
    ensure_project_read,
    list_response,
    record_usage_view,
    validate_pagination,
)


def build_experiments_router(api: LabTrackerAPI) -> APIRouter:
    router = APIRouter()

    @router.post(
        "/experiments",
        response_model=Envelope[Experiment],
        status_code=http_status.HTTP_201_CREATED,
    )
    def create_experiment(payload: ExperimentCreate, request: Request):
        experiment = api_from_request(request, api).create_experiment(
            project_id=payload.project_id,
            name=payload.name,
            primary_question_id=payload.primary_question_id,
            description=payload.description,
            actor=actor_from_request(request),
        )
        return Envelope(data=experiment)

    @router.get("/experiments", response_model=ListEnvelope[Experiment])
    def list_experiments(
        request: Request,
        project_id: UUID | None = None,
        primary_question_id: UUID | None = None,
        status: ExperimentStatus | None = None,
        search: str | None = None,
        session_id: UUID | None = None,
        dataset_id: UUID | None = None,
        limit: int = 50,
        offset: int = 0,
    ):
        validate_pagination(limit, offset)
        if project_id is not None:
            ensure_project_read(request, project_id)
            project_ids = None
        else:
            project_ids = accessible_project_ids_from_request(request)
        experiments, total = api_from_request(
            request,
            api,
        ).query_experiments(
            project_id=project_id,
            project_ids=project_ids,
            primary_question_id=primary_question_id,
            status=status,
            search=search,
            session_id=session_id,
            dataset_id=dataset_id,
            limit=limit,
            offset=offset,
        )
        return list_response(
            experiments,
            limit=limit,
            offset=offset,
            total=total,
        )

    @router.get(
        "/experiments/{experiment_id}",
        response_model=Envelope[Experiment],
    )
    def get_experiment(experiment_id: UUID, request: Request):
        experiment = api_from_request(request, api).get_experiment_for_read(
            experiment_id,
            actor=actor_from_request(request),
        )
        record_usage_view(
            request,
            resource_type=UsageEventResourceType.EXPERIMENT,
            resource_id=experiment.experiment_id,
            project_id=experiment.project_id,
        )
        return Envelope(data=experiment)

    @router.patch(
        "/experiments/{experiment_id}",
        response_model=Envelope[Experiment],
    )
    def update_experiment(
        experiment_id: UUID,
        payload: ExperimentUpdate,
        request: Request,
    ):
        request_api = api_from_request(request, api)
        existing = request_api.get_experiment(experiment_id)
        ensure_project_read(request, existing.project_id)
        update_kwargs = {
            "name": payload.name,
            "status": payload.status,
            "actor": actor_from_request(request),
        }
        if "description" in payload.model_fields_set:
            update_kwargs["description"] = payload.description
        experiment = request_api.update_experiment(
            experiment_id,
            **update_kwargs,
        )
        return Envelope(data=experiment)

    @router.get(
        "/experiments/{experiment_id}/sessions",
        response_model=ListEnvelope[Session],
    )
    def list_experiment_sessions(
        experiment_id: UUID,
        request: Request,
        limit: int = 50,
        offset: int = 0,
    ):
        validate_pagination(limit, offset)
        request_api = api_from_request(request, api)
        request_api.get_experiment_for_read(
            experiment_id,
            actor=actor_from_request(request),
        )
        sessions, total = request_api.query_experiment_sessions(
            experiment_id,
            limit=limit,
            offset=offset,
        )
        return list_response(
            sessions,
            limit=limit,
            offset=offset,
            total=total,
        )

    @router.put(
        "/experiments/{experiment_id}/sessions/{session_id}",
        response_model=Envelope[Experiment],
    )
    def add_experiment_session(
        experiment_id: UUID,
        session_id: UUID,
        request: Request,
    ):
        request_api = api_from_request(request, api)
        experiment = request_api.get_experiment(experiment_id)
        ensure_project_read(request, experiment.project_id)
        return Envelope(
            data=request_api.add_experiment_session(
                experiment_id,
                session_id,
                actor=actor_from_request(request),
            )
        )

    @router.delete(
        "/experiments/{experiment_id}/sessions/{session_id}",
        response_model=Envelope[Experiment],
    )
    def remove_experiment_session(
        experiment_id: UUID,
        session_id: UUID,
        request: Request,
    ):
        request_api = api_from_request(request, api)
        experiment = request_api.get_experiment(experiment_id)
        ensure_project_read(request, experiment.project_id)
        return Envelope(
            data=request_api.remove_experiment_session(
                experiment_id,
                session_id,
                actor=actor_from_request(request),
            )
        )

    @router.get(
        "/experiments/{experiment_id}/datasets",
        response_model=ListEnvelope[Dataset],
    )
    def list_experiment_datasets(
        experiment_id: UUID,
        request: Request,
        limit: int = 50,
        offset: int = 0,
    ):
        validate_pagination(limit, offset)
        request_api = api_from_request(request, api)
        request_api.get_experiment_for_read(
            experiment_id,
            actor=actor_from_request(request),
        )
        datasets, total = request_api.query_experiment_datasets(
            experiment_id,
            limit=limit,
            offset=offset,
        )
        return list_response(
            datasets,
            limit=limit,
            offset=offset,
            total=total,
        )

    @router.put(
        "/experiments/{experiment_id}/datasets/{dataset_id}",
        response_model=Envelope[Experiment],
    )
    def add_experiment_dataset(
        experiment_id: UUID,
        dataset_id: UUID,
        request: Request,
    ):
        request_api = api_from_request(request, api)
        experiment = request_api.get_experiment(experiment_id)
        ensure_project_read(request, experiment.project_id)
        return Envelope(
            data=request_api.add_experiment_dataset(
                experiment_id,
                dataset_id,
                actor=actor_from_request(request),
            )
        )

    @router.delete(
        "/experiments/{experiment_id}/datasets/{dataset_id}",
        response_model=Envelope[Experiment],
    )
    def remove_experiment_dataset(
        experiment_id: UUID,
        dataset_id: UUID,
        request: Request,
    ):
        request_api = api_from_request(request, api)
        experiment = request_api.get_experiment(experiment_id)
        ensure_project_read(request, experiment.project_id)
        return Envelope(
            data=request_api.remove_experiment_dataset(
                experiment_id,
                dataset_id,
                actor=actor_from_request(request),
            )
        )

    @router.get(
        "/sessions/{session_id}/experiments",
        response_model=ListEnvelope[Experiment],
    )
    def list_session_experiments(
        session_id: UUID,
        request: Request,
        limit: int = 50,
        offset: int = 0,
    ):
        validate_pagination(limit, offset)
        request_api = api_from_request(request, api)
        request_api.get_session_for_read(
            session_id,
            actor=actor_from_request(request),
        )
        experiments, total = request_api.query_experiments(
            session_id=session_id,
            limit=limit,
            offset=offset,
        )
        return list_response(
            experiments,
            limit=limit,
            offset=offset,
            total=total,
        )

    @router.get(
        "/datasets/{dataset_id}/experiments",
        response_model=ListEnvelope[Experiment],
    )
    def list_dataset_experiments(
        dataset_id: UUID,
        request: Request,
        limit: int = 50,
        offset: int = 0,
    ):
        validate_pagination(limit, offset)
        request_api = api_from_request(request, api)
        request_api.get_dataset_for_read(
            dataset_id,
            actor=actor_from_request(request),
        )
        experiments, total = request_api.query_experiments(
            dataset_id=dataset_id,
            limit=limit,
            offset=offset,
        )
        return list_response(
            experiments,
            limit=limit,
            offset=offset,
            total=total,
        )

    return router
