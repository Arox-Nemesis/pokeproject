#!/usr/bin/env bash
# Backup system health check.
#
# The failure mode that actually bites is silent: archiving stops working and
# nobody notices for three weeks. Run this from a timer; it screams when the
# archive falls behind.

SCRIPT_NAME=status
# shellcheck disable=SC1091
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

QUIET=0
[[ "${1:-}" == "--quiet" ]] && QUIET=1   # only output/alert on problems

PROBLEMS=()
say() { [[ "$QUIET" -eq 1 ]] || echo -e "$*"; }

say ""
say "════════ TELEMON BACKUP STATUS ════════"

# ---------------------------------------------------------------------------
# Postgres archiver
# ---------------------------------------------------------------------------
if pg_running; then
  say "postgres            : running"

  ARCH_MODE="$(psql_q 'SHOW archive_mode;')"
  [[ "$ARCH_MODE" == "on" ]] || PROBLEMS+=("archive_mode is '$ARCH_MODE', not 'on' — NOTHING is being archived")
  say "archive_mode        : $ARCH_MODE"

  # pg_stat_archiver is the ground truth for whether archiving works.
  # Queried one field at a time — the timestamps contain spaces, so splitting
  # a single delimited row with `read` mangles them.
  ARCHIVED="$(psql_q 'SELECT archived_count FROM pg_stat_archiver;')"
  FAILED="$(psql_q 'SELECT failed_count FROM pg_stat_archiver;')"
  LAST_ARCH="$(psql_q "SELECT COALESCE(to_char(last_archived_time,'YYYY-MM-DD HH24:MI:SS'),'never') FROM pg_stat_archiver;")"
  LAST_FAIL="$(psql_q "SELECT COALESCE(to_char(last_failed_time,'YYYY-MM-DD HH24:MI:SS'),'never') FROM pg_stat_archiver;")"

  say "segments archived   : $ARCHIVED"
  say "archive failures    : $FAILED"
  say "last archived       : $LAST_ARCH"

  if [[ "$FAILED" != "0" ]]; then
    RECENT_FAIL="$(psql_q "SELECT CASE WHEN last_failed_time > COALESCE(last_archived_time, '-infinity') THEN 'yes' ELSE 'no' END FROM pg_stat_archiver;")"
    if [[ "$RECENT_FAIL" == "yes" ]]; then
      PROBLEMS+=("archive_command is FAILING (last failure $LAST_FAIL is newer than last success). pg_wal will grow until the disk fills.")
    fi
  fi

  # Archive lag. With archive_timeout=60 anything past ~5 min is broken.
  LAG="$(psql_q "SELECT COALESCE(EXTRACT(EPOCH FROM (now() - last_archived_time))::int, 999999) FROM pg_stat_archiver;")"
  if [[ "$LAG" -gt 300 ]]; then
    PROBLEMS+=("last successful archive was ${LAG}s ago (expected <120s) — the WAL stream has stalled")
  fi
  say "archive lag         : ${LAG}s"

  # pg_wal growth is the canary for a stuck archiver.
  WAL_SIZE="$(docker exec "$PG_CONTAINER" du -sm /var/lib/postgresql/data/pg_wal 2>/dev/null | cut -f1)"
  say "pg_wal size         : ${WAL_SIZE:-?} MB"
  if [[ -n "${WAL_SIZE:-}" && "$WAL_SIZE" -gt 4096 ]]; then
    PROBLEMS+=("pg_wal has grown to ${WAL_SIZE} MB — archiving is almost certainly stuck")
  fi
else
  PROBLEMS+=("postgres container '$PG_CONTAINER' is not running")
  say "postgres            : NOT RUNNING"
fi

# ---------------------------------------------------------------------------
# Spool + uploader
# ---------------------------------------------------------------------------
if docker inspect -f '{{.State.Running}}' telemon_wal_uploader 2>/dev/null | grep -q true; then
  say "wal-uploader        : running"
  SPOOL_N="$(docker exec telemon_wal_uploader sh -c 'ls -1 /wal-spool 2>/dev/null | grep -v "^\." | wc -l' 2>/dev/null || echo '?')"
  say "spool backlog       : ${SPOOL_N} segment(s)"
  if [[ "$SPOOL_N" != "?" && "$SPOOL_N" -gt 60 ]]; then
    PROBLEMS+=("$SPOOL_N segments are stuck in the local spool — uploads are failing or too slow")
  fi
else
  PROBLEMS+=("wal-uploader container is not running — segments are spooling locally and NOT reaching the cloud")
  say "wal-uploader        : NOT RUNNING"
fi

# ---------------------------------------------------------------------------
# Remote archive freshness
# ---------------------------------------------------------------------------
# A health check must never block. Unreachable or misconfigured remotes get a
# hard deadline rather than rclone's default retry-until-the-heat-death schedule.
rcfast() { timeout 25 rclone --config "$RCLONE_CONF" \
             --timeout 10s --contimeout 5s --retries 1 --low-level-retries 1 "$@"; }

for R in $BACKUP_REMOTES; do
  say ""
  say "── remote: $R"
  if ! rcfast lsjson "${R}/base" --max-depth 1 >/dev/null 2>&1; then
    PROBLEMS+=("cannot reach remote $R (unreachable, or credentials not configured)")
    say "  UNREACHABLE"
    continue
  fi

  # `|| true` is required: with `set -o pipefail`, rclone returning non-zero on
  # a prefix that doesn't exist yet (e.g. quarantine/ on a healthy system)
  # would abort the whole health check.
  count_at() { rcfast lsf "$1" ${2:+--include "$2"} 2>/dev/null | wc -l || true; }
  N_BASE="$(count_at "${R}/base" 'base-*.tar.gz')"
  N_WAL="$(count_at "${R}/wal")"
  N_USER="$(count_at "${R}/user" '*.pgc')"
  N_QUAR="$(count_at "${R}/quarantine" '*.pgc')"

  say "  base backups      : $N_BASE"
  say "  wal segments      : $N_WAL"
  say "  user dumps        : $N_USER"
  [[ "$N_QUAR" -gt 0 ]] && say "  QUARANTINED       : $N_QUAR"

  [[ "$N_BASE" -eq 0 ]] && PROBLEMS+=("$R has NO base backups — WAL alone cannot be restored")
  [[ "$N_WAL" -eq 0 ]] && PROBLEMS+=("$R has NO WAL segments")

  LATEST_BASE="$(rcfast lsf "${R}/base" --include 'base-*.tar.gz' 2>/dev/null | sort | tail -1 || true)"
  if [[ -n "$LATEST_BASE" ]]; then
    raw="${LATEST_BASE#base-}"; raw="${raw%.tar.gz}"
    iso="${raw:0:4}-${raw:4:2}-${raw:6:2} ${raw:9:2}:${raw:11:2}:${raw:13:2}+00"
    age=$(( ($(date -u +%s) - $(date -u -d "$iso" +%s)) / 3600 ))
    say "  newest base age   : ${age}h"
    [[ "$age" -gt 48 ]] && PROBLEMS+=("$R newest base backup is ${age}h old (expected <24h)")
  fi
done

# ---------------------------------------------------------------------------
# Schema drift
# ---------------------------------------------------------------------------
# A restore that reproduces a schema the app cannot query is not a usable
# restore, and alembic_version matching HEAD does not prove the schema matches
# the models. Checked here so drift surfaces hourly rather than at recovery time.
say ""
if docker inspect -f '{{.State.Running}}' "${BOT_CONTAINER:-telemon_bot}" 2>/dev/null | grep -q true; then
  if DRIFT_OUT="$("$OPS_DIR/check_drift.sh" --quiet 2>&1)"; then
    say "schema drift        : none"
  else
    say "schema drift        : ⚠ DETECTED"
    PROBLEMS+=("schema drift: $(echo "$DRIFT_OUT" | grep 'DRIFT:' | head -3 | sed 's/^ *DRIFT: //' | paste -sd'; ')")
  fi
else
  say "schema drift        : skipped (bot container not running)"
fi

# ---------------------------------------------------------------------------
# Gate + drill state
# ---------------------------------------------------------------------------
say ""
if [[ -f "$STATE_DIR/PRUNING_HALTED" ]]; then
  say "anomaly gate        : ⚠ TRIPPED (pruning halted)"
  PROBLEMS+=("the anomaly gate is tripped — a dump was quarantined and retention pruning is frozen. Run ops/clear_anomaly.sh once you have investigated.")
else
  say "anomaly gate        : clear"
fi

if [[ -f "$STATE_DIR/last_drill" ]]; then
  # shellcheck disable=SC1091
  source "$STATE_DIR/last_drill"
  d_iso="${stamp:0:4}-${stamp:4:2}-${stamp:6:2} ${stamp:9:2}:${stamp:11:2}:${stamp:13:2}+00"
  d_age=$(( ($(date -u +%s) - $(date -u -d "$d_iso" +%s)) / 86400 ))
  say "last restore drill  : ${d_age}d ago, ${failures} failure(s), ${elapsed_seconds}s"
  [[ "$d_age" -gt 10 ]] && PROBLEMS+=("last restore drill was ${d_age} days ago — verification has stopped running")
  [[ "$failures" != "0" ]] && PROBLEMS+=("the last restore drill FAILED — your backups did not verify")
else
  say "last restore drill  : NEVER RUN"
  PROBLEMS+=("no restore drill has ever run — your backups are unverified")
fi

# ---------------------------------------------------------------------------
# Verdict
# ---------------------------------------------------------------------------
say ""
if [[ ${#PROBLEMS[@]} -eq 0 ]]; then
  say "VERDICT: ✅ healthy"
  say "═══════════════════════════════════════"
  exit 0
fi

echo ""
echo "VERDICT: 🚨 ${#PROBLEMS[@]} PROBLEM(S)"
printf '  • %s\n' "${PROBLEMS[@]}"
echo "═══════════════════════════════════════"

alert "$(printf '🚨 Telemon backup health check found %d problem(s):\n\n%s' \
  "${#PROBLEMS[@]}" "$(printf '  • %s\n' "${PROBLEMS[@]}")")"
exit 1
