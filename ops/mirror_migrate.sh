#!/usr/bin/env bash
# Run an Alembic migration safely while a logical-replication mirror is live.
#
# WHY THIS SCRIPT EXISTS
#
# Logical replication copies ROWS, never DDL. If you `alembic upgrade head` on
# the primary while a mirror is subscribed, the primary immediately starts
# sending rows shaped by the new schema to a mirror that still has the old one.
# Replication stops with an apply error and stays stopped. Your warm standby
# silently rots from that moment on.
#
# ORDERING RULE
#   additive DDL   (ADD COLUMN, CREATE TABLE, CREATE INDEX) -> MIRROR FIRST
#   destructive DDL (DROP COLUMN, DROP TABLE)               -> PRIMARY FIRST
#
# Additive-first is safe because a mirror with an extra nullable column keeps
# applying fine. Destructive-first is safe because a primary that has stopped
# sending a column is fine for a mirror that still has it (nullable).
#
# Telemon's migrations are overwhelmingly additive, so this script does
# mirror-first and then verifies. For a migration containing DROPs, use
# --primary-first.
#
#   ops/mirror_migrate.sh                  # normal: additive, mirror first
#   ops/mirror_migrate.sh --primary-first   # migration contains DROP/destructive DDL
#   ops/mirror_migrate.sh --revision <rev>  # target a specific revision

SCRIPT_NAME=mirror_migrate
# shellcheck disable=SC1091
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

acquire_lock
require_pg

TARGET="${MIRROR_URL:-}"
SUBSCRIPTION="${MIRROR_SUBSCRIPTION:-telemon_sub}"
BOT_CONTAINER="${BOT_CONTAINER:-telemon_bot}"
REVISION="head"
PRIMARY_FIRST=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    --primary-first) PRIMARY_FIRST=1; shift ;;
    --revision)      REVISION="$2"; shift 2 ;;
    *) die "unknown argument: $1" ;;
  esac
done

docker inspect -f '{{.State.Running}}' "$BOT_CONTAINER" 2>/dev/null | grep -q true \
  || die "container $BOT_CONTAINER is not running (it carries the alembic environment)"

# Alembic's env.py reads DATABASE_URL and needs the asyncpg driver form.
to_asyncpg() { echo "$1" | sed -E 's#^postgresql(\+[a-z]+)?://#postgresql+asyncpg://#'; }

alembic_on() {  # alembic_on <asyncpg-url> <args...>
  local url="$1"; shift
  docker exec -e DATABASE_URL="$url" "$BOT_CONTAINER" alembic "$@"
}

PRIMARY_ASYNC="$(docker exec "$BOT_CONTAINER" printenv DATABASE_URL 2>/dev/null || true)"
[[ -n "$PRIMARY_ASYNC" ]] \
  || die "could not read DATABASE_URL from $BOT_CONTAINER — cannot safely target the primary. (Hardcoding credentials here would break the moment you rotate the password.)"

# ---------------------------------------------------------------------------
# No mirror configured -> ordinary migration, nothing special to coordinate
# ---------------------------------------------------------------------------
if [[ -z "$TARGET" ]] || [[ "$(psql_q "SELECT count(*) FROM pg_publication;")" == "0" ]]; then
  log "no mirror configured — running a plain migration on the primary"
  alembic_on "$PRIMARY_ASYNC" upgrade "$REVISION" || die "alembic upgrade failed"
  log "primary now at: $(alembic_on "$PRIMARY_ASYNC" current 2>/dev/null | tail -1)"
  "$OPS_DIR/check_drift.sh" || die "schema drift remains after the migration"
  exit 0
fi

MIRROR_ASYNC="$(to_asyncpg "$TARGET")"
psql_target() { docker exec -i "$PG_CONTAINER" psql "$TARGET" -Atq -c "$1"; }
psql_target "SELECT 1" >/dev/null 2>&1 || die "cannot reach the mirror — fix that before migrating, or you will break replication"

# ---------------------------------------------------------------------------
# Preflight: mirror must be healthy and caught up BEFORE we change any schema
# ---------------------------------------------------------------------------
log "preflight: verifying the mirror is caught up"
"$OPS_DIR/mirror_status.sh" --quiet || die "the mirror is unhealthy. Migrating now would break replication further. Fix the mirror first."

BEFORE_PRIMARY="$(alembic_on "$PRIMARY_ASYNC" current 2>/dev/null | tail -1)"
BEFORE_MIRROR="$(alembic_on "$MIRROR_ASYNC" current 2>/dev/null | tail -1)"
log "primary at: ${BEFORE_PRIMARY:-none}"
log "mirror  at: ${BEFORE_MIRROR:-none}"

# Safety net before any DDL.
PRE="$STATE_DIR/pre-migration-$(stamp).pgc"
log "snapshotting the primary to $PRE"
docker exec "$PG_CONTAINER" pg_dump -U "$PG_USER" -d "$PG_DB" -Fc -Z6 \
  --no-owner --no-privileges > "$PRE" || die "pre-migration snapshot failed"

# ---------------------------------------------------------------------------
# Apply, in the correct order
# ---------------------------------------------------------------------------
if [[ "$PRIMARY_FIRST" -eq 1 ]]; then
  log "destructive mode: PRIMARY first, then mirror"
  alembic_on "$PRIMARY_ASYNC" upgrade "$REVISION" || die "alembic upgrade on the primary failed (rollback: $PRE)"
  alembic_on "$MIRROR_ASYNC"  upgrade "$REVISION" || die "alembic upgrade on the MIRROR failed — the primary is migrated but the mirror is not. Replication will break. Fix the mirror now."
else
  log "additive mode: MIRROR first, then primary"
  alembic_on "$MIRROR_ASYNC"  upgrade "$REVISION" || die "alembic upgrade on the mirror failed — the primary is untouched, so replication is still intact. Investigate the mirror."
  alembic_on "$PRIMARY_ASYNC" upgrade "$REVISION" || die "alembic upgrade on the primary failed (rollback: $PRE)"
fi

# ---------------------------------------------------------------------------
# A FOR ALL TABLES publication picks up new tables automatically, but the
# SUBSCRIBER will not replicate them until it refreshes.
# ---------------------------------------------------------------------------
log "refreshing the subscription so any new tables start replicating"
psql_target "ALTER SUBSCRIPTION $SUBSCRIPTION REFRESH PUBLICATION;" >/dev/null \
  || log "warn: REFRESH PUBLICATION failed — new tables may not replicate"

# ---------------------------------------------------------------------------
# Verify
# ---------------------------------------------------------------------------
AFTER_PRIMARY="$(alembic_on "$PRIMARY_ASYNC" current 2>/dev/null | tail -1)"
AFTER_MIRROR="$(alembic_on "$MIRROR_ASYNC" current 2>/dev/null | tail -1)"

echo
echo "════════ MIGRATION COMPLETE ════════"
printf ' primary : %s -> %s\n' "${BEFORE_PRIMARY:-none}" "$AFTER_PRIMARY"
printf ' mirror  : %s -> %s\n' "${BEFORE_MIRROR:-none}" "$AFTER_MIRROR"
echo "────────────────────────────────────"

[[ "$AFTER_PRIMARY" == "$AFTER_MIRROR" ]] \
  || die "primary and mirror are at DIFFERENT revisions after migrating. Replication will break. Reconcile immediately."

"$OPS_DIR/check_drift.sh" || die "schema drift remains after the migration"

log "waiting for replication to settle..."
sleep 5
"$OPS_DIR/mirror_status.sh" --quiet \
  || die "the mirror is unhealthy AFTER the migration — replication may have broken. Snapshot: $PRE"

log "primary and mirror both at $AFTER_PRIMARY, replication healthy"
alert "✅ Telemon migration applied to primary and mirror ($AFTER_PRIMARY). Replication healthy."
