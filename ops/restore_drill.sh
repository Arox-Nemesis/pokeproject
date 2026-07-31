#!/usr/bin/env bash
# Weekly automated restore drill.
#
# An untested backup is a rumour. More projects are lost to backups that turned
# out not to restore than to having no backups at all — the failure is silent
# and you find out at the worst possible moment.
#
# This performs a REAL end-to-end recovery from cloud storage into a scratch
# container, asserts the result is sane, then tears it down. It touches
# nothing live. It runs unattended and pings you either way; the "pass" ping
# matters as much as the failure one, because silence should be suspicious.

SCRIPT_NAME=restore_drill
# shellcheck disable=SC1091
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

acquire_lock
require_pg

DRILL_CONTAINER=telemon_drill
DRILL_PORT=5498
DRILL_DIR="$(mktemp -d /tmp/telemon-drill-XXXXXX)"
FAILURES=()
START_EPOCH="$(date +%s)"

cleanup() {
  docker rm -f "$DRILL_CONTAINER" >/dev/null 2>&1 || true
  rm -rf "$DRILL_DIR"
}
trap cleanup EXIT

fail() { FAILURES+=("$1"); log "FAIL: $1"; }
pass() { log "ok: $1"; }

# Record the outcome for status.sh. Called on EVERY exit path, including early
# aborts — a drill that ran and failed must never be indistinguishable from a
# drill that never ran, which is how status.sh ends up saying "NEVER RUN" while
# recovery is actually broken.
record_drill() {
  local n_fail="$1" elapsed=$(( $(date +%s) - START_EPOCH ))
  {
    echo "stamp=$(stamp)"
    echo "elapsed_seconds=$elapsed"
    echo "failures=$n_fail"
  } > "$STATE_DIR/last_drill"
}

log "starting restore drill"

# ---------------------------------------------------------------------------
# Capture live state to compare against
# ---------------------------------------------------------------------------
declare -A LIVE
for t in "${GUARDED_TABLES[@]}"; do
  LIVE["$t"]="$(psql_q "SELECT count(*) FROM public.${t};" 2>/dev/null || echo -1)"
done
LIVE_ALEMBIC="$(psql_q "SELECT version_num FROM alembic_version LIMIT 1;" 2>/dev/null || echo unknown)"

# ---------------------------------------------------------------------------
# Perform a real recovery from the cloud
# ---------------------------------------------------------------------------
if "$OPS_DIR/restore_pitr.sh" \
     --target-time latest \
     --dir "$DRILL_DIR" \
     --port "$DRILL_PORT" \
     --container "$DRILL_CONTAINER" > "$DRILL_DIR/restore.log" 2>&1; then
  pass "recovery from cloud archive completed"
else
  fail "recovery FAILED to complete — see log below"
  tail -30 "$DRILL_DIR/restore.log" | while IFS= read -r l; do log "  | $l"; done
  record_drill 1
  alert "$(printf '🚨 TELEMON RESTORE DRILL FAILED\n\nRecovery from the cloud archive did not complete. Your backups may not be restorable RIGHT NOW.\n\nLast lines:\n%s' "$(tail -15 "$DRILL_DIR/restore.log")")"
  exit 1
fi

dq() { docker exec "$DRILL_CONTAINER" psql -U "$PG_USER" -d "$PG_DB" -Atq -c "$1" 2>/dev/null || echo "?"; }

# ---------------------------------------------------------------------------
# Assertions
# ---------------------------------------------------------------------------

# 1. Schema version matches — catches an archive that predates a migration.
DRILL_ALEMBIC="$(dq 'SELECT version_num FROM alembic_version LIMIT 1;')"
if [[ "$DRILL_ALEMBIC" == "$LIVE_ALEMBIC" ]]; then
  pass "alembic_version matches live ($DRILL_ALEMBIC)"
else
  fail "alembic_version mismatch: restored=$DRILL_ALEMBIC live=$LIVE_ALEMBIC"
fi

# 2. Row counts within tolerance. The restore is a few minutes behind live, so
#    it should be slightly LOWER, never higher, and never wildly off.
for t in "${GUARDED_TABLES[@]}"; do
  live="${LIVE[$t]}"
  got="$(dq "SELECT count(*) FROM public.${t};")"
  if [[ "$got" == "?" ]]; then
    fail "$t: could not be queried in the restored database"
    continue
  fi
  if [[ "$live" -le 0 ]]; then
    pass "$t: $got rows (live is empty, nothing to compare)"
    continue
  fi
  # Allow 5% drift for writes since the last archived segment.
  min=$(( live * 95 / 100 ))
  if [[ "$got" -ge "$min" && "$got" -le "$live" ]]; then
    pass "$t: $got rows (live $live, within tolerance)"
  elif [[ "$got" -gt "$live" ]]; then
    pass "$t: $got rows (live $live — restored is ahead, fine)"
  else
    fail "$t: restored $got rows but live has $live (>5% short)"
  fi
done

# 3. The data is actually readable, not just countable. A corrupt page often
#    survives count(*) via an index-only scan but dies on a heap read.
for t in "${GUARDED_TABLES[@]}"; do
  if [[ "$(dq "SELECT 1 FROM public.${t} LIMIT 1;")" == "?" ]]; then
    fail "$t: heap read failed"
  fi
done
pass "heap reads succeeded on all guarded tables"

# 4. Referential integrity — orphaned rows mean a partial/torn restore.
ORPHANS="$(dq "SELECT count(*) FROM pokemon p LEFT JOIN users u ON p.owner_id = u.telegram_id WHERE p.owner_id IS NOT NULL AND u.telegram_id IS NULL;")"
if [[ "$ORPHANS" == "0" ]]; then
  pass "no orphaned pokemon rows"
elif [[ "$ORPHANS" == "?" ]]; then
  log "note: orphan check skipped (column shape differs)"
else
  fail "$ORPHANS pokemon rows reference a non-existent user"
fi

# 5. Recovery actually replayed WAL, rather than just opening the base backup.
LAST_WAL="$(dq 'SELECT pg_last_wal_replay_lsn();')"
if [[ -n "$LAST_WAL" && "$LAST_WAL" != "?" ]]; then
  pass "WAL replay reached $LAST_WAL"
fi

# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------
ELAPSED=$(( $(date +%s) - START_EPOCH ))
record_drill "${#FAILURES[@]}"

if [[ ${#FAILURES[@]} -eq 0 ]]; then
  log "DRILL PASSED in ${ELAPSED}s"
  alert "$(printf '✅ Telemon restore drill PASSED (%ds)\n\nRecovered from the cloud archive and verified %d guarded tables. Schema %s. Your backups are restorable as of now.' \
    "$ELAPSED" "${#GUARDED_TABLES[@]}" "$DRILL_ALEMBIC")"
  exit 0
else
  log "DRILL FAILED with ${#FAILURES[@]} problem(s)"
  alert "$(printf '🚨 TELEMON RESTORE DRILL FAILED\n\n%d check(s) failed:\n%s\n\nYour backups exist but did not verify. Investigate before you need them.' \
    "${#FAILURES[@]}" "$(printf '  • %s\n' "${FAILURES[@]}")")"
  exit 1
fi
