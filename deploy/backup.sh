#!/usr/bin/env bash
# Back up every ValorLink SQLite database into one timestamped, compressed
# archive, then prune old archives. Safe to run while the bot and web app are
# live: it uses SQLite's online ".backup" (a consistent snapshot that accounts
# for the WAL), never a plain cp of a file being written.
#
# Run by hand:            sudo -u valorlink bash deploy/backup.sh
# Or on a schedule:       via valorlink-backup.timer (see install.sh)
#
# Configuration (env vars, or set them in /opt/valorlink/.env):
#   VALORLINK_HOME     app / data root         (default /opt/valorlink)
#   BACKUP_DIR         where archives are kept  (default $VALORLINK_HOME/backups)
#   BACKUP_RETENTION   how many archives to keep (default 14)
#   BACKUP_REMOTE      optional rclone target, e.g. "spaces:valorlink-backups".
#                      If set and rclone is installed, each archive is copied
#                      off-box after it is written.
set -euo pipefail

VALORLINK_HOME="${VALORLINK_HOME:-/opt/valorlink}"

# Pull BACKUP_* / paths from the app env file if present (without clobbering
# anything already exported in this shell).
if [[ -f "$VALORLINK_HOME/.env" ]]; then
    set -a
    # shellcheck disable=SC1091
    source <(grep -E '^(BACKUP_[A-Z_]+|VALORLINK_HOME|UNIT_DB_DIR)=' "$VALORLINK_HOME/.env" || true)
    set +a
fi

BACKUP_DIR="${BACKUP_DIR:-$VALORLINK_HOME/backups}"
BACKUP_RETENTION="${BACKUP_RETENTION:-14}"
BACKUP_REMOTE="${BACKUP_REMOTE:-}"

if ! command -v sqlite3 >/dev/null 2>&1; then
    echo "backup: sqlite3 is not installed (apt install -y sqlite3)" >&2
    exit 1
fi

stamp="$(date -u +%Y%m%d-%H%M%S)"
work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT
snap="$work/valorlink-$stamp"
mkdir -p "$snap"

# Every *.db under the data root is a live database (registry.db, the default
# valorlink.db, and units/*.db). Skip the backups dir and archived-unit files.
count=0
while IFS= read -r -d '' db; do
    rel="${db#"$VALORLINK_HOME"/}"          # e.g. "registry.db" or "units/5thva.db"
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
done < <(find "$VALORLINK_HOME" -type f -name '*.db' \
            -not -path "$BACKUP_DIR/*" \
            -not -name '*.removed-*' -print0)

if [[ "$count" -eq 0 ]]; then
    echo "backup: no databases found under $VALORLINK_HOME — nothing to do" >&2
    exit 1
fi

mkdir -p "$BACKUP_DIR"
archive="$BACKUP_DIR/valorlink-$stamp.tar.gz"
tar -czf "$archive" -C "$work" "valorlink-$stamp"
chmod 600 "$archive"
echo "backup: wrote $archive ($count database(s), $(du -h "$archive" | cut -f1))"

# Prune: keep the newest $BACKUP_RETENTION archives.
mapfile -t old < <(ls -1t "$BACKUP_DIR"/valorlink-*.tar.gz 2>/dev/null | tail -n +"$((BACKUP_RETENTION + 1))")
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
