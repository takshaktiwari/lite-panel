"""Swap management: parsing, validation, the file/fstab steps and the routes.

The kernel's swap table is simulated with a small fake, so what is exercised
for real is the panel's own logic: which commands run in which order, what
ends up in /etc/fstab, and what is refused.
"""

import json
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app import jobs
from app.main import create_app
from app.models import Job
from app.services import swap as swap_service
from app.validators import ValidationError

PROC_SWAPS = """Filename\t\t\t\tType\t\tSize\t\tUsed\t\tPriority
/swapfile                               file\t\t2097148\t\t1024\t\t-2
/dev/sda2                               partition\t524284\t\t0\t\t-3
"""


# --------------------------------------------------------------------------
# Pure helpers
# --------------------------------------------------------------------------


def test_parse_proc_swaps_reads_every_device_in_mb():
    devices = swap_service.parse_proc_swaps(PROC_SWAPS)
    assert [d.name for d in devices] == ["/swapfile", "/dev/sda2"]
    assert devices[0].size_mb == 2047 and devices[0].used_mb == 1
    assert devices[0].priority == -2
    assert devices[0].managed and not devices[1].managed


def test_parse_proc_swaps_of_an_empty_table():
    assert swap_service.parse_proc_swaps("Filename Type Size Used Priority\n") == []
    assert swap_service.parse_proc_swaps("") == []


def test_fstab_entry_is_added_once_and_keeps_existing_lines():
    original = "UUID=abc / ext4 defaults 0 1"
    once = swap_service.fstab_with_swapfile(original)
    assert once == original + "\n/swapfile none swap sw 0 0\n"
    assert swap_service.fstab_with_swapfile(once) == once


def test_fstab_removal_only_drops_the_swapfile_line():
    text = "UUID=abc / ext4 defaults 0 1\n/swapfile none swap sw 0 0\n# /swapfile none swap sw 0 0\n/dev/sda2 none swap sw 0 0\n"
    result = swap_service.fstab_without_swapfile(text)
    assert "/dev/sda2 none swap sw 0 0" in result
    assert "UUID=abc" in result
    assert "\n/swapfile none swap" not in result and not result.startswith("/swapfile")
    assert "# /swapfile none swap sw 0 0" in result  # comments are left alone


def test_a_commented_swapfile_line_is_not_counted_as_configured():
    assert not swap_service.fstab_has_swapfile("# /swapfile none swap sw 0 0\n")


@pytest.mark.parametrize("bad", ["", "abc", "100", "999999", "-5", "1.5"])
def test_size_validation_rejects_out_of_range_and_garbage(bad):
    with pytest.raises(ValidationError):
        swap_service.validate_size_mb(bad)


def test_size_validation_accepts_the_range_edges():
    assert swap_service.validate_size_mb("256") == 256
    assert swap_service.validate_size_mb(16384) == 16384


@pytest.mark.parametrize("bad", ["", "x", "-1", "101"])
def test_swappiness_validation(bad):
    with pytest.raises(ValidationError):
        swap_service.validate_swappiness(bad)


def test_swappiness_accepts_zero_to_one_hundred():
    assert swap_service.validate_swappiness("0") == 0
    assert swap_service.validate_swappiness("100") == 100


def test_sysctl_file_carries_the_value():
    assert "vm.swappiness=10" in swap_service.render_sysctl(10)


# --------------------------------------------------------------------------
# A simulated kernel + filesystem for the job steps
# --------------------------------------------------------------------------


class FakeSystem:
    def __init__(self, tmp_path, monkeypatch, *, free_mb=50_000, ram_mb=8000):
        self.swapfile = tmp_path / "swapfile"
        self.fstab = tmp_path / "fstab"
        self.proc = tmp_path / "swaps"
        self.sysctl = tmp_path / "sysctl.conf"
        self.fstab.write_text("UUID=abc / ext4 defaults 0 1\n")
        self.proc.write_text("Filename Type Size Used Priority\n")
        self.commands = []
        self.free_mb = free_mb
        self.fail = set()

        monkeypatch.setattr(swap_service, "SWAPFILE", self.swapfile)
        monkeypatch.setattr(swap_service, "FSTAB_LINE", f"{self.swapfile} none swap sw 0 0")
        monkeypatch.setattr(swap_service, "FSTAB", self.fstab)
        monkeypatch.setattr(swap_service, "PROC_SWAPS", self.proc)
        monkeypatch.setattr(swap_service, "SYSCTL_FILE", self.sysctl)
        monkeypatch.setattr(swap_service, "SWAPPINESS_PROC", tmp_path / "swappiness")
        (tmp_path / "swappiness").write_text("60\n")
        monkeypatch.setattr(swap_service, "is_supported", lambda: True)
        monkeypatch.setattr(swap_service.system, "total_ram_mb", lambda: ram_mb)
        monkeypatch.setattr(
            swap_service.system, "disk_usage_for",
            lambda path: {"total_mb": 100_000, "used_mb": 100_000 - self.free_mb, "free_mb": self.free_mb},
        )

    def _allocate(self, size_mb):
        with open(self.swapfile, "wb") as handle:  # sparse: no real gigabytes in tests
            handle.truncate(size_mb * 1024 * 1024)

    # -- the "kernel" --
    def _swapon(self):
        size_kb = self.swapfile.stat().st_size // 1024
        self.proc.write_text(
            "Filename Type Size Used Priority\n"
            f"{self.swapfile} file {size_kb} 0 -2\n"
        )

    def _swapoff(self):
        self.proc.write_text("Filename Type Size Used Priority\n")

    def execute(self, args, **_kw):
        args = list(args)
        self.commands.append(args)
        name = args[0]
        if name in self.fail:
            return 1
        if name == "fallocate":
            self._allocate(int(args[2].rstrip("M")))
        elif name == "dd":
            self._allocate(int(next(a for a in args if a.startswith("count=")).split("=")[1]))
        elif name == "swapon":
            self._swapon()
        elif name == "swapoff":
            self._swapoff()
        return 0


class FakeCtx:
    def __init__(self, system):
        self.system = system
        self.lines = []

    def log(self, line):
        self.lines.append(line)

    def run(self, args, **kw):
        return self.system.execute(args, **kw)

    def check(self, args, **kw):
        if self.system.execute(args, **kw) != 0:
            raise RuntimeError(f"{args[0]} failed")


@pytest.fixture
def fake(tmp_path, monkeypatch):
    return FakeSystem(tmp_path, monkeypatch)


def _verbs(fake):
    return [c[0] for c in fake.commands]


def test_create_runs_the_steps_in_order_and_persists_in_fstab(fake):
    swap_service.set_size(FakeCtx(fake), 1024)

    assert _verbs(fake) == ["fallocate", "mkswap", "swapon"]
    assert fake.swapfile.stat().st_size == 1024 * 1024 * 1024
    assert oct(fake.swapfile.stat().st_mode & 0o777) == "0o600"
    assert f"{fake.swapfile} none swap sw 0 0" in fake.fstab.read_text()
    assert "UUID=abc" in fake.fstab.read_text()
    status = swap_service.get_status()
    assert status.managed_active and status.total_mb == 1024 and status.in_fstab


def test_create_falls_back_to_dd_when_fallocate_is_unsupported(fake):
    fake.fail.add("fallocate")
    swap_service.set_size(FakeCtx(fake), 512)
    assert _verbs(fake) == ["fallocate", "dd", "mkswap", "swapon"]
    assert fake.swapfile.stat().st_size == 512 * 1024 * 1024


def test_resize_turns_swap_off_replaces_the_file_and_keeps_one_fstab_line(fake):
    swap_service.set_size(FakeCtx(fake), 512)
    fake.commands.clear()

    swap_service.set_size(FakeCtx(fake), 1024)

    assert _verbs(fake) == ["swapoff", "fallocate", "mkswap", "swapon"]
    assert swap_service.get_status().total_mb == 1024
    assert fake.fstab.read_text().count("none swap sw") == 1


def test_setting_the_current_size_again_changes_nothing(fake):
    swap_service.set_size(FakeCtx(fake), 512)
    fake.commands.clear()
    ctx = FakeCtx(fake)
    swap_service.set_size(ctx, 512)
    assert fake.commands == []
    assert any("nothing to change" in line for line in ctx.lines)


def test_create_is_refused_when_the_disk_would_be_too_full(tmp_path, monkeypatch):
    small = FakeSystem(tmp_path, monkeypatch, free_mb=1500)
    with pytest.raises(ValidationError, match="Not enough free disk"):
        swap_service.set_size(FakeCtx(small), 1024)
    assert small.commands == []
    assert not small.swapfile.exists()


def test_resize_counts_the_old_file_as_reclaimable_space(tmp_path, monkeypatch):
    system = FakeSystem(tmp_path, monkeypatch, free_mb=50_000)
    swap_service.set_size(FakeCtx(system), 2048)
    system.free_mb = 1500
    # 2500 MB + 1024 headroom - 2048 reclaimed = 1476 <= 1500 free: allowed.
    swap_service.set_size(FakeCtx(system), 2500)
    assert swap_service.get_status().total_mb == 2500


def test_resize_is_refused_just_over_that_limit(tmp_path, monkeypatch):
    system = FakeSystem(tmp_path, monkeypatch, free_mb=50_000)
    swap_service.set_size(FakeCtx(system), 2048)
    system.free_mb = 1500
    with pytest.raises(ValidationError, match="Not enough free disk"):
        swap_service.set_size(FakeCtx(system), 2600)  # needs 1576 > 1500
    assert swap_service.get_status().total_mb == 2048


def test_resize_refuses_when_even_with_the_old_file_reclaimed_it_will_not_fit(tmp_path, monkeypatch):
    system = FakeSystem(tmp_path, monkeypatch, free_mb=50_000)
    swap_service.set_size(FakeCtx(system), 512)
    system.free_mb = 100
    with pytest.raises(ValidationError, match="Not enough free disk"):
        swap_service.set_size(FakeCtx(system), 4096)
    assert swap_service.get_status().total_mb == 512  # untouched


def test_a_failed_swapoff_leaves_the_existing_swap_in_place(fake):
    swap_service.set_size(FakeCtx(fake), 512)
    fake.fail.add("swapoff")
    with pytest.raises(RuntimeError):
        swap_service.set_size(FakeCtx(fake), 1024)
    assert fake.swapfile.exists()
    assert swap_service.get_status().total_mb == 512


def test_other_swap_is_left_alone_and_reported(fake):
    fake.proc.write_text("Filename Type Size Used Priority\n/dev/sda2 partition 524288 0 -3\n")
    ctx = FakeCtx(fake)
    swap_service.set_size(ctx, 512)
    assert any("/dev/sda2" in line for line in ctx.lines)
    names = [d.name for d in swap_service.get_status().devices]
    assert "/swapfile" in names[0] or str(fake.swapfile) in names[0]


def test_remove_turns_off_deletes_and_cleans_fstab(fake):
    swap_service.set_size(FakeCtx(fake), 512)
    fake.commands.clear()

    swap_service.remove(FakeCtx(fake))

    assert _verbs(fake) == ["swapoff"]
    assert not fake.swapfile.exists()
    assert "swap" not in fake.fstab.read_text()
    assert "UUID=abc" in fake.fstab.read_text()
    assert swap_service.get_status().total_mb == 0


def test_remove_with_nothing_to_remove_is_a_clean_no_op(fake):
    ctx = FakeCtx(fake)
    swap_service.remove(ctx)
    assert fake.commands == []
    assert any("no panel-managed swap" in line for line in ctx.lines)


def test_swappiness_is_applied_now_and_persisted(fake):
    swap_service.set_swappiness(FakeCtx(fake), 10)
    assert ["sysctl", "-w", "vm.swappiness=10"] in fake.commands
    assert "vm.swappiness=10" in fake.sysctl.read_text()


def test_unsupported_systems_refuse_every_change(fake, monkeypatch):
    monkeypatch.setattr(swap_service, "is_supported", lambda: False)
    for call in (
        lambda: swap_service.set_size(FakeCtx(fake), 512),
        lambda: swap_service.remove(FakeCtx(fake)),
        lambda: swap_service.set_swappiness(FakeCtx(fake), 10),
    ):
        with pytest.raises(ValidationError, match="can't be managed"):
            call()


def test_status_reports_ram_disk_and_recommendation(fake):
    status = swap_service.get_status()
    assert status.ram_mb == 8000
    assert status.free_disk_mb == 50_000
    assert status.recommended_mb == 2048
    assert status.swappiness == 60
    assert not status.swapfile_exists and status.total_mb == 0


# --------------------------------------------------------------------------
# Routes
# --------------------------------------------------------------------------


@pytest.fixture
def web(db, admin, monkeypatch):
    monkeypatch.setattr(jobs.worker, "submit", lambda job_id: None)
    monkeypatch.setattr(
        swap_service, "get_status",
        lambda: swap_service.SwapStatus(
            supported=True, ram_mb=8000, free_disk_mb=50_000, recommended_mb=2048, swappiness=60,
        ),
    )
    with TestClient(create_app(), follow_redirects=False) as client:
        assert client.post("/login", data={"username": "admin", "password": "correct-horse-battery"}).status_code == 303
        yield client


def _token(client, path="/swap"):
    page = client.get(path)
    marker = 'name="csrf_token" value="'
    start = page.text.index(marker) + len(marker)
    return page.text[start : page.text.index('"', start)]


def _last_job(db):
    return db.scalars(select(Job).order_by(Job.id.desc())).first()


def test_swap_requires_login(db, admin):
    with TestClient(create_app(), follow_redirects=False) as client:
        assert client.get("/swap").status_code == 303
        assert client.post("/swap/set", data={"size_mb": "512"}).status_code in (303, 400)


def test_page_offers_to_set_up_swap_when_there_is_none(web):
    page = web.get("/swap")
    assert page.status_code == 200
    assert "Set up swap" in page.text
    assert "Create swap" in page.text
    assert "Remove swap file" not in page.text
    assert "(recommended)" in page.text


def test_page_disables_sizes_that_do_not_fit(db, admin, monkeypatch):
    monkeypatch.setattr(
        swap_service, "get_status",
        lambda: swap_service.SwapStatus(supported=True, ram_mb=1000, free_disk_mb=2000, recommended_mb=2048),
    )
    with TestClient(create_app(), follow_redirects=False) as client:
        client.post("/login", data={"username": "admin", "password": "correct-horse-battery"})
        page = client.get("/swap").text
    assert "not enough disk" in page


def test_page_shows_resize_and_remove_when_swap_is_active(db, admin, monkeypatch):
    status = swap_service.SwapStatus(
        supported=True, ram_mb=8000, free_disk_mb=50_000, recommended_mb=2048, swappiness=10,
        devices=[swap_service.SwapDevice("/swapfile", "file", 2048, 100, -2),
                 swap_service.SwapDevice("/dev/sda2", "partition", 512, 0, -3)],
    )
    monkeypatch.setattr(swap_service, "get_status", lambda: status)
    with TestClient(create_app(), follow_redirects=False) as client:
        client.post("/login", data={"username": "admin", "password": "correct-horse-battery"})
        page = client.get("/swap").text
    assert "Resize swap file" in page and "Remove swap file" in page
    assert "/dev/sda2" in page and "Not managed by the panel" in page


def test_unsupported_system_shows_a_notice(db, admin, monkeypatch):
    monkeypatch.setattr(swap_service, "get_status", lambda: swap_service.SwapStatus(supported=False))
    with TestClient(create_app(), follow_redirects=False) as client:
        client.post("/login", data={"username": "admin", "password": "correct-horse-battery"})
        assert "be managed here" in client.get("/swap").text


def test_set_enqueues_a_job_with_the_size(web, db):
    response = web.post("/swap/set", data={"csrf_token": _token(web), "size_mb": "2048"})
    assert response.status_code == 303
    job = _last_job(db)
    assert job.kind == "swap.set"
    assert json.loads(job.payload) == {"size_mb": 2048}


@pytest.mark.parametrize("bad", ["10", "abc", "999999", ""])
def test_set_rejects_a_bad_size_without_queueing(web, db, bad):
    response = web.post("/swap/set", data={"csrf_token": _token(web), "size_mb": bad})
    assert response.status_code in (303, 422)
    if response.status_code == 303:
        assert "error=" in response.headers["location"]
    assert _last_job(db) is None


def test_remove_enqueues_a_job(web, db):
    web.post("/swap/remove", data={"csrf_token": _token(web)})
    assert _last_job(db).kind == "swap.remove"


def test_swappiness_enqueues_a_job_and_rejects_bad_values(web, db):
    token = _token(web)
    web.post("/swap/swappiness", data={"csrf_token": token, "value": "10"})
    assert json.loads(_last_job(db).payload) == {"value": 10}
    before = _last_job(db).id
    response = web.post("/swap/swappiness", data={"csrf_token": token, "value": "500"})
    assert "error=" in response.headers["location"]
    assert _last_job(db).id == before


def test_changes_require_csrf(web):
    assert web.post("/swap/set", data={"size_mb": "512"}).status_code == 400
    assert web.post("/swap/remove", data={}).status_code == 400
    assert web.post("/swap/swappiness", data={"value": "10"}).status_code == 400


def test_dashboard_and_sidebar_link_to_swap(web):
    assert 'href="/swap"' in web.get("/sites").text
