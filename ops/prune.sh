#!/usr/bin/env bash
# Retention pruning (Grandfather-Father-Son).
#
# ⚠ READ THIS BEFORE USING ⚠
#
# This is the ONLY script in the system that deletes anything from the cloud.
# The whole design rests on the archive being append-only, so:
#
#   * Do NOT run this on the bot server with the server's credentials.
#     Run it from your laptop, with a separate delete-capable key.
#   * PREFERRED: don't use this script at all. Configure bucket lifecycle
#     rules (B2 "Lifecycle Settings", R2 "Object lifecycle rules") so the
#     provider expires old objects server-side. Then no delete credential
#     ever has to exist on any machine you control, and a compromised server
#     genuinely cannot destroy history.
#
# Recommended lifecycle rules, by prefix:
#
#     wal/          delete after  14 days
#     user/         delete after  14 days
#     full/         delete after  90 days
#     base/         delete after  30 days
#     quarantine/   NEVER auto-delete — these are your incident snapshots
#     assets/       NEVER auto-delete
#
# Those numbers give: second-granularity recovery for 2 weeks, daily
# granularity for a month, and a version-independent logical dump for a
# quarter. Long tail matters — the most dangerous corruption is the kind you
# don't notice for a week.

SCRIPT_NAME=prune
# shellcheck disable=SC1091
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

WAL_DAYS="${RETAIN_WAL_DAYS:-14}"
USER_DAYS="${RETAIN_USER_DAYS:-14}"
FULL_DAYS="${RETAIN_FULL_DAYS:-90}"
BASE_DAYS="${RETAIN_BASE_DAYS:-30}"

DRY_RUN=1
[[ "${1:-}" == "--execute" ]] && DRY_RUN=0

# ---------------------------------------------------------------------------
# Refuse to prune while an anomaly is unresolved
# ---------------------------------------------------------------------------
if [[ -f "$STATE_DIR/PRUNING_HALTED" ]]; then
  die "the anomaly gate is tripped. Pruning is frozen so that good backups cannot age out while an incident is unresolved. Investigate, then run ops/clear_anomaly.sh."
fi

# ---------------------------------------------------------------------------
# Refuse to prune if the most recent drill didn't pass
# ---------------------------------------------------------------------------
if [[ -f "$STATE_DIR/last_drill" ]]; then
  # shellcheck disable=SC1091
  source "$STATE_DIR/last_drill"
  [[ "${failures:-1}" == "0" ]] || die "the last restore drill failed. Refusing to delete old backups while the newer ones are unverified."
else
  die "no restore drill has ever passed. Refusing to delete anything until the backups are proven restorable."
fi

# ---------------------------------------------------------------------------
# A base backup must survive to anchor the retained WAL
# ---------------------------------------------------------------------------
for R in $BACKUP_REMOTES; do
  n="$(rc lsf "${R}/base" --include 'base-*.tar.gz' 2>/dev/null | wc -l)"
  [[ "$n" -ge 2 ]] || die "$R has only $n base backup(s). Refusing to prune — WAL without a base backup is unrestorable."
done

[[ "$DRY_RUN" -eq 1 ]] && log "DRY RUN — pass --execute to actually delete"

for R in $BACKUP_REMOTES; do
  log "── $R"
  for spec in "wal:${WAL_DAYS}" "user:${USER_DAYS}" "full:${FULL_DAYS}" "base:${BASE_DAYS}"; do
    prefix="${spec%%:*}"; days="${spec##*:}"
    n="$(rc lsf "${R}/${prefix}" --min-age "${days}d" 2>/dev/null | wc -l)"
    if [[ "$DRY_RUN" -eq 1 ]]; then
      log "  would delete $n object(s) from ${prefix}/ older than ${days}d"
    else
      log "  deleting $n object(s) from ${prefix}/ older than ${days}d"
      rc delete "${R}/${prefix}" --min-age "${days}d" --rmdirs || log "  ERROR pruning ${prefix}/"
    fi
  done
  log "  quarantine/ and assets/ are never pruned"
done

log "done"
