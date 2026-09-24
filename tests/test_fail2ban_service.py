"""Tests for app.services.fail2ban -- parsing, validation, rendering, bans."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from app.services import fail2ban as f2b
from app.shell import CommandResult
from app.validators import ValidationError


def _result(returncode=0, stdout="", stderr=""):
    return CommandResult(("x",), returncode, stdout, stderr)


@pytest.fixture
def f2b_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(f2b, "FAIL2BAN_DIR", tmp_path)
    return tmp_path


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------

SSHD_STATUS = """Status for the jail: sshd
|- Filter
|  |- Currently failed:\t3
|  |- Total failed:\t57
|  `- Journal matches:\t_SYSTEMD_UNIT=sshd.service + _COMM=sshd
`- Actions
   |- Currently banned:\t2
   |- Total banned:\t9
   `- Banned IP list:\t198.51.100.4 203.0.113.9
"""


def test_parse_jail_status():
    parsed = f2b.parse_jail_status(SSHD_STATUS)
    assert parsed == {
        "currently_failed": 3,
        "total_failed": 57,
        "currently_banned": 2,
        "total_banned": 9,
        "banned_ips": ["198.51.100.4", "203.0.113.9"],
    }


def test_parse_jail_status_empty_ban_list():
    parsed = f2b.parse_jail_status(SSHD_STATUS.replace("198.51.100.4 203.0.113.9", ""))
    assert parsed["banned_ips"] == []


def test_parse_banip_with_time():
    output = (
        "198.51.100.4 \t2026-09-24 10:00:00 + 3600 = 2026-09-24 11:00:00\n"
        "203.0.113.9 \t2026-09-24 10:05:00 + -1 = 9999-12-31 23:59:59\n"
    )
    bans = f2b.parse_banip_with_time(output)
    assert bans["198.51.100.4"] == {
        "banned_at": "2026-09-24 10:00:00", "duration": 3600, "expires_at": "2026-09-24 11:00:00",
    }
    assert bans["203.0.113.9"]["expires_at"] is None


def test_parse_sshd_config_dump():
    info = f2b.parse_sshd_config_dump("port 22\nport 2222\npasswordauthentication yes\nx y z\n")
    assert info.ports == ["22", "2222"]
    assert info.password_auth is True


def test_parse_sshd_config_dump_falls_back_to_service_name():
    info = f2b.parse_sshd_config_dump("")
    assert info.ports == ["ssh"]
    assert info.password_auth is None


def test_format_duration():
    assert f2b.format_duration(3600) == "1 hour"
    assert f2b.format_duration(600) == "10 minutes"
    assert f2b.format_duration(604800) == "1 week"
    assert f2b.format_duration(90) == "90 seconds"
    assert f2b.format_duration(-1) == "permanent"


# ---------------------------------------------------------------------------
# Validation and settings
# ---------------------------------------------------------------------------


def test_parse_ignoreip_normalises_and_dedupes():
    assert f2b.parse_ignoreip("10.0.0.5, 10.0.0.5\n192.168.1.7/24  ::1") == [
        "10.0.0.5", "192.168.1.0/24", "::1",
    ]


def test_parse_ignoreip_rejects_garbage():
    with pytest.raises(ValidationError):
        f2b.parse_ignoreip("10.0.0.5 not-an-ip")


def test_validate_ip_rejects_cidr_for_bans():
    with pytest.raises(ValidationError):
        f2b.validate_ip("10.0.0.0/8")


def test_settings_row_created_with_recommended_defaults(db):
    row = f2b.get_settings(db)
    assert (row.maxretry, row.findtime, row.bantime) == (5, 600, 3600)
    assert row.increment_enabled is True
    assert row.increment_maxtime == 604800
    assert row.recidive_enabled is False
    assert row.sshd_enabled is True


def _update(db, **overrides):
    values = dict(sshd_enabled=True, maxretry=5, findtime=600, bantime=3600,
                  increment_enabled=True, increment_maxtime=604800,
                  recidive_enabled=False, ignoreip="")
    values.update(overrides)
    return f2b.update_settings(db, **values)


def test_update_settings_stores_values(db):
    row = _update(db, maxretry=3, bantime=86400, ignoreip="203.0.113.1 10.0.0.0/8")
    assert row.maxretry == 3
    assert row.bantime == 86400
    assert row.ignoreip == "203.0.113.1\n10.0.0.0/8"


@pytest.mark.parametrize("overrides", [
    {"maxretry": 0},
    {"findtime": 10},
    {"bantime": 3600, "increment_maxtime": 600},
    {"ignoreip": "bogus"},
])
def test_update_settings_rejects_bad_values(db, overrides):
    with pytest.raises(ValidationError):
        _update(db, **overrides)


def test_maxtime_below_bantime_is_fine_when_growing_bans_off(db):
    row = _update(db, increment_enabled=False, bantime=86400, increment_maxtime=3600)
    assert row.increment_enabled is False


def test_add_ignoreip(db):
    assert f2b.add_ignoreip(db, "203.0.113.5") is True
    assert f2b.add_ignoreip(db, "203.0.113.5") is False
    assert f2b.add_ignoreip(db, "testclient") is False
    assert f2b.get_settings(db).ignoreip == "203.0.113.5"


def test_is_ignored():
    assert f2b.is_ignored("127.0.0.1", [])
    assert f2b.is_ignored("10.1.2.3", ["10.0.0.0/8"])
    assert not f2b.is_ignored("203.0.113.9", ["10.0.0.0/8", "::1"])


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def _render_jail(db, ssh=None, banactions=("iptables-multiport", "iptables-allports")):
    from app.services import renderer

    ctx = f2b.build_context(f2b.get_settings(db), ssh or f2b.SshInfo(ports=["22"]), banactions)
    return renderer.render("fail2ban-jail.local.j2", ctx), ctx


def test_jail_render_defaults(db):
    text, ctx = _render_jail(db, f2b.SshInfo(ports=["22", "2222"]))
    assert "ignoreip = 127.0.0.1/8 ::1" in text
    assert "bantime.increment = true" in text
    assert "bantime.maxtime = 604800" in text
    assert "port = 22,2222" in text
    assert "backend = systemd" in text
    assert "maxretry = 5" in text
    assert "findtime = 600" in text
    assert "bantime = 3600" in text
    assert "banaction = iptables-multiport" in text
    # recidive off by default, manual jail always on and permanent
    recidive = text.split("[recidive]")[1].split("[lite-panel-manual]")[0]
    assert "enabled = false" in recidive
    manual = text.split("[lite-panel-manual]")[1]
    assert "enabled = true" in manual
    assert "bantime = -1" in manual
    assert ctx["dbpurgeage"] == 604800


def test_jail_render_custom(db):
    _update(db, increment_enabled=False, recidive_enabled=True, sshd_enabled=False,
            ignoreip="203.0.113.1", bantime=86400)
    text, ctx = _render_jail(db)
    assert "ignoreip = 127.0.0.1/8 ::1 203.0.113.1" in text
    assert "bantime.increment = false" in text.split("[sshd]")[0]
    assert "bantime.maxtime" not in text
    assert "enabled = false" in text.split("[sshd]")[1].split("[recidive]")[0]
    assert "enabled = true" in text.split("[recidive]")[1].split("[lite-panel-manual]")[0]
    assert ctx["dbpurgeage"] == 604800


def test_dbpurgeage_covers_longest_ban(db):
    _update(db, increment_maxtime=365 * 86400)
    _, ctx = _render_jail(db)
    assert ctx["dbpurgeage"] == 365 * 86400


def test_choose_banactions_prefers_iptables():
    with patch.object(f2b, "which", side_effect=lambda p: "/usr/sbin/" + p):
        assert f2b.choose_banactions() == ("iptables-multiport", "iptables-allports")
    with patch.object(f2b, "which", side_effect=lambda p: "/usr/sbin/nft" if p == "nft" else None):
        assert f2b.choose_banactions() == ("nftables-multiport", "nftables-allports")


# ---------------------------------------------------------------------------
# Applying config
# ---------------------------------------------------------------------------


def _fake_run(test_ok=True, running=True):
    calls = []

    def fake(args, **kwargs):
        calls.append(list(args))
        if args[:2] == ["fail2ban-client", "-t"]:
            return _result(0 if test_ok else 1, stderr="" if test_ok else "bad option")
        if args[:2] == ["fail2ban-client", "ping"]:
            return _result(0 if running else 255)
        if args[:2] == ["sshd", "-T"]:
            return _result(0, "port 22\n")
        return _result(0, SSHD_STATUS.replace("sshd", f2b.MANUAL_JAIL))

    return fake, calls


def test_apply_config_writes_files_and_reloads(db, f2b_dir):
    fake, calls = _fake_run()
    with patch.object(f2b, "run", side_effect=fake), \
         patch.object(f2b, "which", return_value="/usr/sbin/iptables"):
        f2b.apply_config(db)

    assert (f2b_dir / "jail.d" / "lite-panel.local").exists()
    assert (f2b_dir / "fail2ban.d" / "lite-panel.local").read_text().count("dbpurgeage = 604800") == 1
    assert (f2b_dir / "filter.d" / "lite-panel-manual.conf").exists()
    assert ["fail2ban-client", "reload"] in calls


def test_apply_config_starts_service_when_stopped(db, f2b_dir):
    fake, calls = _fake_run(running=False)
    with patch.object(f2b, "run", side_effect=fake), \
         patch.object(f2b, "which", return_value="/usr/sbin/iptables"):
        f2b.apply_config(db)
    assert ["systemctl", "restart", "fail2ban"] in calls
    assert ["fail2ban-client", "reload"] not in calls


def test_apply_config_rolls_back_on_failed_check(db, f2b_dir):
    jail = f2b_dir / "jail.d" / "lite-panel.local"
    jail.parent.mkdir(parents=True)
    jail.write_text("previous good config\n")

    fake, calls = _fake_run(test_ok=False)
    with patch.object(f2b, "run", side_effect=fake), \
         patch.object(f2b, "which", return_value="/usr/sbin/iptables"), \
         pytest.raises(RuntimeError, match="bad option"):
        f2b.apply_config(db)

    assert jail.read_text() == "previous good config\n"
    # Files that didn't exist before are removed again rather than left half-applied.
    assert not (f2b_dir / "fail2ban.d" / "lite-panel.local").exists()
    assert ["fail2ban-client", "reload"] not in calls


# ---------------------------------------------------------------------------
# Bans
# ---------------------------------------------------------------------------


def test_temporary_ban_goes_to_sshd_jail(db):
    fake, calls = _fake_run()
    with patch.object(f2b, "run", side_effect=fake):
        f2b.ban_ip(db, " 203.0.113.9 ", permanent=False)
    assert ["fail2ban-client", "set", "sshd", "banip", "203.0.113.9"] in calls
    assert f2b.list_permanent_bans(db) == []


def test_permanent_ban_is_recorded(db):
    fake, calls = _fake_run()
    with patch.object(f2b, "run", side_effect=fake):
        f2b.ban_ip(db, "203.0.113.9", permanent=True, note="scanner")
    assert ["fail2ban-client", "set", f2b.MANUAL_JAIL, "banip", "203.0.113.9"] in calls
    [row] = f2b.list_permanent_bans(db)
    assert (row.ip, row.note) == ("203.0.113.9", "scanner")


def test_ban_refuses_never_ban_list_and_own_ip(db):
    _update(db, ignoreip="10.0.0.0/8")
    with patch.object(f2b, "run") as run:
        with pytest.raises(ValidationError, match="never-ban"):
            f2b.ban_ip(db, "10.2.3.4", permanent=True)
        with pytest.raises(ValidationError, match="never-ban"):
            f2b.ban_ip(db, "127.0.0.1", permanent=True)
        with pytest.raises(ValidationError, match="lock you out"):
            f2b.ban_ip(db, "203.0.113.9", permanent=True, protected=["203.0.113.9"])
    run.assert_not_called()
    assert f2b.list_permanent_bans(db) == []


def test_temporary_ban_needs_sshd_jail(db):
    _update(db, sshd_enabled=False)
    with patch.object(f2b, "run"), pytest.raises(ValidationError, match="SSH jail"):
        f2b.ban_ip(db, "203.0.113.9", permanent=False)


def test_unban_forgets_permanent_ban(db):
    fake, calls = _fake_run()
    with patch.object(f2b, "run", side_effect=fake):
        f2b.ban_ip(db, "203.0.113.9", permanent=True)
        f2b.unban_ip(db, "203.0.113.9")
    assert ["fail2ban-client", "unban", "203.0.113.9"] in calls
    assert f2b.list_permanent_bans(db) == []


def test_reapply_permanent_bans_only_bans_missing(db):
    from app.models import Fail2banPermanentBan

    db.add_all([Fail2banPermanentBan(ip="198.51.100.4"), Fail2banPermanentBan(ip="192.0.2.50")])
    db.commit()
    fake, calls = _fake_run()  # manual jail status lists 198.51.100.4 as banned
    with patch.object(f2b, "run", side_effect=fake):
        assert f2b.reapply_permanent_bans(db) == 1
    banips = [c for c in calls if "banip" in c]
    assert banips == [["fail2ban-client", "set", f2b.MANUAL_JAIL, "banip", "192.0.2.50"]]


def test_get_status_aggregates_jails():
    def fake(args, **kwargs):
        if args[:2] == ["fail2ban-client", "ping"]:
            return _result(0)
        if args[1] == "status":
            if args[2] == "recidive":
                return _result(255, stderr="Sorry but the jail 'recidive' does not exist")
            if args[2] == f2b.MANUAL_JAIL:
                return _result(0, SSHD_STATUS.replace("198.51.100.4 203.0.113.9", "192.0.2.50")
                               .replace("Total failed:\t57", "Total failed:\t0"))
            return _result(0, SSHD_STATUS)
        if args[1] == "get":
            return _result(0, "198.51.100.4 \t2026-09-24 10:00:00 + 3600 = 2026-09-24 11:00:00\n")
        return _result(0)

    with patch.object(f2b, "run", side_effect=fake):
        status = f2b.get_status()

    assert status["running"] is True
    assert set(status["jails"]) == {"sshd", f2b.MANUAL_JAIL}
    assert status["total_failed"] == 57
    ips = {b["ip"]: b for b in status["bans"]}
    assert ips["198.51.100.4"]["expires_at"] == "2026-09-24 11:00:00"
    assert ips["198.51.100.4"]["permanent"] is False
    assert ips["192.0.2.50"]["permanent"] is True


def test_get_status_when_stopped():
    with patch.object(f2b, "run", return_value=_result(255)):
        status = f2b.get_status()
    assert status["running"] is False
    assert status["bans"] == []
