#!/usr/bin/env bash
# Inspect an old raw PGDATA directory safely, and compare it to the live database.
#
# WHY A COPY IS NON-NEGOTIABLE
#
# These directories were taken from running servers (postmaster.pid is present),
# so Postgres will perform crash recovery on first start — which WRITES to the
# data directory. Pointing a container at the original would mutate the only
# copy of that data you have. This script therefore always works on a scratch
# copy and never touches the source.
#
#   ops/inspect_old_db.sh <path-to-old-PGDATA> [--keep]
#   ops/inspect_old_db.sh ../ProjectsOLD_Backup/Databases_AndConfigs/db_telemon_postgres
#
# Reports: databases present, schema revision, and row counts for every table,
# side by side with the live database, so you can see exactly what a merge
# would involve.

set -uo pipefail

OPS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck disable=SC1091
source "$OPS_DIR/tables.sh"

SRC="${1:-}"
KEEP=0
[[ "${2:-}" == "--keep" ]] && KEEP=1

[[ -n "$SRC" ]] || { echo "usage: $0 <path-to-old-PGDATA> [--keep]"; exit 1; }
[[ -d "$SRC" ]] || { echo "FATAL: $SRC is not a directory"; exit 1; }
[[ -f "$SRC/PG_VERSION" ]] || { echo "FATAL: $SRC has no PG_VERSION — not a PGDATA directory"; exit 1; }

SRC="$(cd "$SRC" && pwd)"
NAME="$(basename "$SRC")"
VER="$(cat "$SRC/PG_VERSION")"
CONTAINER="telemon_inspect_$$"
WORK="$(mktemp -d /tmp/telemon-inspect-XXXXXX)"
PORT="${INSPECT_PORT:-5489}"

PG_CONTAINER=telemon_postgres
PG_USER=telemon
PG_DB=telemon

cleanup() {
  if [[ "$KEEP" -eq 1 ]]; then
    echo
    echo "--keep: container '$CONTAINER' left on port $PORT, data at $WORK"
    echo "  psql 'postgresql://postgres:inspect@localhost:$PORT/postgres'"
    echo "  tear down: docker rm -f $CONTAINER && rm -rf $WORK"
    return
  fi
  docker rm -f "$CONTAINER" >/dev/null 2>&1 || true
  rm -rf "$WORK"
}
trap cleanup EXIT

echo "════════════════════════════════════════════════════════════"
echo " INSPECTING OLD DATABASE"
echo "════════════════════════════════════════════════════════════"
echo " source     : $SRC"
echo " pg version : $VER"
echo " size       : $(du -sh "$SRC" 2>/dev/null | cut -f1)"
echo " scratch    : $WORK"
echo " ⚠ the source is treated as READ-ONLY; all work happens on a copy"
echo "════════════════════════════════════════════════════════════"

if [[ "$VER" != "16" ]]; then
  echo
  echo "⚠ This is Postgres $VER but the local image is 16. A major-version"
  echo "  mismatch means it cannot be mounted directly — you would need a"
  echo "  postgres:$VER image to read it, then pg_dump across."
  echo
fi

# ---------------------------------------------------------------------------
# Copy, then neutralise the crash state
# ---------------------------------------------------------------------------
echo
echo "copying PGDATA (this may take a moment)..."
cp -a "$SRC" "$WORK/pgdata" || { echo "FATAL: copy failed"; exit 1; }

# A stale pid file makes Postgres refuse to start.
rm -f "$WORK/pgdata/postmaster.pid"
# The old server's config may reference archive commands, replication slots or
# paths that do not exist here. Neutralise anything that could make startup fail
# or, worse, cause it to write to our real archive.
if [[ -f "$WORK/pgdata/postgresql.auto.conf" ]]; then
  cp "$WORK/pgdata/postgresql.auto.conf" "$WORK/auto.conf.orig"
  : > "$WORK/pgdata/postgresql.auto.conf"
fi
cat >> "$WORK/pgdata/postgresql.auto.conf" <<'EOF'
archive_mode = off
archive_command = ''
restore_command = ''
max_replication_slots = 0
EOF

chmod 700 "$WORK/pgdata"
chown -R 70:70 "$WORK/pgdata" 2>/dev/null || echo "warn: chown to uid 70 failed"

# ---------------------------------------------------------------------------
# Start it
# ---------------------------------------------------------------------------
echo "starting a throwaway Postgres $VER on port $PORT..."
docker rm -f "$CONTAINER" >/dev/null 2>&1 || true
docker run -d --name "$CONTAINER" \
  -v "$WORK/pgdata:/var/lib/postgresql/data" \
  -e POSTGRES_PASSWORD=inspect \
  -p "${PORT}:5432" \
  "postgres:${VER}-alpine" >/dev/null 2>&1 \
  || { echo "FATAL: could not start container"; exit 1; }

echo "waiting for crash recovery to finish..."
UP=0
for _ in $(seq 1 90); do
  if docker exec "$CONTAINER" pg_isready >/dev/null 2>&1; then UP=1; break; fi
  sleep 2
done

if [[ "$UP" -eq 0 ]]; then
  echo
  echo "FATAL: it did not come up. Container logs:"
  docker logs --tail 40 "$CONTAINER" 2>&1 | sed 's/^/  /'
  exit 1
fi
echo "✅ recovered and accepting connections"

# ---------------------------------------------------------------------------
# Find a superuser that actually exists.
# ---------------------------------------------------------------------------
# We are mounting a foreign PGDATA, so its roles are whatever the ORIGINAL
# cluster had — POSTGRES_USER does not create anything here. Assuming
# `postgres` and hiding the error makes a role mismatch look identical to an
# empty database, which is exactly the wrong conclusion to draw.
SU=""
for cand in postgres telemon pikamon root admin; do
  if docker exec "$CONTAINER" psql -U "$cand" -Atqc "SELECT 1" >/dev/null 2>&1; then
    SU="$cand"; break
  fi
done

if [[ -z "$SU" ]]; then
  echo
  echo "FATAL: could not connect as any of: postgres telemon pikamon root admin"
  echo
  echo "The exact error connecting as 'postgres':"
  docker exec "$CONTAINER" psql -U postgres -Atqc "SELECT 1" 2>&1 | sed 's/^/  /'
  echo
  echo "Roles visible on disk (from the filesystem, no connection needed):"
  du -sh "$WORK"/pgdata/base/*/ 2>/dev/null | sed 's/^/  /'
  echo
  echo "Try a specific superuser:  INSPECT_SUPERUSER=<name> $0 $SRC"
  exit 1
fi
[[ -n "${INSPECT_SUPERUSER:-}" ]] && SU="$INSPECT_SUPERUSER"
echo "   connecting as superuser: $SU"

Q()  { docker exec "$CONTAINER" psql -U "$SU" -Atq -c "$1" 2>/dev/null; }
QD() { docker exec "$CONTAINER" psql -U "$SU" -d "$2" -Atq -c "$1" 2>/dev/null; }
LIVE() { docker exec "$PG_CONTAINER" psql -U "$PG_USER" -d "$PG_DB" -Atq -c "$1" 2>/dev/null; }

# ---------------------------------------------------------------------------
# Filesystem cross-check: proves whether data exists independently of whether
# we can successfully query it.
# ---------------------------------------------------------------------------
echo
echo "── on-disk database directories (largest first)"
du -sh "$WORK"/pgdata/base/*/ 2>/dev/null | sort -rh | head -8 | sed 's/^/  /'

# ---------------------------------------------------------------------------
# What databases and roles exist?
# ---------------------------------------------------------------------------
echo
echo "── databases"
DBLIST="$(Q "SELECT datname||'  ('||pg_size_pretty(pg_database_size(datname))||')'
   FROM pg_database WHERE datistemplate=false ORDER BY datname;")"
if [[ -n "$DBLIST" ]]; then echo "$DBLIST" | sed 's/^/  /'; else echo "  (query returned nothing)"; fi

echo
echo "── roles"
ROLES="$(Q "SELECT rolname FROM pg_roles WHERE rolcanlogin ORDER BY 1;" | tr '\n' ' ')"
echo "  ${ROLES:-(query returned nothing)}"

echo
echo "── searching for game tables"

# Pick the database that actually holds game tables.
TARGET_DB=""
BEST_N=0
for cand in $(Q "SELECT datname FROM pg_database WHERE datistemplate=false ORDER BY datname;"); do
  n="$(QD "SELECT count(*) FROM information_schema.tables WHERE table_schema='public';" "$cand")"
  echo "  $cand: ${n:-0} public table(s)"
  if [[ "${n:-0}" -gt "$BEST_N" ]]; then BEST_N="${n:-0}"; TARGET_DB="$cand"; fi
done

if [[ -z "$TARGET_DB" || "$BEST_N" -eq 0 ]]; then
  echo
  echo "── no database has a populated public schema"
  # Distinguish "genuinely empty" from "we failed to read it". Conflating those
  # is how you throw away 677 MB of real data believing it was empty.
  ONDISK_MB="$(du -sm "$WORK/pgdata/base" 2>/dev/null | cut -f1)"
  echo "  base/ holds ${ONDISK_MB:-?} MB on disk"
  if [[ "${ONDISK_MB:-0}" -gt 100 ]]; then
    echo
    echo "  ⚠ CONTRADICTION: ${ONDISK_MB} MB of data files exist but no public"
    echo "    schema was found. This is NOT an empty cluster. Likely causes:"
    echo "      • the game tables live in a non-public schema"
    echo "      • the database was dropped but files remain"
    echo "      • we connected as a role that cannot see them"
    echo
    echo "  All schemas across all databases:"
    for cand in $(Q "SELECT datname FROM pg_database WHERE datistemplate=false;"); do
      echo "    $cand:"
      QD "SELECT '      '||nspname||' ('||count(c.oid)||' tables)'
          FROM pg_namespace n LEFT JOIN pg_class c ON c.relnamespace=n.oid AND c.relkind='r'
          WHERE nspname NOT LIKE 'pg_%' AND nspname<>'information_schema'
          GROUP BY nspname ORDER BY 1;" "$cand"
    done
    echo
    echo "  Inspect it yourself:  $0 $SRC --keep"
  else
    echo "  ✅ consistent with a genuinely empty cluster — nothing to merge"
  fi
  exit 0
fi

echo
echo "── game database: $TARGET_DB"
echo -n "  schema revision : "
QD "SELECT version_num FROM alembic_version LIMIT 1;" "$TARGET_DB" || echo "(no alembic_version table)"
echo -n "  public tables   : "
QD "SELECT count(*) FROM information_schema.tables WHERE table_schema='public';" "$TARGET_DB"

# ---------------------------------------------------------------------------
# The comparison that answers "is a merge worth doing?"
# ---------------------------------------------------------------------------
echo
echo "── row counts: OLD vs LIVE"
printf '  %-24s %12s %12s\n' "table" "OLD" "LIVE"
printf '  %-24s %12s %12s\n' "------------------------" "------------" "------------"
TOTAL_OLD=0
for t in "${USER_TABLES[@]}" "${REFERENCE_TABLES[@]}"; do
  o="$(QD "SELECT count(*) FROM public.${t};" "$TARGET_DB" 2>/dev/null || echo "-")"
  l="$(LIVE "SELECT count(*) FROM public.${t};" 2>/dev/null || echo "-")"
  [[ "$o" =~ ^[0-9]+$ ]] && TOTAL_OLD=$(( TOTAL_OLD + o ))
  flag=""
  [[ "$o" =~ ^[0-9]+$ && "$o" -gt 0 ]] && flag="  ←"
  printf '  %-24s %12s %12s%s\n' "$t" "$o" "$l" "$flag"
done

# ---------------------------------------------------------------------------
# Overlap analysis — the crux of whether a merge is easy or hard
# ---------------------------------------------------------------------------
echo
echo "── overlap analysis (this decides how hard a merge is)"
OLD_USERS="$(QD "SELECT count(*) FROM public.users;" "$TARGET_DB" 2>/dev/null || echo 0)"
if [[ "${OLD_USERS:-0}" -gt 0 ]]; then
  # `| sort` is essential: SQL ORDER BY on a bigint sorts NUMERICALLY, but comm
  # requires LEXICOGRAPHIC order and silently produces wrong counts otherwise.
  QD "SELECT telegram_id FROM public.users;" "$TARGET_DB" 2>/dev/null | sort -u > "$WORK/old_ids.txt"
  LIVE "SELECT telegram_id FROM public.users;" 2>/dev/null | sort -u > "$WORK/live_ids.txt"
  BOTH="$(comm -12 "$WORK/old_ids.txt" "$WORK/live_ids.txt" | wc -l)"
  ONLY_OLD="$(comm -23 "$WORK/old_ids.txt" "$WORK/live_ids.txt" | wc -l)"
  ONLY_LIVE="$(comm -13 "$WORK/old_ids.txt" "$WORK/live_ids.txt" | wc -l)"
  echo "  USERS"
  echo "    only in OLD  : $ONLY_OLD"
  echo "    only in LIVE : $ONLY_LIVE"
  echo "    in BOTH      : $BOTH"

  if [[ "$BOTH" -gt 0 ]]; then
    echo
    echo "    overlapping users, side by side (OLD vs LIVE):"
    printf '      %-14s %12s %12s %8s %8s\n' "telegram_id" "bal_OLD" "bal_LIVE" "pkmn_O" "pkmn_L"
    comm -12 "$WORK/old_ids.txt" "$WORK/live_ids.txt" | head -20 | while read -r uid; do
      co="$(QD "SELECT COALESCE(balance,-1) FROM public.users WHERE telegram_id=$uid;" "$TARGET_DB" 2>/dev/null || echo '?')"
      cl="$(LIVE "SELECT COALESCE(balance,-1) FROM public.users WHERE telegram_id=$uid;" 2>/dev/null || echo '?')"
      po="$(QD "SELECT count(*) FROM public.pokemon WHERE owner_id=$uid;" "$TARGET_DB" 2>/dev/null || echo '?')"
      pl="$(LIVE "SELECT count(*) FROM public.pokemon WHERE owner_id=$uid;" 2>/dev/null || echo '?')"
      printf '      %-14s %12s %12s %8s %8s\n' "$uid" "${co:-?}" "${cl:-?}" "${po:-?}" "${pl:-?}"
    done
  fi

  # Groups matter as much as users: chat_id is the PK, and the same Telegram
  # group appearing in both means per-group settings would conflict.
  QD "SELECT chat_id FROM public.groups;" "$TARGET_DB" 2>/dev/null | sort -u > "$WORK/old_g.txt"
  LIVE "SELECT chat_id FROM public.groups;" 2>/dev/null | sort -u > "$WORK/live_g.txt"
  echo
  echo "  GROUPS"
  echo "    only in OLD  : $(comm -23 "$WORK/old_g.txt" "$WORK/live_g.txt" | wc -l)"
  echo "    only in LIVE : $(comm -13 "$WORK/old_g.txt" "$WORK/live_g.txt" | wc -l)"
  echo "    in BOTH      : $(comm -12 "$WORK/old_g.txt" "$WORK/live_g.txt" | wc -l)"

  # Sequence-backed ids will collide unless remapped during the merge.
  echo
  echo "  SEQUENCE-BACKED IDS (collide unless remapped)"
  for spec in "teams:id" "daycare_slots:id"; do
    t="${spec%%:*}"; c="${spec##*:}"
    om="$(QD "SELECT COALESCE(MAX($c),0) FROM public.$t;" "$TARGET_DB" 2>/dev/null || echo '?')"
    lm="$(LIVE "SELECT COALESCE(MAX($c),0) FROM public.$t;" 2>/dev/null || echo '?')"
    printf '    %-18s max OLD=%-8s max LIVE=%s\n' "$t.$c" "$om" "$lm"
  done

  echo
  if [[ "$BOTH" -eq 0 ]]; then
    echo "  ✅ ZERO user overlap — the merge is mechanical, no conflict policy needed."
  else
    echo "  ⚠ $BOTH overlapping user(s) need an explicit per-field conflict policy."
  fi
else
  echo "  old database has no users — nothing to merge from it"
fi

echo
echo "════════════════════════════════════════════════════════════"
echo " SUMMARY: $NAME"
echo "════════════════════════════════════════════════════════════"
echo " game database   : $TARGET_DB"
echo " total rows in tracked tables : $TOTAL_OLD"
if [[ "$TOTAL_OLD" -eq 0 ]]; then
  echo " VERDICT: empty — nothing here to merge"
else
  echo " VERDICT: contains data — see the overlap analysis above"
fi
echo "════════════════════════════════════════════════════════════"
