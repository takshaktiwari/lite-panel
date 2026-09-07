# lite-panel — handoff notes

Read this before touching anything else. The full original design is at
`~/.claude/plans/streamed-dreaming-rocket.md` on the machine that created it —
if you don't have that file, this document plus the code is enough to continue.

## What this is

A minimal, self-hosted control panel for a bare Ubuntu/Debian VPS — a light
alternative to cPanel/Plesk/CyberPanel for someone who only needs: install a
web stack, add a domain + SSL, create a database (browsed via Adminer),
create an FTP account, browse files. Installed with one `curl | bash`, then
used entirely through a web UI afterward.

Backend: **Python (FastAPI + Uvicorn)**. GitHub repo: `takshaktiwari/lite-panel`,
branch `main`.

## The one idea that governs every design choice here

**Nothing is decided once and frozen.** The first attempt at this plan got
rejected for exactly that mistake — it had a fixed PHP version and a hardcoded
extension list, copied from the old bash scripts. The correction: a panel's
whole value is that choices stay changeable at runtime. So:

- PHP versions and extensions are **discovered from apt at runtime**
  (`app/services/apt.py` + `app/providers/php.py`), never hardcoded. Several
  versions install side by side; each site picks one.
- The panel **writes only its own config files**, rendered from Jinja2
  templates + database state (`app/services/renderer.py`), and never edits a
  vendor config in place. Every render is idempotent, so
  `panel.rebuild` can reconstruct the whole server's config from the DB at any time.
- RAM-based tuning (`app/services/tuning.py`) is **computed on demand**, not
  a one-shot `sed`, and every value has a per-key override stored in the DB.

If you're about to hardcode something a person would reasonably want to
change later, stop — that's the anti-pattern this project exists to avoid.

## Where things stand — build order from the plan

| # | Step | Status |
|---|---|---|
| 1 | Skeleton: config, DB, models, security, validators, shell, jobs | ✅ Done |
| 2 | Auth + login UI, dashboard | ✅ Done |
| 3 | Providers: `base.py`, `nginx.py`, `php.py` (+ `mariadb.py`, `vsftpd.py`, `certbot.py`) | ✅ Done |
| 4 | `renderer.py` + `sites.py` (Site create/delete) | ✅ Done |
| 5 | Setup wizard (`routers/setup.py`) + Stack UI | ✅ Done |
| 6 | MariaDB + databases, certbot + SSL, vsftpd + FTP | ✅ Done |
| 7 | File manager | ✅ Done |
| 8 | Adminer + `auth_request` gate | ✅ Done — vhost template + gate wired |
| 9 | `install.sh`, `uninstall.sh`, `panel rebuild` CLI | ✅ Done |
| 10 | README.md | ✅ Done |
| 11 | First real server deploy + debug | ✅ Done — panel live on test server |

**163 tests passing** (`.venv/bin/python -m pytest`), all runnable on a
workstation, no server needed.

**The panel is fully built and deployed.** Login, dashboard, every page
renders. All privileged actions work. The test server is running it live.

## What was done in this session (2026-09-07)

This session picked up where the previous quota-limited session left off.
Everything below was completed in this sitting:

### Step 8 — Adminer nginx auth gate (previously missing)
- Created `assets/templates/panel-vhost.conf.j2` — the panel's own nginx vhost
  with SSL on 443, HTTP→HTTPS redirect, `auth_request /internal/auth-check`
  gate on `/adminer/` (redirects to login on 401), `proxy_buffering off` on
  `/jobs/<id>/stream` so the SSE job log streams live instead of buffering
  until the job ends, and an `internal;` subrequest location so the auth
  endpoint can't be hit from the outside.

### Step 9 — Full delivery layer
- `assets/units/lite-panel.service` — uvicorn FastAPI daemon (root, port 8765,
  logs to `/var/log/lite-panel/panel.log`)
- `assets/units/lite-panel-adminer.service` — `php -S 127.0.0.1:8766`, user
  `www-data`, loopback only
- `install.sh` — 10-step idempotent installer:
  1. Root + OS check (Ubuntu/Debian only)
  2. apt bootstrap (python3-venv, nginx, php-cli, openssl, git, curl)
  3. **Auto swap** — if RAM ≤ 512 MB and no swap exists, creates a 1 GB swapfile
     automatically. Critical for the 448 MB test server; MariaDB OOM-kills
     without it.
  4. Clone repo to `/opt/lite-panel` (or rsync from local if `$SRC_DIR` set)
  5. Python venv + `pip install -r panel/requirements.txt`
  6. Generate `secret_key` via `openssl rand -hex 32`; write `/etc/lite-panel/panel.env`
  7. Self-signed TLS cert → `/etc/lite-panel/tls/` (valid 10 years)
  8. Render panel nginx vhost from template (inline Python regex, no Jinja2
     runtime needed); symlink into `sites-enabled`; disable default nginx site
  9. Install + `systemctl enable --now` both units
  10. `nginx -t && systemctl reload nginx`; health-check `/healthz` (retry ×5);
      generate random admin password; print 🎉 banner with URL + credentials
  - Re-running is safe (idempotent). `--force-reinstall` to reset.
  - `--no-swap` to skip auto-swap if you manage swap yourself.
- `uninstall.sh` — stops units, removes nginx vhost, clears `/opt/lite-panel`,
  `/etc/lite-panel`, `/var/lib/lite-panel`, `/var/log/lite-panel`. Leaves
  `/home` tenant site files unless `--remove-sites` is passed.
- `dev/sync.sh` — rsync + restart helper for EC2 dev loop. Reads IP and key
  from `server-details.md`. Uses `--rsync-path="sudo rsync"` so it can write
  to `/opt` (root-owned) as the `ubuntu` user. `--watch` mode with fswatch.
- `panel/app/cli.py` — added `panel rebuild` subcommand. Tries the running
  app's HTTP API first; falls back to in-process rebuild when the service is
  down (recovery path).
- `tests/test_cli.py` — 9 new tests covering rebuild in-process path, renderer
  failure propagation, nginx-not-installed edge case, API path success and failure.

### Step 10 — README.md
- `README.md` at repo root with: one-liner install, step-by-step installer
  breakdown, completion banner example, management commands, architecture
  ASCII diagram, security design notes, local dev setup, test suite command.

### Step 11 — First real server deploy (test server, 2026-09-07 ~22:00 IST)

**Server:** Ubuntu 26.04 LTS, x86_64, **448 MB RAM**, public IP `13.233.146.180`
(in `server-details.md`). AWS EC2, `ap-south-1`.

**Issues encountered and fixed during deploy:**

1. `bash dev/sync.sh # comment` — bash passes `#` as an argument. Not a bug;
   the user just shouldn't append inline comments to bash invocations. No fix needed.

2. `rsync: mkdir "/opt/lite-panel" failed: Permission denied` — `/opt` is
   root-owned; the `ubuntu` user needs `sudo rsync` on the remote.
   **Fix:** added `--rsync-path="sudo rsync"` to the rsync call in `dev/sync.sh`.

3. `curl: (22) 404` — the GitHub raw URL was wrong. The remote is
   `takshaktiwari/lite-panel` (not `server-setup-script`), branch `main`
   (not `lite-panel`). **Fix:** corrected `REPO_URL` and `REPO_BRANCH` in
   `install.sh` and the curl example in `PROJECT.md` / `README.md`.
   **Note:** GitHub SSH key is not configured in this dev environment, so
   `git push` returns `Permission denied (publickey)`. Push manually from a
   machine with GitHub access or via HTTPS token.

4. `This site can't be reached` in Chrome after successful install —
   The installer's banner showed `172.31.13.128` (private AWS IP from
   `hostname -I`). That address is unreachable from the public internet.
   **The correct public URL is `https://13.233.146.180`.**
   Diagnosis confirmed: UFW inactive, iptables fully open, nginx listening on
   `0.0.0.0:443`, AWS Security Group allows 80+443+22 from `0.0.0.0/0`,
   `curl` from the Mac returned `{"status":"ok"}` — server was working perfectly.
   **Root cause:** Chrome silently refuses to show the self-signed cert warning
   for raw IP addresses in some versions. Fix: click Advanced → Proceed, or use
   Safari which shows the warning reliably. The panel URL in the banner will be
   improved to use public IP in a future fix.

5. `502 Bad Gateway` after syncing with `dev/sync.sh` —
   `dev/sync.sh` runs `rsync --delete`. On the server, `install.sh` builds the
   production Python venv at `/opt/lite-panel/venv`. On the local dev machine,
   the virtualenv is named `.venv`. Because the rsync exclude list only omitted
   `.venv/` and not `venv/`, `--delete` wiped the server's virtualenv on every
   sync, killing the uvicorn process.
   **Fix:** Rebuilt the venv on the remote server (`python3 -m venv venv && pip install -r panel/requirements.txt`),
   and permanently added `--exclude='venv/'` to `dev/sync.sh`.

6. Blank job detail page & SSE stream hanging after service restart —
   When a job ran during a service restart, the in-process `log_buffer` was
   cleared from memory. The browser connected to `/jobs/<id>/stream`, but because
   the buffer was empty and no terminal event was emitted, the page remained
   blank and stuck in the `running` state.
   **Fix:**
   - Updated lifespan startup in `panel/app/main.py`: on application boot, any
     job left in `RUNNING` status from a previous killed process is marked `FAILED`
     with an explicit recovery explanation. This causes the SSE stream to emit a
     `done` event, prompting the browser to reload and display the static log.
   - Fixed `assets/units/lite-panel.service`: `StartLimitIntervalSec=60` and
     `StartLimitBurst=3` were relocated from `[Service]` to `[Unit]` where
     systemd expects them, eliminating systemd journal configuration warnings.

7. Ubuntu 26.04 ("Resolute Raccoon") `ondrej/php` PPA incompatibility —
   The test server is running an Ubuntu 26.04 LTS development build. The
   third-party `ondrej/php` PPA does not yet provide releases for the `resolute`
   codename. Trying to add the PPA broke subsequent `apt-get update` calls with
   HTTP 404s, and attempting to install PPA-only versions (e.g. PHP 8.2 or 7.4)
   failed with `apt-get exit 100` (`Unable to locate package php8.2-fpm`).
   **Fix:**
   - Added `apt.is_php_ppa_supported()` with `@lru_cache` to check if Launchpad
     carries a Release file for the current suite via `curl --head` before adding
     the PPA. If unsupported, it logs an explanatory note and cleanly falls back
     to official Ubuntu repositories.
   - Removed broken PPA repository entries from `/etc/apt/sources.list.d/`.
   - Updated `PhpProvider.available_versions()` so unsupported distributions only
     offer the versions actually present in the host's base OS repositories (preventing
     users from attempting impossible installations), while preserving full
     discovery in dev mode.
   - Installed PHP 8.5 natively from Ubuntu official repos, registered it in
     `InstalledProvider`, rendered `/etc/php/8.5/fpm/conf.d/99-lite-panel.ini`,
     and confirmed active FPM socket at `/run/php/php8.5-fpm.sock`.

**Final status:** Panel is live and accessible at `https://13.233.146.180`.
All services confirmed active and healthy:
- `lite-panel.service` (uvicorn FastAPI on 127.0.0.1:8765)
- `lite-panel-adminer.service` (PHP built-in server on 127.0.0.1:8766)
- `nginx.service` (reverse proxy on 443 + SSL)
- `php8.5-fpm.service` (FPM pool running on /run/php/php8.5-fpm.sock)
- Stack components tracked in DB: Nginx, MariaDB, Certbot, vsftpd, PHP 8.5.
- All 163 test suite unit/integration tests passing.

## The test server

- Details: `server-details.md` at repo root (gitignored).
- Key: `test-server-key.pem` at repo root (gitignored, `chmod 600` before use).
- **SSH as `ubuntu`, not `root`** — `server-details.md` says `root` but that's wrong.
  The `ubuntu` user has passwordless sudo.
  ```
  ssh -i test-server-key.pem ubuntu@13.233.146.180
  ```
- Ubuntu 26.04 LTS, x86_64, 448 MB RAM.
- `install.sh` automatically added 1 GB swap on first run (RAM ≤ 512 MB threshold).
- Panel is installed at `/opt/lite-panel`, DB at `/var/lib/lite-panel/panel.db`.

## Run it locally (macOS, no server needed)

```bash
cd /Users/takshaktiwari/Projects/server-setup-script
export LITE_PANEL_DEV_MODE=true LITE_PANEL_SECRET_KEY=dev PYTHONPATH=panel
export LITE_PANEL_DATABASE_URL="sqlite:///$PWD/dev.db"
echo "devpassword123" | .venv/bin/python -m app.cli create-admin --username admin
.venv/bin/python -m uvicorn app.main:app --port 8799
# open http://127.0.0.1:8799 — services show "not installed" (correct, no systemd on macOS)
```

Run tests: `.venv/bin/python -m pytest` (163 tests, all pass, no server needed).

## Deploy workflow

```bash
# Sync local changes to test server + restart services
bash dev/sync.sh

# Watch mode: re-sync on every file change (requires fswatch: brew install fswatch)
bash dev/sync.sh --watch

# Recovery: rebuild all config files from the DB (use when service is down)
ssh -i test-server-key.pem ubuntu@13.233.146.180 \
  'sudo /opt/lite-panel/venv/bin/python -m app.cli rebuild'

# Clean install from GitHub (once repo is pushed)
ssh -i test-server-key.pem ubuntu@13.233.146.180 \
  'curl -fsSL https://raw.githubusercontent.com/takshaktiwari/lite-panel/main/install.sh | sudo bash'
```

## Key files map

```
panel/app/
  validators.py        — every untrusted value passes through here first. Read this before touching any router.
  shell.py             — the ONLY place a subprocess is spawned. Argument lists only, never a string.
  cli.py               — management CLI: init-db, create-admin, reset-password, generate-password, rebuild
  jobs.py + tasks.py   — background job queue + every registered job handler (one per privileged action)
  models.py            — DB schema; note SiteDatabase/FtpAccount never store passwords
  services/
    apt.py             — apt/dpkg parsing, PHP repo setup (ondrej PPA / Sury)
    tuning.py          — RAM-based sizing math, ported from legacy/ scripts
    renderer.py        — atomic, idempotent config file writes
    sites.py           — the Site lifecycle (system user + FPM pool + vhost + SSL + FTP as one unit)
    databases.py, ftp.py, files.py — the rest of the service layer
  providers/           — one file per stack component (nginx, php, mariadb, vsftpd, certbot), same interface (base.py)
  routers/             — HTTP layer; thin, calls into services/tasks
    internal.py        — /internal/auth-check (the Adminer session gate endpoint)
assets/templates/      — Jinja2 templates for nginx vhosts, FPM pools, php.ini/my.cnf drop-ins
  panel-vhost.conf.j2  — the panel's own nginx vhost (SSL, SSE no-buffer, Adminer auth gate) — rendered by install.sh
assets/units/          — systemd unit files
  lite-panel.service          — uvicorn, root, 127.0.0.1:8765
  lite-panel-adminer.service  — php -S, www-data, 127.0.0.1:8766
assets/vendor/         — vendored Adminer (adminer.php v4.8.1 + index.php wrapper pinning to localhost socket only)
install.sh             — idempotent 10-step installer (root check → apt → swap → clone → venv → config → TLS → nginx → systemd → DB → healthz → 🎉)
uninstall.sh           — safe teardown; leaves /home tenant files unless --remove-sites is passed
dev/sync.sh            — rsync (sudo rsync on remote) + restart; reads IP/key from server-details.md; --watch mode
README.md              — public-facing docs: one-liner install, architecture, security, dev setup
legacy/                — original bash scripts, kept ONLY for reference
tests/                 — 163 tests, all pure-Python/HTTP-client, no server required
  test_cli.py          — tests for the CLI including the panel rebuild subcommand
```

## Things a fresh AI/tool should know before continuing

- **Auto Mode** — the user prefers the agent make reasonable calls and keep
  moving rather than stopping to ask, except for genuine architecture forks.
- Decisions already made and NOT open for re-litigation: Python/FastAPI backend,
  root daemon bound to 127.0.0.1 only (nginx reverse-proxies with its own
  session auth in front), Bash installer, Ubuntu/Debian only, GitHub raw
  distribution from `takshaktiwari/lite-panel` `main` branch, project name
  `lite-panel`.
- Git commit messages end with
  `Co-Authored-By: Claude Sonnet 4.6 <noreply@anthropic.com>` — keep that
  convention (swap the model name if a different assistant continues).
- The user explicitly does not want scope creep: email is out of v1, so is
  Apache, so is multi-user/RBAC.

## Phase 2 backlog (not started)

- Replace self-signed cert with Let's Encrypt renewal cron (Certbot provider
  already exists; wizard step already in the Stack UI — just wire the cron)
- Swap provisioning UI in the panel (currently handled automatically by
  install.sh for instances ≤ 512 MB RAM)
- Log rotation config for `/var/log/lite-panel/`
- Fix installer banner to detect and show the public IP instead of `hostname -I`
  (which returns the private AWS IP on EC2 instances)
- Push repo to GitHub (`git push` currently fails — no SSH key configured in
  the dev environment; use HTTPS token or push from a different machine)
