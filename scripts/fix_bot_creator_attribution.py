"""One-time backfill for the writerbot creator-attribution bug (see
znuny_client.py ZnunyClient.BOT_OWNER_LOGINS / _harvest_user_names /
_build_details_from_ticket).

Background: writerbot (an external integration that auto-creates ISP tickets
via /api/external/isp-tickets) sets its own agent-typed article's From header
to the customer's account+name instead of its own identity. Our creator
resolution harvested that bad name into the CreateBy-id -> display-name
cache, so every ticket it created showed the customer string as "created by"
instead of "writerbot". The code fix (see git history) stops this from
happening going forward and self-corrects any OPEN ticket the routine sync
touches -- but CLOSED tickets aren't part of that routine sync, so ones
already wrong before the fix stay wrong until something re-touches them.
This script corrects those existing rows directly: for any ticket owned by a
known bot login, the correct "created by" value is simply that bot login
(same value the fixed code now uses), so no live Znuny call is needed.

Columns fixed (only rows currently wrong; a bot's own already-correct rows
are left untouched):
  - znuny_tickets.created_by       (all tickets owned by a bot login)
  - tickets.znuny_created_by       (ISP tickets linked to one of those)
  - znuny_articles.created_by      (agent-typed articles on those tickets;
                                     articles with created_by='' -- i.e. the
                                     non-agent customer articles -- are never
                                     touched)

Idempotent and re-runnable: after the fix, nothing matches "owner is a bot
but created_by isn't", so a second run finds 0 rows. Takes a timestamped DB
backup before applying.

Run inside the container:
    docker exec ticket-extractor python scripts/fix_bot_creator_attribution.py          # dry-run (default)
    docker exec ticket-extractor python scripts/fix_bot_creator_attribution.py --apply   # apply the fix
Or locally:
    python scripts/fix_bot_creator_attribution.py --apply
"""
import os
import sys
import shutil
import sqlite3
import argparse
from datetime import datetime

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB_PATH = os.getenv("DATABASE_PATH", os.path.join(ROOT, "data", "tickets.db"))
if not os.path.exists(DB_PATH) or os.path.getsize(DB_PATH) == 0:
    DB_PATH = os.path.join(ROOT, "tickets.db")

sys.path.insert(0, ROOT)
from znuny_client import ZnunyClient  # noqa: E402 -- just reads a class constant, no network/init


def main():
    ap = argparse.ArgumentParser(description="Backfill created_by to the real bot login for known bot-owned tickets.")
    ap.add_argument("--apply", action="store_true", help="Apply the fix (default is a dry-run).")
    args = ap.parse_args()
    apply = args.apply

    bot_logins = sorted(ZnunyClient.BOT_OWNER_LOGINS)
    if not bot_logins:
        print("[ERROR] ZnunyClient.BOT_OWNER_LOGINS is empty -- nothing to fix.")
        sys.exit(1)

    if not os.path.exists(DB_PATH):
        print(f"[ERROR] Database not found at {DB_PATH}")
        sys.exit(1)

    print(f"Database   : {DB_PATH}")
    print(f"Bot logins : {', '.join(bot_logins)}")
    print(f"Mode       : {'APPLY' if apply else 'DRY-RUN (no changes; pass --apply to write)'}")

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()

    if apply:
        bak = f"{DB_PATH}.bak-botcreator-{datetime.now().strftime('%Y%m%d%H%M%S')}"
        shutil.copy2(DB_PATH, bak)
        print(f"Backup     : {bak}")

    counts = {"znuny_tickets": 0, "tickets": 0, "znuny_articles": 0}
    samples = []

    for bot in bot_logins:
        # 1. znuny_tickets.created_by
        rows = cur.execute(
            "SELECT znuny_ticket_id, created_by FROM znuny_tickets WHERE owner = ? AND created_by != ?",
            (bot, bot),
        ).fetchall()
        for r in rows:
            if len(samples) < 8:
                samples.append(f"  znuny_tickets#{r['znuny_ticket_id']} created_by: {r['created_by']!r} -> {bot!r}")
            if apply:
                cur.execute(
                    "UPDATE znuny_tickets SET created_by = ? WHERE znuny_ticket_id = ?",
                    (bot, r["znuny_ticket_id"]),
                )
        counts["znuny_tickets"] += len(rows)

        # 2. tickets.znuny_created_by (ISP tickets linked to a bot-owned znuny ticket)
        rows = cur.execute(
            """
            SELECT t.id, t.znuny_created_by FROM tickets t
            JOIN znuny_tickets z ON z.znuny_ticket_id = t.znuny_ticket_id
            WHERE z.owner = ? AND (t.znuny_created_by IS NULL OR t.znuny_created_by != ?)
            """,
            (bot, bot),
        ).fetchall()
        for r in rows:
            if len(samples) < 8:
                samples.append(f"  tickets#{r['id']} znuny_created_by: {r['znuny_created_by']!r} -> {bot!r}")
            if apply:
                cur.execute("UPDATE tickets SET znuny_created_by = ? WHERE id = ?", (bot, r["id"]))
        counts["tickets"] += len(rows)

        # 3. znuny_articles.created_by (agent-typed articles only -- never touch
        #    the empty created_by that non-agent/customer articles legitimately have)
        rows = cur.execute(
            """
            SELECT a.id, a.created_by FROM znuny_articles a
            JOIN znuny_tickets z ON z.znuny_ticket_id = a.znuny_ticket_id
            WHERE z.owner = ? AND a.created_by != '' AND a.created_by != ?
            """,
            (bot, bot),
        ).fetchall()
        for r in rows:
            if len(samples) < 8:
                samples.append(f"  znuny_articles#{r['id']} created_by: {r['created_by']!r} -> {bot!r}")
            if apply:
                cur.execute("UPDATE znuny_articles SET created_by = ? WHERE id = ?", (bot, r["id"]))
        counts["znuny_articles"] += len(rows)

    print("\nRows with wrong created_by (to be fixed):" if not apply else "\nRows fixed:")
    for k, v in counts.items():
        print(f"  {k}: {v}")
    if samples:
        print("\nSample changes:")
        for s in samples:
            print(s)

    if apply:
        conn.commit()
        print("\nDone.")
    else:
        total = sum(counts.values())
        print(f"\nDry-run only -- {total} row(s) would change. Re-run with --apply to commit.")
    conn.close()


if __name__ == "__main__":
    main()
