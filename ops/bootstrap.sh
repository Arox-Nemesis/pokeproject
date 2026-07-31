#!/usr/bin/env bash
# Provision a fresh server from the archive. One command.
#
# This is the "move to a new VPS" and "the old server is gone" path. It pulls
# everything the bot needs that is NOT in git:
#
#   * the database        (base backup + WAL replay, or a logical dump)
#   * data/official-artwork, data/csv, data/*.json   (~380 MB, not in the DB)
#
# It deliberately does NOT restore .env. Your BOT_TOKEN is never put in the
# archive, so it cannot be leaked from a compromised bucket — copy it across by
# hand from your password manager.
#
#   ops/bootstrap.sh                     # newest available state
#   ops/bootstrap.sh --target-time "2026-07-26 07:30:00+00"
#   ops/bootstrap.sh --logical           # use a logical dump instead of PITR
#   ops/bootstrap.sh --skip-assets       # database only
#
# Run it from the project root of a fresh clone, with ops/backup.env and
# ops/rclone.conf already filled in.

SCRIPT_NAME=bootstrap
# shellcheck disable=SC1091
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

TARGET_TIME="latest"
MODE="pitr"
SKIP_ASSETS=0
REMOTE=""
ASSUME_YES=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    --target-time) TARGET_TIME="$2"; shift 2 ;;
    --logical)     MODE="logical"; shift ;;
    --skip-assets) SKIP_ASSETS=1; shift ;;
    --remote)      REMOTE="$2"; shift 2 ;;
    --yes)         ASSUME_YES=1; shift ;;
    *) die "unknown argument: $1" ;;
  esac
done

# Reads come from the uncapped remote (see resolve_restore_remote in lib.sh):
# defaulting to BACKUP_REMOTES[0] would read from B2, whose free tier caps
# downloads at 1 GB/day and reports the cap as "object not found".
[[ -n "$REMOTE" ]] || REMOTE="$(resolve_restore_remote)"
warn_if_download_capped "$REMOTE"

echo
echo "════════════════════════════════════════════════════════════"
echo " TELEMON BOOTSTRAP — provision this server from the archive"
echo "════════════════════════════════════════════════════════════"
echo " source remote : $REMOTE"
echo " db mode       : $MODE (target: $TARGET_TIME)"
echo " assets        : $([[ "$SKIP_ASSETS" -eq 1 ]] && echo skip || echo restore)"
echo " project dir   : $PROJECT_DIR"
echo "════════════════════════════════════════════════════════════"
echo

# ---------------------------------------------------------------------------
# 0. Preflight — fail before doing half the job
# ---------------------------------------------------------------------------
PRE_FAIL=()
command -v docker  >/dev/null || PRE_FAIL+=("docker is not installed")
command -v rclone  >/dev/null || PRE_FAIL+=("rclone is not installed (curl https://rclone.org/install.sh | sudo bash)")
docker compose version >/dev/null 2>&1 || PRE_FAIL+=("docker compose v2 is not available")
[[ -f "$PROJECT_DIR/.env" ]] || PRE_FAIL+=(".env is missing — copy it from your password manager (it is intentionally NOT in the archive)")
rc lsjson "${REMOTE}/base" --max-depth 1 >/dev/null 2>&1 || PRE_FAIL+=("cannot reach $REMOTE — check ops/rclone.conf")

if [[ ${#PRE_FAIL[@]} -gt 0 ]]; then
  echo "PREFLIGHT FAILED:"
  printf '  ✗ %s\n' "${PRE_FAIL[@]}"
  exit 1
fi
log "preflight passed"

if [[ -f "$PROJECT_DIR/.env" ]]; then
  grep -q '^BOT_TOKEN=.\+' "$PROJECT_DIR/.env" \
    || die ".env exists but BOT_TOKEN is empty — fill it in before bootstrapping"
fi

# Refuse to clobber an existing database unless told to.
if docker volume ls -q 2>/dev/null | grep -qx "telemon_postgres_data"; then
  # Distinguish "leftover empty volume" from "this is production". Bootstrap is
  # a FRESH-SERVER tool; running it against a live database replaces real data
  # and takes the bot offline for the duration.
  #
  # Postgres may not be up yet on a genuinely fresh host, so start it before
  # counting — otherwise a live database reads as empty and the strong
  # confirmation prompt is skipped exactly when it matters most.
  if ! pg_running; then
    ( cd "$PROJECT_DIR" && docker compose up -d postgres ) >/dev/null 2>&1 || true
    for _ in $(seq 1 30); do pg_running && break; sleep 2; done
  fi
  EXISTING_USERS="$(psql_q 'SELECT count(*) FROM users;' 2>/dev/null || echo 0)"
  EXISTING_USERS="$(echo "$EXISTING_USERS" | tr -cd '0-9')"
  EXISTING_USERS="${EXISTING_USERS:-0}"
  if [[ "$EXISTING_USERS" -gt 0 ]]; then
    echo "🚨 THIS SERVER ALREADY HAS A POPULATED DATABASE"
    echo
    echo "   users currently in it : $EXISTING_USERS"
    echo "   pokemon               : $(psql_q 'SELECT count(*) FROM pokemon;' 2>/dev/null || echo '?')"
    echo
    echo " bootstrap.sh provisions a FRESH server. Running it here will replace"
    echo " the above with archive contents and stop the bot while it works."
    echo
    echo " If you meant to recover this server rather than provision it, use:"
    echo "   ops/restore_pitr.sh --target-time '<when>'   # inspect first, then promote"
    echo
    echo " If the delayed standby is running, that is faster still:"
    echo "   ops/standby_freeze.sh"
    echo
    if [[ "$ASSUME_YES" -eq 0 ]]; then
      read -r -p " Replace $EXISTING_USERS users with archive contents? [type REPLACE-PRODUCTION] " c
      [[ "$c" == "REPLACE-PRODUCTION" ]] || { echo "aborted — nothing was changed"; exit 1; }
    fi
  else
    echo "⚠ A telemon_postgres_data volume already exists but holds no users."
    echo "  Bootstrapping will replace its contents."
    if [[ "$ASSUME_YES" -eq 0 ]]; then
      read -r -p "  Continue? [type BOOTSTRAP] " c
      [[ "$c" == "BOOTSTRAP" ]] || { echo "aborted"; exit 1; }
    fi
  fi
fi

# Stop the bot for the duration: restore_logical.sh refuses to run under a live
# writer (correctly), and discovering that after a 5-minute PITR download wastes
# the whole run.
BOT_C="${BOT_CONTAINER:-telemon_bot}"
BOT_WAS_RUNNING=0
if docker inspect -f '{{.State.Running}}' "$BOT_C" 2>/dev/null | grep -q true; then
  log "stopping $BOT_C for the duration of the restore"
  docker stop "$BOT_C" >/dev/null && BOT_WAS_RUNNING=1
fi

# Whatever happens next, do not leave the bot stopped.
restore_bot_on_exit() {
  local rc=$?
  if [[ "$BOT_WAS_RUNNING" -eq 1 ]]; then
    if docker inspect -f '{{.State.Running}}' "$BOT_C" 2>/dev/null | grep -q true; then
      :
    else
      echo
      echo "restarting $BOT_C (it was running before bootstrap started)"
      docker start "$BOT_C" >/dev/null 2>&1 || echo "WARNING: could not restart $BOT_C — start it manually"
    fi
  fi
  return $rc
}
trap restore_bot_on_exit EXIT

# ---------------------------------------------------------------------------
# 1. Assets (do these first — slowest, and independent of the DB)
# ---------------------------------------------------------------------------
if [[ "$SKIP_ASSETS" -eq 0 ]]; then
  # Assets have their own remote and credentials — a key scoped to the archive
  # bucket cannot read the assets bucket.
  ASSET_REMOTE="$(echo "${ASSET_REMOTES:-}" | awk '{print $1}')"
  if [[ -z "$ASSET_REMOTE" ]]; then
    log "skipping assets: ASSET_REMOTES not set in ops/backup.env"
    log "  the bot needs data/official-artwork to render Pokemon. Either set"
    log "  ASSET_REMOTES and re-run, or regenerate with scripts/fetch_artwork.py"
  else
    log "restoring static assets from $ASSET_REMOTE (~380 MB, this is the slow part)"
    for target in official-artwork csv; do
      if rc lsjson "${ASSET_REMOTE}/${target}" --max-depth 1 >/dev/null 2>&1; then
        log "  data/$target"
        rc copy "${ASSET_REMOTE}/${target}" "$PROJECT_DIR/data/$target" \
          --transfers 8 --checkers 16 --no-traverse --log-level NOTICE \
          || log "  warn: $target restore incomplete"
      else
        log "  skipping $target (not in the archive — run ops/sync_assets.sh on the old server)"
      fi
    done
    if rc lsjson "${ASSET_REMOTE}/json" --max-depth 1 >/dev/null 2>&1; then
      log "  data/*.json"
      rc copy "${ASSET_REMOTE}/json" "$PROJECT_DIR/data" --transfers 8 --no-traverse || true
    fi
  fi
else
  log "skipping assets"
fi

# ---------------------------------------------------------------------------
# 2. Bring up Postgres (empty) so we have somewhere to restore into
# ---------------------------------------------------------------------------
log "starting Postgres"
( cd "$PROJECT_DIR" && docker compose up -d postgres ) >/dev/null 2>&1 \
  || die "could not start Postgres"

for _ in $(seq 1 60); do
  pg_running && psql_q "SELECT 1" >/dev/null 2>&1 && break
  sleep 2
done
require_pg
log "Postgres is up"

# ---------------------------------------------------------------------------
# 3. Database
# ---------------------------------------------------------------------------
if [[ "$MODE" == "pitr" ]]; then
  log "recovering the database via PITR into a scratch instance"
  "$OPS_DIR/restore_pitr.sh" \
      --target-time "$TARGET_TIME" \
      --port 5494 \
      --container telemon_bootstrap_restore \
      --remote "$REMOTE" \
    || die "PITR recovery failed"

  log "loading the recovered data into the live database (full restore)"
  # --full, not the default user-tables-only path. On a fresh server the target
  # database is EMPTY: restoring only USER_TABLES would leave no schema and no
  # pokemon_species/moves/learnsets, and the bot would start against a database
  # it cannot query. Bootstrap always wants the whole thing.
  "$OPS_DIR/restore_logical.sh" --from-container telemon_bootstrap_restore --full --yes \
    || die "could not load the recovered data"

  docker rm -f telemon_bootstrap_restore >/dev/null 2>&1 || true
else
  log "restoring from the newest full logical dump"
  DUMP="$(rc lsf "${REMOTE}/full" --include '*.pgc' 2>/dev/null | sort | tail -1)"
  [[ -n "$DUMP" ]] || die "no full dump found at ${REMOTE}/full"
  log "using $DUMP"
  WORK="$(mktemp -d)"; trap 'rm -rf "$WORK"' EXIT
  rc copyto "${REMOTE}/full/${DUMP}" "$WORK/$DUMP" || die "download failed"
  docker exec -i "$PG_CONTAINER" pg_restore -U "$PG_USER" -d "$PG_DB" \
    --no-owner --no-privileges --clean --if-exists < "$WORK/$DUMP" \
    || log "warn: pg_restore reported errors (often harmless on a fresh database)"
fi

# ---------------------------------------------------------------------------
# 4. Verify before starting the bot
# ---------------------------------------------------------------------------
echo
log "verifying the restored database"
FAIL=()
ALEMBIC="$(psql_q 'SELECT version_num FROM alembic_version LIMIT 1;' 2>/dev/null || echo none)"
[[ "$ALEMBIC" == "none" ]] && FAIL+=("alembic_version is missing — the schema did not restore")

for t in "${GUARDED_TABLES[@]}"; do
  n="$(psql_q "SELECT count(*) FROM public.$t;" 2>/dev/null || echo "ERR")"
  printf ' %-22s %s\n' "$t" "$n"
  [[ "$n" == "ERR" ]] && FAIL+=("table $t is not queryable")
done

# Reference data is regenerable, but an empty species table means the bot
# cannot spawn anything, so flag it loudly rather than let it surprise you.
SPECIES="$(psql_q "SELECT count(*) FROM public.pokemon_species;" 2>/dev/null || echo 0)"
printf ' %-22s %s\n' "pokemon_species" "$SPECIES"
if [[ "$SPECIES" == "0" ]]; then
  log "note: pokemon_species is empty. If you bootstrapped from a user-only dump,"
  log "      repopulate reference data with: python scripts/seed_database.py"
fi

if [[ ${#FAIL[@]} -gt 0 ]]; then
  echo
  echo "VERIFICATION FAILED:"
  printf '  ✗ %s\n' "${FAIL[@]}"
  die "not starting the bot against a bad restore"
fi

# ---------------------------------------------------------------------------
# 5. Start everything, then check the app's own view of the schema
# ---------------------------------------------------------------------------
log "starting the full stack"
( cd "$PROJECT_DIR" && docker compose up -d ) >/dev/null 2>&1 || die "docker compose up failed"

sleep 15
"$OPS_DIR/check_drift.sh" || log "warn: schema drift detected — run alembic upgrade head"

echo
echo "════════════════════════════════════════════════════════════"
echo " ✅ BOOTSTRAP COMPLETE"
echo "════════════════════════════════════════════════════════════"
echo " schema revision : $ALEMBIC"
echo ""
echo " REMAINING STEPS — this server is not yet protected:"
echo "   1. sudo ops/install.sh        # schedule backups on THIS host"
echo "   2. ops/basebackup.sh          # anchor the archive to this server"
echo "   3. ops/status.sh              # confirm healthy"
echo "   4. ops/restore_drill.sh       # prove recovery works from here"
echo ""
echo " Until step 2 runs, this server has no fresh PITR anchor of its own."
echo "════════════════════════════════════════════════════════════"

alert "$(printf '✅ Telemon bootstrapped on a new host (schema %s). Run ops/install.sh and ops/basebackup.sh — this server is not protected until you do.' "$ALEMBIC")"
