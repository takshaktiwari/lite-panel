#!/bin/bash
set -e

echo "🚀 Email Setup & Management Script"
echo "----------------------------------"

install_ssl_only() {
    echo "🔐 Installing SSL certificate only..."
    
    if command -v certbot >/dev/null 2>&1; then
        # Backup existing configs
        cp /etc/dovecot/dovecot.conf /etc/dovecot/dovecot.conf.backup
        
        # Detect PHP version
        PHP_VERSION=$(ls /run/php/ | grep php | head -1 | sed 's/php//g' | sed 's/-fpm.sock//g')
        if [[ -z "$PHP_VERSION" ]]; then
            PHP_VERSION="8.1"  # fallback
        fi
        
        # Check if Nginx config exists for SSL installation
        if [[ -f "/etc/nginx/sites-available/$MAIL_HOSTNAME" ]]; then
            echo "🔧 Found existing Nginx config for $MAIL_HOSTNAME"
            certbot --nginx -d "$MAIL_HOSTNAME" --redirect --non-interactive --agree-tos -m admin@$CURRENT_DOMAIN || {
                echo "❌ SSL certificate installation failed!"
                echo "🔄 Restoring backup..."
                mv /etc/dovecot/dovecot.conf.backup /etc/dovecot/dovecot.conf
                return 1
            }
        else
            echo "❌ Nginx config not found for $MAIL_HOSTNAME. Cannot install SSL."
            echo "Please run full mail server installation first."
            return 1
        fi
        
        postconf -e "smtpd_tls_cert_file = /etc/letsencrypt/live/$MAIL_HOSTNAME/fullchain.pem"
        postconf -e "smtpd_tls_key_file = /etc/letsencrypt/live/$MAIL_HOSTNAME/privkey.pem"
        postconf -e "smtpd_use_tls = yes"
        
        # Update Dovecot SSL config safely - append to config instead of replacing
        if ! grep -q "ssl_cert = </etc/letsencrypt/live/$MAIL_HOSTNAME/fullchain.pem>" /etc/dovecot/dovecot.conf; then
            echo "" >> /etc/dovecot/dovecot.conf
            echo "# SSL settings" >> /etc/dovecot/dovecot.conf
            echo "ssl = required" >> /etc/dovecot/dovecot.conf
            echo "ssl_cert = </etc/letsencrypt/live/$MAIL_HOSTNAME/fullchain.pem>" >> /etc/dovecot/dovecot.conf
            echo "ssl_key = </etc/letsencrypt/live/$MAIL_HOSTNAME/privkey.pem>" >> /etc/dovecot/dovecot.conf
        fi
        
        systemctl restart postfix dovecot
        echo "✅ SSL installation completed!"
    else
        echo "❌ Certbot not found. Please install SSL manually."
    fi
}

install_webmail_only() {
    echo "🌐 Installing webmail only..."
    DOMAIN=$(hostname -d)
    HOSTNAME="mail.$DOMAIN"
    
    # Check if Nginx config already exists
    if [[ -f "/etc/nginx/sites-available/$HOSTNAME" ]]; then
        echo "ℹ️ Nginx config already exists for $HOSTNAME"
    else
        echo "ℹ️ Using existing PHP and Nginx (skipping package installation)"
        
        WEBMAIL_DIR="/var/www/webmail"
        
        # Clean up existing webmail directory if reinstalling
        if [[ -d "$WEBMAIL_DIR" && -f "$WEBMAIL_DIR/index.php" ]]; then
            echo "🧹 Cleaning up existing webmail installation..."
            rm -rf "$WEBMAIL_DIR"/*
        fi
        
        mkdir -p "$WEBMAIL_DIR"
        chown -R www-data:www-data "$WEBMAIL_DIR"

        cd /tmp
        wget -q https://github.com/roundcube/roundcubemail/releases/download/1.6.9/roundcubemail-1.6.9-complete.tar.gz
        tar -xzf roundcubemail-1.6.9-complete.tar.gz
        mv roundcubemail-1.6.9/* "$WEBMAIL_DIR/"
        rm -rf roundcubemail-1.6.9*

        chown -R www-data:www-data "$WEBMAIL_DIR"
        cd "$WEBMAIL_DIR"
        
        # Check if composer exists, if not install minimal requirements
        if ! command -v composer >/dev/null 2>&1; then
            echo "📦 Installing composer..."
            curl -sS https://getcomposer.org/installer | php
            mv composer.phar /usr/local/bin/composer
            chmod +x /usr/local/bin/composer
        fi
        
        # Run composer as non-root for safety
        if command -v sudo >/dev/null 2>&1 && id -u www-data >/dev/null 2>&1; then
            echo "🎵 Running composer as www-data user..."
            sudo -u www-data composer install --no-dev --optimize-autoloader
        else
            echo "⚠️ Running composer as root (not recommended)"
            composer install --no-dev --optimize-autoloader --ignore-platform-reqs
        fi

        cp config/config.inc.php.sample config/config.inc.php
        SECRET_KEY=$(openssl rand -base64 24)
        # Use different delimiter to avoid conflicts with special characters
        sed -i "s|\$config\['des_key'\] = .*|\$config['des_key'] = '$SECRET_KEY';|" config/config.inc.php
        sed -i "s|\$config\['default_host'\] = .*|\$config['default_host'] = 'localhost';|" config/config.inc.php
        
        cat > /etc/nginx/sites-available/$MAIL_HOSTNAME <<EOF
server {
    listen 80;
    server_name $MAIL_HOSTNAME;
    root $WEBMAIL_DIR;
    index index.php;
    
    location / {
        try_files \$uri \$uri/ /index.php?\$query_string;
    }
    
    location ~ \.php$ {
        include snippets/fastcgi-php.conf;
        fastcgi_pass unix:/run/php/php$PHP_VERSION-fpm.sock;
    }
}
EOF

        ln -sf /etc/nginx/sites-available/$MAIL_HOSTNAME /etc/nginx/sites-enabled/
        nginx -t && systemctl reload nginx

        if command -v certbot >/dev/null 2>&1; then
            certbot --nginx -d "$MAIL_HOSTNAME" --redirect --non-interactive --agree-tos -m admin@$CURRENT_DOMAIN || true
        fi

        echo "✅ Webmail installation completed!"
    fi
}

install_mailserver() {
    # -----------------------------
    # INPUT
    # -----------------------------
    read -p "Enter your main domain (e.g., example.com): " DOMAIN

    if [[ -z "$DOMAIN" ]]; then
        echo "❌ Domain is required. Aborting."
        exit 1
    fi

    # Set hostname automatically
    HOSTNAME="mail.$DOMAIN"

    echo "🔧 Using hostname: $HOSTNAME"

    # -----------------------------
    # SET HOSTNAME
    # -----------------------------
    echo "🔧 Setting hostname..."
    hostnamectl set-hostname "$HOSTNAME"
    echo "127.0.0.1 $HOSTNAME" >> /etc/hosts

    # -----------------------------
    # INSTALL PACKAGES
    # -----------------------------
    echo "📦 Installing mail server packages..."
    apt update
    apt install -y postfix dovecot-imapd dovecot-pop3d dovecot-mysql opendkim opendkim-tools spamassassin spamc

    # -----------------------------
    # POSTFIX CONFIGURATION
    # -----------------------------
    echo "⚙️ Configuring Postfix..."
    postconf -e "myhostname = $HOSTNAME"
    postconf -e "mydomain = $DOMAIN"
    postconf -e "myorigin = \$mydomain"
    postconf -e "mydestination = \$myhostname, localhost.\$mydomain, localhost, \$mydomain"
    postconf -e "relayhost = "
    postconf -e "mynetworks = 127.0.0.0/8 [::ffff:127.0.0.0]/104 [::1]/128"
    postconf -e "mailbox_size_limit = 0"
    postconf -e "recipient_delimiter = +"
    postconf -e "inet_interfaces = all"
    postconf -e "inet_protocols = all"
    postconf -e "home_mailbox = Maildir/"
    postconf -e "smtpd_sasl_type = dovecot"
    postconf -e "smtpd_sasl_path = private/auth"
    postconf -e "smtpd_sasl_auth_enable = yes"
    postconf -e "smtpd_recipient_restrictions = permit_sasl_authenticated,permit_mynetworks,reject_unauth_destination"

    # -----------------------------
    # CONFIGURE DOVECOT
    # -----------------------------
    echo "🕊️ Configuring Dovecot..."
    
    # Remove old config completely
    rm -f /etc/dovecot/dovecot.conf
    
    # Create fresh, complete config
    cat > /etc/dovecot/dovecot.conf << 'EOF'
!include conf.d/*.conf

protocols = imap pop3 lmtp
listen = *
mail_location = maildir:~/Maildir
auth_mechanisms = plain login

passdb {
  driver = pam
}

userdb {
  driver = passwd
}

service auth {
  unix_listener /var/spool/postfix/private/auth {
    mode = 0666
    user = postfix
    group = postfix
  }
}

service imap-login {
  inet_listener imap { port = 143 }
  inet_listener imaps { port = 993; ssl = yes }
}
service pop3-login {
  inet_listener pop3 { port = 110 }
  inet_listener pop3s { port = 995; ssl = yes }
}
EOF

    # -----------------------------
    # OPENDKIM CONFIGURATION
    # -----------------------------
    echo "🔐 Configuring OpenDKIM..."
    mkdir -p /etc/opendkim/keys/$DOMAIN
    opendkim-genkey -s mail -d $DOMAIN -D /etc/opendkim/keys/$DOMAIN
    chown opendkim:opendkim /etc/opendkim/keys/$DOMAIN/mail.private

    cat > /etc/opendkim.conf <<EOF
Syslog yes
UMask 002
Canonicalization relaxed/simple
ExternalIgnoreList refile:/etc/opendkim/TrustedHosts
InternalHosts refile:/etc/opendkim/TrustedHosts
KeyTable refile:/etc/opendkim/KeyTable
SigningTable refile:/etc/opendkim/SigningTable
Mode sv
UserID opendkim:opendkim
Socket inet:12301@localhost
EOF

    echo "127.0.0.1" > /etc/opendkim/TrustedHosts
    echo "localhost" >> /etc/opendkim/TrustedHosts
    echo "*.$DOMAIN" >> /etc/opendkim/TrustedHosts
    echo "mail._domainkey.$DOMAIN $DOMAIN:mail:/etc/opendkim/keys/$DOMAIN/mail.private" > /etc/opendkim/KeyTable
    echo "*@$DOMAIN mail._domainkey.$DOMAIN" > /etc/opendkim/SigningTable

    postconf -e "milter_default_action = accept"
    postconf -e "milter_protocol = 2"
    postconf -e "smtpd_milters = inet:localhost:12301"
    postconf -e "non_smtpd_milters = inet:localhost:12301"

    # -----------------------------
    # CONFIGURE FIREWALL (SKIPPED)
    # -----------------------------
    echo "🔥 Firewall configuration skipped (managed by AWS Security Groups)"

    # -----------------------------
    # CREATE NGINX CONFIG FIRST (before SSL)
    # -----------------------------
    echo "🌐 Creating Nginx config for webmail..."
    
    # Detect PHP version dynamically FIRST
    PHP_VERSION=$(ls /run/php/ | grep -E 'php[0-9]+\.[0-9]+-fpm\.sock' | head -1 | sed 's/php//g' | sed 's/-fpm\.sock//g')
    if [[ -z "$PHP_VERSION" ]]; then
        PHP_VERSION="8.1"  # fallback
    fi
    
    echo "📦 Installing required PHP extensions for PHP $PHP_VERSION..."
    apt install -y "php$PHP_VERSION-curl" "php$PHP_VERSION-intl" "php$PHP_VERSION-xml" "php$PHP_VERSION-mbstring" "php$PHP_VERSION-mysql" "php$PHP_VERSION-imagick" "php$PHP_VERSION-ldap" "php$PHP_VERSION-zip" "php$PHP_VERSION-bcmath" "php$PHP_VERSION-gd" "php$PHP_VERSION-dom"
    
    WEBMAIL_DIR="/var/www/webmail"
    mkdir -p "$WEBMAIL_DIR"
    chown -R www-data:www-data "$WEBMAIL_DIR"
    
    cat > /etc/nginx/sites-available/$HOSTNAME <<EOF
server {
    listen 80;
    server_name $HOSTNAME;
    root $WEBMAIL_DIR;
    index index.php;
    
    location / {
        try_files \$uri \$uri/ /index.php?\$query_string;
    }
    
    location ~ \.php$ {
        include snippets/fastcgi-php.conf;
        fastcgi_pass unix:/run/php/php$PHP_VERSION-fpm.sock;
    }
}
EOF

    ln -sf /etc/nginx/sites-available/$HOSTNAME /etc/nginx/sites-enabled/
    nginx -t && systemctl reload nginx

    # -----------------------------
    # SSL CERTIFICATE (after Nginx config exists)
    # -----------------------------
    echo "🔐 Setting up SSL certificate..."
    if command -v certbot >/dev/null 2>&1; then
        certbot --nginx -d "$HOSTNAME" --redirect --non-interactive --agree-tos -m admin@$DOMAIN || true
        postconf -e "smtpd_tls_cert_file = /etc/letsencrypt/live/$HOSTNAME/fullchain.pem"
        postconf -e "smtpd_tls_key_file = /etc/letsencrypt/live/$HOSTNAME/privkey.pem"
        postconf -e "smtpd_use_tls = yes"
    fi

    # -----------------------------
    # INSTALL ROUNDCUBE FILES
    # -----------------------------
    echo "🌐 Installing Roundcube webmail files..."
    
    # Clean up existing webmail directory if reinstalling
    if [[ -d "$WEBMAIL_DIR" && -f "$WEBMAIL_DIR/index.php" ]]; then
        echo "🧹 Cleaning up existing webmail installation..."
        rm -rf "$WEBMAIL_DIR"/*
    fi
    
    cd /tmp
    wget -q https://github.com/roundcube/roundcubemail/releases/download/1.6.9/roundcubemail-1.6.9-complete.tar.gz
    tar -xzf roundcubemail-1.6.9-complete.tar.gz
    mv roundcubemail-1.6.9/* "$WEBMAIL_DIR/"
    rm -rf roundcubemail-1.6.9*

    chown -R www-data:www-data "$WEBMAIL_DIR"
    cd "$WEBMAIL_DIR"
    
    # Check if composer exists, if not install minimal requirements
    if ! command -v composer >/dev/null 2>&1; then
        echo "📦 Installing composer..."
        curl -sS https://getcomposer.org/installer | php
        mv composer.phar /usr/local/bin/composer
        chmod +x /usr/local/bin/composer
    fi
    
    # Run composer as non-root for safety
    if command -v sudo >/dev/null 2>&1 && id -u www-data >/dev/null 2>&1; then
        echo "🎵 Running composer as www-data user..."
        sudo -u www-data composer install --no-dev --optimize-autoloader
    else
        echo "⚠️ Running composer as root (not recommended)"
        composer install --no-dev --optimize-autoloader --ignore-platform-reqs
    fi

    cp config/config.inc.php.sample config/config.inc.php
    SECRET_KEY=$(openssl rand -base64 24)
    # Use different delimiter to avoid conflicts with special characters
    sed -i "s|\$config\['des_key'\] = .*|\$config['des_key'] = '$SECRET_KEY';|" config/config.inc.php
    sed -i "s|\$config\['default_host'\] = .*|\$config['default_host'] = 'localhost';|" config/config.inc.php

    # -----------------------------
    # START SERVICES
    # -----------------------------
    echo "🚀 Starting mail services..."
    systemctl restart postfix dovecot opendkim
    systemctl enable postfix dovecot opendkim

    echo ""
    echo "🎉 Mail server setup completed!"
    echo "--------------------------------"
    echo "Hostname    : $HOSTNAME"
    echo "Domain      : $DOMAIN"
    echo "Webmail     : https://$HOSTNAME"
    echo ""
    echo "🔧 IMPORTANT: Add these DNS records:"
    echo "------------------------------------"
    echo "A Record    : $HOSTNAME → YOUR_SERVER_IP"
    echo "MX Record   : $DOMAIN → $HOSTNAME (priority 10)"
    echo "SPF Record  : v=spf1 mx -all"
    echo "DKIM Record : $(cat /etc/opendkim/keys/$DOMAIN/mail.txt)"
    echo "DMARC Record : v=DMARC1; p=quarantine; rua=mailto:dmarc@$DOMAIN"
    echo ""
    echo "✅ Mail server installation completed!"
}

create_email() {
    read -p "Enter email address (e.g., user@$(hostname -d)): " EMAIL_ADDR
    read -s -p "Enter password: " EMAIL_PASS
    echo
    read -s -p "Confirm password: " EMAIL_PASS_CONFIRM
    echo
    
    if [[ "$EMAIL_PASS" != "$EMAIL_PASS_CONFIRM" ]]; then
        echo "❌ Passwords do not match."
        return
    fi
    
    EMAIL_USER=$(echo "$EMAIL_ADDR" | cut -d'@' -f1)
    EMAIL_DOMAIN=$(echo "$EMAIL_ADDR" | cut -d'@' -f2)
    
    if ! id "$EMAIL_USER" &>/dev/null; then
        useradd -m -s /bin/bash "$EMAIL_USER"
        echo "$EMAIL_USER:$EMAIL_PASS" | chpasswd
        echo "✅ System user created: $EMAIL_USER"
    else
        echo "$EMAIL_USER:$EMAIL_PASS" | chpasswd
        echo "ℹ️ Password updated for: $EMAIL_USER"
    fi
    
    usermod -aG mail "$EMAIL_USER"
    mkdir -p "/home/$EMAIL_USER/Maildir"{/cur,/new,/tmp}
    chown -R "$EMAIL_USER":mail "/home/$EMAIL_USER/Maildir"
    
    echo "✅ Email account ready: $EMAIL_ADDR"
    echo ""
    echo "📧 Email Settings:"
    echo "------------------"
    echo "Webmail     : https://mail.$EMAIL_DOMAIN"
    echo "IMAP Server : mail.$EMAIL_DOMAIN:993"
    echo "SMTP Server : mail.$EMAIL_DOMAIN:587"
    echo "Username    : $EMAIL_ADDR"
    echo "Password    : [your password]"
    echo ""
    echo "🔧 Required DNS Records (if not already added):"
    echo "--------------------------------------------"
    echo "A Record    : mail.$EMAIL_DOMAIN → YOUR_SERVER_IP"
    echo "MX Record   : $EMAIL_DOMAIN → mail.$EMAIL_DOMAIN (priority 10)"
    echo "SPF Record  : v=spf1 mx -all"
    if [[ -f "/etc/opendkim/keys/$EMAIL_DOMAIN/mail.txt" ]]; then
        echo "DKIM Record : $(cat /etc/opendkim/keys/$EMAIL_DOMAIN/mail.txt)"
    fi
    echo "DMARC Record : v=DMARC1; p=quarantine; rua=mailto:dmarc@$EMAIL_DOMAIN"
    echo ""
    echo "🔥 Required Firewall Ports (configure in AWS Security Groups):"
    echo "---------------------------"
    echo "25   - SMTP"
    echo "587  - SMTP Submission"
    echo "465  - SMTPS"
    echo "993  - IMAPS"
    echo "143  - IMAP"
    echo "995  - POP3S"
    echo "110  - POP3"
    echo ""
    echo "⚠️ Make sure DNS records and firewall ports are configured!"
}

list_emails() {
    echo "📋 Email accounts:"
    for user in $(getent passwd | grep -E "home.*bash" | cut -d: -f1); do
        if [[ -d "/home/$user/Maildir" ]]; then
            echo "📧 $user@$(hostname -d)"
        fi
    done
}

change_password() {
    read -p "Enter email address: " EMAIL_ADDR
    EMAIL_USER=$(echo "$EMAIL_ADDR" | cut -d'@' -f1)
    
    if ! id "$EMAIL_USER" &>/dev/null; then
        echo "❌ User not found."
        return
    fi
    
    read -s -p "Enter new password: " NEW_PASS
    echo
    read -s -p "Confirm new password: " NEW_PASS_CONFIRM
    echo
    
    if [[ "$NEW_PASS" != "$NEW_PASS_CONFIRM" ]]; then
        echo "❌ Passwords do not match."
        return
    fi
    
    echo "$EMAIL_USER:$NEW_PASS" | chpasswd
    echo "✅ Password updated for $EMAIL_ADDR"
}

delete_email() {
    read -p "Enter email address to delete: " EMAIL_ADDR
    EMAIL_USER=$(echo "$EMAIL_ADDR" | cut -d'@' -f1)
    
    if ! id "$EMAIL_USER" &>/dev/null; then
        echo "❌ User not found."
        return
    fi
    
    read -p "Delete $EMAIL_ADDR? This removes all emails. (y/n): " CONFIRM
    if [[ "$CONFIRM" =~ ^[Yy]$ ]]; then
        userdel -r "$EMAIL_USER" 2>/dev/null || true
        echo "✅ Email account deleted: $EMAIL_ADDR"
    else
        echo "❌ Cancelled."
    fi
}

# -----------------------------
# MAIN LOGIC
# -----------------------------
# Get current hostname to avoid inconsistencies
CURRENT_HOSTNAME=$(hostname)
CURRENT_DOMAIN=$(hostname -d)
MAIL_HOSTNAME="mail.$CURRENT_DOMAIN"

# Check if mail server is properly installed
if [[ ! -d "/etc/postfix" || ! -d "/etc/dovecot" || ! -f "/etc/opendkim.conf" ]]; then
    echo "🔧 Mail server not found or incomplete. Installing..."
    install_mailserver
elif [[ ! -f "/var/www/webmail/index.php" ]]; then
    echo "🔧 Mail server found but webmail missing. Installing webmail..."
    install_webmail_only
elif [[ ! -f "/etc/letsencrypt/live/$MAIL_HOSTNAME/fullchain.pem" ]]; then
    echo "🔐 Mail server and webmail found but SSL missing. Installing SSL..."
    install_ssl_only
fi

echo ""
echo "What would you like to do?"
echo "1) Create new email account"
echo "2) List email accounts"
echo "3) Change email password"
echo "4) Delete email account"
echo "5) Reinstall mail server (WARNING: Deletes everything)"
echo "6) Exit"
read -p "Choose option (1-6): " CHOICE

case $CHOICE in
    1) create_email ;;
    2) list_emails ;;
    3) change_password ;;
    4) delete_email ;;
    5) 
        echo "⚠️ Removing existing mail server..."
        systemctl stop postfix dovecot opendkim 2>/dev/null || true
        apt remove --purge -y postfix dovecot-* opendkim* 2>/dev/null || true
        rm -rf /etc/postfix /etc/dovecot /etc/opendkim /var/mail/*
        echo "🔄 Proceeding with fresh installation..."
        install_mailserver
        ;;
    6) echo "👋 Goodbye!"; exit 0 ;;
    *) echo "❌ Invalid option."; exit 1 ;;
esac
