#!/usr/bin/env bash
# Pre-merge analysis: prove the two schemas are compatible and surface every
# obstacle BEFORE any data is written.
#
# Merging two live production datasets is the highest-risk operation in this
# whole project — much riskier than a restore, because a bad restore is
# revertible and a bad merge silently corrupts. So this script writes nothing.
# It only answers: "can these two be merged, and what will conflict?"
#
#   ops/merge_analyze.sh ../ProjectsOLD_Backup/Databases_AndConfigs/db_pikamon_postgres
#
# Run this before ops/merge_execute.sh.

set -uo pipefail

OPS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck disable=SC1091
source "$OPS_DIR/tables.sh"

SRC="${1:-}"
[[ -n "$SRC" && -d "$SRC" ]] || { echo "usage: $0 <path-to-old-PGDATA>"; exit 1; }
SRC="$(cd "$SRC" && pwd)"

PG_CONTAINER=telemon_postgres
PG_USER=telemon
PG_DB=telemon
OLD_CONTAINER="telemon_merge_src"
OLD_DB=telemon
OLD_SU=telemon
WORK="$(mktemp -d /tmp/telemon-merge-analyze-XXXXXX)"
BLOCKERS=()
WARNINGS=()

cleanup() { docker rm -f "$OLD_CONTAINER" >/dev/null 2>&1 || true; rm -rf "$WORK"; }
trap cleanup EXIT

echo "════════════════════════════════════════════════════════════"
echo " PRE-MERGE ANALYSIS  (read-only — writes nothing anywhere)"
echo "════════════════════════════════════════════════════════════"

# ---------------------------------------------------------------------------
# Bring up the old cluster on a copy
# ---------------------------------------------------------------------------
echo "copying old PGDATA..."
cp -a "$SRC" "$WORK/pgdata" || { echo "FATAL: copy failed"; exit 1; }
rm -f "$WORK/pgdata/postmaster.pid"
: > "$WORK/pgdata/postgresql.auto.conf"
cat >> "$WORK/pgdata/postgresql.auto.conf" <<'EOF'
archive_mode = off
archive_command = ''
restore_command = ''
max_replication_slots = 0
EOF
chmod 700 "$WORK/pgdata"; chown -R 70:70 "$WORK/pgdata" 2>/dev/null || true

docker rm -f "$OLD_CONTAINER" >/dev/null 2>&1 || true
docker run -d --name "$OLD_CONTAINER" -v "$WORK/pgdata:/var/lib/postgresql/data" \
  -e POSTGRES_PASSWORD=merge -p 5488:5432 postgres:16-alpine >/dev/null 2>&1

for _ in $(seq 1 90); do docker exec "$OLD_CONTAINER" pg_isready >/dev/null 2>&1 && break; sleep 2; done
docker exec "$OLD_CONTAINER" pg_isready >/dev/null 2>&1 || { echo "FATAL: old cluster did not start"; docker logs --tail 20 "$OLD_CONTAINER"; exit 1; }
echo "✅ old cluster up"

O()    { docker exec "$OLD_CONTAINER" psql -U "$OLD_SU" -d "$OLD_DB" -Atq -c "$1" 2>/dev/null; }
L()    { docker exec "$PG_CONTAINER" psql -U "$PG_USER" -d "$PG_DB" -Atq -c "$1" 2>/dev/null; }

# ---------------------------------------------------------------------------
# 1. Schema comparison — the merge is only safe if these agree
# ---------------------------------------------------------------------------
echo
echo "── 1. schema comparison"
COLQ="SELECT table_name||'.'||column_name||':'||data_type
      FROM information_schema.columns
      WHERE table_schema='public' ORDER BY 1;"
O "$COLQ" | sort > "$WORK/old_cols.txt"
L "$COLQ" | sort > "$WORK/live_cols.txt"

ONLY_OLD_COL="$(comm -23 "$WORK/old_cols.txt" "$WORK/live_cols.txt")"
ONLY_LIVE_COL="$(comm -13 "$WORK/old_cols.txt" "$WORK/live_cols.txt")"

if [[ -z "$ONLY_OLD_COL" && -z "$ONLY_LIVE_COL" ]]; then
  echo "  ✅ schemas are IDENTICAL ($(wc -l < "$WORK/old_cols.txt") columns)"
else
  [[ -n "$ONLY_LIVE_COL" ]] && {
    echo "  columns in LIVE but not OLD (expected: bot_joined_at from the new migration):"
    echo "$ONLY_LIVE_COL" | sed 's/^/    + /'
  }
  [[ -n "$ONLY_OLD_COL" ]] && {
    echo "  columns in OLD but not LIVE:"
    echo "$ONLY_OLD_COL" | sed 's/^/    - /'
    BLOCKERS+=("OLD has columns LIVE does not — data would be silently dropped on merge")
  }
fi

echo
echo "  alembic revision  OLD=$(O 'SELECT version_num FROM alembic_version LIMIT 1;')  LIVE=$(L 'SELECT version_num FROM alembic_version LIMIT 1;')"

# ---------------------------------------------------------------------------
# 2. Primary keys — these define every conflict rule
# ---------------------------------------------------------------------------
echo
echo "── 2. primary keys (define the merge conflict rules)"
for t in "${USER_TABLES[@]}"; do
  [[ "$t" == "alembic_version" ]] && continue
  pk="$(L "SELECT string_agg(a.attname, ',' ORDER BY k.ord)
          FROM pg_constraint c
          JOIN LATERAL unnest(c.conkey) WITH ORDINALITY AS k(attnum, ord) ON true
          JOIN pg_attribute a ON a.attrelid=c.conrelid AND a.attnum=k.attnum
          WHERE c.contype='p' AND c.conrelid='public.${t}'::regclass;")"
  printf '  %-22s %s\n' "$t" "${pk:-<none>}"
  [[ -z "$pk" ]] && BLOCKERS+=("table $t has no primary key — cannot merge deterministically")
done

# ---------------------------------------------------------------------------
# 3. Referential integrity of what we are about to move
# ---------------------------------------------------------------------------
echo
echo "── 3. referential integrity in LIVE (rows we will copy into the merged DB)"
ORPHAN_PK="$(L "SELECT count(*) FROM pokemon p LEFT JOIN users u ON p.owner_id=u.telegram_id WHERE u.telegram_id IS NULL;")"
printf '  %-40s %s\n' "pokemon with no owner" "${ORPHAN_PK:-?}"
[[ "${ORPHAN_PK:-0}" != "0" ]] && WARNINGS+=("LIVE has $ORPHAN_PK pokemon rows whose owner does not exist — they will be skipped")

ORPHAN_INV="$(L "SELECT count(*) FROM inventory_items i LEFT JOIN users u ON i.user_id=u.telegram_id WHERE u.telegram_id IS NULL;")"
printf '  %-40s %s\n' "inventory_items with no user" "${ORPHAN_INV:-?}"

BAD_ITEM="$(L "SELECT count(*) FROM inventory_items i LEFT JOIN items it ON i.item_id=it.id WHERE it.id IS NULL;")"
printf '  %-40s %s\n' "inventory_items with unknown item" "${BAD_ITEM:-?}"

BAD_SPECIES="$(L "SELECT count(*) FROM pokedex_entries e LEFT JOIN pokemon_species s ON e.species_id=s.national_dex WHERE s.national_dex IS NULL;")"
printf '  %-40s %s\n' "pokedex_entries w/ unknown species" "${BAD_SPECIES:-?}"

# ---------------------------------------------------------------------------
# 4. UUID collision check — pokemon.id is the one PK that spans both datasets
# ---------------------------------------------------------------------------
echo
echo "── 4. pokemon UUID collisions (should be zero — UUIDs are app-generated)"
O "SELECT id FROM pokemon;" | sort > "$WORK/old_pk.txt"
L "SELECT id FROM pokemon;" | sort > "$WORK/live_pk.txt"
DUP="$(comm -12 "$WORK/old_pk.txt" "$WORK/live_pk.txt" | wc -l)"
echo "  colliding pokemon UUIDs : $DUP"
[[ "$DUP" != "0" ]] && BLOCKERS+=("$DUP pokemon UUIDs exist in BOTH databases — merge would violate the primary key")

# ---------------------------------------------------------------------------
# 5. Reference data must match, or FKs break after the merge
# ---------------------------------------------------------------------------
echo
echo "── 5. reference data (must match; these are FK targets)"
for t in "${REFERENCE_TABLES[@]}"; do
  o="$(O "SELECT count(*) FROM public.$t;")"; l="$(L "SELECT count(*) FROM public.$t;")"
  same="✅"; [[ "$o" != "$l" ]] && { same="⚠"; WARNINGS+=("$t differs: OLD=$o LIVE=$l — verify FK targets exist for both datasets"); }
  printf '  %s %-22s OLD=%-8s LIVE=%s\n' "$same" "$t" "$o" "$l"
done

# ---------------------------------------------------------------------------
# 6. Overlap volumes — what the merge actually has to reconcile
# ---------------------------------------------------------------------------
echo
echo "── 6. merge volumes"
O "SELECT telegram_id FROM users;" | sort -u > "$WORK/ou.txt"
L "SELECT telegram_id FROM users;" | sort -u > "$WORK/lu.txt"
printf '  users   : OLD-only=%s  LIVE-only=%s  BOTH=%s\n' \
  "$(comm -23 "$WORK/ou.txt" "$WORK/lu.txt" | wc -l)" \
  "$(comm -13 "$WORK/ou.txt" "$WORK/lu.txt" | wc -l)" \
  "$(comm -12 "$WORK/ou.txt" "$WORK/lu.txt" | wc -l)"

O "SELECT chat_id FROM groups;" | sort -u > "$WORK/og.txt"
L "SELECT chat_id FROM groups;" | sort -u > "$WORK/lg.txt"
printf '  groups  : OLD-only=%s  LIVE-only=%s  BOTH=%s\n' \
  "$(comm -23 "$WORK/og.txt" "$WORK/lg.txt" | wc -l)" \
  "$(comm -13 "$WORK/og.txt" "$WORK/lg.txt" | wc -l)" \
  "$(comm -12 "$WORK/og.txt" "$WORK/lg.txt" | wc -l)"

# Composite-PK tables: how many rows will need UPDATE rather than INSERT
for spec in "pokedex_entries:user_id,species_id" "inventory_items:user_id,item_id"; do
  t="${spec%%:*}"; k="${spec##*:}"
  O "SELECT ${k//,/||'-'||} FROM $t;" | sort -u > "$WORK/o_$t.txt"
  L "SELECT ${k//,/||'-'||} FROM $t;" | sort -u > "$WORK/l_$t.txt"
  printf '  %-16s: OLD-only=%s  LIVE-only=%s  BOTH=%s\n' "$t" \
    "$(comm -23 "$WORK/o_$t.txt" "$WORK/l_$t.txt" | wc -l)" \
    "$(comm -13 "$WORK/o_$t.txt" "$WORK/l_$t.txt" | wc -l)" \
    "$(comm -12 "$WORK/o_$t.txt" "$WORK/l_$t.txt" | wc -l)"
done

# ---------------------------------------------------------------------------
# 7. Sequence ranges — live rows must be remapped above the old maximum
# ---------------------------------------------------------------------------
echo
echo "── 7. sequence-backed ids needing remap"
for spec in "teams:id" "daycare_slots:id"; do
  t="${spec%%:*}"; c="${spec##*:}"
  printf '  %-18s OLD max=%-8s LIVE max=%-8s LIVE rows=%s\n' "$t.$c" \
    "$(O "SELECT COALESCE(MAX($c),0) FROM $t;")" \
    "$(L "SELECT COALESCE(MAX($c),0) FROM $t;")" \
    "$(L "SELECT count(*) FROM $t;")"
done

# ---------------------------------------------------------------------------
# 8. selected_pokemon_id must still point at a real pokemon post-merge
# ---------------------------------------------------------------------------
echo
echo "── 8. dangling references"
printf '  %-44s %s\n' "LIVE users w/ selected_pokemon not in LIVE" \
  "$(L "SELECT count(*) FROM users u WHERE u.selected_pokemon_id IS NOT NULL
        AND NOT EXISTS (SELECT 1 FROM pokemon p WHERE p.id::text=u.selected_pokemon_id);")"
printf '  %-44s %s\n' "OLD users w/ selected_pokemon not in OLD" \
  "$(O "SELECT count(*) FROM users u WHERE u.selected_pokemon_id IS NOT NULL
        AND NOT EXISTS (SELECT 1 FROM pokemon p WHERE p.id::text=u.selected_pokemon_id);")"

# ---------------------------------------------------------------------------
# Verdict
# ---------------------------------------------------------------------------
echo
echo "════════════════════════════════════════════════════════════"
if [[ ${#BLOCKERS[@]} -eq 0 ]]; then
  echo " ✅ NO BLOCKERS — the merge can proceed"
else
  echo " 🚨 ${#BLOCKERS[@]} BLOCKER(S):"
  printf '   • %s\n' "${BLOCKERS[@]}"
fi
[[ ${#WARNINGS[@]} -gt 0 ]] && { echo; echo " ⚠ ${#WARNINGS[@]} warning(s):"; printf '   • %s\n' "${WARNINGS[@]}"; }
echo "════════════════════════════════════════════════════════════"
[[ ${#BLOCKERS[@]} -eq 0 ]] || exit 1
