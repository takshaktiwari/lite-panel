"""Tests for password hashing, sessions and login throttling."""

import pytest

from app import security
from app.models import Session
from app.validators import ValidationError


# --------------------------------------------------------------------------
# Passwords
# --------------------------------------------------------------------------


def test_hash_and_verify():
    hashed = security.hash_password("correct-horse-battery")
    assert hashed != "correct-horse-battery"
    assert security.verify_password("correct-horse-battery", hashed)
    assert not security.verify_password("wrong", hashed)


def test_verify_tolerates_a_malformed_hash():
    assert not security.verify_password("anything", "not-a-bcrypt-hash")


def test_long_passwords_are_rejected_not_truncated():
    """bcrypt ignores bytes past 72; silently truncating would make a long
    passphrase weaker than the user believes."""
    with pytest.raises(ValidationError):
        security.hash_password("a" * 73)


def test_password_policy():
    with pytest.raises(ValidationError):
        security.validate_new_password("short")
    assert security.validate_new_password("a-long-enough-password")


def test_generated_passwords_are_unique_and_safe():
    passwords = {security.generate_password() for _ in range(50)}
    assert len(passwords) == 50
    for pw in passwords:
        assert len(pw) == 20
        assert all(ch.isalnum() or ch in "-_" for ch in pw)


# --------------------------------------------------------------------------
# Authentication
# --------------------------------------------------------------------------


def test_authenticate_success(db, admin):
    assert security.authenticate(db, "admin", "correct-horse-battery") is admin


def test_authenticate_wrong_password(db, admin):
    assert security.authenticate(db, "admin", "nope") is None


def test_authenticate_unknown_user(db):
    assert security.authenticate(db, "ghost", "whatever") is None


def test_create_admin_rejects_duplicates(db, admin):
    with pytest.raises(ValidationError):
        security.create_admin(db, "admin", "another-good-password")


def test_create_admin_rejects_malformed_names(db):
    for bad in ["", "-admin", "ad min", "admin/../root", "a" * 33]:
        with pytest.raises(ValidationError):
            security.create_admin(db, bad, "a-good-long-password")


def test_create_admin_rejects_weak_passwords(db):
    with pytest.raises(ValidationError):
        security.create_admin(db, "operator", "short")


def test_panel_admin_may_use_a_system_reserved_name(db):
    """Panel logins and Linux accounts are separate namespaces; an admin
    called 'root' creates no system account and grants nothing extra."""
    user = security.create_admin(db, "root", "a-good-long-password")
    assert user.username == "root"


# --------------------------------------------------------------------------
# Sessions
# --------------------------------------------------------------------------


def test_session_round_trip(db, admin):
    session, token = security.create_session(db, admin, ip_address="10.0.0.1")
    assert security.get_session(db, token) is not None
    assert session.csrf_token


def test_only_the_hash_is_stored(db, admin):
    """Read access to panel.db must not be a valid login."""
    _, token = security.create_session(db, admin)
    stored = db.query(Session).one()
    assert stored.token_hash != token
    assert stored.token_hash == security.hash_token(token)


def test_unknown_token_is_rejected(db, admin):
    security.create_session(db, admin)
    assert security.get_session(db, "some-other-token") is None
    assert security.get_session(db, "") is None


def test_revoked_session_is_immediately_invalid(db, admin):
    """The reason sessions are server-side rather than signed cookies."""
    session, token = security.create_session(db, admin)
    security.revoke_session(db, session)
    assert security.get_session(db, token) is None


def test_expired_session_is_rejected_and_cleaned_up(db, admin):
    from datetime import timedelta

    from app.models import utcnow

    session, token = security.create_session(db, admin)
    session.expires_at = utcnow() - timedelta(hours=1)
    db.commit()

    assert security.get_session(db, token) is None
    assert db.query(Session).count() == 0


def test_purge_expired_sessions(db, admin):
    from datetime import timedelta

    from app.models import utcnow

    live, _ = security.create_session(db, admin)
    stale, _ = security.create_session(db, admin)
    stale.expires_at = utcnow() - timedelta(hours=1)
    db.commit()

    assert security.purge_expired_sessions(db) == 1
    assert db.query(Session).count() == 1


def test_csrf_comparison_is_constant_time():
    assert security.constant_time_equals("abc", "abc")
    assert not security.constant_time_equals("abc", "abd")
    assert not security.constant_time_equals("abc", "")
    assert not security.constant_time_equals("", None)


# --------------------------------------------------------------------------
# Throttling
# --------------------------------------------------------------------------


def test_lockout_after_repeated_failures(db):
    ip = "203.0.113.9"
    assert not security.is_locked_out(db, ip)

    for _ in range(5):
        security.record_failed_login(db, ip)

    assert security.is_locked_out(db, ip)


def test_successful_login_clears_the_counter(db):
    ip = "203.0.113.10"
    for _ in range(3):
        security.record_failed_login(db, ip)
    security.clear_failed_logins(db, ip)
    assert not security.is_locked_out(db, ip)


def test_lockout_expires(db):
    from datetime import timedelta

    from app.models import LoginAttempt, utcnow

    ip = "203.0.113.11"
    for _ in range(5):
        security.record_failed_login(db, ip)
    assert security.is_locked_out(db, ip)

    record = db.query(LoginAttempt).filter_by(ip_address=ip).one()
    record.locked_until = utcnow() - timedelta(minutes=1)
    db.commit()

    assert not security.is_locked_out(db, ip)
