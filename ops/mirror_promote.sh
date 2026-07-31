#!/usr/bin/env bash
# Promote the mirror to primary — failover, or moving to a new server.
#
# THE SEQUENCE PROBLEM (read this, it is the thing that silently corrupts data)
#
# Logical replication does NOT replicate sequence values. It copies rows, so
# `teams.id` values arrive intact, but the mirror's `teams_id_seq` is still
# sitting wherever the initial copy left it. Promote without fixing that and
# the very next INSERT calls nextval(), gets a number that is already in use,
# and fails on the primary-key constraint — or worse, succeeds into a gap and
# collides later.
#
# Telemon has 8 sequence-backed columns, including `users.telegram_id` and
# `groups.chat_id` where the sequence is vestigial (the app assigns Telegram
# IDs explicitly) but IS still attached as a column default. So they all get
# advanced. This script derives them from the live catalog rather than a
# hardcoded list, so it stays correct as the schema grows.
#
#   ops/mirror_promote.sh                # interactive, with confirmation
#   ops/mirror_promote.sh --yes          # unattended
#   ops/mirror_promote.sh --keep-primary # do not try to demote the old primary

SCRIPT_NAME=mirror_promote
# shellcheck disable=SC1091
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

TARGET="${MIRROR_URL:-}"
SUBSCRIPTION="${MIRROR_SUBSCRIPTION:-telemon_sub}"
PUBLICATION="${MIRROR_PUBLICATION:-telemon_pub}"
ASSUME_YES=0
KEEP_PRIMARY=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    --yes)           ASSUME_YES=1; shift ;;
    --keep-primary)  KEEP_PRIMARY=1; shift ;;
    *) die "unknown argument: $1" ;;
  esac
done

[[ -n "$TARGET" ]] || die "MIRROR_URL is not set in ops/backup.env"

PRIMARY_UP=0
pg_running && psql_q "SELECT 1" >/dev/null 2>&1 && PRIMARY_UP=1

# No `-i`, stdin closed, bounded — see mirror_setup.sh for why.
psql_target() { timeout "${MIRROR_PSQL_TIMEOUT:-60}" docker exec "$PG_CONTAINER" psql "$TARGET" -Atq -c "$1" </dev/null; }

# We need *a* running postgres container to act as a psql client. If the old
# primary is down, borrow a throwaway one.
CLIENT_TEMP=""
if [[ "$PRIMARY_UP" -eq 0 ]]; then
  log "primary is DOWN — starting a temporary client container to reach the mirror"
  CLIENT_TEMP="telemon_promote_client"
  docker rm -f "$CLIENT_TEMP" >/dev/null 2>&1 || true
  docker run -d --name "$CLIENT_TEMP" --network "${DOCKER_NETWORK:-telemon_default}" \
    -e POSTGRES_PASSWORD=tmp postgres:16-alpine >/dev/null \
    || die "could not start a temporary client container"
  trap 'docker rm -f "$CLIENT_TEMP" >/dev/null 2>&1 || true' EXIT
  sleep 6
  psql_target() { timeout "${MIRROR_PSQL_TIMEOUT:-60}" docker exec "$CLIENT_TEMP" psql "$TARGET" -Atq -c "$1" </dev/null; }
fi

psql_target "SELECT 1" >/dev/null 2>&1 || die "cannot reach the mirror at MIRROR_URL"

# ---------------------------------------------------------------------------
# 1. Stop the writer
# ---------------------------------------------------------------------------
# Honour BOT_CONTAINER rather than hardcoding, so a test harness can point this
# at a non-existent container and never touch production.
BOT_C="${BOT_CONTAINER:-telemon_bot}"
if docker inspect -f '{{.State.Running}}' "$BOT_C" 2>/dev/null | grep -q true; then
  log "stopping the bot ($BOT_C) so nothing writes during promotion"
  docker stop "$BOT_C" >/dev/null
  BOT_WAS_RUNNING=1
else
  BOT_WAS_RUNNING=0
fi

# ---------------------------------------------------------------------------
# 2. Let the mirror drain, if the primary is still alive
# ---------------------------------------------------------------------------
if [[ "$PRIMARY_UP" -eq 1 ]]; then
  log "waiting for the mirror to catch up with the primary..."
  for _ in $(seq 1 60); do
    LAG="$(psql_q "SELECT COALESCE(max(pg_wal_lsn_diff(sent_lsn, replay_lsn)),0)::bigint FROM pg_stat_replication;" 2>/dev/null || echo 0)"
    [[ "${LAG:-0}" -le 0 ]] && break
    sleep 1
  done
  log "residual lag: ${LAG:-unknown} bytes"
  if [[ "${LAG:-0}" -gt 0 ]]; then
    log "WARNING: promoting with ${LAG} bytes not yet replayed — those writes will be LOST"
  fi
else
  log "primary is down; promoting with whatever the mirror already has"
fi

# ---------------------------------------------------------------------------
# 3. Report what we are about to do
# ---------------------------------------------------------------------------
echo
echo "════════════════════════════════════════════════════════════"
echo " PROMOTE MIRROR TO PRIMARY"
echo "════════════════════════════════════════════════════════════"
printf ' %-22s %-12s %s\n' "table" "old primary" "mirror"
for t in "${GUARDED_TABLES[@]}"; do
  p="n/a"; [[ "$PRIMARY_UP" -eq 1 ]] && p="$(psql_q "SELECT count(*) FROM public.$t;" 2>/dev/null || echo '?')"
  m="$(psql_target "SELECT count(*) FROM public.$t;" 2>/dev/null || echo '?')"
  printf ' %-22s %-12s %s\n' "$t" "$p" "$m"
done
echo "────────────────────────────────────────────────────────────"
echo " This will:"
echo "   1. drop subscription '$SUBSCRIPTION' on the mirror (stops replication)"
echo "   2. advance ALL sequences on the mirror past their table maximums"
echo "   3. leave the mirror as a standalone, writable database"
echo ""
echo " Afterwards you must repoint DATABASE_URL at the mirror."
echo "════════════════════════════════════════════════════════════"
echo

if [[ "$ASSUME_YES" -eq 0 ]]; then
  read -r -p "Proceed with promotion? [type PROMOTE] " confirm
  if [[ "$confirm" != "PROMOTE" ]]; then
    echo "aborted"
    [[ "$BOT_WAS_RUNNING" -eq 1 ]] && docker start "$BOT_C" >/dev/null && echo "bot restarted"
    exit 1
  fi
fi

# If we stopped the bot and then fail for ANY reason, bring it back. Leaving
# production down because a promotion aborted midway is worse than the abort.
restore_bot_on_failure() {
  local rc=$?
  if [[ "$rc" -ne 0 && "$BOT_WAS_RUNNING" -eq 1 ]]; then
    echo
    echo "promotion failed — restarting $BOT_C so the bot is not left offline"
    docker start "$BOT_C" >/dev/null 2>&1 || echo "WARNING: could not restart $BOT_C — start it manually"
  fi
}
trap restore_bot_on_failure EXIT

# ---------------------------------------------------------------------------
# 4. Detach the mirror
# ---------------------------------------------------------------------------
log "dropping subscription $SUBSCRIPTION"
# Disable first so a dead upstream can't block the drop, then clear the slot
# link so DROP doesn't try (and fail) to reach the old primary.
psql_target "ALTER SUBSCRIPTION $SUBSCRIPTION DISABLE;" >/dev/null 2>&1 || true
psql_target "ALTER SUBSCRIPTION $SUBSCRIPTION SET (slot_name = NONE);" >/dev/null 2>&1 || true
psql_target "DROP SUBSCRIPTION $SUBSCRIPTION;" >/dev/null 2>&1 \
  || log "warn: DROP SUBSCRIPTION reported an error; verify manually"

# ---------------------------------------------------------------------------
# 5. THE CRITICAL STEP — advance every sequence
# ---------------------------------------------------------------------------
log "advancing sequences on the promoted database"
SEQ_REPORT="$(psql_target "
DO \$\$
DECLARE
  r RECORD;
  maxval BIGINT;
BEGIN
  FOR r IN
    SELECT c.relname AS tbl, a.attname AS col,
           pg_get_serial_sequence(quote_ident(c.relname), a.attname) AS seq
    FROM pg_class c
    JOIN pg_namespace n ON n.oid = c.relnamespace
    JOIN pg_attribute a ON a.attrelid = c.oid AND a.attnum > 0 AND NOT a.attisdropped
    WHERE n.nspname = 'public' AND c.relkind = 'r'
      AND pg_get_serial_sequence(quote_ident(c.relname), a.attname) IS NOT NULL
  LOOP
    EXECUTE format('SELECT COALESCE(MAX(%I), 0) FROM %I', r.col, r.tbl) INTO maxval;
    -- is_called := true so the NEXT nextval() returns maxval + 1
    PERFORM setval(r.seq, GREATEST(maxval, 1), true);
    RAISE NOTICE 'advanced % to %', r.seq, GREATEST(maxval, 1);
  END LOOP;
END
\$\$;
SELECT string_agg(schemaname||'.'||sequencename||'='||COALESCE(last_value::text,'unset'), E'\n  ' ORDER BY sequencename)
FROM pg_sequences WHERE schemaname='public';" 2>&1 | tail -20)"

echo "  sequence state after advancing:"
echo "  $SEQ_REPORT"

# Prove no sequence can hand out a value that already exists. max(col) needs
# dynamic SQL, so the assertion lives in a DO block: it raises an exception if
# any sequence would still collide, and psql's exit code carries the verdict.
if psql_target "
DO \$\$
DECLARE
  r RECORD;
  maxval BIGINT;
  nextv  BIGINT;
  bad    TEXT := '';
BEGIN
  FOR r IN
    SELECT c.relname AS tbl, a.attname AS col,
           pg_get_serial_sequence(quote_ident(c.relname), a.attname) AS seq
    FROM pg_class c
    JOIN pg_namespace n ON n.oid = c.relnamespace
    JOIN pg_attribute a ON a.attrelid = c.oid AND a.attnum > 0 AND NOT a.attisdropped
    WHERE n.nspname = 'public' AND c.relkind = 'r'
      AND pg_get_serial_sequence(quote_ident(c.relname), a.attname) IS NOT NULL
  LOOP
    EXECUTE format('SELECT COALESCE(MAX(%I), 0) FROM %I', r.col, r.tbl) INTO maxval;
    -- last_value with is_called=true means the next nextval() returns it + 1
    EXECUTE format('SELECT last_value FROM %s', r.seq) INTO nextv;
    IF nextv IS NULL OR nextv < maxval THEN
      bad := bad || format('%s.%s (seq=%s, max=%s) ', r.tbl, r.col, COALESCE(nextv::text,'unset'), maxval);
    END IF;
  END LOOP;
  IF bad <> '' THEN
    RAISE EXCEPTION 'sequences would still collide: %', bad;
  END IF;
END
\$\$;" >/dev/null 2>&1; then
  log "verified: every sequence is past its table maximum — no PK collision risk"
else
  die "sequence verification FAILED — the promoted database would produce primary-key collisions on the next INSERT. Do NOT point the bot at it. Re-run this script, or fix manually with setval()."
fi

# ---------------------------------------------------------------------------
# 6. Old primary: stop publishing so it can't fight the new one
# ---------------------------------------------------------------------------
if [[ "$PRIMARY_UP" -eq 1 && "$KEEP_PRIMARY" -eq 0 ]]; then
  log "dropping publication + slots on the OLD primary (prevents split-brain and frees retained WAL)"
  psql_q "DROP PUBLICATION IF EXISTS $PUBLICATION;" >/dev/null 2>&1 || true
  psql_q "SELECT pg_drop_replication_slot(slot_name) FROM pg_replication_slots WHERE NOT active;" >/dev/null 2>&1 || true
fi

# ---------------------------------------------------------------------------
# 7. Verify the promoted database is actually writable
# ---------------------------------------------------------------------------
if psql_target "CREATE TEMP TABLE _promote_probe(x int); DROP TABLE _promote_probe;" >/dev/null 2>&1; then
  log "promoted database accepts writes"
else
  die "the promoted database is not accepting writes — do NOT repoint the bot at it yet"
fi

ALEMBIC="$(psql_target "SELECT version_num FROM alembic_version LIMIT 1;" 2>/dev/null || echo unknown)"

echo
echo "════════════════════════════════════════════════════════════"
echo " ✅ PROMOTION COMPLETE — the mirror is now a standalone primary"
echo "════════════════════════════════════════════════════════════"
echo " schema revision : $ALEMBIC"
echo ""
echo " NEXT STEPS (the bot is still stopped):"
echo "   1. Point the bot at the promoted database:"
echo "        edit .env  ->  DATABASE_URL=$(echo "$TARGET" | sed -E 's#^postgresql(\+[a-z]+)?://#postgresql+asyncpg://#')"
echo "   2. docker compose up -d bot"
echo "   3. ops/check_drift.sh          # confirm the app can query it"
echo "   4. Re-point the BACKUP system at the new primary and take a base"
echo "      backup immediately — the archive still describes the old one:"
echo "        ops/basebackup.sh"
echo "   5. Set up a fresh mirror from the new primary:"
echo "        ops/mirror_setup.sh"
echo ""
echo " ⚠ Your archive is now anchored to the OLD primary's timeline."
echo "   Step 4 is not optional — until it runs you have no valid PITR"
echo "   anchor for the new database."
echo "════════════════════════════════════════════════════════════"

alert "$(printf '⚠️ Telemon mirror PROMOTED to primary (schema %s).\n\nThe bot is stopped. Repoint DATABASE_URL, restart it, then run ops/basebackup.sh immediately — the archive has no valid anchor for the new primary until you do.' "$ALEMBIC")"
