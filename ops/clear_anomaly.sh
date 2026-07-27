#!/usr/bin/env bash
# Acknowledge an anomaly and resume normal operation.
#
# Run this ONLY after you have confirmed the current database state is correct
# and intentional (e.g. you deliberately pruned inactive users, or you just
# finished a legitimate restore).
#
# This re-baselines the anomaly gate to the CURRENT row counts and unfreezes
# retention pruning. If you run it while the database is actually damaged, you
# are telling the system "this damage is the new normal" — and pruning will
# resume aging out the good backups. Read the alert first.

SCRIPT_NAME=clear_anomaly
# shellcheck disable=SC1091
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

require_pg

echo
echo "Current row counts for guarded tables:"
for t in "${GUARDED_TABLES[@]}"; do
  printf '  %-22s %s\n' "$t" "$(psql_q "SELECT count(*) FROM public.${t};" 2>/dev/null || echo '?')"
done
echo
echo "Re-baselining the anomaly gate to these counts will unfreeze retention"
echo "pruning. Older backups will start aging out per the lifecycle policy."
echo
read -r -p "Are these counts correct and intentional? [type YES] " confirm
[[ "$confirm" == "YES" ]] || { echo "aborted"; exit 1; }

CENSUS="$STATE_DIR/last_good_census.json"
{
  echo "{"
  echo "  \"stamp\": \"$(stamp)\","
  echo "  \"mode\": \"manual-rebaseline\","
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

rm -f "$STATE_DIR/PRUNING_HALTED"

log "anomaly gate re-baselined; pruning unfrozen"
alert "✅ Telemon backup anomaly acknowledged by operator. Gate re-baselined, retention pruning resumed."
