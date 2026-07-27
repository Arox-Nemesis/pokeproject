#!/usr/bin/env bash
# Nightly physical base backup.
#
# WAL segments alone are useless — you need a base backup to replay them onto.
# This produces the "onto" part. Run daily; PITR then reaches any second
# between the most recent base backup and now.
#
# -Xnone is deliberate: the WAL needed for recovery comes from the archive,
# not bundled into the tarball. Keeps base backups small and avoids storing
# the same segments twice.

SCRIPT_NAME=basebackup
# shellcheck disable=SC1091
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

acquire_lock
require_pg

STAMP="$(stamp)"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

log "starting base backup $STAMP"

# Record the WAL position before the backup so the restore path knows the
# earliest segment it needs. (backup_label inside the tar is authoritative;
# this is a convenience for humans reading the bucket.)
START_LSN="$(psql_q "SELECT pg_current_wal_lsn();")"

docker exec "$PG_CONTAINER" pg_basebackup \
  -U "$PG_USER" \
  -D - -Ft -Xnone -z -Z6 \
  --checkpoint=fast \
  --label="telemon-$STAMP" \
  > "$WORK/base-${STAMP}.tar.gz" \
  || die "pg_basebackup failed"

SIZE="$(stat -c %s "$WORK/base-${STAMP}.tar.gz")"
[[ "$SIZE" -gt 1024 ]] || die "base backup is only ${SIZE} bytes — refusing to upload a truncated backup"

END_LSN="$(psql_q "SELECT pg_current_wal_lsn();")"
PG_VERSION="$(psql_q "SHOW server_version;")"
ALEMBIC="$(psql_q "SELECT version_num FROM alembic_version LIMIT 1;" 2>/dev/null || echo unknown)"

cat > "$WORK/base-${STAMP}.json" <<EOF
{
  "stamp": "${STAMP}",
  "type": "basebackup",
  "start_lsn": "${START_LSN}",
  "end_lsn": "${END_LSN}",
  "server_version": "${PG_VERSION}",
  "alembic_version": "${ALEMBIC}",
  "size_bytes": ${SIZE},
  "wal_method": "none (segments come from the archive)"
}
EOF

log "base backup ${STAMP}: $((SIZE / 1048576)) MB, lsn ${START_LSN} -> ${END_LSN}"

push_to_all_remotes "$WORK" "base" \
  || die "base backup $STAMP produced but FAILED to upload to one or more remotes"

echo "$STAMP" > "$STATE_DIR/last_basebackup"
log "done"
