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
from app.models import Base

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
    logger.info("database ready at %s", settings.sqlalchemy_url)


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
