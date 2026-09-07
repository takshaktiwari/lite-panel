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

    password = _read_password(args)
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
    print(f"password reset for '{args.username}'; all sessions revoked")
    return 0


def cmd_generate_password(args) -> int:
    print(generate_password(args.length))
    return 0


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
    reset.add_argument("--password", help="omit to read from stdin (preferred)")
    reset.set_defaults(func=cmd_reset_password)

    gen = sub.add_parser("generate-password", help="print a random password")
    gen.add_argument("--length", type=int, default=20)
    gen.set_defaults(func=cmd_generate_password)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
