# Telemon Backup & Disaster Recovery

Two-tier storage for the bot's database, built around one rule:

> **A mirror is not a backup. A mirror replicates disasters faithfully and fast.**

If Tier 2 were a plain synced copy of Tier 1, then a `DROP TABLE` at 07:31
would reach the cloud in milliseconds and the good 07:30 data would be gone.
So Tier 2 here is **append-only history**, not a mirror. Nothing that happens
to the live database can reach back and unwrite what was already archived.

---

## Architecture

```
TIER 1 — LIVE
  Postgres 16 (telemon_postgres). What the bot reads and writes.

        │  archive_command: gzip + atomic rename into /wal-spool
        ▼
  /wal-spool  (docker volume — decouples Postgres from the network)
        │
        │  wal-uploader sidecar: rclone COPY (never sync), then local rm
        ▼
TIER 2 — APPEND-ONLY ARCHIVE (Backblaze B2 + Cloudflare R2)

  wal/         every 60s   continuous changelog     → RPO 60s
  user/        every 15m   irreplaceable tables     → version-independent
  full/        every 6h    everything               → major-version upgrades
  base/        daily       physical base backup     → the PITR anchor
  quarantine/  on anomaly  suspicious snapshots     → never auto-deleted
  assets/      on demand   data/official-artwork    → not in the DB
```

**RPO ≤ 60 seconds. RTO ≈ 10 minutes.**

---

## Why this survives the 07:31 scenario

WAL is a *changelog*, not a snapshot. A `TRUNCATE` at 07:31 is appended as a
new segment saying "at 07:31, a truncate happened". It cannot modify the
segments written at 07:30. Recovery then means: *replay up to 07:30:59 and
stop*.

Five independent layers, any one of which would save you:

| # | Layer | What it stops |
|---|---|---|
| 1 | Append-only WAL archive | The wipe is a new object; old objects are untouched |
| 2 | No-delete cloud credentials | A rooted server can add to the archive but never destroy it |
| 3 | B2 Object Lock (WORM) | Not even an admin key can erase within the retention window |
| 4 | GFS retention (14d/30d/90d) | Slow-burn corruption noticed a week late is still recoverable |
| 5 | Anomaly gate | A mass-delete quarantines the dump and freezes pruning |

Plus: `prune.sh` refuses to delete anything unless the most recent restore
drill passed.

---

## Setup

### 1. Cloud buckets

Create `telemon-archive` and `telemon-assets` on both providers.

**Backblaze B2** (primary — free 10 GB, supports Object Lock):
- Application Key scoped to the bucket
- Capabilities: `listBuckets, listFiles, readFiles, writeFiles`
- **Explicitly NOT** `deleteFiles`
- Enable Object Lock, 30-day governance retention

**Cloudflare R2** (secondary — free 10 GB, zero egress):
- API token with Object Read & Write
- Add lifecycle rules for retention (see `prune.sh` for the recommended values)

`readFiles` is granted deliberately — the restore drill has to read the
archive back. An unverifiable backup is not a backup.

### 2. Configure

```bash
cp ops/backup.env.example ops/backup.env
cp ops/rclone.conf.example ops/rclone.conf
chmod 600 ops/backup.env ops/rclone.conf
# fill both in
```

Set `BOT_TOKEN` / `ALERT_CHAT_ID` in `backup.env` for alerts. Prefer a
*separate* ops bot so a leaked backup config can't post as the game bot.

### 3. Restart Postgres with archiving on

```bash
docker compose up -d
```

`archive_mode` needs a full restart, not a reload — `docker compose up -d`
recreates the container, which is sufficient.

### 4. Verify, then schedule

```bash
ops/status.sh          # should show archive_mode=on and a rising segment count
sudo ops/install.sh    # systemd timers
ops/sync_assets.sh     # one-off: push data/official-artwork (~380 MB)
```

### 5. Prove it works

```bash
ops/restore_drill.sh
```

Do not consider the system live until this passes.

---

## Daily use

```bash
ops/status.sh                      # health
systemctl list-timers 'telemon-*'  # schedule
journalctl -u telemon-userdump -n 50
```

Alerts fire on: archive stalls, upload failures, spool backpressure, tripped
anomaly gate, failed drills, and stale base backups.

---

## Runbooks

### "The database got wiped, restore to just before it"

```bash
docker compose stop bot                       # 1. stop the writer FIRST

ops/restore_pitr.sh --target-time "2026-07-26 07:30:59+00"
                                              # 2. recovers into a SCRATCH
                                              #    container on port 5499.
                                              #    Live data is untouched.

psql 'postgresql://telemon:restore@localhost:5499/telemon'
                                              # 3. verify it looks right

ops/restore_logical.sh --from-container telemon_restore
                                              # 4. promote into live
                                              #    (snapshots current state
                                              #     first, for rollback)

docker compose start bot                      # 5.
ops/clear_anomaly.sh                          # 6. re-baseline the gate
```

Restoring into scratch first is the point. If you pick the wrong target time,
you just re-run with a different one — you haven't destroyed the evidence.

### "Restore one table"

```bash
ops/restore_logical.sh --dump user-20260726T073000Z.pgc --tables users,pokemon
```

### "Move to a new server"

The database is only part of it. `data/official-artwork` (~342 MB) and
`data/csv` (~36 MB) live outside Postgres.

```bash
git clone <repo> Telemon && cd Telemon
cp ops/backup.env.example ops/backup.env      # fill in
cp ops/rclone.conf.example ops/rclone.conf    # fill in
# copy .env across manually — it is NOT in the archive, by design

rclone --config ops/rclone.conf copy b2:telemon-assets/official-artwork data/official-artwork
rclone --config ops/rclone.conf copy b2:telemon-assets/csv              data/csv
rclone --config ops/rclone.conf copy b2:telemon-assets/json             data

docker compose up -d postgres
ops/restore_pitr.sh --target-time latest --port 5499
ops/restore_logical.sh --from-container telemon_restore
docker compose up -d
```

### "The anomaly gate tripped"

1. Don't panic and don't clear it — pruning is frozen, nothing is aging out.
2. Look at what shrank (the alert lists it).
3. Was it intentional? Then `ops/clear_anomaly.sh`.
4. Was it not? The dump is in `quarantine/`, and everything before it is
   still in `wal/` and `base/`. Use the restore runbook above.

---

## Design decisions worth knowing

**Why gzip in `archive_command`?** `archive_timeout=60` forces a full 16 MB
segment every minute even when nearly empty — 23 GB/day raw. Idle segments
compress to ~20 KB, making it ~30 MB/day. Without this, the free tier lasts
about six hours.

**Why a spool + sidecar instead of uploading from `archive_command`?** If
`archive_command` fails or blocks, Postgres retains WAL and `pg_wal` grows
until the disk fills — taking the bot down. This is the classic way a backup
system causes the outage it was meant to prevent. Writing to a local spool
cannot realistically fail, and network problems back up the spool instead of
the database. (Also, `postgres:16-alpine` has no rclone binary.)

**Why `copy` and never `sync`?** `rclone sync` makes the destination identical
to the source, *including deletions*. Pointed at the archive it would be a
loaded gun. Every push in this system is `copy`; the only deletes are local,
after every remote has confirmed receipt.

**Why does the anomaly gate still upload?** A guard that refuses to back up is
a guard that creates gaps in your history. Tripping it redirects to
`quarantine/` and freezes pruning — it never suppresses data. We would much
rather keep a suspicious dump we never need than skip the one that mattered.

**Why doesn't the gate cover WAL?** WAL is all-or-nothing. A gap makes every
subsequent segment unrestorable. WAL always ships, unconditionally.

**Why exact `COUNT(*)` instead of `pg_stat_user_tables`?** `n_live_tup` is a
stale estimate. It produces both false alarms and missed incidents. (Observed
here: `pg_stat_user_tables` reported 66 users when the real count was 97.)

**Why check schema drift in a backup system?** `alembic_version` matching HEAD
proves the migration *ledger* is current — not that the schema matches the
models. A column added to a model without a generated migration leaves the
ledger looking healthy while every query touching that table fails. Restoring
such a database faithfully reproduces a schema the application cannot query,
which is not a usable restore. This was not hypothetical: `groups.bot_joined_at`
was in exactly that state and had been breaking the spawn loops.

**Why is `wal_level=logical` when nothing subscribes yet?** Changing it needs
a restart. Setting it now means adding a Neon/Postgres replica later costs no
downtime.

---

## Optional next step: a warm mirror

Everything above is Tier 2 as *history*. If you also want fast failover and
one-command server migration, add a mirror — but **only after the archive is
proven**, and knowing that a mirror will faithfully replicate your 07:31
disaster. It complements the archive; it does not replace it.

`wal_level=logical` is already set, so:

```sql
-- local
CREATE PUBLICATION telemon_pub FOR ALL TABLES;
-- remote (Neon free tier, or your own box)
CREATE SUBSCRIPTION telemon_sub
  CONNECTION 'host=... dbname=telemon' PUBLICATION telemon_pub;
```

Caveat: logical replication does **not** replicate DDL. Every
`alembic upgrade` must be applied to the subscriber manually, before the
publisher, or replication breaks.

If you run your own standby instead, set `recovery_min_apply_delay = '1h'`.
The standby then holds data from an hour ago — an undo button with a
one-hour fuse, queryable and fully indexed.

---

## Files

| File | Purpose |
|---|---|
| `wal_uploader.sh` | Sidecar: drains the WAL spool to all remotes |
| `basebackup.sh` | Daily physical base backup (the PITR anchor) |
| `logical_dump.sh` | 15-min user dumps / 6-hourly full dumps + anomaly gate |
| `check_anomaly.py` | Row-census comparison; detects mass deletion |
| `clear_anomaly.sh` | Acknowledge an anomaly, re-baseline, unfreeze pruning |
| `restore_pitr.sh` | Point-in-time recovery into a scratch container |
| `restore_logical.sh` | Restore a dump into the live database |
| `restore_drill.sh` | Weekly unattended proof that recovery works |
| `check_drift.sh` | Do the SQLAlchemy models and the live schema agree? |
| `status.sh` | Health check; alerts on stalls and staleness |
| `prune.sh` | Retention (prefer bucket lifecycle rules instead) |
| `sync_assets.sh` | Push `data/official-artwork` + `data/csv` |
| `install.sh` | Install systemd timers |
| `tables.sh` | Which tables are irreplaceable — single source of truth |

**When you add a model** under `src/telemon/database/models/`, add its table
to `USER_TABLES` in `tables.sh`. Nothing else needs changing.
