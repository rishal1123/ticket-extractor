"""Recovery fix for Znuny timestamps that got shifted +5h TWICE (future-dated).

Background: the code fix (znuny_client._parse_dt now converts API-UTC -> MVT)
and the one-time +5h data migration (scripts/fix_znuny_utc_times.py) overlap if
the new code is already running when the migration is applied: rows the new code
had just corrected to true Maldives time get an extra +5h from the migration,
landing 5 hours in the FUTURE. Example seen in prod: an article whose real
Znuny CreateTime is 12:16 MVT was stored as 17:16 MVT.

This script undoes the extra +5h. A correctly-stored timestamp can NEVER be in
the future, so subtracting 5h from any future-dated value only ever touches the
double-shifted rows -- it cannot corrupt a correct one. "Now" is taken from the
real instant (datetime.now in UTC -> Maldives), so it is correct whether the
container clock is UTC or already set to Indian/Maldives.

Columns checked: tickets.znuny_created_at; znuny_tickets.created_at / closed_at /
last_article_at; znuny_articles.created_at + created_at_str; site_visits
.article_created_at / ticket_completed_at (+ recompute visit_date).

Idempotent and re-runnable: after the fix nothing is future-dated, so a second
run finds nothing. Takes a timestamped DB backup before applying.

Run inside the container:
    docker exec ticket-extractor python scripts/fix_znuny_double_shift.py          # dry-run (default)
    docker exec ticket-extractor python scripts/fix_znuny_double_shift.py --apply  # apply the fix
Or locally:
    python scripts/fix_znuny_double_shift.py --apply
"""
import os
import sys
import shutil
import sqlite3
import argparse
from datetime import datetime, timezone, timedelta

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB_PATH = os.getenv("DATABASE_PATH", os.path.join(ROOT, "data", "tickets.db"))
if not os.path.exists(DB_PATH) or os.path.getsize(DB_PATH) == 0:
    DB_PATH = os.path.join(ROOT, "tickets.db")

MVT = timezone(timedelta(hours=5))
SHIFT = timedelta(hours=5)
# Small buffer so genuine "just now" rows at the exact current second are not
# mistaken for future-dated double-shifts.
BUFFER = timedelta(minutes=2)


def _parse_any(val):
    """Parse a stored timestamp into an aware datetime (assume MVT if naive). None if unparseable."""
    if not val or not str(val).strip():
        return None
    s = str(val).strip()
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
            try:
                dt = datetime.strptime(s, fmt)
                break
            except ValueError:
                dt = None
        if dt is None:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=MVT)
    return dt


def _reformat(dt, original):
    """Render dt back in the same textual style as the original stored string."""
    s = str(original)
    has_offset = ("+" in s[10:]) or s.endswith("Z")
    sep = "T" if "T" in s else " "
    if has_offset:
        return dt.isoformat(sep=sep)
    # Naive style (e.g. created_at_str): drop tz, keep seconds.
    return dt.astimezone(MVT).strftime("%Y-%m-%d %H:%M:%S")


def main():
    ap = argparse.ArgumentParser(description="Subtract 5h from future-dated (double-shifted) Znuny timestamps.")
    ap.add_argument("--apply", action="store_true", help="Apply the fix (default is a dry-run).")
    args = ap.parse_args()
    apply = args.apply

    if not os.path.exists(DB_PATH):
        print(f"[ERROR] Database not found at {DB_PATH}")
        sys.exit(1)

    now = datetime.now(timezone.utc).astimezone(MVT)
    cutoff = now + BUFFER
    print(f"Database : {DB_PATH}")
    print(f"Now (MVT): {now.strftime('%Y-%m-%d %H:%M:%S')}  -> future cutoff {cutoff.strftime('%H:%M:%S')}")
    print(f"Mode     : {'APPLY' if apply else 'DRY-RUN (no changes; pass --apply to write)'}")

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()

    if apply:
        bak = f"{DB_PATH}.bak-doubleshift-{datetime.now().strftime('%Y%m%d%H%M%S')}"
        shutil.copy2(DB_PATH, bak)
        print(f"Backup   : {bak}")

    # (table, columns to check/fix). visit_date is recomputed from article_created_at, not checked directly.
    targets = [
        ("tickets", "id", ["znuny_created_at"]),
        ("znuny_tickets", "id", ["created_at", "closed_at", "last_article_at"]),
        ("znuny_articles", "id", ["created_at", "created_at_str"]),
        ("site_visits", "id", ["article_created_at", "ticket_completed_at"]),
    ]

    counts = {}
    samples = []
    for table, pk, cols in targets:
        collist = ", ".join(cols)
        rows = cur.execute(f"SELECT {pk}, {collist} FROM {table}").fetchall()
        n = 0
        for r in rows:
            updates = {}
            for col in cols:
                dt = _parse_any(r[col])
                if dt is not None and dt > cutoff:
                    newdt = dt - SHIFT
                    updates[col] = _reformat(newdt, r[col])
            if not updates:
                continue
            # Recompute site_visits.visit_date if we moved article_created_at.
            if table == "site_visits" and "article_created_at" in updates:
                updates["visit_date"] = updates["article_created_at"][:10]
            if len(samples) < 8:
                col0 = cols[0]
                samples.append(f"  {table}#{r[pk]} {col0}: {r[col0]} -> {updates.get(col0, '(unchanged)')}")
            if apply:
                setclause = ", ".join(f"{c}=?" for c in updates)
                cur.execute(f"UPDATE {table} SET {setclause} WHERE {pk}=?", (*updates.values(), r[pk]))
            n += 1
        counts[table] = n

    print("\nRows with future-dated timestamps (to be moved -5h):" if not apply
          else "\nRows fixed (-5h):")
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