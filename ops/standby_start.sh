#!/usr/bin/env bash
# Time-delayed standby — a live, queryable undo button with a configurable fuse.
#
# WHAT THIS IS
#
# A second Postgres that continuously replays the WAL archive but *deliberately
# stays N hours behind* (recovery_min_apply_delay). It receives the 07:31
# TRUNCATE immediately and then refuses to apply it until 08:31.
#
# So at 07:32, when you notice, you have a fully-indexed, instantly-queryable
# database holding your 06:31 data — and a whole hour to freeze it before the
# damage lands. No download, no base-backup extraction, no waiting.
#
# WHY IT REPLAYS THE ARCHIVE, NOT A REPLICATION SLOT
#
# A streaming standby needs a replication slot on the primary. If the standby
# dies, that slot retains WAL on the PRIMARY forever and pg_wal grows until the
# disk fills — the backup system taking down the thing it was protecting. By
# replaying the local WAL retention tier instead, this standby is completely
# passive: the primary does not know or care that it exists, and it cannot
# affect production no matter how badly it breaks.
#
#   ops/standby_start.sh                 # start it (delay from backup.env)
#   ops/standby_start.sh --delay 2h
#   ops/standby_status.sh                # how far behind is it?
#   ops/standby_freeze.sh                # STOP replay — do this during an incident
#   ops/standby_stop.sh

SCRIPT_NAME=standby_start
# shellcheck disable=SC1091
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

DELAY="${STANDBY_DELAY:-1h}"
PORT="${STANDBY_PORT:-5495}"
CONTAINER="${STANDBY_CONTAINER:-telemon_standby}"
DATA_DIR="${STANDBY_DATA_DIR:-/var/lib/telemon-standby}"
REMOTE=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --delay)     DELAY="$2"; shift 2 ;;
    --port)      PORT="$2"; shift 2 ;;
    --remote)    REMOTE="$2"; shift 2 ;;
    --data-dir)  DATA_DIR="$2"; shift 2 ;;
    *) die "unknown argument: $1" ;;
  esac
done

require_pg
# Reads come from the uncapped remote (see resolve_restore_remote in lib.sh):
# defaulting to BACKUP_REMOTES[0] would read from B2, whose free tier caps
# downloads at 1 GB/day and reports the cap as "object not found".
[[ -n "$REMOTE" ]] || REMOTE="$(resolve_restore_remote)"
warn_if_download_capped "$REMOTE"

# Retention must outlast the delay, or the standby will ask for a segment that
# has already been pruned and recovery will stall. Let Postgres parse the
# interval — it validates the syntax and handles '90min'/'1h30m' correctly,
# with no dependency on bc being installed.
RETAIN_HOURS="${LOCAL_WAL_RETAIN_HOURS:-30}"
DELAY_SECONDS="$(psql_q "SELECT EXTRACT(EPOCH FROM '${DELAY}'::interval)::bigint;" 2>/dev/null || echo "")"
[[ -n "$DELAY_SECONDS" ]] || die "STANDBY_DELAY '$DELAY' is not a valid Postgres interval (try '1h', '90min', '2h30min')"
RETAIN_SECONDS=$(( RETAIN_HOURS * 3600 ))
if [[ "$DELAY_SECONDS" -ge "$RETAIN_SECONDS" ]]; then
  die "STANDBY_DELAY ($DELAY = ${DELAY_SECONDS}s) must be comfortably less than LOCAL_WAL_RETAIN_HOURS (${RETAIN_HOURS}h = ${RETAIN_SECONDS}s), or the standby will stall on pruned segments"
fi
log "delay ${DELAY} (${DELAY_SECONDS}s) fits inside ${RETAIN_HOURS}h of local WAL retention"

# Segments live in /wal-retain only AFTER the uploader has moved them there.
# Until it runs, everything sits in /wal-spool. Depending on retain alone means
# the standby silently never reaches consistency — it restores the base backup,
# finds no WAL, and hangs in recovery forever.
# `grep | wc -l`, never `grep -c`: grep -c exits 1 on a zero count, so the
# `|| echo 0` fallback APPENDS a second value and the arithmetic below dies with
# "syntax error in expression". wc -l always exits 0.
RETAIN_N="$(docker run --rm -v telemon_wal_retain:/r:ro postgres:16-alpine \
             sh -c 'ls -1 /r 2>/dev/null | grep "\.gz$" | wc -l' 2>/dev/null | tr -d ' \n')"
SPOOL_N="$(docker exec "$PG_CONTAINER" sh -c 'ls -1 /wal-spool 2>/dev/null | grep "\.gz$" | wc -l' 2>/dev/null | tr -d ' \n')"
RETAIN_N="${RETAIN_N:-0}"; SPOOL_N="${SPOOL_N:-0}"
log "WAL available: retain=${RETAIN_N} spool=${SPOOL_N}"
if [[ $(( RETAIN_N + SPOOL_N )) -eq 0 ]]; then
  die "no WAL segments in either /wal-retain or /wal-spool. A standby cannot reach consistency without WAL to replay. Start the uploader (docker compose up -d wal-uploader) or wait for archive_timeout to produce segments."
fi

if docker inspect -f '{{.State.Running}}' "$CONTAINER" >/dev/null 2>&1; then
  log "standby container already exists; removing it to rebuild"
  docker rm -f "$CONTAINER" >/dev/null
fi

# ---------------------------------------------------------------------------
# Base backup to build the standby from
# ---------------------------------------------------------------------------
rm -rf "$DATA_DIR"
mkdir -p "$DATA_DIR/pgdata"

log "fetching the most recent base backup"
LATEST="$(rc lsf "${REMOTE}/base" --include 'base-*.tar.gz' 2>/dev/null | sort | tail -1)"
[[ -n "$LATEST" ]] || die "no base backup found at ${REMOTE}/base — run ops/basebackup.sh first"
log "using $LATEST"

rc copyto "${REMOTE}/base/${LATEST}" "$DATA_DIR/${LATEST}" || die "could not download the base backup"
tar -xzf "$DATA_DIR/${LATEST}" -C "$DATA_DIR/pgdata" || die "extract failed"
rm -f "$DATA_DIR/${LATEST}"

chmod 700 "$DATA_DIR/pgdata"
chown -R 70:70 "$DATA_DIR/pgdata" 2>/dev/null || log "warn: chown to uid 70 failed"

# ---------------------------------------------------------------------------
# Standby configuration
# ---------------------------------------------------------------------------
# standby.signal (NOT recovery.signal) = stay in recovery forever rather than
# promoting when the archive runs dry. recovery_min_apply_delay is the fuse.
#
# restore_command checks retention FIRST, then falls back to the spool. The
# uploader moves segments retain-ward over time, so at any moment the one we
# need may be in either place. Trying both makes the standby independent of
# whether the uploader is running.
cat >> "$DATA_DIR/pgdata/postgresql.auto.conf" <<EOF
restore_command = 'gunzip -c /wal-retain/%f.gz > %p 2>/dev/null || gunzip -c /wal-spool/%f.gz > %p'
recovery_min_apply_delay = '${DELAY}'
hot_standby = on
archive_mode = off
EOF
touch "$DATA_DIR/pgdata/standby.signal"
chown 70:70 "$DATA_DIR/pgdata/postgresql.auto.conf" "$DATA_DIR/pgdata/standby.signal" 2>/dev/null || true

# ---------------------------------------------------------------------------
# Launch
# ---------------------------------------------------------------------------
log "starting delayed standby (delay=${DELAY}, port=${PORT})"
docker run -d --name "$CONTAINER" \
  --restart unless-stopped \
  -v "$DATA_DIR/pgdata:/var/lib/postgresql/data" \
  -v telemon_wal_retain:/wal-retain:ro \
  -v telemon_wal_spool:/wal-spool:ro \
  -e POSTGRES_USER="$PG_USER" \
  -e POSTGRES_PASSWORD=standby \
  -p "${PORT}:5432" \
  postgres:16-alpine >/dev/null || die "could not start the standby container"

# Recovery on a 570MB+ base backup plus thousands of segments takes minutes,
# not seconds. The old 180s ceiling was tuned for a 12MB database.
log "waiting for recovery to reach a consistent state (can take several minutes)..."
UP=0
for i in $(seq 1 150); do
  if docker exec "$CONTAINER" pg_isready -U "$PG_USER" -d "$PG_DB" >/dev/null 2>&1; then UP=1; break; fi
  if [[ $(( i % 15 )) -eq 0 ]]; then
    log "  still recovering (${i}0s)... last log: $(docker logs --tail 1 "$CONTAINER" 2>&1 | tr -d '\n' | cut -c1-90)"
  fi
  sleep 4
done

if [[ "$UP" -eq 0 ]]; then
  echo
  echo "standby did not reach a consistent state. Recent logs:"
  docker logs --tail 25 "$CONTAINER" 2>&1 | sed 's/^/  /'
  echo
  echo "Most common cause: the WAL segment the base backup needs is not present"
  echo "in /wal-retain or /wal-spool. Check that the segment named in the logs"
  echo "above exists, and that LOCAL_WAL_RETAIN_HOURS is long enough."
  die "standby did not come up"
fi

echo "$DELAY"     > "$STATE_DIR/standby_delay"
echo "$CONTAINER" > "$STATE_DIR/standby_container"

echo
echo "════════════════════════════════════════════════════════════"
echo " ✅ DELAYED STANDBY RUNNING"
echo "════════════════════════════════════════════════════════════"
echo " delay      : $DELAY behind the primary"
echo " port       : $PORT"
echo " connect    : psql 'postgresql://${PG_USER}:standby@localhost:${PORT}/${PG_DB}'"
echo ""
echo " DURING AN INCIDENT — freeze it before the damage replays:"
echo "   ops/standby_freeze.sh"
echo ""
echo " This standby is passive: it holds no replication slot on the primary"
echo " and cannot affect production however badly it fails."
echo "════════════════════════════════════════════════════════════"

alert "🕐 Telemon delayed standby started — holding data from ${DELAY} ago on port ${PORT}. Freeze with ops/standby_freeze.sh during an incident."
