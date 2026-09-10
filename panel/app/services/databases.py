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
from pathlib import Path
from typing import Dict, List, Optional

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


def import_database(db_name: str, source_file: str | Path) -> None:
    """Import a SQL dump file (.sql or .sql.gz) into an existing database."""
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
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=base_env(),
        )
    except FileNotFoundError as exc:
        raise RuntimeError("mysql utility is not installed on the system.") from exc

    try:
        opener = gzip_lib.open if is_gz else open
        with opener(source_path, "rb") as f_in:
            assert proc.stdin is not None
            while True:
                chunk = f_in.read(65536)
                if not chunk:
                    break
                proc.stdin.write(chunk)
            proc.stdin.close()

        stdout, stderr = proc.communicate()
        if proc.returncode != 0:
            err_msg = (stderr or b"").decode("utf-8", errors="replace").strip()
            raise RuntimeError(f"Database import failed (code {proc.returncode}): {err_msg[:400]}")
    except Exception:
        proc.kill()
        proc.wait()
        raise

    logger.info("imported dump %s into database %s", source_path, db_name)

