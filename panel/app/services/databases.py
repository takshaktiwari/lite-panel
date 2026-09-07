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


# --------------------------------------------------------------------------
# Writes
# --------------------------------------------------------------------------


def create_database(db_name: str, db_user: str, password: str, *,
                    host: str = DEFAULT_HOST) -> None:
    """Create a database, its user, and the grant between them."""
    db_name = validate_db_identifier(db_name)
    db_user = validate_db_identifier(db_user, kind="user")
    if not password:
        raise ValidationError("A database password is required.")

    if database_exists(db_name):
        raise ValidationError(f"Database '{db_name}' already exists.")

    quoted_db = quote_identifier(db_name)

    with _connect() as conn, conn.cursor() as cur:
        # utf8mb4 throughout: utf8 in MySQL is three-byte and cannot store
        # emoji or many CJK characters, which surfaces later as data loss.
        cur.execute(
            f"CREATE DATABASE {quoted_db} "
            "CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci"
        )
        cur.execute("CREATE USER IF NOT EXISTS %s@%s IDENTIFIED BY %s",
                    (db_user, host, password))
        cur.execute(f"GRANT ALL PRIVILEGES ON {quoted_db}.* TO %s@%s", (db_user, host))
        cur.execute("FLUSH PRIVILEGES")

    logger.info("created database %s for user %s", db_name, db_user)


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
