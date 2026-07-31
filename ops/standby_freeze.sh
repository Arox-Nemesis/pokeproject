#!/usr/bin/env bash
# FREEZE the delayed standby — run this the moment you notice an incident.
#
# The delayed standby is holding data from N hours ago, but it is still
# replaying. Left alone, the damage you are trying to escape WILL be applied
# once the delay elapses. This pauses replay indefinitely, locking in the
# pre-incident state so you can take your time.
#
# This is the single most time-sensitive command in the whole system. If you
# are unsure whether you need it, run it — pausing costs nothing and is
# trivially reversible with ops/standby_resume.sh.

SCRIPT_NAME=standby_freeze
# shellcheck disable=SC1091
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

CONTAINER="${STANDBY_CONTAINER:-telemon_standby}"

docker inspect -f '{{.State.Running}}' "$CONTAINER" 2>/dev/null | grep -q true \
  || die "the delayed standby ($CONTAINER) is not running — there is nothing to freeze. Recover from the archive instead: ops/restore_pitr.sh"

sq() { docker exec "$CONTAINER" psql -U "$PG_USER" -d "$PG_DB" -Atq -c "$1" 2>/dev/null || echo "?"; }

PAUSED_BEFORE="$(sq 'SELECT pg_is_wal_replay_paused();')"
if [[ "$PAUSED_BEFORE" == "t" ]]; then
  log "already frozen — no change"
else
  sq "SELECT pg_wal_replay_pause();" >/dev/null
  log "WAL replay PAUSED"
fi

LAST_REPLAY="$(sq 'SELECT pg_last_xact_replay_timestamp();')"
LSN="$(sq 'SELECT pg_last_wal_replay_lsn();')"

echo
echo "════════════════════════════════════════════════════════════"
echo " ❄️  STANDBY FROZEN"
echo "════════════════════════════════════════════════════════════"
echo " replay paused at   : ${LAST_REPLAY:-unknown}"
echo " lsn                : ${LSN:-unknown}"
echo " frozen             : $(sq 'SELECT pg_is_wal_replay_paused();')"
echo ""
echo " Data as of ${LAST_REPLAY:-the above timestamp} is now held indefinitely."
echo " Nothing that happened on the primary after this point will be applied."
echo ""
printf ' %-22s %s\n' "table" "rows held"
for t in "${GUARDED_TABLES[@]}"; do
  printf ' %-22s %s\n' "$t" "$(sq "SELECT count(*) FROM public.$t;")"
done
echo "────────────────────────────────────────────────────────────"
echo " Inspect:"
echo "   psql 'postgresql://${PG_USER}:standby@localhost:${STANDBY_PORT:-5495}/${PG_DB}'"
echo ""
echo " Recover data from it into the live database:"
echo "   docker compose stop bot"
echo "   ops/restore_logical.sh --from-container ${CONTAINER}"
echo "   docker compose start bot"
echo ""
echo " Or resume replay if this was a false alarm:"
echo "   ops/standby_resume.sh"
echo "════════════════════════════════════════════════════════════"

alert "$(printf '❄️ Telemon delayed standby FROZEN at %s. Pre-incident data is now held indefinitely on port %s.' "${LAST_REPLAY:-unknown}" "${STANDBY_PORT:-5495}")"
