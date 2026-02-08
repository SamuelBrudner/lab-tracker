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
from typing import Iterable
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from lab_tracker.db_models import UserModel
from lab_tracker.errors import AuthError, ConflictError, ValidationError


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class Role(str, Enum):
    ADMIN = "admin"
    EDITOR = "editor"
    VIEWER = "viewer"


@dataclass(frozen=True)
class AuthContext:
    user_id: UUID
    role: Role


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
            existing = session.scalar(
                select(UserModel).where(UserModel.username == normalized)
            )
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
