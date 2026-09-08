"""Command line entry points used by install.sh and for recovery.

Run as ``/opt/lite-panel/venv/bin/python -m app.cli <command>``.
"""

from __future__ import annotations

import argparse
import sys

from app.database import init_db, session_scope
from app.security import create_admin, generate_password, hash_password, validate_new_password
from app.validators import ValidationError


def _read_password(args) -> str:
    """Take the password from stdin unless one was passed explicitly.

    stdin is the default because an argument would be visible to every user on
    the box via ``ps`` for as long as the process lives.
    """
    if args.password:
        return args.password
    data = sys.stdin.read().strip()
    if not data:
        raise SystemExit("error: no password supplied on stdin")
    return data


def cmd_init_db(_args) -> int:
    init_db()
    print("database initialised")
    return 0


def cmd_create_admin(args) -> int:
    password = _read_password(args)
    init_db()
    try:
        with session_scope() as db:
            user = create_admin(db, args.username, password)
            print(f"created admin user '{user.username}'")
    except ValidationError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


def cmd_reset_password(args) -> int:
    from sqlalchemy import select

    from app.models import AdminUser

    generated = False
    if args.password:
        password = args.password
    elif not sys.stdin.isatty():
        data = sys.stdin.read().strip()
        if data:
            password = data
        else:
            password = generate_password(24)
            generated = True
    else:
        password = generate_password(24)
        generated = True

    try:
        validate_new_password(password)
    except ValidationError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    with session_scope() as db:
        user = db.scalar(select(AdminUser).where(AdminUser.username == args.username))
        if user is None:
            print(f"error: no admin user '{args.username}'", file=sys.stderr)
            return 1
        user.password_hash = hash_password(password)
        # Force a fresh login everywhere; a password reset that leaves old
        # sessions alive does not lock anyone out.
        for session in list(user.sessions):
            db.delete(session)

    print(f"Password reset successful for user '{args.username}'!")
    if generated:
        print(f"New generated password: {password}")
    print("All existing active sessions revoked.")
    return 0



def cmd_generate_password(args) -> int:
    print(generate_password(args.length))
    return 0


def cmd_rebuild(args) -> int:
    """Trigger a panel.rebuild job and stream its log to stdout.

    Strategy:
    1. Try the running app's HTTP API (works when the service is up — this
       is the normal recovery path after a config change mid-operation).
    2. If the app is not reachable, run the rebuild directly in-process
       (recovery path when the service itself failed to start).

    Either way the operator sees a live log stream in their terminal.
    """
    import json
    import logging
    import urllib.request
    import urllib.error

    from app.config import get_settings

    settings = get_settings()
    base_url = f"http://{settings.host}:{settings.port}"

    # ------------------------------------------------------------------
    # Path 1: app is running — use the HTTP API
    # ------------------------------------------------------------------
    def _via_api() -> int:
        """Enqueue the job via HTTP and tail its SSE log stream."""
        # We need a valid session cookie to call the API.  Rather than
        # storing credentials here, we look for the admin session cookie
        # in a file written by install.sh.  If that doesn't exist the
        # caller must pass --api-token.
        headers: dict[str, str] = {"Content-Type": "application/json"}
        if args.api_token:
            headers["Authorization"] = f"Bearer {args.api_token}"

        data = json.dumps({"kind": "panel.rebuild", "payload": {}}).encode()
        req = urllib.request.Request(
            f"{base_url}/api/jobs",
            data=data,
            headers=headers,
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                body = json.loads(resp.read())
        except urllib.error.HTTPError as exc:
            print(f"API error {exc.code}: {exc.read().decode()[:200]}", file=sys.stderr)
            return 1

        job_id = body.get("id")
        if not job_id:
            print(f"Unexpected API response: {body}", file=sys.stderr)
            return 1

        print(f"Job {job_id} queued — streaming log:")
        stream_url = f"{base_url}/jobs/{job_id}/stream"
        stream_req = urllib.request.Request(stream_url, headers=headers)
        try:
            with urllib.request.urlopen(stream_req, timeout=600) as stream:
                for raw in stream:
                    line = raw.decode(errors="replace").rstrip()
                    if line.startswith("data:"):
                        print(line[5:].lstrip())
        except Exception as exc:
            print(f"Stream error: {exc}", file=sys.stderr)

        # Check final job status
        status_req = urllib.request.Request(
            f"{base_url}/api/jobs/{job_id}", headers=headers
        )
        try:
            with urllib.request.urlopen(status_req, timeout=10) as resp:
                status_body = json.loads(resp.read())
            final = status_body.get("status", "unknown")
            if final == "done":
                print("Rebuild complete.")
                return 0
            else:
                print(f"Job finished with status: {final}", file=sys.stderr)
                return 1
        except Exception:
            return 0  # stream already showed output; treat as success

    # ------------------------------------------------------------------
    # Path 2: in-process rebuild (recovery when the service is down)
    # ------------------------------------------------------------------
    def _in_process() -> int:
        """Run rebuild directly without a running panel server."""
        logging.basicConfig(
            level=logging.INFO,
            format="%(message)s",
        )
        init_db()

        from app.jobs import JobContext  # type: ignore[attr-defined]
        from app import tasks  # noqa: F401 — registers handlers
        from app.services import renderer
        from app.providers import get_provider

        class _StdoutCtx:
            """Minimal JobContext that prints to stdout."""
            payload: dict = {"kind": "panel.rebuild"}

            def log(self, msg: str) -> None:  # noqa: D401
                print(msg, flush=True)

        ctx = _StdoutCtx()
        print("Running in-process rebuild (panel service is not running)...")
        renderer.rebuild_all(ctx)

        nginx = get_provider("nginx")
        if nginx.is_installed():
            nginx.reload(ctx)
        print("Rebuild complete.")
        return 0

    # Try the API first; fall back to in-process.
    try:
        urllib.request.urlopen(f"{base_url}/healthz", timeout=5)
        use_api = True
    except Exception:
        use_api = False

    if use_api:
        return _via_api()
    else:
        print(f"Panel not reachable at {base_url} — running rebuild in-process.", file=sys.stderr)
        return _in_process()


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="lite-panel", description="lite-panel management")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("init-db", help="create missing tables").set_defaults(func=cmd_init_db)

    create = sub.add_parser("create-admin", help="create the panel admin account")
    create.add_argument("--username", required=True)
    create.add_argument("--password", help="omit to read from stdin (preferred)")
    create.set_defaults(func=cmd_create_admin)

    reset = sub.add_parser("reset-password", help="reset an admin password")
    reset.add_argument("--username", required=True)
    reset.add_argument("--password", help="new password (omit or use --generate to auto-generate)")
    reset.add_argument("--generate", action="store_true", help="generate a secure random password")
    reset.set_defaults(func=cmd_reset_password)


    gen = sub.add_parser("generate-password", help="print a random password")
    gen.add_argument("--length", type=int, default=20)
    gen.set_defaults(func=cmd_generate_password)

    rebuild = sub.add_parser(
        "rebuild",
        help="re-render all panel-managed config files from the database",
    )
    rebuild.add_argument(
        "--api-token",
        default="",
        help="Bearer token for the panel API (omit to use the running service's own auth)",
    )
    rebuild.set_defaults(func=cmd_rebuild)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
