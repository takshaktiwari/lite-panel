"""SQLite engine and session handling.

SQLite is a deliberate choice, not a placeholder: a panel whose whole premise
is "lighter than the alternatives" should not require a database server to
manage a database server.  A single file also makes backup and restore of the
panel's own state trivial.
"""

from __future__ import annotations

import logging
from contextlib import contextmanager
from typing import Iterator

from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session as OrmSession
from sqlalchemy.orm import sessionmaker

from app.config import get_settings
from app.models import Base, CronJob

logger = logging.getLogger(__name__)

_settings = get_settings()

engine: Engine = create_engine(
    _settings.sqlalchemy_url,
    # The job worker runs in its own thread and shares this engine.
    connect_args={"check_same_thread": False, "timeout": 30},
    future=True,
)

SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False, future=True)


@event.listens_for(engine, "connect")
def _configure_sqlite(dbapi_connection, _record):
    """WAL so the job worker writing progress cannot block a page load, and
    foreign keys because SQLite leaves them off by default."""
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.execute("PRAGMA synchronous=NORMAL")
    cursor.close()


def init_db() -> None:
    """Create any missing tables.

    Sufficient while the schema only grows; a real migration tool arrives with
    the first destructive change rather than being set up before it is needed.
    """
    settings = get_settings()
    if not settings.database_url:
        settings.state_dir.mkdir(parents=True, exist_ok=True)
    Base.metadata.create_all(bind=engine)
    _ensure_additive_columns()
    _ensure_cron_site_nullable()
    logger.info("database ready at %s", settings.sqlalchemy_url)


# Columns added to an existing table after its initial release.
# create_all() only creates missing *tables*, never alters an existing one,
# so a column added here needs a one-line entry -- still not a real migration
# tool, just enough for changes that are purely additive (a new nullable
# column) rather than destructive.
_ADDITIVE_COLUMNS: dict = {}


def _ensure_additive_columns() -> None:
    with engine.begin() as conn:
        for table, columns in _ADDITIVE_COLUMNS.items():
            existing = {row[1] for row in conn.exec_driver_sql(f"PRAGMA table_info({table})")}
            for name, sql_type in columns:
                if name not in existing:
                    conn.exec_driver_sql(f"ALTER TABLE {table} ADD COLUMN {name} {sql_type}")
                    logger.info("added column %s.%s", table, name)


def _ensure_cron_site_nullable() -> None:
    """``cron_jobs.site_id`` started out NOT NULL -- every job belonged to a
    site. Server-wide jobs need it nullable, and SQLite has no ALTER COLUMN,
    so an existing table still carrying the old constraint is rebuilt in
    place: renamed aside, recreated from today's model (already nullable),
    data copied across, old one dropped. This is the "first destructive
    change" the purely-additive mechanism above was never meant to cover.
    A fresh install never reaches the rebuild branch -- create_all() already
    made the table with today's schema.
    """
    with engine.begin() as conn:
        info = list(conn.exec_driver_sql("PRAGMA table_info(cron_jobs)"))
        if not info:
            return  # table doesn't exist yet; create_all() will have made it nullable

        site_col = next((row for row in info if row[1] == "site_id"), None)
        if site_col is None or not site_col[3]:
            return  # already nullable

        logger.info("rebuilding cron_jobs so site_id can be null (server-wide jobs)")
        columns = ", ".join(row[1] for row in info)
        conn.exec_driver_sql("ALTER TABLE cron_jobs RENAME TO cron_jobs_old")
        CronJob.__table__.create(bind=conn)
        conn.exec_driver_sql(
            f"INSERT INTO cron_jobs ({columns}) SELECT {columns} FROM cron_jobs_old"
        )
        conn.exec_driver_sql("DROP TABLE cron_jobs_old")


@contextmanager
def session_scope() -> Iterator[OrmSession]:
    """Transactional scope for code outside the request cycle (the worker)."""
    session = SessionLocal()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def get_session() -> Iterator[OrmSession]:
    """FastAPI dependency."""
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()
