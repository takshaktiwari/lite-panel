"""SSH Access Keys service.

Manages SSH public/private key generation, key validation, fingerprinting,
and synchronization to ~/.ssh/authorized_keys for both root and site accounts.
"""

from __future__ import annotations

import logging
import os
import pwd
import shutil
import tempfile
from pathlib import Path
from typing import Iterable, List, Optional, Tuple

from sqlalchemy import select
from sqlalchemy.orm import Session as OrmSession

from app.models import Site, SshKey
from app.services import sites as sites_service
from app.shell import CommandError, run, which
from app.validators import ValidationError

logger = logging.getLogger(__name__)

MARKER = "# managed by lite-panel -- edits here are overwritten on the next change"
LOGIN_SHELL = "/bin/bash"


def is_available() -> bool:
    """Check if ssh-keygen is available on this system."""
    return which("ssh-keygen") is not None


def home_dir_for_user(user: str, db: Optional[OrmSession] = None) -> Path:
    """Determine the home directory for a system user (root or a site account)."""
    user = user.strip()
    if user == "root":
        return Path("/root")

    if db is not None:
        site = db.scalar(select(Site).where(Site.system_user == user))
        if site:
            return Path(site.root_dir)

    # Fallback to system pwd entry if available
    try:
        pw = pwd.getpwnam(user)
        return Path(pw.pw_dir)
    except (KeyError, OSError):
        # Default site root pattern
        return sites_service.sites_root() / user


def get_user_shell(user: str) -> Optional[str]:
    try:
        pw = pwd.getpwnam(user)
        return pw.pw_shell
    except (KeyError, OSError):
        return None


def set_user_shell(user: str, shell: str) -> None:
    """Set the system shell for a user."""
    if not which("chsh"):
        return
    try:
        run(["chsh", "-s", shell, user], check=False)
    except Exception as exc:
        logger.warning("could not set shell for %s to %s: %s", user, shell, exc)


def generate_key_pair(comment: str, key_type: str = "ed25519") -> Tuple[str, str, str]:
    """Generate a new SSH key pair.

    Returns:
        tuple of (private_key_pem_str, public_key_str, fingerprint_str)
    """
    if not is_available():
        raise ValidationError("ssh-keygen is not installed on this system.")

    valid_types = {"ed25519", "rsa"}
    if key_type not in valid_types:
        raise ValidationError(f"Invalid key type: {key_type}. Must be 'ed25519' or 'rsa'.")

    tmp_dir = tempfile.mkdtemp(prefix="lite-panel-ssh-")
    key_path = os.path.join(tmp_dir, "id_key")
    cmd = ["ssh-keygen", "-t", key_type]
    if key_type == "rsa":
        cmd.extend(["-b", "4096"])
    cmd.extend(["-N", "", "-C", comment.strip(), "-f", key_path])

    try:
        run(cmd)
        with open(key_path, "r", encoding="utf-8") as f:
            private_key = f.read()
        with open(f"{key_path}.pub", "r", encoding="utf-8") as f:
            public_key = f.read().strip()

        fingerprint, _ = parse_public_key(public_key)
        return private_key, public_key, fingerprint
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


def parse_public_key(public_key: str) -> Tuple[str, str]:
    """Validate a public key string and return (fingerprint, key_type).

    Raises ValidationError if the key is invalid.
    """
    key_str = public_key.strip()
    if not key_str:
        raise ValidationError("Public key cannot be empty.")

    # A valid OpenSSH public key consists of 2 or 3 whitespace-separated fields:
    # <type> <base64-blob> [<comment>]
    parts = key_str.split(None, 2)
    if len(parts) < 2:
        raise ValidationError("Invalid public key format. Expected '<type> <key-data> [comment]'.")

    key_type = parts[0]
    allowed_types = (
        "ssh-ed25519",
        "ssh-rsa",
        "ecdsa-sha2-nistp256",
        "ecdsa-sha2-nistp384",
        "ecdsa-sha2-nistp521",
        "sk-ssh-ed25519@openssh.com",
        "sk-ecdsa-sha2-nistp256@openssh.com",
    )
    if key_type not in allowed_types:
        raise ValidationError(f"Unsupported key type '{key_type}'.")

    if not is_available():
        # Fallback if ssh-keygen is not found
        return f"SHA256:unknown ({key_type})", key_type

    # Verify with ssh-keygen -lf -
    try:
        res = run(["ssh-keygen", "-lf", "-"], input=key_str + "\n")
        # Format: 256 SHA256:... comment (ED25519)
        output = (res.stdout or "").strip()
        tokens = output.split()
        if len(tokens) >= 2:
            fingerprint = tokens[1]
        else:
            fingerprint = output
        return fingerprint, key_type
    except (CommandError, OSError) as exc:
        raise ValidationError(f"The provided public key is invalid or malformed: {exc}")


def render_authorized_keys(keys: Iterable[SshKey]) -> str:
    """Render the contents of authorized_keys from active SshKey objects."""
    lines = [MARKER]
    for key in keys:
        clean = key.public_key.strip()
        if clean:
            lines.append(clean)
    return "\n".join(lines) + "\n"


def sync_authorized_keys(system_user: str, keys: Iterable[SshKey], home_dir: Optional[Path] = None) -> None:
    """Write ~/.ssh/authorized_keys for the user and ensure secure permissions.

    Sets permissions:
      ~/.ssh directory: 0700
      authorized_keys file: 0600
      Ownership: system_user:system_user (or root:root)
    """
    if home_dir is None:
        home_dir = home_dir_for_user(system_user)

    key_list = list(keys)
    ssh_dir = home_dir / ".ssh"
    auth_file = ssh_dir / "authorized_keys"

    if not key_list:
        # If no keys exist, clear or remove the file
        if auth_file.exists():
            try:
                auth_file.unlink()
            except OSError:
                pass
        # Revert site user's shell back to SITE_SHELL if not root
        if system_user != "root":
            set_user_shell(system_user, sites_service.SITE_SHELL)
        return

    # Ensure ~/.ssh exists with 0700 permissions
    ssh_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        os.chmod(ssh_dir, 0o700)
    except OSError:
        pass

    content = render_authorized_keys(key_list)
    auth_file.write_text(content, encoding="utf-8")
    try:
        os.chmod(auth_file, 0o600)
    except OSError:
        pass

    # Ensure ownership
    if system_user != "root":
        try:
            run(["chown", "-R", f"{system_user}:{system_user}", str(ssh_dir)], check=False)
        except Exception:
            pass

        # Switch shell to bash so the user can actually log in via SSH
        set_user_shell(system_user, LOGIN_SHELL)


def create_key(
    db: OrmSession,
    *,
    name: str,
    system_user: str,
    public_key: str,
    site_id: Optional[int] = None,
) -> SshKey:
    """Register a new public key in the database and project to authorized_keys."""
    name = name.strip()
    if not name:
        raise ValidationError("Key name is required.")
    if len(name) > 64:
        raise ValidationError("Key name must be at most 64 characters.")

    fingerprint, key_type = parse_public_key(public_key)

    # Check for duplicate fingerprint for the same system_user
    existing = db.scalar(
        select(SshKey).where(
            SshKey.system_user == system_user,
            SshKey.fingerprint == fingerprint,
        )
    )
    if existing:
        raise ValidationError(f"This SSH key is already registered for {system_user} as '{existing.name}'.")

    key_record = SshKey(
        name=name,
        system_user=system_user,
        site_id=site_id,
        public_key=public_key.strip(),
        fingerprint=fingerprint,
        key_type=key_type,
    )
    db.add(key_record)
    db.commit()

    # Re-sync authorized_keys for this user
    user_keys = db.scalars(select(SshKey).where(SshKey.system_user == system_user)).all()
    sync_authorized_keys(system_user, user_keys, home_dir_for_user(system_user, db))

    return key_record


def delete_key(db: OrmSession, key_record: SshKey) -> None:
    """Revoke an SSH key and update the user's authorized_keys file."""
    system_user = key_record.system_user
    db.delete(key_record)
    db.commit()

    user_keys = db.scalars(select(SshKey).where(SshKey.system_user == system_user)).all()
    sync_authorized_keys(system_user, user_keys, home_dir_for_user(system_user, db))
