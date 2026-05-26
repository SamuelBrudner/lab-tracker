"""Authentication and authorization helpers."""

from __future__ import annotations

import base64
import binascii
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum
import hashlib
import hmac
import json
import os
import secrets
from typing import Iterable
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from lab_tracker.db_models import DeviceEnrollmentModel, DeviceTokenModel, UserModel
from lab_tracker.errors import AuthError, ConflictError, NotFoundError, ValidationError


LOCAL_AUTH_USER_ID = UUID("00000000-0000-4000-8000-000000000001")
LOCAL_AUTH_USERNAME = "local-tester"
LOCAL_AUTH_PASSWORD_HASH = "local-auth-disabled"


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class Role(str, Enum):
    ADMIN = "admin"
    EDITOR = "editor"
    VIEWER = "viewer"


class PrincipalType(str, Enum):
    USER = "user"
    DEVICE = "device"


@dataclass(frozen=True)
class AuthContext:
    user_id: UUID
    role: Role
    principal_type: PrincipalType = PrincipalType.USER
    device_token_id: UUID | None = None

    @property
    def is_device(self) -> bool:
        return self.principal_type == PrincipalType.DEVICE


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
        if not password:
            raise ValidationError("Password must not be empty.")
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

    def has_users(self) -> bool:
        if self._session_factory is None:
            return bool(self._users_by_username)
        with self._session_factory() as session:
            return session.scalar(select(UserModel.user_id).limit(1)) is not None

    @staticmethod
    def _normalize_username(username: str) -> str:
        if not username or not username.strip():
            raise ValidationError("Username must not be empty.")
        return username.strip()


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
        signature = self._sign(f"{header_segment}.{payload_segment}".encode("utf-8"))
        token = f"{header_segment}.{payload_segment}.{_b64url_encode(signature)}"
        return AccessToken(token=token, expires_at=expires_at)

    def verify_access_token(self, token: str) -> TokenClaims:
        _ensure_non_empty(token, "token")
        parts = token.split(".")
        if len(parts) != 3:
            raise AuthError("Invalid token.")
        header_segment, payload_segment, signature_segment = parts
        signing_input = f"{header_segment}.{payload_segment}".encode("utf-8")
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


DEVICE_TOKEN_PREFIX = "ldev_"
ENROLLMENT_OFFER_PREFIX = "lpair_"


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
    if len(segments) == 3 and segments[0] == "notes" and segments[2] == "transcript":
        return True
    return False


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
        with self._session_factory() as session:
            enrollment = session.scalar(
                select(DeviceEnrollmentModel).where(
                    DeviceEnrollmentModel.offer_token_hash == offer_hash
                )
            )
            if enrollment is None:
                raise AuthError("Enrollment offer is invalid.")
            if enrollment.consumed_at is not None:
                raise AuthError("Enrollment offer has already been consumed.")
            if _as_utc(enrollment.expires_at) <= utc_now():
                raise AuthError("Enrollment offer has expired.")
            secret = _generate_secret(DEVICE_TOKEN_PREFIX)
            device_row = DeviceTokenModel(
                device_token_id=str(uuid4()),
                user_id=enrollment.user_id,
                label=label.strip(),
                token_hash=_hash_token(secret),
                created_at=utc_now(),
            )
            session.add(device_row)
            session.flush()
            enrollment.consumed_at = utc_now()
            enrollment.consumed_device_token_id = device_row.device_token_id
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
            row.last_used_at = utc_now()
            session.commit()
            return DevicePrincipal(
                user_id=UUID(row.user_id),
                device_token_id=UUID(row.device_token_id),
                label=row.label,
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


def _user_from_model(row: UserModel) -> User:
    return User(
        user_id=UUID(row.user_id),
        username=row.username,
        password_hash=row.password_hash,
        role=Role(row.role),
        created_at=_as_utc(row.created_at),
    )
