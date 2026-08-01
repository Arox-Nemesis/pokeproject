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

# Progress markers. If this script ever hangs, the last marker written to the
# log tells you exactly which external call wedged — without them a timeout is
# undiagnosable. Written to stderr so --quiet still records them.
mark() { echo "[status] >> $*" >&2; }

# Every external command gets a hard ceiling. A health check that can hang is
# worse than no health check: the timer silently stops reporting.
dex() { timeout "${DOCKER_TIMEOUT:-15}" docker "$@" </dev/null 2>/dev/null; }

say ""
say "════════ TELEMON BACKUP STATUS ════════"

# ---------------------------------------------------------------------------
# Postgres archiver
# ---------------------------------------------------------------------------
mark "postgres archiver"
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

  # pg_wal growth is a symptom with two very different causes. Naming the wrong
  # one costs real time: this alerted "archiving is almost certainly stuck" for
  # 9 hours while archiving was healthy and an orphaned replication slot was
  # the actual cause. Diagnose before blaming.
  mark "pg_wal du"
  WAL_SIZE="$(dex exec "$PG_CONTAINER" du -sm /var/lib/postgresql/data/pg_wal | cut -f1)"
  say "pg_wal size         : ${WAL_SIZE:-?} MB"
  if [[ -n "${WAL_SIZE:-}" && "$WAL_SIZE" =~ ^[0-9]+$ && "$WAL_SIZE" -gt 2048 ]]; then
    # Which is it: a stalled archiver, or a slot pinning WAL?
    SLOT_INFO="$(psql_q "
      SELECT string_agg(slot_name||' (active='||active||', retaining '||
             pg_size_pretty(pg_wal_lsn_diff(pg_current_wal_lsn(), restart_lsn))||')', '; ')
      FROM pg_replication_slots;" 2>/dev/null)"
    ARCH_STALLED=0
    [[ "${LAG:-0}" =~ ^[0-9]+$ && "${LAG:-0}" -gt 300 ]] && ARCH_STALLED=1

    if [[ -n "$SLOT_INFO" ]]; then
      PROBLEMS+=("pg_wal has grown to ${WAL_SIZE} MB and a REPLICATION SLOT is retaining it: ${SLOT_INFO}. If that slot is inactive its consumer is gone and it will pin WAL until the disk fills. Drop it with: SELECT pg_drop_replication_slot('<name>');")
    elif [[ "$ARCH_STALLED" -eq 1 ]]; then
      PROBLEMS+=("pg_wal has grown to ${WAL_SIZE} MB and the archiver is stalled (last success ${LAG}s ago) — segments cannot be recycled until archiving resumes")
    else
      PROBLEMS+=("pg_wal has grown to ${WAL_SIZE} MB, but archiving is healthy (${LAG}s lag) and no replication slot is holding it. Likely a heavy write burst versus max_wal_size, or a long-running transaction. Check: SELECT pid, state, xact_start FROM pg_stat_activity WHERE xact_start < now() - interval '1 hour';")
    fi
  fi
else
  PROBLEMS+=("postgres container '$PG_CONTAINER' is not running")
  say "postgres            : NOT RUNNING"
fi

# ---------------------------------------------------------------------------
# Spool + uploader
# ---------------------------------------------------------------------------
mark "uploader + spool"
if dex inspect -f '{{.State.Running}}' telemon_wal_uploader | grep -q true; then
  say "wal-uploader        : running"
else
  PROBLEMS+=("wal-uploader container is not running — segments are spooling locally and NOT reaching the cloud")
  say "wal-uploader        : NOT RUNNING"
fi

# Measured via the POSTGRES container, not the uploader. The spool only grows
# without bound when the uploader is DOWN, so checking it through the uploader
# meant the metric vanished at exactly the moment it started to matter.
if pg_running; then
  mark "spool count"
  # `grep -v | wc -l`, not `grep -vc`: grep -vc exits 1 on a zero count, which
  # would make the `|| echo '?'` fallback append to the real value.
  SPOOL_N="$(dex exec "$PG_CONTAINER" sh -c 'ls -1 /wal-spool 2>/dev/null | grep -v "^\." | wc -l' || echo '?')"
  mark "spool du"
  SPOOL_MB="$(dex exec "$PG_CONTAINER" du -sm /wal-spool | cut -f1 || echo '?')"
  SPOOL_N="$(echo "$SPOOL_N" | tr -d ' \n')"
  SPOOL_MB="$(echo "$SPOOL_MB" | tr -d ' \n')"
  say "spool backlog       : ${SPOOL_N:-?} segment(s), ${SPOOL_MB:-?} MB"
  if [[ "${SPOOL_N:-?}" =~ ^[0-9]+$ && "$SPOOL_N" -gt 60 ]]; then
    PROBLEMS+=("$SPOOL_N WAL segments (${SPOOL_MB:-?} MB) are stuck in the local spool and have NOT reached any cloud remote. They are safe on this disk for now, but this is single-copy data and the spool will grow until the disk fills.")
  fi
  AVAIL_MB="$(df -Pm / 2>/dev/null | awk 'NR==2{print $4}')"
  if [[ -n "${AVAIL_MB:-}" && "$AVAIL_MB" =~ ^[0-9]+$ && "$AVAIL_MB" -lt 5120 ]]; then
    PROBLEMS+=("only ${AVAIL_MB} MB free on / — if this reaches zero, archive_command fails, pg_wal grows and Postgres stops accepting writes")
  fi
fi

# ---------------------------------------------------------------------------
# Remote archive freshness
# ---------------------------------------------------------------------------
# A health check must never block. Unreachable or misconfigured remotes get a
# hard deadline rather than rclone's default retry-until-the-heat-death schedule.
rcfast() { timeout 25 rclone --config "$RCLONE_CONF" \
             --timeout 10s --contimeout 5s --retries 1 --low-level-retries 1 "$@"; }

for R in $BACKUP_REMOTES; do
  mark "remote $R"
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
mark "schema drift"
if dex inspect -f '{{.State.Running}}' "${BOT_CONTAINER:-telemon_bot}" | grep -q true; then
  # Bounded: this imports SQLAlchemy and every model inside the bot container,
  # which is the slowest single call in this script.
  # `|| DRC=$?` keeps the assignment non-fatal under `set -e` while still
  # capturing the real exit code (checking $? inside elif is unreliable).
  DRC=0
  DRIFT_OUT="$(timeout 60 "$OPS_DIR/check_drift.sh" --quiet 2>&1)" || DRC=$?
  if [[ "$DRC" -eq 0 ]]; then
    say "schema drift        : none"
  elif [[ "$DRC" -eq 124 ]]; then
    say "schema drift        : TIMED OUT"
    PROBLEMS+=("the schema drift check timed out after 60s — run ops/check_drift.sh directly to see why")
  else
    say "schema drift        : ⚠ DETECTED"
    PROBLEMS+=("schema drift: $(echo "$DRIFT_OUT" | grep 'DRIFT:' | head -3 | sed 's/^ *DRIFT: //' | paste -sd'; ')")
  fi
else
  say "schema drift        : skipped (bot container not running)"
fi

# ---------------------------------------------------------------------------
# Orphaned replication slots
# ---------------------------------------------------------------------------
# Checked unconditionally, NOT delegated to mirror_status.sh: that only runs
# when a mirror is configured, so a slot left behind by a test, an abandoned
# experiment, or a deleted mirror goes unnoticed — exactly how 11 GB of WAL
# accumulated here. An inactive slot pins WAL until the disk fills and Postgres
# stops accepting writes.
mark "replication slots"
say ""
ORPHANS="$(psql_q "
  SELECT string_agg(slot_name||' ('||pg_size_pretty(pg_wal_lsn_diff(pg_current_wal_lsn(), restart_lsn))||')', '; ')
  FROM pg_replication_slots WHERE NOT active;" 2>/dev/null)"
SLOT_TOTAL="$(psql_q "SELECT count(*) FROM pg_replication_slots;" 2>/dev/null || echo '?')"
if [[ -n "$ORPHANS" ]]; then
  say "replication slots   : ⚠ INACTIVE: $ORPHANS"
  PROBLEMS+=("INACTIVE replication slot(s) are pinning WAL on the primary: ${ORPHANS}. Their consumer is gone; they will retain WAL until the disk fills and Postgres stops accepting writes. Drop with: SELECT pg_drop_replication_slot('<name>');")
else
  say "replication slots   : ${SLOT_TOTAL:-0} (none inactive)"
fi

# ---------------------------------------------------------------------------
# Tier 2 summary (delegated — these have their own dedicated timers)
# ---------------------------------------------------------------------------
say ""
mark "tier 2"
if [[ -n "${MIRROR_URL:-}" ]] || [[ "$(psql_q "SELECT count(*) FROM pg_publication;" 2>/dev/null || echo 0)" != "0" ]]; then
  if timeout 60 "$OPS_DIR/mirror_status.sh" --quiet >/dev/null 2>&1; then
    say "warm mirror         : healthy"
  else
    say "warm mirror         : ⚠ PROBLEM (see ops/mirror_status.sh)"
    PROBLEMS+=("the warm mirror reports problems — run ops/mirror_status.sh. If a replication slot is inactive it is retaining WAL on this primary and will eventually fill the disk.")
  fi
else
  say "warm mirror         : not configured"
fi

if dex inspect -f '{{.State.Running}}' "${STANDBY_CONTAINER:-telemon_standby}" | grep -q true; then
  if timeout 60 "$OPS_DIR/standby_status.sh" --quiet >/dev/null 2>&1; then
    say "delayed standby     : healthy"
  else
    say "delayed standby     : ⚠ PROBLEM (see ops/standby_status.sh)"
    PROBLEMS+=("the delayed standby reports problems — it may have stalled, which means it is NOT the recovery option you think it is")
  fi
else
  say "delayed standby     : not running"
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
