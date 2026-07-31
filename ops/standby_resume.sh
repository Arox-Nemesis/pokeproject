#!/usr/bin/env bash
# Resume a frozen delayed standby, or report how far behind it is.
#
#   ops/standby_resume.sh     # un-pause replay
#   ops/standby_status.sh     # symlink-style alias: report only

SCRIPT_NAME=standby_resume
# shellcheck disable=SC1091
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

CONTAINER="${STANDBY_CONTAINER:-telemon_standby}"
REPORT_ONLY=0
[[ "$(basename "$0")" == "standby_status.sh" || "${1:-}" == "--status" ]] && REPORT_ONLY=1
QUIET=0
[[ "${1:-}" == "--quiet" ]] && { QUIET=1; REPORT_ONLY=1; }

if ! docker inspect -f '{{.State.Running}}' "$CONTAINER" 2>/dev/null | grep -q true; then
  [[ "$QUIET" -eq 1 ]] && exit 0
  echo "delayed standby ($CONTAINER): NOT RUNNING"
  echo "start one with: ops/standby_start.sh"
  exit 0
fi

sq() { docker exec "$CONTAINER" psql -U "$PG_USER" -d "$PG_DB" -Atq -c "$1" 2>/dev/null || echo "?"; }

PAUSED="$(sq 'SELECT pg_is_wal_replay_paused();')"
LAST_REPLAY="$(sq 'SELECT pg_last_xact_replay_timestamp();')"
BEHIND="$(sq "SELECT COALESCE(EXTRACT(EPOCH FROM (now() - pg_last_xact_replay_timestamp()))::int, -1);")"
DELAY_CFG="$(sq 'SHOW recovery_min_apply_delay;')"

if [[ "$REPORT_ONLY" -eq 1 ]]; then
  PROBLEMS=()
  # A standby that has silently stopped replaying is worse than no standby,
  # because you will reach for it during an incident and find it stale.
  if [[ "$PAUSED" == "t" ]]; then
    [[ "$QUIET" -eq 0 ]] && echo "standby: ❄️ FROZEN at ${LAST_REPLAY}"
  elif [[ "$BEHIND" != "-1" && "$BEHIND" != "?" ]]; then
    # Expected lag is the configured delay; flag anything far beyond it.
    DELAY_S="$(sq "SELECT EXTRACT(EPOCH FROM '${DELAY_CFG}'::interval)::int;")"
    MAXOK=$(( ${DELAY_S:-3600} + 1800 ))
    if [[ "$BEHIND" -gt "$MAXOK" ]]; then
      PROBLEMS+=("the delayed standby is ${BEHIND}s behind, well past its configured ${DELAY_CFG} — replay has probably stalled (a pruned WAL segment, or the retention window is shorter than the delay)")
    fi
    [[ "$QUIET" -eq 0 ]] && echo "standby: replaying, ${BEHIND}s behind (configured delay ${DELAY_CFG})"
  fi
  if [[ ${#PROBLEMS[@]} -gt 0 ]]; then
    printf '  • %s\n' "${PROBLEMS[@]}"
    alert "$(printf '🚨 Telemon delayed standby problem:\n\n%s' "$(printf '  • %s\n' "${PROBLEMS[@]}")")"
    exit 1
  fi
  exit 0
fi

if [[ "$PAUSED" != "t" ]]; then
  log "standby is not paused — nothing to resume"
  exit 0
fi

echo "Resuming replay will apply everything that happened on the primary after"
echo "${LAST_REPLAY}, INCLUDING whatever incident you froze this standby to escape."
echo
read -r -p "Really resume? [type RESUME] " confirm
[[ "$confirm" == "RESUME" ]] || { echo "aborted — standby stays frozen"; exit 1; }

sq "SELECT pg_wal_replay_resume();" >/dev/null
log "WAL replay resumed"
alert "▶️ Telemon delayed standby replay resumed by operator."
