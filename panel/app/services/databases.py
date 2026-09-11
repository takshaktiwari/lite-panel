"""MariaDB database and user management.

Connects as root over the unix socket, so no database password exists on disk
for the panel to store or an attacker to find.

Values are passed as query parameters (PyMySQL escapes them); identifiers are
regex-validated and then backtick-quoted.  The original scripts built this SQL
by interpolating prompt input into a heredoc, which is exactly the shape this
module exists to avoid.
"""

from __future__ import annotations

import logging
import shutil
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple
from uuid import uuid4

from app.providers.mariadb import MariaDbProvider
from app.validators import ValidationError, quote_identifier, validate_db_identifier

logger = logging.getLogger(__name__)

# Databases created by MariaDB itself, never shown or touched.
SYSTEM_SCHEMAS = frozenset(
    {"mysql", "information_schema", "performance_schema", "sys", "test"}
)

# Site users connect over the local socket or loopback only. Granting on '%'
# would expose them to the internet the moment port 3306 were opened.
DEFAULT_HOST = "localhost"

# The OS user Adminer's own PHP process runs as (see
# assets/units/lite-panel-adminer.service). Matched exactly by a MariaDB
# account name so unix_socket peer authentication can approve it with no
# password at all.
ADMINER_OS_USER = "www-data"


def _connect():
    """Open a root connection over the unix socket."""
    try:
        import pymysql
    except ImportError as exc:  # pragma: no cover - dependency is declared
        raise RuntimeError("PyMySQL is not installed") from exc

    socket_path = MariaDbProvider.socket_path()
    if not socket_path:
        raise RuntimeError(
            "Cannot reach MariaDB: no unix socket found. Is the service running?"
        )

    return pymysql.connect(
        unix_socket=socket_path,
        user="root",
        charset="utf8mb4",
        autocommit=True,
    )


def is_available() -> bool:
    try:
        with _connect():
            return True
    except Exception as exc:  # noqa: BLE001
        logger.debug("MariaDB unavailable: %s", exc)
        return False


# --------------------------------------------------------------------------
# Reads
# --------------------------------------------------------------------------


def list_databases() -> List[Dict]:
    """Every non-system database, with its size on disk."""
    with _connect() as conn, conn.cursor() as cur:
        cur.execute("SHOW DATABASES")
        names = [row[0] for row in cur.fetchall() if row[0] not in SYSTEM_SCHEMAS]

        cur.execute(
            """
            SELECT table_schema, SUM(data_length + index_length)
            FROM information_schema.TABLES
            GROUP BY table_schema
            """
        )
        sizes = {row[0]: int(row[1] or 0) for row in cur.fetchall()}

    return [
        {"name": name, "size_bytes": sizes.get(name, 0), "size_mb": round(sizes.get(name, 0) / 1048576, 1)}
        for name in sorted(names)
    ]


def size_map() -> Dict[str, float]:
    """Convenience wrapper around :func:`list_databases` for callers that only
    want a ``{name: size_mb}`` lookup (e.g. rendering a table)."""
    return {entry["name"]: entry["size_mb"] for entry in list_databases()}


def database_exists(name: str) -> bool:
    name = validate_db_identifier(name)
    with _connect() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT 1 FROM information_schema.SCHEMATA WHERE schema_name = %s",
            (name,),
        )
        return cur.fetchone() is not None


def user_exists(username: str) -> bool:
    username = validate_db_identifier(username, kind="user")
    with _connect() as conn, conn.cursor() as cur:
        cur.execute("SELECT 1 FROM mysql.user WHERE user = %s", (username,))
        return cur.fetchone() is not None


SYSTEM_USERS = frozenset({"root", "mysql", "debian-sys-maint", ADMINER_OS_USER})


def list_database_users() -> List[str]:
    """Every non-system database username in MariaDB."""
    with _connect() as conn, conn.cursor() as cur:
        cur.execute("SELECT DISTINCT user FROM mysql.user ORDER BY user")
        return [
            row[0]
            for row in cur.fetchall()
            if row[0]
            and row[0] not in SYSTEM_USERS
            and not row[0].startswith("mysql.")
            and not row[0].startswith("mariadb.")
        ]


# --------------------------------------------------------------------------
# Writes
# --------------------------------------------------------------------------


def create_database(db_name: str, db_user: str, password: Optional[str] = None, *,
                    host: str = DEFAULT_HOST) -> None:
    """Create a database, its user (if not existing), and the grant between them."""
    db_name = validate_db_identifier(db_name)
    db_user = validate_db_identifier(db_user, kind="user")

    if database_exists(db_name):
        raise ValidationError(f"Database '{db_name}' already exists.")

    exists = user_exists(db_user)
    if not exists and not password:
        raise ValidationError("A database password is required for new users.")

    quoted_db = quote_identifier(db_name)

    with _connect() as conn, conn.cursor() as cur:
        # utf8mb4 throughout: utf8 in MySQL is three-byte and cannot store
        # emoji or many CJK characters, which surfaces later as data loss.
        cur.execute(
            f"CREATE DATABASE {quoted_db} "
            "CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci"
        )
        if not exists:
            cur.execute("CREATE USER %s@%s IDENTIFIED BY %s",
                        (db_user, host, password))

        cur.execute(f"GRANT ALL PRIVILEGES ON {quoted_db}.* TO %s@%s", (db_user, host))
        cur.execute("FLUSH PRIVILEGES")

    logger.info("created database %s for user %s", db_name, db_user)


def grant_database_user(db_name: str, db_user: str, password: Optional[str] = None, *,
                        host: str = DEFAULT_HOST) -> None:
    """Grant an existing or new user access to a database."""
    db_name = validate_db_identifier(db_name)
    db_user = validate_db_identifier(db_user, kind="user")

    exists = user_exists(db_user)
    if not exists and not password:
        raise ValidationError(f"Database user '{db_user}' does not exist. A password is required to create it.")

    quoted_db = quote_identifier(db_name)

    with _connect() as conn, conn.cursor() as cur:
        if not exists:
            cur.execute("CREATE USER %s@%s IDENTIFIED BY %s", (db_user, host, password))

        cur.execute(f"GRANT ALL PRIVILEGES ON {quoted_db}.* TO %s@%s", (db_user, host))
        cur.execute("FLUSH PRIVILEGES")

    logger.info("granted database %s to user %s", db_name, db_user)


def reassign_database_user(db_name: str, new_user: str, old_user: Optional[str] = None,
                           new_password: Optional[str] = None, *,
                           host: str = DEFAULT_HOST) -> None:
    """Grant new_user on db_name, and revoke old_user if different (cleaning up orphaned user)."""
    db_name = validate_db_identifier(db_name)
    new_user = validate_db_identifier(new_user, kind="user")
    quoted_db = quote_identifier(db_name)

    # 1. Grant new user
    grant_database_user(db_name, new_user, new_password, host=host)

    # 2. If old_user differs, revoke its privilege on this database
    if old_user and old_user != new_user and user_exists(old_user):
        old_user = validate_db_identifier(old_user, kind="user")
        with _connect() as conn, conn.cursor() as cur:
            cur.execute(f"REVOKE ALL PRIVILEGES, GRANT OPTION FROM %s@%s", (old_user, host))
            # Re-grant privileges for remaining databases of old_user if any
            cur.execute(
                """
                SELECT db FROM mysql.db
                WHERE user = %s AND db NOT IN (%s, '')
                """,
                (old_user, db_name),
            )
            other_dbs = [row[0] for row in cur.fetchall()]
            # Delete db entry for this database in mysql.db
            cur.execute("DELETE FROM mysql.db WHERE user = %s AND db = %s", (old_user, db_name))

            if not other_dbs:
                # No other databases left for this user; drop user
                cur.execute("DROP USER IF EXISTS %s@%s", (old_user, host))
            cur.execute("FLUSH PRIVILEGES")
        logger.info("reassigned database %s from user %s to %s", db_name, old_user, new_user)


def drop_database(db_name: str, db_user: Optional[str] = None, *,
                  host: str = DEFAULT_HOST) -> None:
    db_name = validate_db_identifier(db_name)
    quoted_db = quote_identifier(db_name)

    with _connect() as conn, conn.cursor() as cur:
        cur.execute(f"DROP DATABASE IF EXISTS {quoted_db}")

        if db_user:
            db_user = validate_db_identifier(db_user, kind="user")
            # Only drop the account if it has no other databases; a user
            # shared between two databases must survive one being deleted.
            cur.execute(
                """
                SELECT COUNT(*) FROM mysql.db
                WHERE user = %s AND db NOT IN (%s, '')
                """,
                (db_user, db_name),
            )
            (remaining,) = cur.fetchone()
            if not remaining:
                cur.execute("DROP USER IF EXISTS %s@%s", (db_user, host))
        cur.execute("FLUSH PRIVILEGES")

    logger.info("dropped database %s", db_name)


def change_password(db_user: str, password: str, *, host: str = DEFAULT_HOST) -> None:
    db_user = validate_db_identifier(db_user, kind="user")
    if not password:
        raise ValidationError("A password is required.")
    with _connect() as conn, conn.cursor() as cur:
        cur.execute("ALTER USER %s@%s IDENTIFIED BY %s", (db_user, host, password))
        cur.execute("FLUSH PRIVILEGES")


def ensure_adminer_account() -> None:
    """A MariaDB account Adminer can log into with an empty password field.

    Adminer runs as the "www-data" OS user (see the systemd unit). Creating
    a MariaDB account of that same name with unix_socket authentication
    means MariaDB approves the login by checking who is actually on the
    other end of the socket -- no password to generate, show once, store,
    or rotate, and critically, nothing that touches a site's own database
    credentials the way minting a fresh one for it would (that would break
    the site's own app the moment its stored connection string went stale).

    Privileges mirror what the panel's own root connection already does
    through this page (create/drop any database, reset any password) --
    this does not hand out anything an admin using Databases couldn't
    already do, just through Adminer's UI instead of this one.
    """
    with _connect() as conn, conn.cursor() as cur:
        cur.execute(
            "CREATE USER IF NOT EXISTS %s@%s IDENTIFIED VIA unix_socket",
            (ADMINER_OS_USER, DEFAULT_HOST),
        )
        cur.execute(
            "GRANT ALL PRIVILEGES ON *.* TO %s@%s",
            (ADMINER_OS_USER, DEFAULT_HOST),
        )
        cur.execute("FLUSH PRIVILEGES")

    logger.info("Adminer's passwordless login account (%s@%s) is ready", ADMINER_OS_USER, DEFAULT_HOST)


def export_database(db_name: str, target_file: str | Path, *, gzip: bool = True) -> Path:
    """Export a database to a SQL dump file (optionally gzipped) via mysqldump."""
    import gzip as gzip_lib
    import subprocess
    from app.shell import base_env

    db_name = validate_db_identifier(db_name)
    if not database_exists(db_name):
        raise ValidationError(f"Database '{db_name}' does not exist.")

    socket_path = MariaDbProvider.socket_path()
    if not socket_path:
        raise RuntimeError("Cannot reach MariaDB: no unix socket found.")

    target_path = Path(target_file)
    target_path.parent.mkdir(parents=True, exist_ok=True)

    args = [
        "mysqldump",
        f"--socket={socket_path}",
        "-u", "root",
        "--single-transaction",
        "--quick",
        "--routines",
        "--events",
        db_name,
    ]

    try:
        proc = subprocess.Popen(
            args,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=base_env(),
        )
    except FileNotFoundError as exc:
        raise RuntimeError("mysqldump utility is not installed on the system.") from exc

    try:
        if gzip:
            with gzip_lib.open(target_path, "wb") as f_out:
                assert proc.stdout is not None
                while True:
                    chunk = proc.stdout.read(65536)
                    if not chunk:
                        break
                    f_out.write(chunk)
        else:
            with open(target_path, "wb") as f_out:
                assert proc.stdout is not None
                while True:
                    chunk = proc.stdout.read(65536)
                    if not chunk:
                        break
                    f_out.write(chunk)

        _, stderr = proc.communicate()
        if proc.returncode != 0:
            err_msg = (stderr or b"").decode("utf-8", errors="replace").strip()
            if target_path.exists():
                target_path.unlink(missing_ok=True)
            raise RuntimeError(f"mysqldump failed (code {proc.returncode}): {err_msg[:400]}")
    except Exception:
        if target_path.exists():
            target_path.unlink(missing_ok=True)
        raise

    logger.info("exported database %s to %s", db_name, target_path)
    return target_path


def import_database(db_name: str, source_file: str | Path, ctx=None) -> None:
    """Import a SQL dump file (.sql or .sql.gz) into an existing database.

    ``ctx`` is the job's :class:`~app.jobs.JobContext` when this runs as a
    background job (the normal case -- see ``tasks.import_database_task``);
    it is optional so the function stays directly callable from tests. When
    given, progress is reported as bytes of the *dump file* streamed into
    ``mysql``'s stdin, not rows applied -- mysql gives no way to observe the
    latter, but for a multi-gigabyte dump, bytes streamed is still the
    difference between "frozen" and "working" in the UI.
    """
    import gzip as gzip_lib
    import subprocess
    from app.shell import base_env

    db_name = validate_db_identifier(db_name)
    if not database_exists(db_name):
        raise ValidationError(f"Database '{db_name}' does not exist.")

    source_path = Path(source_file)
    if not source_path.is_file():
        raise ValidationError("The database dump file does not exist.")

    socket_path = MariaDbProvider.socket_path()
    if not socket_path:
        raise RuntimeError("Cannot reach MariaDB: no unix socket found.")

    is_gz = source_path.suffix.lower() == ".gz" or source_path.name.lower().endswith(".sql.gz")
    total_bytes = source_path.stat().st_size

    args = [
        "mysql",
        f"--socket={socket_path}",
        "-u", "root",
        db_name,
    ]

    try:
        proc = subprocess.Popen(
            args,
            stdin=subprocess.PIPE,
            # A dump's own stdout is never shown anywhere, and leaving it as a
            # pipe nobody drains is a way to deadlock a big import: mysql
            # blocks once the pipe buffer fills while this side is still
            # busy writing stdin.
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            env=base_env(),
        )
    except FileNotFoundError as exc:
        raise RuntimeError("mysql utility is not installed on the system.") from exc

    if ctx is not None:
        ctx.progress(0, total_bytes)

    try:
        assert proc.stdin is not None
        with open(source_path, "rb") as raw:
            # Read via `raw` directly so progress can track its own .tell() --
            # the file's position on disk -- rather than bytes handed to
            # mysql's stdin. For a .sql.gz those diverge (the pipe carries
            # the *decompressed* stream, which runs larger than the file on
            # disk), and the file's on-disk size is the only total available
            # to report progress against.
            f_in = gzip_lib.GzipFile(fileobj=raw) if is_gz else raw
            while True:
                chunk = f_in.read(65536)
                if not chunk:
                    break
                proc.stdin.write(chunk)
                if ctx is not None:
                    ctx.progress(min(raw.tell(), total_bytes), total_bytes)
            proc.stdin.close()

        # Deliberately not communicate(): it wants to close stdin itself, and
        # closing a BufferedWriter flushes it first -- on a pipe this side has
        # already closed that raises "flush of closed file" and fails every
        # import, however cleanly mysql actually finished.
        stderr = proc.stderr.read() if proc.stderr else b""
        proc.wait()
        if proc.returncode != 0:
            err_msg = (stderr or b"").decode("utf-8", errors="replace").strip()
            raise RuntimeError(f"Database import failed (code {proc.returncode}): {err_msg[:400]}")
    except Exception:
        proc.kill()
        proc.wait()
        raise

    if ctx is not None:
        ctx.progress(total_bytes, total_bytes)

    logger.info("imported dump %s into database %s", source_path, db_name)


# --------------------------------------------------------------------------
# Chunked dump uploads (import modal)
# --------------------------------------------------------------------------
#
# Same strategy as the file manager's chunked uploads
# (app.services.files: init_upload / append_chunk / complete_upload), just
# targeting a fresh temp directory instead of a path inside the sites root --
# a dump is never written under a site's own files, so there is no
# resolve_within() to reuse here, only the session bookkeeping. Sessions live
# in this same process-local dict for the same reason: the panel runs a
# single Uvicorn worker, and an upload is not meant to survive a restart,
# only the browser tab that started it.

MAX_IMPORT_UPLOAD_BYTES = 5 * 1024 * 1024 * 1024  # 5 GB -- dumps run larger than site files
IMPORT_UPLOAD_FREE_SPACE_MARGIN_BYTES = 512 * 1024 * 1024
IMPORT_UPLOAD_SESSION_TTL = timedelta(hours=2)
IMPORT_DUMP_SUFFIXES = (".sql.gz", ".sql", ".gz")


@dataclass
class _ImportUploadSession:
    id: str
    tmp_dir: Path
    part_path: Path
    final_path: Path
    total_size: int
    original_filename: str
    received_bytes: int = 0
    next_index: int = 0
    last_activity: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


_import_upload_sessions: Dict[str, "_ImportUploadSession"] = {}


def _discard_import_upload_session(upload_id: str) -> None:
    session = _import_upload_sessions.pop(upload_id, None)
    if session is None:
        return
    shutil.rmtree(session.tmp_dir, ignore_errors=True)


def _sweep_expired_import_upload_sessions() -> None:
    now = datetime.now(timezone.utc)
    expired = [
        upload_id
        for upload_id, session in _import_upload_sessions.items()
        if now - session.last_activity > IMPORT_UPLOAD_SESSION_TTL
    ]
    for upload_id in expired:
        _discard_import_upload_session(upload_id)


def init_import_upload(db_name: str, filename: str, total_size: int) -> _ImportUploadSession:
    # Same reasoning as files_service.init_upload: called on every new
    # upload, so this doubles as the cleanup point for sessions abandoned by
    # a closed tab, without needing a background sweep thread.
    _sweep_expired_import_upload_sessions()

    fname_lower = (filename or "").lower()
    if not fname_lower.endswith(IMPORT_DUMP_SUFFIXES):
        raise ValidationError("Only .sql and .sql.gz dump files are supported.")

    if total_size <= 0 or total_size > MAX_IMPORT_UPLOAD_BYTES:
        limit_gb = MAX_IMPORT_UPLOAD_BYTES // (1024 * 1024 * 1024)
        raise ValidationError(f"Dump files are limited to {limit_gb} GB.")

    tmp_dir = Path(tempfile.mkdtemp(prefix="lp-db-import-"))

    free_bytes = shutil.disk_usage(tmp_dir).free
    if total_size > free_bytes - IMPORT_UPLOAD_FREE_SPACE_MARGIN_BYTES:
        shutil.rmtree(tmp_dir, ignore_errors=True)
        headroom_mb = max(free_bytes - IMPORT_UPLOAD_FREE_SPACE_MARGIN_BYTES, 0) // (1024 * 1024)
        raise ValidationError(
            f"That dump is larger than the {headroom_mb} MB currently free on disk."
        )

    ext = ".sql.gz" if fname_lower.endswith(".gz") else ".sql"
    upload_id = uuid4().hex
    part_path = tmp_dir / f"upload{ext}.part"
    part_path.write_bytes(b"")

    session = _ImportUploadSession(
        id=upload_id,
        tmp_dir=tmp_dir,
        part_path=part_path,
        final_path=tmp_dir / f"upload_{db_name}{ext}",
        total_size=total_size,
        original_filename=filename,
    )
    _import_upload_sessions[upload_id] = session
    return session


def append_import_chunk(upload_id: str, index: int, data: bytes) -> int:
    session = _import_upload_sessions.get(upload_id)
    if session is None:
        raise ValidationError("Upload session not found or has expired.")

    if index < session.next_index:
        # A client retry after a dropped response -- already applied, so
        # report success without writing it twice.
        return session.received_bytes
    if index != session.next_index:
        raise ValidationError("Upload chunks arrived out of order.")
    if session.received_bytes + len(data) > session.total_size:
        raise ValidationError("Upload received more data than expected.")

    with open(session.part_path, "ab") as fh:
        fh.write(data)

    session.received_bytes += len(data)
    session.next_index += 1
    session.last_activity = datetime.now(timezone.utc)
    return session.received_bytes


def complete_import_upload(upload_id: str) -> Tuple[Path, str]:
    """Finish an upload session, returning ``(assembled_path, original_filename)``."""
    session = _import_upload_sessions.get(upload_id)
    if session is None:
        raise ValidationError("Upload session not found or has expired.")
    if session.received_bytes != session.total_size:
        raise ValidationError("Upload is incomplete.")

    session.part_path.rename(session.final_path)
    _import_upload_sessions.pop(upload_id, None)
    return session.final_path, session.original_filename


def abort_import_upload(upload_id: str) -> None:
    _discard_import_upload_session(upload_id)

