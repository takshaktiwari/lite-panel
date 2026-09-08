"""A real interactive shell, bridged over a PTY.

This is the one deliberate exception to the rule in :mod:`app.shell`: every
other subprocess in this codebase takes an argument list and never touches a
shell, specifically to keep untrusted input from becoming a command. A web
terminal is, by definition, the opposite of that -- an unstructured,
unvalidated shell an operator drives directly. Do not use this module as
precedent for adding a shell anywhere else; it exists because a terminal was
explicitly requested, is gated behind a valid panel session the same as
every other page, and is the one place in the app where "unstructured" is
the whole point.

This module is intentionally just an OS-resource wrapper -- no networking,
no auth, no asyncio. That keeps it testable on its own (including on macOS,
since POSIX ptys work the same everywhere) and keeps the websocket plumbing
in the router where it belongs.
"""

from __future__ import annotations

import fcntl
import logging
import os
import pty
import signal
import struct
import subprocess
import sys
import termios
import time
from typing import Optional

logger = logging.getLogger(__name__)

# Given to the child shell. Deliberately not the daemon's own environment:
# that could carry secrets (LITE_PANEL_SECRET_KEY, etc.) from panel.env into
# an interactive shell that has no reason to see them.
_SHELL_ENV = {
    "TERM": "xterm-256color",
    "HOME": "/root",
    "PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
    "LANG": "C.UTF-8",
    "LC_ALL": "C.UTF-8",
}

# How long to wait after SIGTERM before escalating to SIGKILL.
_KILL_GRACE_SECONDS = 2.0


class TerminalSession:
    """One PTY-backed shell process.

    Not thread-safe by itself and not meant to be shared across requests --
    one instance per open terminal, owned by the websocket handler that
    created it.
    """

    def __init__(self, *, shell: str = "/bin/bash", cwd: Optional[str] = None) -> None:
        """``cwd=None`` (the default) inherits the daemon's own working
        directory rather than hardcoding a Linux-specific path -- callers on
        a real server pass ``cwd="/root"`` explicitly (see routers/terminal.py);
        leaving the default portable is what keeps this testable on macOS,
        where ``/root`` doesn't exist."""
        master_fd, slave_fd = pty.openpty()
        self.master_fd = master_fd
        self._closed = False

        try:
            self.process = subprocess.Popen(
                [shell, "-l"],
                stdin=slave_fd,
                stdout=slave_fd,
                stderr=slave_fd,
                cwd=cwd,
                env=dict(_SHELL_ENV),
                # New session/process-group leader so the shell (and anything
                # it spawns) can be killed as a unit, and so it gets proper
                # controlling-terminal behavior from the pty.
                start_new_session=True,
                close_fds=True,
            )
        except Exception:
            os.close(master_fd)
            os.close(slave_fd)
            raise

        # The child has its own copy via inheritance; the parent's fd for the
        # slave side is now just a handle we don't need and must not leak.
        os.close(slave_fd)

        # Non-blocking reads: the router polls this fd via the asyncio event
        # loop rather than blocking a thread on it.
        flags = fcntl.fcntl(master_fd, fcntl.F_GETFL)
        fcntl.fcntl(master_fd, fcntl.F_SETFL, flags | os.O_NONBLOCK)

        logger.info("terminal session started: pid=%s", self.process.pid)

    # -- I/O -----------------------------------------------------------

    def read(self, max_bytes: int = 65536) -> bytes:
        """Read whatever output is currently available.

        Returns b"" if nothing is available right now (the fd is
        non-blocking) -- callers driving this from an event loop should only
        call it when the loop has signaled the fd is readable.
        """
        try:
            return os.read(self.master_fd, max_bytes)
        except BlockingIOError:
            return b""
        except OSError:
            # EIO is what a pty gives you once the child side has exited.
            return b""

    def write(self, data: bytes) -> None:
        if self._closed:
            return
        try:
            os.write(self.master_fd, data)
        except OSError as exc:
            logger.debug("write to terminal fd failed (session likely closing): %s", exc)

    def resize(self, cols: int, rows: int) -> None:
        """Tell the pty its new size so full-screen programs (vim, htop,
        less) redraw correctly instead of wrapping at the wrong width."""
        if self._closed:
            return
        cols = max(1, min(int(cols), 1000))
        rows = max(1, min(int(rows), 1000))
        try:
            winsize = struct.pack("HHHH", rows, cols, 0, 0)
            fcntl.ioctl(self.master_fd, termios.TIOCSWINSZ, winsize)
        except OSError as exc:
            logger.debug("resize failed (session likely closing): %s", exc)

    # -- lifecycle -------------------------------------------------------

    def is_alive(self) -> bool:
        return not self._closed and self.process.poll() is None

    def close(self) -> None:
        """Terminate the shell and release the pty. Safe to call more than
        once."""
        if self._closed:
            return
        self._closed = True

        if self.process.poll() is None:
            self._kill_session(signal.SIGTERM)

            deadline = time.monotonic() + _KILL_GRACE_SECONDS
            while self.process.poll() is None and time.monotonic() < deadline:
                time.sleep(0.05)

            if self.process.poll() is None:
                self._kill_session(signal.SIGKILL)
                self.process.wait(timeout=2)

        try:
            os.close(self.master_fd)
        except OSError:
            pass

        logger.info("terminal session closed: pid=%s", self.process.pid)

    def _kill_session(self, sig: int) -> None:
        """Signal the shell's process group, and -- on Linux -- every other
        process group sharing its session id.

        Killing just the shell's own process group is not enough: bash's job
        control gives each backgrounded command (``sleep 30 &``) its own
        process group by design, precisely so ``fg``/``bg``/Ctrl-Z can target
        it independently. That means a background job survives a plain
        ``killpg`` on the shell, and would otherwise be orphaned when the
        terminal closes. All of those groups still share one session (the
        pty's session, created via ``start_new_session=True``), so sweeping
        by session id on Linux via /proc reaches them too. Best-effort only:
        the shell's own group is always signaled regardless of platform.
        """
        try:
            os.killpg(self.process.pid, sig)
        except ProcessLookupError:
            pass
        except OSError as exc:
            logger.debug("signal %s to terminal process group failed: %s", sig, exc)

        if sys.platform != "linux":
            return

        try:
            sid = os.getsid(self.process.pid)
        except OSError:
            return

        for entry in os.listdir("/proc"):
            if not entry.isdigit():
                continue
            pid = int(entry)
            try:
                with open(f"/proc/{pid}/stat", "r") as handle:
                    fields = handle.read().split()
                # field 6 (0-indexed 5) is session id, per proc(5).
                proc_sid = int(fields[5])
                pgid = int(fields[4])
            except (OSError, IndexError, ValueError):
                continue
            if proc_sid != sid:
                continue
            try:
                os.killpg(pgid, sig)
            except (ProcessLookupError, OSError):
                pass

    def __enter__(self) -> "TerminalSession":
        return self

    def __exit__(self, *_exc) -> None:
        self.close()
