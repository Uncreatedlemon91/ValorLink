#!/usr/bin/env bash
# Install (or update) the ValorLink systemd units and start them.
# Run from the repo on the server:  sudo bash deploy/install.sh
set -euo pipefail

DIR="$(cd "$(dirname "$0")" && pwd)"

if [[ $EUID -ne 0 ]]; then
    echo "Run me with sudo: sudo bash deploy/install.sh" >&2
    exit 1
fi

cp "$DIR/valorlink-bot.service" "$DIR/valorlink-web.service" /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now valorlink-bot valorlink-web
systemctl restart valorlink-bot valorlink-web
systemctl --no-pager --lines=0 status valorlink-bot valorlink-web

echo
echo "Done. Tail logs with:"
echo "  journalctl -u valorlink-bot -f"
echo "  journalctl -u valorlink-web -f"
