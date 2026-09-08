"""Tests for the schema-upgrade helpers in app.database.

_ensure_cron_site_nullable() is the one genuinely destructive migration in
this codebase (see its docstring): an existing cron_jobs table with the old
NOT NULL site_id constraint has to be rebuilt in place, not just patched
with an ALTER TABLE ADD COLUMN like everything in _ADDITIVE_COLUMNS. This
file simulates that old table shape directly with raw SQL and checks the
rebuild both relaxes the constraint and keeps the data.
"""

from app import database
from app.models import Site


def _create_legacy_cron_jobs_table(conn):
    """The pre-server-scope schema: site_id NOT NULL."""
    conn.exec_driver_sql("DROP TABLE IF EXISTS cron_jobs")
    conn.exec_driver_sql(
        """
        CREATE TABLE cron_jobs (
            id INTEGER PRIMARY KEY,
            created_at DATETIME NOT NULL,
            site_id INTEGER NOT NULL,
            description VARCHAR(255) NOT NULL DEFAULT '',
            minute VARCHAR(64) NOT NULL,
            hour VARCHAR(64) NOT NULL,
            day_of_month VARCHAR(64) NOT NULL,
            month VARCHAR(64) NOT NULL,
            day_of_week VARCHAR(64) NOT NULL,
            command TEXT NOT NULL,
            is_enabled BOOLEAN NOT NULL
        )
        """
    )


def _site_id_is_not_null(conn) -> bool:
    info = list(conn.exec_driver_sql("PRAGMA table_info(cron_jobs)"))
    site_col = next(row for row in info if row[1] == "site_id")
    return bool(site_col[3])


def test_ensure_cron_site_nullable_relaxes_an_old_table(db):
    site = Site(
        name="demo",
        domain="demo.example.com",
        system_user="site_demo",
        root_dir="/var/www/demo",
        webroot="/var/www/demo",
    )
    db.add(site)
    db.commit()

    with database.engine.begin() as conn:
        _create_legacy_cron_jobs_table(conn)
        conn.exec_driver_sql(
            "INSERT INTO cron_jobs "
            "(created_at, site_id, description, minute, hour, day_of_month, month, day_of_week, command, is_enabled) "
            f"VALUES ('2026-01-01 00:00:00', {site.id}, 'keep me', '*', '*', '*', '*', '*', 'echo hi', 1)"
        )
        assert _site_id_is_not_null(conn) is True

    database._ensure_cron_site_nullable()

    with database.engine.begin() as conn:
        assert _site_id_is_not_null(conn) is False
        rows = list(conn.exec_driver_sql("SELECT description, command FROM cron_jobs"))
        assert rows == [("keep me", "echo hi")]

        # And a NULL site_id -- the whole point -- is now actually accepted.
        conn.exec_driver_sql(
            "INSERT INTO cron_jobs "
            "(created_at, site_id, description, minute, hour, day_of_month, month, day_of_week, command, is_enabled) "
            "VALUES ('2026-01-01 00:00:00', NULL, '', '*', '*', '*', '*', '*', 'echo server', 1)"
        )


def test_ensure_cron_site_nullable_is_a_no_op_on_a_current_table(db):
    """The normal case: create_all() already made cron_jobs nullable, so
    this must not touch it (and, per its docstring, never runs a rebuild
    when there's nothing to fix)."""
    with database.engine.begin() as conn:
        assert _site_id_is_not_null(conn) is False

    database._ensure_cron_site_nullable()  # must not raise or drop data

    with database.engine.begin() as conn:
        assert _site_id_is_not_null(conn) is False


def test_ensure_cron_site_nullable_is_a_no_op_when_the_table_does_not_exist_yet(db):
    with database.engine.begin() as conn:
        conn.exec_driver_sql("DROP TABLE cron_jobs")

    database._ensure_cron_site_nullable()  # must not raise
