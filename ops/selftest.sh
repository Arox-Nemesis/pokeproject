#!/usr/bin/env bash
# Full-ecosystem self-test. No cloud credentials required.
#
# Exercises every component end to end against a LOCAL filesystem remote and
# scratch containers, then tears everything down and prints a pass/fail table.
#
# SAFETY — what this does and does not touch:
#   * It reads your live database and takes backups of it. Read-only.
#   * It creates and drops tables named `_selftest_*`. Nothing else is written.
#   * It NEVER writes to your real archive (uses a temp local remote) and never
#     touches ops/backup.env or ops/rclone.conf (uses isolated copies).
#   * It NEVER restores into the live database. Restores go to scratch
#     containers on scratch ports.
#   * It stops nothing. Your bot keeps running throughout.
#
# The one caveat: it briefly generates WAL and forces segment switches, so your
# spool will gain a few segments. Harmless.
#
#   ops/selftest.sh              # everything
#   ops/selftest.sh --quick      # skip standby + mirror (the slow parts)
#   ops/selftest.sh --keep       # leave scratch containers up for inspection

set -uo pipefail

OPS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$OPS_DIR")"
# shellcheck disable=SC1091
source "$OPS_DIR/tables.sh"

QUICK=0
KEEP=0
while [[ $# -gt 0 ]]; do
  case "$1" in
    --quick) QUICK=1; shift ;;
    --keep)  KEEP=1; shift ;;
    *) echo "unknown argument: $1"; exit 1 ;;
  esac
done

PG_CONTAINER=telemon_postgres
PG_USER=telemon
PG_DB=telemon

# ---------------------------------------------------------------------------
# Result tracking
# ---------------------------------------------------------------------------
RESULTS=()
PASSED=0
FAILED=0
SKIPPED=0

ok()   { RESULTS+=("PASS|$1|${2:-}"); PASSED=$((PASSED+1)); echo "  ✅ $1"; }
no()   { RESULTS+=("FAIL|$1|${2:-}"); FAILED=$((FAILED+1)); echo "  ❌ $1${2:+ — $2}"; }
skip() { RESULTS+=("SKIP|$1|${2:-}"); SKIPPED=$((SKIPPED+1)); echo "  ⊘  $1${2:+ — $2}"; }
head2(){ echo; echo "── $1"; }

PSQL()  { docker exec "$PG_CONTAINER" psql -U "$PG_USER" -d "$PG_DB" -Atq -c "$1" 2>/dev/null; }

# ---------------------------------------------------------------------------
# Isolated sandbox
# ---------------------------------------------------------------------------
SANDBOX="$(mktemp -d /tmp/telemon-selftest-XXXXXX)"
ARCHIVE="$SANDBOX/archive"
mkdir -p "$ARCHIVE" "$SANDBOX/state"

cat > "$SANDBOX/rclone.conf" <<'EOF'
[selftest]
type = local
EOF

cat > "$SANDBOX/backup.env" <<EOF
BACKUP_REMOTES="selftest:$ARCHIVE"
PG_CONTAINER=$PG_CONTAINER
PG_USER=$PG_USER
PG_DB=$PG_DB
BOT_CONTAINER=telemon_bot
STATE_DIR=$SANDBOX/state
BOT_TOKEN=
ALERT_CHAT_ID=
ANOMALY_SHRINK_THRESHOLD=0.10
ANOMALY_ABSOLUTE_DROP=25
RCLONE_CONF=$SANDBOX/rclone.conf
LOCAL_WAL_RETAIN_HOURS=30
STANDBY_DELAY=1h
STANDBY_PORT=5493
STANDBY_CONTAINER=telemon_selftest_standby
STANDBY_DATA_DIR=$SANDBOX/standby
# Deliberately a container that does NOT exist. mirror_promote.sh stops the bot
# as its first step (correct for a real failover), and pointing it at the real
# telemon_bot means running the test suite takes production offline. This was a
# real incident, not a hypothetical.
BOT_CONTAINER=telemon_selftest_no_such_bot
MIRROR_URL=postgresql://$PG_USER:mirror@127.0.0.1:5492/$PG_DB
MIRROR_PRIMARY_CONNINFO=postgresql://$PG_USER:telemon@$PG_CONTAINER:5432/$PG_DB
MIRROR_PUBLICATION=selftest_pub
MIRROR_SUBSCRIPTION=selftest_sub
DOCKER_NETWORK=telemon_default
EOF

export TELEMON_BACKUP_ENV="$SANDBOX/backup.env"

MIRROR_CONTAINER=telemon_selftest_mirror
STANDBY_CONTAINER=telemon_selftest_standby

cleanup() {
  echo
  if [[ "$KEEP" -eq 1 ]]; then
    echo "--keep: leaving scratch containers and $SANDBOX in place"
    return
  fi
  echo "cleaning up..."
  # Remove the publication we created on the live primary. Leaving it behind
  # would create a replication slot that retains WAL forever.
  PSQL "DROP PUBLICATION IF EXISTS selftest_pub;" >/dev/null 2>&1 || true
  PSQL "SELECT pg_drop_replication_slot(slot_name) FROM pg_replication_slots WHERE slot_name LIKE 'selftest%' AND NOT active;" >/dev/null 2>&1 || true
  PSQL "DROP TABLE IF EXISTS _selftest_proof;" >/dev/null 2>&1 || true
  PSQL "DROP TABLE IF EXISTS _selftest_seq;" >/dev/null 2>&1 || true
  docker rm -f "$MIRROR_CONTAINER" "$STANDBY_CONTAINER" telemon_selftest_restore >/dev/null 2>&1 || true
  rm -rf "$SANDBOX"
  echo "done"
}
trap cleanup EXIT

FLUSH_SPOOL() {
  # Mimic the uploader: push spool -> local archive, and mirror into retain so
  # standby / local-seed paths have something to read.
  docker run --rm \
    -v telemon_wal_spool:/wal-spool:ro \
    -v "$ARCHIVE":/archive \
    -v "$SANDBOX/rclone.conf":/config/rclone/rclone.conf:ro \
    --entrypoint rclone rclone/rclone:latest \
    copy /wal-spool "selftest:/archive/wal" --exclude ".*/**" --exclude ".*" --no-traverse \
    >/dev/null 2>&1 || true
  docker run --rm \
    -v telemon_wal_spool:/src:ro -v telemon_wal_retain:/dst \
    postgres:16-alpine sh -c 'cp -n /src/*.gz /dst/ 2>/dev/null; true' >/dev/null 2>&1 || true
}

echo "════════════════════════════════════════════════════════════"
echo " TELEMON ECOSYSTEM SELF-TEST"
echo "════════════════════════════════════════════════════════════"
echo " sandbox : $SANDBOX"
echo " mode    : $([[ "$QUICK" -eq 1 ]] && echo quick || echo full)"
echo "════════════════════════════════════════════════════════════"

# ---------------------------------------------------------------------------
head2 "0. Preflight"
# ---------------------------------------------------------------------------
for f in "$OPS_DIR"/*.sh; do
  bash -n "$f" 2>/dev/null || no "syntax: $(basename "$f")"
done
[[ "$FAILED" -eq 0 ]] && ok "all shell scripts parse"

python3 -m py_compile "$OPS_DIR/check_anomaly.py" 2>/dev/null \
  && ok "check_anomaly.py compiles" || no "check_anomaly.py compiles"

( cd "$PROJECT_DIR" && docker compose config >/dev/null 2>&1 ) \
  && ok "docker-compose.yml validates" || no "docker-compose.yml validates"

docker exec "$PG_CONTAINER" pg_isready -U "$PG_USER" -d "$PG_DB" >/dev/null 2>&1 \
  && ok "primary reachable" || { no "primary reachable"; echo; echo "cannot continue"; exit 1; }

[[ "$(PSQL 'SHOW archive_mode;')" == "on" ]] && ok "archive_mode=on" || no "archive_mode=on"
[[ "$(PSQL 'SHOW wal_level;')" == "logical" ]] && ok "wal_level=logical" || no "wal_level=logical"

# ---------------------------------------------------------------------------
head2 "1. Archiving"
# ---------------------------------------------------------------------------
BEFORE_ARCH="$(PSQL 'SELECT archived_count FROM pg_stat_archiver;')"
# Needs a PRIMARY KEY: a FOR ALL TABLES publication includes this scratch table,
# and mirror_setup.sh (correctly) refuses to replicate a table whose UPDATEs and
# DELETEs cannot be identified. CREATE TABLE AS produces no key.
PSQL "DROP TABLE IF EXISTS _selftest_seq;" >/dev/null
PSQL "CREATE TABLE _selftest_seq (n int PRIMARY KEY);" >/dev/null
PSQL "INSERT INTO _selftest_seq SELECT generate_series(1,5000);" >/dev/null
PSQL "SELECT pg_switch_wal();" >/dev/null
sleep 6
AFTER_ARCH="$(PSQL 'SELECT archived_count FROM pg_stat_archiver;')"
[[ "${AFTER_ARCH:-0}" -gt "${BEFORE_ARCH:-0}" ]] \
  && ok "WAL segments archive" "$BEFORE_ARCH -> $AFTER_ARCH" \
  || no "WAL segments archive" "count did not advance"

RECENT_FAIL="$(PSQL "SELECT CASE WHEN last_failed_time > COALESCE(last_archived_time,'-infinity') THEN 'yes' ELSE 'no' END FROM pg_stat_archiver;")"
[[ "$RECENT_FAIL" == "no" ]] && ok "no recent archive failures" || no "no recent archive failures"

SEG="$(docker exec "$PG_CONTAINER" sh -c 'ls -S /wal-spool/*.gz 2>/dev/null | head -1')"
if [[ -n "$SEG" ]]; then
  SZ="$(docker exec "$PG_CONTAINER" stat -c %s "$SEG" 2>/dev/null || echo 0)"
  [[ "$SZ" -lt 16777216 ]] && ok "segments are compressed" "$(( SZ / 1024 ))KB vs 16MB raw" \
                           || no "segments are compressed"
fi
FLUSH_SPOOL

# ---------------------------------------------------------------------------
head2 "2. Base backup + logical dumps"
# ---------------------------------------------------------------------------
"$OPS_DIR/basebackup.sh" >"$SANDBOX/base.log" 2>&1 \
  && ok "basebackup.sh" || no "basebackup.sh" "$(tail -2 "$SANDBOX/base.log" | tr '\n' ' ')"

[[ -n "$(find "$ARCHIVE/base" -name 'base-*.tar.gz' 2>/dev/null)" ]] \
  && ok "base backup landed in archive" || no "base backup landed in archive"

"$OPS_DIR/logical_dump.sh" user >"$SANDBOX/user.log" 2>&1 \
  && ok "logical_dump.sh user" || no "logical_dump.sh user" "$(tail -2 "$SANDBOX/user.log" | tr '\n' ' ')"

"$OPS_DIR/logical_dump.sh" full >"$SANDBOX/full.log" 2>&1 \
  && ok "logical_dump.sh full" || no "logical_dump.sh full" "$(tail -2 "$SANDBOX/full.log" | tr '\n' ' ')"

[[ -f "$SANDBOX/state/last_good_census.json" ]] \
  && ok "anomaly baseline established" || no "anomaly baseline established"

# ---------------------------------------------------------------------------
head2 "3. Anomaly gate"
# ---------------------------------------------------------------------------
G="users,pokemon,pokedex_entries,inventory_items,user_achievements,trade_history"
cat > "$SANDBOX/a.json" <<'EOF'
{"tables":{"users":4820,"pokemon":91340,"pokedex_entries":50110,"inventory_items":28800,"user_achievements":9100,"trade_history":15200}}
EOF
gate() { python3 "$OPS_DIR/check_anomaly.py" --previous "$SANDBOX/a.json" --current "$1" \
           --guarded "$G" --shrink-threshold 0.10 --absolute-drop 25 >/dev/null 2>&1; }

echo '{"tables":{"users":4890,"pokemon":92100,"pokedex_entries":50400,"inventory_items":29050,"user_achievements":9180,"trade_history":15400}}' > "$SANDBOX/grow.json"
gate "$SANDBOX/grow.json" && ok "gate: growth passes" || no "gate: growth passes"

echo '{"tables":{"users":0,"pokemon":0,"pokedex_entries":0,"inventory_items":0,"user_achievements":0,"trade_history":0}}' > "$SANDBOX/wipe.json"
gate "$SANDBOX/wipe.json" && no "gate: wipe trips" "it PASSED a full wipe" || ok "gate: wipe trips"

echo '{"tables":{"users":4825,"pokemon":80379,"pokedex_entries":50150,"inventory_items":28850,"user_achievements":9110,"trade_history":15250}}' > "$SANDBOX/part.json"
gate "$SANDBOX/part.json" && no "gate: 12% loss trips" || ok "gate: 12% loss trips"

echo '{"tables":{"users":4818,"pokemon":91355,"pokedex_entries":50112,"inventory_items":28795,"user_achievements":9100,"trade_history":15205}}' > "$SANDBOX/churn.json"
gate "$SANDBOX/churn.json" && ok "gate: churn passes" || no "gate: churn passes"

echo '{"tables":{"users":4820,"pokedex_entries":50110,"inventory_items":28800,"user_achievements":9100,"trade_history":15200}}' > "$SANDBOX/miss.json"
gate "$SANDBOX/miss.json" && no "gate: missing table trips" || ok "gate: missing table trips"

# ---------------------------------------------------------------------------
head2 "4. Schema drift"
# ---------------------------------------------------------------------------
if docker inspect -f '{{.State.Running}}' telemon_bot 2>/dev/null | grep -q true; then
  "$OPS_DIR/check_drift.sh" --quiet >/dev/null 2>&1 \
    && ok "no schema drift" || no "no schema drift" "models and DB disagree"
else
  skip "schema drift" "bot container not running"
fi

# ---------------------------------------------------------------------------
head2 "5. PITR through a wipe  ← the one that matters"
# ---------------------------------------------------------------------------
PSQL "DROP TABLE IF EXISTS _selftest_proof;" >/dev/null
PSQL "CREATE TABLE _selftest_proof (id int primary key, payload text);" >/dev/null
PSQL "INSERT INTO _selftest_proof SELECT g,'precious-'||g FROM generate_series(1,1000) g;" >/dev/null
PSQL "SELECT pg_switch_wal();" >/dev/null
sleep 4; FLUSH_SPOOL

GOOD_TIME="$(PSQL 'SELECT now();')"
sleep 3

# The disaster
PSQL "TRUNCATE _selftest_proof;" >/dev/null
PSQL "INSERT INTO _selftest_proof VALUES (1,'wiped');" >/dev/null
PSQL "SELECT pg_switch_wal();" >/dev/null
sleep 4; FLUSH_SPOOL

LIVE_ROWS="$(PSQL 'SELECT count(*) FROM _selftest_proof;')"
[[ "$LIVE_ROWS" == "1" ]] && ok "wipe staged on live" "1 row" || no "wipe staged on live"

docker rm -f telemon_selftest_restore >/dev/null 2>&1 || true
if "$OPS_DIR/restore_pitr.sh" --target-time "$GOOD_TIME" --port 5491 \
     --container telemon_selftest_restore >"$SANDBOX/pitr.log" 2>&1; then
  ok "restore_pitr.sh completes"
  R="$(docker exec telemon_selftest_restore psql -U "$PG_USER" -d "$PG_DB" -Atq \
        -c 'SELECT count(*) FROM _selftest_proof;' 2>/dev/null || echo 0)"
  [[ "$R" == "1000" ]] && ok "PITR recovered all 1000 rows" "live had $LIVE_ROWS" \
                       || no "PITR recovered all 1000 rows" "got $R"
  I="$(docker exec telemon_selftest_restore psql -U "$PG_USER" -d "$PG_DB" -Atq \
        -c "SELECT CASE WHEN count(*)=1000 AND min(id)=1 AND max(id)=1000 THEN 'ok' ELSE 'bad' END FROM _selftest_proof;" 2>/dev/null)"
  [[ "$I" == "ok" ]] && ok "recovered data has no gaps" || no "recovered data has no gaps"
  docker rm -f telemon_selftest_restore >/dev/null 2>&1 || true
else
  no "restore_pitr.sh completes" "$(tail -3 "$SANDBOX/pitr.log" | tr '\n' ' ')"
  skip "PITR row verification" "restore failed"
fi

# ---------------------------------------------------------------------------
head2 "6. Restore drill"
# ---------------------------------------------------------------------------
if "$OPS_DIR/restore_drill.sh" >"$SANDBOX/drill.log" 2>&1; then
  ok "restore_drill.sh passes"
else
  no "restore_drill.sh passes" "$(grep -m2 'FAIL:' "$SANDBOX/drill.log" | tr '\n' ' ')"
fi
# Regression guard for the bug where a failed drill recorded nothing and
# status.sh then reported "NEVER RUN".
[[ -f "$SANDBOX/state/last_drill" ]] \
  && ok "drill outcome recorded" || no "drill outcome recorded" "status.sh would say NEVER RUN"

# ---------------------------------------------------------------------------
head2 "7. status.sh"
# ---------------------------------------------------------------------------
timeout 180 "$OPS_DIR/status.sh" >"$SANDBOX/status.log" 2>&1
SRC=$?
if [[ "$SRC" -eq 0 || "$SRC" -eq 1 ]]; then
  ok "status.sh runs and exits cleanly" "exit $SRC"
else
  no "status.sh runs and exits cleanly" "exit $SRC$([[ "$SRC" -eq 124 ]] && echo ' = TIMEOUT')"
  # A bare "exit 124" is undiagnosable, so surface the progress markers — the
  # last one printed is the external call that wedged.
  echo "     last progress markers:"
  grep '^\[status\] >>' "$SANDBOX/status.log" 2>/dev/null | tail -4 | sed 's/^/       /'
  echo "     tail of output:"
  tail -8 "$SANDBOX/status.log" 2>/dev/null | sed 's/^/       /'
fi
grep -q "spool backlog" "$SANDBOX/status.log" \
  && ok "spool backlog reported" || no "spool backlog reported" "status.sh never reached that section"

# ---------------------------------------------------------------------------
if [[ "$QUICK" -eq 1 ]]; then
  skip "delayed standby" "--quick"
  skip "warm mirror" "--quick"
else

head2 "8. Delayed standby"
# Use a tiny delay so the test finishes; production default is 1h.
sed -i 's/^STANDBY_DELAY=.*/STANDBY_DELAY=30s/' "$SANDBOX/backup.env"
if "$OPS_DIR/standby_start.sh" --port 5493 >"$SANDBOX/standby.log" 2>&1; then
  ok "standby_start.sh"
  SB() { docker exec "$STANDBY_CONTAINER" psql -U "$PG_USER" -d "$PG_DB" -Atq -c "$1" 2>/dev/null; }
  [[ "$(SB 'SELECT pg_is_in_recovery();')" == "t" ]] \
    && ok "standby is in recovery mode" || no "standby is in recovery mode"
  D="$(SB 'SHOW recovery_min_apply_delay;')"
  [[ -n "$D" && "$D" != "0" ]] && ok "apply delay configured" "$D" || no "apply delay configured"

  if "$OPS_DIR/standby_freeze.sh" >"$SANDBOX/freeze.log" 2>&1; then
    ok "standby_freeze.sh"
    [[ "$(SB 'SELECT pg_is_wal_replay_paused();')" == "t" ]] \
      && ok "replay actually paused" || no "replay actually paused"
  else
    no "standby_freeze.sh" "$(tail -2 "$SANDBOX/freeze.log" | tr '\n' ' ')"
  fi

  "$OPS_DIR/standby_status.sh" --quiet >/dev/null 2>&1
  ok "standby_status.sh runs" "exit $?"
  "$OPS_DIR/standby_stop.sh" --yes >/dev/null 2>&1 \
    && ok "standby_stop.sh" || no "standby_stop.sh"
else
  no "standby_start.sh" "$(tail -3 "$SANDBOX/standby.log" | tr '\n' ' ')"
  skip "standby freeze/status/stop" "start failed"
fi

head2 "9. Warm mirror (logical replication)"
docker rm -f "$MIRROR_CONTAINER" >/dev/null 2>&1 || true
docker run -d --name "$MIRROR_CONTAINER" \
  --network telemon_default \
  -e POSTGRES_USER="$PG_USER" -e POSTGRES_PASSWORD=mirror -e POSTGRES_DB="$PG_DB" \
  -p 5492:5432 postgres:16-alpine \
  postgres -c wal_level=logical -c max_replication_slots=10 >/dev/null 2>&1

for _ in $(seq 1 45); do
  docker exec "$MIRROR_CONTAINER" pg_isready -U "$PG_USER" -d "$PG_DB" >/dev/null 2>&1 && break
  sleep 2
done

if docker exec "$MIRROR_CONTAINER" pg_isready -U "$PG_USER" -d "$PG_DB" >/dev/null 2>&1; then
  ok "scratch mirror container up"
  # The mirror must dial the primary over the docker network.
  sed -i "s#^MIRROR_URL=.*#MIRROR_URL=postgresql://$PG_USER:mirror@$MIRROR_CONTAINER:5432/$PG_DB#" "$SANDBOX/backup.env"

  if "$OPS_DIR/mirror_setup.sh" >"$SANDBOX/mirror.log" 2>&1; then
    ok "mirror_setup.sh"
    MQ() { docker exec "$MIRROR_CONTAINER" psql -U "$PG_USER" -d "$PG_DB" -Atq -c "$1" 2>/dev/null; }

    PU="$(PSQL 'SELECT count(*) FROM users;')"
    MU="$(MQ 'SELECT count(*) FROM users;')"
    [[ "$PU" == "$MU" ]] && ok "initial data synced" "users: $PU = $MU" \
                         || no "initial data synced" "primary=$PU mirror=$MU"

    # Live replication of a new row
    PSQL "INSERT INTO _selftest_seq VALUES (999999);" >/dev/null 2>&1 || true
    sleep 4
    [[ "$(MQ 'SELECT count(*) FROM _selftest_seq WHERE n=999999;')" == "1" ]] \
      && ok "live changes replicate" || no "live changes replicate"

    "$OPS_DIR/mirror_status.sh" >"$SANDBOX/mstatus.log" 2>&1
    grep -q "replication slots" "$SANDBOX/mstatus.log" \
      && ok "mirror_status.sh reports slots" || no "mirror_status.sh reports slots"

    # Sequence advancement is the correctness crux of promotion.
    BEFORE_SEQ="$(MQ "SELECT COALESCE(last_value,-1) FROM pg_sequences WHERE sequencename='teams_id_seq';")"
    if "$OPS_DIR/mirror_promote.sh" --yes --keep-primary >"$SANDBOX/promote.log" 2>&1; then
      ok "mirror_promote.sh"
      grep -q "no PK collision risk" "$SANDBOX/promote.log" \
        && ok "sequence advancement verified" \
        || no "sequence advancement verified" "$(grep -m1 -i 'sequence' "$SANDBOX/promote.log" | tr -d '\n')"
      [[ "$(MQ 'SELECT count(*) FROM pg_subscription;')" == "0" ]] \
        && ok "subscription dropped on promote" || no "subscription dropped on promote"
      MQ "CREATE TEMP TABLE _p(x int); DROP TABLE _p;" >/dev/null 2>&1 \
        && ok "promoted mirror accepts writes" || no "promoted mirror accepts writes"
    else
      no "mirror_promote.sh" "$(tail -3 "$SANDBOX/promote.log" | tr '\n' ' ')"
    fi
  else
    no "mirror_setup.sh" "$(tail -3 "$SANDBOX/mirror.log" | tr '\n' ' ')"
    skip "mirror replication + promotion" "setup failed"
  fi
else
  no "scratch mirror container up"
  skip "mirror tests" "container did not start"
fi

fi  # end non-quick

# ---------------------------------------------------------------------------
# Verify we left no replication slot behind — a forgotten slot retains WAL on
# the primary forever and is the worst thing this test could leave lying around.
# ---------------------------------------------------------------------------
head2 "10. Cleanliness"
PSQL "DROP PUBLICATION IF EXISTS selftest_pub;" >/dev/null 2>&1 || true
PSQL "SELECT pg_drop_replication_slot(slot_name) FROM pg_replication_slots WHERE slot_name LIKE 'selftest%' AND NOT active;" >/dev/null 2>&1 || true
LEFT="$(PSQL "SELECT count(*) FROM pg_replication_slots WHERE slot_name LIKE 'selftest%';")"
[[ "${LEFT:-0}" == "0" ]] && ok "no replication slots left on the primary" \
                          || no "no replication slots left on the primary" "$LEFT remain — drop them or WAL will accumulate"

[[ -f "$OPS_DIR/backup.env" ]] && ok "real backup.env untouched" || skip "real backup.env" "not present"

# ---------------------------------------------------------------------------
echo
echo "════════════════════════════════════════════════════════════"
echo " RESULTS"
echo "════════════════════════════════════════════════════════════"
for r in "${RESULTS[@]}"; do
  IFS='|' read -r st name detail <<< "$r"
  case "$st" in
    PASS) printf ' ✅ %-42s %s\n' "$name" "$detail" ;;
    FAIL) printf ' ❌ %-42s %s\n' "$name" "$detail" ;;
    SKIP) printf ' ⊘  %-42s %s\n' "$name" "$detail" ;;
  esac
done
echo "────────────────────────────────────────────────────────────"
printf ' %d passed, %d failed, %d skipped\n' "$PASSED" "$FAILED" "$SKIPPED"
echo "════════════════════════════════════════════════════════════"

[[ "$FAILED" -eq 0 ]] && exit 0 || exit 1
