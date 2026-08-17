"""One-time backfill for the writerbot creator-attribution bug (see
znuny_client.py ZnunyClient.BOT_OWNER_LOGINS / _harvest_user_names /
_build_details_from_ticket).

Background: writerbot (an external integration that auto-creates ISP tickets
via /api/external/isp-tickets) sets its own agent-typed article's From header
to the customer's account+name instead of its own identity -- but ONLY on
that one Phone-channel ticket-creation article. v1 of this script (and the
znuny_client.py code it matched at the time) treated the whole TICKET as
suspect: every agent-typed article on a bot-owned ticket got created_by
force-set to the bot login, including later Internal-channel articles that
real staff added when closing/updating the ticket -- those already had a
perfectly good From header, just now overwritten. Confirmed against
production data: every Phone article on a writerbot-owned ticket has the
broken From; zero Internal articles do. v2 corrects that overreach:
  - Phone-channel articles on a bot-owned ticket: created_by = the bot login
    (unchanged from v1 -- this is the one case the fallback is actually for).
  - Non-Phone articles on a bot-owned ticket: created_by is restored from the
    article's own `sender` column (the correctly-parsed From header, which v1
    never touched -- only created_by was overwritten, so the real name is
    still sitting right there to recover from). Rows where sender is empty
    are left alone (nothing safe to restore from) rather than guessed at.

Columns fixed (only rows currently wrong; already-correct rows are left
untouched):
  - znuny_tickets.created_by       (all tickets owned by a bot login -- the
                                     bot genuinely did create the ticket, so
                                     this stays forced to the bot login)
  - tickets.znuny_created_by       (ISP tickets linked to one of those, same
                                     reasoning as above)
  - znuny_articles.created_by      (agent-typed articles on those tickets,
                                     channel-aware per above; articles with
                                     created_by='' -- i.e. the non-agent
                                     customer articles -- are never touched)

Idempotent and re-runnable: after the fix, nothing matches "owner is a bot
but created_by is wrong for its channel", so a second run finds 0 rows.
Takes a timestamped DB backup before applying.

Auto-run: entrypoint.sh runs this with --apply on every container start.
A successful --apply run sets MIGRATION_FLAG in app_settings, so normal
restarts after the first skip straight past the check (this script is DB-
only/fast either way, but the flag keeps it from re-writing a backup file on
every single restart). Pass --force to bypass the flag and re-verify
everything regardless (e.g. if BOT_OWNER_LOGINS gains a new entry later).
Bumped to v2 (new flag name) so this re-runs once more on top of a DB where
v1 already ran, to correct the Internal-article overreach described above.

Run inside the container:
    docker exec ticket-extractor python scripts/fix_bot_creator_attribution.py          # dry-run (default)
    docker exec ticket-extractor python scripts/fix_bot_creator_attribution.py --apply   # apply the fix
    docker exec ticket-extractor python scripts/fix_bot_creator_attribution.py --apply --force  # re-run anyway
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

MIGRATION_FLAG = "migration_fix_bot_creator_attribution_v2"


def main():
    ap = argparse.ArgumentParser(
        description="Fix created_by for bot-owned tickets: force it to the bot login on "
                     "the Phone-channel article (the one with a genuinely broken sender), "
                     "restore it from `sender` everywhere else.")
    ap.add_argument("--apply", action="store_true", help="Apply the fix (default is a dry-run).")
    ap.add_argument("--force", action="store_true",
                     help="Re-verify everything even if MIGRATION_FLAG is already set.")
    args = ap.parse_args()
    apply = args.apply

    bot_logins = sorted(ZnunyClient.BOT_OWNER_LOGINS)
    if not bot_logins:
        print("[ERROR] ZnunyClient.BOT_OWNER_LOGINS is empty -- nothing to fix.")
        sys.exit(1)

    if not os.path.exists(DB_PATH):
        print(f"[ERROR] Database not found at {DB_PATH}")
        sys.exit(1)

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()

    cur.execute("SELECT value FROM app_settings WHERE key = ?", (MIGRATION_FLAG,))
    if cur.fetchone() and not args.force:
        print("Bot creator-attribution check already completed; skipping (pass --force to re-verify).")
        conn.close()
        return

    print(f"Database   : {DB_PATH}")
    print(f"Bot logins : {', '.join(bot_logins)}")
    print(f"Mode       : {'APPLY' if apply else 'DRY-RUN (no changes; pass --apply to write)'}")

    if apply:
        bak = f"{DB_PATH}.bak-botcreator-{datetime.now().strftime('%Y%m%d%H%M%S')}"
        shutil.copy2(DB_PATH, bak)
        print(f"Backup     : {bak}")

    counts = {"znuny_tickets": 0, "tickets": 0, "znuny_articles_phone": 0, "znuny_articles_restored": 0}
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

        # 3a. znuny_articles.created_by on the Phone-channel article -- the one
        #     genuine case (the bot's own From header is broken). Same as v1.
        rows = cur.execute(
            """
            SELECT a.id, a.created_by FROM znuny_articles a
            JOIN znuny_tickets z ON z.znuny_ticket_id = a.znuny_ticket_id
            WHERE z.owner = ? AND a.via = 'Phone' AND a.created_by != '' AND a.created_by != ?
            """,
            (bot, bot),
        ).fetchall()
        for r in rows:
            if len(samples) < 8:
                samples.append(f"  znuny_articles#{r['id']} (Phone) created_by: {r['created_by']!r} -> {bot!r}")
            if apply:
                cur.execute("UPDATE znuny_articles SET created_by = ? WHERE id = ?", (bot, r["id"]))
        counts["znuny_articles_phone"] += len(rows)

        # 3b. znuny_articles.created_by on non-Phone articles -- v1's overreach.
        #     Restore from the article's own `sender` (never touched by v1, so
        #     the real name is recoverable). Rows where sender is empty are
        #     skipped -- nothing safe to restore from.
        rows = cur.execute(
            """
            SELECT a.id, a.created_by, a.sender FROM znuny_articles a
            JOIN znuny_tickets z ON z.znuny_ticket_id = a.znuny_ticket_id
            WHERE z.owner = ? AND a.via != 'Phone' AND a.created_by = ?
              AND a.sender != '' AND a.sender != ?
            """,
            (bot, bot, bot),
        ).fetchall()
        for r in rows:
            if len(samples) < 8:
                samples.append(f"  znuny_articles#{r['id']} (non-Phone) created_by: {r['created_by']!r} -> {r['sender']!r}")
            if apply:
                cur.execute("UPDATE znuny_articles SET created_by = ? WHERE id = ?", (r["sender"], r["id"]))
        counts["znuny_articles_restored"] += len(rows)

    print("\nRows with wrong created_by (to be fixed):" if not apply else "\nRows fixed:")
    for k, v in counts.items():
        print(f"  {k}: {v}")
    if samples:
        print("\nSample changes:")
        for s in samples:
            print(s)

    if apply:
        cur.execute(
            "INSERT OR REPLACE INTO app_settings (key, value, description) VALUES (?, ?, ?)",
            (MIGRATION_FLAG, "1",
             "Backfilled created_by to the bot login for bot-owned Phone articles, and "
             "restored real staff attribution (from sender) on non-Phone articles v1 had "
             "over-corrected"),
        )
        conn.commit()
        print("\nDone.")
    else:
        total = sum(counts.values())
        print(f"\nDry-run only -- {total} row(s) would change. Re-run with --apply to commit.")
    conn.close()


if __name__ == "__main__":
    main()
