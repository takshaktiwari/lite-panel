"""Tests for app.services.databases's Adminer login account.

_connect() is mocked throughout -- these check the SQL issued, not a real
MariaDB connection.
"""

from unittest.mock import patch

from app.services import databases as db_service


class _FakeCursor:
    def __init__(self, calls):
        self._calls = calls

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=None):
        self._calls.append((sql, params))


class _FakeConn:
    def __init__(self, calls):
        self._calls = calls

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def cursor(self):
        return _FakeCursor(self._calls)


def test_ensure_adminer_account_creates_a_unix_socket_user_matching_the_adminer_process():
    calls = []
    with patch.object(db_service, "_connect", return_value=_FakeConn(calls)):
        db_service.ensure_adminer_account()

    statements = [sql for sql, _params in calls]
    assert any("CREATE USER" in sql and "unix_socket" in sql for sql in statements)
    assert any("GRANT ALL PRIVILEGES ON *.*" in sql for sql in statements)
    assert any("FLUSH PRIVILEGES" in sql for sql in statements)

    create_call = next(call for call in calls if "CREATE USER" in call[0])
    assert create_call[1] == (db_service.ADMINER_OS_USER, db_service.DEFAULT_HOST)

    grant_call = next(call for call in calls if "GRANT ALL PRIVILEGES" in call[0])
    assert grant_call[1] == (db_service.ADMINER_OS_USER, db_service.DEFAULT_HOST)


def test_adminer_os_user_is_www_data():
    """Must match the "User=" line in the Adminer systemd unit -- unix_socket
    authentication approves a login by checking who is actually connected,
    so this account name and that OS user have to agree exactly."""
    assert db_service.ADMINER_OS_USER == "www-data"
