"""Authentication routes."""

from __future__ import annotations

import hmac
import ipaddress
from datetime import datetime, timezone
from urllib.parse import quote, urlencode, urlparse
from uuid import UUID

from fastapi import APIRouter
from starlette import status as http_status
from starlette.requests import Request

from lab_tracker.auth import (
    LOCAL_AUTH_USERNAME,
    AuthService,
    Invitation,
    InvitationTokenService,
    PrincipalType,
    Role,
    TokenService,
    extract_bearer_token,
    resolve_session_user,
)
from lab_tracker.db_types import ensure_uuid
from lab_tracker.errors import AuthError, PermissionDeniedError
from lab_tracker.instance_url import build_instance_url
from lab_tracker.patching import provided_fields
from lab_tracker.rate_limit import InMemoryRateLimiter
from lab_tracker.schemas import (
    AuthBootstrapStatus,
    AuthInvitationCreate,
    AuthInvitationRead,
    AuthLoginRequest,
    AuthRegisterRequest,
    AuthSetupReadiness,
    AuthTokenRead,
    AuthUserRead,
    AuthUserUpdate,
    Envelope,
    ListEnvelope,
)

from .shared import (
    actor_from_authorization_header,
    actor_from_request,
    auth_token_read,
    auth_user_read,
    list_response,
    paginate,
    validate_pagination,
)


def build_auth_router(
    *,
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
        # Registration has its own limiter so a flood of blocked login buckets
        # can never lock signup, invitation acceptance or the first admin out.
        _register_rate_limiter(request).record_attempt(
            _auth_rate_key(request, "register"),
            client=_rate_limit_client(request),
        )
        registration_role = payload.role
        username = payload.username
        if payload.invite_token:
            user = auth_service.register_invited_user(
                invitation_token_service=invitation_token_service,
                invite_token=payload.invite_token,
                username=payload.username,
                password=payload.password,
                password_confirmation=payload.password_confirmation or "",
            )
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
                    raise PermissionDeniedError(
                        "Admin privileges required to register non-viewer users."
                    )
        elif not request.app.state.settings.auth_public_viewer_registration_enabled:
            if not request.headers.get("authorization"):
                raise AuthError("Public viewer registration is disabled.")
            actor = actor_from_authorization_header(
                request,
                auth_service=auth_service,
                token_service=token_service,
            )
            if actor.role != Role.ADMIN:
                raise PermissionDeniedError("Public viewer registration is disabled.")
        if not payload.invite_token:
            user = auth_service.register_user(
                username=username,
                password=payload.password,
                role=registration_role,
            )
        token = token_service.issue_access_token(user)
        return Envelope(data=auth_token_read(user, token.token, token.expires_at))

    @router.post(
        "/auth/invitations",
        response_model=Envelope[AuthInvitationRead],
        status_code=http_status.HTTP_201_CREATED,
    )
    def create_auth_invitation(payload: AuthInvitationCreate, request: Request):
        _ensure_admin(request)
        email = invitation_token_service.normalize_email(payload.email)
        issued = invitation_token_service.issue_invitation(
            email=email,
            role=payload.role,
        )
        base_url, warning = _public_base_url_with_warning(request)
        invite_token = issued.token
        # Keep the invitation secret in the URL fragment so it is never sent to
        # a server or captured in referrers/logs, while still composing the
        # origin through the normalized instance-URL helper.
        app_url = build_instance_url(base_url, "/app")
        invite_fragment = urlencode({"invite": invite_token, "email": email})
        invite_url = f"{app_url}#{invite_fragment}"
        mailto_url = _mailto_invitation_url(
            email=email,
            invite_url=invite_url,
            role=payload.role,
            expires_at=issued.invitation.expires_at,
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
            **provided_fields(payload),
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
        if actor.principal_type is not PrincipalType.USER:
            raise PermissionDeniedError("Token refresh requires a user session.")
        claims, user = resolve_session_user(
            extract_bearer_token(request.headers.get("authorization")),
            token_service=token_service,
            auth_service=auth_service,
        )
        # Carry the original sign-in time so refresh cannot outlive the
        # absolute session lifetime.
        token = token_service.issue_access_token(user, auth_time=claims.auth_time)
        return Envelope(data=auth_token_read(user, token.token, token.expires_at))

    @router.post("/auth/sessions/revoke", response_model=Envelope[AuthUserRead])
    def revoke_auth_sessions(request: Request):
        """Sign out everywhere: invalidate every session JWT of the caller."""

        if not request.app.state.auth_enabled:
            raise AuthError("Session revocation is unavailable when authentication is disabled.")
        actor = actor_from_request(request)
        if actor.principal_type is not PrincipalType.USER:
            raise PermissionDeniedError("Session revocation requires a user session.")
        user = auth_service.revoke_sessions(actor.user_id)
        return Envelope(data=auth_user_read(user))

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
            return Envelope(data=user, meta={"auth_enabled": False})
        user = auth_service.get_user_by_id(actor.user_id)
        if user is None:
            raise AuthError("Authentication required.")
        return Envelope(data=auth_user_read(user), meta={"auth_enabled": True})

    @router.get(
        "/auth/setup-readiness",
        response_model=Envelope[AuthSetupReadiness],
    )
    def auth_setup_readiness(request: Request):
        actor_from_request(request)
        settings = request.app.state.settings
        provider, credential_configured = _graph_draft_provider_readiness(settings)
        scheduler_enabled = bool(settings.graph_draft_scheduler_enabled)
        return Envelope(
            data=AuthSetupReadiness(
                scheduler_enabled=scheduler_enabled,
                background_worker_enabled=bool(
                    settings.graph_draft_background_enabled or scheduler_enabled
                ),
                provider=provider,
                provider_credential_configured=credential_configured,
                source_revision=settings.source_revision,
            )
        )

    return router


def _graph_draft_provider_readiness(settings) -> tuple[str, bool]:
    provider = (settings.graph_draft_provider or "openai").strip().lower()
    provider_aliases = {
        "claude": "anthropic",
        "gemini": "google",
        "agentic-openai": "agentic",
        "agentic_openai": "agentic",
    }
    provider = provider_aliases.get(provider, provider)
    credential_fields = {
        "openai": "openai_api_key",
        "anthropic": "anthropic_api_key",
        "google": "google_api_key",
        "agentic": "openai_api_key",
    }
    credential_field = credential_fields.get(provider)
    if credential_field is None:
        return provider, False
    credential = getattr(settings, credential_field, "")
    return provider, bool(str(credential or "").strip())


def _ensure_admin(request: Request) -> None:
    actor = actor_from_request(request)
    if actor.role != Role.ADMIN:
        raise PermissionDeniedError("Admin privileges required.")


def _rate_limit_client(request: Request) -> str:
    return request.client.host if request.client is not None else "unknown"


def _auth_rate_key(request: Request, purpose: str, username: str | None = None) -> str:
    parts = [purpose, _rate_limit_client(request)]
    if username is not None:
        parts.append(username.strip().lower())
    return ":".join(parts)


def _auth_rate_limiter(request: Request) -> InMemoryRateLimiter:
    limiter: InMemoryRateLimiter = request.app.state.auth_rate_limiter
    return limiter


def _register_rate_limiter(request: Request) -> InMemoryRateLimiter:
    limiter: InMemoryRateLimiter = request.app.state.register_rate_limiter
    return limiter


def _check_auth_rate_limit(request: Request, key: str) -> None:
    _auth_rate_limiter(request).check(key)


def _record_auth_failure(request: Request, key: str) -> None:
    # Login buckets are keyed by attacker-chosen usernames, so each one is
    # charged to the peer's per-client share of the table.
    _auth_rate_limiter(request).record_failure(key, client=_rate_limit_client(request))


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
    configured = request.app.state.settings.resolved_base_url()
    if configured:
        return configured, None
    base_url = build_instance_url(str(request.base_url), "")
    hostname = urlparse(base_url).hostname or ""
    if _host_needs_public_base_url_warning(hostname):
        return (
            base_url,
            "Invitation link uses a local or private host. Set LAB_TRACKER_BASE_URL "
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
    proxy or Docker network this is the proxy's or gateway's private address,
    so this check is only consulted in LAB_TRACKER_ENVIRONMENT=local.
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
    mode = request.app.state.settings.effective_bootstrap_admin_token_disclosure()
    if mode == "never":
        return None, _BOOTSTRAP_TOKEN_HIDDEN_WARNING
    if mode == "first_run":
        return bootstrap_token, None
    # 'local' mode (the default, and only allowed, in LAB_TRACKER_ENVIRONMENT=local):
    # disclose only to a real local/private-network peer. The trust boundary MUST
    # come from the connection peer (request.client.host), never the
    # client-controlled Host header — otherwise a remote attacker can send
    # `Host: 127.0.0.1`, read the token, and seize the first admin. Outside the
    # local environment the peer is typically a proxy or Docker gateway with a
    # private address, which is why Settings rejects this mode there.
    if _client_is_local(request):
        return bootstrap_token, None
    return (
        None,
        "First-admin token display is available only from a local, LAN, or VPN "
        "address for this deployment.",
    )


_BOOTSTRAP_TOKEN_HIDDEN_WARNING = (
    "First-admin token display is disabled for this deployment. Paste the "
    "LAB_TRACKER_BOOTSTRAP_ADMIN_TOKEN value; Docker deployments that generate it "
    "store it in /app/data/runtime-env/bootstrap-admin-token inside the app container."
)


def _mailto_invitation_url(
    *,
    email: str,
    invite_url: str,
    role: Role,
    expires_at: datetime,
) -> str:
    subject = "Lab Tracker invitation"
    body = (
        "You have been invited to Lab Tracker.\n\n"
        f"Open this link to create your password and sign in as {role.value}:\n"
        f"{invite_url}\n\n"
        "After sign-in, a guided setup will help you choose a project, set your "
        "daily review time, and connect Lab Tracker to your coding agent.\n\n"
        f"This invitation expires at {expires_at.isoformat()}."
    )
    return f"mailto:{quote(email)}?{urlencode({'subject': subject, 'body': body})}"
