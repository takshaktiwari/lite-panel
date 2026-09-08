"""Tests for the newer file manager operations: duplicate/copy, bulk delete
and copy, and archive create/extract.

Archive extraction gets the most scrutiny here: a naive
``ZipFile.extractall()`` follows whatever path a member claims, including
one crafted to climb out of the destination with "../" segments (zip-slip).
extract_archive() must refuse that, the same way every other path in this
module refuses to leave the sites root.
"""

import shutil
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
# move_item
# --------------------------------------------------------------------------


def test_move_relocates_into_the_destination_directory(sites_root):
    (sites_root / "site.txt").write_text("hello")
    (sites_root / "archive").mkdir()

    moved = files_service.move_item("site.txt", "archive")

    assert moved == sites_root / "archive" / "site.txt"
    assert moved.read_text() == "hello"
    assert not (sites_root / "site.txt").exists()


def test_move_a_directory_recursively(sites_root):
    src = sites_root / "app"
    (src / "nested").mkdir(parents=True)
    (src / "nested" / "file.txt").write_text("x")
    (sites_root / "dest").mkdir()

    moved = files_service.move_item("app", "dest")

    assert (moved / "nested" / "file.txt").read_text() == "x"
    assert not src.exists()


def test_cannot_move_the_root_directory(sites_root):
    (sites_root / "dest").mkdir()
    with pytest.raises(ValidationError):
        files_service.move_item(".", "dest")


def test_cannot_move_a_folder_into_itself(sites_root):
    (sites_root / "app").mkdir()
    with pytest.raises(ValidationError):
        files_service.move_item("app", "app")


def test_move_refuses_an_existing_destination(sites_root):
    (sites_root / "a.txt").write_text("a")
    (sites_root / "dest").mkdir()
    (sites_root / "dest" / "a.txt").write_text("already here")
    with pytest.raises(ValidationError):
        files_service.move_item("a.txt", "dest")


def test_move_rejects_traversal_in_destination(sites_root):
    (sites_root / "a.txt").write_text("a")
    with pytest.raises(ValidationError):
        files_service.move_item("a.txt", "../../etc")
    assert (sites_root / "a.txt").exists()  # nothing moved on failure


def test_bulk_move_reports_successes_and_failures(sites_root):
    (sites_root / "a.txt").write_text("a")
    (sites_root / "dest").mkdir()

    succeeded, failed = files_service.bulk_move(["a.txt", "missing.txt"], "dest")

    assert succeeded == ["a.txt"]
    assert failed[0][0] == "missing.txt"
    assert (sites_root / "dest" / "a.txt").read_text() == "a"
    assert not (sites_root / "a.txt").exists()


# --------------------------------------------------------------------------
# bulk_delete / bulk_copy
# --------------------------------------------------------------------------


# --------------------------------------------------------------------------
# chmod / chmod_recursive / bulk_chmod
# --------------------------------------------------------------------------


def test_chmod_sets_the_permission_bits(sites_root):
    import stat

    target = sites_root / "a.txt"
    target.write_text("a")

    files_service.chmod("a.txt", "600")

    assert stat.S_IMODE(target.stat().st_mode) == 0o600


def test_chmod_rejects_an_invalid_mode(sites_root):
    (sites_root / "a.txt").write_text("a")
    with pytest.raises(ValidationError):
        files_service.chmod("a.txt", "999")
    with pytest.raises(ValidationError):
        files_service.chmod("a.txt", "12345")


def test_chmod_refuses_the_root_directory(sites_root):
    with pytest.raises(ValidationError):
        files_service.chmod(".", "777")


def test_chmod_refuses_a_missing_path(sites_root):
    with pytest.raises(ValidationError):
        files_service.chmod("missing.txt", "644")


def test_bulk_chmod_reports_successes_and_failures(sites_root):
    (sites_root / "a.txt").write_text("a")
    (sites_root / "b.txt").write_text("b")

    succeeded, failed = files_service.bulk_chmod(["a.txt", "b.txt", "missing.txt"], "600")

    assert set(succeeded) == {"a.txt", "b.txt"}
    assert failed[0][0] == "missing.txt"


def test_chmod_recursive_applies_only_to_files(sites_root):
    import stat

    (sites_root / "app" / "nested").mkdir(parents=True)
    (sites_root / "app" / "file.txt").write_text("x")
    (sites_root / "app" / "nested" / "inner.txt").write_text("y")

    count = files_service.chmod_recursive("app", "600", "files")

    assert count == 2
    assert stat.S_IMODE((sites_root / "app" / "file.txt").stat().st_mode) == 0o600
    assert stat.S_IMODE((sites_root / "app" / "nested" / "inner.txt").stat().st_mode) == 0o600
    # The folder itself and the nested folder were not touched.
    assert stat.S_IMODE((sites_root / "app" / "nested").stat().st_mode) != 0o600


def test_chmod_recursive_applies_only_to_dirs(sites_root):
    import stat

    (sites_root / "app" / "nested").mkdir(parents=True)
    (sites_root / "app" / "file.txt").write_text("x")

    count = files_service.chmod_recursive("app", "700", "dirs")

    assert count == 1
    assert stat.S_IMODE((sites_root / "app" / "nested").stat().st_mode) == 0o700
    assert stat.S_IMODE((sites_root / "app" / "file.txt").stat().st_mode) != 0o700


def test_chmod_recursive_applies_to_both(sites_root):
    (sites_root / "app" / "nested").mkdir(parents=True)
    (sites_root / "app" / "file.txt").write_text("x")

    count = files_service.chmod_recursive("app", "750", "both")

    assert count == 2


def test_chmod_recursive_does_not_follow_a_symlinked_directory_outside_the_root(sites_root):
    """A symlink planted inside a site pointing outside the sites root must
    not let a recursive chmod reach files it doesn't own."""
    import os
    import stat

    outside = sites_root.parent / "outside"
    outside.mkdir()
    outside_file = outside / "secret.txt"
    outside_file.write_text("secret")
    os.chmod(outside_file, 0o644)

    (sites_root / "app").mkdir()
    os.symlink(outside, sites_root / "app" / "escape")

    files_service.chmod_recursive("app", "600", "both")

    assert stat.S_IMODE(outside_file.stat().st_mode) == 0o644


def test_chmod_recursive_rejects_a_file_target(sites_root):
    (sites_root / "a.txt").write_text("a")
    with pytest.raises(ValidationError):
        files_service.chmod_recursive("a.txt", "644", "files")


def test_chmod_recursive_rejects_an_invalid_scope(sites_root):
    (sites_root / "app").mkdir()
    with pytest.raises(ValidationError):
        files_service.chmod_recursive("app", "644", "everything")


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


def test_extract_archive_refuses_when_disk_does_not_have_room(sites_root, monkeypatch):
    with zipfile.ZipFile(sites_root / "bundle.zip", "w") as zf:
        zf.writestr("a.txt", "this is definitely more than five bytes")

    fake_usage = shutil.disk_usage("/").__class__(total=0, used=0, free=5)
    monkeypatch.setattr(files_service.shutil, "disk_usage", lambda path: fake_usage)

    with pytest.raises(ValidationError):
        files_service.extract_archive("bundle.zip")


def test_is_archive_name():
    assert files_service.is_archive_name("bundle.zip")
    assert files_service.is_archive_name("Bundle.ZIP")
    assert not files_service.is_archive_name("bundle.tar.gz")
    assert not files_service.is_archive_name("notes.txt")


# --------------------------------------------------------------------------
# Entry.editable
# --------------------------------------------------------------------------


def _entry(name, size=10):
    return files_service.Entry(
        name=name,
        path=name,
        relative=name,
        is_dir=False,
        size=size,
        modified=None,
        owner="root",
        mode="644",
        permissions="rw-r--r--",
        is_symlink=False,
    )


def test_bare_dotfiles_in_text_extensions_are_editable():
    # Path(".env").suffix is "" (pathlib treats the whole name as the stem),
    # so this regressed silently despite ".env" being in TEXT_EXTENSIONS.
    assert _entry(".env").editable
    assert _entry(".gitignore").editable
    assert _entry(".htaccess").editable


def test_named_file_with_text_extension_is_still_editable():
    assert _entry("config.env").editable
    assert _entry("notes.txt").editable


def test_unknown_extension_is_not_editable():
    assert not _entry("photo.png").editable
