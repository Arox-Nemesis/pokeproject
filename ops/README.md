# Telemon Backup & Disaster Recovery

Complete data-protection ecosystem for the bot's Postgres database, built
around one rule:

> **A mirror is not a backup. A mirror replicates disasters faithfully and fast.**

If Tier 2 were a plain synced copy of Tier 1, a `DROP TABLE` at 07:31 would
reach the cloud in milliseconds and the good 07:30 data would be gone. So the
protective tier here is **append-only history**, not a mirror. Nothing that
happens to the live database can reach back and unwrite what was already
archived.

---

## The four layers

```
┌────────────────────────────────────────────────────────────────────────┐
│ TIER 1 — LIVE                                                          │
│ Postgres 16 (telemon_postgres). What the bot reads and writes.          │
└───────┬────────────────────────────────────────────────────────────────┘
        │ archive_command: gzip + atomic rename (never blocks Postgres)
        ▼
   /wal-spool ──uploaded──▶ /wal-retain (30h local, feeds standby + fast restore)
        │
        │ rclone COPY (never sync) to every remote
        ▼
┌────────────────────────────────────────────────────────────────────────┐
│ TIER 3 — APPEND-ONLY ARCHIVE   ◀── THIS IS THE BACKUP                  │
│ Backblaze B2 + Cloudflare R2. Write-capable, delete-incapable creds.    │
│                                                                        │
│   wal/         every 60s   continuous changelog      RPO 60s            │
│   user/        every 15m   17 irreplaceable tables                      │
│   full/        every 6h    everything                                   │
│   base/        daily       PITR anchor                                  │
│   quarantine/  on anomaly  never auto-deleted                           │
│   assets/      on demand   data/official-artwork (~380MB, not in DB)    │
└────────────────────────────────────────────────────────────────────────┘

┌────────────────────────────────────────────────────────────────────────┐
│ TIER 2b — DELAYED STANDBY (undo button with a fuse)                    │
│ Live Postgres deliberately 1h behind, replaying the LOCAL retention.   │
│ Passive: no replication slot, cannot affect production.                │
│ Receives the 07:31 wipe but won't apply it until 08:31.                │
└────────────────────────────────────────────────────────────────────────┘

┌────────────────────────────────────────────────────────────────────────┐
│ TIER 2a — WARM MIRROR (optional)                                       │
│ Logical replication to Neon/any Postgres. Failover + server migration. │
│ ⚠ REPLICATES DELETES. Not a backup. Complements Tier 3, never replaces │
└────────────────────────────────────────────────────────────────────────┘
```

**RPO ≤ 60 seconds. RTO ~10 min from cloud, faster from local retention,
~0 from a frozen standby.**

---

## Why this survives the 07:31 scenario

WAL is a *changelog*, not a snapshot. A `TRUNCATE` at 07:31 is appended as a
new segment saying "at 07:31, a truncate happened". It cannot modify the
segments written at 07:30. Recovery means: *replay up to 07:30:59 and stop*.

**This was tested, not assumed.** 1000 rows written, wiped seconds later,
recovered to the moment before: 1000/1000 rows returned, no gaps.

Six independent layers, any one of which would save you:

| # | Layer | What it stops |
|---|---|---|
| 1 | Append-only WAL archive | The wipe is a new object; old objects untouched |
| 2 | No-delete cloud credentials | A rooted server can add to the archive, never destroy it |
| 3 | B2 Object Lock (WORM) | Not even an admin key can erase within the window |
| 4 | GFS retention (14d/30d/90d) | Slow-burn corruption noticed a week late is still recoverable |
| 5 | Anomaly gate | A mass-delete quarantines the dump and freezes pruning |
| 6 | Delayed standby | An hour of "undo" on a live, queryable database |

Plus: `prune.sh` refuses to delete anything unless the last restore drill passed.

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
- Add lifecycle rules for retention (values in `prune.sh`)

`readFiles` is granted deliberately — the restore drill must read the archive
back. An unverifiable backup is not a backup.

### 2. Configure

```bash
cp ops/backup.env.example ops/backup.env
cp ops/rclone.conf.example ops/rclone.conf
chmod 600 ops/backup.env ops/rclone.conf
# fill both in
```

### 3. Start archiving

```bash
docker compose up -d          # brings up postgres (archiving on) + wal-uploader
ops/status.sh                 # expect archive_mode=on and a rising segment count
```

### 4. Schedule + prove

```bash
sudo ops/install.sh           # systemd timers
ops/sync_assets.sh            # one-off: push data/official-artwork (~380 MB)
ops/basebackup.sh             # first PITR anchor
ops/restore_drill.sh          # ← do not consider this live until it passes
```

### 5. Optional extras

```bash
ops/standby_start.sh          # 1h delayed standby (recommended, cheap, local)
ops/encrypt_setup.sh          # encrypt the archive (read the key warning)
ops/mirror_setup.sh           # warm mirror (needs MIRROR_URL configured)
```

---

## Verifying the whole system

```bash
ops/selftest.sh           # everything, ~5 min
ops/selftest.sh --quick   # skip standby + mirror, ~2 min
```

Runs every component end to end against a **temporary local filesystem
remote** — no cloud credentials required, so it works before setup and in CI.

It is safe on a live system: it reads your database, creates and drops only
`_selftest_*` tables, restores exclusively into scratch containers on scratch
ports, never writes to your real archive or config, and never stops the bot.
It also asserts it left no replication slot behind, since a forgotten slot
would retain WAL on the primary forever.

Run it after any change to `ops/`, and after upgrading Postgres.

---

## Daily use

```bash
ops/status.sh            # archiving, drift, gate, drill freshness
ops/mirror_status.sh     # replication lag + slot health
ops/standby_status.sh    # how far behind the standby is
systemctl list-timers 'telemon-*'
```

Alerts fire on: archive stalls, upload failures, spool backpressure, tripped
anomaly gate, failed drills, stale base backups, schema drift, replication
divergence, and inactive replication slots retaining WAL.

---

## Runbooks

### 🚨 "The database got wiped, restore to just before it"

**Fastest path — if the delayed standby is running:**

```bash
ops/standby_freeze.sh                        # DO THIS FIRST. Stops the clock.
# You now have a live, queryable database holding pre-incident data.
docker compose stop bot
ops/restore_logical.sh --from-container telemon_standby
docker compose start bot
ops/clear_anomaly.sh
```

`standby_freeze.sh` is the most time-sensitive command in the system. If you
are unsure whether you need it, run it — pausing costs nothing and reverses
with `standby_resume.sh`.

**From the archive — always available:**

```bash
docker compose stop bot                      # stop the writer FIRST
ops/restore_pitr.sh --target-time "2026-07-26 07:30:59+00"
                                             # recovers into a SCRATCH container
                                             # on port 5499. Live is untouched.
psql 'postgresql://telemon:restore@localhost:5499/telemon'   # verify
ops/restore_logical.sh --from-container telemon_restore      # promote
docker compose start bot
ops/clear_anomaly.sh
```

Restoring into scratch first is the point: a wrong target time costs you a
re-run, not the evidence.

### "Restore one table"

```bash
ops/restore_logical.sh --dump user-20260726T073000Z.pgc --tables users,pokemon
```

### "Move to a new server" / "the old server is gone"

```bash
git clone <repo> Telemon && cd Telemon
cp ops/backup.env.example ops/backup.env && vim ops/backup.env
cp ops/rclone.conf.example ops/rclone.conf && vim ops/rclone.conf
# copy .env across by hand — it is intentionally NOT in the archive
ops/bootstrap.sh
```

`bootstrap.sh` pulls the database (PITR or logical), all ~380 MB of assets,
verifies every guarded table is queryable, starts the stack, and checks schema
drift. Then run `ops/install.sh` and `ops/basebackup.sh` — **the new server is
not protected until you do.**

### "Failover to the mirror"

```bash
ops/mirror_promote.sh
# then: repoint DATABASE_URL, restart bot, and run ops/basebackup.sh
```

Promotion advances **all** sequences past their table maximums. Skipping that
step is the classic way to corrupt a promoted database — see below.

### "Run a migration while a mirror is live"

```bash
ops/mirror_migrate.sh                 # additive DDL (the normal case)
ops/mirror_migrate.sh --primary-first  # migration contains DROP/destructive DDL
```

**Never** `alembic upgrade head` directly against the primary while a mirror is
subscribed — it breaks replication permanently and silently.

### "The anomaly gate tripped"

1. Don't clear it. Pruning is frozen; nothing is aging out.
2. Look at what shrank (the alert lists it).
3. Intentional? `ops/clear_anomaly.sh`.
4. Not intentional? The dump is in `quarantine/`, and everything before it is
   still in `wal/` and `base/`. Use the recovery runbook.

---

## Design decisions worth knowing

**Why gzip in `archive_command`?** `archive_timeout=60` forces a full 16 MB
segment every minute even when nearly empty — 23 GB/day raw. Idle segments
compress to ~20 KB (measured), making it ~30 MB/day. Without this the free tier
lasts about six hours.

**Why a spool + sidecar instead of uploading from `archive_command`?** If
`archive_command` fails or blocks, Postgres retains WAL and `pg_wal` grows until
the disk fills — taking the bot down. This is the classic way a backup system
causes the outage it was meant to prevent. A local spool write cannot
realistically fail, and network problems back up the spool instead of the
database. (Also, `postgres:16-alpine` has no rclone binary.)

**Why keep WAL locally after uploading?** Two payoffs: the delayed standby
replays from it (so it needs no cloud access and creates **no replication slot
on the primary**), and a recent PITR skips the download entirely. The cloud
archive stays authoritative, so local pruning can never cost history.

**Why does the delayed standby replay the archive instead of streaming?** A
streaming standby needs a replication slot on the primary. If the standby dies,
that slot retains WAL on the *primary* forever until the disk fills. Replaying
the archive makes the standby completely passive — the primary doesn't know it
exists, so it cannot take production down however badly it breaks.

**Why `copy` and never `sync`?** `rclone sync` makes the destination identical
to the source, *including deletions*. Pointed at the archive it is a loaded gun.
Every push here is `copy`; the only deletes are local, after every remote has
confirmed receipt.

**Why does the anomaly gate still upload?** A guard that refuses to back up is a
guard that creates gaps. Tripping it redirects to `quarantine/` and freezes
pruning — it never suppresses data. Better to keep a suspicious dump we never
need than skip the one that mattered.

**Why doesn't the gate cover WAL?** WAL is all-or-nothing. A gap makes every
subsequent segment unrestorable. WAL always ships, unconditionally.

**Why exact `COUNT(*)` instead of `pg_stat_user_tables`?** `n_live_tup` is a
stale estimate. Observed here: it reported 66 users when the real count was 97.

**Why does promotion advance sequences?** Logical replication does not replicate
sequence values. Rows arrive with their `id`s intact, but the mirror's sequence
still sits where the initial copy left it. The next INSERT calls `nextval()`,
gets a number already in use, and collides. Telemon has 8 sequence-backed
columns — including `users.telegram_id` and `groups.chat_id`, where the sequence
is vestigial but still attached as a column default. `mirror_promote.sh` derives
them from the live catalog, so it stays correct as the schema grows.

**Why check schema drift in a backup system?** `alembic_version` matching HEAD
proves the migration *ledger* is current, not that the schema matches the
models. A restore that faithfully reproduces a schema the app cannot query is
not a usable restore. Not hypothetical: `groups.bot_joined_at` was in exactly
that state, breaking the spawn loops, while the ledger looked healthy.

**Why isn't Redis backed up?** Audited: it holds only aiogram FSM state
(mid-dialog conversation position) and a `REBUILD_REQUESTED` IPC flag for the
host watchdog. `DBSIZE` was 0. Losing it means a user mid-command restarts that
command — zero permanent data loss. **This changes** if you ever store
authoritative game state in Redis; at that point it needs its own snapshot path.

---

## Files

| File | Purpose |
|---|---|
| **Archiving** | |
| `wal_uploader.sh` | Sidecar: spool → all remotes → local retention |
| `basebackup.sh` | Daily physical base backup (the PITR anchor) |
| `logical_dump.sh` | 15-min user dumps / 6-hourly full dumps + anomaly gate |
| `sync_assets.sh` | Push `data/official-artwork` + `data/csv` + JSON |
| **Guards** | |
| `check_anomaly.py` | Row-census comparison; detects mass deletion |
| `clear_anomaly.sh` | Acknowledge, re-baseline, unfreeze pruning |
| `check_drift.sh` | Do the models and the live schema agree? |
| `status.sh` | Health check; alerts on stalls, drift, staleness |
| **Recovery** | |
| `restore_pitr.sh` | Point-in-time recovery into a scratch container |
| `restore_logical.sh` | Restore a dump into the live database |
| `restore_drill.sh` | Weekly unattended proof that recovery works |
| `bootstrap.sh` | One-command provision of a fresh server |
| **Delayed standby** | |
| `standby_start.sh` | Start the 1h-delayed standby |
| `standby_freeze.sh` | ❄️ Stop replay — run this during an incident |
| `standby_resume.sh` | Un-freeze |
| `standby_status.sh` | How far behind is it? |
| `standby_stop.sh` | Tear down |
| **Warm mirror** | |
| `mirror_setup.sh` | Publication + subscription + initial sync |
| `mirror_status.sh` | Lag, slot health, divergence |
| `mirror_migrate.sh` | Safe Alembic upgrades with a live mirror |
| `mirror_promote.sh` | Failover, incl. sequence advancement |
| **Plumbing** | |
| `tables.sh` | Which tables are irreplaceable — single source of truth |
| `lib.sh` | Shared config, logging, alerting, rclone wrappers |
| `install.sh` | Install systemd timers |
| `prune.sh` | Retention (prefer bucket lifecycle rules) |
| `encrypt_setup.sh` | Encrypted archive + round-trip verification |
| `selftest.sh` | **Full-ecosystem test. No cloud credentials needed.** |

**When you add a model** under `src/telemon/database/models/`, add its table to
`USER_TABLES` in `tables.sh`. Nothing else needs changing — `check_drift.sh`
will tell you if you forget the migration.
