"""Authentication and authorization helpers."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
import hashlib
import hmac
import os
from enum import Enum
from typing import Iterable
from uuid import UUID, uuid4

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
        salt = bytes.fromhex(salt_hex)
        expected = bytes.fromhex(digest_hex)
        computed = hashlib.pbkdf2_hmac(
            "sha256",
            password.encode("utf-8"),
            salt,
            iterations_int,
        )
        return hmac.compare_digest(computed, expected)


class AuthService:
    def __init__(self) -> None:
        self._users_by_username: dict[str, User] = {}

    def register_user(self, username: str, password: str, role: Role) -> User:
        self._validate_username(username)
        if username in self._users_by_username:
            raise ConflictError("Username already exists.")
        password_hash = PasswordHasher.hash_password(password)
        user = User(user_id=uuid4(), username=username, password_hash=password_hash, role=role)
        self._users_by_username[username] = user
        return user

    def authenticate(self, username: str, password: str) -> User:
        self._validate_username(username)
        user = self._users_by_username.get(username)
        if user is None:
            raise AuthError("Invalid credentials.")
        if not PasswordHasher.verify_password(password, user.password_hash):
            raise AuthError("Invalid credentials.")
        return user

    def get_user(self, username: str) -> User | None:
        return self._users_by_username.get(username)

    @staticmethod
    def _validate_username(username: str) -> None:
        if not username or not username.strip():
            raise ValidationError("Username must not be empty.")


def require_role(actor: AuthContext | None, allowed_roles: Iterable[Role]) -> None:
    if actor is None:
        raise AuthError("Authentication required.")
    if actor.role not in set(allowed_roles):
        raise AuthError("Insufficient role.")
