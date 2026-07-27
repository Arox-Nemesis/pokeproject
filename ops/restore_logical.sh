#!/usr/bin/env bash
# Logical restore — put data back into the LIVE database.
#
# Two sources:
#   --dump <file|remote-name>    restore from a pg_dump custom archive
#   --from-container <name>      pull straight out of a verified scratch
#                                instance produced by restore_pitr.sh
#
# Optionally limit to specific tables:
#   ops/restore_logical.sh --dump user-20260726T073000Z.pgc --tables users,pokemon
#
# THIS WRITES TO THE LIVE DATABASE. It refuses to run while the bot container
# is up, because restoring under a live writer produces a torn result.

SCRIPT_NAME=restore_logical
# shellcheck disable=SC1091
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

DUMP=""
FROM_CONTAINER=""
TABLES=""
REMOTE=""
ASSUME_YES=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    --dump)           DUMP="$2"; shift 2 ;;
    --from-container) FROM_CONTAINER="$2"; shift 2 ;;
    --tables)         TABLES="$2"; shift 2 ;;
    --remote)         REMOTE="$2"; shift 2 ;;
    --yes)            ASSUME_YES=1; shift ;;
    *) die "unknown argument: $1" ;;
  esac
done

[[ -n "$DUMP" || -n "$FROM_CONTAINER" ]] || die "need --dump or --from-container"
[[ -n "$REMOTE" ]] || REMOTE="$(echo "$BACKUP_REMOTES" | awk '{print $1}')"

require_pg

# ---------------------------------------------------------------------------
# Refuse to restore under a live writer
# ---------------------------------------------------------------------------
if docker inspect -f '{{.State.Running}}' telemon_bot 2>/dev/null | grep -q true; then
  die "telemon_bot is running. Stop it first (docker compose stop bot) — restoring while the bot writes produces a torn, inconsistent result."
fi

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

# ---------------------------------------------------------------------------
# Obtain the dump
# ---------------------------------------------------------------------------
LOCAL_DUMP=""
if [[ -n "$FROM_CONTAINER" ]]; then
  log "dumping from verified scratch container $FROM_CONTAINER"
  ARGS=()
  if [[ -n "$TABLES" ]]; then
    IFS=',' read -ra TS <<< "$TABLES"
    for t in "${TS[@]}"; do ARGS+=(--table="public.$t"); done
  else
    for t in "${USER_TABLES[@]}"; do ARGS+=(--table="public.$t"); done
  fi
  LOCAL_DUMP="$WORK/from-container.pgc"
  docker exec "$FROM_CONTAINER" pg_dump -U "$PG_USER" -d "$PG_DB" \
    -Fc -Z6 --no-owner --no-privileges "${ARGS[@]}" > "$LOCAL_DUMP" \
    || die "could not dump from $FROM_CONTAINER"
elif [[ -f "$DUMP" ]]; then
  LOCAL_DUMP="$DUMP"
  log "using local dump $DUMP"
else
  log "downloading $DUMP from $REMOTE"
  for prefix in user full quarantine; do
    if rc copyto "${REMOTE}/${prefix}/${DUMP}" "$WORK/$DUMP" 2>/dev/null; then
      LOCAL_DUMP="$WORK/$DUMP"
      log "found under ${prefix}/"
      break
    fi
  done
  [[ -n "$LOCAL_DUMP" ]] || die "could not find $DUMP under user/, full/ or quarantine/ on $REMOTE"
fi

# ---------------------------------------------------------------------------
# Safety net: snapshot current state before overwriting it
# ---------------------------------------------------------------------------
PRE="$STATE_DIR/pre-restore-$(stamp).pgc"
log "snapshotting current live state to $PRE (in case this restore is wrong)"
docker exec "$PG_CONTAINER" pg_dump -U "$PG_USER" -d "$PG_DB" -Fc -Z6 \
  --no-owner --no-privileges > "$PRE" || die "pre-restore snapshot failed"

# ---------------------------------------------------------------------------
# Confirm
# ---------------------------------------------------------------------------
echo
echo "About to restore into the LIVE database '$PG_DB'."
echo "  dump         : $(basename "$LOCAL_DUMP") ($(stat -c %s "$LOCAL_DUMP" | numfmt --to=iec))"
echo "  tables       : ${TABLES:-all user tables}"
echo "  current live :"
for t in "${GUARDED_TABLES[@]}"; do
  printf '      %-22s %s\n' "$t" "$(psql_q "SELECT count(*) FROM public.${t};" 2>/dev/null || echo '?')"
done
echo "  rollback     : $PRE"
echo
if [[ "$ASSUME_YES" -eq 0 ]]; then
  read -r -p "Proceed? [type RESTORE] " confirm
  [[ "$confirm" == "RESTORE" ]] || { echo "aborted"; exit 1; }
fi

# ---------------------------------------------------------------------------
# Restore
# ---------------------------------------------------------------------------
# --clean --if-exists drops and recreates the restored tables. Wrapped in a
# single transaction so a mid-restore failure leaves the database untouched
# rather than half-populated.
RARGS=(--no-owner --no-privileges --clean --if-exists --single-transaction)
if [[ -n "$TABLES" ]]; then
  IFS=',' read -ra TS <<< "$TABLES"
  for t in "${TS[@]}"; do RARGS+=(--table="$t"); done
fi

log "restoring..."
if docker exec -i "$PG_CONTAINER" pg_restore -U "$PG_USER" -d "$PG_DB" \
     "${RARGS[@]}" < "$LOCAL_DUMP"; then
  log "restore completed"
else
  die "pg_restore failed. The database was NOT modified (single-transaction). Rollback snapshot is at $PRE"
fi

echo
echo "Post-restore row counts:"
for t in "${GUARDED_TABLES[@]}"; do
  printf '  %-22s %s\n' "$t" "$(psql_q "SELECT count(*) FROM public.${t};" 2>/dev/null || echo '?')"
done
echo
echo "Next steps:"
echo "  1. Verify the data looks right."
echo "  2. docker compose start bot"
echo "  3. ops/clear_anomaly.sh   # re-baseline the gate to the restored counts"
echo
echo "If this restore was wrong, roll back with:"
echo "  ops/restore_logical.sh --dump $PRE"

alert "♻️ Telemon logical restore completed into live DB from $(basename "$LOCAL_DUMP"). Rollback snapshot: $PRE"
