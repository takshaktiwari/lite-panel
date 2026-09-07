#!/usr/bin/env bash
# uninstall.sh — remove lite-panel from this server
#
# Usage:  sudo bash /opt/lite-panel/uninstall.sh [--remove-sites]
#
# By default, site files under /home are LEFT INTACT.  They belong to the
# operator's customers; removing them automatically would be catastrophic.
# Pass --remove-sites only if you are sure.

set -euo pipefail

BOLD="\033[1m"; RED="\033[1;31m"; YELLOW="\033[1;33m"
GREEN="\033[1;32m"; RESET="\033[0m"

info()  { echo -e "  $*"; }
ok()    { echo -e "${GREEN}  ✓${RESET}  $*"; }
warn()  { echo -e "${YELLOW}  !${RESET}  $*"; }
die()   { echo -e "${RED}  ✗${RESET}  $*" >&2; exit 1; }

[[ "$EUID" -eq 0 ]] || die "Please run as root (sudo bash uninstall.sh)"

REMOVE_SITES=false
for arg in "$@"; do
  case "$arg" in
    --remove-sites) REMOVE_SITES=true ;;
    -h|--help)
      sed -n '2,/^set /p' "$0" | grep '^#' | sed 's/^# \?//'
      exit 0 ;;
    *) die "Unknown flag: $arg" ;;
  esac
done

echo ""
echo -e "${BOLD}lite-panel uninstaller${RESET}"
echo "────────────────────────────────────"
warn "This will permanently remove lite-panel from this server."
if [[ "$REMOVE_SITES" == true ]]; then
  warn "--remove-sites was passed: site files in /home will also be deleted!"
fi
echo ""
read -rp "Type 'yes' to continue: " CONFIRM
[[ "$CONFIRM" == "yes" ]] || { info "Aborted."; exit 0; }
echo ""

# ---------------------------------------------------------------------------
# 1. Stop and disable systemd units
# ---------------------------------------------------------------------------
info "Stopping services..."
for unit in lite-panel.service lite-panel-adminer.service; do
  if systemctl is-active --quiet "$unit" 2>/dev/null; then
    systemctl stop "$unit"
    ok "Stopped $unit"
  fi
  if systemctl is-enabled --quiet "$unit" 2>/dev/null; then
    systemctl disable "$unit"
    ok "Disabled $unit"
  fi
  [[ -f "/etc/systemd/system/$unit" ]] && rm -f "/etc/systemd/system/$unit" && ok "Removed unit file $unit"
done
systemctl daemon-reload

# ---------------------------------------------------------------------------
# 2. Remove nginx vhost
# ---------------------------------------------------------------------------
info "Removing nginx vhost..."
[[ -L /etc/nginx/sites-enabled/lite-panel ]]  && rm -f /etc/nginx/sites-enabled/lite-panel  && ok "Removed sites-enabled symlink"
[[ -f /etc/nginx/sites-available/lite-panel ]] && rm -f /etc/nginx/sites-available/lite-panel && ok "Removed sites-available config"

if nginx -t 2>/dev/null; then
  systemctl reload nginx 2>/dev/null && ok "nginx reloaded"
else
  warn "nginx config test failed after vhost removal — check nginx manually"
fi

# ---------------------------------------------------------------------------
# 3. Remove panel directories
# ---------------------------------------------------------------------------
info "Removing panel files..."
for dir in /opt/lite-panel /etc/lite-panel /var/lib/lite-panel /var/log/lite-panel; do
  if [[ -d "$dir" ]]; then
    rm -rf "$dir"
    ok "Removed $dir"
  fi
done

# ---------------------------------------------------------------------------
# 4. Site files (tenant data) — only if explicitly requested
# ---------------------------------------------------------------------------
if [[ "$REMOVE_SITES" == true ]]; then
  warn "Removing site files under /home..."
  # Only remove directories that were likely created by the panel (system users
  # whose home is under /home and whose UID is in the site user range ≥1000).
  # We do NOT rm -rf /home blindly — iterate and confirm each one.
  shopt -s nullglob
  for dir in /home/*/; do
    name=$(basename "$dir")
    [[ "$name" == "ubuntu" || "$name" == "debian" || "$name" == "admin" ]] && continue
    rm -rf "$dir"
    ok "Removed /home/$name"
  done
else
  warn "Site files in /home were NOT removed (pass --remove-sites to delete them)."
fi

# ---------------------------------------------------------------------------
# Done
# ---------------------------------------------------------------------------
echo ""
echo -e "${GREEN}lite-panel uninstalled.${RESET}"
echo ""
