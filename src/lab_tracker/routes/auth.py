"""Authentication routes."""

from __future__ import annotations

import hmac
import ipaddress
from datetime import datetime, timezone
from urllib.parse import quote, urlencode, urlparse
from uuid import UUID
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import APIRouter
from starlette import status as http_status
from starlette.requests import Request

from lab_tracker.api import LabTrackerAPI
from lab_tracker.auth import (
    LOCAL_AUTH_USERNAME,
    AuthContext,
    AuthService,
    Invitation,
    InvitationClaims,
    InvitationTokenService,
    Role,
    TokenService,
    User,
)
from lab_tracker.db_types import ensure_uuid
from lab_tracker.errors import AuthError, ConflictError, ValidationError
from lab_tracker.models import ProjectMembershipRole
from lab_tracker.schemas import (
    AuthBootstrapStatus,
    AuthInvitationCreate,
    AuthInvitationRead,
    AuthLoginRequest,
    AuthRegisterRequest,
    AuthTokenRead,
    AuthUserRead,
    AuthUserUpdate,
    Envelope,
    ListEnvelope,
)

from .shared import (
    actor_from_authorization_header,
    actor_from_request,
    api_from_request,
    auth_token_read,
    auth_user_read,
    list_response,
    paginate,
    validate_pagination,
)


def build_auth_router(
    *,
    api: LabTrackerAPI,
    auth_service: AuthService,
    token_service: TokenService,
    invitation_token_service: InvitationTokenService,
    bootstrap_admin_token: str | None = None,
) -> APIRouter:
    router = APIRouter()

    @router.get("/auth/bootstrap-status", response_model=Envelope[AuthBootstrapStatus])
    def auth_bootstrap_status(request: Request):
        expected = (bootstrap_admin_token or "").strip()
        has_users = auth_service.has_users()
        bootstrap_token, bootstrap_token_warning = _bootstrap_token_for_status(
            request,
            bootstrap_token=expected,
            has_users=has_users,
        )
        return Envelope(
            data=AuthBootstrapStatus(
                has_users=has_users,
                bootstrap_admin_configured=bool(expected),
                first_admin_available=not has_users and bool(expected),
                public_viewer_registration_enabled=(
                    request.app.state.settings.auth_public_viewer_registration_enabled
                ),
                bootstrap_token=bootstrap_token,
                bootstrap_token_warning=bootstrap_token_warning,
            )
        )

    @router.post(
        "/auth/register",
        response_model=Envelope[AuthTokenRead],
        status_code=http_status.HTTP_201_CREATED,
    )
    def register_auth(payload: AuthRegisterRequest, request: Request):
        _record_auth_attempt(request, _auth_rate_key(request, "register"))
        registration_role = payload.role
        username = payload.username
        invitation: InvitationClaims | None = None
        if payload.invite_token:
            invitation = invitation_token_service.verify_invitation_token(payload.invite_token)
            invited_email = invitation_token_service.normalize_email(invitation.email)
            provided_username = invitation_token_service.normalize_email(payload.username)
            if provided_username != invited_email:
                raise AuthError("Invitation token does not match this email address.")
            if auth_service.get_user(invited_email) is not None:
                raise ConflictError("Invitation has already been used.")
            username = invited_email
            registration_role = invitation.role
        elif payload.role != Role.VIEWER:
            if payload.role == Role.ADMIN and not auth_service.has_users():
                expected = (bootstrap_admin_token or "").strip()
                provided = (payload.bootstrap_token or "").strip()
                if not expected:
                    raise AuthError("Admin bootstrap is not configured for this deployment.")
                if not provided:
                    raise AuthError("Bootstrap token required to create initial admin user.")
                if not hmac.compare_digest(provided, expected):
                    raise AuthError("Invalid bootstrap token.")
            else:
                actor = actor_from_authorization_header(
                    request,
                    auth_service=auth_service,
                    token_service=token_service,
                )
                if actor.role != Role.ADMIN:
                    raise AuthError("Admin privileges required to register non-viewer users.")
        elif not request.app.state.settings.auth_public_viewer_registration_enabled:
            if not request.headers.get("authorization"):
                raise AuthError("Public viewer registration is disabled.")
            actor = actor_from_authorization_header(
                request,
                auth_service=auth_service,
                token_service=token_service,
            )
            if actor.role != Role.ADMIN:
                raise AuthError("Public viewer registration is disabled.")
        user = auth_service.register_user(
            username=username,
            password=payload.password,
            role=registration_role,
            session=request.state.db_session if invitation is not None else None,
        )
        if payload.invite_token and invitation is not None:
            _provision_invited_user(
                api_from_request(request, api),
                user=user,
                invitation=invitation,
            )
            invitation_token_service.consume_invitation_token(
                payload.invite_token,
                consumed_by_user_id=user.user_id,
                session=request.state.db_session,
            )
        token = token_service.issue_access_token(user)
        return Envelope(data=auth_token_read(user, token.token, token.expires_at))

    @router.post(
        "/auth/invitations",
        response_model=Envelope[AuthInvitationRead],
        status_code=http_status.HTTP_201_CREATED,
    )
    def create_auth_invitation(payload: AuthInvitationCreate, request: Request):
        actor = _ensure_admin(request)
        email = invitation_token_service.normalize_email(payload.email)
        project_role: ProjectMembershipRole | None = None
        review_cadence_minutes: int | None = None
        review_run_at_local_time: str | None = None
        review_timezone_name: str | None = None
        if payload.project_id is not None:
            request_api = api_from_request(request, api)
            request_api.require_project_owner(payload.project_id, actor=actor)
            request_api.get_project(payload.project_id)
            project_role = payload.project_role or ProjectMembershipRole.CONTRIBUTOR
            if payload.review_enabled:
                review_cadence_minutes = payload.review_cadence_minutes or 24 * 60
                review_run_at_local_time = payload.review_run_at_local_time or "18:00"
                review_timezone_name = payload.review_timezone_name or "America/New_York"
                _validate_invitation_timezone(review_timezone_name)
        issued = invitation_token_service.issue_invitation(
            email=email,
            role=payload.role,
            project_id=payload.project_id,
            project_role=project_role,
            review_enabled=payload.review_enabled,
            review_cadence_minutes=review_cadence_minutes,
            review_run_at_local_time=review_run_at_local_time,
            review_timezone_name=review_timezone_name,
        )
        base_url, warning = _public_base_url_with_warning(request)
        invite_token = issued.token
        invite_url = f"{base_url}/app/?{urlencode({'invite': invite_token, 'email': email})}"
        mailto_url = _mailto_invitation_url(
            email=email,
            invite_url=invite_url,
            role=payload.role,
            expires_at=issued.invitation.expires_at,
            project_role=issued.invitation.project_role,
            review_enabled=issued.invitation.review_enabled,
            review_run_at_local_time=issued.invitation.review_run_at_local_time,
            review_timezone_name=issued.invitation.review_timezone_name,
        )
        return Envelope(
            data=_auth_invitation_read(
                issued.invitation,
                invite_url=invite_url,
                mailto_url=mailto_url,
                warning=warning,
            )
        )

    @router.get("/auth/invitations", response_model=ListEnvelope[AuthInvitationRead])
    def list_auth_invitations(request: Request, limit: int = 50, offset: int = 0):
        validate_pagination(limit, offset)
        _ensure_admin(request)
        invitations = [
            _auth_invitation_read(invitation)
            for invitation in invitation_token_service.list_invitations()
        ]
        items, total = paginate(invitations, limit, offset)
        return list_response(items, limit=limit, offset=offset, total=total)

    @router.delete(
        "/auth/invitations/{invitation_id:uuid}",
        response_model=Envelope[AuthInvitationRead],
    )
    def revoke_auth_invitation(invitation_id: UUID, request: Request):
        _ensure_admin(request)
        invitation = invitation_token_service.revoke_invitation(invitation_id)
        return Envelope(data=_auth_invitation_read(invitation))

    @router.get("/auth/users", response_model=ListEnvelope[AuthUserRead])
    def list_auth_users(request: Request, limit: int = 50, offset: int = 0):
        validate_pagination(limit, offset)
        _ensure_admin(request)
        users = [auth_user_read(user) for user in auth_service.list_users()]
        items, total = paginate(users, limit, offset)
        return list_response(items, limit=limit, offset=offset, total=total)

    @router.patch("/auth/users/{user_id:uuid}", response_model=Envelope[AuthUserRead])
    def update_auth_user(user_id: UUID, payload: AuthUserUpdate, request: Request):
        _ensure_admin(request)
        user = auth_service.update_user(
            user_id,
            role=payload.role,
            password=payload.password,
        )
        return Envelope(data=auth_user_read(user))

    @router.post("/auth/login", response_model=Envelope[AuthTokenRead])
    def login_auth(payload: AuthLoginRequest, request: Request):
        rate_key = _auth_rate_key(request, "login", payload.username)
        _check_auth_rate_limit(request, rate_key)
        try:
            user = auth_service.authenticate(payload.username, payload.password)
        except AuthError:
            _record_auth_failure(request, rate_key)
            raise
        _reset_auth_rate_limit(request, rate_key)
        token = token_service.issue_access_token(user)
        return Envelope(data=auth_token_read(user, token.token, token.expires_at))

    @router.post("/auth/refresh", response_model=Envelope[AuthTokenRead])
    def refresh_auth(request: Request):
        if not request.app.state.auth_enabled:
            raise AuthError("Token refresh is unavailable when authentication is disabled.")
        actor = actor_from_request(request)
        user = auth_service.get_user_by_id(actor.user_id)
        if user is None:
            raise AuthError("Authentication required.")
        token = token_service.issue_access_token(user)
        return Envelope(data=auth_token_read(user, token.token, token.expires_at))

    @router.get("/auth/me", response_model=Envelope[AuthUserRead])
    def auth_me(request: Request):
        actor = actor_from_request(request)
        if not request.app.state.auth_enabled:
            user = AuthUserRead(
                user_id=ensure_uuid(str(actor.user_id)),
                username=LOCAL_AUTH_USERNAME,
                role=actor.role,
                created_at=datetime.now(timezone.utc),
            )
            return Envelope(data=user, meta=_auth_me_meta(actor, auth_enabled=False))
        user = auth_service.get_user_by_id(actor.user_id)
        if user is None:
            raise AuthError("Authentication required.")
        response_user = auth_user_read(user)
        if actor.is_service:
            response_user = response_user.model_copy(update={"role": actor.role})
        return Envelope(data=response_user, meta=_auth_me_meta(actor, auth_enabled=True))

    return router


def _ensure_admin(request: Request) -> AuthContext:
    actor = actor_from_request(request)
    if actor.role != Role.ADMIN:
        raise AuthError("Admin privileges required.")
    return actor


def _auth_me_meta(actor: AuthContext, *, auth_enabled: bool) -> dict[str, object]:
    meta: dict[str, object] = {
        "auth_enabled": auth_enabled,
        "principal_type": actor.principal_type.value,
        "is_interactive": actor.is_interactive,
    }
    if actor.device_token_id is not None:
        meta["device_token_id"] = str(actor.device_token_id)
        meta["device_token"] = {
            "device_token_id": str(actor.device_token_id),
            "label": actor.device_token_label,
            "kind": actor.device_token_kind.value if actor.device_token_kind is not None else None,
        }
    if actor.service_token_id is not None:
        meta["service_token"] = {
            "token_id": str(actor.service_token_id),
            "label": actor.service_token_label,
            "role": actor.role.value,
            "read_only": actor.service_token_read_only,
            "expires_at": (
                actor.service_token_expires_at.isoformat()
                if actor.service_token_expires_at is not None
                else None
            ),
        }
    return meta


def _auth_rate_key(request: Request, purpose: str, username: str | None = None) -> str:
    client_host = request.client.host if request.client is not None else "unknown"
    parts = [purpose, client_host]
    if username is not None:
        parts.append(username.strip().lower())
    return ":".join(parts)


def _auth_rate_limiter(request: Request):
    return request.app.state.auth_rate_limiter


def _check_auth_rate_limit(request: Request, key: str) -> None:
    _auth_rate_limiter(request).check(key)


def _record_auth_attempt(request: Request, key: str) -> None:
    _auth_rate_limiter(request).record_attempt(key)


def _record_auth_failure(request: Request, key: str) -> None:
    _auth_rate_limiter(request).record_failure(key)


def _reset_auth_rate_limit(request: Request, key: str) -> None:
    _auth_rate_limiter(request).reset(key)


def _auth_invitation_read(
    invitation: Invitation,
    *,
    invite_url: str | None = None,
    mailto_url: str | None = None,
    warning: str | None = None,
) -> AuthInvitationRead:
    return AuthInvitationRead(
        invitation_id=invitation.invitation_id,
        email=invitation.email,
        role=invitation.role,
        project_id=invitation.project_id,
        project_role=invitation.project_role,
        review_enabled=invitation.review_enabled,
        review_cadence_minutes=invitation.review_cadence_minutes,
        review_run_at_local_time=invitation.review_run_at_local_time,
        review_timezone_name=invitation.review_timezone_name,
        status=invitation.status,
        invite_url=invite_url,
        mailto_url=mailto_url,
        expires_at=invitation.expires_at,
        created_at=invitation.created_at,
        consumed_at=invitation.consumed_at,
        revoked_at=invitation.revoked_at,
        warning=warning,
    )


def _public_base_url_with_warning(request: Request) -> tuple[str, str | None]:
    configured = str(getattr(request.app.state.settings, "public_base_url", "") or "").strip()
    if configured:
        return configured.rstrip("/"), None
    base_url = str(request.base_url).rstrip("/")
    hostname = urlparse(base_url).hostname or ""
    if _host_needs_public_base_url_warning(hostname):
        return (
            base_url,
            "Invitation link uses a local or private host. Set LAB_TRACKER_PUBLIC_BASE_URL "
            "to a reachable lab URL before sending it off-machine.",
        )
    return base_url, None


def _host_needs_public_base_url_warning(hostname: str) -> bool:
    host = hostname.strip().lower()
    if host in {"", "localhost", "0.0.0.0"} or host.endswith(".local"):
        return True
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return False
    return address.is_loopback or address.is_private or address.is_link_local


def _client_is_local(request: Request) -> bool:
    """True only when the real connection peer is a loopback/private/link-local IP.

    The peer comes from the transport (request.client), not the client-controlled
    Host header, so it cannot be spoofed by a remote attacker. Behind a reverse
    proxy this is the proxy's address; configure trusted proxy headers (or use the
    ``never``/``first_run`` disclosure modes) for hosted deployments.
    """
    client = request.client
    if client is None:
        return False
    host = (client.host or "").strip().lower()
    if not host:
        return False
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return False
    return address.is_loopback or address.is_private or address.is_link_local


def _bootstrap_token_for_status(
    request: Request,
    *,
    bootstrap_token: str,
    has_users: bool,
) -> tuple[str | None, str | None]:
    if has_users or not bootstrap_token:
        return None, None
    mode = (
        str(
            getattr(
                request.app.state.settings,
                "bootstrap_admin_token_disclosure",
                "local",
            )
            or "local"
        )
        .strip()
        .lower()
    )
    if mode == "never":
        return (
            None,
            "First-admin token display is disabled for this deployment.",
        )
    if mode == "first_run":
        return bootstrap_token, None
    # Default ('local') mode: disclose only to a real local/private-network peer.
    # The trust boundary MUST come from the connection peer (request.client.host),
    # never the client-controlled Host header — otherwise a remote attacker on an
    # internet-exposed deploy can send `Host: 127.0.0.1`, read the token, and seize
    # the first admin.
    if _client_is_local(request):
        return bootstrap_token, None
    return (
        None,
        "First-admin token display is available only from a local, LAN, or VPN "
        "address for this deployment.",
    )


def _mailto_invitation_url(
    *,
    email: str,
    invite_url: str,
    role: Role,
    expires_at: datetime,
    project_role: ProjectMembershipRole | None = None,
    review_enabled: bool = False,
    review_run_at_local_time: str | None = None,
    review_timezone_name: str | None = None,
) -> str:
    subject = "Lab Tracker invitation"
    project_line = (
        f"This invite includes {project_role.value} access to a shared project.\n\n"
        if project_role is not None
        else ""
    )
    review_line = (
        "Your daily review will run at "
        f"{review_run_at_local_time} ({review_timezone_name}).\n\n"
        if review_enabled
        else ""
    )
    body = (
        "You have been invited to Lab Tracker.\n\n"
        f"{project_line}"
        f"{review_line}"
        f"Open this link to create your password and sign in as {role.value}:\n"
        f"{invite_url}\n\n"
        f"This invitation expires at {expires_at.isoformat()}."
    )
    return f"mailto:{quote(email)}?{urlencode({'subject': subject, 'body': body})}"


def _validate_invitation_timezone(timezone_name: str) -> None:
    try:
        ZoneInfo(timezone_name)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ValidationError("review_timezone_name must be a valid IANA timezone.") from exc


def _provision_invited_user(
    api: LabTrackerAPI,
    *,
    user: User,
    invitation: InvitationClaims,
) -> None:
    if invitation.project_id is None:
        return
    project_role = invitation.project_role or ProjectMembershipRole.CONTRIBUTOR
    invitation_actor = AuthContext(user_id=user.user_id, role=Role.ADMIN)
    api.upsert_project_membership(
        invitation.project_id,
        user.user_id,
        project_role,
        actor=invitation_actor,
    )
    if not invitation.review_enabled:
        return
    member_actor = AuthContext(user_id=user.user_id, role=user.role)
    api.update_graph_draft_batch_settings(
        invitation.project_id,
        enabled=True,
        cadence_minutes=invitation.review_cadence_minutes or 24 * 60,
        run_at_local_time=invitation.review_run_at_local_time or "18:00",
        timezone_name=invitation.review_timezone_name or "America/New_York",
        user_id=user.user_id,
        actor=member_actor,
    )
