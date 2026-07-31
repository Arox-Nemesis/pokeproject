#!/usr/bin/env bash
# Merge the old (pre-incident) database with the new (post-incident) one.
#
# DIRECTION: OLD is the base. The old dataset (8294 users / 1.05M pokemon) is
# restored as the foundation and the new dataset (~340 users / ~2300 pokemon) is
# merged INTO it. Same end state as the reverse, but the million-row table never
# moves, so the operation is far smaller and far less risky.
#
# THIS SCRIPT DOES NOT GO LIVE. It builds and verifies a merged database
# alongside the current one and stops. Promotion is a separate, explicit step
# (--promote) so you can inspect everything first.
#
#   ops/merge_execute.sh <old-pgdata>          # backup, merge, verify. NO cutover.
#   ops/merge_execute.sh --promote             # swap merged -> live (after review)
#   ops/merge_execute.sh --rollback            # undo a promote
#
# MERGE POLICY (agreed):
#   balance, battle_wins/losses, total_evolutions, total_trades   SUM
#   battle_rating, daily_streak, shiny_hunt_chain                 GREATEST
#   last_daily, xp_boost_until                                    GREATEST
#   incense_spawns_remaining                                      GREATEST (session state, not goods)
#   username, first_name, last_name                               NEW wins (people rename)
#   settings, team_id, team_role, selected_pokemon_id             OLD wins
#   is_banned                                                     OR (fail safe)
#   created_at                                                    LEAST
#   pokedex_entries.times_caught                                  SUM; flags OR; first_caught_at LEAST
#   inventory_items.quantity                                      SUM
#   groups (all settings)                                         OLD wins; counters SUM
#   bot_config                                                    OLD wins
#   pokemon and all UUID-keyed history                            UNION

set -uo pipefail

OPS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck disable=SC1091
source "$OPS_DIR/tables.sh"

PG_CONTAINER=telemon_postgres
PG_USER=telemon
PG_DB=telemon
MERGED_DB=telemon_merged
OLD_CONTAINER=telemon_merge_src
OLD_SU=telemon
OLD_DB=telemon
BOT_CONTAINER=telemon_bot
BACKUP_DIR="${MERGE_BACKUP_DIR:-/var/lib/telemon-merge}"
SRC=""
MODE=merge

case "${1:-}" in
  --promote)  MODE=promote ;;
  --rollback) MODE=rollback ;;
  "")         echo "usage: $0 <old-pgdata> | --promote | --rollback"; exit 1 ;;
  *)          SRC="$1" ;;
esac

ts() { date -u +%Y%m%dT%H%M%SZ; }
log() { echo "[$(date -u +%H:%M:%S)] $*"; }
die() { echo; echo "FATAL: $*" >&2; exit 1; }

LQ()  { docker exec "$PG_CONTAINER" psql -U "$PG_USER" -d "$PG_DB" -Atq -c "$1" </dev/null 2>/dev/null; }
MQ()  { docker exec "$PG_CONTAINER" psql -U "$PG_USER" -d "$MERGED_DB" -Atq -c "$1" </dev/null 2>/dev/null; }
MQE() { docker exec "$PG_CONTAINER" psql -U "$PG_USER" -d "$MERGED_DB" -v ON_ERROR_STOP=1 -q -c "$1" </dev/null; }
PQ()  { docker exec "$PG_CONTAINER" psql -U "$PG_USER" -d postgres -Atq -c "$1" </dev/null 2>/dev/null; }
OQ()  { docker exec "$OLD_CONTAINER" psql -U "$OLD_SU" -d "$OLD_DB" -Atq -c "$1" </dev/null 2>/dev/null; }

# ===========================================================================
# PROMOTE / ROLLBACK
# ===========================================================================
if [[ "$MODE" == "promote" ]]; then
  [[ "$(PQ "SELECT count(*) FROM pg_database WHERE datname='$MERGED_DB';")" == "1" ]] \
    || die "$MERGED_DB does not exist — run the merge first"
  docker inspect -f '{{.State.Running}}' "$BOT_CONTAINER" 2>/dev/null | grep -q true \
    && die "the bot is running. Stop it first: docker compose stop bot"

  STAMP="$(ts)"
  echo "This will rename:"
  echo "  $PG_DB      ->  ${PG_DB}_premerge_${STAMP}   (your current database, kept)"
  echo "  $MERGED_DB  ->  $PG_DB                        (the merged database goes live)"
  read -r -p "Proceed? [type PROMOTE] " c
  [[ "$c" == "PROMOTE" ]] || { echo aborted; exit 1; }

  PQ "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname IN ('$PG_DB','$MERGED_DB') AND pid<>pg_backend_pid();" >/dev/null
  docker exec "$PG_CONTAINER" psql -U "$PG_USER" -d postgres -v ON_ERROR_STOP=1 -q -c \
    "ALTER DATABASE $PG_DB RENAME TO ${PG_DB}_premerge_${STAMP};" </dev/null || die "rename of live db failed"
  docker exec "$PG_CONTAINER" psql -U "$PG_USER" -d postgres -v ON_ERROR_STOP=1 -q -c \
    "ALTER DATABASE $MERGED_DB RENAME TO $PG_DB;" </dev/null \
    || { docker exec "$PG_CONTAINER" psql -U "$PG_USER" -d postgres -q -c "ALTER DATABASE ${PG_DB}_premerge_${STAMP} RENAME TO $PG_DB;" </dev/null; die "rename of merged db failed — rolled back"; }

  mkdir -p "$BACKUP_DIR"
  echo "${PG_DB}_premerge_${STAMP}" > "$BACKUP_DIR/last_promote"
  log "PROMOTED. Merged database is now '$PG_DB'."
  echo
  echo "NEXT:"
  echo "  docker compose up -d bot"
  echo "  ops/check_drift.sh"
  echo "  ops/basebackup.sh      # ← REQUIRED: the archive still describes the old DB"
  echo
  echo "Rollback if needed:  $0 --rollback"
  exit 0
fi

if [[ "$MODE" == "rollback" ]]; then
  PREV="$(cat "$BACKUP_DIR/last_promote" 2>/dev/null || true)"
  [[ -n "$PREV" ]] || die "no record of a promote to roll back"
  [[ "$(PQ "SELECT count(*) FROM pg_database WHERE datname='$PREV';")" == "1" ]] || die "$PREV no longer exists"
  docker inspect -f '{{.State.Running}}' "$BOT_CONTAINER" 2>/dev/null | grep -q true \
    && die "stop the bot first: docker compose stop bot"
  read -r -p "Restore '$PREV' as '$PG_DB'? [type ROLLBACK] " c
  [[ "$c" == "ROLLBACK" ]] || { echo aborted; exit 1; }
  PQ "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname IN ('$PG_DB','$PREV') AND pid<>pg_backend_pid();" >/dev/null
  docker exec "$PG_CONTAINER" psql -U "$PG_USER" -d postgres -v ON_ERROR_STOP=1 -q -c \
    "ALTER DATABASE $PG_DB RENAME TO ${MERGED_DB};" </dev/null || die "could not park the merged db"
  docker exec "$PG_CONTAINER" psql -U "$PG_USER" -d postgres -v ON_ERROR_STOP=1 -q -c \
    "ALTER DATABASE $PREV RENAME TO $PG_DB;" </dev/null || die "could not restore $PREV"
  log "ROLLED BACK. '$PREV' is live again as '$PG_DB'."
  exit 0
fi

# ===========================================================================
# MERGE
# ===========================================================================
[[ -n "$SRC" && -d "$SRC" ]] || die "old PGDATA path required"
SRC="$(cd "$SRC" && pwd)"
mkdir -p "$BACKUP_DIR"

echo "════════════════════════════════════════════════════════════"
echo " TELEMON DATABASE MERGE"
echo "════════════════════════════════════════════════════════════"
echo " old source : $SRC"
echo " backups    : $BACKUP_DIR"
echo " target     : database '$MERGED_DB' (built alongside '$PG_DB')"
echo " ⚠ this does NOT go live — promotion is a separate --promote step"
echo "════════════════════════════════════════════════════════════"
echo

# --- 0. preflight ----------------------------------------------------------
log "PHASE 0: preflight"
docker inspect -f '{{.State.Running}}' "$PG_CONTAINER" >/dev/null 2>&1 || die "$PG_CONTAINER not found"
LQ "SELECT 1" >/dev/null || die "cannot query the live database"

AVAIL_GB=$(( $(df -Pm / | awk 'NR==2{print $4}') / 1024 ))
log "  free disk: ${AVAIL_GB} GB"
[[ "$AVAIL_GB" -ge 8 ]] || die "need at least 8 GB free (old DB is 570MB, plus backups and the merged copy)"

log "  live users=$(LQ 'SELECT count(*) FROM users;') pokemon=$(LQ 'SELECT count(*) FROM pokemon;')"

# --- 1. stop the bot BEFORE snapshotting -----------------------------------
log "PHASE 1: stopping the bot (nothing may write during the merge)"
if docker inspect -f '{{.State.Running}}' "$BOT_CONTAINER" 2>/dev/null | grep -q true; then
  docker stop "$BOT_CONTAINER" >/dev/null && log "  bot stopped"
else
  log "  bot already stopped"
fi
# Postgres stays UP: we need it to perform the merge. With the bot down nothing
# writes, so the data is effectively frozen.

# --- 2. back up BOTH databases before touching anything --------------------
log "PHASE 2: backing up both databases"
STAMP="$(ts)"
NEW_BK="$BACKUP_DIR/new-preMerge-${STAMP}.pgc"
docker exec "$PG_CONTAINER" pg_dump -U "$PG_USER" -d "$PG_DB" -Fc -Z6 --no-owner --no-privileges \
  > "$NEW_BK" </dev/null || die "backup of the new database failed"
docker exec -i "$PG_CONTAINER" pg_restore -l < "$NEW_BK" >/dev/null || die "new-database backup does not validate"
log "  new database  -> $(basename "$NEW_BK") ($(du -h "$NEW_BK" | cut -f1)) ✅ validated"

# --- 3. bring up the old cluster on a copy ---------------------------------
log "PHASE 3: starting the old cluster (on a copy; source stays read-only)"
OLD_WORK="$BACKUP_DIR/old-pgdata-${STAMP}"
rm -rf "$OLD_WORK"; cp -a "$SRC" "$OLD_WORK" || die "copy of old PGDATA failed"
rm -f "$OLD_WORK/postmaster.pid"
: > "$OLD_WORK/postgresql.auto.conf"
printf 'archive_mode = off\narchive_command = %s\nrestore_command = %s\nmax_replication_slots = 0\n' "''" "''" \
  >> "$OLD_WORK/postgresql.auto.conf"
chmod 700 "$OLD_WORK"; chown -R 70:70 "$OLD_WORK" 2>/dev/null || true

docker rm -f "$OLD_CONTAINER" >/dev/null 2>&1 || true
docker run -d --name "$OLD_CONTAINER" -v "$OLD_WORK:/var/lib/postgresql/data" \
  -e POSTGRES_PASSWORD=merge postgres:16-alpine >/dev/null 2>&1 || die "old cluster would not start"
for _ in $(seq 1 90); do docker exec "$OLD_CONTAINER" pg_isready >/dev/null 2>&1 && break; sleep 2; done
docker exec "$OLD_CONTAINER" pg_isready >/dev/null 2>&1 || { docker logs --tail 20 "$OLD_CONTAINER"; die "old cluster did not become ready"; }

OLD_BK="$BACKUP_DIR/old-preMerge-${STAMP}.pgc"
docker exec "$OLD_CONTAINER" pg_dump -U "$OLD_SU" -d "$OLD_DB" -Fc -Z6 --no-owner --no-privileges \
  > "$OLD_BK" </dev/null || die "backup of the old database failed"
docker exec -i "$OLD_CONTAINER" pg_restore -l < "$OLD_BK" >/dev/null || die "old-database backup does not validate"
log "  old database  -> $(basename "$OLD_BK") ($(du -h "$OLD_BK" | cut -f1)) ✅ validated"
log "  old users=$(OQ 'SELECT count(*) FROM users;') pokemon=$(OQ 'SELECT count(*) FROM pokemon;')"

# Freshness. The old VPS auto-booted Docker and ran the bot again for several
# hours, so this copy must be the POST-reboot one or that activity is lost.
# These timestamps are the only way to tell.
log "  old data freshness:"
printf '    %-26s %s\n' "newest users.updated_at"  "$(OQ 'SELECT max(updated_at) FROM users;')"
printf '    %-26s %s\n' "newest users.created_at"  "$(OQ 'SELECT max(created_at) FROM users;')"
printf '    %-26s %s\n' "newest pokemon.caught_at" "$(OQ 'SELECT max(caught_at) FROM pokemon;' 2>/dev/null || OQ 'SELECT max(created_at) FROM pokemon;')"
printf '    %-26s %s\n' "newest battles"           "$(OQ 'SELECT max(created_at) FROM battles;' 2>/dev/null || echo n/a)"
log "  ^ if these predate the old VPS reboot, you are merging a STALE copy"

# --- 4. build the merged database from the OLD dump ------------------------
log "PHASE 4: creating '$MERGED_DB' from the old dataset"
PQ "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname='$MERGED_DB' AND pid<>pg_backend_pid();" >/dev/null 2>&1 || true
docker exec "$PG_CONTAINER" psql -U "$PG_USER" -d postgres -q -c "DROP DATABASE IF EXISTS $MERGED_DB;" </dev/null
docker exec "$PG_CONTAINER" psql -U "$PG_USER" -d postgres -v ON_ERROR_STOP=1 -q -c "CREATE DATABASE $MERGED_DB;" </dev/null \
  || die "could not create $MERGED_DB"

docker exec -i "$PG_CONTAINER" pg_restore -U "$PG_USER" -d "$MERGED_DB" --no-owner --no-privileges < "$OLD_BK" \
  >/dev/null 2>&1 || log "  (pg_restore reported non-fatal notices)"
[[ "$(MQ 'SELECT count(*) FROM users;')" == "$(OQ 'SELECT count(*) FROM users;')" ]] \
  || die "restored user count does not match the old database"
log "  restored: users=$(MQ 'SELECT count(*) FROM users;') pokemon=$(MQ 'SELECT count(*) FROM pokemon;')"

# --- 5. bring the merged schema to HEAD ------------------------------------
log "PHASE 5: bringing '$MERGED_DB' schema up to match live"
log "  before: $(MQ 'SELECT version_num FROM alembic_version;')"
LIVE_REV="$(LQ 'SELECT version_num FROM alembic_version;')"

# Attempt 1: alembic, via a one-off container. NOT `docker start telemon_bot` —
# starting the bot would put it back online mid-merge, polling Telegram and
# writing to the live DB.
#
# alembic/ is mounted from the host because the newest migration was copied
# into the running container, not baked into the image.
BOT_IMAGE="$(docker inspect -f '{{.Config.Image}}' "$BOT_CONTAINER" 2>/dev/null || echo telemon-bot)"
PROJECT_DIR="$(dirname "$OPS_DIR")"
ALEMBIC_OK=0
# --env-file is required, not optional: alembic/env.py imports telemon.config,
# which builds a pydantic Settings() with bot_token as a REQUIRED field. Passing
# DATABASE_URL alone fails validation before a single migration runs. The
# explicit -e after --env-file overrides DATABASE_URL (later wins in Docker), so
# we target the merged DB while inheriting everything else the app needs.
ALEMBIC_OUT="$(docker run --rm --network "${DOCKER_NETWORK:-telemon_default}" \
  --env-file "$PROJECT_DIR/.env" \
  -e DATABASE_URL="postgresql+asyncpg://${PG_USER}:telemon@postgres:5432/${MERGED_DB}" \
  -v "$PROJECT_DIR/alembic:/app/alembic:ro" \
  -v "$PROJECT_DIR/alembic.ini:/app/alembic.ini:ro" \
  "$BOT_IMAGE" alembic upgrade head 2>&1)" && ALEMBIC_OK=1

if [[ "$ALEMBIC_OK" -eq 1 ]]; then
  log "  alembic upgrade succeeded"
else
  # Do not hide the error — that turned a two-minute fix into a guessing game
  # the first time round.
  log "  alembic failed; output was:"
  echo "$ALEMBIC_OUT" | tail -15 | sed 's/^/      /'
  log "  falling back to a deterministic schema sync against the LIVE schema"

  # LIVE is the ground truth for what the schema must look like. Add whatever
  # columns it has that the merged DB lacks, using its exact type/nullability.
  ADDED=0
  while IFS='|' read -r tbl col typ nullable defval; do
    [[ -z "$tbl" ]] && continue
    stmt="ALTER TABLE public.\"$tbl\" ADD COLUMN IF NOT EXISTS \"$col\" $typ"
    [[ "$nullable" == "NO" ]] && stmt="$stmt NOT NULL"
    [[ -n "$defval" && "$defval" != "NULL" ]] && stmt="$stmt DEFAULT $defval"
    MQE "$stmt;" || die "could not add $tbl.$col to $MERGED_DB"
    log "    + $tbl.$col $typ"
    ADDED=$((ADDED+1))
  done < <(LQ "
    SELECT table_name||'|'||column_name||'|'||
           CASE WHEN data_type='character varying' AND character_maximum_length IS NOT NULL
                THEN 'varchar('||character_maximum_length||')'
                ELSE data_type END
           ||'|'||is_nullable||'|'||COALESCE(column_default,'NULL')
    FROM information_schema.columns
    WHERE table_schema='public'
    ORDER BY table_name, ordinal_position;" \
    | while IFS='|' read -r t c ty n d; do
        # Only emit columns the merged DB is actually missing. information_schema
        # is per-database, so this has to be checked with a second connection.
        if [[ -z "$(MQ "SELECT 1 FROM information_schema.columns
                        WHERE table_schema='public' AND table_name='$t' AND column_name='$c';")" ]]; then
          echo "$t|$c|$ty|$n|$d"
        fi
      done)

  # Backfill the one known data-bearing case, mirroring the real migration.
  MQE "UPDATE public.groups SET bot_joined_at = created_at WHERE bot_joined_at IS NULL;" 2>/dev/null || true
  MQE "UPDATE alembic_version SET version_num = '${LIVE_REV}';" \
    || die "could not stamp alembic_version to $LIVE_REV"
  log "  synced $ADDED column(s) and stamped alembic_version=$LIVE_REV"
fi

# Whichever path ran, the merged schema MUST match live exactly. This is the
# assertion that actually matters — without it a merge could silently produce a
# database the application cannot query. information_schema is per-database, so
# the two column lists are gathered separately and compared.
LQ "SELECT table_name||'.'||column_name FROM information_schema.columns
    WHERE table_schema='public' ORDER BY 1;" | sort > "$BACKUP_DIR/.live_cols"
MQ "SELECT table_name||'.'||column_name FROM information_schema.columns
    WHERE table_schema='public' ORDER BY 1;" | sort > "$BACKUP_DIR/.merged_cols"
MISSING_IN_MERGED="$(comm -23 "$BACKUP_DIR/.live_cols" "$BACKUP_DIR/.merged_cols")"
EXTRA_IN_MERGED="$(comm -13 "$BACKUP_DIR/.live_cols" "$BACKUP_DIR/.merged_cols")"
if [[ -n "$MISSING_IN_MERGED" ]]; then
  echo "$MISSING_IN_MERGED" | sed 's/^/    missing: /'
  die "the merged database is missing columns that live has — refusing to continue"
fi
[[ -n "$EXTRA_IN_MERGED" ]] && { echo "$EXTRA_IN_MERGED" | sed 's/^/    extra: /'; log "  (extra columns are harmless)"; }
log "  schema verified identical to live ($(wc -l < "$BACKUP_DIR/.live_cols") columns)"

AFTER_REV="$(MQ 'SELECT version_num FROM alembic_version;')"
LIVE_REV="$(LQ 'SELECT version_num FROM alembic_version;')"
log "  after : $AFTER_REV  (live is $LIVE_REV)"
[[ "$AFTER_REV" == "$LIVE_REV" ]] || die "merged schema ($AFTER_REV) != live schema ($LIVE_REV)"

# --- 6. stage the new dataset ---------------------------------------------
log "PHASE 6: staging the new dataset into schema 'stage'"
MQE "DROP SCHEMA IF EXISTS stage CASCADE; CREATE SCHEMA stage;"

MERGE_TABLES=(users groups pokemon pokemon_eggs daycare_slots inventory_items
              pokedex_entries user_achievements user_quests teams trades
              trade_history wonder_trades market_listings battles spawn_admins bot_config)

for t in "${MERGE_TABLES[@]}"; do
  # LIKE guarantees identical column order, so a positional COPY is safe.
  MQE "CREATE TABLE stage.$t (LIKE public.$t);"
  COLS="$(MQ "SELECT string_agg(quote_ident(column_name), ',' ORDER BY ordinal_position)
              FROM information_schema.columns WHERE table_schema='public' AND table_name='$t';")"
  [[ -n "$COLS" ]] || die "could not resolve columns for $t"
  docker exec "$PG_CONTAINER" psql -U "$PG_USER" -d "$PG_DB" -q \
      -c "\\copy (SELECT $COLS FROM public.$t) TO STDOUT" </dev/null \
    | docker exec -i "$PG_CONTAINER" psql -U "$PG_USER" -d "$MERGED_DB" -v ON_ERROR_STOP=1 -q \
      -c "\\copy stage.$t ($COLS) FROM STDIN" \
    || die "staging of $t failed"
  printf '  %-20s %s rows staged\n' "$t" "$(MQ "SELECT count(*) FROM stage.$t;")"
done

# --- 7. remap integer-sequence ids that would collide ---------------------
log "PHASE 7: remapping sequence-backed ids"
# Only teams.id and daycare_slots.id are integer sequences; everything else is
# a UUID and cannot collide.
NEW_TEAMS_WITH_MEMBERS="$(MQ "SELECT count(*) FROM stage.users WHERE team_id IS NOT NULL;")"
if [[ "${NEW_TEAMS_WITH_MEMBERS:-0}" != "0" ]]; then
  OFF="$(MQ 'SELECT COALESCE(MAX(id),0) FROM public.teams;')"
  MQE "UPDATE stage.teams SET id = id + $OFF;"
  MQE "UPDATE stage.users SET team_id = team_id + $OFF WHERE team_id IS NOT NULL;"
  log "  teams offset by $OFF"
else
  log "  teams: no new rows reference a team — no remap needed"
fi
OFF_D="$(MQ 'SELECT COALESCE(MAX(id),0) FROM public.daycare_slots;')"
MQE "UPDATE stage.daycare_slots SET id = id + $OFF_D;"
log "  daycare_slots offset by $OFF_D"

# Capture the overlap NOW, before the merge. Measuring it afterwards counts
# every staged user as "already present" (because the merge just inserted them),
# which collapses the expected total and reports a false failure.
PRE_OLD_U="$(MQ 'SELECT count(*) FROM public.users;')"
PRE_NEW_U="$(MQ 'SELECT count(*) FROM stage.users;')"
PRE_BOTH_U="$(MQ 'SELECT count(*) FROM stage.users s
                  WHERE EXISTS (SELECT 1 FROM public.users u WHERE u.telegram_id=s.telegram_id);')"
PRE_OLD_P="$(MQ 'SELECT count(*) FROM public.pokemon;')"
PRE_NEW_P="$(MQ 'SELECT count(*) FROM stage.pokemon;')"
log "  pre-merge: old=$PRE_OLD_U new=$PRE_NEW_U overlap=$PRE_BOTH_U -> expecting $(( PRE_OLD_U + PRE_NEW_U - PRE_BOTH_U ))"

# --- 8. THE MERGE ---------------------------------------------------------
log "PHASE 8: merging (single transaction — all or nothing)"
MERGE_SQL=$(cat <<'SQL'
BEGIN;

-- USERS: sum what accumulates, take the greater of levels/streaks, new name,
-- old settings/team/selection, fail-safe on bans, earliest signup.
INSERT INTO public.users SELECT * FROM stage.users
ON CONFLICT (telegram_id) DO UPDATE SET
  username                  = COALESCE(EXCLUDED.username, public.users.username),
  first_name                = COALESCE(EXCLUDED.first_name, public.users.first_name),
  last_name                 = COALESCE(EXCLUDED.last_name, public.users.last_name),
  balance                   = public.users.balance + EXCLUDED.balance,
  battle_wins               = public.users.battle_wins + EXCLUDED.battle_wins,
  battle_losses             = public.users.battle_losses + EXCLUDED.battle_losses,
  total_evolutions          = public.users.total_evolutions + EXCLUDED.total_evolutions,
  total_trades              = public.users.total_trades + EXCLUDED.total_trades,
  battle_rating             = GREATEST(public.users.battle_rating, EXCLUDED.battle_rating),
  daily_streak              = GREATEST(public.users.daily_streak, EXCLUDED.daily_streak),
  shiny_hunt_chain          = GREATEST(public.users.shiny_hunt_chain, EXCLUDED.shiny_hunt_chain),
  incense_spawns_remaining  = GREATEST(public.users.incense_spawns_remaining, EXCLUDED.incense_spawns_remaining),
  last_daily                = GREATEST(public.users.last_daily, EXCLUDED.last_daily),
  xp_boost_until            = GREATEST(public.users.xp_boost_until, EXCLUDED.xp_boost_until),
  shiny_hunt_species_id     = COALESCE(public.users.shiny_hunt_species_id, EXCLUDED.shiny_hunt_species_id),
  selected_pokemon_id       = COALESCE(public.users.selected_pokemon_id, EXCLUDED.selected_pokemon_id),
  team_id                   = COALESCE(public.users.team_id, EXCLUDED.team_id),
  team_role                 = COALESCE(public.users.team_role, EXCLUDED.team_role),
  settings                  = EXCLUDED.settings || public.users.settings,
  is_banned                 = public.users.is_banned OR EXCLUDED.is_banned,
  ban_reason                = COALESCE(public.users.ban_reason, EXCLUDED.ban_reason),
  created_at                = LEAST(public.users.created_at, EXCLUDED.created_at),
  updated_at                = GREATEST(public.users.updated_at, EXCLUDED.updated_at);

-- TEAMS before anything that references them.
INSERT INTO public.teams SELECT * FROM stage.teams ON CONFLICT (id) DO NOTHING;

-- GROUPS: old settings win outright; only counters accumulate.
INSERT INTO public.groups SELECT * FROM stage.groups
ON CONFLICT (chat_id) DO UPDATE SET
  title         = COALESCE(EXCLUDED.title, public.groups.title),
  username      = COALESCE(EXCLUDED.username, public.groups.username),
  total_spawns  = public.groups.total_spawns + EXCLUDED.total_spawns,
  total_catches = public.groups.total_catches + EXCLUDED.total_catches,
  message_count = GREATEST(public.groups.message_count, EXCLUDED.message_count),
  bot_joined_at = LEAST(public.groups.bot_joined_at, EXCLUDED.bot_joined_at),
  is_banned     = public.groups.is_banned OR EXCLUDED.is_banned,
  updated_at    = GREATEST(public.groups.updated_at, EXCLUDED.updated_at);

-- POKEMON and all UUID-keyed history: pure union.
INSERT INTO public.pokemon           SELECT * FROM stage.pokemon           ON CONFLICT (id) DO NOTHING;
INSERT INTO public.pokemon_eggs      SELECT * FROM stage.pokemon_eggs      ON CONFLICT (id) DO NOTHING;
INSERT INTO public.daycare_slots     SELECT * FROM stage.daycare_slots     ON CONFLICT (id) DO NOTHING;
INSERT INTO public.user_achievements SELECT * FROM stage.user_achievements ON CONFLICT (id) DO NOTHING;
INSERT INTO public.user_quests       SELECT * FROM stage.user_quests       ON CONFLICT (id) DO NOTHING;
INSERT INTO public.trades            SELECT * FROM stage.trades            ON CONFLICT (id) DO NOTHING;
INSERT INTO public.trade_history     SELECT * FROM stage.trade_history     ON CONFLICT (id) DO NOTHING;
INSERT INTO public.wonder_trades     SELECT * FROM stage.wonder_trades     ON CONFLICT (id) DO NOTHING;
INSERT INTO public.market_listings   SELECT * FROM stage.market_listings   ON CONFLICT (id) DO NOTHING;
INSERT INTO public.battles           SELECT * FROM stage.battles           ON CONFLICT (id) DO NOTHING;
INSERT INTO public.spawn_admins      SELECT * FROM stage.spawn_admins      ON CONFLICT (user_id) DO NOTHING;

-- POKEDEX: counts add, flags OR, earliest first-catch survives.
INSERT INTO public.pokedex_entries SELECT * FROM stage.pokedex_entries
ON CONFLICT (user_id, species_id) DO UPDATE SET
  seen            = public.pokedex_entries.seen OR EXCLUDED.seen,
  caught          = public.pokedex_entries.caught OR EXCLUDED.caught,
  caught_shiny    = public.pokedex_entries.caught_shiny OR EXCLUDED.caught_shiny,
  times_caught    = public.pokedex_entries.times_caught + EXCLUDED.times_caught,
  first_caught_at = LEAST(public.pokedex_entries.first_caught_at, EXCLUDED.first_caught_at);

-- INVENTORY: quantities add.
INSERT INTO public.inventory_items SELECT * FROM stage.inventory_items
ON CONFLICT (user_id, item_id) DO UPDATE SET
  quantity   = public.inventory_items.quantity + EXCLUDED.quantity,
  updated_at = GREATEST(public.inventory_items.updated_at, EXCLUDED.updated_at);

-- BOT CONFIG: old wins, unconditionally.
INSERT INTO public.bot_config SELECT * FROM stage.bot_config ON CONFLICT (key) DO NOTHING;

COMMIT;
SQL
)
echo "$MERGE_SQL" | docker exec -i "$PG_CONTAINER" psql -U "$PG_USER" -d "$MERGED_DB" -v ON_ERROR_STOP=1 -q \
  || die "the merge transaction failed and was rolled back. '$PG_DB' is untouched."
log "  merge committed"

# --- 9. sequences --------------------------------------------------------
log "PHASE 9: advancing all sequences past their table maximums"
MQE "DO \$\$
DECLARE r RECORD; m BIGINT;
BEGIN
  FOR r IN SELECT c.relname t, a.attname c,
                  pg_get_serial_sequence(quote_ident(c.relname), a.attname) s
           FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
           JOIN pg_attribute a ON a.attrelid=c.oid AND a.attnum>0 AND NOT a.attisdropped
           WHERE n.nspname='public' AND c.relkind='r'
             AND pg_get_serial_sequence(quote_ident(c.relname), a.attname) IS NOT NULL
  LOOP
    EXECUTE format('SELECT COALESCE(MAX(%I),0) FROM %I', r.c, r.t) INTO m;
    PERFORM setval(r.s, GREATEST(m,1), true);
  END LOOP;
END \$\$;"
log "  done"

# --- 10. verification ----------------------------------------------------
log "PHASE 10: verification"
FAIL=()
GOT_U="$(MQ 'SELECT count(*) FROM public.users;')"
WANT_U=$(( PRE_OLD_U + PRE_NEW_U - PRE_BOTH_U ))
printf '  %-34s got=%-9s expected=%s\n' "users (old + new - overlap)" "$GOT_U" "$WANT_U"
[[ "$GOT_U" == "$WANT_U" ]] || FAIL+=("user count mismatch: $GOT_U vs expected $WANT_U")

GOT_P="$(MQ 'SELECT count(*) FROM public.pokemon;')"
WANT_P=$(( PRE_OLD_P + PRE_NEW_P ))
printf '  %-34s got=%-9s expected=%s\n' "pokemon (union)" "$GOT_P" "$WANT_P"
[[ "$GOT_P" == "$WANT_P" ]] || FAIL+=("pokemon count mismatch: $GOT_P vs expected $WANT_P")

for chk in \
  "orphan pokemon:SELECT count(*) FROM pokemon p LEFT JOIN users u ON p.owner_id=u.telegram_id WHERE u.telegram_id IS NULL" \
  "orphan pokedex:SELECT count(*) FROM pokedex_entries e LEFT JOIN users u ON e.user_id=u.telegram_id WHERE u.telegram_id IS NULL" \
  "orphan inventory:SELECT count(*) FROM inventory_items i LEFT JOIN users u ON i.user_id=u.telegram_id WHERE u.telegram_id IS NULL" \
  "bad team_id:SELECT count(*) FROM users u WHERE u.team_id IS NOT NULL AND NOT EXISTS (SELECT 1 FROM teams t WHERE t.id=u.team_id)" \
  "negative balance:SELECT count(*) FROM users WHERE balance < 0" \
  "negative quantity:SELECT count(*) FROM inventory_items WHERE quantity < 0" ; do
  lbl="${chk%%:*}"; q="${chk#*:}"
  v="$(MQ "$q;")"
  printf '  %-34s %s\n' "$lbl" "${v:-?}"
  [[ "${v:-1}" == "0" ]] || FAIL+=("$lbl = $v (expected 0)")
done

# Spot-check the summing actually happened on a known overlapping user.
SPOT="$(MQ "SELECT telegram_id FROM stage.users s WHERE EXISTS (SELECT 1 FROM stage.users) AND EXISTS
            (SELECT 1 FROM public.users u WHERE u.telegram_id=s.telegram_id AND u.balance>0)
            ORDER BY (SELECT balance FROM public.users u2 WHERE u2.telegram_id=s.telegram_id) DESC LIMIT 1;")"
if [[ -n "$SPOT" ]]; then
  echo
  echo "  spot-check user $SPOT:"
  printf '    old balance    %s\n' "$(OQ "SELECT balance FROM users WHERE telegram_id=$SPOT;")"
  printf '    new balance    %s\n' "$(MQ "SELECT balance FROM stage.users WHERE telegram_id=$SPOT;")"
  printf '    merged balance %s\n' "$(MQ "SELECT balance FROM public.users WHERE telegram_id=$SPOT;")"
  printf '    merged pokemon %s\n' "$(MQ "SELECT count(*) FROM pokemon WHERE owner_id=$SPOT;")"
fi

MQE "DROP SCHEMA stage CASCADE;"
docker rm -f "$OLD_CONTAINER" >/dev/null 2>&1 || true

echo
echo "════════════════════════════════════════════════════════════"
if [[ ${#FAIL[@]} -eq 0 ]]; then
  echo " ✅ MERGE BUILT AND VERIFIED — not yet live"
else
  echo " 🚨 ${#FAIL[@]} VERIFICATION FAILURE(S):"
  printf '   • %s\n' "${FAIL[@]}"
fi
echo "════════════════════════════════════════════════════════════"
echo " merged database : $MERGED_DB   (live '$PG_DB' is UNCHANGED)"
echo " users           : $GOT_U"
echo " pokemon         : $GOT_P"
echo " backups         : $BACKUP_DIR"
echo "                   $(basename "$NEW_BK")"
echo "                   $(basename "$OLD_BK")"
echo
echo " Inspect before promoting:"
echo "   docker exec -it $PG_CONTAINER psql -U $PG_USER -d $MERGED_DB"
echo
echo " When you are satisfied:"
echo "   ops/merge_execute.sh --promote"
echo "   docker compose up -d bot"
echo "   ops/basebackup.sh"
echo
echo " The bot is currently STOPPED and will stay stopped."
echo "════════════════════════════════════════════════════════════"
[[ ${#FAIL[@]} -eq 0 ]] || exit 1
