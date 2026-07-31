#!/usr/bin/env python3
"""Audit which groups the bot is ACTUALLY in, live from the Telegram API.

Neither database is authoritative here. The old DB lists 406 groups from before
the incident (the bot may since have been removed from many), and the new DB
lists 30 discovered over 8 days (an undercount — a group only appears once
someone talks). The only source of truth is Telegram itself.

Read-only: performs no writes to either database and no changes to any chat.

  python3 ops/audit_groups.py                        # audit both DBs' chat_ids
  python3 ops/audit_groups.py --db merged            # audit the merged DB
  python3 ops/audit_groups.py --write-report out.csv

Output classifies every chat_id as:
  MEMBER      bot is present and can operate
  LEFT        bot was removed / left
  KICKED      bot was banned
  NOT_FOUND   chat deleted, or bot never had access
  ERROR       transient failure (retry these)
"""

from __future__ import annotations

import argparse
import csv
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

PG_CONTAINER = "telemon_postgres"
PG_USER = "telemon"


def read_bot_token() -> str:
    """Prefer an explicit env var; fall back to parsing .env."""
    if tok := os.environ.get("BOT_TOKEN"):
        return tok.strip()
    here = os.path.dirname(os.path.abspath(__file__))
    env_path = os.path.join(os.path.dirname(here), ".env")
    try:
        with open(env_path) as fh:
            for line in fh:
                if m := re.match(r"^\s*BOT_TOKEN\s*=\s*(.+?)\s*$", line):
                    return m.group(1).strip().strip("'\"")
    except OSError:
        pass
    sys.exit("FATAL: no BOT_TOKEN. Set it in the environment or in .env")


def psql(db: str, sql: str) -> list[str]:
    out = subprocess.run(
        ["docker", "exec", PG_CONTAINER, "psql", "-U", PG_USER, "-d", db, "-Atq", "-c", sql],
        capture_output=True, text=True, stdin=subprocess.DEVNULL,
    )
    if out.returncode != 0:
        return []
    return [ln for ln in out.stdout.splitlines() if ln.strip()]


def api(token: str, method: str, **params) -> tuple[bool, dict | str]:
    url = f"https://api.telegram.org/bot{token}/{method}?" + urllib.parse.urlencode(params)
    try:
        with urllib.request.urlopen(url, timeout=20) as r:
            import json
            body = json.load(r)
            return bool(body.get("ok")), body.get("result", {})
    except urllib.error.HTTPError as e:
        import json
        try:
            body = json.load(e)
            return False, body.get("description", f"HTTP {e.code}")
        except Exception:
            return False, f"HTTP {e.code}"
    except Exception as e:  # network, timeout, DNS
        return False, str(e)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="", help="single database to read chat_ids from")
    ap.add_argument("--old-container", default="", help="also read from this old-cluster container")
    ap.add_argument("--write-report", default="", help="CSV output path")
    ap.add_argument("--delay", type=float, default=0.35,
                    help="seconds between API calls (default 0.35 ~ under 3/s)")
    args = ap.parse_args()

    token = read_bot_token()

    ok, me = api(token, "getMe")
    if not ok:
        return int(bool(sys.stderr.write(
            f"FATAL: getMe failed: {me}\n"
            "If you revoked the token after it leaked, update .env with the new one.\n")) ) or 1
    bot_id = me.get("id")
    print(f"bot: @{me.get('username')} (id {bot_id})")

    # ---- collect chat_ids -------------------------------------------------
    chat_ids: set[str] = set()
    sources: dict[str, list[str]] = {}

    dbs = [args.db] if args.db else ["telemon", "telemon_merged"]
    for db in dbs:
        rows = psql(db, "SELECT chat_id FROM groups;")
        if rows:
            sources[db] = rows
            chat_ids.update(rows)
            print(f"  {db}: {len(rows)} group(s)")

    if args.old_container:
        out = subprocess.run(
            ["docker", "exec", args.old_container, "psql", "-U", "telemon", "-d", "telemon",
             "-Atq", "-c", "SELECT chat_id FROM groups;"],
            capture_output=True, text=True, stdin=subprocess.DEVNULL,
        )
        rows = [ln for ln in out.stdout.splitlines() if ln.strip()]
        if rows:
            sources[args.old_container] = rows
            chat_ids.update(rows)
            print(f"  {args.old_container}: {len(rows)} group(s)")

    if not chat_ids:
        sys.exit("FATAL: no chat_ids found. Is the database reachable?")

    total = len(chat_ids)
    est = int(total * args.delay)
    print(f"\nauditing {total} unique chat(s) against Telegram — ~{est}s at {args.delay}s/call\n")

    # ---- audit ------------------------------------------------------------
    results: list[tuple[str, str, str, str]] = []
    counts = {"MEMBER": 0, "LEFT": 0, "KICKED": 0, "NOT_FOUND": 0, "ERROR": 0}

    for i, cid in enumerate(sorted(chat_ids, key=lambda x: int(x)), 1):
        ok, res = api(token, "getChatMember", chat_id=cid, user_id=bot_id)
        title = ""
        if ok and isinstance(res, dict):
            status = res.get("status", "")
            if status in ("member", "administrator", "creator", "restricted"):
                verdict = "MEMBER"
            elif status == "left":
                verdict = "LEFT"
            elif status == "kicked":
                verdict = "KICKED"
            else:
                verdict = "ERROR"
            detail = status
        else:
            msg = str(res).lower()
            if "chat not found" in msg or "invalid" in msg:
                verdict, detail = "NOT_FOUND", str(res)
            elif "kicked" in msg or "bot was blocked" in msg or "bot is not a member" in msg:
                verdict, detail = "LEFT", str(res)
            elif "too many requests" in msg or "retry after" in msg:
                # Respect the throttle and retry once.
                wait = 5
                if m := re.search(r"retry after (\d+)", msg):
                    wait = int(m.group(1)) + 1
                print(f"  rate limited — sleeping {wait}s")
                time.sleep(wait)
                ok2, res2 = api(token, "getChatMember", chat_id=cid, user_id=bot_id)
                if ok2 and isinstance(res2, dict):
                    st = res2.get("status", "")
                    verdict = "MEMBER" if st in ("member", "administrator", "creator", "restricted") else "LEFT"
                    detail = st
                else:
                    verdict, detail = "ERROR", str(res2)
            else:
                verdict, detail = "ERROR", str(res)

        if verdict == "MEMBER":
            ok_c, chat = api(token, "getChat", chat_id=cid)
            if ok_c and isinstance(chat, dict):
                title = chat.get("title", "") or ""
            time.sleep(args.delay)

        counts[verdict] += 1
        results.append((cid, verdict, detail, title))

        if i % 25 == 0 or i == total:
            print(f"  {i}/{total}  member={counts['MEMBER']} left={counts['LEFT']} "
                  f"kicked={counts['KICKED']} notfound={counts['NOT_FOUND']} error={counts['ERROR']}")
        time.sleep(args.delay)

    # ---- report -----------------------------------------------------------
    print("\n" + "=" * 60)
    print(" GROUP AUDIT RESULT — live from Telegram")
    print("=" * 60)
    for k in ("MEMBER", "LEFT", "KICKED", "NOT_FOUND", "ERROR"):
        print(f"  {k:<12} {counts[k]}")
    print("-" * 60)
    print(f"  ACTIVE GROUPS: {counts['MEMBER']}  ← this is the real count")
    print("=" * 60)

    if counts["MEMBER"]:
        print("\n active groups:")
        for cid, verdict, _, title in results:
            if verdict == "MEMBER":
                print(f"   {cid:>16}  {title[:50]}")

    if counts["ERROR"]:
        print(f"\n ⚠ {counts['ERROR']} chat(s) errored — these are inconclusive, not")
        print("   confirmed-absent. Re-run before deleting anything based on this.")

    if args.write_report:
        with open(args.write_report, "w", newline="") as fh:
            w = csv.writer(fh)
            w.writerow(["chat_id", "verdict", "detail", "title"])
            w.writerows(results)
        print(f"\n report written to {args.write_report}")

    print("\n NOTE: this audit deletes nothing. Group rows for LEFT/KICKED chats")
    print(" are worth keeping — their settings return automatically if the bot is")
    print(" re-added. Prune only if you specifically want them gone.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
