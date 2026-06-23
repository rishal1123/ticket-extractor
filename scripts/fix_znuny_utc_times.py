"""One-time production fix: Znuny REST timestamps were stored 5 hours early.

The Znuny Generic Interface REST API returns timestamps in Znuny's system
timezone, which is UTC (OTRSTimeZone). The old `znuny_client._parse_dt()` tagged
those raw strings as Maldives time (+05:00) WITHOUT converting, so every
Znuny-sourced time was stored 5 hours behind reality (e.g. a 1pm ticket showed
as 8am). The code is fixed going forward; this script repairs existing rows.

It shifts these columns by +5 hours:
    tickets.znuny_created_at
    znuny_tickets.created_at, closed_at, last_article_at
    znuny_articles.created_at, created_at_str
    site_visits.article_created_at, ticket_completed_at (+ recompute visit_date)

Rows from the old Playwright-scraping era are ALREADY correct Maldives time and
are skipped. They are detected dynamically: any article whose `created_at_str`
contains "(Indian/Maldives)" is a pre-converted UI string, and that article and
its parent ticket are excluded.

Idempotent: sets `tz_utc_migration_done=1` in app_settings and refuses to run
twice. Takes a timestamped backup of the DB before applying.

Run inside the container:
    docker exec ticket-extractor python scripts/fix_znuny_utc_times.py          # dry-run (default)
    docker exec ticket-extractor python scripts/fix_znuny_utc_times.py --apply  # apply the fix
Or locally:
    python scripts/fix_znuny_utc_times.py --apply
"""
import os
import sys
import shutil
import sqlite3
import argparse
from datetime import datetime, timezone, timedelta

# Resolve the DB path the same way config.py / fix_data.py do.
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB_PATH = os.getenv("DATABASE_PATH", os.path.join(ROOT, "data", "tickets.db"))
if not os.path.exists(DB_PATH) or os.path.getsize(DB_PATH) == 0:
    DB_PATH = os.path.join(ROOT, "tickets.db")

MVT = timezone(timedelta(hours=5))
SHIFT = timedelta(hours=5)
GUARD_KEY = "tz_utc_migration_done"


def shift_iso(val):
    """'YYYY-MM-DD HH:MM:SS+05:00' (offset-aware) -> +5h, same format. None if unparseable."""
    if not val or not str(val).strip():
        return None
    try:
        dt = datetime.fromisoformat(val)
    except ValueError:
        return None
    return (dt + SHIFT).isoformat(sep=" ")


def shift_plain(val):
    """'YYYY-MM-DD HH:MM:SS' (naive) -> +5h, same format. None if unparseable."""
    if not val or not str(val).strip():
        return None
    try:
        dt = datetime.strptime(val, "%Y-%m-%d %H:%M:%S")
    except ValueError:
        try:
            dt = datetime.fromisoformat(val)
        except ValueError:
            return None
    return (dt + SHIFT).strftime("%Y-%m-%d %H:%M:%S")


def main():
    ap = argparse.ArgumentParser(description="Shift mislabeled Znuny UTC timestamps by +5h to Maldives time.")
    ap.add_argument("--apply", action="store_true", help="Apply the fix (default is a dry-run).")
    args = ap.parse_args()
    apply = args.apply

    if not os.path.exists(DB_PATH):
        print(f"[ERROR] Database not found at {DB_PATH}")
        sys.exit(1)
    print(f"Database: {DB_PATH}")
    print(f"Mode    : {'APPLY' if apply else 'DRY-RUN (no changes; pass --apply to write)'}")

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()

    # Idempotency guard.
    row = cur.execute("SELECT value FROM app_settings WHERE key=?", (GUARD_KEY,)).fetchone()
    if row and str(row[0]) == "1":
        print(f"[SKIP] Guard '{GUARD_KEY}' already set -> migration was applied before. Nothing to do.")
        conn.close()
        return

    # Detect already-correct Playwright-era tickets (UI strings carry "(Indian/Maldives)").
    excluded = {
        r[0] for r in cur.execute(
            "SELECT DISTINCT znuny_ticket_id FROM znuny_articles "
            "WHERE created_at_str LIKE '%Maldives%' AND znuny_ticket_id IS NOT NULL"
        )
    }
    print(f"Already-correct (Playwright-era) tickets excluded: {len(excluded)}")
    if excluded:
        print("  " + ", ".join(sorted(excluded)))

    placeholders = ",".join("?" * len(excluded)) if excluded else "''"
    excl = list(excluded)
    counts = {}

    # Take a backup before writing.
    if apply:
        bak = f"{DB_PATH}.bak-tz-{datetime.now().strftime('%Y%m%d%H%M%S')}"
        shutil.copy2(DB_PATH, bak)
        print(f"Backup  : {bak}")

    # 1) tickets.znuny_created_at
    rows = cur.execute(
        f"SELECT id, znuny_created_at FROM tickets "
        f"WHERE znuny_created_at IS NOT NULL AND znuny_created_at != '' "
        f"AND (znuny_ticket_id IS NULL OR znuny_ticket_id NOT IN ({placeholders}))",
        excl,
    ).fetchall()
    n = 0
    for r in rows:
        nv = shift_iso(r["znuny_created_at"])
        if nv:
            if apply:
                cur.execute("UPDATE tickets SET znuny_created_at=? WHERE id=?", (nv, r["id"]))
            n += 1
    counts["tickets.znuny_created_at"] = n

    # 2) znuny_tickets.created_at / closed_at / last_article_at
    rows = cur.execute(
        f"SELECT id, created_at, closed_at, last_article_at FROM znuny_tickets "
        f"WHERE znuny_ticket_id NOT IN ({placeholders})",
        excl,
    ).fetchall()
    n = 0
    for r in rows:
        ca, cl, la = shift_iso(r["created_at"]), shift_iso(r["closed_at"]), shift_iso(r["last_article_at"])
        if ca or cl or la:
            if apply:
                cur.execute(
                    "UPDATE znuny_tickets SET created_at=COALESCE(?,created_at), "
                    "closed_at=COALESCE(?,closed_at), last_article_at=COALESCE(?,last_article_at) WHERE id=?",
                    (ca, cl, la, r["id"]),
                )
            n += 1
    counts["znuny_tickets (created/closed/last_article)"] = n

    # 3) znuny_articles.created_at + created_at_str (skip the "(Indian/Maldives)" rows)
    rows = cur.execute(
        "SELECT id, created_at, created_at_str FROM znuny_articles "
        "WHERE created_at_str IS NULL OR created_at_str NOT LIKE '%Maldives%'"
    ).fetchall()
    n = 0
    for r in rows:
        nv = shift_iso(r["created_at"])
        if r["created_at_str"] and r["created_at_str"].strip():
            ns = shift_plain(r["created_at_str"])
        else:
            ns = nv[:19] if nv else None
        if nv or ns:
            if apply:
                cur.execute(
                    "UPDATE znuny_articles SET created_at=COALESCE(?,created_at), "
                    "created_at_str=COALESCE(?,created_at_str) WHERE id=?",
                    (nv, ns, r["id"]),
                )
            n += 1
    counts["znuny_articles"] = n

    # 4) site_visits.article_created_at / ticket_completed_at (+ recompute visit_date)
    rows = cur.execute(
        f"SELECT id, article_created_at, ticket_completed_at FROM site_visits "
        f"WHERE znuny_ticket_id IS NULL OR znuny_ticket_id NOT IN ({placeholders})",
        excl,
    ).fetchall()
    n = 0
    for r in rows:
        ac, tc = shift_iso(r["article_created_at"]), shift_iso(r["ticket_completed_at"])
        vd = ac[:10] if ac else None
        if ac or tc:
            if apply:
                cur.execute(
                    "UPDATE site_visits SET article_created_at=COALESCE(?,article_created_at), "
                    "ticket_completed_at=COALESCE(?,ticket_completed_at), visit_date=COALESCE(?,visit_date) WHERE id=?",
                    (ac, tc, vd, r["id"]),
                )
            n += 1
    counts["site_visits"] = n

    print("\nRows to shift by +5h:" if not apply else "\nRows shifted by +5h:")
    for k, v in counts.items():
        print(f"  {k}: {v}")

    if apply:
        cur.execute(
            "INSERT OR REPLACE INTO app_settings(key, value, updated_at) VALUES(?,?,?)",
            (GUARD_KEY, "1", datetime.now(MVT).isoformat()),
        )
        conn.commit()
        print(f"\nGuard set: {GUARD_KEY}=1. Done.")
    else:
        print("\nDry-run only -- no changes written. Re-run with --apply to commit.")
    conn.close()


if __name__ == "__main__":
    main()