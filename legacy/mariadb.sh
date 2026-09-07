#!/bin/bash
set -e

# --------------------------------------------------
# ROOT CHECK
# --------------------------------------------------
if [ "$EUID" -ne 0 ]; then
  echo "❌ Please run this script as root (use sudo)"
  exit 1
fi

echo "🚀 MariaDB Installation & FULL RAM-SAFE Optimization"
echo "---------------------------------------------------"

# -------------------------------
# USER INPUT
# -------------------------------
read -p "Enter Database Name: " DB_NAME
read -p "Enter Database Username: " DB_USER

while true; do
  read -s -p "Enter Database Password: " DB_PASS
  echo
  read -s -p "Confirm Database Password: " DB_PASS_CONFIRM
  echo
  [[ "$DB_PASS" == "$DB_PASS_CONFIRM" ]] && break
  echo "❌ Passwords do not match. Try again."
done

# -------------------------------
# RAM DETECTION
# -------------------------------
TOTAL_RAM_MB=$(awk '/MemTotal/ {print int($2/1024)}' /proc/meminfo)
echo "🧠 Detected RAM: ${TOTAL_RAM_MB} MB"

# -------------------------------
# SAFE MEMORY CALCULATION
# -------------------------------
# Use max 50% of RAM for MariaDB
DB_RAM_MB=$((TOTAL_RAM_MB / 2))
[ "$DB_RAM_MB" -lt 128 ] && DB_RAM_MB=128

BUFFER_POOL_MB=$((DB_RAM_MB * 60 / 100))
TMP_TABLE_MB=$((DB_RAM_MB * 5 / 100))
LOG_FILE_MB=$((DB_RAM_MB * 15 / 100))

[ "$BUFFER_POOL_MB" -lt 64 ] && BUFFER_POOL_MB=64
[ "$TMP_TABLE_MB" -lt 8 ] && TMP_TABLE_MB=8
[ "$LOG_FILE_MB" -lt 32 ] && LOG_FILE_MB=32

MAX_CONNECTIONS=20
[ "$TOTAL_RAM_MB" -ge 1024 ] && MAX_CONNECTIONS=50

echo "⚙️ MariaDB RAM Allocation"
echo "   ➜ Buffer Pool : ${BUFFER_POOL_MB}M"
echo "   ➜ Max Conn    : ${MAX_CONNECTIONS}"
echo "   ➜ Temp Table  : ${TMP_TABLE_MB}M"
echo "   ➜ Log File    : ${LOG_FILE_MB}M"

# -------------------------------
# CHECK MARIADB INSTALLATION
# -------------------------------
if command -v mariadb >/dev/null 2>&1 || command -v mysql >/dev/null 2>&1; then
  echo "✅ MariaDB already installed. Skipping installation and config."
  DB_INSTALLED=true
else
  DB_INSTALLED=false
fi

# -------------------------------
# INSTALL MARIADB (IF NOT INSTALLED)
# -------------------------------
if [ "$DB_INSTALLED" = false ]; then
  echo "📦 Installing MariaDB (low-RAM safe)..."
  sudo apt update -y
  sudo DEBIAN_FRONTEND=noninteractive apt install -y mariadb-server
fi

echo "🛑 Stopping MariaDB for safe configuration..."
sudo systemctl stop mariadb || true

# -------------------------------
# WRITE RAM-SAFE CONFIG (ONLY IF NEW INSTALL)
# -------------------------------
if [ "$DB_INSTALLED" = false ]; then
  echo "🛠 Writing RAM-safe MariaDB configuration..."
  # Backup existing file if exists
  if [ -f /etc/mysql/conf.d/99-ram-safe.cnf ]; then
      sudo cp /etc/mysql/conf.d/99-ram-safe.cnf /etc/mysql/conf.d/99-ram-safe.cnf.bak
  fi

  sudo tee /etc/mysql/conf.d/99-ram-safe.cnf > /dev/null <<EOF
[mysqld]
# -----------------------------
# CORE MEMORY SAFETY
# -----------------------------
innodb_buffer_pool_size = ${BUFFER_POOL_MB}M
innodb_buffer_pool_instances = 1
innodb_log_file_size = ${LOG_FILE_MB}M
innodb_log_buffer_size = 8M

# -----------------------------
# CONNECTION SAFETY
# -----------------------------
max_connections = ${MAX_CONNECTIONS}
thread_cache_size = 16
table_open_cache = 400
open_files_limit = 1024

# -----------------------------
# TEMP TABLES
# -----------------------------
tmp_table_size = ${TMP_TABLE_MB}M
max_heap_table_size = ${TMP_TABLE_MB}M

# -----------------------------
# DISK & FLUSH
# -----------------------------
innodb_flush_method = O_DIRECT
innodb_flush_log_at_trx_commit = 2

# -----------------------------
# DISABLE MEMORY EATERS
# -----------------------------
performance_schema = OFF
query_cache_type = 0
query_cache_size = 0

# -----------------------------
# LOGGING (LIGHT)
# -----------------------------
slow_query_log = 1
long_query_time = 2
slow_query_log_file = /var/log/mysql/slow.log

# -----------------------------
# TIMEOUTS
# -----------------------------
wait_timeout = 300
interactive_timeout = 300
EOF
else
  echo "ℹ️ Existing MariaDB installation detected. Skipping RAM-safe config write."
fi

# -------------------------------
# START MARIADB SAFELY
# -------------------------------
echo "▶️ Starting MariaDB..."
sudo systemctl daemon-reexec
sudo systemctl restart mariadb

sleep 5

if ! sudo systemctl is-active --quiet mariadb; then
  echo "❌ MariaDB failed to start."
  echo "👉 Strongly recommended: enable swap (1GB) before retrying."
  exit 1
fi

echo "✅ MariaDB is running."

# -------------------------------
# CREATE DB & USER (SAFE)
# -------------------------------
echo "👤 Creating database & user..."

sudo mariadb <<EOF
CREATE DATABASE IF NOT EXISTS \`${DB_NAME}\`
  CHARACTER SET utf8mb4
  COLLATE utf8mb4_unicode_ci;

CREATE USER IF NOT EXISTS '${DB_USER}'@'%' IDENTIFIED BY '${DB_PASS}';
GRANT ALL PRIVILEGES ON \`${DB_NAME}\`.* TO '${DB_USER}'@'%';
FLUSH PRIVILEGES;
EOF

# -------------------------------
# FINAL STATUS
# -------------------------------
echo "🎉 MariaDB setup completed successfully!"
echo "---------------------------------------"
echo "Database : $DB_NAME"
echo "User     : $DB_USER"
echo "Engine   : MariaDB"
echo "RAM Used : ~${DB_RAM_MB} MB"
echo "---------------------------------------"
