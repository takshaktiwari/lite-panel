#!/bin/bash
set -e

# --------------------------------------------------
# ROOT CHECK
# --------------------------------------------------
if [ "$EUID" -ne 0 ]; then
  echo "❌ Please run this script as root (use sudo)"
  exit 1
fi

BASE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# --------------------------------------------------
# SYSTEM UPDATE
# --------------------------------------------------
echo "========================================"
echo " System Update"
echo "========================================"
apt update -y && apt upgrade -y

# --------------------------------------------------
# ESSENTIAL TOOLS
# --------------------------------------------------
echo ""
read -p "👉 Install essential tools (zip, unzip, curl, htop, git, wget)? (y/n): " INSTALL_TOOLS
if [[ "$INSTALL_TOOLS" =~ ^[Yy]$ ]]; then
  apt install -y zip unzip curl htop git software-properties-common ca-certificates lsb-release wget
  echo "✅ Essential tools installed"
else
  echo "⏭️ Skipped essential tools"
fi


# --------------------------------------------------
# DOWNLOAD MODULE SCRIPTS
# --------------------------------------------------
echo ""
echo "========================================"
echo " Downloading required module scripts"
echo "========================================"

# URLs of the scripts on your server / GitHub / CDN
MARIADB_URL="https://dev.takshaktiwari.com/server-setup-scripts/mariadb.sh"
SWAP_URL="https://dev.takshaktiwari.com/server-setup-scripts/swap.sh"
FTP_URL="https://dev.takshaktiwari.com/server-setup-scripts/ftp.sh"
DOMAIN_URL="https://dev.takshaktiwari.com/server-setup-scripts/domain.sh"

# Download each script
for SCRIPT_URL in "$MARIADB_URL" "$SWAP_URL" "$FTP_URL" "$DOMAIN_URL"; do
    FILE_NAME="$BASE_DIR/$(basename $SCRIPT_URL)"
    echo "📥 Downloading $(basename $SCRIPT_URL)..."
    wget -q -O "$FILE_NAME" "$SCRIPT_URL"
    chmod +x "$FILE_NAME"
done

echo "✅ All module scripts downloaded"


# --------------------------------------------------
# TOP-LEVEL SELECTOR
# --------------------------------------------------
echo "========================================"
echo " Server Setup Menu"
echo "========================================"
echo "1) Full server setup (Nginx + PHP + Composer + Node + Optional Modules)"
echo "2) Only setup MariaDB / create db"
echo "3) Only setup Swap"
echo "4) Only setup FTP / create FTP user"
echo "5) Only add extra domain (Nginx + SSL)"
echo "6) Exit"
echo "----------------------------------------"

read -p "Choose an option (1-6): " SELECT_OPTION

case "$SELECT_OPTION" in
  1)
    echo "🚀 Running full server setup..."
    FULL_SETUP=true
    ;;
  2)
    echo "🚀 Running only MariaDB setup..."
    source "$BASE_DIR/mariadb.sh"
    exit 0
    ;;
  3)
    echo "🚀 Running only Swap setup..."
    source "$BASE_DIR/swap.sh"
    exit 0
    ;;
  4)
    echo "🚀 Running only FTP setup..."
    source "$BASE_DIR/ftp.sh"
    exit 0
    ;;
  5)
    echo "🚀 Running only Extra Domain setup..."
    source "$BASE_DIR/domain.sh"
    exit 0
    ;;
  6)
    echo "👋 Exiting..."
    exit 0
    ;;
  *)
    echo "❌ Invalid option, exiting..."
    exit 1
    ;;
esac

# --------------------------------------------------
# FULL SERVER SETUP
# --------------------------------------------------
# The rest of your main script continues here...
# Nginx, PHP, Composer, Node.js, PHP-FPM optimizations,
# and optional modules prompts


# --------------------------------------------------
# PHP SETUP
# --------------------------------------------------
echo ""
echo "========================================"
echo " PHP Setup"
echo "========================================"

PHP_INSTALLED=false
PHP_VERSION=""
PHP_FPM_SOCK=""

read -p "👉 Do you want to install PHP? (y/n): " INSTALL_PHP

# Detect existing PHP-FPM safely (set -e safe)
EXISTING_PHP_VERSION=$(ls /run/php/php*-fpm.sock 2>/dev/null \
  | grep -oP 'php\K[0-9]+\.[0-9]+' \
  | head -n 1 || true)

if [[ -n "$EXISTING_PHP_VERSION" ]]; then
  PHP_VERSION="$EXISTING_PHP_VERSION"
  PHP_FPM_SOCK="/run/php/php${PHP_VERSION}-fpm.sock"
  PHP_INSTALLED=true

  echo "✅ PHP $PHP_VERSION already installed"
  echo "🔌 PHP-FPM socket: $PHP_FPM_SOCK"
  echo "ℹ️ Skipping PHP installation prompt"

else
  if [[ "$INSTALL_PHP" =~ ^[Yy]$ ]]; then
    read -p "👉 Enter PHP version (e.g. 8.1, 8.2, 8.3): " PHP_VERSION

    echo "📦 Installing PHP $PHP_VERSION..."
    add-apt-repository -y ppa:ondrej/php
    apt update -y

    apt install -y \
      php${PHP_VERSION} \
      php${PHP_VERSION}-fpm \
      php${PHP_VERSION}-cli \
      php${PHP_VERSION}-common \
      php${PHP_VERSION}-mysql \
      php${PHP_VERSION}-pgsql \
      php${PHP_VERSION}-sqlite3 \
      php${PHP_VERSION}-curl \
      php${PHP_VERSION}-mbstring \
      php${PHP_VERSION}-xml \
      php${PHP_VERSION}-bcmath \
      php${PHP_VERSION}-zip \
      php${PHP_VERSION}-gd \
      php${PHP_VERSION}-intl \
      php${PHP_VERSION}-soap \
      php${PHP_VERSION}-readline \
      php${PHP_VERSION}-opcache

    systemctl enable php${PHP_VERSION}-fpm
    systemctl start php${PHP_VERSION}-fpm

    PHP_FPM_SOCK="/run/php/php${PHP_VERSION}-fpm.sock"
    PHP_INSTALLED=true

    echo "✅ PHP $PHP_VERSION installed"
    echo "🔌 PHP-FPM socket: $PHP_FPM_SOCK"
  else
    echo "⏭️ PHP installation skipped"
  fi
fi

# --------------------------------------------------
# COMPOSER
# --------------------------------------------------
echo ""
read -p "👉 Install Composer (PHP dependency manager)? (y/n): " INSTALL_COMPOSER
if [[ "$INSTALL_COMPOSER" =~ ^[Yy]$ ]]; then
  if [[ "$PHP_INSTALLED" == true ]]; then
    if command -v composer >/dev/null 2>&1; then
      echo "✅ Composer already installed"
    else
      echo "📦 Installing Composer..."
      curl -sS https://getcomposer.org/installer | php
      mv composer.phar /usr/local/bin/composer
      chmod +x /usr/local/bin/composer
      echo "✅ Composer installed"
    fi
  else
    echo "⚠️ Composer requires PHP — skipping"
  fi
else
  echo "⏭️ Skipped Composer"
fi

# --------------------------------------------------
# NODE.JS
# --------------------------------------------------
echo ""
read -p "👉 Install Node.js (LTS)? (y/n): " INSTALL_NODE
if [[ "$INSTALL_NODE" =~ ^[Yy]$ ]]; then
  if command -v node >/dev/null 2>&1; then
    echo "✅ Node.js already installed"
  else
    echo "📦 Installing Node.js LTS..."
    curl -fsSL https://deb.nodesource.com/setup_lts.x | bash -
    apt install -y nodejs
    echo "✅ Node.js installed"
  fi
else
  echo "⏭️ Skipped Node.js"
fi

# --------------------------------------------------
# NGINX
# --------------------------------------------------
echo ""
echo "========================================"
echo " Nginx Setup"
echo "========================================"

read -p "👉 Do you want to install and setup Nginx? (y/n): " INSTALL_NGINX

if [[ "$INSTALL_NGINX" =~ ^[Yy]$ ]]; then

  if ! command -v nginx >/dev/null 2>&1; then
    echo "📦 Installing Nginx..."
    apt install -y nginx
    systemctl enable nginx
    systemctl start nginx
  else
    echo "✅ Nginx already installed"
  fi

  DEFAULT_ROOT="/var/www/html"
  mkdir -p "$DEFAULT_ROOT/public"
  chown -R www-data:www-data "$DEFAULT_ROOT"
  chmod -R 755 "$DEFAULT_ROOT"

  if [[ ! -f "$DEFAULT_ROOT/public/index.html" ]]; then
    cat <<EOF > "$DEFAULT_ROOT/public/index.html"
<!DOCTYPE html>
<html>
<head><title>Server Ready</title></head>
<body>
<h1>✅ Server is ready</h1>
<p>Nginx is running successfully.</p>
</body>
</html>
EOF
  fi

  DEFAULT_CONF="/etc/nginx/sites-available/default"

  cat <<EOF > "$DEFAULT_CONF"
server {
    listen 80 default_server;
    listen [::]:80 default_server;

    server_name _;

    root /var/www/html/public;
    index index.php index.html;

    access_log /var/log/nginx/default.access.log;
    error_log /var/log/nginx/default.error.log;

    location ~ /\.(?!well-known).* {
        deny all;
    }

    location / {
        try_files \$uri \$uri/ /index.php?\$query_string;
    }
EOF

  if [[ "$PHP_INSTALLED" == true && -S "$PHP_FPM_SOCK" ]]; then
    cat <<EOF >> "$DEFAULT_CONF"
    location ~ \.php$ {
        include snippets/fastcgi-php.conf;
        fastcgi_pass unix:$PHP_FPM_SOCK;
    }
EOF
  fi

  cat <<EOF >> "$DEFAULT_CONF"
    autoindex off;
    add_header X-Frame-Options "SAMEORIGIN";
    add_header X-Content-Type-Options "nosniff";
}
EOF

  nginx -t
  systemctl reload nginx

  echo "✅ Nginx ready (PHP: $PHP_INSTALLED)"
else
  echo "⏭️ Skipped Nginx"
fi

echo ""
echo "🎉 BASE SERVER SETUP COMPLETE"
echo ""

# --------------------------------------------------
# PHP-FPM PROCESS MANAGER OPTIMIZATION
# --------------------------------------------------
if [[ "$PHP_INSTALLED" == true ]]; then
  echo ""
  echo "========================================"
  echo " PHP-FPM Worker Optimization"
  echo "========================================"

  TOTAL_RAM_MB=$(free -m | awk '/Mem:/ {print $2}')
  FPM_POOL="/etc/php/${PHP_VERSION}/fpm/pool.d/www.conf"

  echo "🧠 Detected RAM: ${TOTAL_RAM_MB} MB"
  echo "📄 FPM pool file: $FPM_POOL"

  if [[ "$TOTAL_RAM_MB" -le 1024 ]]; then
    PM_MODE="ondemand"
    MAX_CHILDREN=6
  elif [[ "$TOTAL_RAM_MB" -le 2048 ]]; then
    PM_MODE="ondemand"
    MAX_CHILDREN=12
  elif [[ "$TOTAL_RAM_MB" -le 4096 ]]; then
    PM_MODE="ondemand"
    MAX_CHILDREN=24
  else
    PM_MODE="dynamic"
    MAX_CHILDREN=40
  fi

  echo "⚙️ Applying FPM settings:"
  echo "  - pm = $PM_MODE"
  echo "  - pm.max_children = $MAX_CHILDREN"

  sed -i "s/^pm = .*/pm = ${PM_MODE}/" "$FPM_POOL"
  sed -i "s/^pm.max_children = .*/pm.max_children = ${MAX_CHILDREN}/" "$FPM_POOL"

  if [[ "$PM_MODE" == "ondemand" ]]; then
    sed -i "s/^;*pm.process_idle_timeout = .*/pm.process_idle_timeout = 10s/" "$FPM_POOL"
    sed -i "s/^;*pm.max_requests = .*/pm.max_requests = 500/" "$FPM_POOL"
  else
    sed -i "s/^pm.start_servers = .*/pm.start_servers = 8/" "$FPM_POOL"
    sed -i "s/^pm.min_spare_servers = .*/pm.min_spare_servers = 4/" "$FPM_POOL"
    sed -i "s/^pm.max_spare_servers = .*/pm.max_spare_servers = 16/" "$FPM_POOL"
  fi

  echo "🔄 Restarting PHP-FPM..."
  systemctl restart php${PHP_VERSION}-fpm

  echo "✅ PHP-FPM workers optimized safely"
fi

# --------------------------------------------------
# PHP AUTO MEMORY OPTIMIZATION
# --------------------------------------------------
if [[ "$PHP_INSTALLED" == true ]]; then
  echo ""
  echo "========================================"
  echo " PHP Memory Optimization"
  echo "========================================"

  TOTAL_RAM_MB=$(free -m | awk '/Mem:/ {print $2}')
  PHP_INI="/etc/php/${PHP_VERSION}/fpm/php.ini"

  echo "🧠 Detected RAM: ${TOTAL_RAM_MB} MB"
  echo "📄 PHP ini: $PHP_INI"

  if [[ "$TOTAL_RAM_MB" -le 1024 ]]; then
    PHP_MEMORY="128M"
    OPCACHE_MEM="64"
  elif [[ "$TOTAL_RAM_MB" -le 2048 ]]; then
    PHP_MEMORY="256M"
    OPCACHE_MEM="128"
  else
    PHP_MEMORY="512M"
    OPCACHE_MEM="256"
  fi

  echo "⚙️ Applying PHP settings:"
  echo "  - memory_limit = $PHP_MEMORY"
  echo "  - opcache.memory_consumption = ${OPCACHE_MEM}MB"

  sed -i "s/^memory_limit = .*/memory_limit = ${PHP_MEMORY}/" "$PHP_INI"
  sed -i "s/^post_max_size = .*/post_max_size = 128M/" "$PHP_INI"
  sed -i "s/^upload_max_filesize = .*/upload_max_filesize = 128M/" "$PHP_INI"
  sed -i "s/^max_execution_time = .*/max_execution_time = 300/" "$PHP_INI"
  sed -i "s/^max_input_time = .*/max_input_time = 300/" "$PHP_INI"

  # Enable & tune OPcache
  sed -i "s/^;*opcache.enable=.*/opcache.enable=1/" "$PHP_INI"
  sed -i "s/^;*opcache.memory_consumption=.*/opcache.memory_consumption=${OPCACHE_MEM}/" "$PHP_INI"
  sed -i "s/^;*opcache.max_accelerated_files=.*/opcache.max_accelerated_files=20000/" "$PHP_INI"
  sed -i "s/^;*opcache.validate_timestamps=.*/opcache.validate_timestamps=1/" "$PHP_INI"

  echo "🔄 Restarting PHP-FPM..."
  systemctl restart php${PHP_VERSION}-fpm

  echo "✅ PHP optimized for ${TOTAL_RAM_MB}MB RAM"
fi

# --------------------------------------------------
# OPTIONAL MODULES
# --------------------------------------------------

# -----------------------------
# MariaDB
# -----------------------------
read -p "Do you want to setup MariaDB? (y/n): " RUN_DB
if [[ "$RUN_DB" =~ ^[Yy]$ ]]; then
    echo "🚀 Running MariaDB setup..."
    source "$BASE_DIR/mariadb.sh"
    echo "✅ MariaDB setup completed"
else
    echo "⏭️ Skipped MariaDB setup"
fi

# -----------------------------
# Swap
# -----------------------------
read -p "Do you want to setup Swap? (y/n): " RUN_SWAP
if [[ "$RUN_SWAP" =~ ^[Yy]$ ]]; then
    echo "🚀 Running Swap setup..."
    source "$BASE_DIR/swap.sh"
    echo "✅ Swap setup completed"
else
    echo "⏭️ Skipped Swap setup"
fi

# -----------------------------
# FTP
# -----------------------------
read -p "Do you want to setup FTP? (y/n): " RUN_FTP
if [[ "$RUN_FTP" =~ ^[Yy]$ ]]; then
    echo "🚀 Running FTP setup..."
    source "$BASE_DIR/ftp.sh"
    echo "✅ FTP setup completed"
else
    echo "⏭️ Skipped FTP setup"
fi