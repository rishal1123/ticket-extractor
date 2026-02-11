#!/usr/bin/env python3
"""
Runtime data fix script for deployed servers.
Run this to check and fix common data issues without a full sync cycle.

Usage:
    python fix_data.py              # Run all checks (dry-run by default)
    python fix_data.py --fix        # Apply all fixes
    python fix_data.py --check      # Only show issues, don't fix
"""

import os
import re
import sys
import sqlite3
from datetime import datetime, timezone, timedelta

# Database path (same logic as config.py)
DB_PATH = os.getenv("DATABASE_PATH", os.path.join(os.path.dirname(__file__), "data", "tickets.db"))
if not os.path.exists(DB_PATH):
    # Try legacy path
    DB_PATH = os.path.join(os.path.dirname(__file__), "tickets.db")

MVT = timezone(timedelta(hours=5))


def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def fix_site_visit_assigned_to(conn, dry_run=True):
    """Fix multi-staff assigned_to values: '@aslan @ayan' -> 'aslan, ayan'"""
    cursor = conn.cursor()
    cursor.execute("SELECT id, assigned_to FROM site_visits WHERE assigned_to LIKE '%@%'")
    rows = cursor.fetchall()

    if not rows:
        print("  [OK] No site visits with @ in assigned_to")
        return 0

    fixed = 0
    for row in rows:
        old_val = row["assigned_to"]
        # Split on @ and whitespace, extract names, rejoin with comma
        names = [n.strip() for n in re.split(r'\s*@\s*', old_val) if n.strip()]
        new_val = ", ".join(names)

        if old_val != new_val:
            print(f"  FIX id={row['id']}: '{old_val}' -> '{new_val}'")
            if not dry_run:
                cursor.execute(
                    "UPDATE site_visits SET assigned_to = ?, updated_at = ? WHERE id = ?",
                    (new_val, datetime.now(MVT).isoformat(), row["id"])
                )
            fixed += 1

    if not dry_run and fixed:
        conn.commit()

    return fixed


def fix_empty_article_bodies(conn, dry_run=True):
    """Report articles with empty bodies that need re-fetching."""
    cursor = conn.cursor()
    cursor.execute("""
        SELECT COUNT(*) as total,
               SUM(CASE WHEN body IS NULL OR body = '' THEN 1 ELSE 0 END) as empty
        FROM znuny_articles
    """)
    row = cursor.fetchone()
    total = row["total"] or 0
    empty = row["empty"] or 0

    if empty == 0:
        print(f"  [OK] All {total} articles have body content")
    else:
        print(f"  [INFO] {empty}/{total} articles have empty bodies")
        print(f"         These will be populated on next Znuny sync cycle")

    return empty


def check_orphan_znuny_links(conn, dry_run=True):
    """Check for tickets marked in_znuny but with no znuny_ticket_id."""
    cursor = conn.cursor()
    cursor.execute("""
        SELECT id, portal, ticket_id FROM tickets
        WHERE in_znuny = 1 AND (znuny_ticket_id IS NULL OR znuny_ticket_id = '')
    """)
    rows = cursor.fetchall()

    if not rows:
        print("  [OK] All in_znuny tickets have znuny_ticket_id")
        return 0

    for row in rows:
        print(f"  WARN id={row['id']} {row['portal']}/{row['ticket_id']}: in_znuny=1 but no znuny_ticket_id")
        if not dry_run:
            cursor.execute(
                "UPDATE tickets SET in_znuny = 0, updated_at = ? WHERE id = ?",
                (datetime.now(MVT).isoformat(), row["id"])
            )

    if not dry_run and rows:
        conn.commit()
        print(f"  Fixed {len(rows)} orphan znuny links (set in_znuny=0)")

    return len(rows)


def check_duplicate_site_visits(conn, dry_run=True):
    """Check for duplicate site visits (same znuny_ticket_id + article_id)."""
    cursor = conn.cursor()
    cursor.execute("""
        SELECT znuny_ticket_id, article_id, COUNT(*) as cnt
        FROM site_visits
        GROUP BY znuny_ticket_id, article_id
        HAVING cnt > 1
    """)
    rows = cursor.fetchall()

    if not rows:
        print("  [OK] No duplicate site visits")
        return 0

    for row in rows:
        print(f"  WARN znuny={row['znuny_ticket_id']} article={row['article_id']}: {row['cnt']} duplicates")

    return len(rows)


def check_stale_pending_visits(conn, dry_run=True):
    """Check for site visits still pending but ticket is closed."""
    cursor = conn.cursor()
    cursor.execute("""
        SELECT sv.id, sv.znuny_ticket_id, sv.assigned_to, sv.visit_date,
               zt.state as znuny_state
        FROM site_visits sv
        LEFT JOIN znuny_tickets zt ON sv.znuny_ticket_id = zt.znuny_ticket_id
        WHERE sv.status = 'pending'
        AND zt.state = 'closed'
    """)
    rows = cursor.fetchall()

    if not rows:
        print("  [OK] No pending visits for closed tickets")
        return 0

    fixed = 0
    for row in rows:
        print(f"  FIX sv_id={row['id']} znuny={row['znuny_ticket_id']} assigned={row['assigned_to']}: pending but ticket closed")
        if not dry_run:
            cursor.execute(
                "UPDATE site_visits SET status = 'completed', updated_at = ? WHERE id = ?",
                (datetime.now(MVT).isoformat(), row["id"])
            )
            fixed += 1

    if not dry_run and fixed:
        conn.commit()

    return len(rows)


def show_stats(conn):
    """Show database statistics."""
    cursor = conn.cursor()

    stats = {}
    for table in ["tickets", "znuny_articles", "znuny_tickets", "site_visits", "extraction_logs", "system_logs"]:
        try:
            cursor.execute(f"SELECT COUNT(*) as cnt FROM {table}")
            stats[table] = cursor.fetchone()["cnt"]
        except Exception:
            stats[table] = "N/A"

    cursor.execute("SELECT COUNT(*) as cnt FROM tickets WHERE completed_at IS NULL")
    active = cursor.fetchone()["cnt"]

    cursor.execute("SELECT COUNT(*) as cnt FROM tickets WHERE in_znuny = 1")
    in_znuny = cursor.fetchone()["cnt"]

    cursor.execute("SELECT COUNT(*) as cnt FROM site_visits WHERE status = 'pending'")
    pending_visits = cursor.fetchone()["cnt"]

    print(f"\n{'='*50}")
    print(f"Database: {DB_PATH}")
    print(f"{'='*50}")
    print(f"  Tickets:        {stats['tickets']:>6}  (active: {active}, in Znuny: {in_znuny})")
    print(f"  Znuny Articles: {stats['znuny_articles']:>6}")
    print(f"  Znuny Tickets:  {stats['znuny_tickets']:>6}")
    print(f"  Site Visits:    {stats['site_visits']:>6}  (pending: {pending_visits})")
    print(f"  Extraction Logs:{stats['extraction_logs']:>6}")
    print(f"  System Logs:    {stats['system_logs']:>6}")
    print()


def main():
    dry_run = "--fix" not in sys.argv
    check_only = "--check" in sys.argv

    if not os.path.exists(DB_PATH):
        print(f"ERROR: Database not found at {DB_PATH}")
        sys.exit(1)

    conn = get_db()

    show_stats(conn)

    if dry_run:
        print("Running in DRY-RUN mode (use --fix to apply changes)\n")
    else:
        print("Running in FIX mode - changes will be applied\n")

    checks = [
        ("Site visit assigned_to cleanup", fix_site_visit_assigned_to),
        ("Empty article bodies", fix_empty_article_bodies),
        ("Orphan Znuny links", check_orphan_znuny_links),
        ("Duplicate site visits", check_duplicate_site_visits),
        ("Stale pending visits", check_stale_pending_visits),
    ]

    total_issues = 0
    for name, check_fn in checks:
        print(f"[{name}]")
        issues = check_fn(conn, dry_run=dry_run or check_only)
        total_issues += issues
        print()

    if total_issues == 0:
        print("All checks passed!")
    elif dry_run:
        print(f"Found {total_issues} issue(s). Run with --fix to apply changes.")
    else:
        print(f"Fixed {total_issues} issue(s).")

    conn.close()


if __name__ == "__main__":
    main()
