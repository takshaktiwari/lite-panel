"""Tests for the subprocess wrapper.

The point of these is not that ``echo`` works -- it's that the wrapper refuses
the shapes that turn a root daemon into a root shell.
"""

import pytest

from app import shell


def test_run_captures_output():
    result = shell.run(["echo", "hello"])
    assert result.ok
    assert result.stdout.strip() == "hello"
    assert result.returncode == 0


def test_run_rejects_a_string_command():
    """The single most important assertion in this file: a string would mean
    someone was about to reach for a shell."""
    with pytest.raises(TypeError):
        shell.run("echo hello")


def test_run_does_not_interpret_shell_metacharacters():
    """A hostile argument stays an argument -- it never becomes a command."""
    payload = "; touch /tmp/lite-panel-pwned"
    result = shell.run(["echo", payload])
    assert payload in result.stdout
    import os

    assert not os.path.exists("/tmp/lite-panel-pwned")


def test_run_rejects_non_string_arguments():
    with pytest.raises(TypeError):
        shell.run(["echo", 42])


def test_run_rejects_null_bytes():
    with pytest.raises(ValueError):
        shell.run(["echo", "a\x00b"])


def test_run_rejects_empty_command():
    with pytest.raises(ValueError):
        shell.run([])


def test_run_accepts_path_arguments(tmp_path):
    result = shell.run(["ls", tmp_path])
    assert result.ok


def test_run_raises_on_failure_by_default():
    with pytest.raises(shell.CommandError):
        shell.run(["false"])


def test_run_can_tolerate_failure():
    result = shell.run(["false"], check=False)
    assert not result.ok
    assert result.returncode != 0


def test_missing_command_raises_command_error():
    with pytest.raises(shell.CommandError):
        shell.run(["lite-panel-definitely-not-a-real-binary"])


def test_input_reaches_stdin():
    """Secrets travel via stdin, never as arguments where ps would see them."""
    result = shell.run(["cat"], input="s3cret")
    assert result.stdout == "s3cret"


def test_timeout_raises():
    with pytest.raises(shell.CommandTimeout):
        shell.run(["sleep", "5"], timeout=1)


def test_stream_delivers_lines_in_order():
    lines = []
    code = shell.stream(["printf", "one\\ntwo\\nthree\\n"], lines.append)
    assert code == 0
    assert lines == ["one", "two", "three"]


def test_stream_reports_exit_code_without_raising():
    lines = []
    assert shell.stream(["false"], lines.append) != 0


def test_stream_reports_missing_command():
    lines = []
    assert shell.stream(["lite-panel-not-real"], lines.append) == 127
    assert any("not found" in line for line in lines)


def test_base_env_is_predictable():
    env = shell.base_env()
    assert env["DEBIAN_FRONTEND"] == "noninteractive"
    assert env["LC_ALL"] == "C"
    assert "PATH" in env
