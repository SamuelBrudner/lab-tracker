"""Typed API delegation for ongoing-project member onboarding."""

from __future__ import annotations

from typing import TYPE_CHECKING
from uuid import UUID

from lab_tracker.auth import AuthContext
from lab_tracker.graph_drafting import GraphDraftClient
from lab_tracker.schemas import (
    MemberOnboardingCheckpointRequest,
    MemberOnboardingManualAlignmentRequest,
    MemberOnboardingOwnerQueueItem,
    MemberOnboardingRead,
)
from lab_tracker.services.member_onboarding_service import (
    MemberOnboardingCommandResult,
    MemberOnboardingService,
)


class MemberOnboardingApiMixin:
    if TYPE_CHECKING:
        member_onboarding: MemberOnboardingService

    def get_member_onboarding(
        self,
        project_id: UUID,
        *,
        actor: AuthContext | None,
    ) -> MemberOnboardingRead:
        return self.member_onboarding.get_member_onboarding(project_id, actor=actor)

    def put_member_onboarding_checkpoint(
        self,
        project_id: UUID,
        payload: MemberOnboardingCheckpointRequest,
        *,
        actor: AuthContext | None,
    ) -> MemberOnboardingCommandResult:
        return self.member_onboarding.put_checkpoint(project_id, payload, actor=actor)

    def put_member_onboarding_manual_alignment(
        self,
        project_id: UUID,
        payload: MemberOnboardingManualAlignmentRequest,
        *,
        actor: AuthContext | None,
    ) -> MemberOnboardingRead:
        return self.member_onboarding.put_manual_alignment(project_id, payload, actor=actor)

    def start_member_onboarding_ai_alignment(
        self,
        project_id: UUID,
        *,
        external_provider_acknowledged: bool,
        draft_client: GraphDraftClient,
        actor: AuthContext | None,
    ) -> MemberOnboardingCommandResult:
        return self.member_onboarding.start_ai_alignment(
            project_id,
            external_provider_acknowledged=external_provider_acknowledged,
            draft_client=draft_client,
            actor=actor,
        )

    def list_member_onboarding_owner_queue(
        self,
        project_id: UUID,
        *,
        actor: AuthContext | None,
    ) -> list[MemberOnboardingOwnerQueueItem]:
        return self.member_onboarding.owner_queue(project_id, actor=actor)
