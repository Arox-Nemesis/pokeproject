#!/usr/bin/env bash
# One-off / on-demand sync of the large static assets that live outside the
# database.
#
# These are NOT part of the automated backup schedule — they're ~380 MB, they
# change only when you run scripts/fetch_*.py, and they'd eat most of the free
# tier if versioned hourly. But a fresh server does need them, and re-fetching
# 1000+ artwork files from PokeAPI is slow and rate-limited.
#
# Run manually after a data refresh:
#   ops/sync_assets.sh

SCRIPT_NAME=sync_assets
# shellcheck disable=SC1091
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

acquire_lock

# Assets use their OWN remotes, not the archive remotes. A B2 application key
# scoped to telemon-archive cannot touch telemon-assets, so deriving the target
# from BACKUP_REMOTES (e.g. b2: -> b2:telemon-assets) fails with a 401.
REMOTES="${ASSET_REMOTES:-}"
if [[ -z "$REMOTES" ]]; then
  die "ASSET_REMOTES is not set in ops/backup.env. Assets live in a separate bucket with its own credentials — see ops/rclone.conf.example. Example: ASSET_REMOTES=\"b2assets:telemon-assets r2:telemon-assets\""
fi
log "asset remotes: $REMOTES"

# Note: `copy`, not `sync`. If you delete artwork locally we do NOT want that
# deletion mirrored — same principle as the WAL archive.
for target in official-artwork csv; do
  src="$PROJECT_DIR/data/$target"
  [[ -d "$src" ]] || { log "skipping $target (not present)"; continue; }
  log "syncing data/$target ($(du -sh "$src" | cut -f1))"
  for R in $REMOTES; do
    rc copy "$src" "${R}/$target" \
      --transfers 8 --checkers 16 --no-traverse \
      --retries 3 --log-level NOTICE \
      || log "ERROR: asset sync of $target to ${R} failed"
  done
done

# The small JSON files are cheap and change more often — include them all.
log "syncing data/*.json"
for R in $REMOTES; do
  rc copy "$PROJECT_DIR/data" "${R}/json" \
    --include "*.json" --max-depth 1 \
    --transfers 8 --no-traverse --log-level NOTICE \
    || log "ERROR: json sync to ${R} failed"
done

log "done"
