#!/bin/bash
set -e

# --------------------------------------------------
# ROOT CHECK
# --------------------------------------------------
if [ "$EUID" -ne 0 ]; then
  echo "❌ Please run this script as root (use sudo)"
  exit 1
fi

echo "🧠 Swap Setup Script (RAM-Safe)"
echo "-------------------------------"

# -----------------------------
# CHECK EXISTING SWAP
# -----------------------------
if swapon --show | grep -q '^'; then
  echo "✅ Swap already exists. Skipping creation."
  swapon --show
  exit 0
fi

# -----------------------------
# DETECT RAM
# -----------------------------
TOTAL_MEM_MB=$(free -m | awk '/^Mem:/{print $2}')
echo "📊 Detected RAM: ${TOTAL_MEM_MB} MB"

# -----------------------------
# DECIDE SWAP SIZE (SAFE RULES)
# -----------------------------
if [ "$TOTAL_MEM_MB" -le 512 ]; then
  SWAP_MB=1024        # 1 GB swap for tiny servers
elif [ "$TOTAL_MEM_MB" -le 1024 ]; then
  SWAP_MB=2048        # 2 GB swap
elif [ "$TOTAL_MEM_MB" -le 2048 ]; then
  SWAP_MB=2048        # equal swap
else
  SWAP_MB=2048        # cap at 2 GB (safe default)
fi

echo "🗄️ Creating swap size: ${SWAP_MB} MB"

SWAPFILE="/swapfile"

# -----------------------------
# CREATE SWAP FILE
# -----------------------------
if command -v fallocate >/dev/null 2>&1; then
  sudo fallocate -l ${SWAP_MB}M $SWAPFILE
else
  sudo dd if=/dev/zero of=$SWAPFILE bs=1M count=$SWAP_MB status=progress
fi

# -----------------------------
# SET PERMISSIONS
# -----------------------------
sudo chmod 600 $SWAPFILE
sudo mkswap $SWAPFILE
sudo swapon $SWAPFILE

# -----------------------------
# PERSIST SWAP
# -----------------------------
if ! grep -q "$SWAPFILE" /etc/fstab; then
  echo "$SWAPFILE none swap sw 0 0" | sudo tee -a /etc/fstab > /dev/null
fi

# -----------------------------
# KERNEL TUNING (SAFE)
# -----------------------------
echo "⚙️ Applying kernel tuning..."

sudo sysctl vm.swappiness=10
sudo sysctl vm.vfs_cache_pressure=50

if ! grep -q "vm.swappiness" /etc/sysctl.conf; then
  echo "vm.swappiness=10" | sudo tee -a /etc/sysctl.conf > /dev/null
fi

if ! grep -q "vm.vfs_cache_pressure" /etc/sysctl.conf; then
  echo "vm.vfs_cache_pressure=50" | sudo tee -a /etc/sysctl.conf > /dev/null
fi

# -----------------------------
# VERIFY
# -----------------------------
echo "✅ Swap successfully enabled!"
free -h
swapon --show

echo "🎉 Swap setup complete (RAM-safe)"
