# lite-panel

A minimal, self-hosted server control panel for Ubuntu / Debian VPS — a lightweight alternative to cPanel, Plesk, or CyberPanel for developers who only need the essentials.

Install with one command. Manage everything through a clean web UI.

---

## What it does

| Feature | Details |
|---|---|
| **Web stack** | Install Nginx + PHP (multiple versions side-by-side, via ondrej/php PPA) |
| **Sites** | Add domains, configure PHP version per site, auto-create system users & FPM pools |
| **SSL** | Let's Encrypt via Certbot — one click per domain |
| **Databases** | Create / delete MariaDB databases & users; browse with Adminer (session-gated) |
| **FTP** | Create vsftpd accounts scoped to each site's directory |
| **File manager** | Upload, download, rename, delete — contained to `/home` |
| **Stack tuning** | RAM-aware PHP-FPM worker sizing, OPcache and MariaDB settings computed at runtime |
| **Job log streaming** | Every privileged action runs as a background job with a live log tail in the UI |

---

## Requirements

- Ubuntu 20.04 / 22.04 / 24.04 or Debian 11 / 12
- A fresh VPS (1 GB RAM recommended; 512 MB works with the auto-swap the installer adds)
- Root / sudo access
- Ports 80 and 443 open in your firewall

---

## Installation

```bash
curl -fsSL https://raw.githubusercontent.com/takshaktiwari/lite-panel/main/install.sh | sudo bash
```

The installer will:

1. Check OS compatibility (Ubuntu / Debian only)
2. Install system dependencies (`nginx`, `python3-venv`, `php-cli`, `openssl`, `git`, `curl`)
3. Add a 1 GB swapfile automatically if RAM ≤ 512 MB and no swap exists
4. Clone this repo to `/opt/lite-panel` and create a Python venv
5. Generate a random `secret_key` and write `/etc/lite-panel/panel.env`
6. Create a self-signed TLS certificate (valid 10 years; replace with Let's Encrypt later from the Stack tab)
7. Write and enable the nginx vhost at `/etc/nginx/sites-available/lite-panel`
8. Install and start two systemd units: `lite-panel.service` (uvicorn) and `lite-panel-adminer.service` (php -S)
9. Initialise the SQLite database and create an `admin` account with a random password
10. Run a health check and print the URL + credentials

At the end you will see:

```
╔══════════════════════════════════════════════════╗
║        🎉  lite-panel installed!                 ║
╠══════════════════════════════════════════════════╣
║  Panel URL  : https://your-server-ip/
║  Username   : admin
║  Password   : <random-generated>
║
║  ⚠  Self-signed certificate — browser will warn.
║     Install Certbot later via the panel's Stack tab.
║
║  Logs       : journalctl -u lite-panel -f
║  Config     : /etc/lite-panel/panel.env
║  Uninstall  : sudo bash /opt/lite-panel/uninstall.sh
╚══════════════════════════════════════════════════╝
```

### Re-running the installer

The install is **idempotent** — you can run it again safely. Each step checks if it is already done and skips itself. To force a full reset:

```bash
sudo bash /opt/lite-panel/install.sh --force-reinstall
```

---

## After installation

### First steps in the UI

1. **Stack tab** — install Nginx (required), one or more PHP versions, and optionally MariaDB, Certbot, vsftpd
2. **Sites tab** — add your first domain
3. **SSL** — click "Enable SSL" on the site once DNS points at the server

### Replace the self-signed certificate

Once Certbot is installed via the Stack tab, click **Enable SSL** on any site to get a Let's Encrypt certificate automatically.

---

## Management commands

All commands run from the venv at `/opt/lite-panel/venv/bin/python -m app.cli`:

```bash
# Reset the admin password
echo "newpassword" | sudo /opt/lite-panel/venv/bin/python -m app.cli reset-password --username admin

# Re-render all panel-managed config files from the database (recovery tool)
sudo /opt/lite-panel/venv/bin/python -m app.cli rebuild

# Generate a random password
sudo /opt/lite-panel/venv/bin/python -m app.cli generate-password --length 24
```

### Service management

```bash
sudo systemctl status lite-panel            # panel status
sudo systemctl restart lite-panel           # restart after config change
sudo journalctl -u lite-panel -f            # live logs
sudo journalctl -u lite-panel-adminer -f    # Adminer PHP server logs
```

---

## Uninstallation

```bash
sudo bash /opt/lite-panel/uninstall.sh
```

Site files under `/home` are **not removed** by default. To also delete them:

```bash
sudo bash /opt/lite-panel/uninstall.sh --remove-sites
```

---

## Architecture

```
curl | bash
    └── install.sh
            ├── apt: nginx python3-venv php-cli openssl git curl
            ├── /opt/lite-panel/         ← repo clone
            │     ├── panel/             ← FastAPI app (Python)
            │     ├── assets/
            │     │     ├── templates/   ← Jinja2 config templates
            │     │     └── vendor/      ← Adminer 4.8.1 (vendored)
            │     ├── install.sh
            │     └── uninstall.sh
            ├── /etc/lite-panel/panel.env     ← secret_key + paths
            ├── /var/lib/lite-panel/panel.db  ← SQLite database
            ├── /etc/nginx/sites-available/lite-panel
            └── systemd:
                  lite-panel.service           ← uvicorn on 127.0.0.1:8765
                  lite-panel-adminer.service   ← php -S on 127.0.0.1:8766
```

**nginx** terminates TLS, enforces the session gate in front of Adminer (`auth_request`), disables buffering on the SSE job-log stream, and reverse-proxies everything else to uvicorn.

**The panel never binds a public port.** The FastAPI app and the Adminer PHP server both listen on loopback only.

---

## Security design

- Every untrusted value passes through `validators.py` before use
- Every subprocess call goes through `shell.py` using argument lists — never shell strings
- Adminer is behind the panel's session gate (`/internal/auth-check`) — it cannot be accessed without a valid panel login, and its connection is pinned to the local MariaDB socket (the server field is removed from the login form)
- The panel's own nginx vhost sends strict security headers (`HSTS`, `X-Frame-Options: DENY`, `X-Content-Type-Options`, `CSP`)
- Passwords and FTP credentials are never stored in the database

---

## Development

### Run locally (macOS, no server needed)

```bash
git clone https://github.com/takshaktiwari/lite-panel
cd lite-panel
python3 -m venv .venv && .venv/bin/pip install -r panel/requirements.txt

export LITE_PANEL_DEV_MODE=true LITE_PANEL_SECRET_KEY=dev PYTHONPATH=panel
export LITE_PANEL_DATABASE_URL="sqlite:///$PWD/dev.db"
echo "devpassword123" | .venv/bin/python -m app.cli create-admin --username admin
.venv/bin/python -m uvicorn app.main:app --port 8799
# open http://127.0.0.1:8799
# services will show "not installed" — correct, no systemd on macOS
```

### Tests

```bash
.venv/bin/python -m pytest          # 163 tests, all pure-Python, no server needed
```

### Sync to a dev server

```bash
bash dev/sync.sh          # rsync + restart; reads IP/key from server-details.md
bash dev/sync.sh --watch  # re-sync on every local file change (requires fswatch)
```

---

## What's not in v1

- Email / SMTP configuration
- Apache (Nginx only)
- Multi-user / RBAC (single admin account)
- Swap provisioning UI (handled automatically by install.sh on tiny instances)

---

## License

MIT
