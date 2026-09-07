#!/bin/bash
set -e

# --------------------------------------------------
# ROOT CHECK
# --------------------------------------------------
if [ "$EUID" -ne 0 ]; then
  echo "❌ Please run this script as root (use sudo)"
  exit 1
fi

echo "🚀 New Domain Setup Script"
echo "--------------------------------------"

# -----------------------------
# ASK DOMAIN NAME
# -----------------------------
read -p "Enter domain name (e.g., example.com): " DOMAIN
DOMAIN_SANITIZED=$(echo "$DOMAIN" | tr '.' '_')
WEBROOT="/var/www/$DOMAIN_SANITIZED"

# -----------------------------
# CREATE WEBROOT FOLDER
# -----------------------------
mkdir -p "$WEBROOT"
chown -R www-data:www-data "$WEBROOT"
chmod -R 755 "$WEBROOT"

# -----------------------------
# ASK FOR CUSTOM SUB-FOLDER
# -----------------------------
read -p "Do you want to point Nginx to a sub-folder inside $WEBROOT? (e.g., public) Leave empty for root: " SUBFOLDER
if [[ -n "$SUBFOLDER" ]]; then
  ROOT_DIR="$WEBROOT/$SUBFOLDER"
  mkdir -p "$ROOT_DIR"
else
  ROOT_DIR="$WEBROOT"
fi
echo "🔹 Nginx will point to: $ROOT_DIR"

# -----------------------------
# DETECT PHP-FPM SOCKET
# -----------------------------
PHP_FPM_SOCK=""
for sock in /run/php/php*-fpm.sock; do
  if [[ -S "$sock" ]]; then
    PHP_FPM_SOCK="$sock"
    break
  fi
done

if [[ -n "$PHP_FPM_SOCK" ]]; then
  echo "✅ Detected PHP-FPM socket: $PHP_FPM_SOCK"
else
  echo "⚠️ No PHP-FPM socket detected. PHP sites may not run."
fi

# -----------------------------
# CREATE NGINX CONFIG
# -----------------------------
NGINX_CONF="/etc/nginx/sites-available/$DOMAIN"
cat > "$NGINX_CONF" <<EOF
# Redirect www to non-www
server {
    listen 80;
    listen [::]:80;
    server_name www.$DOMAIN;
    return 301 http://$DOMAIN\$request_uri;
}

server {
    listen 80;
    listen [::]:80;

    server_name $DOMAIN;

    root $ROOT_DIR;
    index index.php index.html;

    access_log /var/log/nginx/${DOMAIN}.access.log;
    error_log /var/log/nginx/${DOMAIN}.error.log;

    client_max_body_size 128M;

    location / {
        try_files \$uri \$uri/ /index.php?\$query_string;
    }
    
    location ~* \.(css|js|jpg|jpeg|png|gif|ico|svg|woff|woff2|ttf|eot|pdf|doc|docx|xls|xlsx|ppt|pptx|zip|rar|7z)$ {
        try_files \$uri =404;
        expires max;
        access_log off;
    }
    location ~ \.php\$ {
EOF

if [[ -n "$PHP_FPM_SOCK" ]]; then
cat >> "$NGINX_CONF" <<EOF
        include snippets/fastcgi-php.conf;
        fastcgi_pass unix:$PHP_FPM_SOCK;
EOF
fi

cat >> "$NGINX_CONF" <<EOF
    }

    # Deny access to hidden files
    location ~ /\. {
        deny all;
    }
}
EOF

# Enable site
ln -sf "$NGINX_CONF" /etc/nginx/sites-enabled/

# Test Nginx config
nginx -t

# -----------------------------
# INSTALL CERTBOT & SSL
# -----------------------------
if ! command -v certbot >/dev/null 2>&1; then
  echo "📦 Installing Certbot for Let's Encrypt..."
  apt update
  apt install -y certbot python3-certbot-nginx
fi

echo "🔐 Obtaining SSL for $DOMAIN and www.$DOMAIN..."
certbot --nginx -d "$DOMAIN" -d "www.$DOMAIN" --redirect --non-interactive --agree-tos -m admin@$DOMAIN || true

# Reload Nginx
systemctl reload nginx

# -----------------------------
# FINAL STATUS
# -----------------------------
echo ""
echo "🎉 Domain setup completed successfully!"
echo "--------------------------------------"
echo "Domain       : $DOMAIN"
echo "Webroot      : $ROOT_DIR"
echo "PHP-FPM Sock : ${PHP_FPM_SOCK:-Not detected}"
echo "Nginx Config : $NGINX_CONF"
echo "SSL          : Let's Encrypt enabled"
echo "Upload Limit : 128M"
echo "--------------------------------------"

# Export webroot for other scripts
echo "$ROOT_DIR" > /tmp/domain_webroot.txt
