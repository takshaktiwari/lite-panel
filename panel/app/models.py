"""The panel's database schema.

This database is the source of truth for the server's configuration.  Nginx
vhosts, PHP-FPM pools and tuning drop-ins are *projections* of these rows --
the panel can rebuild every file it owns from what is stored here, which is
what makes drift recoverable and upgrades safe.

Note what is deliberately absent: site database passwords and FTP passwords.
They are shown once at creation and never persisted, so a copy of this file is
not a copy of every credential on the box.
"""

from __future__ import annotations

import enum
from datetime import datetime, timezone
from typing import List, Optional

from sqlalchemy import (
    Boolean,
    DateTime,
    Enum,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)


# --------------------------------------------------------------------------
# Authentication
# --------------------------------------------------------------------------


class AdminUser(TimestampMixin, Base):
    __tablename__ = "admin_users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    username: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    last_login_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    # v1 provisions exactly one admin; the table is shaped for more so adding
    # users later is a feature, not a migration of the auth model.
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    sessions: Mapped[List["Session"]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )


class Session(TimestampMixin, Base):
    """Server-side session.

    Sessions live here rather than in a signed cookie so that logging out, or
    revoking a stolen session, takes effect immediately -- worth the row lookup
    when the process holding the session runs as root.  Only the hash of the
    token is stored, so database read access does not confer login.
    """

    __tablename__ = "sessions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True, nullable=False)
    csrf_token: Mapped[str] = mapped_column(String(64), nullable=False)
    user_id: Mapped[int] = mapped_column(ForeignKey("admin_users.id"), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    ip_address: Mapped[Optional[str]] = mapped_column(String(45), nullable=True)
    user_agent: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)

    user: Mapped[AdminUser] = relationship(back_populates="sessions")

    @property
    def is_expired(self) -> bool:
        expires = self.expires_at
        if expires.tzinfo is None:  # SQLite hands back naive datetimes
            expires = expires.replace(tzinfo=timezone.utc)
        return expires <= utcnow()


class LoginAttempt(Base):
    """Failed-login counter, keyed by IP, for rate limiting."""

    __tablename__ = "login_attempts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    ip_address: Mapped[str] = mapped_column(String(45), unique=True, index=True, nullable=False)
    attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    window_start: Mapped[datetime] = mapped_column(DateTime, default=utcnow, nullable=False)
    locked_until: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)


# --------------------------------------------------------------------------
# Jobs
# --------------------------------------------------------------------------


class JobStatus(str, enum.Enum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCESS = "success"
    FAILED = "failed"


class Job(TimestampMixin, Base):
    """A privileged operation running outside the request cycle.

    ``apt install php8.3`` takes minutes; an HTTP request cannot hold it open.
    Jobs also double as the audit transcript for anything that changes the
    server, since the full command output is retained.
    """

    __tablename__ = "jobs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    kind: Mapped[str] = mapped_column(String(64), nullable=False)
    description: Mapped[str] = mapped_column(String(255), nullable=False)
    status: Mapped[JobStatus] = mapped_column(
        Enum(JobStatus), default=JobStatus.PENDING, nullable=False, index=True
    )
    # JSON arguments for the handler (which PHP version, which site, ...).
    payload: Mapped[str] = mapped_column(Text, default="{}", nullable=False)
    log: Mapped[str] = mapped_column(Text, default="", nullable=False)
    error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    started_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    finished_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    started_by: Mapped[Optional[int]] = mapped_column(
        ForeignKey("admin_users.id"), nullable=True
    )

    @property
    def is_terminal(self) -> bool:
        return self.status in (JobStatus.SUCCESS, JobStatus.FAILED)


# --------------------------------------------------------------------------
# Stack
# --------------------------------------------------------------------------


class InstalledProvider(TimestampMixin, Base):
    """Records that a stack component is installed and managed by the panel.

    ``version`` is empty for single-version components (nginx, vsftpd) and
    carries the PHP version for PHP, which is installed many times over.
    """

    __tablename__ = "installed_providers"
    __table_args__ = (UniqueConstraint("key", "version", name="uq_provider_version"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    key: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    version: Mapped[str] = mapped_column(String(32), default="", nullable=False)
    is_default: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)


class TuningOverride(Base):
    """An operator's manual override of a computed tuning value.

    Tuning is derived from the machine's RAM, but the derived number is a
    default rather than a decree; anything set here wins when config is
    re-rendered, and survives a resize recalculation.
    """

    __tablename__ = "tuning_overrides"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    key: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    value: Mapped[str] = mapped_column(String(255), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=utcnow, onupdate=utcnow, nullable=False
    )


# --------------------------------------------------------------------------
# Sites
# --------------------------------------------------------------------------


class Site(TimestampMixin, Base):
    """A hosted site: its own system user, PHP-FPM pool, vhost and webroot.

    The dedicated system user is the structural difference from running
    everything as ``www-data``: it is what makes per-site PHP versions, real
    isolation between sites, and FTP without ACL juggling possible at all.
    """

    __tablename__ = "sites"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(32), unique=True, nullable=False)
    domain: Mapped[str] = mapped_column(String(253), unique=True, nullable=False)
    system_user: Mapped[str] = mapped_column(String(32), unique=True, nullable=False)
    root_dir: Mapped[str] = mapped_column(String(255), nullable=False)
    webroot: Mapped[str] = mapped_column(String(255), nullable=False)
    php_version: Mapped[Optional[str]] = mapped_column(String(8), nullable=True)
    ssl_enabled: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    redirect_www: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    upload_limit_mb: Mapped[int] = mapped_column(Integer, default=128, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    aliases: Mapped[List["SiteAlias"]] = relationship(
        back_populates="site", cascade="all, delete-orphan"
    )
    databases: Mapped[List["SiteDatabase"]] = relationship(
        back_populates="site", cascade="all, delete-orphan"
    )
    ftp_accounts: Mapped[List["FtpAccount"]] = relationship(
        back_populates="site", cascade="all, delete-orphan"
    )
    cron_jobs: Mapped[List["CronJob"]] = relationship(
        back_populates="site", cascade="all, delete-orphan"
    )


class SiteAlias(Base):
    """An additional domain served by the same site."""

    __tablename__ = "site_aliases"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    site_id: Mapped[int] = mapped_column(ForeignKey("sites.id"), nullable=False)
    domain: Mapped[str] = mapped_column(String(253), unique=True, nullable=False)

    site: Mapped[Site] = relationship(back_populates="aliases")


class SiteDatabase(TimestampMixin, Base):
    """A MariaDB database and its user.  The password is never stored."""

    __tablename__ = "site_databases"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    site_id: Mapped[Optional[int]] = mapped_column(ForeignKey("sites.id"), nullable=True)
    db_name: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    db_user: Mapped[str] = mapped_column(String(64), nullable=False)

    site: Mapped[Optional[Site]] = relationship(back_populates="databases")


class FtpAccount(TimestampMixin, Base):
    """FTP access for a site.

    Normally this *is* the site's system user, which is why there is no ACL
    reconciliation anywhere in this codebase.
    """

    __tablename__ = "ftp_accounts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    site_id: Mapped[int] = mapped_column(ForeignKey("sites.id"), nullable=False)
    username: Mapped[str] = mapped_column(String(32), unique=True, nullable=False)
    home_dir: Mapped[str] = mapped_column(String(255), nullable=False)
    is_enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    site: Mapped[Site] = relationship(back_populates="ftp_accounts")


class CronJob(TimestampMixin, Base):
    """A scheduled command for one site, run as that site's own system user.

    This table is the source of truth; the actual crontab installed for the
    user (via ``crontab -u <user> -``) is a full regeneration from every
    enabled row belonging to that site, the same "database is truth, files
    are a projection" approach the rest of the panel uses for nginx/PHP-FPM
    config. There is deliberately no root-scoped/server-wide job here -- a
    job only ever runs with the privilege its own site already has.
    """

    __tablename__ = "cron_jobs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    site_id: Mapped[int] = mapped_column(ForeignKey("sites.id"), nullable=False)
    description: Mapped[str] = mapped_column(String(255), default="", nullable=False)
    minute: Mapped[str] = mapped_column(String(64), nullable=False)
    hour: Mapped[str] = mapped_column(String(64), nullable=False)
    day_of_month: Mapped[str] = mapped_column(String(64), nullable=False)
    month: Mapped[str] = mapped_column(String(64), nullable=False)
    day_of_week: Mapped[str] = mapped_column(String(64), nullable=False)
    command: Mapped[str] = mapped_column(Text, nullable=False)
    is_enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    site: Mapped[Site] = relationship(back_populates="cron_jobs")

    @property
    def schedule_display(self) -> str:
        return f"{self.minute} {self.hour} {self.day_of_month} {self.month} {self.day_of_week}"


# --------------------------------------------------------------------------
# Audit
# --------------------------------------------------------------------------


class AuditLog(Base):
    """Every mutating action.

    Cheap to write and disproportionately valuable given the accepted risk
    model: the daemon runs as root, so "what changed, when, from where" is the
    first question after anything goes wrong.
    """

    __tablename__ = "audit_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    timestamp: Mapped[datetime] = mapped_column(
        DateTime, default=utcnow, nullable=False, index=True
    )
    user_id: Mapped[Optional[int]] = mapped_column(ForeignKey("admin_users.id"), nullable=True)
    username: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    action: Mapped[str] = mapped_column(String(64), nullable=False)
    target: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    detail: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    ip_address: Mapped[Optional[str]] = mapped_column(String(45), nullable=True)
