"""Tests for the newer file manager operations: duplicate/copy, bulk delete
and copy, and archive create/extract.

Archive extraction gets the most scrutiny here: a naive
``ZipFile.extractall()`` follows whatever path a member claims, including
one crafted to climb out of the destination with "../" segments (zip-slip).
extract_archive() must refuse that, the same way every other path in this
module refuses to leave the sites root.
"""

import zipfile

import pytest

from app.services import files as files_service
from app.validators import ValidationError


@pytest.fixture(autouse=True)
def sites_root(tmp_path, monkeypatch):
    """Point the file manager's root at a scratch directory for every test."""
    monkeypatch.setattr(files_service, "settings", type(files_service.settings)(sites_root=tmp_path))
    return tmp_path


# --------------------------------------------------------------------------
# copy_item
# --------------------------------------------------------------------------


def test_duplicate_in_place_gets_a_copy_suffix(sites_root):
    original = sites_root / "config.php"
    original.write_text("<?php echo 1;")

    duplicate = files_service.copy_item("config.php")

    assert duplicate.name == "config-copy.php"
    assert duplicate.read_text() == "<?php echo 1;"
    assert original.exists()  # the original is untouched


def test_duplicating_twice_does_not_collide(sites_root):
    (sites_root / "note.txt").write_text("a")
    files_service.copy_item("note.txt")
    second = files_service.copy_item("note.txt")
    assert second.name == "note-copy-2.txt"


def test_copy_to_a_destination_directory(sites_root):
    (sites_root / "site.txt").write_text("hello")
    (sites_root / "backups").mkdir()

    copied = files_service.copy_item("site.txt", destination_dir="backups")

    assert copied == sites_root / "backups" / "site.txt"
    assert copied.read_text() == "hello"


def test_copy_a_directory_recursively(sites_root):
    src = sites_root / "app"
    (src / "nested").mkdir(parents=True)
    (src / "nested" / "file.txt").write_text("x")

    copied = files_service.copy_item("app", destination_dir=".", new_name="app2")

    assert (copied / "nested" / "file.txt").read_text() == "x"


def test_cannot_copy_a_folder_into_itself(sites_root):
    (sites_root / "app").mkdir()
    with pytest.raises(ValidationError):
        files_service.copy_item("app", destination_dir="app")


def test_copy_refuses_an_existing_destination(sites_root):
    (sites_root / "a.txt").write_text("a")
    (sites_root / "b.txt").write_text("b")
    with pytest.raises(ValidationError):
        files_service.copy_item("a.txt", destination_dir=".", new_name="b.txt")


def test_copy_rejects_traversal_in_destination(sites_root):
    (sites_root / "a.txt").write_text("a")
    with pytest.raises(ValidationError):
        files_service.copy_item("a.txt", destination_dir="../../etc")


def test_copied_directory_ownership_is_restored(sites_root):
    """copytree runs as the daemon's own user (root in production); the
    result must not stay root-owned or the site's own user couldn't touch it."""
    import os

    src = sites_root / "app"
    src.mkdir()
    (src / "file.txt").write_text("x")
    os.chown(sites_root, os.getuid(), os.getgid())  # ensure a known owner

    copied = files_service.copy_item("app", destination_dir=".", new_name="app2")

    assert copied.stat().st_uid == sites_root.stat().st_uid
    assert (copied / "file.txt").stat().st_uid == sites_root.stat().st_uid


# --------------------------------------------------------------------------
# bulk_delete / bulk_copy
# --------------------------------------------------------------------------


def test_bulk_delete_reports_successes_and_failures_separately(sites_root):
    (sites_root / "a.txt").write_text("a")
    (sites_root / "b.txt").write_text("b")

    succeeded, failed = files_service.bulk_delete(["a.txt", "b.txt", "missing.txt"])

    assert set(succeeded) == {"a.txt", "b.txt"}
    assert len(failed) == 1
    assert failed[0][0] == "missing.txt"
    assert not (sites_root / "a.txt").exists()


def test_bulk_delete_does_not_abort_on_the_first_failure(sites_root):
    """One bad path in a multi-select must not block deleting the rest."""
    (sites_root / "good.txt").write_text("x")
    succeeded, failed = files_service.bulk_delete(["../escape", "good.txt"])
    assert succeeded == ["good.txt"]
    assert len(failed) == 1


def test_bulk_copy_reports_successes_and_failures(sites_root):
    (sites_root / "a.txt").write_text("a")
    (sites_root / "dest").mkdir()

    succeeded, failed = files_service.bulk_copy(["a.txt", "missing.txt"], "dest")

    assert succeeded == ["a.txt"]
    assert failed[0][0] == "missing.txt"
    assert (sites_root / "dest" / "a.txt").read_text() == "a"


# --------------------------------------------------------------------------
# create_archive
# --------------------------------------------------------------------------


def test_create_archive_zips_selected_files(sites_root):
    (sites_root / "a.txt").write_text("aaa")
    (sites_root / "b.txt").write_text("bbb")

    archive = files_service.create_archive(["a.txt", "b.txt"], ".", "bundle")

    assert archive.name == "bundle.zip"
    with zipfile.ZipFile(archive) as zf:
        assert set(zf.namelist()) == {"a.txt", "b.txt"}
        assert zf.read("a.txt") == b"aaa"


def test_create_archive_appends_zip_extension_if_missing(sites_root):
    (sites_root / "a.txt").write_text("a")
    archive = files_service.create_archive(["a.txt"], ".", "bundle.zip")
    assert archive.name == "bundle.zip"


def test_create_archive_includes_a_directory_recursively(sites_root):
    (sites_root / "app" / "nested").mkdir(parents=True)
    (sites_root / "app" / "nested" / "f.txt").write_text("x")

    archive = files_service.create_archive(["app"], ".", "bundle")

    with zipfile.ZipFile(archive) as zf:
        assert "app/nested/f.txt" in zf.namelist()


def test_create_archive_refuses_to_overwrite_an_existing_file(sites_root):
    (sites_root / "a.txt").write_text("a")
    (sites_root / "bundle.zip").write_text("already here")
    with pytest.raises(ValidationError):
        files_service.create_archive(["a.txt"], ".", "bundle")


def test_create_archive_rejects_an_empty_selection(sites_root):
    with pytest.raises(ValidationError):
        files_service.create_archive([], ".", "bundle")


def test_create_archive_enforces_a_size_cap(sites_root, monkeypatch):
    monkeypatch.setattr(files_service, "MAX_ARCHIVE_INPUT_BYTES", 10)
    (sites_root / "a.txt").write_text("this is more than ten bytes")
    with pytest.raises(ValidationError):
        files_service.create_archive(["a.txt"], ".", "bundle")


# --------------------------------------------------------------------------
# extract_archive -- zip-slip is the whole point of this section
# --------------------------------------------------------------------------


def test_extract_archive_recreates_the_original_tree(sites_root):
    with zipfile.ZipFile(sites_root / "bundle.zip", "w") as zf:
        zf.writestr("a.txt", "aaa")
        zf.writestr("nested/b.txt", "bbb")

    destination = files_service.extract_archive("bundle.zip")

    assert destination.name == "bundle"
    assert (destination / "a.txt").read_text() == "aaa"
    assert (destination / "nested" / "b.txt").read_text() == "bbb"


def test_extract_archive_avoids_colliding_with_an_existing_folder(sites_root):
    (sites_root / "bundle").mkdir()
    with zipfile.ZipFile(sites_root / "bundle.zip", "w") as zf:
        zf.writestr("a.txt", "a")

    destination = files_service.extract_archive("bundle.zip")
    assert destination.name == "bundle-2"


def test_extract_archive_rejects_a_non_zip_file(sites_root):
    (sites_root / "notes.txt").write_text("not a zip")
    with pytest.raises(ValidationError):
        files_service.extract_archive("notes.txt")


def test_extract_archive_rejects_a_corrupt_zip(sites_root):
    (sites_root / "bad.zip").write_bytes(b"not actually a zip file")
    with pytest.raises(ValidationError):
        files_service.extract_archive("bad.zip")


def test_extract_archive_blocks_zip_slip_via_parent_traversal(sites_root):
    """The classic attack: a member path that climbs out of the extraction
    folder with "../" segments to write somewhere else on disk entirely."""
    with zipfile.ZipFile(sites_root / "evil.zip", "w") as zf:
        zf.writestr("../../../etc/cron.d/evil", "* * * * * root pwned\n")

    with pytest.raises(ValidationError):
        files_service.extract_archive("evil.zip")

    # And critically: nothing was written outside the sites root at all.
    assert not (sites_root.parent / "etc").exists()


def test_extract_archive_blocks_zip_slip_via_absolute_path(sites_root):
    """Some zip tools allow an absolute member path outright."""
    with zipfile.ZipFile(sites_root / "evil.zip", "w") as zf:
        zf.writestr("/etc/cron.d/evil", "* * * * * root pwned\n")

    with pytest.raises(ValidationError):
        files_service.extract_archive("evil.zip")


def test_extract_archive_enforces_an_entry_count_cap(sites_root, monkeypatch):
    monkeypatch.setattr(files_service, "MAX_EXTRACT_ENTRIES", 2)
    with zipfile.ZipFile(sites_root / "bundle.zip", "w") as zf:
        zf.writestr("a.txt", "a")
        zf.writestr("b.txt", "b")
        zf.writestr("c.txt", "c")

    with pytest.raises(ValidationError):
        files_service.extract_archive("bundle.zip")


def test_extract_archive_enforces_a_total_size_cap(sites_root, monkeypatch):
    monkeypatch.setattr(files_service, "MAX_EXTRACT_BYTES", 5)
    with zipfile.ZipFile(sites_root / "bundle.zip", "w") as zf:
        zf.writestr("a.txt", "this is definitely more than five bytes")

    with pytest.raises(ValidationError):
        files_service.extract_archive("bundle.zip")


def test_is_archive_name():
    assert files_service.is_archive_name("bundle.zip")
    assert files_service.is_archive_name("Bundle.ZIP")
    assert not files_service.is_archive_name("bundle.tar.gz")
    assert not files_service.is_archive_name("notes.txt")
