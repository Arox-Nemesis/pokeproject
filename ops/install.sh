#!/usr/bin/env bash
# Install the backup schedule as systemd timers.
#
# systemd over cron: real logs via journalctl, Persistent=true catches up runs
# missed while the box was down, and no silent PATH surprises.
#
#   sudo ops/install.sh
#
# Falls back to cron if systemd isn't available.

set -euo pipefail

OPS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

[[ -f "$OPS_DIR/backup.env" ]]  || { echo "FATAL: create ops/backup.env first (cp ops/backup.env.example ops/backup.env)"; exit 1; }
[[ -f "$OPS_DIR/rclone.conf" ]] || { echo "FATAL: create ops/rclone.conf first (cp ops/rclone.conf.example ops/rclone.conf)"; exit 1; }

command -v rclone >/dev/null || { echo "FATAL: rclone is not installed. Run: curl https://rclone.org/install.sh | sudo bash"; exit 1; }
command -v flock  >/dev/null || { echo "FATAL: flock is not installed (package: util-linux)"; exit 1; }

chmod 600 "$OPS_DIR/rclone.conf" "$OPS_DIR/backup.env"
chmod +x "$OPS_DIR"/*.sh "$OPS_DIR"/*.py

# shellcheck disable=SC1091
source "$OPS_DIR/backup.env"
mkdir -p "${STATE_DIR:-/var/lib/telemon-backup}"

if ! command -v systemctl >/dev/null; then
  echo "systemd not found — installing cron entries instead"
  CRON=$(mktemp)
  crontab -l 2>/dev/null | grep -v 'telemon-backup' > "$CRON" || true
  cat >> "$CRON" <<EOF
*/15 * * * *  $OPS_DIR/logical_dump.sh user   >> /var/log/telemon-backup.log 2>&1  # telemon-backup
0 */6 * * *   $OPS_DIR/logical_dump.sh full   >> /var/log/telemon-backup.log 2>&1  # telemon-backup
0 4 * * *     $OPS_DIR/basebackup.sh          >> /var/log/telemon-backup.log 2>&1  # telemon-backup
0 * * * *     $OPS_DIR/status.sh --quiet      >> /var/log/telemon-backup.log 2>&1  # telemon-backup
0 5 * * 0     $OPS_DIR/restore_drill.sh       >> /var/log/telemon-backup.log 2>&1  # telemon-backup
EOF
  crontab "$CRON"; rm -f "$CRON"
  echo "cron installed. Verify with: crontab -l"
  exit 0
fi

unit() {
  local name="$1" desc="$2" cmd="$3" sched="$4"
  cat > "/etc/systemd/system/telemon-${name}.service" <<EOF
[Unit]
Description=${desc}
After=docker.service
Requires=docker.service

[Service]
Type=oneshot
ExecStart=${cmd}
StandardOutput=journal
StandardError=journal
EOF

  cat > "/etc/systemd/system/telemon-${name}.timer" <<EOF
[Unit]
Description=${desc} (timer)

[Timer]
OnCalendar=${sched}
Persistent=true
RandomizedDelaySec=30

[Install]
WantedBy=timers.target
EOF

  systemctl enable --now "telemon-${name}.timer" >/dev/null
  echo "  installed telemon-${name}.timer  (${sched})"
}

echo "Installing systemd timers..."

# User data every 15 minutes. This is the irreplaceable stuff — WAL already
# covers it continuously, but a logical dump survives version upgrades and
# allows single-table restores.
# Cadences are sized to the CURRENT database against a 10 GB free tier.
# Measured post-merge: user dump 67.8 MB, full dump 127.5 MB, WAL 244 MB/day.
#
#   user 4x/day x 7d  = 1.85 GB      full 1x/day x 10d = 1.25 GB
#   wal  10d          = 2.38 GB      base 1x/day x 10d = 1.84 GB   -> 7.3 GB
#
# Hourly user dumps would be 11.1 GB on their own. WAL already gives 60-second
# RPO, so dump frequency buys almost nothing — dumps exist for
# version-independence and single-table restores.
unit userdump   "Telemon user-data logical dump"  "$OPS_DIR/logical_dump.sh user" "*-*-* 00,06,12,18:07:00"
unit fulldump   "Telemon full logical dump"       "$OPS_DIR/logical_dump.sh full" "*-*-* 02:05:00"
unit basebackup "Telemon physical base backup"    "$OPS_DIR/basebackup.sh"        "*-*-* 04:00:00"
unit status     "Telemon backup health check"     "$OPS_DIR/status.sh --quiet"    "hourly"
unit drill      "Telemon restore drill"           "$OPS_DIR/restore_drill.sh"     "Sun *-*-* 05:00:00"

# Tier 2 / standby watchdogs. Both no-op cleanly when the feature is not
# configured, so they are safe to install unconditionally.
#
# The mirror check runs every 10 minutes for one specific reason: an INACTIVE
# replication slot retains WAL on the primary indefinitely and will fill the
# disk, taking the bot down. That is the worst failure mode in this system and
# it deserves a tight loop, not an hourly one.
unit mirror     "Telemon mirror health"           "$OPS_DIR/mirror_status.sh --quiet"  "*:0/10"
unit standby    "Telemon delayed standby health"  "$OPS_DIR/standby_status.sh --quiet" "*:0/20"

systemctl daemon-reload

cat <<EOF

Installed. Schedule:
  every 10 min   mirror health (replication slot / WAL-retention watchdog)
  every 20 min   delayed standby health
  4x daily :07   user-data logical dump  (+ anomaly gate)  00,06,12,18
  daily 02:05    full logical dump
  daily 04:00    physical base backup
  hourly         health check (+ schema drift)
  weekly Sun 05  restore drill

Continuous WAL archiving gives 60-second RPO; the dump cadences above are for
version-independence and single-table restores, not primary recovery.

Continuous: WAL segments archived every 60s by the wal-uploader container.

The mirror and standby timers no-op cleanly when those features are not
configured, so nothing breaks if you never enable them.

Check status:
  ops/status.sh
  ops/mirror_status.sh
  ops/standby_status.sh
  systemctl list-timers 'telemon-*'
  journalctl -u telemon-userdump.service -n 50

EOF
