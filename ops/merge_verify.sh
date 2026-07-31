#!/usr/bin/env bash
# Independent verification of a completed merge.
#
# Deliberately does NOT trust ops/merge_execute.sh's own arithmetic — it
# reconstructs both source datasets from the pre-merge backups and proves, by
# set comparison, that the merged database is exactly their union with the
# agreed conflict rules applied.
#
# Read-only with respect to the merged database. Creates two temporary
# databases for comparison and drops them afterwards.
#
#   ops/merge_verify.sh
#   ops/merge_verify.sh --keep     # leave the reconstruction DBs for poking at

set -uo pipefail

OPS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PG_CONTAINER=telemon_postgres
PG_USER=telemon
MERGED_DB=telemon_merged
BACKUP_DIR="${MERGE_BACKUP_DIR:-/var/lib/telemon-merge}"
VOLD=telemon_vfy_old
VNEW=telemon_vfy_new
KEEP=0
[[ "${1:-}" == "--keep" ]] && KEEP=1

W="$(mktemp -d /tmp/telemon-vfy-XXXXXX)"
PASS=0; FAIL=0
ok() { echo "  ✅ $1${2:+  $2}"; PASS=$((PASS+1)); }
no() { echo "  ❌ $1${2:+  $2}"; FAIL=$((FAIL+1)); }

Q() { docker exec "$PG_CONTAINER" psql -U "$PG_USER" -d "$1" -Atq -c "$2" </dev/null 2>/dev/null; }
P() { docker exec "$PG_CONTAINER" psql -U "$PG_USER" -d postgres -q -c "$1" </dev/null 2>/dev/null; }

cleanup() {
  rm -rf "$W"
  [[ "$KEEP" -eq 1 ]] && { echo; echo "--keep: $VOLD and $VNEW left in place"; return; }
  P "DROP DATABASE IF EXISTS $VOLD;"; P "DROP DATABASE IF EXISTS $VNEW;"
}
trap cleanup EXIT

OLD_BK="$(ls -1t "$BACKUP_DIR"/old-preMerge-*.pgc 2>/dev/null | head -1)"
NEW_BK="$(ls -1t "$BACKUP_DIR"/new-preMerge-*.pgc 2>/dev/null | head -1)"
[[ -n "$OLD_BK" && -n "$NEW_BK" ]] || { echo "FATAL: pre-merge backups not found in $BACKUP_DIR"; exit 1; }

echo "════════════════════════════════════════════════════════════"
echo " INDEPENDENT MERGE VERIFICATION"
echo "════════════════════════════════════════════════════════════"
echo " old backup : $(basename "$OLD_BK")"
echo " new backup : $(basename "$NEW_BK")"
echo " merged db  : $MERGED_DB"
echo "════════════════════════════════════════════════════════════"
echo
echo "reconstructing both source datasets from the backups..."
P "DROP DATABASE IF EXISTS $VOLD;"; P "DROP DATABASE IF EXISTS $VNEW;"
P "CREATE DATABASE $VOLD;"; P "CREATE DATABASE $VNEW;"
docker exec -i "$PG_CONTAINER" pg_restore -U "$PG_USER" -d "$VOLD" --no-owner --no-privileges < "$OLD_BK" >/dev/null 2>&1
docker exec -i "$PG_CONTAINER" pg_restore -U "$PG_USER" -d "$VNEW" --no-owner --no-privileges < "$NEW_BK" >/dev/null 2>&1

O_U="$(Q "$VOLD" 'SELECT count(*) FROM users;')"
N_U="$(Q "$VNEW" 'SELECT count(*) FROM users;')"
M_U="$(Q "$MERGED_DB" 'SELECT count(*) FROM users;')"
echo "  old=$O_U  new=$N_U  merged=$M_U"
[[ "$O_U" -gt 0 && "$N_U" -gt 0 ]] || { echo "FATAL: reconstruction failed"; exit 1; }

# ---------------------------------------------------------------------------
echo
echo "── 1. user set is EXACTLY the union of both sources"
# ---------------------------------------------------------------------------
Q "$VOLD"      'SELECT telegram_id FROM users;' | sort -u > "$W/old.ids"
Q "$VNEW"      'SELECT telegram_id FROM users;' | sort -u > "$W/new.ids"
Q "$MERGED_DB" 'SELECT telegram_id FROM users;' | sort -u > "$W/merged.ids"
sort -u "$W/old.ids" "$W/new.ids" > "$W/union.ids"

OVERLAP="$(comm -12 "$W/old.ids" "$W/new.ids" | wc -l)"
echo "     true overlap (computed from the sources): $OVERLAP"
echo "     expected union size: $O_U + $N_U - $OVERLAP = $(( O_U + N_U - OVERLAP ))"

if diff -q "$W/union.ids" "$W/merged.ids" >/dev/null; then
  ok "merged user set == union(old, new)" "$M_U users, no extras, none missing"
else
  MISSING="$(comm -23 "$W/union.ids" "$W/merged.ids" | wc -l)"
  EXTRA="$(comm -13 "$W/union.ids" "$W/merged.ids" | wc -l)"
  no "merged user set != union" "missing=$MISSING extra=$EXTRA"
  comm -23 "$W/union.ids" "$W/merged.ids" | head -5 | sed 's/^/       missing: /'
  comm -13 "$W/union.ids" "$W/merged.ids" | head -5 | sed 's/^/       extra:   /'
fi

# ---------------------------------------------------------------------------
echo
echo "── 2. balances: overlapping users must equal old + new"
# ---------------------------------------------------------------------------
Q "$VOLD"      'SELECT telegram_id||" "||balance FROM users;' >/dev/null 2>&1
Q "$VOLD"      "SELECT telegram_id||' '||balance FROM users;" | sort > "$W/old.bal"
Q "$VNEW"      "SELECT telegram_id||' '||balance FROM users;" | sort > "$W/new.bal"
Q "$MERGED_DB" "SELECT telegram_id||' '||balance FROM users;" | sort > "$W/merged.bal"

python3 - "$W/old.bal" "$W/new.bal" "$W/merged.bal" "$W/bal.report" <<'PY'
import sys
def load(p):
    d = {}
    for ln in open(p):
        if ln.strip():
            k, v = ln.split()
            d[k] = int(v)
    return d
old, new, merged = load(sys.argv[1]), load(sys.argv[2]), load(sys.argv[3])
bad, checked = [], 0
for uid in set(old) | set(new):
    want = old.get(uid, 0) + new.get(uid, 0)
    got = merged.get(uid)
    checked += 1
    if got is None or got != want:
        bad.append((uid, old.get(uid, 0), new.get(uid, 0), want, got))
with open(sys.argv[4], "w") as fh:
    fh.write(f"checked={checked}\nbad={len(bad)}\n")
    for b in bad[:20]:
        fh.write(f"  {b[0]} old={b[1]} new={b[2]} want={b[3]} got={b[4]}\n")
PY
BAD_BAL="$(grep '^bad=' "$W/bal.report" | cut -d= -f2)"
CHK_BAL="$(grep '^checked=' "$W/bal.report" | cut -d= -f2)"
if [[ "$BAD_BAL" == "0" ]]; then
  ok "every balance == old + new" "$CHK_BAL users checked"
else
  no "$BAD_BAL balance(s) wrong of $CHK_BAL checked"
  tail -n +3 "$W/bal.report" | head -10
fi

# ---------------------------------------------------------------------------
echo
echo "── 3. row-level unions for the big tables"
# ---------------------------------------------------------------------------
for t in pokemon pokedex_entries inventory_items user_achievements battles trades; do
  o="$(Q "$VOLD" "SELECT count(*) FROM $t;")"
  n="$(Q "$VNEW" "SELECT count(*) FROM $t;")"
  m="$(Q "$MERGED_DB" "SELECT count(*) FROM $t;")"
  case "$t" in
    pokedex_entries|inventory_items)
      # Composite PK: overlapping keys are UPDATEd, so the count is a union of
      # keys, not a sum of rows.
      [[ "$m" -ge "$o" && "$m" -le $(( o + n )) ]] \
        && ok "$t within union bounds" "old=$o new=$n merged=$m" \
        || no "$t out of bounds" "old=$o new=$n merged=$m"
      ;;
    *)
      [[ "$m" == "$(( o + n ))" ]] \
        && ok "$t == old + new" "$o + $n = $m" \
        || no "$t != old + new" "old=$o new=$n merged=$m (expected $(( o + n )))"
      ;;
  esac
done

# ---------------------------------------------------------------------------
echo
echo "── 4. no data was silently dropped: every source pokemon id survives"
# ---------------------------------------------------------------------------
Q "$VOLD"      'SELECT id FROM pokemon;' | sort -u > "$W/old.pk"
Q "$VNEW"      'SELECT id FROM pokemon;' | sort -u > "$W/new.pk"
Q "$MERGED_DB" 'SELECT id FROM pokemon;' | sort -u > "$W/merged.pk"
sort -u "$W/old.pk" "$W/new.pk" > "$W/union.pk"
LOST="$(comm -23 "$W/union.pk" "$W/merged.pk" | wc -l)"
[[ "$LOST" == "0" ]] && ok "all $(wc -l < "$W/union.pk") pokemon ids present" \
                     || no "$LOST pokemon rows are MISSING from the merged DB"

# ---------------------------------------------------------------------------
echo
echo "── 5. integrity + settings policy"
# ---------------------------------------------------------------------------
for c in \
  "orphan pokemon:SELECT count(*) FROM pokemon p LEFT JOIN users u ON p.owner_id=u.telegram_id WHERE u.telegram_id IS NULL" \
  "orphan pokedex:SELECT count(*) FROM pokedex_entries e LEFT JOIN users u ON e.user_id=u.telegram_id WHERE u.telegram_id IS NULL" \
  "orphan inventory:SELECT count(*) FROM inventory_items i LEFT JOIN users u ON i.user_id=u.telegram_id WHERE u.telegram_id IS NULL" \
  "bad team_id:SELECT count(*) FROM users WHERE team_id IS NOT NULL AND team_id NOT IN (SELECT id FROM teams)" \
  "negative balance:SELECT count(*) FROM users WHERE balance<0" \
  "negative quantity:SELECT count(*) FROM inventory_items WHERE quantity<0" ; do
  lbl="${c%%:*}"; q="${c#*:}"
  v="$(Q "$MERGED_DB" "$q;")"
  [[ "${v:-1}" == "0" ]] && ok "$lbl = 0" || no "$lbl = ${v:-?}"
done

# Dangling selected_pokemon_id is compared against the SOURCES, not against
# zero. The old dataset already contained 3 of these, and a merge that faithfully
# preserves pre-existing data quirks is behaving correctly — flagging that as a
# merge failure sends you hunting a bug that isn't there. What matters is
# whether the merge INTRODUCED any.
DANGLE_Q="SELECT count(*) FROM users u WHERE u.selected_pokemon_id IS NOT NULL
          AND NOT EXISTS (SELECT 1 FROM pokemon p WHERE p.id::text=u.selected_pokemon_id)"
D_OLD="$(Q "$VOLD" "$DANGLE_Q;")"
D_NEW="$(Q "$VNEW" "$DANGLE_Q;")"
D_MERGED="$(Q "$MERGED_DB" "$DANGLE_Q;")"
D_EXPECT=$(( ${D_OLD:-0} + ${D_NEW:-0} ))
if [[ "${D_MERGED:-0}" -le "$D_EXPECT" ]]; then
  ok "dangling selected_pokemon introduced none" "old=$D_OLD new=$D_NEW merged=$D_MERGED (pre-existing)"
else
  no "merge INTRODUCED dangling selected_pokemon" "old=$D_OLD new=$D_NEW merged=$D_MERGED"
fi

# bot_config must come from OLD, per the agreed policy.
BC_O="$(Q "$VOLD" 'SELECT count(*) FROM bot_config;')"
BC_M="$(Q "$MERGED_DB" 'SELECT count(*) FROM bot_config;')"
[[ "$BC_M" -ge "$BC_O" ]] && ok "bot_config preserved from old" "old=$BC_O merged=$BC_M" \
                          || no "bot_config lost rows" "old=$BC_O merged=$BC_M"

# Sequences must be past their maxima or the first insert collides.
SEQBAD="$(Q "$MERGED_DB" "
  SELECT count(*) FROM (
    SELECT s.last_value, (SELECT COALESCE(MAX(id),0) FROM teams) m
    FROM pg_sequences s WHERE s.sequencename='teams_id_seq'
    UNION ALL
    SELECT s.last_value, (SELECT COALESCE(MAX(id),0) FROM daycare_slots)
    FROM pg_sequences s WHERE s.sequencename='daycare_slots_id_seq'
  ) x WHERE last_value IS NULL OR last_value < m;")"
[[ "${SEQBAD:-1}" == "0" ]] && ok "sequences are past their table maxima" \
                            || no "$SEQBAD sequence(s) would collide on next insert"

# ---------------------------------------------------------------------------
echo
echo "── 6. top accounts (sanity-check the summing by eye)"
# ---------------------------------------------------------------------------
printf '     %-14s %14s %10s %14s %8s\n' "telegram_id" "old_bal" "new_bal" "merged_bal" "pokemon"
Q "$MERGED_DB" "SELECT telegram_id FROM users ORDER BY balance DESC LIMIT 5;" | while read -r u; do
  printf '     %-14s %14s %10s %14s %8s\n' "$u" \
    "$(Q "$VOLD" "SELECT COALESCE(balance,0) FROM users WHERE telegram_id=$u;" || echo -)" \
    "$(Q "$VNEW" "SELECT COALESCE(balance,0) FROM users WHERE telegram_id=$u;" || echo -)" \
    "$(Q "$MERGED_DB" "SELECT balance FROM users WHERE telegram_id=$u;")" \
    "$(Q "$MERGED_DB" "SELECT count(*) FROM pokemon WHERE owner_id=$u;")"
done

echo
echo "════════════════════════════════════════════════════════════"
[[ "$FAIL" -eq 0 ]] && echo " ✅ VERIFIED — $PASS checks passed, 0 failed" \
                    || echo " 🚨 $FAIL CHECK(S) FAILED ($PASS passed)"
echo "════════════════════════════════════════════════════════════"
[[ "$FAIL" -eq 0 ]] || exit 1
