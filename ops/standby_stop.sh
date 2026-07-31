#!/usr/bin/env bash
# Stop and remove the delayed standby.
#
# Safe by construction: the standby is passive (it holds no replication slot on
# the primary), so removing it has zero effect on production. The cloud archive
# is untouched.

SCRIPT_NAME=standby_stop
# shellcheck disable=SC1091
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

CONTAINER="${STANDBY_CONTAINER:-telemon_standby}"
DATA_DIR="${STANDBY_DATA_DIR:-/var/lib/telemon-standby}"
ASSUME_YES=0
[[ "${1:-}" == "--yes" ]] && ASSUME_YES=1

if docker inspect "$CONTAINER" >/dev/null 2>&1; then
  # A frozen standby almost always means an incident is still open and this
  # container is the only place the pre-incident data is instantly available.
  FROZEN="$(docker exec "$CONTAINER" psql -U "$PG_USER" -d "$PG_DB" -Atq \
             -c 'SELECT pg_is_wal_replay_paused();' 2>/dev/null || echo '?')"
  if [[ "$FROZEN" == "t" && "$ASSUME_YES" -eq 0 ]]; then
    echo "⚠ This standby is FROZEN, which usually means you paused it to preserve"
    echo "  pre-incident data. Removing it DISCARDS that held state."
    echo "  (The cloud archive can still recover it, but not instantly.)"
    read -r -p "  Really remove it? [type REMOVE] " c
    [[ "$c" == "REMOVE" ]] || { echo "aborted"; exit 1; }
  fi
  docker rm -f "$CONTAINER" >/dev/null
  log "removed container $CONTAINER"
else
  log "no standby container found"
fi

rm -rf "$DATA_DIR"
rm -f "$STATE_DIR/standby_delay" "$STATE_DIR/standby_container"
log "delayed standby torn down"
