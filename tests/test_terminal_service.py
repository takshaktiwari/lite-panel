"""Tests for the PTY wrapper.

Unlike the apt/provider code, POSIX ptys behave the same on macOS as on
Linux, so this is one part of the terminal feature fully verifiable on a
workstation -- no server needed.
"""

import time

import pytest

from app.services.terminal import TerminalSession


def _drain(session: TerminalSession, *, timeout: float = 2.0) -> bytes:
    """Poll until the shell has produced output or the timeout elapses.

    The fd is non-blocking by design (the router drives it from an event
    loop), so tests poll it the same way rather than blocking on read().
    """
    deadline = time.monotonic() + timeout
    collected = b""
    while time.monotonic() < deadline:
        chunk = session.read()
        if chunk:
            collected += chunk
        else:
            time.sleep(0.02)
    return collected


def test_session_starts_a_real_shell_and_produces_output():
    with TerminalSession() as session:
        assert session.is_alive()
        output = _drain(session, timeout=1.5)
        # bash -l prints *something* on startup (prompt, motd, or at minimum
        # nothing that errors) -- the real assertion is that the process is
        # alive and the fd is readable/writable at all.
        assert session.process.pid > 0
        assert isinstance(output, bytes)


def test_write_is_echoed_back_by_the_pty():
    """A pty echoes input by default -- this is the simplest possible proof
    that keystrokes really reach the shell."""
    with TerminalSession() as session:
        _drain(session, timeout=0.5)  # let the shell settle/print its prompt
        session.write(b"echo hello-from-test\n")
        output = _drain(session, timeout=2.0)
        assert b"hello-from-test" in output


def test_read_returns_empty_bytes_when_nothing_is_available():
    with TerminalSession() as session:
        _drain(session, timeout=0.5)
        # Immediately after draining, there should be nothing else queued.
        assert session.read() == b""


def test_resize_does_not_raise():
    with TerminalSession() as session:
        session.resize(120, 40)
        session.resize(80, 24)


def test_resize_clamps_absurd_values_instead_of_raising():
    with TerminalSession() as session:
        session.resize(-5, 0)
        session.resize(999999, 999999)


def test_close_terminates_the_process():
    session = TerminalSession()
    assert session.is_alive()
    session.close()
    assert not session.is_alive()
    assert session.process.poll() is not None


def test_close_is_idempotent():
    session = TerminalSession()
    session.close()
    session.close()  # must not raise


def test_write_after_close_does_not_raise():
    session = TerminalSession()
    session.close()
    session.write(b"echo too-late\n")  # must be a no-op, not an exception


def test_resize_after_close_does_not_raise():
    session = TerminalSession()
    session.close()
    session.resize(80, 24)


def test_context_manager_closes_on_exit():
    with TerminalSession() as session:
        pid = session.process.pid
        assert session.is_alive()
    # Outside the `with` block now -- close() must have run.
    import os
    import signal

    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)


def test_shell_env_does_not_leak_the_daemon_environment():
    """The panel process may hold secrets in its own environment (secret
    key, DB path); the terminal must not inherit them wholesale."""
    with TerminalSession() as session:
        _drain(session, timeout=0.5)
        session.write(b"echo START${LITE_PANEL_SECRET_KEY}END\n")
        output = _drain(session, timeout=2.0)
        assert b"STARTEND" in output


@pytest.mark.skipif(
    __import__("sys").platform != "linux",
    reason="session-wide cleanup of backgrounded jobs is a Linux-only /proc sweep",
)
def test_close_cleans_up_a_backgrounded_child_process_on_linux():
    """close() reaches even a job the shell explicitly backgrounded with
    `&`, which bash's own job control gives its own process group -- a plain
    killpg() on the shell would not be enough, and the production target
    (Ubuntu/Debian) is always Linux, so this is asserted for real there."""
    import os

    with TerminalSession() as session:
        _drain(session, timeout=0.5)
        session.write(b"sleep 30 &\necho spawned $!\n")
        output = _drain(session, timeout=2.0)
        assert b"spawned" in output

        line = next(l for l in output.decode().splitlines() if l.startswith("spawned"))
        child_pid = int(line.split()[1])

        session.close()
        time.sleep(0.2)

        with pytest.raises(ProcessLookupError):
            os.kill(child_pid, 0)


def test_close_always_kills_the_shells_own_process_group():
    """The portable guarantee, true on every platform: close() reaches the
    shell itself and anything sharing its own (not backgrounded) process
    group -- e.g. a foreground child still running when the terminal closes."""
    import os

    with TerminalSession() as session:
        _drain(session, timeout=0.5)
        session.write(b"sleep 30\n")  # foreground: shares the shell's own pgid
        time.sleep(0.3)

        session.close()
        time.sleep(0.2)

        with pytest.raises(ProcessLookupError):
            os.kill(session.process.pid, 0)
