"""Startup repair: recover NULL Medianet ``account`` / ``customer_name`` from ``raw_dump``.

Background
----------
Medianet's CRM.COM portal refreshed its UI in Oct 2026, moving the account
number out of the contact name ("Rise and Shine (7610023404100858)") onto its
own muted line in the Contact card ("Account-(282326)"). Two defects in the
extractor's handling of that layout left rows with a NULL account:

1. The refresh fix read only the *first* ``span.text-muted`` in the Contact
   card and made the two account sources mutually exclusive (``if`` / ``elif``
   on whether any muted text was present). A card whose first muted span was a
   phone or email, or whose account line was the empty ``Account-()`` form the
   portal renders for contacts with no account id, stored NULL even though the
   number was sitting in the contact name.
2. The Contact card loads on its own XHR, after the breadcrumb the parse waited
   for. A blind one-second sleep raced it, so the card could be absent from the
   DOM entirely at parse time — which zeroed ``customer_name`` as well as
   ``account``.

Both are fixed in ``extractors/medianet.py``, but only for tickets extracted
from here on. Already-stored rows never self-heal: Medianet participates in the
extract-once scheme, so a ticket already in the DB is routed to
``touch_tickets_seen()`` (presence only) and its detail page is never opened
again.

Why the raw_dump can repair them
--------------------------------
The raw dump is captured in a *separate, later* pass in ``base.run()`` — its own
navigation with a longer settle — so it routinely holds the contact data the
racing detail parse missed. That is exactly the signature the bug report showed:
ticket S41882 rendered its phone (parsed live from ``raw_dump``) while account
and customer both displayed "-" (read from the NULL DB columns).

``MedianetFormatter._contact()`` parses that dump by line *shape* rather than
CSS class and handles both the pre- and post-refresh layouts. Validated against
the 809 Medianet dumps in a production snapshot: it recovered the account for
795 of them with **zero** disagreements against the stored value. The 14
failures were all truncated or HTTP-403 dumps captured when the page had not
loaded — no contact block to parse — and this repair simply leaves those alone.

What it does
------------
For every Medianet ticket with a usable ``raw_dump`` and a NULL/empty
``account`` or ``customer_name``, parse the dump and fill **only** the empty
columns. A column that already holds a value is never overwritten, so this
cannot damage correctly-extracted rows, and a dump that yields nothing for a
field leaves that field untouched.

Deliberately NOT flag-guarded, for the same reason as the Dhiraagu swap repair:
it only ever writes into empty columns, so on a healthy DB it finds nothing and
returns in well under a second. Staying unflagged means later restarts also
catch stragglers — in particular a row whose ``raw_dump`` had not been captured
yet when an earlier run looked at it, which is common because the dump pass
backfills only five older tickets per cycle. It takes a DB backup only when it
is actually about to change something.

Usage
-----
    python scripts/fix_medianet_contact_fields.py            # dry-run
    python scripts/fix_medianet_contact_fields.py --apply    # commit
"""

import argparse
import json
import os
import shutil
import sqlite3
import sys
from datetime import datetime, timedelta, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB_PATH = os.getenv("DATABASE_PATH", os.path.join(ROOT, "data", "tickets.db"))
if not os.path.exists(DB_PATH) or os.path.getsize(DB_PATH) == 0:
    DB_PATH = os.path.join(ROOT, "tickets.db")

sys.path.insert(0, ROOT)
from formatter.model.formatters import MedianetFormatter  # noqa: E402

MVT = timezone(timedelta(hours=5))


def parse_contact(formatter: MedianetFormatter, raw_dump: str) -> tuple[str | None, str | None]:
    """Return (account, customer_name) recovered from a dump, or (None, None)."""
    try:
        name, account, _phone, _address = formatter._contact(raw_dump)
    except Exception:
        return None, None
    return (account or "").strip() or None, (name or "").strip() or None


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Recover NULL Medianet account/customer_name from raw_dump."
    )
    ap.add_argument("--apply", action="store_true", help="Apply the fix (default is a dry-run).")
    args = ap.parse_args()

    if not os.path.exists(DB_PATH):
        print(f"No database at {DB_PATH} - nothing to do.")
        return 0

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()

    print(f"Database   : {DB_PATH}")
    print(f"Mode       : {'APPLY' if args.apply else 'dry-run'}")

    cur.execute(
        """
        SELECT id, ticket_id, account, customer_name, raw_dump
          FROM tickets
         WHERE portal = 'medianet'
           AND raw_dump IS NOT NULL AND raw_dump != ''
           AND (account IS NULL OR account = ''
                OR customer_name IS NULL OR customer_name = '')
        """
    )
    rows = cur.fetchall()
    print(f"Candidates : {len(rows)} row(s) with a dump and a missing contact field")

    formatter = MedianetFormatter()
    plan = []
    unparsable = 0

    for row in rows:
        account, name = parse_contact(formatter, row["raw_dump"])

        # Fill only what's actually empty - never overwrite a stored value.
        set_account = account if not (row["account"] or "").strip() else None
        set_name = name if not (row["customer_name"] or "").strip() else None

        if set_account or set_name:
            plan.append((row, set_account, set_name))
        else:
            unparsable += 1

    print(f"Recoverable: {len(plan)}")
    print(f"No contact block in dump (left alone): {unparsable}")

    if not plan:
        print("\nNothing to fix.")
        conn.close()
        return 0

    if args.apply:
        bak = f"{DB_PATH}.bak-medianetcontact-{datetime.now().strftime('%Y%m%d%H%M%S')}"
        shutil.copy2(DB_PATH, bak)
        print(f"Backup     : {bak}")

    now = datetime.now(MVT).isoformat(sep=" ")
    accounts_set = names_set = 0
    samples = []

    for row, set_account, set_name in plan:
        assignments = []
        params = []
        if set_account:
            assignments.append("account = ?")
            params.append(set_account)
            accounts_set += 1
        if set_name:
            assignments.append("customer_name = ?")
            params.append(set_name)
            names_set += 1

        if args.apply:
            # updated_at is deliberately left alone: these rows are not changing
            # on the portal, and the dashboard's staleness indicators key off it.
            cur.execute(
                f"UPDATE tickets SET {', '.join(assignments)} WHERE id = ?",
                (*params, row["id"]),
            )

        if len(samples) < 25:
            samples.append(
                f"  id={row['id']} {row['ticket_id']}: "
                f"account={row['account']!r} -> {set_account or row['account']!r}, "
                f"customer={row['customer_name']!r} -> {set_name or row['customer_name']!r}"
            )

    print(f"\n{'Set' if args.apply else 'Would set'} account      : {accounts_set}")
    print(f"{'Set' if args.apply else 'Would set'} customer_name: {names_set}")
    if samples:
        print("\nSample changes:")
        for s in samples:
            print(s)

    if args.apply:
        cur.execute(
            "INSERT INTO system_logs (level, source, message, details, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            ("warning", "migration.medianet_contact",
             f"Recovered Medianet contact fields on {len(plan)} ticket(s) from raw_dump",
             json.dumps({"rows": len(plan), "accounts_set": accounts_set,
                         "names_set": names_set, "unparsable_dumps": unparsable}),
             now),
        )
        conn.commit()
        print("\nDone.")
    else:
        print("\nDry-run only - re-run with --apply to commit.")

    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
