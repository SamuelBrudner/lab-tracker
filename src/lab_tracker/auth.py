"""Authentication and authorization helpers."""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import os
import secrets
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum
from uuid import UUID, uuid4

from sqlalchemy import select, update
from sqlalchemy.orm import Session, sessionmaker

from lab_tracker.db_models import (
    DeviceEnrollmentModel,
    DeviceTokenModel,
    InvitationModel,
    PersonalAccessTokenModel,
    UserModel,
)
from lab_tracker.errors import AuthError, ConflictError, NotFoundError, ValidationError

LOCAL_AUTH_USER_ID = UUID("00000000-0000-4000-8000-000000000001")
LOCAL_AUTH_USERNAME = "local-tester"
LOCAL_AUTH_PASSWORD_HASH = "local-auth-disabled"
MIN_PASSWORD_LENGTH = 6


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class Role(str, Enum):
    ADMIN = "admin"
    EDITOR = "editor"
    VIEWER = "viewer"


class PrincipalType(str, Enum):
    USER = "user"
    DEVICE = "device"
    SERVICE = "service"
    SYSTEM = "system"


@dataclass(frozen=True)
class AuthContext:
    user_id: UUID
    role: Role
    principal_type: PrincipalType = PrincipalType.USER
    device_token_id: UUID | None = None

    @property
    def is_device(self) -> bool:
        return self.principal_type == PrincipalType.DEVICE

    @property
    def is_service(self) -> bool:
        return self.principal_type == PrincipalType.SERVICE

    @property
    def is_system(self) -> bool:
        return self.principal_type == PrincipalType.SYSTEM

    @property
    def is_interactive(self) -> bool:
        """Whether a human drove this request.

        Accept and commit gates require an interactive principal so an
        unattended automation actor (``SYSTEM``) can DRAFT but never launder AI
        proposals into the committed graph. Allow-list rather than deny-list so
        a future non-interactive principal type is fail-closed (excluded) until
        it is deliberately admitted here.
        """
        return self.principal_type in {
            PrincipalType.USER,
            PrincipalType.DEVICE,
            PrincipalType.SERVICE,
        }


@dataclass
class User:
    user_id: UUID
    username: str
    password_hash: str
    role: Role
    created_at: datetime = field(default_factory=utc_now)


class PasswordHasher:
    algorithm = "pbkdf2_sha256"
    iterations = 120_000
    salt_bytes = 16

    @classmethod
    def hash_password(cls, password: str) -> str:
        if not password or not password.strip():
            raise ValidationError("Password must not be empty.")
        if len(password) < MIN_PASSWORD_LENGTH:
            raise ValidationError(
                f"Password must be at least {MIN_PASSWORD_LENGTH} characters long."
            )
        salt = os.urandom(cls.salt_bytes)
        digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, cls.iterations)
        return f"{cls.algorithm}${cls.iterations}${salt.hex()}${digest.hex()}"

    @classmethod
    def verify_password(cls, password: str, encoded: str) -> bool:
        try:
            algorithm, iterations, salt_hex, digest_hex = encoded.split("$")
        except ValueError:
            return False
        if algorithm != cls.algorithm:
            return False
        try:
            iterations_int = int(iterations)
        except ValueError:
            return False
        try:
            salt = bytes.fromhex(salt_hex)
            expected = bytes.fromhex(digest_hex)
        except ValueError:
            return False
        computed = hashlib.pbkdf2_hmac(
            "sha256",
            password.encode("utf-8"),
            salt,
            iterations_int,
        )
        return hmac.compare_digest(computed, expected)


class AuthService:
    """Authentication user store with optional SQLAlchemy persistence."""

    def __init__(self, session_factory: sessionmaker[Session] | None = None) -> None:
        self._session_factory = session_factory
        self._users_by_username: dict[str, User] = {}

    def register_user(self, username: str, password: str, role: Role) -> User:
        normalized = self._normalize_username(username)
        password_hash = PasswordHasher.hash_password(password)
        if self._session_factory is None:
            if normalized in self._users_by_username:
                raise ConflictError("Username already exists.")
            user = User(
                user_id=uuid4(),
                username=normalized,
                password_hash=password_hash,
                role=role,
            )
            self._users_by_username[normalized] = user
            return user

        with self._session_factory() as session:
            existing = session.scalar(select(UserModel).where(UserModel.username == normalized))
            if existing is not None:
                raise ConflictError("Username already exists.")
            user_row = UserModel(
                user_id=str(uuid4()),
                username=normalized,
                password_hash=password_hash,
                role=role.value,
                created_at=utc_now(),
            )
            session.add(user_row)
            session.commit()
            return _user_from_model(user_row)

    def authenticate(self, username: str, password: str) -> User:
        normalized = self._normalize_username(username)
        user = self.get_user(normalized)
        if user is None:
            raise AuthError("Invalid credentials.")
        if not PasswordHasher.verify_password(password, user.password_hash):
            raise AuthError("Invalid credentials.")
        return user

    def get_user(self, username: str) -> User | None:
        normalized = self._normalize_username(username)
        if self._session_factory is None:
            return self._users_by_username.get(normalized)
        with self._session_factory() as session:
            row = session.scalar(select(UserModel).where(UserModel.username == normalized))
            if row is None:
                return None
            return _user_from_model(row)

    def get_user_by_id(self, user_id: UUID) -> User | None:
        if self._session_factory is None:
            for user in self._users_by_username.values():
                if user.user_id == user_id:
                    return user
            return None
        with self._session_factory() as session:
            row = session.get(UserModel, str(user_id))
            if row is None:
                return None
            return _user_from_model(row)

    def list_users(self) -> list[User]:
        if self._session_factory is None:
            return sorted(self._users_by_username.values(), key=lambda user: user.username)
        with self._session_factory() as session:
            rows = list(session.scalars(select(UserModel).order_by(UserModel.username)))
            return [_user_from_model(row) for row in rows]

    def update_user(
        self,
        user_id: UUID,
        *,
        role: Role | None = None,
        password: str | None = None,
    ) -> User:
        if role is None and password is None:
            raise ValidationError("A role or password update is required.")

        if self._session_factory is None:
            user = self.get_user_by_id(user_id)
            if user is None:
                raise NotFoundError("User does not exist.")
            if role is not None:
                self._ensure_not_demoting_last_admin(user, role, self.list_users())
                user.role = role
            if password is not None:
                user.password_hash = PasswordHasher.hash_password(password)
            return user

        with self._session_factory() as session:
            row = session.get(UserModel, str(user_id))
            if row is None:
                raise NotFoundError("User does not exist.")
            if role is not None:
                users = [_user_from_model(item) for item in session.scalars(select(UserModel))]
                current = _user_from_model(row)
                self._ensure_not_demoting_last_admin(current, role, users)
                row.role = role.value
            if password is not None:
                row.password_hash = PasswordHasher.hash_password(password)
            session.commit()
            session.refresh(row)
            return _user_from_model(row)

    def has_users(self) -> bool:
        if self._session_factory is None:
            return bool(self._users_by_username)
        with self._session_factory() as session:
            return session.scalar(select(UserModel.user_id).limit(1)) is not None

    @staticmethod
    def _normalize_username(username: str) -> str:
        if not username or not username.strip():
            raise ValidationError("Username must not be empty.")
        return username.strip().lower()

    @staticmethod
    def _ensure_not_demoting_last_admin(
        user: User,
        next_role: Role,
        users: Iterable[User],
    ) -> None:
        if user.role != Role.ADMIN or next_role == Role.ADMIN:
            return
        admin_count = sum(1 for item in users if item.role == Role.ADMIN)
        if admin_count <= 1:
            raise ValidationError("At least one admin account is required.")


def ensure_local_auth_user(session_factory: sessionmaker[Session]) -> User:
    """Persist the synthetic local-auth user for DB-backed features.

    Auth-disabled local mode still needs a user row for foreign-keyed surfaces
    such as paired device tokens. The placeholder password hash is intentionally
    not a valid PasswordHasher value, so this account cannot log in if auth is
    later enabled against the same local database.
    """

    with session_factory() as session:
        row = session.get(UserModel, str(LOCAL_AUTH_USER_ID))
        if row is None:
            row = UserModel(
                user_id=str(LOCAL_AUTH_USER_ID),
                username=LOCAL_AUTH_USERNAME,
                password_hash=LOCAL_AUTH_PASSWORD_HASH,
                role=Role.ADMIN.value,
                created_at=utc_now(),
            )
            session.add(row)
        else:
            row.username = LOCAL_AUTH_USERNAME
            row.password_hash = LOCAL_AUTH_PASSWORD_HASH
            row.role = Role.ADMIN.value
        session.commit()
        return _user_from_model(row)


@dataclass(frozen=True)
class AccessToken:
    token: str
    expires_at: datetime


@dataclass(frozen=True)
class TokenClaims:
    user_id: UUID
    role: Role
    expires_at: datetime
    issued_at: datetime


@dataclass(frozen=True)
class InvitationClaims:
    invitation_id: UUID
    email: str
    role: Role
    expires_at: datetime
    issued_at: datetime


@dataclass(frozen=True)
class Invitation:
    invitation_id: UUID
    email: str
    role: Role
    expires_at: datetime
    created_at: datetime
    consumed_at: datetime | None = None
    consumed_by_user_id: UUID | None = None
    revoked_at: datetime | None = None

    @property
    def status(self) -> str:
        if self.revoked_at is not None:
            return "revoked"
        if self.consumed_at is not None:
            return "consumed"
        if self.expires_at <= utc_now():
            return "expired"
        return "pending"


@dataclass(frozen=True)
class IssuedInvitation:
    invitation: Invitation
    token: str


class TokenService:
    """HMAC-signed JWT-style token issuer and verifier."""

    def __init__(self, secret_key: str, *, ttl_minutes: int = 60) -> None:
        if not secret_key or not secret_key.strip():
            raise ValidationError("auth_secret_key must not be empty.")
        if ttl_minutes < 1:
            raise ValidationError("auth_token_ttl_minutes must be at least 1.")
        self._secret = secret_key.encode("utf-8")
        self._ttl_minutes = ttl_minutes

    def issue_access_token(self, user: User) -> AccessToken:
        issued_at = utc_now()
        expires_at = issued_at + timedelta(minutes=self._ttl_minutes)
        header = {"alg": "HS256", "typ": "JWT"}
        payload = {
            "sub": str(user.user_id),
            "role": user.role.value,
            "iat": int(issued_at.timestamp()),
            "exp": int(expires_at.timestamp()),
        }
        header_segment = _b64url_encode_json(header)
        payload_segment = _b64url_encode_json(payload)
        signature = self._sign(f"{header_segment}.{payload_segment}".encode())
        token = f"{header_segment}.{payload_segment}.{_b64url_encode(signature)}"
        return AccessToken(token=token, expires_at=expires_at)

    def verify_access_token(self, token: str) -> TokenClaims:
        _ensure_non_empty(token, "token")
        parts = token.split(".")
        if len(parts) != 3:
            raise AuthError("Invalid token.")
        header_segment, payload_segment, signature_segment = parts
        signing_input = f"{header_segment}.{payload_segment}".encode()
        expected_signature = self._sign(signing_input)
        provided_signature = _b64url_decode(signature_segment)
        if not hmac.compare_digest(expected_signature, provided_signature):
            raise AuthError("Invalid token.")
        payload = _b64url_decode_json(payload_segment)
        try:
            user_id = UUID(str(payload["sub"]))
            role = Role(str(payload["role"]))
            issued_at = datetime.fromtimestamp(int(payload["iat"]), tz=timezone.utc)
            expires_at = datetime.fromtimestamp(int(payload["exp"]), tz=timezone.utc)
        except (KeyError, ValueError, TypeError) as exc:
            raise AuthError("Invalid token.") from exc
        if expires_at <= utc_now():
            raise AuthError("Token has expired.")
        return TokenClaims(
            user_id=user_id,
            role=role,
            expires_at=expires_at,
            issued_at=issued_at,
        )

    def _sign(self, data: bytes) -> bytes:
        return hmac.new(self._secret, data, hashlib.sha256).digest()


class InvitationTokenService:
    """Persistent single-use invitation issuer and verifier."""

    def __init__(
        self,
        secret_key: str,
        *,
        ttl_hours: int = 168,
        session_factory: sessionmaker[Session] | None = None,
    ) -> None:
        if not secret_key or not secret_key.strip():
            raise ValidationError("auth_secret_key must not be empty.")
        if ttl_hours < 1:
            raise ValidationError("auth_invite_ttl_hours must be at least 1.")
        self._secret = secret_key.encode("utf-8")
        self._ttl_hours = ttl_hours
        self._session_factory = session_factory
        self._memory_invitations_by_hash: dict[str, InvitationModel] = {}

    def issue_invitation(self, *, email: str, role: Role) -> IssuedInvitation:
        issued_at = utc_now()
        expires_at = issued_at + timedelta(hours=self._ttl_hours)
        token = _generate_secret(INVITATION_TOKEN_PREFIX)
        token_hash = _hash_token(token)
        invitation_id = uuid4()
        row = InvitationModel(
            invitation_id=str(invitation_id),
            email=self.normalize_email(email),
            role=role.value,
            token_hash=token_hash,
            expires_at=expires_at,
            created_at=issued_at,
        )
        if self._session_factory is None:
            self._memory_invitations_by_hash[token_hash] = row
            return IssuedInvitation(invitation=_invitation_from_model(row), token=token)

        with self._session_factory() as session:
            session.add(row)
            session.commit()
            session.refresh(row)
            return IssuedInvitation(invitation=_invitation_from_model(row), token=token)

    def issue_invitation_token(self, *, email: str, role: Role) -> tuple[str, datetime]:
        issued = self.issue_invitation(email=email, role=role)
        return issued.token, issued.invitation.expires_at

    def verify_invitation_token(self, token: str) -> InvitationClaims:
        _ensure_non_empty(token, "invite_token")
        invitation = self._invitation_for_token(token)
        self._ensure_invitation_pending(invitation)
        return InvitationClaims(
            invitation_id=invitation.invitation_id,
            email=invitation.email,
            role=invitation.role,
            expires_at=invitation.expires_at,
            issued_at=invitation.created_at,
        )

    def consume_invitation_token(self, token: str, *, consumed_by_user_id: UUID) -> Invitation:
        _ensure_non_empty(token, "invite_token")
        token_hash = _hash_token(token)
        if self._session_factory is None:
            row = self._memory_invitations_by_hash.get(token_hash)
            if row is None:
                raise AuthError("Invitation token is invalid.")
            invitation = _invitation_from_model(row)
            self._ensure_invitation_pending(invitation)
            row.consumed_at = utc_now()
            row.consumed_by_user_id = str(consumed_by_user_id)
            return _invitation_from_model(row)

        with self._session_factory() as session:
            row = session.scalar(
                select(InvitationModel).where(InvitationModel.token_hash == token_hash)
            )
            if row is None:
                raise AuthError("Invitation token is invalid.")
            invitation = _invitation_from_model(row)
            self._ensure_invitation_pending(invitation)
            row.consumed_at = utc_now()
            row.consumed_by_user_id = str(consumed_by_user_id)
            session.commit()
            session.refresh(row)
            return _invitation_from_model(row)

    def list_invitations(self) -> list[Invitation]:
        if self._session_factory is None:
            return sorted(
                (_invitation_from_model(row) for row in self._memory_invitations_by_hash.values()),
                key=lambda invitation: invitation.created_at,
                reverse=True,
            )
        with self._session_factory() as session:
            rows = list(
                session.scalars(
                    select(InvitationModel).order_by(InvitationModel.created_at.desc())
                )
            )
            return [_invitation_from_model(row) for row in rows]

    def revoke_invitation(self, invitation_id: UUID) -> Invitation:
        if self._session_factory is None:
            for row in self._memory_invitations_by_hash.values():
                if row.invitation_id == str(invitation_id):
                    if row.consumed_at is not None:
                        raise ValidationError("Consumed invitations cannot be revoked.")
                    if row.revoked_at is None:
                        row.revoked_at = utc_now()
                    return _invitation_from_model(row)
            raise NotFoundError("Invitation does not exist.")

        with self._session_factory() as session:
            row = session.get(InvitationModel, str(invitation_id))
            if row is None:
                raise NotFoundError("Invitation does not exist.")
            if row.consumed_at is not None:
                raise ValidationError("Consumed invitations cannot be revoked.")
            if row.revoked_at is None:
                row.revoked_at = utc_now()
                session.commit()
                session.refresh(row)
            return _invitation_from_model(row)

    def _invitation_for_token(self, token: str) -> Invitation:
        if not token.startswith(INVITATION_TOKEN_PREFIX):
            raise AuthError("Unrecognized invitation token.")
        token_hash = _hash_token(token)
        if self._session_factory is None:
            row = self._memory_invitations_by_hash.get(token_hash)
            if row is None:
                raise AuthError("Invitation token is invalid.")
            return _invitation_from_model(row)

        with self._session_factory() as session:
            row = session.scalar(
                select(InvitationModel).where(InvitationModel.token_hash == token_hash)
            )
            if row is None:
                raise AuthError("Invitation token is invalid.")
            return _invitation_from_model(row)

    @staticmethod
    def _ensure_invitation_pending(invitation: Invitation) -> None:
        if invitation.revoked_at is not None:
            raise AuthError("Invitation has been revoked.")
        if invitation.consumed_at is not None:
            raise AuthError("Invitation has already been used.")
        if invitation.expires_at <= utc_now():
            raise AuthError("Invitation token has expired.")

    @staticmethod
    def normalize_email(email: str) -> str:
        normalized = (email or "").strip().lower()
        if not normalized or "@" not in normalized or normalized.startswith("@"):
            raise ValidationError("Invite email must be a valid email address.")
        local_part, _, domain = normalized.partition("@")
        if not local_part or "." not in domain or domain.endswith("."):
            raise ValidationError("Invite email must be a valid email address.")
        return normalized

    def _sign(self, data: bytes) -> bytes:
        return hmac.new(self._secret, data, hashlib.sha256).digest()


DEVICE_TOKEN_PREFIX = "ldev_"
ENROLLMENT_OFFER_PREFIX = "lpair_"
INVITATION_TOKEN_PREFIX = "linv_"
LPAT_TOKEN_PREFIX = "lpat_"
DEVICE_LAST_USED_UPDATE_INTERVAL = timedelta(minutes=5)
PERSONAL_ACCESS_TOKEN_LAST_USED_UPDATE_INTERVAL = timedelta(minutes=5)
PERSONAL_ACCESS_TOKEN_MAX_TTL = timedelta(days=90)
_ROLE_RANK = {
    Role.VIEWER: 0,
    Role.EDITOR: 1,
    Role.ADMIN: 2,
}


def device_principal_can_access(method: str, path: str) -> bool:
    """Coarse-grained policy for paired-device principals.

    Devices can read everything (the data is the user's own anyway) but
    can only write to capture endpoints. /auth/* is off-limits in both
    directions so a stolen device cannot enumerate other devices, refresh
    arbitrary sessions, or escalate.
    """
    method = method.upper()
    # Device principals may introspect their own session (/auth/me) so the
    # phone PWA can confirm pairing succeeded without a parallel endpoint;
    # all other /auth/* surfaces stay off-limits.
    if path == "/auth/me":
        return method in {"GET", "HEAD", "OPTIONS"}
    if path.startswith("/auth"):
        return False
    if method in {"GET", "HEAD", "OPTIONS"}:
        return True
    if method != "POST":
        return False
    if path in {"/notes", "/notes/upload-file", "/notes/quick-capture"}:
        return True
    segments = [segment for segment in path.split("/") if segment]
    return len(segments) == 3 and segments[0] == "notes" and segments[2] == "transcript"


def service_principal_can_access(
    method: str,
    path: str,
    *,
    read_only: bool,
    role: Role,
) -> bool:
    """Coarse-grained policy for lpat_ service principals."""
    method = method.upper()
    if path.startswith("/auth"):
        return False
    if method in {"GET", "HEAD", "OPTIONS"}:
        return True
    if read_only:
        return False
    return role in {Role.ADMIN, Role.EDITOR}


@dataclass(frozen=True)
class DeviceToken:
    device_token_id: UUID
    user_id: UUID
    label: str
    created_at: datetime
    last_used_at: datetime | None
    revoked_at: datetime | None

    @property
    def revoked(self) -> bool:
        return self.revoked_at is not None


@dataclass(frozen=True)
class EnrollmentOffer:
    enrollment_id: UUID
    user_id: UUID
    offer_token: str
    expires_at: datetime


@dataclass(frozen=True)
class IssuedDeviceToken:
    device_token: DeviceToken
    secret: str


@dataclass(frozen=True)
class DevicePrincipal:
    user_id: UUID
    device_token_id: UUID
    label: str


@dataclass(frozen=True)
class PersonalAccessToken:
    token_id: UUID
    user_id: UUID
    label: str
    role: Role
    read_only: bool
    expires_at: datetime
    created_at: datetime
    last_used_at: datetime | None
    revoked_at: datetime | None

    @property
    def revoked(self) -> bool:
        return self.revoked_at is not None

    @property
    def expired(self) -> bool:
        return self.expires_at <= utc_now()


@dataclass(frozen=True)
class IssuedPersonalAccessToken:
    token: PersonalAccessToken
    secret: str


@dataclass(frozen=True)
class PersonalAccessTokenPrincipal:
    user_id: UUID
    token_id: UUID
    label: str
    role: Role
    read_only: bool


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _generate_secret(prefix: str) -> str:
    return f"{prefix}{secrets.token_urlsafe(32)}"


def _device_token_from_model(model: DeviceTokenModel) -> DeviceToken:
    return DeviceToken(
        device_token_id=UUID(model.device_token_id),
        user_id=UUID(model.user_id),
        label=model.label,
        created_at=model.created_at,
        last_used_at=model.last_used_at,
        revoked_at=model.revoked_at,
    )


def _personal_access_token_from_model(model: PersonalAccessTokenModel) -> PersonalAccessToken:
    return PersonalAccessToken(
        token_id=UUID(model.token_id),
        user_id=UUID(model.user_id),
        label=model.label,
        role=Role(model.role),
        read_only=bool(model.read_only),
        expires_at=model.expires_at,
        created_at=model.created_at,
        last_used_at=model.last_used_at,
        revoked_at=model.revoked_at,
    )


class DeviceAuthService:
    """Issues, verifies, and revokes long-lived per-device tokens.

    Tokens are stored only as SHA-256 hashes; the raw secret is returned
    exactly once at issuance (or enrollment offer creation) and never
    persisted. Enrollment offers are short-lived single-use grants the
    desktop generates so the paired phone can claim a token without ever
    needing the user's password.
    """

    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        if session_factory is None:
            raise ValidationError("DeviceAuthService requires a session_factory.")
        self._session_factory = session_factory

    def create_enrollment(
        self,
        user_id: UUID,
        *,
        ttl_minutes: int = 5,
    ) -> EnrollmentOffer:
        if ttl_minutes < 1:
            raise ValidationError("Enrollment TTL must be at least one minute.")
        offer_token = _generate_secret(ENROLLMENT_OFFER_PREFIX)
        offer_hash = _hash_token(offer_token)
        expires_at = utc_now() + timedelta(minutes=ttl_minutes)
        enrollment_id = uuid4()
        with self._session_factory() as session:
            user = session.get(UserModel, str(user_id))
            if user is None:
                raise NotFoundError("User does not exist.")
            row = DeviceEnrollmentModel(
                enrollment_id=str(enrollment_id),
                user_id=str(user_id),
                offer_token_hash=offer_hash,
                expires_at=expires_at,
                created_at=utc_now(),
            )
            session.add(row)
            session.commit()
        return EnrollmentOffer(
            enrollment_id=enrollment_id,
            user_id=user_id,
            offer_token=offer_token,
            expires_at=expires_at,
        )

    def consume_enrollment(self, offer_token: str, *, label: str) -> IssuedDeviceToken:
        if not offer_token or not offer_token.strip():
            raise AuthError("Enrollment offer token must not be empty.")
        if not label or not label.strip():
            raise ValidationError("Device label must not be empty.")
        if not offer_token.startswith(ENROLLMENT_OFFER_PREFIX):
            raise AuthError("Unrecognized enrollment offer token.")
        offer_hash = _hash_token(offer_token)
        label = label.strip()
        claim_time = utc_now()
        with self._session_factory() as session:
            claim_result = session.execute(
                update(DeviceEnrollmentModel)
                .where(
                    DeviceEnrollmentModel.offer_token_hash == offer_hash,
                    DeviceEnrollmentModel.consumed_at.is_(None),
                    DeviceEnrollmentModel.expires_at > claim_time,
                )
                .values(consumed_at=claim_time)
            )
            if claim_result.rowcount != 1:
                enrollment = session.scalar(
                    select(DeviceEnrollmentModel).where(
                        DeviceEnrollmentModel.offer_token_hash == offer_hash
                    )
                )
                if enrollment is None:
                    raise AuthError("Enrollment offer is invalid.")
                if enrollment.consumed_at is not None:
                    raise AuthError("Enrollment offer has already been consumed.")
                if _as_utc(enrollment.expires_at) <= claim_time:
                    raise AuthError("Enrollment offer has expired.")
                raise AuthError("Enrollment offer has already been consumed.")

            enrollment = session.scalar(
                select(DeviceEnrollmentModel).where(
                    DeviceEnrollmentModel.offer_token_hash == offer_hash
                )
            )
            if enrollment is None:
                raise AuthError("Enrollment offer is invalid.")
            secret = _generate_secret(DEVICE_TOKEN_PREFIX)
            device_row = DeviceTokenModel(
                device_token_id=str(uuid4()),
                user_id=enrollment.user_id,
                label=label,
                token_hash=_hash_token(secret),
                created_at=utc_now(),
            )
            session.add(device_row)
            session.flush()
            session.execute(
                update(DeviceEnrollmentModel)
                .where(DeviceEnrollmentModel.enrollment_id == enrollment.enrollment_id)
                .values(consumed_device_token_id=device_row.device_token_id)
            )
            session.commit()
            session.refresh(device_row)
            return IssuedDeviceToken(
                device_token=_device_token_from_model(device_row),
                secret=secret,
            )

    def list_devices(self, user_id: UUID) -> list[DeviceToken]:
        with self._session_factory() as session:
            rows = (
                session.scalars(
                    select(DeviceTokenModel)
                    .where(DeviceTokenModel.user_id == str(user_id))
                    .order_by(DeviceTokenModel.created_at)
                )
                .all()
            )
            return [_device_token_from_model(row) for row in rows]

    def revoke_device(self, user_id: UUID, device_token_id: UUID) -> DeviceToken:
        with self._session_factory() as session:
            row = session.get(DeviceTokenModel, str(device_token_id))
            if row is None or row.user_id != str(user_id):
                raise NotFoundError("Device token does not exist.")
            if row.revoked_at is None:
                row.revoked_at = utc_now()
                session.commit()
                session.refresh(row)
            return _device_token_from_model(row)

    def verify_device_token(self, token: str) -> DevicePrincipal | None:
        if not token or not token.startswith(DEVICE_TOKEN_PREFIX):
            return None
        token_hash = _hash_token(token)
        with self._session_factory() as session:
            row = session.scalar(
                select(DeviceTokenModel).where(DeviceTokenModel.token_hash == token_hash)
            )
            if row is None or row.revoked_at is not None:
                return None
            now = utc_now()
            last_used_at = _as_utc(row.last_used_at) if row.last_used_at is not None else None
            if (
                last_used_at is None
                or now - last_used_at >= DEVICE_LAST_USED_UPDATE_INTERVAL
            ):
                row.last_used_at = now
                session.commit()
            return DevicePrincipal(
                user_id=UUID(row.user_id),
                device_token_id=UUID(row.device_token_id),
                label=row.label,
            )


class PersonalAccessTokenService:
    """Issues, verifies, and revokes lpat_ personal access tokens."""

    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        if session_factory is None:
            raise ValidationError("PersonalAccessTokenService requires a session_factory.")
        self._session_factory = session_factory

    def issue_token(
        self,
        user: User,
        *,
        label: str,
        role: Role,
        read_only: bool = True,
        expires_at: datetime,
    ) -> IssuedPersonalAccessToken:
        if not label or not label.strip():
            raise ValidationError("Token label must not be empty.")
        now = utc_now()
        expires_at = _as_utc(expires_at)
        if expires_at <= now:
            raise ValidationError("Token expiration must be in the future.")
        if expires_at - now > PERSONAL_ACCESS_TOKEN_MAX_TTL:
            raise ValidationError("Token expiration exceeds the maximum token TTL.")
        capped_role = _cap_role(role, issuer_role=user.role)
        secret = _generate_secret(LPAT_TOKEN_PREFIX)
        row = PersonalAccessTokenModel(
            token_id=str(uuid4()),
            user_id=str(user.user_id),
            label=label.strip(),
            token_hash=_hash_token(secret),
            role=capped_role.value,
            read_only=bool(read_only),
            expires_at=expires_at,
            created_at=now,
        )
        with self._session_factory() as session:
            if session.get(UserModel, str(user.user_id)) is None:
                raise NotFoundError("User does not exist.")
            session.add(row)
            session.commit()
            session.refresh(row)
            return IssuedPersonalAccessToken(
                token=_personal_access_token_from_model(row),
                secret=secret,
            )

    def list_tokens(self, user_id: UUID) -> list[PersonalAccessToken]:
        with self._session_factory() as session:
            rows = list(
                session.scalars(
                    select(PersonalAccessTokenModel)
                    .where(PersonalAccessTokenModel.user_id == str(user_id))
                    .order_by(PersonalAccessTokenModel.created_at.desc())
                )
            )
            return [_personal_access_token_from_model(row) for row in rows]

    def revoke_token(self, user_id: UUID, token_id: UUID) -> PersonalAccessToken:
        with self._session_factory() as session:
            row = session.get(PersonalAccessTokenModel, str(token_id))
            if row is None or row.user_id != str(user_id):
                raise NotFoundError("Personal access token does not exist.")
            if row.revoked_at is None:
                row.revoked_at = utc_now()
                session.commit()
                session.refresh(row)
            return _personal_access_token_from_model(row)

    def verify_token(self, token: str) -> PersonalAccessTokenPrincipal | None:
        if not token or not token.startswith(LPAT_TOKEN_PREFIX):
            return None
        token_hash = _hash_token(token)
        with self._session_factory() as session:
            row = session.scalar(
                select(PersonalAccessTokenModel).where(
                    PersonalAccessTokenModel.token_hash == token_hash
                )
            )
            now = utc_now()
            if row is None or row.revoked_at is not None or _as_utc(row.expires_at) <= now:
                return None
            last_used_at = _as_utc(row.last_used_at) if row.last_used_at is not None else None
            if (
                last_used_at is None
                or now - last_used_at >= PERSONAL_ACCESS_TOKEN_LAST_USED_UPDATE_INTERVAL
            ):
                row.last_used_at = now
                session.commit()
            return PersonalAccessTokenPrincipal(
                user_id=UUID(row.user_id),
                token_id=UUID(row.token_id),
                label=row.label,
                role=Role(row.role),
                read_only=bool(row.read_only),
            )


def extract_bearer_token(authorization_header: str | None) -> str:
    if authorization_header is None:
        raise AuthError("Missing Authorization header.")
    prefix, _, token = authorization_header.partition(" ")
    if prefix.lower() != "bearer" or not token.strip():
        raise AuthError("Authorization header must use Bearer token.")
    return token.strip()


def require_role(actor: AuthContext | None, allowed_roles: Iterable[Role]) -> None:
    if actor is None:
        raise AuthError("Authentication required.")
    if actor.role not in set(allowed_roles):
        raise AuthError("Insufficient role.")


def _cap_role(role: Role, *, issuer_role: Role) -> Role:
    if _ROLE_RANK[role] <= _ROLE_RANK[issuer_role]:
        return role
    return issuer_role


def _ensure_non_empty(value: str, field_name: str) -> None:
    if not value or not value.strip():
        raise ValidationError(f"{field_name} must not be empty.")


def _b64url_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def _b64url_decode(data: str) -> bytes:
    padding = "=" * ((4 - len(data) % 4) % 4)
    try:
        return base64.urlsafe_b64decode(data + padding)
    except (ValueError, binascii.Error) as exc:
        raise AuthError("Invalid token.") from exc


def _b64url_encode_json(payload: dict[str, object]) -> str:
    packed = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return _b64url_encode(packed)


def _b64url_decode_json(data: str) -> dict[str, object]:
    try:
        decoded = _b64url_decode(data)
        parsed = json.loads(decoded.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise AuthError("Invalid token.") from exc
    if not isinstance(parsed, dict):
        raise AuthError("Invalid token.")
    return parsed


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _optional_as_utc(value: datetime | None) -> datetime | None:
    return None if value is None else _as_utc(value)


def _invitation_from_model(row: InvitationModel) -> Invitation:
    return Invitation(
        invitation_id=UUID(row.invitation_id),
        email=row.email,
        role=Role(row.role),
        expires_at=_as_utc(row.expires_at),
        created_at=_as_utc(row.created_at),
        consumed_at=_optional_as_utc(row.consumed_at),
        consumed_by_user_id=UUID(row.consumed_by_user_id) if row.consumed_by_user_id else None,
        revoked_at=_optional_as_utc(row.revoked_at),
    )


def _user_from_model(row: UserModel) -> User:
    return User(
        user_id=UUID(row.user_id),
        username=row.username,
        password_hash=row.password_hash,
        role=Role(row.role),
        created_at=_as_utc(row.created_at),
    )
