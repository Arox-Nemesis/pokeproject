#!/usr/bin/env bash
# Tier 2 — warm mirror via logical replication.
#
# ⚠ READ THIS FIRST ⚠
#
# THE MIRROR IS NOT A BACKUP. It replicates deletes. A TRUNCATE on the primary
# reaches the mirror in milliseconds. If you wipe the primary at 07:31, the
# mirror is wiped at 07:31 too.
#
# The mirror exists for exactly two things:
#   * fast failover if the primary's hardware/host dies
#   * one-command migration to a new server
#
# The append-only archive (wal/ + base/ in object storage) remains the thing
# that protects you from mistakes, bugs and attackers. Do not let the existence
# of a mirror make you complacent about it.
#
#   ops/mirror_setup.sh                       # uses MIRROR_URL from backup.env
#   ops/mirror_setup.sh --target 'postgresql://user:pw@host:5432/telemon'
#
# Works against any Postgres 16+ that accepts logical replication as a
# subscriber, including Neon's free tier.

SCRIPT_NAME=mirror_setup
# shellcheck disable=SC1091
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

TARGET="${MIRROR_URL:-}"
PUBLICATION="${MIRROR_PUBLICATION:-telemon_pub}"
SUBSCRIPTION="${MIRROR_SUBSCRIPTION:-telemon_sub}"
# How the SUBSCRIBER reaches the PRIMARY. Not the same as how you reach the
# primary from your laptop — it must resolve from the mirror's network.
PRIMARY_CONNINFO="${MIRROR_PRIMARY_CONNINFO:-}"
SKIP_SCHEMA=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    --target)      TARGET="$2"; shift 2 ;;
    --from)        PRIMARY_CONNINFO="$2"; shift 2 ;;
    --skip-schema) SKIP_SCHEMA=1; shift ;;
    *) die "unknown argument: $1" ;;
  esac
done

[[ -n "$TARGET" ]] || die "no mirror target. Set MIRROR_URL in ops/backup.env or pass --target"
[[ -n "$PRIMARY_CONNINFO" ]] || die "no primary conninfo. Set MIRROR_PRIMARY_CONNINFO in ops/backup.env — this is how the MIRROR dials back to this primary, so it must be an address reachable from the mirror's network (a public host:port, not 'localhost')."

require_pg

# Run psql against the mirror, using the primary's client binary so we don't
# depend on a psql being installed on this host.
# No `-i`, and stdin closed: `-i` keeps stdin attached, so a psql invocation
# under a script whose stdin is a terminal or pipe can block indefinitely and
# ignore Ctrl-C. Bounded too, so a wedged mirror cannot hang the run.
psql_target() {
  timeout "${MIRROR_PSQL_TIMEOUT:-60}" docker exec "$PG_CONTAINER" psql "$TARGET" -Atq -c "$1" </dev/null
}

# ---------------------------------------------------------------------------
# Preflight
# ---------------------------------------------------------------------------
[[ "$(psql_q 'SHOW wal_level;')" == "logical" ]] \
  || die "primary wal_level is not 'logical'. Logical replication is impossible without it."

psql_target "SELECT 1" >/dev/null 2>&1 || die "cannot connect to the mirror at the given target"

TGT_VER="$(psql_target 'SHOW server_version_num;')"
[[ "$TGT_VER" -ge 160000 ]] || log "warn: mirror is server_version_num=$TGT_VER; 16+ recommended"

# Tables with no primary key cannot replicate UPDATE/DELETE unless their
# replica identity is FULL. Catch that here rather than at 3am.
NOPK="$(psql_q "
  SELECT COALESCE(string_agg(c.relname, ', '), '')
  FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
  WHERE n.nspname='public' AND c.relkind='r' AND c.relreplident='d'
    AND NOT EXISTS (SELECT 1 FROM pg_constraint k WHERE k.conrelid=c.oid AND k.contype='p');")"
if [[ -n "$NOPK" ]]; then
  die "these tables have no primary key and default replica identity, so UPDATE/DELETE will fail to replicate: $NOPK. Fix with: ALTER TABLE <t> REPLICA IDENTITY FULL;"
fi
log "preflight ok: every table has a usable replica identity"

# ---------------------------------------------------------------------------
# 1. Publication on the primary
# ---------------------------------------------------------------------------
if [[ "$(psql_q "SELECT count(*) FROM pg_publication WHERE pubname='$PUBLICATION';")" == "0" ]]; then
  log "creating publication $PUBLICATION on the primary"
  psql_q "CREATE PUBLICATION $PUBLICATION FOR ALL TABLES;" >/dev/null
else
  log "publication $PUBLICATION already exists"
fi

# ---------------------------------------------------------------------------
# 2. Schema on the mirror
# ---------------------------------------------------------------------------
# Logical replication copies ROWS, never DDL. The mirror must already have the
# tables, or CREATE SUBSCRIPTION fails / silently syncs nothing.
if [[ "$SKIP_SCHEMA" -eq 0 ]]; then
  log "copying schema to the mirror (structure only, no rows)"
  docker exec "$PG_CONTAINER" pg_dump -U "$PG_USER" -d "$PG_DB" \
      --schema-only --no-owner --no-privileges \
    | docker exec -i "$PG_CONTAINER" psql "$TARGET" -v ON_ERROR_STOP=0 >/dev/null 2>&1 \
    || log "note: some schema statements were skipped (usually objects that already exist)"
fi

# Build a quoted SQL list. printf per-element rather than IFS games: "${a[*]}"
# joins on only the FIRST character of IFS, which silently produces invalid SQL.
TABLE_SQL_LIST="$(printf "'%s'," "${USER_TABLES[@]}")"
TABLE_SQL_LIST="${TABLE_SQL_LIST%,}"

MISSING="$(psql_target "
  SELECT count(*) FROM unnest(ARRAY[$TABLE_SQL_LIST]) AS t
  WHERE NOT EXISTS (
    SELECT 1 FROM information_schema.tables
    WHERE table_schema='public' AND table_name=t);" 2>/dev/null || echo "?")"
[[ "$MISSING" == "0" ]] || die "the mirror is missing $MISSING expected table(s) after the schema copy — refusing to create a subscription that would silently sync nothing"
log "mirror schema verified"

# ---------------------------------------------------------------------------
# 3. Subscription on the mirror
# ---------------------------------------------------------------------------
if [[ "$(psql_target "SELECT count(*) FROM pg_subscription WHERE subname='$SUBSCRIPTION';")" == "0" ]]; then
  log "creating subscription $SUBSCRIPTION on the mirror (this starts the initial data copy)"
  psql_target "
    CREATE SUBSCRIPTION $SUBSCRIPTION
    CONNECTION '$PRIMARY_CONNINFO'
    PUBLICATION $PUBLICATION
    WITH (copy_data = true, streaming = on);" >/dev/null \
    || die "CREATE SUBSCRIPTION failed. Check that MIRROR_PRIMARY_CONNINFO is reachable FROM the mirror."
else
  log "subscription $SUBSCRIPTION already exists"
fi

# ---------------------------------------------------------------------------
# 4. Wait for initial sync
# ---------------------------------------------------------------------------
log "waiting for initial table sync..."
for _ in $(seq 1 120); do
  PENDING="$(psql_target "SELECT count(*) FROM pg_subscription_rel WHERE srsubstate <> 'r';" 2>/dev/null || echo "?")"
  [[ "$PENDING" == "0" ]] && break
  sleep 2
done

echo
echo "════════════════════════════════════════════════════════════"
echo " MIRROR ONLINE"
echo "════════════════════════════════════════════════════════════"
printf ' %-24s %s\n' "tables syncing" "$(psql_target "SELECT count(*) FROM pg_subscription_rel WHERE srsubstate <> 'r';")"
printf ' %-24s %s\n' "tables replicating" "$(psql_target "SELECT count(*) FROM pg_subscription_rel WHERE srsubstate = 'r';")"
for t in users pokemon inventory_items; do
  printf ' %-24s primary=%s mirror=%s\n' "$t" \
    "$(psql_q "SELECT count(*) FROM public.$t;")" \
    "$(psql_target "SELECT count(*) FROM public.$t;")"
done
echo "────────────────────────────────────────────────────────────"
echo " ⚠ The mirror replicates DELETES. It is failover, not backup."
echo ""
echo " Ongoing duties:"
echo "   ops/mirror_status.sh              check lag + slot health"
echo "   ops/mirror_migrate.sh             run BEFORE every alembic upgrade"
echo "   ops/mirror_promote.sh             failover / server move"
echo ""
echo " ⚠ CRITICAL: if the mirror goes offline and stays offline, the"
echo "   replication slot on THIS primary retains WAL forever and pg_wal"
echo "   grows until the disk fills. ops/status.sh watches for this."
echo "════════════════════════════════════════════════════════════"

alert "✅ Telemon mirror online. Remember: it replicates deletes — the object-storage archive is still your real backup."
