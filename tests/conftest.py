"""Shared test setup.

The database engine is built at import time from settings, so the environment
has to be redirected at a scratch file *before* any app module is imported.
"""

import os
import tempfile

_TMPDIR = tempfile.mkdtemp(prefix="lite-panel-tests-")
os.environ.setdefault("LITE_PANEL_DATABASE_URL", f"sqlite:///{_TMPDIR}/test.db")
os.environ.setdefault("LITE_PANEL_SECRET_KEY", "test-secret-key")
os.environ.setdefault("LITE_PANEL_DEV_MODE", "true")

import pytest  # noqa: E402

from app.database import SessionLocal, engine  # noqa: E402
from app.models import Base  # noqa: E402


@pytest.fixture
def db():
    """A session against a freshly created schema."""
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()


@pytest.fixture
def admin(db):
    from app.security import create_admin

    return create_admin(db, "admin", "correct-horse-battery")
