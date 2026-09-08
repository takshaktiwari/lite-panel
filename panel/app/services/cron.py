"""Per-site cron jobs.

Each ``CronJob`` row belongs to a site and runs as that site's own system
user -- never root, and never a job that isn't tied to a site. The database
is the source of truth; the actual crontab installed for a user is always a
full regeneration from every enabled row belonging to that site's jobs, the
same "database is truth, files are a projection" approach the rest of the
panel uses for nginx vhosts and PHP-FPM pools. There is no incremental
edit-in-place of the installed crontab, so it can never drift from what the
panel thinks it set.
"""

from __future__ import annotations

import logging
from typing import Iterable

from app.models import CronJob
from app.shell import CommandError, run, which
from app.validators import ValidationError

logger = logging.getLogger(__name__)

MARKER = "# managed by lite-panel -- edits here are overwritten on the next change"

# (schedule, label) -- offered as a dropdown in the cron form so most jobs
# never need someone to hand-write "*/5 * * * *" and get it wrong. Anything
# not matching one of these falls back to "Custom", which is what the five
# raw fields are still for.
PRESETS = [
    ("* * * * *", "Every minute"),
    ("*/2 * * * *", "Every 2 minutes"),
    ("*/5 * * * *", "Every 5 minutes"),
    ("*/10 * * * *", "Every 10 minutes"),
    ("*/15 * * * *", "Every 15 minutes"),
    ("*/30 * * * *", "Every 30 minutes"),
    ("0 * * * *", "Hourly"),
    ("0 */2 * * *", "Every 2 hours"),
    ("0 */6 * * *", "Every 6 hours"),
    ("0 */12 * * *", "Every 12 hours"),
    ("0 0 * * *", "Daily at midnight"),
    ("0 0 * * 0", "Weekly (Sunday midnight)"),
    ("0 0 1 * *", "Monthly (1st, midnight)"),
]


def match_preset(schedule: str) -> str:
    """The preset value matching ``schedule``, or "custom" if none does."""
    for value, _label in PRESETS:
        if value == schedule:
            return value
    return "custom"


def is_available() -> bool:
    return which("crontab") is not None


def _escape(command: str) -> str:
    """Escape ``%`` for a crontab line.

    Cron treats an unescaped ``%`` as a newline (the first one ends the
    command; anything after becomes its stdin), which would silently
    truncate a command containing one -- a date format like ``%Y-%m-%d`` is
    the common case this protects against.
    """
    return command.replace("%", "\\%")


def render_crontab(jobs: Iterable[CronJob]) -> str:
    lines = [MARKER]
    for job in jobs:
        if not job.is_enabled:
            continue
        lines.append(
            f"{job.minute} {job.hour} {job.day_of_month} {job.month} {job.day_of_week} "
            f"{_escape(job.command)}"
        )
    return "\n".join(lines) + "\n"


def apply_crontab(ctx, username: str, jobs: Iterable[CronJob]) -> None:
    """Replace the user's entire crontab with the panel's current state."""
    if not is_available():
        raise ValidationError("cron is not installed on this server.")

    jobs = list(jobs)
    if not any(job.is_enabled for job in jobs):
        remove_crontab(username)
        ctx.log(f"crontab cleared for {username} (no enabled jobs)")
        return

    text = render_crontab(jobs)
    run(["crontab", "-u", username, "-"], input=text)
    enabled = sum(1 for job in jobs if job.is_enabled)
    ctx.log(f"crontab installed for {username} ({enabled} job(s))")


def remove_crontab(username: str) -> None:
    try:
        run(["crontab", "-u", username, "-r"])
    except CommandError as exc:
        # Exit code 1 with "no crontab for <user>" just means there was
        # nothing to remove -- not a real failure.
        if "no crontab" not in (exc.result.stderr or "").lower():
            raise
