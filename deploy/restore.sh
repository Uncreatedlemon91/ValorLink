#!/usr/bin/env bash
# Restore ValorLink databases from a backup archive produced by backup.sh.
#
#   List archives:   sudo bash deploy/restore.sh --list
#   Restore latest:  sudo bash deploy/restore.sh --latest
#   Restore one:     sudo bash deploy/restore.sh /opt/valorlink/backups/valorlink-20260715-030000.tar.gz
#
# The restore stops the bot and web services, moves the current databases aside
# (renamed *.pre-restore-<timestamp>, never deleted), lays the snapshot back
# down, then restarts the services. Because it overwrites live data, it always
# asks for confirmation unless you pass --yes.
set -euo pipefail

VALORLINK_HOME="${VALORLINK_HOME:-/opt/valorlink}"
BACKUP_DIR="${BACKUP_DIR:-$VALORLINK_HOME/backups}"
SERVICES=(valorlink-bot valorlink-web)

usage() { grep -E '^#( |$)' "$0" | sed -E 's/^# ?//'; exit "${1:-0}"; }

archive=""
assume_yes=0
for arg in "$@"; do
    case "$arg" in
        --list)
            ls -1t "$BACKUP_DIR"/valorlink-*.tar.gz 2>/dev/null || echo "(no archives in $BACKUP_DIR)"
            exit 0 ;;
        --latest)
            archive="$(ls -1t "$BACKUP_DIR"/valorlink-*.tar.gz 2>/dev/null | head -n1 || true)" ;;
        --yes) assume_yes=1 ;;
        -h|--help) usage 0 ;;
        -*) echo "unknown option: $arg" >&2; usage 1 ;;
        *) archive="$arg" ;;
    esac
done

if [[ $EUID -ne 0 ]]; then
    echo "Run me with sudo (it stops/starts services): sudo bash deploy/restore.sh ..." >&2
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
echo "Into:                  $VALORLINK_HOME"
echo "This overwrites the live databases (current copies are kept as *.pre-restore-*)."
if [[ "$assume_yes" -ne 1 ]]; then
    read -r -p "Proceed? [y/N] " reply
    [[ "$reply" =~ ^[Yy]$ ]] || { echo "Aborted."; exit 1; }
fi

work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT
tar -xzf "$archive" -C "$work"
snap="$(find "$work" -maxdepth 1 -type d -name 'valorlink-*' | head -n1)"
[[ -n "$snap" ]] || { echo "Archive has no valorlink-* snapshot directory." >&2; exit 1; }

echo "Stopping services..."
systemctl stop "${SERVICES[@]}" || true

stamp="$(date -u +%Y%m%d-%H%M%S)"
restored=0
while IFS= read -r -d '' src; do
    rel="${src#"$snap"/}"                    # e.g. "registry.db" or "units/5thva.db"
    target="$VALORLINK_HOME/$rel"
    mkdir -p "$(dirname "$target")"
    if [[ -f "$target" ]]; then
        mv "$target" "$target.pre-restore-$stamp"
    fi
    # Drop any stale WAL/SHM so SQLite doesn't replay onto the restored file.
    rm -f "$target-wal" "$target-shm"
    cp "$src" "$target"
    chown valorlink:valorlink "$target" 2>/dev/null || true
    echo "  restored $rel"
    restored=$((restored + 1))
done < <(find "$snap" -type f -name '*.db' -print0)

echo "Restarting services..."
systemctl start "${SERVICES[@]}"
echo "Done. Restored $restored database(s). Previous copies saved as *.pre-restore-$stamp."
