# lite-panel — handoff notes

Written 2026-09-07 because the session building this ran out of quota mid-task.
Read this before touching anything else. The full original design is at
`~/.claude/plans/streamed-dreaming-rocket.md` on the machine that created it —
if you don't have that file, this document plus the code is enough to continue.

## What this is

A minimal, self-hosted control panel for a bare Ubuntu/Debian VPS — a light
alternative to cPanel/Plesk/CyberPanel for someone who only needs: install a
web stack, add a domain + SSL, create a database (browsed via Adminer),
create an FTP account, browse files. Installed with one `curl | bash`, then
used entirely through a web UI afterward.

Backend: **Python (FastAPI + Uvicorn)**, on branch `lite-panel`, in this same
repo (`/Users/takshaktiwari/Projects/server-setup-script`). Not yet merged to
`main`.

## The one idea that governs every design choice here

**Nothing is decided once and frozen.** The first attempt at this plan got
rejected by the user for exactly that mistake — it had a fixed PHP version
and a hardcoded extension list, copied from the old bash scripts. The
correction: a panel's whole value is that choices stay changeable at
runtime. So:

- PHP versions and extensions are **discovered from apt at runtime**
  (`app/services/apt.py` + `app/providers/php.py`), never hardcoded. Several
  versions install side by side; each site picks one.
- The panel **writes only its own config files**, rendered from Jinja2
  templates + database state (`app/services/renderer.py`), and never edits a
  vendor config in place. Every render is idempotent, so
  `panel.rebuild` (a job kind) can reconstruct the whole server's config from
  the database at any time.
- RAM-based tuning (`app/services/tuning.py`) is **computed on demand**, not
  a one-shot `sed`, and every value has a per-key override stored in the DB.

If you're about to hardcode something a person would reasonably want to
change later, stop — that's the anti-pattern this project exists to avoid.
See `legacy/README.md` for the fuller version of this argument and exactly
which formulas/patterns from the old scripts are safe to reuse vs. not.

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
| 8 | Adminer + `auth_request` gate | 🟡 Adminer vendored + wrapper written; nginx gate NOT wired yet |
| 9 | `install.sh`, `uninstall.sh`, `panel rebuild` CLI | ❌ **Not started — this is the next thing to do** |

**154 tests passing** (`.venv/bin/python -m pytest`), all runnable on a
workstation, no server needed.

**The whole web app works end-to-end when run manually** (see "Run it
locally" below) — login, dashboard, every page renders. What does NOT exist
yet is any way to get it *onto* a server. There is no `install.sh`. That is
the very next task.

## Immediate next step

Build `install.sh` + supporting pieces per the plan (`~/.claude/plans/streamed-dreaming-rocket.md`,
section "Install flow"):

1. Root check, OS check (`/etc/os-release` must be ubuntu/debian).
2. `apt update`; install only what the panel itself needs: `python3-venv git curl nginx openssl`.
3. Clone/copy this repo to `/opt/lite-panel`; create a venv; `pip install -r panel/requirements.txt`.
4. `python -m app.cli init-db` then `create-admin` (see `panel/app/cli.py` — already built).
5. Self-signed TLS cert.
6. Render the **panel's own nginx vhost** — this does NOT exist yet as a
   template. It needs: `listen 443 ssl`, `proxy_pass http://127.0.0.1:8765`
   (the FastAPI app), and a `location /adminer/` block using
   `auth_request /internal/auth-check` (that endpoint already exists —
   `app/routers/internal.py`) before proxying to the Adminer PHP server on
   its own port. Nginx buffers SSE by default — the job log stream
   (`/jobs/{id}/stream`) needs `proxy_buffering off;` in this vhost or it
   will hang until the job finishes.
7. Write and enable **two systemd units** — neither exists yet:
   - `lite-panel.service` — `User=root`, `ExecStart=<venv>/bin/uvicorn app.main:app --host 127.0.0.1 --port 8765`, working directory `/opt/lite-panel/panel`.
   - `lite-panel-adminer.service` — runs `php -S 127.0.0.1:8766 -t /opt/lite-panel/assets/vendor` (the Adminer wrapper is `assets/vendor/index.php`, already written, forces the DB connection to the local socket only — see its docstring for why).
8. `systemctl daemon-reload && enable --now` both; `nginx -t && systemctl reload nginx`.
9. Health check against `/healthz` (already exists, no auth required).
10. Print the panel URL + admin credentials, mirroring the "🎉 complete" style
    of the old scripts in `legacy/`.

After that: `uninstall.sh`, and a `dev/sync.sh` rsync helper for the
EC2 dev loop (see plan). A standalone `panel rebuild` CLI subcommand would
also be worth adding to `app/cli.py` (the job kind `panel.rebuild` already
exists and works from inside the running app — see `app/tasks.py`).

## The test server (already provisioned, do not lose these details)

- Details in `server-details.md` at repo root (gitignored — not in git history, don't put secrets in tracked files).
- Key file: `test-server-key.pem` at repo root (also gitignored, `chmod 600` it before use).
- **The details file says `user: root` — that's wrong.** SSH in as `ubuntu`,
  which has passwordless sudo:
  `ssh -i test-server-key.pem ubuntu@<ip>`
- Facts already gathered from it: Ubuntu 26.04 LTS, x86_64, **only 448MB
  RAM** — smaller than a t3.micro, smaller than anything the plan
  anticipated. `tuning.py`'s floors were specifically tested against this
  number (see `test_tuning.py::test_mariadb_settings_tiny_instance_stays_above_floors`).
  **Add swap before running install** — MariaDB will likely fail to start
  or get OOM-killed otherwise (this is also what the old `mariadb.sh`
  warned about). Quickest path: SSH in and `fallocate`/`mkswap`/`swapon` a
  1GB swapfile by hand; swap provisioning is not yet a panel feature
  (it's phase-2 in the plan).

## Run it locally (macOS, no server needed)

```bash
cd /Users/takshaktiwari/Projects/server-setup-script
export LITE_PANEL_DEV_MODE=true LITE_PANEL_SECRET_KEY=dev PYTHONPATH=panel
export LITE_PANEL_DATABASE_URL="sqlite:///$PWD/dev.db"
echo "devpassword123" | .venv/bin/python -m app.cli create-admin --username admin
.venv/bin/python -m uvicorn app.main:app --port 8799
# open http://127.0.0.1:8799 — services will show "not installed" (correct, no systemd on macOS)
```

Run tests: `.venv/bin/python -m pytest` from repo root (venv already has all
deps installed, including `httpx`, `pymysql`, `pytest`).

## Key files map

```
panel/app/
  validators.py       — every untrusted value passes through here first. Read this before touching any router.
  shell.py             — the ONLY place a subprocess is spawned. Argument lists only, never a string.
  jobs.py + tasks.py   — background job queue + every registered job handler (one per privileged action)
  models.py            — DB schema; note SiteDatabase/FtpAccount never store passwords
  services/
    apt.py             — apt/dpkg parsing, PHP repo setup (ondrej PPA / Sury)
    tuning.py           — RAM-based sizing math, ported from legacy/ scripts
    renderer.py         — atomic, idempotent config file writes
    sites.py            — the Site lifecycle (system user + FPM pool + vhost + SSL + FTP as one unit)
    databases.py, ftp.py, files.py — the rest of the service layer
  providers/            — one file per stack component (nginx, php, mariadb, vsftpd, certbot), same interface (base.py)
  routers/               — HTTP layer; thin, calls into services/tasks
assets/templates/        — Jinja2 templates for nginx vhosts, FPM pools, php.ini/my.cnf drop-ins (NOT the panel's own web UI templates — those are panel/app/templates/)
assets/vendor/            — vendored Adminer (adminer.php pinned v4.8.1 + index.php wrapper pinning it to localhost-only)
legacy/                   — the original bash scripts, kept ONLY for reference; see legacy/README.md for what's still owed
tests/                    — 154 tests, all pure-Python/HTTP-client, no server required
```

## Things a fresh AI/tool should know before continuing

- **Auto Mode was active this whole session** — the user prefers the agent
  make reasonable calls and keep moving rather than stopping to ask, except
  for genuine architecture forks (stack choice, distribution method — both
  already decided, see below).
- Decisions already made and NOT open for re-litigation: Python/FastAPI
  backend, root daemon bound to 127.0.0.1 only (nginx reverse-proxies with
  its own session auth in front), Bash installer, Ubuntu/Debian only,
  distributed via GitHub raw from this same repo, project name `lite-panel`.
- Git commit messages in this repo end with
  `Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>` — keep that
  convention (swap the model name if a different assistant continues).
- The user explicitly does not want scope creep: email is out of v1, so is
  Apache, so is multi-user/RBAC. See the plan file's "Phase-2 backlog"
  section before adding anything not already listed above.
