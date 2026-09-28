"""Per-(project, user) batch settings: cadence, consent, and the delegation grant."""

from __future__ import annotations

from typing import TypeVar
from uuid import UUID

from lab_tracker.auth import AuthContext
from lab_tracker.errors import ValidationError
from lab_tracker.models import (
    DelegatedCurationPolicy,
    ExternalContextPolicy,
    GraphDraftBatchSettings,
    utc_now,
)
from lab_tracker.patching import NOT_PROVIDED, PatchValue, is_provided
from lab_tracker.services import graph_draft_batch_policy as batch_policy
from lab_tracker.services.base import BaseService, ServiceContext
from lab_tracker.services.graph_draft_delegation import (
    DELEGATED_CURATION_ACKNOWLEDGEMENT_REQUIRED,
    DELEGATED_CURATION_PROJECT_LEVEL_ONLY,
    GRANT_DELEGATED_CURATION_ACTION,
)
from lab_tracker.services.graph_draft_scheduling_ports import (
    SchedulingAuthorization,
    SchedulingProjects,
    SchedulingRepository,
)
from lab_tracker.services.review_email_service import normalize_review_email
from lab_tracker.services.shared import actor_user_id

SettingValueT = TypeVar("SettingValueT")
EXTERNAL_PROVIDER_ACKNOWLEDGEMENT_REQUIRED = (
    "Scheduled drafting with an external AI provider requires explicit "
    "external-provider acknowledgement."
)
ACKNOWLEDGE_EXTERNAL_PROVIDER_ACTION = "Acknowledging the external AI provider"


def _validated_setting_patch(
    value: PatchValue[SettingValueT | None],
    field_name: str,
) -> PatchValue[SettingValueT]:
    if not is_provided(value):
        return NOT_PROVIDED
    if value is None:
        raise ValidationError(f"{field_name} must not be null.")
    return value




class BatchSettingsCoordinator(BaseService):
    """Own the settings rows the scheduler reads and the consents they record."""

    def __init__(
        self,
        context: ServiceContext,
        *,
        projects: SchedulingProjects,
        authorization: SchedulingAuthorization,
        host: batch_policy.DraftingHostFacts,
    ) -> None:
        super().__init__(context)
        self.projects = projects
        self.authorization = authorization
        self.review_email_available = host.review_email_available
        self.external_provider = host.external_provider

    @property
    def scheduling_repository(self) -> SchedulingRepository:
        return self._context.active_repository()

    def get_graph_draft_batch_settings(
        self,
        project_id: UUID,
        *,
        user_id: UUID | None = None,
        actor: AuthContext | None = None,
    ) -> GraphDraftBatchSettings:
        # Per-user settings now include a notification address. Keep another
        # user's address owner-only while preserving ordinary read access to
        # the project-level default and to one's own settings.
        if user_id is not None and (actor is None or user_id != actor.user_id):
            self.authorization.require_owner(project_id, actor=actor)
        else:
            self.authorization.require_read(project_id, actor=actor)
        # Global-read authorization can succeed without consulting the project
        # repository. Resolve the target only after authorization so missing
        # projects return the canonical 404 without becoming an existence
        # oracle for unauthorized callers.
        self.projects.get_project(project_id)
        settings = self.scheduling_repository.get_graph_draft_batch_settings_by_project(
            project_id,
            user_id=user_id,
        )
        if settings is not None:
            settings.review_email_available = self.review_email_available
            return settings
        default = self.scheduling_repository.get_graph_draft_batch_settings_by_project(project_id)
        settings = batch_policy.default_batch_settings(
            project_id=project_id,
            user_id=user_id,
            actor=actor,
            inherit_from=default,
        )
        settings.review_email_available = self.review_email_available
        return settings

    def update_graph_draft_batch_settings(
        self,
        project_id: UUID,
        *,
        enabled: PatchValue[bool | None] = NOT_PROVIDED,
        cadence_minutes: PatchValue[int | None] = NOT_PROVIDED,
        run_at_local_time: PatchValue[str | None] = NOT_PROVIDED,
        timezone_name: PatchValue[str | None] = NOT_PROVIDED,
        user_id: PatchValue[UUID | None] = NOT_PROVIDED,
        email_notifications_enabled: PatchValue[bool | None] = NOT_PROVIDED,
        notification_email: PatchValue[str | None] = NOT_PROVIDED,
        external_context_policy: PatchValue[ExternalContextPolicy | None] = NOT_PROVIDED,
        external_provider_acknowledged: PatchValue[bool | None] = NOT_PROVIDED,
        delegated_curation: PatchValue[DelegatedCurationPolicy | None] = NOT_PROVIDED,
        delegated_curation_acknowledged: PatchValue[bool | None] = NOT_PROVIDED,
        actor: AuthContext | None = None,
    ) -> GraphDraftBatchSettings:
        enabled = _validated_setting_patch(enabled, "enabled")
        cadence_minutes = _validated_setting_patch(cadence_minutes, "cadence_minutes")
        external_context_policy = _validated_setting_patch(
            external_context_policy, "external_context_policy"
        )
        external_provider_acknowledged = _validated_setting_patch(
            external_provider_acknowledged,
            "external_provider_acknowledged",
        )
        if (
            is_provided(external_provider_acknowledged)
            and external_provider_acknowledged is not True
        ):
            raise ValidationError("external_provider_acknowledged must be true when provided.")
        delegated_curation = _validated_setting_patch(delegated_curation, "delegated_curation")
        delegated_curation_acknowledged = _validated_setting_patch(
            delegated_curation_acknowledged, "delegated_curation_acknowledged"
        )
        if (
            is_provided(delegated_curation_acknowledged)
            and delegated_curation_acknowledged is not True
        ):
            raise ValidationError("delegated_curation_acknowledged must be true when provided.")
        if is_provided(delegated_curation_acknowledged) and not is_provided(delegated_curation):
            raise ValidationError(
                "delegated_curation_acknowledged only accompanies a delegated_curation change."
            )
        run_at_local_time = _validated_setting_patch(
            run_at_local_time,
            "run_at_local_time",
        )
        timezone_name = _validated_setting_patch(timezone_name, "timezone_name")
        user_id = _validated_setting_patch(user_id, "user_id")
        email_notifications_enabled = _validated_setting_patch(
            email_notifications_enabled,
            "email_notifications_enabled",
        )
        resolved_user_id = user_id if is_provided(user_id) else None
        # Contributors may schedule their own project's daily batch -- the
        # project-level default (user_id is None) and their own per-user
        # settings (user_id == actor). Editing *another* user's per-user
        # settings still requires owner.
        editing_other_user = resolved_user_id is not None and (
            actor is None or resolved_user_id != actor.user_id
        )
        if editing_other_user:
            self.authorization.require_owner(project_id, actor=actor)
        else:
            self.authorization.require_contributor(project_id, actor=actor)
        self.projects.get_project(project_id)
        settings = self.scheduling_repository.get_graph_draft_batch_settings_by_project(
            project_id,
            user_id=resolved_user_id,
        )
        if settings is None:
            default = self.scheduling_repository.get_graph_draft_batch_settings_by_project(
                project_id
            )
            settings = batch_policy.default_batch_settings(
                project_id=project_id,
                user_id=resolved_user_id,
                actor=actor,
                inherit_from=default,
            )
        settings.review_email_available = self.review_email_available
        before = settings.model_copy(deep=True)
        if is_provided(external_provider_acknowledged):
            # Consent is a person's act: a service token or automation
            # principal cannot acknowledge on anyone's behalf.
            self.authorization.require_interactive(
                actor,
                action=ACKNOWLEDGE_EXTERNAL_PROVIDER_ACTION,
            )
            settings.external_provider_acknowledged_at = utc_now()
            settings.external_provider_acknowledged_by = actor_user_id(actor)
        if is_provided(external_context_policy):
            settings.external_context_policy = external_context_policy
        if is_provided(delegated_curation):
            self._apply_delegated_curation(
                settings,
                delegated_curation,
                acknowledged=is_provided(delegated_curation_acknowledged),
                actor=actor,
            )
        if is_provided(enabled):
            settings.enabled = enabled
        if is_provided(cadence_minutes):
            if cadence_minutes < 60:
                raise ValidationError("cadence_minutes must be at least 60.")
            settings.cadence_minutes = cadence_minutes
        if is_provided(run_at_local_time):
            batch_policy.validate_run_at_local_time(run_at_local_time)
            settings.run_at_local_time = run_at_local_time
        if is_provided(timezone_name):
            batch_policy.zoneinfo_for(timezone_name)
            settings.timezone_name = timezone_name
        if is_provided(notification_email):
            cleaned_email = (
                normalize_review_email(notification_email)
                if notification_email is not None and notification_email.strip()
                else None
            )
            if cleaned_email != settings.notification_email:
                settings.notification_email = cleaned_email
                settings.notification_email_confirmed_at = (
                    utc_now() if cleaned_email is not None else None
                )
        if is_provided(email_notifications_enabled):
            if email_notifications_enabled and not self.review_email_available:
                raise ValidationError(
                    "Review email delivery is not enabled on this Lab Tracker host."
                )
            settings.email_notifications_enabled = email_notifications_enabled
        if settings.email_notifications_enabled:
            if settings.user_id is None:
                raise ValidationError("Email alerts require per-user batch settings with user_id.")
            if not settings.notification_email or settings.notification_email_confirmed_at is None:
                raise ValidationError(
                    "notification_email is required before email alerts can be enabled."
                )
        self._require_external_provider_acknowledgement(settings, before)
        consent_changed = any(
            (
                settings.external_context_policy != before.external_context_policy,
                settings.external_provider_acknowledged_at
                != before.external_provider_acknowledged_at,
                settings.delegated_curation != before.delegated_curation,
                settings.delegated_curation_granted_at != before.delegated_curation_granted_at,
            )
        )
        scheduling_changed = any(
            (
                settings.enabled != before.enabled,
                settings.cadence_minutes != before.cadence_minutes,
                settings.run_at_local_time != before.run_at_local_time,
                settings.timezone_name != before.timezone_name,
            )
        )
        notification_changed = any(
            (
                settings.email_notifications_enabled != before.email_notifications_enabled,
                settings.notification_email != before.notification_email,
                settings.notification_email_confirmed_at != before.notification_email_confirmed_at,
            )
        )
        if not scheduling_changed and not notification_changed and not consent_changed:
            return settings
        if scheduling_changed:
            settings.next_run_at = (
                batch_policy.next_run_at(
                    cadence_minutes=settings.cadence_minutes,
                    run_at_local_time=settings.run_at_local_time,
                    timezone_name=settings.timezone_name,
                )
                if settings.enabled
                else None
            )
        settings.updated_at = utc_now()
        settings.updated_by = actor_user_id(actor)
        with self.unit_of_work():
            self.scheduling_repository.graph_draft_batch_settings.save(settings)
        return settings

    def _apply_delegated_curation(
        self,
        settings: GraphDraftBatchSettings,
        policy: DelegatedCurationPolicy,
        *,
        acknowledged: bool,
        actor: AuthContext | None,
    ) -> None:
        """Record an owner's grant on the project-default row, or withdraw it.

        Widening (off -> organize -> full, or organize <-> full) is a person's
        act at an interactive owner session with the acknowledgement in the
        same request; it stamps who granted it and when. Turning it off never
        needs consent and clears the stamps. Personal rows never carry a grant.
        """

        if settings.user_id is not None:
            raise ValidationError(DELEGATED_CURATION_PROJECT_LEVEL_ONLY)
        if policy is settings.delegated_curation:
            return
        if policy is DelegatedCurationPolicy.OFF:
            settings.delegated_curation = policy
            settings.delegated_curation_granted_at = None
            settings.delegated_curation_granted_by = None
            return
        self.authorization.require_owner(settings.project_id, actor=actor)
        self.authorization.require_interactive(actor, action=GRANT_DELEGATED_CURATION_ACTION)
        if not acknowledged:
            raise ValidationError(DELEGATED_CURATION_ACKNOWLEDGEMENT_REQUIRED)
        settings.delegated_curation = policy
        settings.delegated_curation_granted_at = utc_now()
        settings.delegated_curation_granted_by = actor_user_id(actor)

    def _require_external_provider_acknowledgement(
        self,
        settings: GraphDraftBatchSettings,
        before: GraphDraftBatchSettings,
    ) -> None:
        """Gate the two transitions that widen what leaves the instance.

        Turning the cadence on sends the person's staged captures to the
        provider; switching to ``project_notes`` sends colleagues' notes too.
        A loopback provider never leaves the host, so nothing is gated.
        """

        if not self.external_provider or settings.external_provider_acknowledged_at is not None:
            return
        enabling = settings.enabled and not before.enabled
        widening = (
            settings.external_context_policy is ExternalContextPolicy.PROJECT_NOTES
            and before.external_context_policy is not ExternalContextPolicy.PROJECT_NOTES
        )
        if enabling or widening:
            raise ValidationError(EXTERNAL_PROVIDER_ACKNOWLEDGEMENT_REQUIRED)
