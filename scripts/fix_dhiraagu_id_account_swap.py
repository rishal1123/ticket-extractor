"""Startup repair: un-swap Dhiraagu ``ticket_id`` / ``account`` on open tickets.

Background
----------
Dhiraagu's list view carries two identifiers per row: the **Order #** (this
portal's ticket identity, e.g. ``0129530729``) and the **Service #** (the
circuit code, e.g. ``BB20052084``). The mapping has always been::

    Order #   -> tickets.ticket_id
    Service # -> tickets.account

A Nov 2026 portal Filament upgrade shifted the table columns. The extractor fix
for it relabeled the column constants correctly but kept building the Ticket as
``ticket_id=service_num`` / ``account=order_num``, so from that deploy until the
mapping was restored, every Dhiraagu ticket was stored with the two values
swapped.

That does more than mislabel a column. Because ``ticket_id`` is the portal's
ticket identity, an order that was already open got re-inserted under its
Service # as a brand-new row, while the original row — no longer "found" on the
portal under the id the extractor was now using — was debounced and marked
complete. So a typical affected order now has:

  * a swapped, currently-open duplicate row (``ticket_id`` = Service #), holding
    the recent portal data and possibly a Znuny link found via the Service # in
    the Znuny title, with a ``created_at`` of the day the bug deployed — which
    makes its time-to-create metric meaningless; and
  * the original row, wrongly completed, holding the real first-seen
    ``created_at``, the original Znuny link, and all article / site-visit /
    reopen history.

This repair walks the currently-open Dhiraagu tickets and, for each one it can
prove is swapped:

  * **No original row exists** (the order first appeared after the bug): swap
    ``ticket_id`` and ``account`` back in place.
  * **The original row exists**: merge into it and drop the duplicate — revive
    the original (``completed_at`` cleared, since the order *is* open on the
    portal), fill its fields from the fresher duplicate where the duplicate has
    a value, keep the original's ``created_at`` (and its Znuny link, taking the
    duplicate's only if the original has none), re-point the duplicate's
    articles / site visits / note history / reopens / Znuny-side rows at it,
    then delete the duplicate row.

Running this BEFORE the first extraction of a boot matters: with the mapping
restored, the extractor keys on the Order # again, so an un-repaired original
row would be re-found while still marked complete and recorded as a *reopen* —
which wipes its Znuny link and raw_dump and resets its created_at, destroying
exactly the history this repair preserves.

How a swap is identified
------------------------
Primary (authoritative): the ticket's captured ``raw_dump`` is the order detail
page itself, which labels both numbers::

    Order number *
    0129530729
    Service number *
    BB20052084

If the dump's Service # is what's stored in ``ticket_id``, the row is swapped,
full stop. If the dump's Order # is stored there, the row is correct and is left
alone. Label-only dumps (pre-form-value-capture) and anything that doesn't parse
into two distinct plain tokens are treated as "no dump".

Fallback (shape), only for rows with no usable dump: a row is treated as swapped
only when ``ticket_id`` looks unambiguously like a Service # (``BB…``/``TV…``/
``FIB…``/``HMF…``, or a short all-digit account) *and* ``account`` looks
unambiguously like an Order # (9+ leading digits, possibly with an ``R1``/``_D``
suffix, or ``SSNET-SO-…``). Both conditions must hold, so a correctly-stored row
can't match. Anything ambiguous is reported and skipped, never guessed at.

Idempotent and deliberately NOT flag-guarded: it only ever touches rows it can
prove are swapped, so on a healthy database it finds nothing and exits in well
under a second. Leaving it unflagged means later boots also pick up stragglers
(e.g. a row whose raw_dump hadn't been captured yet on the first run). Takes a
timestamped DB backup only when it is actually about to change something.

Auto-run: entrypoint.sh runs this with --apply on every container start, before
the app (and therefore before any extraction).

Run inside the container:
    docker exec ticket-extractor python scripts/fix_dhiraagu_id_account_swap.py            # dry-run (default)
    docker exec ticket-extractor python scripts/fix_dhiraagu_id_account_swap.py --apply    # apply the fix
    docker exec ticket-extractor python scripts/fix_dhiraagu_id_account_swap.py --apply --include-completed
Or locally:
    python scripts/fix_dhiraagu_id_account_swap.py --apply
"""
import os
import re
import sys
import json
import shutil
import sqlite3
import argparse
from datetime import datetime, timedelta, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB_PATH = os.getenv("DATABASE_PATH", os.path.join(ROOT, "data", "tickets.db"))
if not os.path.exists(DB_PATH) or os.path.getsize(DB_PATH) == 0:
    DB_PATH = os.path.join(ROOT, "tickets.db")

sys.path.insert(0, ROOT)
from formatter.model.formatters import field_after_label  # noqa: E402

MVT = timezone(timedelta(hours=5))

# A parsed identifier: one bare token. Rejects the label-only dumps captured
# before the Filament form-value fix, where the "value" after a label line is
# just the NEXT LABEL ("Service number *") rather than a number.
TOKEN_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_\-/]{2,31}$")

# Shape fallback, used only when a row has no usable dump. Kept deliberately
# narrow: every real value seen in the table matches exactly one of these.
#   Service # : BB20052084, TV008119, FIB9433, HMF0200, or a short account (3333047)
#   Order #   : 0129530729, 0129440329_R, 018751157R1, SSNET-SO-2026-09-00069, BB17893875_D
SERVICE_LIKE = re.compile(r"^(?:[A-Za-z]{2,4}\d{4,}|\d{1,8})$")
ORDER_LIKE = re.compile(
    r"^(?:\d{9,}[A-Za-z0-9_]*|SSNET-[A-Za-z0-9\-]+|[A-Za-z]{2,4}\d{4,}_[A-Za-z0-9]{1,3})$",
    re.IGNORECASE,
)

# Portal-scraped fields: the swapped duplicate is the fresher scrape, so its
# value wins where it has one (COALESCE(duplicate, original)).
PORTAL_FIELDS = (
    "address", "customer_name", "ticket_type", "service_type",
    "status", "kpi", "notes", "portal_created_at", "portal_url", "raw_dump",
)
# Znuny link: taken from the duplicate ONLY if the original has no link at all,
# so a half-original/half-duplicate link can never be stitched together.
ZNUNY_FIELDS = (
    "in_znuny", "znuny_ticket_id", "znuny_created_at", "znuny_created_by",
    "znuny_address", "znuny_url",
)
# Tables holding a tickets.id reference that must follow a merged row. None of
# their uniqueness constraints involve ticket_id (znuny_articles is unique on
# (znuny_ticket_id, article_number), site_visits on (znuny_ticket_id,
# article_id)), so re-pointing can't collide.
CHILD_TABLES = (
    ("znuny_articles", "ticket_id"),
    ("site_visits", "ticket_id"),
    ("ticket_notes_history", "ticket_id"),
    ("znuny_tickets", "isp_ticket_id"),
)


def dump_ids(raw_dump: str | None) -> tuple[str | None, str | None]:
    """(order_no, service_no) as the captured detail-page dump states them.

    Returns (None, None) when the dump is missing, label-only, or doesn't yield
    two distinct plain tokens — i.e. whenever it can't be trusted to decide.
    """
    if not raw_dump or not raw_dump.strip():
        return None, None
    order = (field_after_label(raw_dump, "Order number") or "").strip()
    service = (field_after_label(raw_dump, "Service number") or "").strip()
    if not TOKEN_RE.match(order) or not TOKEN_RE.match(service):
        return None, None
    if order.casefold() == service.casefold():
        return None, None
    return order, service


def shape_swapped(ticket_id: str | None, account: str | None) -> bool:
    """True only when BOTH ids unambiguously sit in the wrong column."""
    if not ticket_id or not account:
        return False
    return bool(
        SERVICE_LIKE.match(ticket_id) and not ORDER_LIKE.match(ticket_id)
        and ORDER_LIKE.match(account) and not SERVICE_LIKE.match(account)
    )


def classify(row: sqlite3.Row) -> tuple[str, str | None, str | None]:
    """(verdict, order_no, service_no) for one ticket row.

    verdict: 'swapped-dump' | 'swapped-shape' | 'ok' | 'unknown'
    """
    ticket_id = (row["ticket_id"] or "").strip()
    account = (row["account"] or "").strip() or None

    order_no, service_no = dump_ids(row["raw_dump"])
    if order_no:
        if ticket_id.casefold() == service_no.casefold():
            return "swapped-dump", order_no, service_no
        if ticket_id.casefold() == order_no.casefold():
            return "ok", order_no, service_no
        # ticket_id matches neither number on its own detail page — not this
        # bug; don't touch it.
        return "unknown", order_no, service_no

    if shape_swapped(ticket_id, account):
        return "swapped-shape", account, ticket_id
    return "ok" if ticket_id else "unknown", None, None


def merge_into_original(cur, dup: sqlite3.Row, keeper: sqlite3.Row,
                        order_no: str, service_no: str, now: str) -> None:
    """Fold the swapped duplicate into the original row, then delete it."""
    kid, did = keeper["id"], dup["id"]

    # Portal-scraped fields: duplicate wins where it has a value. Also revive
    # the original — the order is open on the portal, that's why we're here.
    sets = ", ".join(f"{f} = COALESCE(?, {f})" for f in PORTAL_FIELDS)
    cur.execute(
        f"UPDATE tickets SET account = ?, {sets}, completed_at = NULL, updated_at = ? WHERE id = ?",
        (service_no, *(dup[f] for f in PORTAL_FIELDS), now, kid),
    )

    # Znuny link: only when the original has none.
    if not keeper["znuny_ticket_id"] and dup["znuny_ticket_id"]:
        assigns = ", ".join(f"{f} = ?" for f in ZNUNY_FIELDS)
        cur.execute(
            f"UPDATE tickets SET {assigns} WHERE id = ?",
            (*(dup[f] for f in ZNUNY_FIELDS), kid),
        )

    for table, column in CHILD_TABLES:
        cur.execute(f"UPDATE {table} SET {column} = ? WHERE {column} = ?", (kid, did))
    cur.execute(
        "UPDATE ticket_reopens SET ticket_id = ?, portal_ticket_id = ? WHERE ticket_id = ?",
        (kid, order_no, did),
    )

    cur.execute("DELETE FROM tickets WHERE id = ?", (did,))


def rename_in_place(cur, row: sqlite3.Row, order_no: str, service_no: str, now: str) -> None:
    """No original row to merge with — just put the two ids back in their columns."""
    cur.execute(
        "UPDATE tickets SET ticket_id = ?, account = ?, updated_at = ? WHERE id = ?",
        (order_no, service_no, now, row["id"]),
    )
    cur.execute(
        "UPDATE ticket_reopens SET portal_ticket_id = ? WHERE ticket_id = ?",
        (order_no, row["id"]),
    )


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Un-swap Dhiraagu ticket_id/account on open tickets (and merge the duplicate rows the swap created)."
    )
    ap.add_argument("--apply", action="store_true", help="Apply the fix (default is a dry-run).")
    ap.add_argument("--include-completed", action="store_true",
                    help="Also repair completed tickets (default: currently-open only).")
    args = ap.parse_args()

    if not os.path.exists(DB_PATH):
        print(f"[ERROR] Database not found at {DB_PATH}")
        return 1

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()

    scope = "" if args.include_completed else " AND completed_at IS NULL"
    cur.execute(f"SELECT * FROM tickets WHERE portal = 'dhiraagu'{scope} ORDER BY id")
    rows = cur.fetchall()

    plan = []          # (row, order_no, service_no, verdict)
    counts = {"ok": 0, "unknown": 0, "swapped-dump": 0, "swapped-shape": 0}
    for row in rows:
        verdict, order_no, service_no = classify(row)
        counts[verdict] += 1
        if verdict.startswith("swapped"):
            plan.append((row, order_no, service_no, verdict))

    print(f"Database   : {DB_PATH}")
    print(f"Scope      : {'all' if args.include_completed else 'currently-open'} Dhiraagu tickets ({len(rows)})")
    print(f"Mode       : {'APPLY' if args.apply else 'DRY-RUN (no changes; pass --apply to write)'}")
    print(f"Correct    : {counts['ok']}")
    print(f"Undecidable: {counts['unknown']} (no usable dump and ambiguous ids — left untouched)")
    print(f"Swapped    : {len(plan)} ({counts['swapped-dump']} confirmed by detail-page dump, "
          f"{counts['swapped-shape']} inferred from id shape)")

    if not plan:
        print("\nNothing to fix.")
        conn.close()
        return 0

    if args.apply:
        bak = f"{DB_PATH}.bak-dhiraaguswap-{datetime.now().strftime('%Y%m%d%H%M%S')}"
        shutil.copy2(DB_PATH, bak)
        print(f"Backup     : {bak}")

    now = datetime.now(MVT).isoformat(sep=" ")
    merged = renamed = 0
    samples = []

    for row, order_no, service_no, verdict in plan:
        cur.execute(
            "SELECT * FROM tickets WHERE portal = 'dhiraagu' AND ticket_id = ? AND id != ?",
            (order_no, row["id"]),
        )
        keeper = cur.fetchone()

        if keeper is None:
            action = "rename"
            renamed += 1
            if args.apply:
                rename_in_place(cur, row, order_no, service_no, now)
        else:
            action = f"merge into id={keeper['id']}"
            merged += 1
            if args.apply:
                merge_into_original(cur, row, keeper, order_no, service_no, now)

        if len(samples) < 25:
            samples.append(
                f"  id={row['id']} ticket_id={row['ticket_id']!r} account={row['account']!r}"
                f" -> ticket_id={order_no!r} account={service_no!r}  [{action}, {verdict}]"
            )

    print(f"\n{'Renamed' if args.apply else 'Would rename'} in place : {renamed}")
    print(f"{'Merged' if args.apply else 'Would merge'} into original: {merged}")
    if samples:
        print("\nSample changes:")
        for s in samples:
            print(s)

    if args.apply:
        cur.execute(
            "INSERT INTO system_logs (level, source, message, details, created_at) VALUES (?, ?, ?, ?, ?)",
            ("warning", "migration.dhiraagu_swap",
             f"Corrected swapped Dhiraagu ticket_id/account on {len(plan)} ticket(s)",
             json.dumps({"renamed": renamed, "merged": merged,
                         "confirmed_by_dump": counts["swapped-dump"],
                         "inferred_by_shape": counts["swapped-shape"],
                         "undecidable": counts["unknown"]}),
             now),
        )
        conn.commit()
        print("\nDone.")
    else:
        print("\nDry-run only — re-run with --apply to commit.")

    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
