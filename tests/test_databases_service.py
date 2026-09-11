"""Tests for app.services.databases's Adminer login account.

_connect() is mocked throughout -- these check the SQL issued, not a real
MariaDB connection.
"""

import gzip
import subprocess
from unittest.mock import patch

import pytest

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


class _FakeCursorWithFetch(_FakeCursor):
    def __init__(self, calls, fetchall_data=None, fetchone_data=None):
        super().__init__(calls)
        self._fetchall_data = fetchall_data or []
        self._fetchone_data = fetchone_data

    def fetchall(self):
        return self._fetchall_data

    def fetchone(self):
        return self._fetchone_data


class _FakeConnWithData(_FakeConn):
    def __init__(self, calls, fetchall_data=None, fetchone_data=None):
        super().__init__(calls)
        self._fetchall_data = fetchall_data
        self._fetchone_data = fetchone_data

    def cursor(self):
        return _FakeCursorWithFetch(self._calls, self._fetchall_data, self._fetchone_data)


def test_list_database_users_filters_system_users():
    calls = []
    fake_users = [
        ("root",),
        ("debian-sys-maint",),
        ("www-data",),
        ("mysql.sys",),
        ("app_user",),
        ("blog_user",),
    ]
    with patch.object(db_service, "_connect", return_value=_FakeConnWithData(calls, fetchall_data=fake_users)):
        users = db_service.list_database_users()

    assert users == ["app_user", "blog_user"]


def test_reassign_database_user_grants_new_and_revokes_old():
    calls = []
    # user_exists returns True (1,)
    with patch.object(db_service, "_connect", return_value=_FakeConnWithData(calls, fetchall_data=[], fetchone_data=(1,))):
        db_service.reassign_database_user("app_db", "new_user", old_user="old_user")

    statements = [sql for sql, _params in calls]
    assert any("GRANT ALL PRIVILEGES ON `app_db`.* TO %s@%s" in sql for sql in statements)
    assert any("REVOKE ALL PRIVILEGES" in sql for sql in statements)
    assert any("DELETE FROM mysql.db WHERE user = %s AND db = %s" in sql for sql in statements)


def test_export_database_invokes_mysqldump(tmp_path):
    out_file = tmp_path / "dump.sql.gz"
    mock_proc = patch("subprocess.Popen").start()
    try:
        from unittest.mock import MagicMock
        proc_instance = MagicMock()
        proc_instance.stdout.read.side_effect = [b"CREATE TABLE test;", b""]
        proc_instance.communicate.return_value = (b"", b"")
        proc_instance.returncode = 0
        mock_proc.return_value = proc_instance

        with patch.object(db_service, "database_exists", return_value=True), \
             patch("app.providers.mariadb.MariaDbProvider.socket_path", return_value="/run/mysqld/mysqld.sock"):
            res = db_service.export_database("app_db", out_file, gzip=True)

        assert res.exists()
        assert mock_proc.called
        args = mock_proc.call_args[0][0]
        assert args[0] == "mysqldump"
        assert "--socket=/run/mysqld/mysqld.sock" in args
        assert "app_db" in args
    finally:
        patch.stopall()


def test_import_database_invokes_mysql(tmp_path):
    sql_file = tmp_path / "test.sql"
    sql_file.write_text("CREATE TABLE t (id INT);")

    mock_proc = patch("subprocess.Popen").start()
    try:
        from unittest.mock import MagicMock
        proc_instance = MagicMock()
        proc_instance.stdin = MagicMock()
        proc_instance.communicate.return_value = (b"", b"")
        proc_instance.returncode = 0
        mock_proc.return_value = proc_instance

        with patch.object(db_service, "database_exists", return_value=True), \
             patch("app.providers.mariadb.MariaDbProvider.socket_path", return_value="/run/mysqld/mysqld.sock"):
            db_service.import_database("app_db", sql_file)

        assert mock_proc.called
        args = mock_proc.call_args[0][0]
        assert args[0] == "mysql"
        assert "--socket=/run/mysqld/mysqld.sock" in args
        assert "app_db" in args
    finally:
        patch.stopall()


# --------------------------------------------------------------------------
# Import: the pipe plumbing itself
# --------------------------------------------------------------------------
#
# The mocked test above cannot see any of this: a MagicMock stdin accepts
# writes, closes and flushes that a real pipe rejects, which is how a
# close()-then-communicate() sequence that raises "flush of closed file" on
# the Python the panel actually runs under shipped green. These drive a real
# child process instead -- a stand-in for `mysql` that just drains stdin --
# so the streaming, the stdin close and the exit-code handling all run
# against real file descriptors.


def _run_import_against(shell_command, source_file, ctx=None):
    """Run import_database() with `shell_command` standing in for mysql."""
    real_popen = subprocess.Popen

    def fake_popen(args, **kwargs):
        assert args[0] == "mysql"
        return real_popen(["sh", "-c", shell_command], **kwargs)

    with patch("subprocess.Popen", side_effect=fake_popen), \
         patch.object(db_service, "database_exists", return_value=True), \
         patch("app.providers.mariadb.MariaDbProvider.socket_path",
               return_value="/run/mysqld/mysqld.sock"):
        db_service.import_database("app_db", source_file, ctx=ctx)


class _RecordingCtx:
    """Stands in for a JobContext, keeping what it was told."""

    def __init__(self):
        self.progress_calls = []
        self.lines = []

    def progress(self, current, total):
        self.progress_calls.append((current, total))

    def log(self, line):
        self.lines.append(line)


def test_import_database_streams_a_large_dump_through_a_real_pipe(tmp_path):
    # Comfortably past a pipe buffer, so the write loop really does block and
    # resume rather than fitting in one go.
    payload = b"SELECT 1;\n" * 100_000
    sql_file = tmp_path / "dump.sql"
    sql_file.write_bytes(payload)
    received = tmp_path / "received.sql"

    _run_import_against(f"cat > {received}", sql_file)

    assert received.read_bytes() == payload


def test_import_database_reports_progress_over_the_dump_size(tmp_path):
    sql_file = tmp_path / "dump.sql"
    sql_file.write_bytes(b"SELECT 1;\n" * 100_000)
    total = sql_file.stat().st_size
    ctx = _RecordingCtx()

    _run_import_against("cat > /dev/null", sql_file, ctx=ctx)

    assert ctx.progress_calls[0] == (0, total)
    assert ctx.progress_calls[-1] == (total, total)
    # Never overshoots the total it reports against.
    assert all(current <= total for current, _ in ctx.progress_calls)


def test_import_database_decompresses_a_gzipped_dump(tmp_path):
    payload = b"SELECT 1;\n" * 10_000
    gz_file = tmp_path / "dump.sql.gz"
    with gzip.open(gz_file, "wb") as fh:
        fh.write(payload)
    received = tmp_path / "received.sql"

    _run_import_against(f"cat > {received}", gz_file)

    # mysql must get plain SQL, not the compressed bytes.
    assert received.read_bytes() == payload


class _OldPythonPopen(subprocess.Popen):
    """A Popen whose communicate() behaves the way older CPython's does.

    Its _communicate() flushes self.stdin unconditionally; when the caller
    has already closed it that raises ValueError("flush of closed file").
    Python 3.12+ swallows it, which is why the bug shipped green from a dev
    machine and failed every import on the server. Reproducing the older
    behaviour here keeps the regression pinned whatever Python runs the
    tests.
    """

    def communicate(self, input=None, timeout=None):
        if self.stdin is not None and self.stdin.closed:
            raise ValueError("flush of closed file")
        return super().communicate(input, timeout)


def test_import_database_does_not_communicate_over_a_closed_stdin(tmp_path):
    payload = b"SELECT 1;\n" * 100_000
    sql_file = tmp_path / "dump.sql"
    sql_file.write_bytes(payload)
    received = tmp_path / "received.sql"

    def fake_popen(args, **kwargs):
        return _OldPythonPopen(["sh", "-c", f"cat > {received}"], **kwargs)

    with patch("subprocess.Popen", side_effect=fake_popen), \
         patch.object(db_service, "database_exists", return_value=True), \
         patch("app.providers.mariadb.MariaDbProvider.socket_path",
               return_value="/run/mysqld/mysqld.sock"):
        db_service.import_database("app_db", sql_file)

    assert received.read_bytes() == payload


def test_import_database_surfaces_a_failing_mysql(tmp_path):
    sql_file = tmp_path / "dump.sql"
    sql_file.write_bytes(b"SELECT 1;\n" * 100_000)

    with pytest.raises(RuntimeError, match="ERROR 1064"):
        _run_import_against(
            "cat > /dev/null; echo 'ERROR 1064 (42000) at line 1' >&2; exit 1",
            sql_file,
        )


def test_import_database_reports_why_mysql_quit_before_reading_the_dump(tmp_path):
    """The reason, not the symptom.

    mysql refuses the dump's first statement and exits while most of it is
    still unwritten, so this side hits a broken pipe. What reaches the job
    must be mysql's own complaint -- reporting "[Errno 32] Broken pipe"
    leaves someone staring at a failed import with no idea why.
    """
    # Well past a pipe buffer, so the write really does fail mid-dump rather
    # than fitting into the kernel's buffer and going unnoticed.
    sql_file = tmp_path / "dump.sql"
    sql_file.write_bytes(b"SELECT 1;\n" * 200_000)

    with pytest.raises(RuntimeError, match="Table 'migrations' already exists"):
        _run_import_against(
            "echo \"ERROR 1050 (42S01) at line 1: Table 'migrations' already exists\" >&2; exit 1",
            sql_file,
        )


