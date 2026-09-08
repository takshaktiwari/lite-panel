#!/usr/bin/env bash
# install.sh — lite-panel installer
#
# Usage:
#   curl -fsSL https://raw.githubusercontent.com/takshaktiwari/server-setup-script/lite-panel/install.sh | sudo bash
#   # or, from a local clone:
#   sudo bash install.sh [--force-reinstall]
#
# Flags:
#   --force-reinstall   overwrite existing venv / cert / config (destructive)
#   --no-swap           skip automatic swap provisioning even on tiny instances
#
# Requirements: Ubuntu 20.04+ or Debian 11+, run as root.
#
# The install is idempotent by default: every step checks whether it has
# already been completed and skips itself if so.  Re-running is safe.

set -euo pipefail

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
BOLD="\033[1m"; GREEN="\033[1;32m"; YELLOW="\033[1;33m"
RED="\033[1;31m"; CYAN="\033[1;36m"; RESET="\033[0m"

info()    { echo -e "${CYAN}[info]${RESET}  $*"; }
ok()      { echo -e "${GREEN}[ok]${RESET}    $*"; }
warn()    { echo -e "${YELLOW}[warn]${RESET}   $*"; }
die()     { echo -e "${RED}[error]${RESET}  $*" >&2; exit 1; }
step()    { echo -e "\n${BOLD}=== $* ===${RESET}"; }

# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------
FORCE=false
NO_SWAP=false
for arg in "$@"; do
  case "$arg" in
    --force-reinstall) FORCE=true ;;
    --no-swap)         NO_SWAP=true ;;
    -h|--help)
      sed -n '2,/^set /p' "$0" | grep '^#' | sed 's/^# \?//'
      exit 0 ;;
    *) die "Unknown flag: $arg" ;;
  esac
done

# ---------------------------------------------------------------------------
# Step 1 — Root + OS check
# ---------------------------------------------------------------------------
step "Pre-flight checks"

[[ "$EUID" -eq 0 ]] || die "Please run as root (sudo bash install.sh)"

if [[ -f /etc/os-release ]]; then
  # shellcheck source=/dev/null
  source /etc/os-release
  case "${ID:-}" in
    ubuntu|debian) ok "OS: ${PRETTY_NAME}" ;;
    *) die "Unsupported OS '${ID:-unknown}'. Only Ubuntu and Debian are supported." ;;
  esac
else
  die "/etc/os-release not found — cannot determine OS."
fi

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
INSTALL_DIR=/opt/lite-panel
VENV_DIR=$INSTALL_DIR/venv
CONFIG_DIR=/etc/lite-panel
STATE_DIR=/var/lib/lite-panel
LOG_DIR=/var/log/lite-panel
TLS_DIR=$CONFIG_DIR/tls
ENV_FILE=$CONFIG_DIR/panel.env
PANEL_PORT=8765
ADMINER_PORT=8766
NGINX_CONF=/etc/nginx/sites-available/lite-panel
NGINX_ENABLED=/etc/nginx/sites-enabled/lite-panel
UNIT_DIR=/etc/systemd/system

# Where is the source tree?  If this script is running from the repo it's
# next to install.sh; if piped through curl, we clone from GitHub instead.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]:-/dev/stdin}")" 2>/dev/null && pwd || echo "")"
if [[ -f "$SCRIPT_DIR/panel/app/main.py" ]]; then
  SRC_DIR="$SCRIPT_DIR"
else
  SRC_DIR=""  # will clone
fi

REPO_URL="https://github.com/takshaktiwari/lite-panel"
REPO_BRANCH="main"

# ---------------------------------------------------------------------------
# Step 2 — System dependencies
# ---------------------------------------------------------------------------
step "Installing system dependencies"

export DEBIAN_FRONTEND=noninteractive
apt-get update -qq

# php-cli needed for the Adminer php -S server.  Other PHP stack components
# are installed later through the panel's own setup wizard, not here.
PKGS=(python3-venv python3-pip git curl nginx openssl php-cli ca-certificates)

MISSING=()
for pkg in "${PKGS[@]}"; do
  dpkg -l "$pkg" &>/dev/null || MISSING+=("$pkg")
done

if [[ ${#MISSING[@]} -gt 0 ]]; then
  info "Installing: ${MISSING[*]}"
  apt-get install -y -qq "${MISSING[@]}"
  ok "Dependencies installed"
else
  ok "All dependencies already present"
fi

# ---------------------------------------------------------------------------
# Swap guard (for tiny instances like the 448 MB test server)
# ---------------------------------------------------------------------------
step "Checking swap"
TOTAL_RAM_MB=$(awk '/MemTotal/ {print int($2/1024)}' /proc/meminfo)
SWAP_MB=$(awk '/SwapTotal/ {print int($2/1024)}' /proc/meminfo)

if [[ "$NO_SWAP" == false && "$TOTAL_RAM_MB" -le 512 && "$SWAP_MB" -lt 512 ]]; then
  warn "Only ${TOTAL_RAM_MB} MB RAM detected and no swap — adding 1 GB swapfile."
  warn "Skip this with --no-swap if you manage swap yourself."
  if [[ -f /swapfile ]]; then
    ok "Swapfile already exists at /swapfile — skipping creation"
  else
    fallocate -l 1G /swapfile
    chmod 600 /swapfile
    mkswap /swapfile
    swapon /swapfile
    # Make it survive reboots
    grep -q '/swapfile' /etc/fstab || echo '/swapfile none swap sw 0 0' >> /etc/fstab
    ok "1 GB swapfile created and activated"
  fi
else
  ok "Swap OK (RAM: ${TOTAL_RAM_MB} MB, swap: ${SWAP_MB} MB)"
fi

# ---------------------------------------------------------------------------
# Step 3 — Source tree
# ---------------------------------------------------------------------------
step "Installing lite-panel source"

if [[ "$FORCE" == true && -d "$INSTALL_DIR" ]]; then
  warn "--force-reinstall: removing existing $INSTALL_DIR"
  rm -rf "$INSTALL_DIR"
fi

if [[ -d "$INSTALL_DIR/panel" ]]; then
  ok "Source already at $INSTALL_DIR — skipping clone"
else
  if [[ -n "$SRC_DIR" ]]; then
    info "Copying from local source: $SRC_DIR"
    rsync -a --exclude='.git' --exclude='.venv' --exclude='__pycache__' \
          --exclude='*.pyc' --exclude='dev.db' \
          "$SRC_DIR/" "$INSTALL_DIR/"
    ok "Source copied"
  else
    info "Cloning $REPO_URL (branch: $REPO_BRANCH)"
    git clone --depth=1 --branch "$REPO_BRANCH" "$REPO_URL" "$INSTALL_DIR"
    ok "Cloned to $INSTALL_DIR"
  fi
fi

# ---------------------------------------------------------------------------
# Step 4 — Python venv + dependencies
# ---------------------------------------------------------------------------
step "Setting up Python venv"

if [[ "$FORCE" == true && -d "$VENV_DIR" ]]; then
  rm -rf "$VENV_DIR"
fi

if [[ -d "$VENV_DIR" ]]; then
  ok "venv already exists at $VENV_DIR"
else
  python3 -m venv "$VENV_DIR"
  ok "venv created"
fi

info "Installing Python dependencies"
"$VENV_DIR/bin/pip" install --quiet --upgrade pip
"$VENV_DIR/bin/pip" install --quiet -r "$INSTALL_DIR/panel/requirements.txt"
ok "Python dependencies installed"

# ---------------------------------------------------------------------------
# Step 5 — Config directory + panel.env
# ---------------------------------------------------------------------------
step "Writing configuration"

mkdir -p "$CONFIG_DIR" "$STATE_DIR" "$LOG_DIR" "$STATE_DIR/acme"

if [[ "$FORCE" == true && -f "$ENV_FILE" ]]; then
  rm -f "$ENV_FILE"
fi

if [[ -f "$ENV_FILE" ]]; then
  ok "panel.env already exists — skipping (use --force-reinstall to regenerate)"
else
  SECRET_KEY=$(openssl rand -hex 32)
  cat > "$ENV_FILE" <<EOF
# Generated by install.sh on $(date -u +"%Y-%m-%dT%H:%M:%SZ")
# Treat this file like a password — it signs all panel sessions.
LITE_PANEL_SECRET_KEY=${SECRET_KEY}
LITE_PANEL_INSTALL_DIR=${INSTALL_DIR}
LITE_PANEL_STATE_DIR=${STATE_DIR}
LITE_PANEL_CONFIG_DIR=${CONFIG_DIR}
LITE_PANEL_LOG_DIR=${LOG_DIR}
LITE_PANEL_SITES_ROOT=/var/www
EOF
  chmod 600 "$ENV_FILE"
  ok "panel.env written"
fi

# ---------------------------------------------------------------------------
# Step 6 — Self-signed TLS certificate
# ---------------------------------------------------------------------------
step "TLS certificate"

mkdir -p "$TLS_DIR"
CERT_FILE="$TLS_DIR/panel.crt"
KEY_FILE="$TLS_DIR/panel.key"

if [[ "$FORCE" == true ]]; then
  rm -f "$CERT_FILE" "$KEY_FILE"
fi

if [[ -f "$CERT_FILE" && -f "$KEY_FILE" ]]; then
  ok "Certificate already exists at $TLS_DIR"
else
  PANEL_FQDN=$(hostname -f 2>/dev/null || hostname)
  openssl req -x509 -nodes -days 3650 \
    -newkey rsa:2048 \
    -keyout "$KEY_FILE" \
    -out    "$CERT_FILE" \
    -subj   "/CN=${PANEL_FQDN}/O=lite-panel/C=XX" \
    -addext "subjectAltName=IP:$(hostname -I | awk '{print $1}'),DNS:${PANEL_FQDN}" \
    2>/dev/null
  chmod 600 "$KEY_FILE"
  ok "Self-signed certificate generated (valid 10 years)"
fi

# ---------------------------------------------------------------------------
# Step 7 — Panel nginx vhost
# ---------------------------------------------------------------------------
step "Writing nginx vhost"

PANEL_FQDN=$(hostname -f 2>/dev/null || hostname)
VHOST_TEMPLATE="$INSTALL_DIR/assets/templates/panel-vhost.conf.j2"

if [[ "$FORCE" == true ]]; then
  rm -f "$NGINX_CONF"
fi

if [[ -f "$NGINX_CONF" ]]; then
  ok "nginx vhost already exists at $NGINX_CONF"
else
  # Render the Jinja2 template with a minimal Python one-liner.
  # We can't use the panel's own renderer (it's not running yet), so we call
  # Python directly. The template only uses three variables so this is safe.
  "$VENV_DIR/bin/python3" - <<PYEOF
import re, sys
with open("$VHOST_TEMPLATE") as f:
    tpl = f.read()

ctx = {
    "panel_fqdn": "$PANEL_FQDN",
    "cert_path":  "$CERT_FILE",
    "key_path":   "$KEY_FILE",
}
# Replace {{ var }} style placeholders (no full Jinja2 needed here)
result = re.sub(r'\{\{\s*(\w+)\s*\}\}', lambda m: ctx.get(m.group(1), m.group(0)), tpl)
with open("$NGINX_CONF", "w") as f:
    f.write(result)
PYEOF

  ln -sf "$NGINX_CONF" "$NGINX_ENABLED"
  ok "nginx vhost written and enabled"
fi

# Disable the default nginx vhost if it's still enabled (it conflicts on port 80).
if [[ -L /etc/nginx/sites-enabled/default ]]; then
  rm /etc/nginx/sites-enabled/default
  info "Removed default nginx site"
fi

# ---------------------------------------------------------------------------
# Step 8 — Systemd units
# ---------------------------------------------------------------------------
step "Installing systemd units"

for unit in lite-panel.service lite-panel-adminer.service; do
  SRC="$INSTALL_DIR/assets/units/$unit"
  DST="$UNIT_DIR/$unit"
  if [[ "$FORCE" == true ]]; then
    rm -f "$DST"
  fi
  if [[ -f "$DST" ]]; then
    ok "$unit already installed"
  else
    cp "$SRC" "$DST"
    ok "Installed $unit"
  fi
done

systemctl daemon-reload
systemctl enable --now lite-panel.service lite-panel-adminer.service
ok "Both units enabled and started"

# ---------------------------------------------------------------------------
# Step 9 — nginx test + reload
# ---------------------------------------------------------------------------
step "Reloading nginx"

nginx -t
systemctl reload nginx
ok "nginx reloaded"

# ---------------------------------------------------------------------------
# Step 10 — Init DB + create admin
# ---------------------------------------------------------------------------
step "Initialising database"

# Source the env so the CLI picks up LITE_PANEL_* variables.
set -a; source "$ENV_FILE"; set +a
export PYTHONPATH="$INSTALL_DIR/panel"

cd "$INSTALL_DIR/panel"

"$VENV_DIR/bin/python3" -m app.cli init-db
ok "Database initialised"

ADMIN_PASSWORD=$(openssl rand -base64 20 | tr -d '=+/')

# create-admin is idempotent only if the user doesn't exist yet; if it fails
# because admin already exists we just skip — the password stays whatever it
# was last set to.
if echo "$ADMIN_PASSWORD" | "$VENV_DIR/bin/python3" -m app.cli create-admin --username admin 2>/dev/null; then
  ok "Admin account created"
else
  warn "Admin account already exists — password NOT changed (use 'lite-panel reset-password' to change it)"
  ADMIN_PASSWORD="<unchanged — check your records>"
fi

# ---------------------------------------------------------------------------
# Step 11 — Health check
# ---------------------------------------------------------------------------
step "Health check"

MAX_WAIT=30
INTERVAL=2
ELAPSED=0
info "Waiting for panel to respond on port $PANEL_PORT ..."
while true; do
  if curl -sfk "https://127.0.0.1:${PANEL_PORT}/healthz" >/dev/null 2>&1; then
    ok "Panel is up"
    break
  fi
  sleep "$INTERVAL"
  ELAPSED=$((ELAPSED + INTERVAL))
  if [[ "$ELAPSED" -ge "$MAX_WAIT" ]]; then
    warn "Panel did not respond within ${MAX_WAIT}s — check 'journalctl -u lite-panel'"
    break
  fi
done

# ---------------------------------------------------------------------------
# Done
# ---------------------------------------------------------------------------
SERVER_IP=$(hostname -I | awk '{print $1}')

echo ""
echo -e "${GREEN}╔══════════════════════════════════════════════════╗${RESET}"
echo -e "${GREEN}║        🎉  lite-panel installed!                 ║${RESET}"
echo -e "${GREEN}╠══════════════════════════════════════════════════╣${RESET}"
echo -e "${GREEN}║${RESET}  Panel URL  : ${BOLD}https://${SERVER_IP}:8443/${RESET}"
echo -e "${GREEN}║${RESET}  Username   : ${BOLD}admin${RESET}"
echo -e "${GREEN}║${RESET}  Password   : ${BOLD}${ADMIN_PASSWORD}${RESET}"
echo -e "${GREEN}║${RESET}"
echo -e "${GREEN}║${RESET}  ${YELLOW}⚠  Self-signed certificate — browser will warn.${RESET}"
echo -e "${GREEN}║${RESET}  ${YELLOW}   Install Certbot later via the panel's Stack tab.${RESET}"
echo -e "${GREEN}║${RESET}"
echo -e "${GREEN}║${RESET}  Logs       : journalctl -u lite-panel -f"
echo -e "${GREEN}║${RESET}  Config     : $ENV_FILE"
echo -e "${GREEN}║${RESET}  Uninstall  : sudo bash $INSTALL_DIR/uninstall.sh"
echo -e "${GREEN}╚══════════════════════════════════════════════════╝${RESET}"
echo ""
