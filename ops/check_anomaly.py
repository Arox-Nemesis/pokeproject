#!/usr/bin/env python3
"""Anomaly gate for Telemon logical dumps.

Compares the row census of the dump about to be uploaded against the last
known-good census. Exits 0 if the snapshot looks sane, 1 if it looks like an
incident.

Design notes
------------
This gate NEVER prevents a backup from being uploaded. Tripping it only:
  * redirects the artifact to the quarantine/ prefix, and
  * freezes retention pruning so nothing ages out during the investigation.

That asymmetry is deliberate. A guard that refuses to back up is a guard that
creates gaps in your history, which is the opposite of what a backup system is
for. We would much rather store a suspicious dump we never need than skip the
one dump that turns out to matter.

Nor does it gate the WAL stream — WAL is all-or-nothing, and a gap in it makes
every subsequent segment unrestorable.

Growth is never an anomaly. Users join, Pokemon get caught, trades accumulate.
Only shrinkage is suspicious.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def load(path: Path) -> dict[str, int]:
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        print(f"could not read census {path}: {exc}", file=sys.stderr)
        sys.exit(2)
    tables = data.get("tables", {})
    return {k: int(v) for k, v in tables.items()}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--previous", required=True, type=Path)
    ap.add_argument("--current", required=True, type=Path)
    ap.add_argument("--guarded", required=True, help="comma-separated table names")
    ap.add_argument("--shrink-threshold", type=float, default=0.10)
    ap.add_argument("--absolute-drop", type=int, default=25)
    ap.add_argument("--report", type=Path, help="write human-readable findings here")
    args = ap.parse_args()

    prev = load(args.previous)
    curr = load(args.current)
    guarded = [t.strip() for t in args.guarded.split(",") if t.strip()]

    findings: list[str] = []

    for table in guarded:
        before = prev.get(table)
        after = curr.get(table)

        if before is None:
            continue  # new table, no baseline to compare against

        if after is None:
            findings.append(f"{table}: MISSING from the new census (was {before})")
            continue

        if after == -1:
            findings.append(f"{table}: could not be counted (query failed)")
            continue

        if after >= before:
            continue  # growth or steady — never an anomaly

        lost = before - after

        # A guarded table hitting exactly zero is always an incident,
        # regardless of how small it was.
        if after == 0 and before > 0:
            findings.append(f"{table}: {before} -> 0 (EMPTIED)")
            continue

        pct = lost / before if before else 0.0
        if pct >= args.shrink_threshold or lost >= args.absolute_drop:
            findings.append(
                f"{table}: {before} -> {after} (lost {lost} rows, {pct:.1%})"
            )

    # A dump where every single guarded table shrank at once is a very strong
    # signal — that pattern means a wipe or a restore-over-live, not organic
    # churn in one feature.
    shrank = sum(
        1 for t in guarded
        if prev.get(t) is not None and curr.get(t) is not None and curr[t] < prev[t]
    )
    if shrank == len(guarded) and len(guarded) > 1:
        findings.append(
            f"ALL {len(guarded)} guarded tables shrank simultaneously "
            "— consistent with a database wipe or an accidental restore"
        )

    if args.report:
        if findings:
            args.report.write_text(
                "Row counts dropped versus the last known-good snapshot:\n\n"
                + "\n".join(f"  • {f}" for f in findings)
                + f"\n\nBaseline: {args.previous.name}\nCurrent:  {args.current.name}\n"
            )
        else:
            args.report.write_text("no anomalies\n")

    if findings:
        for f in findings:
            print(f"ANOMALY: {f}", file=sys.stderr)
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
