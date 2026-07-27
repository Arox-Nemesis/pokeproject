#!/usr/bin/env bash
# Shared helpers for the Telemon backup scripts.
# Source this, don't execute it.

set -euo pipefail

OPS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$OPS_DIR")"

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
if [[ -f "$OPS_DIR/backup.env" ]]; then
  # shellcheck disable=SC1091
  source "$OPS_DIR/backup.env"
else
  echo "FATAL: $OPS_DIR/backup.env not found. Copy backup.env.example and fill it in." >&2
  exit 1
fi

# shellcheck disable=SC1091
source "$OPS_DIR/tables.sh"

PG_CONTAINER="${PG_CONTAINER:-telemon_postgres}"
PG_USER="${PG_USER:-telemon}"
PG_DB="${PG_DB:-telemon}"
BACKUP_REMOTES="${BACKUP_REMOTES:?BACKUP_REMOTES must be set in backup.env}"
STATE_DIR="${STATE_DIR:-/var/lib/telemon-backup}"
RCLONE_CONF="${RCLONE_CONF:-$OPS_DIR/rclone.conf}"

mkdir -p "$STATE_DIR"

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
_ts() { date -u +%Y-%m-%dT%H:%M:%SZ; }
log()  { echo "[$(_ts)] [${SCRIPT_NAME:-backup}] $*"; }
die()  { log "FATAL: $*"; alert "🚨 Telemon backup FAILED (${SCRIPT_NAME:-backup}): $*"; exit 1; }

stamp() { date -u +%Y%m%dT%H%M%SZ; }

# ---------------------------------------------------------------------------
# Telegram alerting
# ---------------------------------------------------------------------------
alert() {
  local msg="$*"
  log "ALERT: $msg"
  [[ -z "${BOT_TOKEN:-}" || -z "${ALERT_CHAT_ID:-}" ]] && return 0
  curl -sS --max-time 15 \
    -X POST "https://api.telegram.org/bot${BOT_TOKEN}/sendMessage" \
    --data-urlencode "chat_id=${ALERT_CHAT_ID}" \
    --data-urlencode "text=${msg}" >/dev/null 2>&1 || log "warn: telegram alert failed"
}

# ---------------------------------------------------------------------------
# rclone wrappers
# ---------------------------------------------------------------------------
rc() { rclone --config "$RCLONE_CONF" "$@"; }

# Upload a local dir to every configured remote under a prefix.
# Uses `copy` only — never `sync`, which would propagate local deletions.
push_to_all_remotes() {
  local src="$1" prefix="$2" ok=1
  for R in $BACKUP_REMOTES; do
    log "pushing $src -> ${R}/${prefix}"
    if ! rc copy "$src" "${R}/${prefix}" \
          --transfers 4 --checkers 8 --no-traverse \
          --retries 3 --low-level-retries 5 --log-level NOTICE; then
      log "ERROR: push to ${R} failed"
      ok=0
    fi
  done
  [[ "$ok" -eq 1 ]] || return 1
}

# ---------------------------------------------------------------------------
# Postgres helpers
# ---------------------------------------------------------------------------
pg_running() { docker inspect -f '{{.State.Running}}' "$PG_CONTAINER" 2>/dev/null | grep -q true; }

psql_q() { docker exec -i "$PG_CONTAINER" psql -U "$PG_USER" -d "$PG_DB" -Atq -c "$1"; }

require_pg() {
  pg_running || die "container $PG_CONTAINER is not running"
  psql_q "SELECT 1" >/dev/null || die "cannot query database $PG_DB"
}

# Guard against concurrent runs of the same script.
acquire_lock() {
  local name="${1:-${SCRIPT_NAME:-backup}}"
  exec 9>"$STATE_DIR/${name}.lock"
  flock -n 9 || { log "another $name run is in progress; exiting"; exit 0; }
}
