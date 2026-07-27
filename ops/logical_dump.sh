#!/usr/bin/env bash
# Logical dumps — the version-independent escape hatch.
#
# Physical base backups only restore into the same Postgres major version.
# Logical dumps survive major-version upgrades, allow single-table restores
# ("just give me back the users table"), and can be inspected without
# starting a server.
#
# Two modes:
#   user   — irreplaceable tables only. Small, fast, run every 15 minutes.
#   full   — everything including regenerable reference data. Every 6 hours.
#
# The anomaly gate runs on `user` dumps. A tripped gate does NOT suppress the
# upload — it redirects to quarantine/ and freezes retention pruning. A guard
# that refuses to back up is a guard that creates gaps.

SCRIPT_NAME=logical_dump
# shellcheck disable=SC1091
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

MODE="${1:-user}"
[[ "$MODE" == "user" || "$MODE" == "full" ]] || die "usage: $0 [user|full]"

acquire_lock "logical_dump_$MODE"
require_pg

STAMP="$(stamp)"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

DEST="$MODE"

# ---------------------------------------------------------------------------
# Build the dump
# ---------------------------------------------------------------------------
DUMP="$WORK/${MODE}-${STAMP}.pgc"

if [[ "$MODE" == "user" ]]; then
  ARGS=()
  for t in "${USER_TABLES[@]}"; do ARGS+=(--table="public.$t"); done
  log "dumping ${#USER_TABLES[@]} user tables"
  docker exec "$PG_CONTAINER" pg_dump -U "$PG_USER" -d "$PG_DB" \
    -Fc -Z6 --no-owner --no-privileges "${ARGS[@]}" \
    > "$DUMP" || die "pg_dump (user) failed"
else
  log "dumping entire database"
  docker exec "$PG_CONTAINER" pg_dump -U "$PG_USER" -d "$PG_DB" \
    -Fc -Z6 --no-owner --no-privileges \
    > "$DUMP" || die "pg_dump (full) failed"
fi

SIZE="$(stat -c %s "$DUMP")"
[[ "$SIZE" -gt 512 ]] || die "dump is only ${SIZE} bytes — refusing to upload a truncated dump"

# pg_restore -l is a real structural parse. If it succeeds the archive is
# readable; if it fails we caught a corrupt dump before it reached the bucket.
docker exec -i "$PG_CONTAINER" pg_restore -l < "$DUMP" > "$WORK/${MODE}-${STAMP}.toc" \
  || die "dump ${STAMP} failed pg_restore -l validation — it is not a readable archive"

# ---------------------------------------------------------------------------
# Census: exact row counts + watermarks for the anomaly gate
# ---------------------------------------------------------------------------
# Exact COUNT(*), not pg_stat_user_tables.n_live_tup — that's a stale
# estimate and will produce both false alarms and missed incidents.
CENSUS="$WORK/census-${STAMP}.json"
{
  echo "{"
  echo "  \"stamp\": \"${STAMP}\","
  echo "  \"mode\": \"${MODE}\","
  echo "  \"tables\": {"
  first=1
  for t in "${USER_TABLES[@]}"; do
    n="$(psql_q "SELECT count(*) FROM public.${t};" 2>/dev/null || echo -1)"
    [[ $first -eq 0 ]] && echo ","
    printf '    "%s": %s' "$t" "$n"
    first=0
  done
  echo ""
  echo "  }"
  echo "}"
} > "$CENSUS"

# ---------------------------------------------------------------------------
# Anomaly gate (user dumps only)
# ---------------------------------------------------------------------------
GATE_TRIPPED=0
if [[ "$MODE" == "user" ]]; then
  LAST_GOOD="$STATE_DIR/last_good_census.json"
  if [[ -f "$LAST_GOOD" ]]; then
    if ! python3 "$OPS_DIR/check_anomaly.py" \
           --previous "$LAST_GOOD" \
           --current "$CENSUS" \
           --guarded "$(IFS=,; echo "${GUARDED_TABLES[*]}")" \
           --shrink-threshold "${ANOMALY_SHRINK_THRESHOLD:-0.10}" \
           --absolute-drop "${ANOMALY_ABSOLUTE_DROP:-25}" \
           --report "$WORK/anomaly-${STAMP}.txt"; then
      GATE_TRIPPED=1
      DEST="quarantine"
      touch "$STATE_DIR/PRUNING_HALTED"
      alert "$(printf '🚨 TELEMON BACKUP ANOMALY at %s\n\n%s\n\nThe dump was uploaded to quarantine/ (NOT deleted). Retention pruning is HALTED so nothing ages out while you investigate.\n\nIf this was an intentional wipe, run: ops/clear_anomaly.sh' \
        "$STAMP" "$(cat "$WORK/anomaly-${STAMP}.txt")")"
    fi
  else
    log "no previous census — establishing baseline"
  fi
fi

# ---------------------------------------------------------------------------
# Upload. Always. Only the destination prefix changes.
# ---------------------------------------------------------------------------
push_to_all_remotes "$WORK" "$DEST" \
  || die "dump $STAMP produced but FAILED to upload to one or more remotes"

if [[ "$GATE_TRIPPED" -eq 0 && "$MODE" == "user" ]]; then
  cp "$CENSUS" "$STATE_DIR/last_good_census.json"
  echo "$STAMP" > "$STATE_DIR/last_good_dump"
fi

log "done: ${MODE}-${STAMP}.pgc -> ${DEST}/ ($((SIZE / 1024)) KB)"
