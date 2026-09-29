#!/usr/bin/env bash
# Restore the site's databases from an archive produced by backup.sh.
#
#   List archives:   sudo bash deploy/restore.sh --list
#   Restore latest:  sudo bash deploy/restore.sh --latest
#   Restore one:     sudo bash deploy/restore.sh /opt/valorlink/backups/yeehaw-fc-20260715-030000.tar.gz
#
# The restore stops the site, moves the current databases aside (renamed
# *.pre-restore-<timestamp>, never deleted), lays the snapshot back down,
# then starts the site again. Because it overwrites live data, it always
# asks for confirmation unless you pass --yes.
set -euo pipefail

APP_HOME="${APP_HOME:-${VALORLINK_HOME:-/opt/valorlink}}"
BACKUP_DIR="${BACKUP_DIR:-$APP_HOME/backups}"
DATA_DIR="${DATA_DIR:-$APP_HOME/proclubs/data}"
SERVICES=(yeehaw-fc)

usage() { grep -E '^#( |$)' "$0" | sed -E 's/^# ?//'; exit "${1:-0}"; }

# Both prefixes: archives taken before the rename are still restorable.
list_archives() {
    ls -1t "$BACKUP_DIR"/yeehaw-fc-*.tar.gz "$BACKUP_DIR"/valorlink-*.tar.gz 2>/dev/null
}

archive=""
assume_yes=0
for arg in "$@"; do
    case "$arg" in
        --list)
            list_archives || echo "(no archives in $BACKUP_DIR)"
            exit 0 ;;
        --latest)
            archive="$(list_archives | head -n1 || true)" ;;
        --yes) assume_yes=1 ;;
        -h|--help) usage 0 ;;
        -*) echo "unknown option: $arg" >&2; usage 1 ;;
        *) archive="$arg" ;;
    esac
done

if [[ $EUID -ne 0 ]]; then
    echo "Run me with sudo (it stops/starts the site): sudo bash deploy/restore.sh ..." >&2
    exit 1
fi
if [[ -z "$archive" ]]; then
    echo "No archive given. Try --list, --latest, or a path." >&2
    usage 1
fi
if [[ ! -f "$archive" ]]; then
    echo "Not a file: $archive" >&2
    exit 1
fi

echo "About to restore from: $archive"
echo "Into:                  $DATA_DIR"
echo "This overwrites the live databases (current copies are kept as *.pre-restore-*)."
if [[ "$assume_yes" -ne 1 ]]; then
    read -r -p "Proceed? [y/N] " reply
    [[ "$reply" =~ ^[Yy]$ ]] || { echo "Aborted."; exit 1; }
fi

work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT
tar -xzf "$archive" -C "$work"
# Either prefix, so a pre-rename archive restores too.
snap="$(find "$work" -maxdepth 1 -type d \( -name 'yeehaw-fc-*' -o -name 'valorlink-*' \) | head -n1)"
[[ -n "$snap" ]] || { echo "Archive has no snapshot directory." >&2; exit 1; }

echo "Stopping the site..."
systemctl stop "${SERVICES[@]}" || true

stamp="$(date -u +%Y%m%d-%H%M%S)"
restored=0
while IFS= read -r -d '' src; do
    # A pre-rename archive holds paths relative to the old app root
    # (e.g. "proclubs/data/site.db"); a current one holds them relative to
    # the data dir ("site.db"). Match on the basename so both land in the
    # right place, and skip the bot's databases, which have nothing to
    # restore into any more.
    name="$(basename "$src")"
    case "$name" in
        site.db|history.db) ;;
        *) echo "  skipped $name (not a site database)"; continue ;;
    esac
    target="$DATA_DIR/$name"
    mkdir -p "$(dirname "$target")"
    if [[ -f "$target" ]]; then
        mv "$target" "$target.pre-restore-$stamp"
    fi
    # Drop any stale WAL/SHM so SQLite doesn't replay onto the restored file.
    rm -f "$target-wal" "$target-shm"
    cp "$src" "$target"
    chown valorlink:valorlink "$target" 2>/dev/null || true
    echo "  restored $name"
    restored=$((restored + 1))
done < <(find "$snap" -type f -name '*.db' -print0)

echo "Starting the site..."
systemctl start "${SERVICES[@]}"
echo "Done. Restored $restored database(s). Previous copies saved as *.pre-restore-$stamp."
