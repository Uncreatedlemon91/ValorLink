#!/usr/bin/env bash
# Install (or update) the YeeHaw FC systemd units and start them.
# Run from the repo on the server:  sudo bash deploy/install.sh
set -euo pipefail

DIR="$(cd "$(dirname "$0")" && pwd)"

if [[ $EUID -ne 0 ]]; then
    echo "Run me with sudo: sudo bash deploy/install.sh" >&2
    exit 1
fi

# --- Retire the ValorLink units, if this box still has them ----------------- #
# This repo used to carry an unrelated Discord bot and its web app
# alongside the site. They're gone, but a droplet that ran them still has
# their unit files enabled and running, pointed at code that no longer
# exists -- so they'd crash-loop forever after an update.
#
# valorlink-proclubs is the site itself under its old name. It MUST be
# stopped before yeehaw-fc starts, or the two fight over 127.0.0.1:8001 and
# whichever loses stays down. Stopping it here rather than leaving it to a
# manual step is the whole reason this block exists.
LEGACY_UNITS=(
    valorlink-bot.service
    valorlink-web.service
    valorlink-proclubs.service
    valorlink-backup.service
    valorlink-backup.timer
)
for unit in "${LEGACY_UNITS[@]}"; do
    if systemctl list-unit-files "$unit" --no-legend 2>/dev/null | grep -q .; then
        echo "Retiring $unit"
        systemctl disable --now "$unit" >/dev/null 2>&1 || true
        rm -f "/etc/systemd/system/$unit"
    fi
done

cp "$DIR/yeehaw-fc.service" \
   "$DIR/yeehaw-fc-backup.service" "$DIR/yeehaw-fc-backup.timer" \
   "$DIR/proclubs-poll.service" "$DIR/proclubs-poll.timer" \
   "$DIR/proclubs-discord-events-poll.service" "$DIR/proclubs-discord-events-poll.timer" \
   "$DIR/proclubs-clips-poll.service" "$DIR/proclubs-clips-poll.timer" \
   "$DIR/proclubs-reactions-poll.service" "$DIR/proclubs-reactions-poll.timer" \
   "$DIR/proclubs-event-invites-poll.service" "$DIR/proclubs-event-invites-poll.timer" /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now yeehaw-fc
systemctl restart yeehaw-fc
# Daily database backups (the timer fires backup.sh; see deploy/README.md).
systemctl enable --now yeehaw-fc-backup.timer
# Hourly Pro Clubs history poll (the timer fires poll.py; optional -- only
# does anything if proclubs/tracked_clubs.json is set up, see deploy/README.md).
systemctl enable --now proclubs-poll.timer
# Discord Scheduled Events -> site fixtures sync, every 10 minutes (optional --
# only does anything if DISCORD_BOT_TOKEN is set in proclubs/.env).
systemctl enable --now proclubs-discord-events-poll.timer
# Discord clips -> site Clips page sync, every 30 minutes (optional -- only
# does anything if DISCORD_BOT_TOKEN and CLIPS_CHANNEL_ID are set).
systemctl enable --now proclubs-clips-poll.timer
# Discord reaction counts on article announcements, every 30 minutes
# (optional -- only does anything if DISCORD_BOT_TOKEN and
# NEWS_ANNOUNCE_CHANNEL_ID are set).
systemctl enable --now proclubs-reactions-poll.timer
# Staged event-thread invites, every 10 minutes (optional -- only does
# anything if EVENT_THREAD_CHANNEL_ID and EVENT_INVITE_TIERS are set, and
# the bot has the Server Members privileged intent; see deploy/README.md).
systemctl enable --now proclubs-event-invites-poll.timer
systemctl --no-pager --lines=0 status yeehaw-fc

VENV=/opt/valorlink/proclubs/.venv/bin/python3
APP=/opt/valorlink/proclubs

echo
echo "Done. Tail logs with:"
echo "  journalctl -u yeehaw-fc -f"
echo
echo "Backups run daily. Check with:"
echo "  systemctl list-timers yeehaw-fc-backup.timer"
echo "  sudo -u valorlink bash deploy/backup.sh    # run one now"
echo
echo "Pro Clubs history poll runs hourly. Check with:"
echo "  systemctl list-timers proclubs-poll.timer"
echo "  sudo -u valorlink $VENV $APP/poll.py    # run one now"
echo
echo "Discord Scheduled Events sync runs every 10 minutes. Check with:"
echo "  systemctl list-timers proclubs-discord-events-poll.timer"
echo "  sudo -u valorlink $VENV $APP/discord_events_poll.py    # run one now"
echo
echo "Discord clips sync runs every 30 minutes. Check with:"
echo "  systemctl list-timers proclubs-clips-poll.timer"
echo "  sudo -u valorlink $VENV $APP/discord_clips_poll.py    # run one now"
echo
echo "Discord article-reaction poll runs every 30 minutes. Check with:"
echo "  systemctl list-timers proclubs-reactions-poll.timer"
echo "  sudo -u valorlink $VENV $APP/discord_reactions_poll.py    # run one now"
echo
echo "Staged event-thread invites run every 10 minutes. Check with:"
echo "  systemctl list-timers proclubs-event-invites-poll.timer"
echo "  sudo -u valorlink $VENV $APP/event_invites_poll.py    # run one now"
