#!/usr/bin/env bash
# Mirror health: replication lag, slot state, and row-count divergence.
#
# The two things that go wrong with logical replication:
#
#   1. It silently stops. The subscription errors out, nobody notices, and the
#      "warm standby" you'd fail over to is three weeks stale.
#
#   2. It fills your primary's disk. An inactive replication slot retains WAL
#      on the PRIMARY forever so the absent subscriber can catch up later. If
#      the mirror is gone for good, pg_wal grows without bound until the disk
#      fills and the bot goes down. The mirror taking the primary with it is
#      the single worst failure mode here, and it is easy to hit.
#
#   ops/mirror_status.sh            human readable
#   ops/mirror_status.sh --quiet    only speak up on problems

SCRIPT_NAME=mirror_status
# shellcheck disable=SC1091
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

QUIET=0
[[ "${1:-}" == "--quiet" ]] && QUIET=1
say() { [[ "$QUIET" -eq 1 ]] || echo -e "$*"; }

TARGET="${MIRROR_URL:-}"
PUBLICATION="${MIRROR_PUBLICATION:-telemon_pub}"
# Retained-WAL threshold before we shout. Default 4 GiB.
SLOT_WARN_BYTES="${MIRROR_SLOT_WARN_BYTES:-4294967296}"

PROBLEMS=()

require_pg

say ""
say "════════ TELEMON MIRROR STATUS ════════"

# ---------------------------------------------------------------------------
# Publication
# ---------------------------------------------------------------------------
if [[ "$(psql_q "SELECT count(*) FROM pg_publication WHERE pubname='$PUBLICATION';")" == "0" ]]; then
  say "publication         : ABSENT (mirror not configured)"
  say "═══════════════════════════════════════"
  exit 0
fi
say "publication         : $PUBLICATION"

# ---------------------------------------------------------------------------
# Replication slots — the disk-filling hazard
# ---------------------------------------------------------------------------
SLOT_COUNT="$(psql_q "SELECT count(*) FROM pg_replication_slots;")"
say "replication slots   : $SLOT_COUNT"

if [[ "$SLOT_COUNT" != "0" ]]; then
  while IFS='|' read -r slot active retained_bytes; do
    [[ -z "$slot" ]] && continue
    human=$(( ${retained_bytes:-0} / 1048576 ))
    say "  ├ $slot: active=$active retained_wal=${human}MB"

    if [[ "$active" == "f" ]]; then
      PROBLEMS+=("replication slot '$slot' is INACTIVE and retaining ${human}MB of WAL on the primary. If the mirror is gone for good, DROP THE SLOT or pg_wal will grow until the disk fills and the bot goes down: SELECT pg_drop_replication_slot('$slot');")
    fi
    if [[ "${retained_bytes:-0}" -gt "$SLOT_WARN_BYTES" ]]; then
      PROBLEMS+=("replication slot '$slot' is retaining ${human}MB of WAL — the mirror is too far behind or gone. This WAL cannot be freed until it catches up or the slot is dropped.")
    fi
  done < <(psql_q "
    SELECT slot_name, active,
           COALESCE(pg_wal_lsn_diff(pg_current_wal_lsn(), restart_lsn), 0)::bigint
    FROM pg_replication_slots;")
fi

# ---------------------------------------------------------------------------
# Live sender lag
# ---------------------------------------------------------------------------
SENDERS="$(psql_q "SELECT count(*) FROM pg_stat_replication;")"
say "active senders      : $SENDERS"
if [[ "$SENDERS" == "0" ]]; then
  PROBLEMS+=("no active replication connection — the mirror is NOT receiving changes right now")
else
  while IFS='|' read -r app state lag_bytes; do
    [[ -z "$app" ]] && continue
    say "  ├ $app: state=$state behind=$(( ${lag_bytes:-0} / 1024 ))KB"
    [[ "$state" != "streaming" ]] && PROBLEMS+=("replication to '$app' is in state '$state', not 'streaming'")
  done < <(psql_q "
    SELECT application_name, state,
           COALESCE(pg_wal_lsn_diff(sent_lsn, replay_lsn),0)::bigint
    FROM pg_stat_replication;")
fi

# ---------------------------------------------------------------------------
# Divergence check against the mirror itself
# ---------------------------------------------------------------------------
if [[ -n "$TARGET" ]]; then
  # No `-i` and stdin closed — `-i` can block forever when the caller's stdin
  # is a pipe (e.g. running under `| tee`).
  psql_target() { timeout 25 docker exec "$PG_CONTAINER" psql "$TARGET" -Atq -c "$1" </dev/null; }
  if psql_target "SELECT 1" >/dev/null 2>&1; then
    say ""
    say "── row counts (primary vs mirror)"
    DIVERGED=0
    for t in "${GUARDED_TABLES[@]}"; do
      p="$(psql_q "SELECT count(*) FROM public.$t;" 2>/dev/null || echo '?')"
      m="$(psql_target "SELECT count(*) FROM public.$t;" 2>/dev/null || echo '?')"
      flag=""
      if [[ "$p" != "$m" ]]; then
        # A small lag-induced gap is normal; a large one is not.
        if [[ "$p" =~ ^[0-9]+$ && "$m" =~ ^[0-9]+$ ]]; then
          diff=$(( p > m ? p - m : m - p ))
          if [[ "$p" -gt 0 ]] && [[ $(( diff * 100 / (p>0?p:1) )) -gt 5 ]]; then
            flag="  ← DIVERGED"; DIVERGED=1
          else
            flag="  (lag)"
          fi
        fi
      fi
      say "$(printf '  %-22s %8s  %8s%s' "$t" "$p" "$m" "$flag")"
    done
    [[ "$DIVERGED" -eq 1 ]] && PROBLEMS+=("the mirror has diverged from the primary by more than 5% on at least one table — replication may be broken or partially applied")

    ERRS="$(psql_target "SELECT count(*) FROM pg_stat_subscription_stats WHERE apply_error_count > 0 OR sync_error_count > 0;" 2>/dev/null || echo 0)"
    [[ "$ERRS" != "0" ]] && PROBLEMS+=("the mirror reports subscription apply/sync errors — replication is failing to apply changes")
  else
    PROBLEMS+=("cannot reach the mirror at MIRROR_URL to verify it")
    say "mirror reachable    : NO"
  fi
fi

say ""
if [[ ${#PROBLEMS[@]} -eq 0 ]]; then
  say "VERDICT: ✅ mirror healthy"
  say "═══════════════════════════════════════"
  exit 0
fi

echo ""
echo "VERDICT: 🚨 ${#PROBLEMS[@]} PROBLEM(S)"
printf '  • %s\n' "${PROBLEMS[@]}"
echo "═══════════════════════════════════════"
alert "$(printf '🚨 Telemon MIRROR problem(s):\n\n%s' "$(printf '  • %s\n' "${PROBLEMS[@]}")")"
exit 1
