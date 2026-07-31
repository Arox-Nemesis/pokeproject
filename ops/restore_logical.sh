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
FULL=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    --dump)           DUMP="$2"; shift 2 ;;
    --from-container) FROM_CONTAINER="$2"; shift 2 ;;
    --tables)         TABLES="$2"; shift 2 ;;
    --remote)         REMOTE="$2"; shift 2 ;;
    --full)           FULL=1; shift ;;
    --yes)            ASSUME_YES=1; shift ;;
    *) die "unknown argument: $1" ;;
  esac
done

[[ -n "$DUMP" || -n "$FROM_CONTAINER" ]] || die "need --dump or --from-container"
# Reads come from the uncapped remote (see resolve_restore_remote in lib.sh):
# defaulting to BACKUP_REMOTES[0] would read from B2, whose free tier caps
# downloads at 1 GB/day and reports the cap as "object not found".
[[ -n "$REMOTE" ]] || REMOTE="$(resolve_restore_remote)"
warn_if_download_capped "$REMOTE"

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
  ARGS=()
  if [[ "$FULL" -eq 1 ]]; then
    # Whole database: schema, reference tables, everything. Required when the
    # target is EMPTY (a fresh server), where a user-tables-only restore would
    # leave no schema and no pokemon_species/moves/learnsets — the bot would
    # start against a database it cannot query.
    log "dumping the ENTIRE database from $FROM_CONTAINER (full mode)"
  elif [[ -n "$TABLES" ]]; then
    IFS=',' read -ra TS <<< "$TABLES"
    for t in "${TS[@]}"; do ARGS+=(--table="public.$t"); done
    log "dumping ${#TS[@]} requested table(s) from $FROM_CONTAINER"
  else
    for t in "${USER_TABLES[@]}"; do ARGS+=(--table="public.$t"); done
    log "dumping ${#USER_TABLES[@]} user tables from $FROM_CONTAINER"
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
if [[ "$FULL" -eq 1 ]]; then
  # Whole-database restore. `--clean` alone is not enough here: it emits
  # per-object DROPs, and dropping pk_users fails because active_spawns holds
  # an FK referencing that index — and active_spawns is NOT in the dump (it is
  # transient state), so nothing ever drops the dependent constraint.
  #
  # Recreating the schema wholesale sidesteps the dependency ordering problem
  # entirely and guarantees the result matches the source exactly.
  log "full restore: recreating the public schema"
  psql_q "DROP SCHEMA public CASCADE; CREATE SCHEMA public;
          GRANT ALL ON SCHEMA public TO ${PG_USER}; GRANT ALL ON SCHEMA public TO public;" >/dev/null \
    || die "could not recreate the public schema"
  RARGS=(--no-owner --no-privileges --single-transaction)
else
  # Table-subset restore. --clean --if-exists drops and recreates just those
  # tables; --disable-triggers defers FK checks so cross-table references to
  # tables not in this dump do not block the drop/reload.
  RARGS=(--no-owner --no-privileges --clean --if-exists --single-transaction)
  if [[ -n "$TABLES" ]]; then
    IFS=',' read -ra TS <<< "$TABLES"
    for t in "${TS[@]}"; do RARGS+=(--table="$t"); done
  fi
fi

log "restoring..."
if docker exec -i "$PG_CONTAINER" pg_restore -U "$PG_USER" -d "$PG_DB" \
     "${RARGS[@]}" < "$LOCAL_DUMP"; then
  log "restore completed"
else
  if [[ "$FULL" -eq 1 ]]; then
    die "pg_restore failed AFTER the schema was dropped — the database is now EMPTY. Restore immediately from the rollback snapshot: ops/restore_logical.sh --dump $PRE --full"
  fi
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
