"""One verified login mailbox per scientist for actionable proposal reviews."""

from __future__ import annotations

import hashlib
import secrets
from datetime import timedelta
from uuid import UUID

from pydantic import BaseModel
from sqlalchemy import or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from lab_tracker.db_models import UserModel
from lab_tracker.errors import ConflictError, NotFoundError, RateLimitError, ValidationError
from lab_tracker.models import utc_now
from lab_tracker.services.review_email_service import normalize_review_email

CHALLENGE_LIFETIME = timedelta(minutes=15)
RESEND_INTERVAL = timedelta(minutes=1)


class ReviewMailboxStatus(BaseModel):
    available: bool
    login_email: str | None
    enabled: bool
    pending_email: str | None


def _login_mailbox(username: str) -> str | None:
    try:
        mailbox = normalize_review_email(username)
    except ValidationError:
        return None
    # Auth normalizes usernames to lowercase. Require one literal addr-spec,
    # excluding display names or comments that the email parser might discard.
    if mailbox != username or len(mailbox) > 150:
        return None
    return mailbox


def get_verified_review_mailbox(session: Session, user_id: UUID) -> str | None:
    """Look up the current opted-in login mailbox, bypassing the ORM identity cache."""
    return session.scalar(
        select(UserModel.review_mailbox_address).where(
            UserModel.user_id == user_id,
            UserModel.review_mailbox_verified_at.is_not(None),
            UserModel.review_mailbox_address == UserModel.username,
        )
    )


def mailbox_status(session: Session, user_id: UUID, *, available: bool) -> ReviewMailboxStatus:
    user = session.get(UserModel, user_id, populate_existing=True)
    if user is None:
        raise NotFoundError("User does not exist.")
    login_email = _login_mailbox(user.username)
    pending = (
        user.review_mailbox_challenge_expires_at is not None
        and user.review_mailbox_challenge_expires_at > utc_now()
        and user.review_mailbox_challenge_hash is not None
    )
    return ReviewMailboxStatus(
        available=available,
        login_email=login_email,
        enabled=login_email is not None
        and user.review_mailbox_address == login_email
        and user.review_mailbox_verified_at is not None,
        pending_email=user.review_mailbox_address if pending else None,
    )


def _challenge_digest(user_id: UUID, username: str, address: str, code: str) -> str:
    return hashlib.sha256(f"{user_id}\0{username}\0{address}\0{code}".encode()).hexdigest()


def issue_challenge(
    session: Session, user_id: UUID, requested_email: str | None
) -> tuple[str, str]:
    user = session.get(UserModel, user_id)
    if user is None:
        raise NotFoundError("User does not exist.")
    username = user.username
    login_email = _login_mailbox(username)
    requested = requested_email.strip().lower() if requested_email is not None else login_email
    if not requested or _login_mailbox(requested) is None:
        raise ValidationError("Enter a valid login email of up to 150 characters.")
    if login_email and requested != login_email:
        raise ValidationError("Use the email address you sign in with.")
    if get_verified_review_mailbox(session, user_id):
        raise ConflictError("Proposal review by email is already enabled.")
    now = utc_now()
    code = secrets.token_hex(12)
    digest = _challenge_digest(user_id, username, requested, code)
    result = session.connection().execute(
        update(UserModel)
        .where(
            UserModel.user_id == user_id,
            UserModel.username == username,
            or_(
                UserModel.review_mailbox_verified_at.is_(None),
                UserModel.review_mailbox_address != username,
            ),
            or_(
                UserModel.review_mailbox_challenge_expires_at.is_(None),
                UserModel.review_mailbox_challenge_expires_at
                <= now + CHALLENGE_LIFETIME - RESEND_INTERVAL,
            ),
        )
        .values(
            review_mailbox_address=requested,
            review_mailbox_verified_at=None,
            review_mailbox_challenge_hash=digest,
            review_mailbox_challenge_expires_at=now + CHALLENGE_LIFETIME,
        )
    )
    if result.rowcount != 1:
        raise RateLimitError("Wait a minute before requesting another verification code.")
    session.commit()
    return requested, code


def confirm_challenge(session: Session, user_id: UUID, code: str) -> None:
    user = session.get(UserModel, user_id)
    if user is None or not user.review_mailbox_address:
        raise ValidationError("The verification code is invalid or expired.")
    username = user.username
    address = user.review_mailbox_address
    digest = _challenge_digest(user_id, username, address, code.strip().lower())
    now = utc_now()
    try:
        result = session.connection().execute(
            update(UserModel)
            .where(
                UserModel.user_id == user_id,
                UserModel.username == username,
                UserModel.review_mailbox_address == address,
                UserModel.review_mailbox_challenge_hash == digest,
                UserModel.review_mailbox_challenge_expires_at > now,
                UserModel.review_mailbox_verified_at.is_(None),
            )
            .values(
                # Legacy logins adopt the proved address only on successful confirmation.
                username=address,
                review_mailbox_verified_at=now,
                review_mailbox_challenge_hash=None,
                review_mailbox_challenge_expires_at=None,
            )
        )
        if result.rowcount != 1:
            raise ValidationError("The verification code is invalid or expired.")
        session.commit()
    except IntegrityError as exc:
        session.rollback()
        raise ConflictError("That login email already belongs to another account.") from exc


def disable_mailbox(session: Session, user_id: UUID) -> None:
    session.execute(
        update(UserModel)
        .where(UserModel.user_id == user_id)
        .values(
            review_mailbox_address=None,
            review_mailbox_verified_at=None,
            review_mailbox_challenge_hash=None,
            review_mailbox_challenge_expires_at=None,
        )
    )
    session.commit()
