#!/bin/bash
set -e

echo "🚀 Domain Setup with Add-ons Script"
echo "--------------------------------------"

# -----------------------------
# RUN DOMAIN SETUP
# -----------------------------
echo "🔹 Running domain setup..."
./domain.sh

echo ""
echo "--------------------------------------"

# -----------------------------
# DATABASE SETUP OPTION
# -----------------------------
read -p "Do you want to setup a database for this domain? (y/n): " SETUP_DB
if [[ "$SETUP_DB" =~ ^[Yy]$ ]]; then
    echo "🔹 Running database setup..."
    ./mariadb.sh
else
    echo "⏭️ Skipping database setup"
fi

echo ""
echo "--------------------------------------"

# -----------------------------
# FTP SETUP OPTION
# -----------------------------
read -p "Do you want to setup FTP access for this domain? (y/n): " SETUP_FTP
if [[ "$SETUP_FTP" =~ ^[Yy]$ ]]; then
    echo "🔹 Running FTP setup..."
    ./ftp.sh
else
    echo "⏭️ Skipping FTP setup"
fi

echo ""
echo "--------------------------------------"

# -----------------------------
# EMAIL SETUP OPTION
# -----------------------------
read -p "Do you want to setup email server and/or create email accounts? (y/n): " SETUP_EMAIL
if [[ "$SETUP_EMAIL" =~ ^[Yy]$ ]]; then
    echo "🔹 Running email setup..."
    ./email.sh
else
    echo "⏭️ Skipping email setup"
fi

echo ""
echo "--------------------------------------"
echo "🎉 Complete setup finished!"
echo "Domain, database (if selected), FTP (if selected), and email (if selected) are ready."
