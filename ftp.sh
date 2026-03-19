#!/bin/bash
set -e

# --------------------------------------------------
# ROOT CHECK
# --------------------------------------------------
if [ "$EUID" -ne 0 ]; then
  echo "❌ Please run this script as root (use sudo)"
  exit 1
fi

echo "🚀 FTP USER CREATION (DIRECTORY ACCESS)"
echo "---------------------------------------"

# -----------------------------
# INPUT
# -----------------------------
read -p "FTP Username: " FTP_USER

while true; do
  read -s -p "Enter FTP Password: " FTP_PASS
  echo
  read -s -p "Confirm Password: " FTP_PASS_CONFIRM
  echo
  [[ "$FTP_PASS" == "$FTP_PASS_CONFIRM" ]] && break
  echo "❌ Password mismatch. Try again."
done

# Check if domain webroot is available and use as default
if [[ -f /tmp/domain_webroot.txt ]]; then
    DEFAULT_DIR=$(cat /tmp/domain_webroot.txt)
    echo "🔹 Detected domain webroot: $DEFAULT_DIR"
    read -p "FTP directory (absolute path) [$DEFAULT_DIR]: " FTP_DIR
    FTP_DIR=${FTP_DIR:-$DEFAULT_DIR}
else
    read -p "FTP directory (absolute path, e.g., /var/www/html): " FTP_DIR
fi

# -----------------------------
# VALIDATE
# -----------------------------
if id "$FTP_USER" &>/dev/null; then
  echo "❌ User already exists. Aborting."
  exit 1
fi

# -----------------------------
# INSTALL VSFTPD
# -----------------------------
if ! command -v vsftpd &>/dev/null; then
  echo "📦 Installing vsftpd..."
  sudo apt update -y
  sudo apt install -y vsftpd
fi

# -----------------------------
# ENSURE SHELL IS ALLOWED
# -----------------------------
grep -q "/usr/sbin/nologin" /etc/shells || echo "/usr/sbin/nologin" | sudo tee -a /etc/shells

# -----------------------------
# ENSURE USER CONFIG DIR
# -----------------------------
sudo mkdir -p /etc/vsftpd/user_conf
if ! grep -q "^user_config_dir=" /etc/vsftpd.conf; then
  echo "🛠 Enabling per-user configs..."
  sudo cp /etc/vsftpd.conf /etc/vsftpd.conf.bak
  echo "user_config_dir=/etc/vsftpd/user_conf" | sudo tee -a /etc/vsftpd.conf
fi

# -----------------------------
# GLOBAL VSFTPD SETTINGS
# -----------------------------
sudo sed -i 's/^#*allow_writeable_chroot=.*/allow_writeable_chroot=YES/' /etc/vsftpd.conf || \
    echo "allow_writeable_chroot=YES" | sudo tee -a /etc/vsftpd.conf

sudo sed -i 's/^#*local_enable=.*/local_enable=YES/' /etc/vsftpd.conf || \
    echo "local_enable=YES" | sudo tee -a /etc/vsftpd.conf

sudo sed -i 's/^#*write_enable=.*/write_enable=YES/' /etc/vsftpd.conf || \
    echo "write_enable=YES" | sudo tee -a /etc/vsftpd.conf

# Passive FTP config (replace with your public IP)
PUBLIC_IP=$(curl -4 -s ifconfig.me)
sudo sed -i '/^pasv_address=/d' /etc/vsftpd.conf
echo "pasv_address=$PUBLIC_IP" | sudo tee -a /etc/vsftpd.conf
sudo sed -i '/^pasv_min_port=/d' /etc/vsftpd.conf
echo "pasv_min_port=30000" | sudo tee -a /etc/vsftpd.conf
sudo sed -i '/^pasv_max_port=/d' /etc/vsftpd.conf
echo "pasv_max_port=30100" | sudo tee -a /etc/vsftpd.conf

# -----------------------------
# CREATE FTP USER
# -----------------------------
sudo useradd -d "$FTP_DIR" -s /usr/sbin/nologin "$FTP_USER"
echo "$FTP_USER:$FTP_PASS" | sudo chpasswd
sudo usermod -aG www-data "$FTP_USER"

# -----------------------------
# DIRECTORY SETUP
# -----------------------------
sudo mkdir -p "$FTP_DIR"
sudo chown "$FTP_USER":www-data "$FTP_DIR"
sudo chmod 775 "$FTP_DIR"

# -----------------------------
# ACLS (for www-data inheritance)
# -----------------------------
if ! command -v setfacl >/dev/null 2>&1; then
    echo "📦 Installing ACL package..."
    sudo apt update
    sudo apt install -y acl
fi

sudo setfacl -R -m g:www-data:rwx "$FTP_DIR"
sudo setfacl -R -d -m g:www-data:rwx "$FTP_DIR"

# -----------------------------
# PER-USER VSFTPD CONFIG
# -----------------------------
sudo tee "/etc/vsftpd/user_conf/$FTP_USER" > /dev/null <<EOF
local_root=$FTP_DIR
chroot_local_user=YES
EOF

# -----------------------------
# RESTART FTP SERVICE
# -----------------------------
sudo systemctl restart vsftpd

echo "✅ FTP user created safely!"
echo "User      : $FTP_USER"
echo "Root      : $FTP_DIR"
echo "Group     : www-data"
echo "Access    : Chrooted, full read/write"
echo "Passive   : 30000-30100 (ensure allowed in AWS SG)"
echo "-----------------------------------------------"

# Clean up temporary webroot file
rm -f /tmp/domain_webroot.txt
