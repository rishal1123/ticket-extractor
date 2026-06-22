"""One-time production fix for the bogus "[1]" staff entry.

Site-visit "Assigned to" articles sometimes number the assignees as a list
("[1] @raidh [2] @ayan"). Older rows stored that literally, so "[1]" showed up
as its own staff member in the stats. This strips the "[n]" markers from
site_visits.assigned_to, removes bogus staff snapshot rows, and rebuilds the
affected days so the real assignees get credited.

Idempotent — safe to run more than once.

Run inside the container:
    docker exec ticket-extractor python scripts/fix_assigned_to_markers.py
Or locally:
    python scripts/fix_assigned_to_markers.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from database import Database


def main():
    db = Database()
    res = db.cleanup_site_visit_assignees()

    print(f"site_visits rows fixed: {res['rows_fixed']}")
    for d in res["details"]:
        print(f"  #{d['id']}: {d['old']!r} -> {d['new']!r}")
    print(f"bogus snapshot rows deleted: {res['snapshot_rows_deleted']}")
    print(f"days regenerated: {res['dates_regenerated'] or '(none)'}")
    print("Done.")


if __name__ == "__main__":
    main()
