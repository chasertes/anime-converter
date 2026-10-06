#!/bin/bash

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

echo "Installing anime-converter..."

if [ "$(id -u)" -ne 0 ]; then
    echo "Run this script as root:"
    echo "sudo $0"
    exit 1
fi

echo "Installing dependencies..."

apt-get update
apt-get install -y \
    ffmpeg \
    python3

echo "Installing converter..."

install -m 0755 \
    "$SCRIPT_DIR/anime-converter.py" \
    /usr/local/bin/anime-converter.py

echo "Installing systemd service..."

install -m 0644 \
    "$SCRIPT_DIR/anime-converter.service" \
    /etc/systemd/system/anime-converter.service

echo "Reloading systemd..."

systemctl daemon-reload

echo "Enabling service..."

systemctl enable anime-converter.service

echo "Starting service..."

systemctl restart anime-converter.service

echo
echo "anime-converter installed successfully."
echo
echo "Service status:"
systemctl --no-pager status anime-converter.service
