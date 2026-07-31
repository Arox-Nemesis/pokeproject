#!/bin/sh
# WAL uploader sidecar.
#
# Drains /wal-spool to every configured rclone remote, then removes the local
# copy only once EVERY remote has accepted it.
#
# Safety properties:
#   * Uses `rclone copy` + local `rm`. Never `rclone sync`, never a remote
#     delete. Nothing this script can do will destroy archived history.
#   * Postgres writes segments via a temp name + atomic rename, so any file
#     without a `.tmp.` prefix is guaranteed complete. No min-age race.
#   * If the network is down, files pile up in the spool (a plain Docker
#     volume) rather than in pg_wal, so Postgres keeps serving the bot.
#
# See ops/README.md.

set -u

# Same config file the host scripts use, so BACKUP_REMOTES cannot drift
# between the uploader and the backup/restore tooling.
if [ -f /backup.env ]; then
  # shellcheck disable=SC1091
  . /backup.env
fi

REMOTES="${BACKUP_REMOTES:?BACKUP_REMOTES must be set in ops/backup.env}"
INTERVAL="${UPLOAD_INTERVAL_SECONDS:-15}"
SPOOL_WARN_BYTES="${SPOOL_WARN_BYTES:-1073741824}"
# Local WAL retention lives on its own volume, so the spool listing below
# can stay a simple "everything in this directory" without tripping over it.
RETAIN_HOURS="${LOCAL_WAL_RETAIN_HOURS:-30}"
SPOOL=/wal-spool
RETAIN=/wal-retain
STATE=/wal-spool/.uploader

mkdir -p "$STATE" "$RETAIN"

log() { echo "[wal-uploader] $(date -u +%Y-%m-%dT%H:%M:%SZ) $*"; }

# Telegram alert, rate-limited to once per hour per key so a sustained outage
# doesn't turn into a message flood.
alert() {
  key="$1"; shift
  msg="$*"
  log "ALERT($key): $msg"
  [ -z "${BOT_TOKEN:-}" ] && return 0
  [ -z "${ALERT_CHAT_ID:-}" ] && return 0
  stamp_file="$STATE/alert.$key"
  now=$(date +%s)
  if [ -f "$stamp_file" ]; then
    last=$(cat "$stamp_file" 2>/dev/null || echo 0)
    [ $((now - last)) -lt 3600 ] && return 0
  fi
  echo "$now" > "$stamp_file"
  wget -qO- --post-data="chat_id=${ALERT_CHAT_ID}&text=$(echo "$msg" | sed 's/ /%20/g')" \
    "https://api.telegram.org/bot${BOT_TOKEN}/sendMessage" >/dev/null 2>&1 || true
}

log "starting; remotes=[$REMOTES] interval=${INTERVAL}s"

while true; do
  # Complete segments only. The `.tmp.` prefix marks in-flight writes, and
  # dotfiles cover the .uploader state dir. Plain `ls` keeps this busybox-safe
  # (the rclone image has no GNU find -printf).
  LIST=$(mktemp)
  ( cd "$SPOOL" && ls -1 2>/dev/null | grep -v '^\.' ) > "$LIST" || true

  COUNT=$(wc -l < "$LIST" | tr -d ' ')

  if [ "$COUNT" -gt 0 ]; then
    ALL_OK=1
    for R in $REMOTES; do
      # B2 only: rclone HEADs the destination before copying, and on B2 a HEAD
      # is a Class B (download) transaction. Once the 1 GB/day free-tier
      # download quota is spent, uploads start failing with "403 failed to HEAD
      # for download" and WAL archiving silently stops. Segment names are
      # unique, so there is nothing to check against.
      #
      # NOT applied to R2/S3, where it suppresses multipart uploads and yields
      # "501 NotImplemented".
      EXTRA=""
      case "$R" in b2:*|*b2*) EXTRA="--no-check-dest" ;; esac
      if rclone copy "$SPOOL" "${R}/wal" \
           --files-from "$LIST" \
           $EXTRA \
           --transfers 8 --checkers 8 \
           --no-traverse --retries 3 --low-level-retries 5 \
           --log-level NOTICE 2>&1; then
        :
      else
        ALL_OK=0
        alert "upload_fail_$(echo "$R" | tr -c 'a-zA-Z0-9' '_')" \
          "Telemon WAL upload to ${R} FAILED. Segments are spooling locally; archive is now behind. Check the wal-uploader container."
      fi
    done

    if [ "$ALL_OK" -eq 1 ]; then
      # Every remote has it. Move to the local retention tier rather than
      # deleting outright — two things depend on it:
      #   * the time-delayed standby replays from here, so it needs no cloud
      #     access and creates no replication slot on the primary
      #   * a local PITR skips the download entirely (large RTO win)
      # Pruned by age below.
      mkdir -p "$RETAIN"
      while IFS= read -r f; do
        [ -n "$f" ] && mv -f "$SPOOL/$f" "$RETAIN/$f" 2>/dev/null
      done < "$LIST"
      log "uploaded $COUNT segment(s); moved to local retention"
      rm -f "$STATE"/alert.upload_fail_* 2>/dev/null || true
    else
      log "kept $COUNT segment(s) in spool; at least one remote failed"
    fi
  fi

  rm -f "$LIST"

  # Prune local WAL retention by age. This tier is a convenience (delayed
  # standby + fast local restore); the cloud archive remains authoritative, so
  # pruning here can never cost us history.
  find "$RETAIN" -maxdepth 1 -type f -mmin "+$((RETAIN_HOURS * 60))" \
    -exec rm -f {} + 2>/dev/null || true

  # Spool backpressure warning. If this grows unbounded the disk fills and
  # archive_command starts failing, which makes pg_wal grow -> outage.
  # busybox du has no -b; -sk is portable.
  SPOOL_KB=$(du -sk "$SPOOL" 2>/dev/null | cut -f1)
  SPOOL_BYTES=$(( ${SPOOL_KB:-0} * 1024 ))
  if [ "$SPOOL_BYTES" -gt "$SPOOL_WARN_BYTES" ]; then
    alert "spool_full" \
      "Telemon WAL spool is $((SPOOL_BYTES / 1048576)) MB and growing. Uploads are not keeping up. If the disk fills, Postgres WILL stall. Investigate now."
  fi

  sleep "$INTERVAL"
done
