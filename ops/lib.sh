#!/usr/bin/env bash
# Shared helpers for the Telemon backup scripts.
# Source this, don't execute it.

set -euo pipefail

OPS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$OPS_DIR")"

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
# TELEMON_BACKUP_ENV lets the self-test run against an isolated config (local
# filesystem remote, scratch STATE_DIR) without touching the real ops/backup.env
# or the production archive.
BACKUP_ENV_FILE="${TELEMON_BACKUP_ENV:-$OPS_DIR/backup.env}"
if [[ -f "$BACKUP_ENV_FILE" ]]; then
  # shellcheck disable=SC1091
  source "$BACKUP_ENV_FILE"
else
  echo "FATAL: $BACKUP_ENV_FILE not found. Copy backup.env.example and fill it in." >&2
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

# Which remote RESTORES read from. Writes fan out to every entry in
# BACKUP_REMOTES; reads must be deliberate about provider, because egress
# pricing differs enormously:
#
#   B2 free tier  — 1 GB/day download cap. Exceeding it returns 403, which
#                   rclone surfaces as "object not found" and exits 0. A
#                   restore then silently downloads nothing.
#   R2            — zero egress, no cap. Drills and restores are free.
#
# So RESTORE_REMOTE defaults to R2 if present, else the first configured
# remote. Every restore path calls this instead of picking entry #1 blindly.
resolve_restore_remote() {
  if [[ -n "${RESTORE_REMOTE:-}" ]]; then
    echo "$RESTORE_REMOTE"; return
  fi
  for R in $BACKUP_REMOTES; do
    case "$R" in r2:*|*r2*) echo "$R"; return ;; esac
  done
  echo "$BACKUP_REMOTES" | awk '{print $1}'
}

# Detect the B2 cap masquerading as a missing object. Without this, an operator
# mid-incident sees "no base backups found" and concludes their archive is gone.
warn_if_download_capped() {
  local remote="$1"
  case "$remote" in
    b2:*|*b2*) log "note: reading from a B2 remote. The free tier caps downloads at 1 GB/day and reports the cap as 'not found'. If this restore finds nothing, check your B2 caps page before assuming the archive is empty." ;;
  esac
}

# Upload a local dir to every configured remote under a prefix.
# Uses `copy` only — never `sync`, which would propagate local deletions.
push_to_all_remotes() {
  local src="$1" prefix="$2" ok=1
  for R in $BACKUP_REMOTES; do
    log "pushing $src -> ${R}/${prefix}"
    # B2 only: rclone HEADs the destination before copying, and a HEAD counts
    # as a Class B (download) transaction. On B2's free tier the daily download
    # quota is 1 GB, so once anything has read that much every subsequent
    # UPLOAD fails with "403 failed to HEAD for download" — archiving stops for
    # a reason that looks nothing like a quota. Skipping the check is also
    # semantically right: every artifact name carries a timestamp or LSN, so it
    # is written exactly once.
    #
    # NOT applied to R2/S3: there it suppresses the multipart-upload path and
    # produces "501 NotImplemented" on large objects.
    local extra=()
    case "$R" in b2:*|*b2*) extra=(--no-check-dest) ;; esac
    if ! rc copy "$src" "${R}/${prefix}" \
          "${extra[@]}" \
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
pg_running() { timeout 10 docker inspect -f '{{.State.Running}}' "$PG_CONTAINER" 2>/dev/null | grep -q true; }

# No `-i`: it is only needed when piping stdin (pg_restore), and for a `-c`
# query it holds the stream open for no reason. Combined with </dev/null and a
# hard timeout so a wedged container can never hang a health check.
psql_q() {
  timeout "${PSQL_TIMEOUT:-20}" docker exec "$PG_CONTAINER" \
    psql -U "$PG_USER" -d "$PG_DB" -Atq -c "$1" </dev/null
}

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
