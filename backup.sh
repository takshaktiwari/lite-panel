#!/bin/bash

# =============================================================================
#  backup_all.sh — Website & Database Backup Script
#  Zips each website folder individually, bundles them, and exports all MySQL
#  databases (one .sql per DB) then bundles those too.
# =============================================================================

set -euo pipefail

# ── Colours ──────────────────────────────────────────────────────────────────
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'
CYAN='\033[0;36m'; BOLD='\033[1m'; RESET='\033[0m'

info()    { echo -e "${CYAN}[INFO]${RESET}  $*"; }
success() { echo -e "${GREEN}[OK]${RESET}    $*"; }
warn()    { echo -e "${YELLOW}[WARN]${RESET}  $*"; }
error()   { echo -e "${RED}[ERROR]${RESET} $*"; }
header()  { echo -e "\n${BOLD}${CYAN}=== $* ===${RESET}\n"; }

# ── Timestamp (used in output folder & zip names) ────────────────────────────
TIMESTAMP=$(date +"%Y-%m-%d_%H-%M-%S")

# =============================================================================
#  STEP 0 — Gather inputs
# =============================================================================
header "Backup Configuration"

# --- Base websites directory -------------------------------------------------
while true; do
    read -rp "$(echo -e "${BOLD}Enter the base directory where all websites are stored:${RESET} ")" SITES_DIR
    SITES_DIR="${SITES_DIR%/}"          # strip trailing slash
    if [[ -d "$SITES_DIR" ]]; then
        success "Sites directory: $SITES_DIR"
        break
    else
        error "Directory '$SITES_DIR' does not exist. Please try again."
    fi
done

# --- Output directory --------------------------------------------------------
DEFAULT_OUTPUT="$HOME/backups/$(date +"%Y-%m-%d")"
read -rp "$(echo -e "${BOLD}Enter output directory [default: $DEFAULT_OUTPUT]:${RESET} ")" OUTPUT_DIR
OUTPUT_DIR="${OUTPUT_DIR:-$DEFAULT_OUTPUT}"
OUTPUT_DIR="${OUTPUT_DIR%/}"

mkdir -p "$OUTPUT_DIR"
success "Output directory: $OUTPUT_DIR"

# --- Report file -------------------------------------------------------------
read -rp "$(echo -e "${BOLD}Report filename [default: index.html]:${RESET} ")" REPORT_FILE
REPORT_FILE="${REPORT_FILE:-index.html}"

read -rp "$(echo -e "${BOLD}Report directory [default: $OUTPUT_DIR]:${RESET} ")" REPORT_DIR
REPORT_DIR="${REPORT_DIR:-$OUTPUT_DIR}"
REPORT_DIR="${REPORT_DIR%/}"
mkdir -p "$REPORT_DIR"

REPORT_PATH="$REPORT_DIR/$REPORT_FILE"
success "Report file: $REPORT_PATH"

# ── Helper: append a <tr> row into the report table ─────────────────────────
report_entry() {
    local TYPE="$1" FPATH="$2"
    local SIZE; SIZE=$(du -sh "$FPATH" 2>/dev/null | cut -f1)
    cat >> "$REPORT_PATH" <<EOF
    <tr>
      <td>${TIMESTAMP//_/ }</td>
      <td>${TYPE}</td>
      <td>$(basename "$FPATH")</td>
      <td>${SIZE}</td>
      <td>${FPATH}</td>
    </tr>
EOF
}

# ── Bootstrap HTML if file does not exist, otherwise reuse it ────────────────
if [[ ! -f "$REPORT_PATH" ]]; then
    cat > "$REPORT_PATH" <<'HTMLEOF'
<!DOCTYPE html>
<html><head><meta charset="UTF-8"><title>Backup Report</title>
<style>
  body { font-family: Arial, sans-serif; padding: 20px; }
  h2   { margin-bottom: 12px; }
  table { border-collapse: collapse; width: 100%; }
  th, td { border: 1px solid #ccc; padding: 8px 12px; text-align: left; font-size: 14px; }
  th { background: #f0f0f0; }
  tr:nth-child(even) { background: #f9f9f9; }
</style></head><body>
<h2>Backup Report</h2>
<table>
  <thead><tr><th>Timestamp</th><th>Type</th><th>File</th><th>Size</th><th>Path</th></tr></thead>
  <tbody>

HTMLEOF
    success "Report created: $REPORT_PATH"
else
    info "Appending to existing report: $REPORT_PATH"
fi

# --- MySQL credentials -------------------------------------------------------
echo ""
info "MySQL credentials (leave password blank to be prompted per-command)"
read -rp "$(echo -e "${BOLD}MySQL username [default: root]:${RESET} ")" MYSQL_USER
MYSQL_USER="${MYSQL_USER:-root}"

read -rsp "$(echo -e "${BOLD}MySQL password (hidden):${RESET} ")" MYSQL_PASS
echo ""

# Build credential flags (avoid exposing password in process list)
if [[ -n "$MYSQL_PASS" ]]; then
    MYSQL_OPTS="-u${MYSQL_USER} -p${MYSQL_PASS}"
else
    MYSQL_OPTS="-u${MYSQL_USER}"
    warn "No password provided — mysqldump will prompt interactively per DB if required."
fi

# =============================================================================
#  STEP 1 — Website backups
# =============================================================================
header "Website Backups"

SITES_BACKUP_DIR="$OUTPUT_DIR/websites"
mkdir -p "$SITES_BACKUP_DIR"

# Collect top-level directories inside SITES_DIR
mapfile -t SITE_FOLDERS < <(find "$SITES_DIR" -mindepth 1 -maxdepth 1 -type d | sort)

if [[ ${#SITE_FOLDERS[@]} -eq 0 ]]; then
    warn "No sub-directories found in '$SITES_DIR'. Skipping website backup."
else
    SITE_ZIP_LIST=()

    for SITE_PATH in "${SITE_FOLDERS[@]}"; do
        SITE_NAME=$(basename "$SITE_PATH")
        ZIP_NAME="${SITE_NAME}_${TIMESTAMP}.zip"
        ZIP_PATH="$SITES_BACKUP_DIR/$ZIP_NAME"

        info "Zipping website: ${BOLD}$SITE_NAME${RESET} …"
        # -r recursive, -q quiet; zip relative to parent so paths inside are clean
        if zip -rq -6 "$ZIP_PATH" "$SITE_PATH"; then
            success "Created: $ZIP_PATH"
            SITE_ZIP_LIST+=("$ZIP_PATH")
        else
            error "Failed to zip '$SITE_NAME' — skipping."
        fi
    done

    # --- Bundle all site zips into one archive -------------------------------
    if [[ ${#SITE_ZIP_LIST[@]} -gt 0 ]]; then
        BUNDLE_ZIP="$OUTPUT_DIR/ALL_WEBSITES_${TIMESTAMP}.zip"
        info "Bundling all website zips → ${BOLD}ALL_WEBSITES_${TIMESTAMP}.zip${RESET} …"
        zip -jq -6 "$BUNDLE_ZIP" "${SITE_ZIP_LIST[@]}"
        success "Bundle created: $BUNDLE_ZIP"
        report_entry "WEBSITE" "$BUNDLE_ZIP"

        # --- Cleanup individual site zips ------------------------------------
        info "Cleaning up individual website zips …"
        for Z in "${SITE_ZIP_LIST[@]}"; do
            rm -f "$Z" && success "Removed: $(basename "$Z")"
        done
        # Remove the now-empty websites/ subfolder if nothing else is in it
        rmdir --ignore-fail-on-non-empty "$SITES_BACKUP_DIR"
    fi
fi

# =============================================================================
#  STEP 2 — MySQL database exports
# =============================================================================
header "MySQL Database Exports"

DB_BACKUP_DIR="$OUTPUT_DIR/databases"
mkdir -p "$DB_BACKUP_DIR"

# Check that mysql / mysqldump are available
if ! command -v mysql &>/dev/null || ! command -v mysqldump &>/dev/null; then
    error "mysql or mysqldump not found in PATH. Skipping database export."
else
    # Fetch list of databases (excluding system schemas)
    info "Fetching database list …"
    DB_LIST=$(mysql $MYSQL_OPTS \
        --batch --skip-column-names \
        -e "SHOW DATABASES;" 2>/dev/null \
        | grep -Ev "^(information_schema|performance_schema|mysql|sys)$" || true)

    if [[ -z "$DB_LIST" ]]; then
        warn "No user databases found (or could not connect). Skipping database export."
    else
        DB_SQL_LIST=()

        while IFS= read -r DB_NAME; do
            [[ -z "$DB_NAME" ]] && continue
            SQL_FILE="$DB_BACKUP_DIR/${DB_NAME}_${TIMESTAMP}.sql"

            info "Exporting database: ${BOLD}$DB_NAME${RESET} …"
            if mysqldump $MYSQL_OPTS \
                    --single-transaction \
                    --routines \
                    --triggers \
                    --events \
                    "$DB_NAME" > "$SQL_FILE" 2>/dev/null; then
                success "Exported: $SQL_FILE"
                DB_SQL_LIST+=("$SQL_FILE")
            else
                error "Failed to export '$DB_NAME' — skipping."
                rm -f "$SQL_FILE"
            fi
        done <<< "$DB_LIST"

        # --- Bundle all SQL files into one archive ---------------------------
        if [[ ${#DB_SQL_LIST[@]} -gt 0 ]]; then
            DB_BUNDLE_ZIP="$OUTPUT_DIR/ALL_DATABASES_${TIMESTAMP}.zip"
            info "Bundling all SQL exports → ${BOLD}ALL_DATABASES_${TIMESTAMP}.zip${RESET} …"
            zip -jq -6 "$DB_BUNDLE_ZIP" "${DB_SQL_LIST[@]}"
            success "Bundle created: $DB_BUNDLE_ZIP"
            report_entry "DATABASE" "$DB_BUNDLE_ZIP"

            # --- Cleanup individual SQL files --------------------------------
            info "Cleaning up individual SQL files …"
            for S in "${DB_SQL_LIST[@]}"; do
                rm -f "$S" && success "Removed: $(basename "$S")"
            done
            # Remove the now-empty databases/ subfolder if nothing else is in it
            rmdir --ignore-fail-on-non-empty "$DB_BACKUP_DIR"
        fi
    fi
fi

# =============================================================================
#  STEP 3 — Summary
# =============================================================================
header "Backup Complete — Summary"

echo -e "${BOLD}Output directory:${RESET} $OUTPUT_DIR"
echo ""
echo -e "${BOLD}Contents:${RESET}"
find "$OUTPUT_DIR" -maxdepth 2 \( -name "*.zip" -o -name "*.sql" \) \
    | sort \
    | while read -r F; do
        SIZE=$(du -sh "$F" 2>/dev/null | cut -f1)
        printf "  %-55s %s\n" "$(basename "$F")" "${SIZE}"
    done

echo ""
success "All done! Backups saved to: ${BOLD}$OUTPUT_DIR${RESET}"

success "Report saved: $REPORT_PATH"