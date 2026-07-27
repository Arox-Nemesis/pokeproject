#!/usr/bin/env bash
# Point-in-time recovery.
#
# This is the script that answers "the database got wiped at 7:31, give me
# 7:30 back". It restores into a SCRATCH container on a SCRATCH port. It never
# touches the live volume — you verify the result, then swap deliberately.
#
#   ops/restore_pitr.sh --target-time "2026-07-26 07:30:59+00"
#   ops/restore_pitr.sh --target-time latest --port 5499
#
# When it finishes you get a running Postgres you can psql into. Check it,
# then follow the printed swap instructions.

SCRIPT_NAME=restore_pitr
# shellcheck disable=SC1091
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

TARGET_TIME=""
RESTORE_DIR=""
PORT=5499
REMOTE=""
KEEP=1
CONTAINER="telemon_restore"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --target-time) TARGET_TIME="$2"; shift 2 ;;
    --dir)         RESTORE_DIR="$2"; shift 2 ;;
    --port)        PORT="$2"; shift 2 ;;
    --remote)      REMOTE="$2"; shift 2 ;;
    --container)   CONTAINER="$2"; shift 2 ;;
    --teardown)    KEEP=0; shift ;;
    *) die "unknown argument: $1" ;;
  esac
done

[[ -n "$TARGET_TIME" ]] || die "--target-time is required (an ISO timestamp, or 'latest')"

# Default to the first configured remote.
[[ -n "$REMOTE" ]] || REMOTE="$(echo "$BACKUP_REMOTES" | awk '{print $1}')"
[[ -n "$RESTORE_DIR" ]] || RESTORE_DIR="$(mktemp -d /tmp/telemon-restore-XXXXXX)"

PGDATA="$RESTORE_DIR/pgdata"
WALDIR="$RESTORE_DIR/wal"
mkdir -p "$PGDATA" "$WALDIR"

log "restore target : $TARGET_TIME"
log "source remote  : $REMOTE"
log "scratch dir    : $RESTORE_DIR"
log "scratch port   : $PORT"

# ---------------------------------------------------------------------------
# 1. Pick a base backup taken BEFORE the target time
# ---------------------------------------------------------------------------
log "listing base backups..."
mapfile -t BASES < <(rc lsf "${REMOTE}/base" --include "base-*.tar.gz" | sort)
[[ ${#BASES[@]} -gt 0 ]] || die "no base backups found at ${REMOTE}/base"

CHOSEN=""
if [[ "$TARGET_TIME" == "latest" ]]; then
  CHOSEN="${BASES[-1]}"
else
  # base-20260726T040000Z.tar.gz -> 20260726T040000Z -> epoch
  TARGET_EPOCH="$(date -u -d "$TARGET_TIME" +%s)" || die "could not parse --target-time '$TARGET_TIME'"
  for b in "${BASES[@]}"; do
    raw="${b#base-}"; raw="${raw%.tar.gz}"
    iso="${raw:0:4}-${raw:4:2}-${raw:6:2} ${raw:9:2}:${raw:11:2}:${raw:13:2}+00"
    be="$(date -u -d "$iso" +%s 2>/dev/null)" || continue
    [[ "$be" -le "$TARGET_EPOCH" ]] && CHOSEN="$b"
  done
  [[ -n "$CHOSEN" ]] || die "no base backup predates $TARGET_TIME — the earliest is ${BASES[0]}. You cannot recover to a point before your first base backup."
fi

log "using base backup: $CHOSEN"

# ---------------------------------------------------------------------------
# 2. Download base + WAL
# ---------------------------------------------------------------------------
log "downloading base backup..."
rc copy "${REMOTE}/base/${CHOSEN}" "$RESTORE_DIR/" --progress || die "base download failed"

log "downloading WAL segments (this is the bulk of the transfer)..."
rc copy "${REMOTE}/wal" "$WALDIR" --transfers 8 --checkers 16 --progress \
  || die "WAL download failed"

WAL_COUNT="$(find "$WALDIR" -type f -name '*.gz' | wc -l)"
log "fetched $WAL_COUNT WAL segments"
[[ "$WAL_COUNT" -gt 0 ]] || die "no WAL segments downloaded — cannot replay to a point in time"

# ---------------------------------------------------------------------------
# 3. Extract
# ---------------------------------------------------------------------------
log "extracting base backup..."
tar -xzf "$RESTORE_DIR/${CHOSEN}" -C "$PGDATA" || die "extract failed"

# Postgres refuses to start on a data directory it doesn't own with 0700.
chmod 700 "$PGDATA"
chown -R 70:70 "$PGDATA" 2>/dev/null || log "warn: chown to uid 70 failed (may be fine if rootless)"

# ---------------------------------------------------------------------------
# 4. Recovery configuration
# ---------------------------------------------------------------------------
# restore_command gunzips out of the mounted WAL dir. recovery_target_time
# tells Postgres to replay UP TO that moment and then stop — which is exactly
# how you land on 07:30:59 and leave the 07:31 wipe unapplied.
{
  echo "restore_command = 'gunzip -c /wal-restore/%f.gz > %p'"
  if [[ "$TARGET_TIME" != "latest" ]]; then
    echo "recovery_target_time = '${TARGET_TIME}'"
    echo "recovery_target_action = 'promote'"
    # Stop at the first restore point at/after the target rather than
    # overshooting into WAL we explicitly do not want applied.
    echo "recovery_target_inclusive = false"
  fi
} >> "$PGDATA/postgresql.auto.conf"

# Presence of this file is what puts Postgres into archive recovery mode.
touch "$PGDATA/recovery.signal"
chown 70:70 "$PGDATA/postgresql.auto.conf" "$PGDATA/recovery.signal" 2>/dev/null || true

# ---------------------------------------------------------------------------
# 5. Start the scratch instance
# ---------------------------------------------------------------------------
docker rm -f "$CONTAINER" >/dev/null 2>&1 || true

log "starting scratch Postgres on port $PORT..."
docker run -d --name "$CONTAINER" \
  -v "$PGDATA:/var/lib/postgresql/data" \
  -v "$WALDIR:/wal-restore:ro" \
  -e POSTGRES_USER="$PG_USER" \
  -e POSTGRES_PASSWORD=restore \
  -p "${PORT}:5432" \
  postgres:16-alpine \
  postgres -c archive_mode=off >/dev/null || die "could not start restore container"

log "waiting for recovery to complete..."
for _ in $(seq 1 180); do
  if docker exec "$CONTAINER" pg_isready -U "$PG_USER" -d "$PG_DB" >/dev/null 2>&1; then
    if ! docker exec "$CONTAINER" test -f /var/lib/postgresql/data/recovery.signal 2>/dev/null; then
      break
    fi
  fi
  sleep 2
done

docker exec "$CONTAINER" pg_isready -U "$PG_USER" -d "$PG_DB" >/dev/null 2>&1 \
  || { docker logs --tail 50 "$CONTAINER"; die "recovery did not complete — logs above"; }

# ---------------------------------------------------------------------------
# 6. Report
# ---------------------------------------------------------------------------
q() { docker exec "$CONTAINER" psql -U "$PG_USER" -d "$PG_DB" -Atq -c "$1" 2>/dev/null || echo "?"; }

echo
echo "════════════════════════════════════════════════════════════════"
echo " RESTORE COMPLETE — recovered to: $TARGET_TIME"
echo "════════════════════════════════════════════════════════════════"
printf ' %-22s %s\n' "alembic_version" "$(q 'SELECT version_num FROM alembic_version LIMIT 1;')"
for t in "${GUARDED_TABLES[@]}"; do
  printf ' %-22s %s\n' "$t" "$(q "SELECT count(*) FROM public.${t};")"
done
echo "────────────────────────────────────────────────────────────────"
echo " Inspect it:"
echo "   psql 'postgresql://${PG_USER}:restore@localhost:${PORT}/${PG_DB}'"
echo
echo " Compare against live before you swap:"
echo "   docker exec ${PG_CONTAINER} psql -U ${PG_USER} -d ${PG_DB} -c 'SELECT count(*) FROM users;'"
echo
echo " If it looks right, promote it (THIS REPLACES LIVE DATA):"
echo "   docker compose stop bot"
echo "   ops/restore_logical.sh --from-container ${CONTAINER}"
echo "   docker compose start bot"
echo
echo " Scratch instance left running. Tear down with:"
echo "   docker rm -f ${CONTAINER} && rm -rf ${RESTORE_DIR}"
echo "════════════════════════════════════════════════════════════════"

if [[ "$KEEP" -eq 0 ]]; then
  docker rm -f "$CONTAINER" >/dev/null
  rm -rf "$RESTORE_DIR"
  log "torn down"
fi
