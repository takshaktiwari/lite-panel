"""The only place lite-panel is allowed to execute a subprocess.

The daemon runs as root, so a shell-injected argument here is remote root code
execution.  Everything therefore goes through :func:`run` or :func:`stream`,
which accept an argument *list* only.  Nothing in this package passes a string
to a shell, and CI greps for ``shell=True`` / ``os.system`` to keep it that way.

Callers must still validate untrusted input with :mod:`app.validators` before
it reaches an argument list -- a list keeps a hostile value from becoming a
second *command*, but it will happily pass it as a *flag* to the one you ran.
"""

from __future__ import annotations

import logging
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Mapping, Optional, Sequence, Union

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT = 300
PATH = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"


class CommandError(RuntimeError):
    """A command exited non-zero (and the caller asked us to care)."""

    def __init__(self, result: "CommandResult") -> None:
        self.result = result
        super().__init__(
            f"{result.display} exited {result.returncode}: "
            f"{(result.stderr or result.stdout or '').strip()[:500]}"
        )


class CommandTimeout(RuntimeError):
    pass


@dataclass(frozen=True)
class CommandResult:
    args: tuple
    returncode: int
    stdout: str
    stderr: str

    @property
    def ok(self) -> bool:
        return self.returncode == 0

    @property
    def display(self) -> str:
        return " ".join(self.args)

    def lines(self) -> list:
        return [line for line in self.stdout.splitlines() if line.strip()]


def base_env(extra: Optional[Mapping] = None) -> dict:
    """A predictable environment for system commands.

    Inheriting the daemon's full environment invites surprises (locale-dependent
    output we then parse, a stray ``LD_PRELOAD``), so we build one explicitly.
    ``DEBIAN_FRONTEND`` keeps apt from blocking on a dialog nobody can answer.
    """
    env = {
        "PATH": PATH,
        "LC_ALL": "C",
        "LANG": "C",
        "DEBIAN_FRONTEND": "noninteractive",
        "HOME": "/root",
    }
    if extra:
        env.update({str(k): str(v) for k, v in extra.items()})
    return env


def _validate(args: Sequence) -> list:
    """Reject anything that is not a flat list of clean strings."""
    if isinstance(args, (str, bytes)):
        raise TypeError(
            "shell.run() takes an argument list, not a string -- passing a "
            "string here is how injection bugs happen"
        )
    if not args:
        raise ValueError("empty command")

    out = []
    for i, arg in enumerate(args):
        if isinstance(arg, Path):
            arg = str(arg)
        if not isinstance(arg, str):
            raise TypeError(f"argument {i} is {type(arg).__name__}, expected str: {arg!r}")
        if "\x00" in arg:
            raise ValueError(f"argument {i} contains a null byte")
        out.append(arg)
    return out


def run(
    args: Sequence,
    *,
    input: Optional[str] = None,
    timeout: int = DEFAULT_TIMEOUT,
    check: bool = True,
    env: Optional[Mapping] = None,
    cwd: Optional[Union[str, Path]] = None,
) -> CommandResult:
    """Run a command to completion and capture its output.

    ``input`` is written to stdin -- that is how secrets reach commands like
    ``chpasswd``, so they never appear in an argument list or a shell pipeline
    where ``ps`` would expose them.
    """
    argv = _validate(args)
    logger.debug("run: %s", " ".join(argv))

    try:
        proc = subprocess.run(  # noqa: S603 - argument list, never shell=True
            argv,
            input=input,
            capture_output=True,
            text=True,
            timeout=timeout,
            env=base_env(env),
            cwd=str(cwd) if cwd else None,
        )
    except FileNotFoundError as exc:
        raise CommandError(
            CommandResult(tuple(argv), 127, "", f"command not found: {argv[0]}")
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise CommandTimeout(f"{' '.join(argv)} timed out after {timeout}s") from exc

    result = CommandResult(tuple(argv), proc.returncode, proc.stdout or "", proc.stderr or "")
    if check and not result.ok:
        raise CommandError(result)
    return result


def stream(
    args: Sequence,
    on_line: Callable,
    *,
    timeout: int = 1800,
    env: Optional[Mapping] = None,
    cwd: Optional[Union[str, Path]] = None,
) -> int:
    """Run a command, delivering merged stdout/stderr one line at a time.

    Used by the job worker so that a five-minute ``apt install`` reports
    progress to the browser as it happens instead of going quiet and then
    dumping everything at the end.  Returns the exit code; unlike :func:`run`
    it never raises on non-zero, because the job record is where that outcome
    belongs.
    """
    argv = _validate(args)
    logger.debug("stream: %s", " ".join(argv))

    try:
        proc = subprocess.Popen(  # noqa: S603 - argument list, never shell=True
            argv,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
            env=base_env(env),
            cwd=str(cwd) if cwd else None,
        )
    except FileNotFoundError:
        on_line(f"command not found: {argv[0]}")
        return 127

    assert proc.stdout is not None
    try:
        for line in proc.stdout:
            on_line(line.rstrip("\n"))
        return proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()
        on_line(f"[timed out after {timeout}s]")
        return 124
    finally:
        proc.stdout.close()


def which(program: str) -> Optional[str]:
    """Locate a program using the same PATH commands will run with."""
    for directory in PATH.split(":"):
        candidate = os.path.join(directory, program)
        if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return candidate
    return None
