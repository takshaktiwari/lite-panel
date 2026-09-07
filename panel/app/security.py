"""Password hashing, server-side sessions, CSRF tokens and login throttling."""

from __future__ import annotations

import hashlib
import hmac
import logging
import secrets
import string
from datetime import timedelta, timezone
from typing import Optional, Tuple

import bcrypt
from sqlalchemy import select
from sqlalchemy.orm import Session as OrmSession

from app.config import get_settings
from app.models import AdminUser, LoginAttempt, Session, utcnow
from app.validators import ValidationError

logger = logging.getLogger(__name__)

SESSION_COOKIE = "lite_panel_session"

# bcrypt silently ignores anything past 72 bytes, which would make a long
# passphrase weaker than it looks.  We reject instead of truncating.
MAX_PASSWORD_BYTES = 72
MIN_PASSWORD_LENGTH = 12


# --------------------------------------------------------------------------
# Passwords
# --------------------------------------------------------------------------


def hash_password(plain: str) -> str:
    _check_password_length(plain)
    return bcrypt.hashpw(plain.encode("utf-8"), bcrypt.gensalt()).decode("ascii")


def verify_password(plain: str, hashed: str) -> bool:
    if not plain or not hashed:
        return False
    try:
        return bcrypt.checkpw(plain.encode("utf-8"), hashed.encode("ascii"))
    except (ValueError, TypeError):  # malformed hash in the database
        logger.warning("password verification failed against a malformed hash")
        return False


def validate_new_password(plain: str) -> str:
    """Policy for a password the operator chooses themselves."""
    if not isinstance(plain, str) or not plain:
        raise ValidationError("Password is required.")
    if len(plain) < MIN_PASSWORD_LENGTH:
        raise ValidationError(
            f"Password must be at least {MIN_PASSWORD_LENGTH} characters."
        )
    _check_password_length(plain)
    return plain


def _check_password_length(plain: str) -> None:
    if len(plain.encode("utf-8")) > MAX_PASSWORD_BYTES:
        raise ValidationError(
            f"Password must be at most {MAX_PASSWORD_BYTES} bytes."
        )


def generate_password(length: int = 20) -> str:
    """Generate a password for a database or FTP account.

    Restricted to an alphabet that survives a MariaDB grant, a vsftpd config
    and a copy-paste into a client's settings without quoting surprises.
    """
    alphabet = string.ascii_letters + string.digits + "-_"
    return "".join(secrets.choice(alphabet) for _ in range(length))


# --------------------------------------------------------------------------
# Tokens
# --------------------------------------------------------------------------


def generate_token() -> str:
    return secrets.token_urlsafe(32)


def hash_token(token: str) -> str:
    """Sessions are stored hashed so that read access to panel.db is not, by
    itself, a valid login."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def constant_time_equals(a: str, b: str) -> bool:
    """Compare two tokens without leaking their contents through timing.

    A missing value never matches: without this guard two absent CSRF tokens
    would compare equal and a request carrying neither would validate.
    """
    if not a or not b:
        return False
    return hmac.compare_digest(str(a), str(b))


# --------------------------------------------------------------------------
# Sessions
# --------------------------------------------------------------------------


def create_session(
    db: OrmSession,
    user: AdminUser,
    *,
    ip_address: Optional[str] = None,
    user_agent: Optional[str] = None,
) -> Tuple[Session, str]:
    """Create a session and return it with the raw token for the cookie.

    The raw token is returned once and never stored; only its hash is kept.
    """
    settings = get_settings()
    raw_token = generate_token()

    session = Session(
        token_hash=hash_token(raw_token),
        csrf_token=generate_token(),
        user_id=user.id,
        expires_at=utcnow() + timedelta(hours=settings.session_ttl_hours),
        ip_address=ip_address,
        user_agent=(user_agent or "")[:255] or None,
    )
    user.last_login_at = utcnow()
    db.add(session)
    db.commit()
    db.refresh(session)
    return session, raw_token


def get_session(db: OrmSession, raw_token: str) -> Optional[Session]:
    """Look up a live session, deleting it if it has expired."""
    if not raw_token:
        return None

    session = db.scalar(select(Session).where(Session.token_hash == hash_token(raw_token)))
    if session is None:
        return None
    if session.is_expired:
        db.delete(session)
        db.commit()
        return None
    return session


def revoke_session(db: OrmSession, session: Session) -> None:
    db.delete(session)
    db.commit()


def purge_expired_sessions(db: OrmSession) -> int:
    now = utcnow().replace(tzinfo=None)
    stale = db.scalars(select(Session).where(Session.expires_at <= now)).all()
    for session in stale:
        db.delete(session)
    if stale:
        db.commit()
    return len(stale)


# --------------------------------------------------------------------------
# Terminal step-up authentication
# --------------------------------------------------------------------------


def grant_terminal_unlock(db: OrmSession, session: Session) -> None:
    """Re-authenticate the caller for the web terminal.

    Called only after the panel password has been verified again -- see
    routers/terminal.py. The grant rides on the existing session row rather
    than a new token, and expires on its own; there is deliberately no way to
    extend it other than entering the password again.
    """
    settings = get_settings()
    session.terminal_unlocked_until = utcnow() + timedelta(minutes=settings.terminal_unlock_minutes)
    db.commit()


def revoke_terminal_unlock(db: OrmSession, session: Session) -> None:
    """End the step-up grant early, e.g. when a terminal is explicitly closed."""
    session.terminal_unlocked_until = None
    db.commit()


# --------------------------------------------------------------------------
# Login throttling
# --------------------------------------------------------------------------


def is_locked_out(db: OrmSession, ip_address: str) -> bool:
    record = db.scalar(select(LoginAttempt).where(LoginAttempt.ip_address == ip_address))
    if record is None or record.locked_until is None:
        return False

    locked_until = record.locked_until
    if locked_until.tzinfo is None:
        locked_until = locked_until.replace(tzinfo=timezone.utc)

    if locked_until <= utcnow():
        record.attempts = 0
        record.locked_until = None
        db.commit()
        return False
    return True


def record_failed_login(db: OrmSession, ip_address: str) -> None:
    settings = get_settings()
    record = db.scalar(select(LoginAttempt).where(LoginAttempt.ip_address == ip_address))
    if record is None:
        record = LoginAttempt(ip_address=ip_address, attempts=0)
        db.add(record)

    window_start = record.window_start
    if window_start and window_start.tzinfo is None:
        window_start = window_start.replace(tzinfo=timezone.utc)

    # Restart the count if the previous window has passed.
    if window_start and utcnow() - window_start > timedelta(minutes=settings.login_lockout_minutes):
        record.attempts = 0
        record.window_start = utcnow()

    record.attempts += 1
    if record.attempts >= settings.login_max_attempts:
        record.locked_until = utcnow() + timedelta(minutes=settings.login_lockout_minutes)
        logger.warning("locked out %s after %s failed logins", ip_address, record.attempts)
    db.commit()


def clear_failed_logins(db: OrmSession, ip_address: str) -> None:
    record = db.scalar(select(LoginAttempt).where(LoginAttempt.ip_address == ip_address))
    if record is not None:
        db.delete(record)
        db.commit()


# --------------------------------------------------------------------------
# Bootstrap
# --------------------------------------------------------------------------


def create_admin(db: OrmSession, username: str, password: str) -> AdminUser:
    """Create the panel's admin account.  Called by install.sh via the CLI."""
    from app.validators import validate_admin_username  # local import: avoids cycle

    username = validate_admin_username(username)
    validate_new_password(password)

    if db.scalar(select(AdminUser).where(AdminUser.username == username)):
        raise ValidationError(f"Admin user '{username}' already exists.")

    user = AdminUser(username=username, password_hash=hash_password(password))
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def authenticate(db: OrmSession, username: str, password: str) -> Optional[AdminUser]:
    """Verify credentials.

    A dummy hash is verified when the user does not exist so that a missing
    account and a wrong password take the same time to answer.
    """
    user = db.scalar(select(AdminUser).where(AdminUser.username == username))
    if user is None or not user.is_active:
        bcrypt.checkpw(b"timing-equalisation", bcrypt.hashpw(b"x", bcrypt.gensalt()))
        return None
    if not verify_password(password, user.password_hash):
        return None
    return user
