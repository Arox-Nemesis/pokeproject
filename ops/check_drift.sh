#!/usr/bin/env bash
# Schema drift check: do the SQLAlchemy models and the live database agree?
#
# Why this lives in the backup tooling:
#
# `alembic_version` matching HEAD proves the migration *ledger* is current. It
# does NOT prove the schema matches the models — a column added to a model
# without a generated migration leaves the ledger looking perfectly healthy
# while every query touching that column fails at runtime.
#
# That failure mode is a backup problem, not just an app problem. A restore
# that faithfully reproduces a schema the application cannot query is not a
# usable restore. This check is what turns "the bytes came back" into "the bot
# actually runs".
#
# Exit 0 = models and database agree. Exit 1 = drift.
#
#   ops/check_drift.sh              # human readable
#   ops/check_drift.sh --quiet      # only speak up on drift

SCRIPT_NAME=check_drift
# shellcheck disable=SC1091
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

QUIET=0
[[ "${1:-}" == "--quiet" ]] && QUIET=1
BOT_CONTAINER="${BOT_CONTAINER:-telemon_bot}"

require_pg

docker inspect -f '{{.State.Running}}' "$BOT_CONTAINER" 2>/dev/null | grep -q true \
  || die "container $BOT_CONTAINER is not running (needed to load the SQLAlchemy models)"

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

# ---------------------------------------------------------------------------
# What the models expect
# ---------------------------------------------------------------------------
docker exec -i "$BOT_CONTAINER" python - > "$WORK/models.json" 2>"$WORK/models.err" <<'PY'
import json, sys
sys.path.insert(0, "/app/src")
import telemon.database.models  # noqa: F401  (registers every model)
from telemon.database.models.base import Base
print(json.dumps({t: sorted(c.name for c in tbl.columns)
                  for t, tbl in Base.metadata.tables.items()}))
PY

[[ -s "$WORK/models.json" ]] || die "could not load models: $(head -5 "$WORK/models.err")"

# ---------------------------------------------------------------------------
# What the database actually has
# ---------------------------------------------------------------------------
psql_q "
SELECT json_object_agg(table_name, cols) FROM (
  SELECT table_name, json_agg(column_name ORDER BY column_name) AS cols
  FROM information_schema.columns
  WHERE table_schema='public'
  GROUP BY table_name
) s;" > "$WORK/db.json"

[[ -s "$WORK/db.json" ]] || die "could not introspect the database schema"

# ---------------------------------------------------------------------------
# Compare
# ---------------------------------------------------------------------------
set +e
REPORT="$(python3 - "$WORK/models.json" "$WORK/db.json" "$QUIET" <<'PY'
import json, sys
models = json.load(open(sys.argv[1]))
db = json.load(open(sys.argv[2]))
quiet = sys.argv[3] == "1"

problems = []
for t in sorted(set(models) - set(db)):
    problems.append(f"table '{t}' is declared by a model but MISSING from the database")
for t in sorted(set(models) & set(db)):
    for c in sorted(set(models[t]) - set(db[t])):
        problems.append(f"{t}.{c} is declared by a model but MISSING from the database "
                        f"— every query touching {t} will fail")

if not quiet:
    print(f"tables checked: {len(set(models) & set(db))}")
    # Columns present in the DB but absent from the models are harmless
    # leftovers (a dropped attribute, a manual column) — reported, not failed.
    for t in sorted(set(models) & set(db)):
        for c in sorted(set(db[t]) - set(models[t])):
            print(f"  note: {t}.{c} exists in the DB but no model declares it (harmless)")

for p in problems:
    print(f"  DRIFT: {p}")

sys.exit(1 if problems else 0)
PY
)"
DRIFT=$?
set -e

if [[ "$DRIFT" -eq 0 ]]; then
  if [[ "$QUIET" -eq 0 ]]; then
    echo "════════ SCHEMA DRIFT CHECK ════════"
    echo "$REPORT"
    echo "VERDICT: ✅ models and database agree"
    echo "════════════════════════════════════"
  fi
  exit 0
fi

echo "════════ SCHEMA DRIFT CHECK ════════"
echo "$REPORT"
echo "VERDICT: 🚨 DRIFT DETECTED"
echo ""
echo "Fix by generating and applying a migration:"
echo "  docker exec $BOT_CONTAINER alembic revision --autogenerate -m 'describe change'"
echo "  docker exec $BOT_CONTAINER alembic upgrade head"
echo "════════════════════════════════════"

alert "$(printf '🚨 Telemon SCHEMA DRIFT detected\n\n%s\n\nThe migration ledger looks current but the schema does not match the models. Queries are failing, and a restore would reproduce the same broken schema.' "$REPORT")"
exit 1
