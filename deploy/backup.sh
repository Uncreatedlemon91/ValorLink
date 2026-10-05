#!/usr/bin/env bash
# Back up the site's SQLite databases into one timestamped, compressed
# archive, then prune old archives. Safe to run while the site is live: it
# uses SQLite's online ".backup" (a consistent snapshot that accounts for
# the WAL), never a plain cp of a file being written.
#
# Two databases live under proclubs/data:
#   site.db      articles, events, sign-ups, squad moves, streamers
#   history.db   locally-accumulated EA stats (see proclubs/db.py) -- this
#                one CANNOT be rebuilt, since EA evicts old matches from
#                its own rolling window.
#
# Run by hand:            sudo -u valorlink bash deploy/backup.sh
# Or on a schedule:       via yeehaw-fc-backup.timer (see install.sh)
#
# Configuration (env vars, or set them in proclubs/.env):
#   APP_HOME           repo / data root          (default /opt/valorlink)
#   BACKUP_DIR         where archives are kept   (default $APP_HOME/backups)
#   BACKUP_RETENTION   how many archives to keep (default 14)
#   BACKUP_REMOTE      optional rclone target, e.g. "spaces:yeehaw-fc-backups".
#                      If set and rclone is installed, each archive is copied
#                      off-box after it is written.
set -euo pipefail

# VALORLINK_HOME is still honoured so an existing .env on a droplet that
# predates the rename keeps working; APP_HOME wins if both are set.
APP_HOME="${APP_HOME:-${VALORLINK_HOME:-/opt/valorlink}}"
DATA_DIR="${DATA_DIR:-$APP_HOME/proclubs/data}"

# Pull BACKUP_* / paths from the app env file if present (without clobbering
# anything already exported in this shell).
if [[ -f "$APP_HOME/proclubs/.env" ]]; then
    set -a
    # shellcheck disable=SC1091
    source <(grep -E '^(BACKUP_[A-Z_]+|APP_HOME|DATA_DIR)=' "$APP_HOME/proclubs/.env" || true)
    set +a
fi

BACKUP_DIR="${BACKUP_DIR:-$APP_HOME/backups}"
BACKUP_RETENTION="${BACKUP_RETENTION:-14}"
BACKUP_REMOTE="${BACKUP_REMOTE:-}"

if ! command -v sqlite3 >/dev/null 2>&1; then
    echo "backup: sqlite3 is not installed (apt install -y sqlite3)" >&2
    exit 1
fi

stamp="$(date -u +%Y%m%d-%H%M%S)"
work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT
snap="$work/yeehaw-fc-$stamp"
mkdir -p "$snap"

count=0
while IFS= read -r -d '' db; do
    rel="${db#"$DATA_DIR"/}"                # e.g. "site.db"
    dest="$snap/$rel"
    mkdir -p "$(dirname "$dest")"
    # Online backup: consistent even under concurrent writes.
    sqlite3 "file:$db?mode=ro" ".backup '$dest'"
    # Prove the snapshot opens and passes a quick integrity check.
    if ! sqlite3 "$dest" 'PRAGMA quick_check;' | grep -q '^ok$'; then
        echo "backup: integrity check FAILED for $rel" >&2
        exit 1
    fi
    count=$((count + 1))
done < <(find "$DATA_DIR" -type f -name '*.db' \
            -not -path "$BACKUP_DIR/*" \
            -not -name '*.pre-restore-*' -print0 2>/dev/null)

if [[ "$count" -eq 0 ]]; then
    echo "backup: no databases found under $DATA_DIR — nothing to do" >&2
    exit 1
fi

mkdir -p "$BACKUP_DIR"
archive="$BACKUP_DIR/yeehaw-fc-$stamp.tar.gz"
tar -czf "$archive" -C "$work" "yeehaw-fc-$stamp"
chmod 600 "$archive"
echo "backup: wrote $archive ($count database(s), $(du -h "$archive" | cut -f1))"

# Prune: keep the newest $BACKUP_RETENTION archives. Only ours -- an older
# valorlink-*.tar.gz is left alone rather than counted or deleted, so the
# rename can't quietly expire backups taken before it.
mapfile -t old < <(ls -1t "$BACKUP_DIR"/yeehaw-fc-*.tar.gz 2>/dev/null | tail -n +"$((BACKUP_RETENTION + 1))")
for f in "${old[@]:-}"; do
    [[ -n "$f" ]] || continue
    rm -f "$f"
    echo "backup: pruned $(basename "$f")"
done

# Optional off-box copy.
if [[ -n "$BACKUP_REMOTE" ]]; then
    if command -v rclone >/dev/null 2>&1; then
        rclone copy "$archive" "$BACKUP_REMOTE" && \
            echo "backup: copied to $BACKUP_REMOTE"
    else
        echo "backup: BACKUP_REMOTE set but rclone is not installed — skipping off-box copy" >&2
    fi
fi
