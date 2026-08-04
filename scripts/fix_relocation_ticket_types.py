"""One-time correction: verify each active ISP ticket's Relocation/New-Service
classification against NocBot's actual provisioned-service records, and
correct the stored ticket_type when the portal's own label disagrees with
what NocBot confirms.

Background: the ticket formatter (formatter/model/ticket_model.py
TicketModel.build) now auto-decides Relocation vs New Service PER TICKET,
live, every time it formats one -- that's self-correcting for every ticket
going forward with no backfill needed for formatting itself. This script is
about the STORED ticket_type column instead, since it's also used elsewhere
(dashboard, reports, filters) and the ISP portal's own label isn't always
reliable in either direction:

  - ticket_type currently says a portal's "new" label (Ooredoo:
    "Installation", Dhiraagu: "New Service", Medianet: "New Connections",
    ROL: "New Connection") but NocBot HAS an existing service for the
    account -> corrected to "Relocation".
  - ticket_type currently says "Relocation" but NocBot has NO service for
    the account -> corrected back to that portal's "new" label.

Only these two ticket_type values are ever touched -- Fault/Telephony/
Disconnect/Package Change/etc. are left alone. Scoped to ACTIVE tickets only
(completed_at IS NULL); historical/closed tickets aren't touched. Needs a
captured raw_dump to extract the account number the exact same way the live
formatter does (via that ISP's own account_id_for_validation()); tickets not
captured yet are skipped, not guessed at.

NocBot's default rate limit is 60 req/min per key (see NOCBOT_API_GUIDE.md);
this paces requests to stay under that and treats a rate-limit/network/
unexpected-status response as INCONCLUSIVE (skip that ticket, don't touch
it) -- never as "no service found", which would risk wrongly downgrading a
real relocation to New Service on a transient NocBot hiccup.

Idempotent and re-runnable: a ticket already matching NocBot's answer isn't
touched, so a second run only affects newly-drifted tickets. Takes a
timestamped DB backup before applying.

Auto-run: entrypoint.sh runs this with --apply on every container start.
A successful --apply run sets the MIGRATION_FLAG in app_settings, so normal
restarts after the first skip straight past the (slow, NocBot-paced) check
instead of re-verifying every candidate every time. NocBot not being
configured yet, or the run erroring out partway, leaves the flag unset so
the next restart retries automatically. Pass --force to bypass the flag and
re-verify everything regardless (e.g. after a bulk data import).

Run inside the container:
    docker exec ticket-extractor python scripts/fix_relocation_ticket_types.py          # dry-run (default)
    docker exec ticket-extractor python scripts/fix_relocation_ticket_types.py --apply   # apply the fix
    docker exec ticket-extractor python scripts/fix_relocation_ticket_types.py --apply --force  # re-verify everything
Or locally:
    python scripts/fix_relocation_ticket_types.py --apply
"""
import os
import sys
import time
import shutil
import sqlite3
import argparse
from datetime import datetime

import httpx

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB_PATH = os.getenv("DATABASE_PATH", os.path.join(ROOT, "data", "tickets.db"))
if not os.path.exists(DB_PATH) or os.path.getsize(DB_PATH) == 0:
    DB_PATH = os.path.join(ROOT, "tickets.db")

sys.path.insert(0, ROOT)
from formatter.model.formatters import detect_formatter  # noqa: E402
from services.nocbot_service import NocBotService  # noqa: E402

# Same portal -> detect_formatter() keyword mapping as utils/ticket_formatter.py,
# needed because raw_dump doesn't reliably contain the ISP's own name.
PORTAL_KEYWORD = {
    "dhiraagu": "dhiraagu",
    "ooredoo": "ooredoo",
    "medianet": "medianet",
    "rol": "raajje online",
}

# Portal's own "new" (non-relocation) ticket_type label -- matches real
# captured values, not a generic string, so a corrected ticket still matches
# that portal's normal vocabulary elsewhere in the DB/reports.
NEW_SERVICE_LABEL = {
    "dhiraagu": "New Service",
    "ooredoo": "Installation",
    "medianet": "New Connections",
    "rol": "New Connection",
}
RELOCATION_LABEL = "Relocation"

# Stay comfortably under NocBot's default 60 req/min per key.
REQUEST_DELAY_SECONDS = 1.1

# Guards the entrypoint.sh auto-run so restarts after a successful --apply
# skip straight past the (slow, NocBot-paced) check instead of re-verifying
# every candidate on every boot.
MIGRATION_FLAG = "migration_fix_relocation_ticket_types_v1"


def account_for_ticket(portal: str, raw_dump: str) -> str | None:
    if not raw_dump or not raw_dump.strip():
        return None
    keyword = PORTAL_KEYWORD.get(portal, portal)
    raw = f"{keyword}\n{raw_dump}"
    fmt = detect_formatter(raw)
    if fmt is None:
        return None
    return (fmt.account_id_for_validation(raw) or "").strip() or None


def service_exists(base_url: str, api_key: str, account: str) -> bool | None:
    """True/False if NocBot gave a confirmed answer, None if inconclusive
    (network error, rate limit, unexpected status) -- callers must SKIP the
    ticket on None, not treat it as "not found"."""
    try:
        resp = httpx.get(
            f"{base_url}/api/services/search",
            params={"port_description": account},
            headers={"X-API-Key": api_key}, timeout=10.0,
        )
    except Exception as e:
        print(f"    [warn] NocBot request failed for account '{account}': {e}")
        return None

    if resp.status_code == 200:
        services = (resp.json() or {}).get("services") or []
        return bool(services)
    if resp.status_code == 404:
        return False
    if resp.status_code == 429:
        print(f"    [warn] NocBot rate limit hit for account '{account}' -- skipping (re-run to pick it up)")
        return None
    print(f"    [warn] NocBot returned unexpected status {resp.status_code} for account '{account}' -- skipping")
    return None


def main():
    ap = argparse.ArgumentParser(
        description="Correct ticket_type (Relocation vs New Service) against NocBot's actual service records."
    )
    ap.add_argument("--apply", action="store_true", help="Apply the fix (default is a dry-run).")
    ap.add_argument("--force", action="store_true",
                     help="Re-verify everything even if MIGRATION_FLAG is already set.")
    args = ap.parse_args()
    apply = args.apply

    if not os.path.exists(DB_PATH):
        print(f"[ERROR] Database not found at {DB_PATH}")
        sys.exit(1)

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()

    cur.execute("SELECT value FROM app_settings WHERE key = ?", (MIGRATION_FLAG,))
    if cur.fetchone() and not args.force:
        print("Relocation ticket_type check already completed; skipping (pass --force to re-verify).")
        conn.close()
        return

    nocbot = NocBotService()
    if not nocbot.enabled:
        # Not configured is a normal, common state (e.g. NocBot never set up
        # for this deployment) -- exit clean/non-fatal so entrypoint.sh's
        # auto-run doesn't print a scary-looking failure on every restart.
        # The flag is deliberately left unset, so this retries automatically
        # once NocBot IS configured.
        print("NocBot isn't configured (Admin > Config > NocBot API) -- nothing to check yet.")
        conn.close()
        return

    print(f"Database   : {DB_PATH}")
    print(f"NocBot     : {nocbot.base_url}")
    print(f"Mode       : {'APPLY' if apply else 'DRY-RUN (no changes; pass --apply to write)'}")

    if apply:
        bak = f"{DB_PATH}.bak-relocationtype-{datetime.now().strftime('%Y%m%d%H%M%S')}"
        shutil.copy2(DB_PATH, bak)
        print(f"Backup     : {bak}")

    portals = tuple(NEW_SERVICE_LABEL.keys())
    new_labels = tuple(NEW_SERVICE_LABEL.values())
    placeholders_portal = ",".join("?" * len(portals))
    placeholders_new = ",".join("?" * len(new_labels))
    cur.execute(f"""
        SELECT id, portal, ticket_id, ticket_type, raw_dump
        FROM tickets
        WHERE completed_at IS NULL
          AND portal IN ({placeholders_portal})
          AND (ticket_type = ? OR ticket_type IN ({placeholders_new}))
        ORDER BY id
    """, (*portals, RELOCATION_LABEL, *new_labels))
    rows = cur.fetchall()
    print(f"Candidates : {len(rows)} active tickets currently labeled Relocation or a portal 'new' type\n")

    checked = 0
    corrected = 0
    skipped_no_dump = 0
    skipped_inconclusive = 0
    samples = []

    for i, row in enumerate(rows):
        portal = row["portal"]
        account = account_for_ticket(portal, row["raw_dump"])
        if not account:
            skipped_no_dump += 1
            continue

        if checked > 0:
            time.sleep(REQUEST_DELAY_SECONDS)
        found = service_exists(nocbot.base_url, nocbot.api_key, account)
        checked += 1

        if found is None:
            skipped_inconclusive += 1
            continue

        current = (row["ticket_type"] or "").strip()
        is_currently_relocation = current.lower() == RELOCATION_LABEL.lower()
        should_be_relocation = found

        if is_currently_relocation == should_be_relocation:
            continue  # already correct

        new_type = RELOCATION_LABEL if should_be_relocation else NEW_SERVICE_LABEL[portal]
        corrected += 1
        if len(samples) < 20:
            samples.append(f"  {portal}/{row['ticket_id']} (id={row['id']}, account={account}): "
                            f"{current!r} -> {new_type!r}")
        if apply:
            cur.execute("UPDATE tickets SET ticket_type = ? WHERE id = ?", (new_type, row["id"]))

    print(f"Checked against NocBot : {checked}")
    print(f"Skipped (no raw_dump)  : {skipped_no_dump}")
    print(f"Skipped (inconclusive) : {skipped_inconclusive}")
    print(f"{'Corrected' if apply else 'Would correct'}              : {corrected}")
    if samples:
        print("\nSample changes:")
        for s in samples:
            print(s)

    if apply:
        if skipped_inconclusive == 0:
            cur.execute(
                "INSERT OR REPLACE INTO app_settings (key, value, description) VALUES (?, ?, ?)",
                (MIGRATION_FLAG, "1", "Verified every active Relocation/New-Service ticket_type against NocBot"),
            )
            print("\nAll candidates got a confirmed NocBot answer -- marking this check complete.")
        else:
            print(f"\n{skipped_inconclusive} ticket(s) got an inconclusive NocBot answer -- "
                  f"leaving this unmarked so the next run retries them.")
        conn.commit()
        print("Done.")
    else:
        print(f"\nDry-run only -- re-run with --apply to commit.")
    conn.close()


if __name__ == "__main__":
    main()
