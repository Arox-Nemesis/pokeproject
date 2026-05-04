#!/usr/bin/env bash
# ============================================================================
# Rebuild Watchdog — polls Redis for a REBUILD_REQUESTED flag and triggers
# a docker compose rebuild when found.
#
# Usage (run on the HOST, not inside a container):
#   nohup ./scripts/rebuild_watchdog.sh &
#   # or in a tmux/screen session:
#   ./scripts/rebuild_watchdog.sh
#
# Requires: docker CLI, access to the telemon_redis container.
# ============================================================================

set -euo pipefail

POLL_INTERVAL=5                     # seconds between Redis checks
REDIS_CONTAINER="telemon_redis"     # name of the Redis container
COMPOSE_DIR="$(cd "$(dirname "$0")/.." && pwd)"  # project root

log() {
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*"
}

log "🔍 Rebuild watchdog started (polling every ${POLL_INTERVAL}s)"
log "   Compose dir : ${COMPOSE_DIR}"
log "   Redis container: ${REDIS_CONTAINER}"

while true; do
    # Check for the rebuild flag in Redis
    FLAG=$(docker exec "${REDIS_CONTAINER}" redis-cli GET REBUILD_REQUESTED 2>/dev/null || true)

    if [[ "${FLAG}" == "1" ]]; then
        log "🏗️  Rebuild requested! Clearing flag and rebuilding…"

        # Clear the flag immediately so we don't rebuild twice
        docker exec "${REDIS_CONTAINER}" redis-cli DEL REBUILD_REQUESTED >/dev/null 2>&1 || true

        # Rebuild and restart only the bot containers (pip cache is preserved)
        log "   Running: docker compose up -d --build bot"
        (cd "${COMPOSE_DIR}" && docker compose up -d --build bot) 2>&1 | while IFS= read -r line; do
            log "   ${line}"
        done

        log "✅ Rebuild complete — bots are restarting."
    fi

    sleep "${POLL_INTERVAL}"
done
