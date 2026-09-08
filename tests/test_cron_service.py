"""Tests for the cron job service.

These stay at the render/escape/dispatch level and never touch a real
crontab -- shell.run is monkeypatched throughout, the same way other service
tests avoid needing real system users or root privileges.
"""

from dataclasses import dataclass
from typing import List

import pytest

from app.services import cron as cron_service
from app.shell import CommandError, CommandResult
from app.validators import ValidationError


@dataclass
class _Job:
    minute: str = "*"
    hour: str = "*"
    day_of_month: str = "*"
    month: str = "*"
    day_of_week: str = "*"
    command: str = "true"
    is_enabled: bool = True


class _FakeCtx:
    def __init__(self):
        self.lines: List[str] = []

    def log(self, line):
        self.lines.append(str(line))


# --------------------------------------------------------------------------
# render_crontab / escaping
# --------------------------------------------------------------------------


def test_render_crontab_includes_only_enabled_jobs():
    jobs = [
        _Job(command="echo one", is_enabled=True),
        _Job(command="echo two", is_enabled=False),
    ]
    text = cron_service.render_crontab(jobs)
    assert "echo one" in text
    assert "echo two" not in text


def test_render_crontab_lays_out_the_five_fields_and_command():
    job = _Job(minute="*/5", hour="3", day_of_month="1", month="*", day_of_week="0", command="/bin/true")
    text = cron_service.render_crontab([job])
    assert "*/5 3 1 * 0 /bin/true" in text


def test_render_crontab_escapes_percent_in_the_command():
    """An unescaped % means "newline, and the rest is stdin" to cron -- a
    command using a date format like %Y-%m-%d would otherwise be silently
    truncated at the first %."""
    job = _Job(command="date +%Y-%m-%d")
    text = cron_service.render_crontab([job])
    assert "date +\\%Y-\\%m-\\%d" in text


def test_render_crontab_with_no_jobs_still_has_the_marker():
    text = cron_service.render_crontab([])
    assert cron_service.MARKER in text


# --------------------------------------------------------------------------
# apply_crontab / remove_crontab
# --------------------------------------------------------------------------


def test_apply_crontab_writes_via_stdin(monkeypatch):
    monkeypatch.setattr(cron_service, "is_available", lambda: True)
    calls = []
    monkeypatch.setattr(
        cron_service,
        "run",
        lambda args, **kwargs: calls.append((args, kwargs)) or CommandResult(tuple(args), 0, "", ""),
    )

    ctx = _FakeCtx()
    cron_service.apply_crontab(ctx, "site_demo", [_Job(command="echo hi")])

    assert len(calls) == 1
    args, kwargs = calls[0]
    assert args == ["crontab", "-u", "site_demo", "-"]
    assert "echo hi" in kwargs["input"]
    assert any("installed" in line for line in ctx.lines)


def test_apply_crontab_with_no_enabled_jobs_clears_instead_of_writing(monkeypatch):
    monkeypatch.setattr(cron_service, "is_available", lambda: True)
    run_calls = []
    remove_calls = []
    monkeypatch.setattr(cron_service, "run", lambda *a, **k: run_calls.append(a))
    monkeypatch.setattr(cron_service, "remove_crontab", lambda username: remove_calls.append(username))

    ctx = _FakeCtx()
    cron_service.apply_crontab(ctx, "site_demo", [_Job(is_enabled=False)])

    assert remove_calls == ["site_demo"]
    assert not run_calls


def test_apply_crontab_refuses_when_cron_is_not_installed(monkeypatch):
    monkeypatch.setattr(cron_service, "is_available", lambda: False)
    with pytest.raises(ValidationError):
        cron_service.apply_crontab(_FakeCtx(), "site_demo", [_Job()])


def test_remove_crontab_swallows_the_no_crontab_case(monkeypatch):
    def _raise(*_args, **_kwargs):
        raise CommandError(CommandResult(("crontab",), 1, "", "no crontab for site_demo"))

    monkeypatch.setattr(cron_service, "run", _raise)
    cron_service.remove_crontab("site_demo")  # must not raise


def test_remove_crontab_reraises_a_real_failure(monkeypatch):
    def _raise(*_args, **_kwargs):
        raise CommandError(CommandResult(("crontab",), 1, "", "permission denied"))

    monkeypatch.setattr(cron_service, "run", _raise)
    with pytest.raises(CommandError):
        cron_service.remove_crontab("site_demo")


# --------------------------------------------------------------------------
# is_available
# --------------------------------------------------------------------------


def test_is_available_reflects_whether_crontab_is_on_path(monkeypatch):
    monkeypatch.setattr(cron_service, "which", lambda name: "/usr/bin/crontab")
    assert cron_service.is_available() is True

    monkeypatch.setattr(cron_service, "which", lambda name: None)
    assert cron_service.is_available() is False
