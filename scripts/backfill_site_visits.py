"""One-time backfill for site visits missed by the creator-sweep ingestion path.

Site-visit extraction (parse_site_visit_article -> upsert_site_visit) historically
only ran in the OAN-open / ISP-linked sync paths. Tickets that entered the DB only
through the creator sweep (_ingest_swept_tickets) — closed tickets, non-OAN tickets,
or tickets never linked to an ISP portal ticket — had their articles stored but their
"OAN Site Visit Arranged" / "Preventative Maintenance - Site Visit" articles were
never turned into site_visits rows.

The forward fix is in services/znuny_service.py (_ingest_swept_tickets now extracts
site visits). This script recovers the historical rows already sitting in
znuny_articles by replaying parse_site_visit_article over them and running the
follow-up completion check, exactly as the live sync would.

Idempotent — upsert_site_visit dedups on (znuny_ticket_id, article_id) and skips
when a richer same-day record already exists. Safe to run more than once.

Run inside the container:
    docker exec ticket-extractor python scripts/backfill_site_visits.py
Or locally:
    python scripts/backfill_site_visits.py
"""
import os
import sys
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from database import Database
from znuny_client import ZnunyArticle, parse_site_visit_article
from services.znuny_service import ZnunyService


def _parse_dt(value):
    """Parse a stored timestamp string ('YYYY-MM-DD HH:MM:SS+05:00') to datetime."""
    if not value:
        return None
    if isinstance(value, datetime):
        return value
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        # Fallback for naive 'YYYY-MM-DD HH:MM:SS'
        try:
            return datetime.strptime(value[:19], "%Y-%m-%d %H:%M:%S")
        except ValueError:
            return None


def main():
    db = Database()
    svc = ZnunyService(db)  # network client is lazy-loaded; never touched here

    # All tickets that have at least one site-visit-style article.
    with db._get_connection() as conn:
        rows = conn.execute(
            """
            SELECT DISTINCT znuny_ticket_id
            FROM znuny_articles
            WHERE lower(subject) LIKE '%oan site visit arranged%'
               OR lower(subject) LIKE '%preventative maintenance - site visit%'
               OR lower(subject) LIKE '%preventative maintenance -%'
            ORDER BY znuny_ticket_id
            """
        ).fetchall()
        ticket_ids = [r[0] for r in rows]

    print(f"Tickets with site-visit articles: {len(ticket_ids)}")

    extracted = 0
    completed = 0
    tickets_touched = 0

    for znuny_ticket_id in ticket_ids:
        # All articles for this ticket (need the full set for follow-up completion).
        with db._get_connection() as conn:
            arows = conn.execute(
                """
                SELECT a.article_number, a.sender, a.via, a.subject,
                       a.created_at, a.created_at_str, a.created_by, a.body,
                       zt.isp_ticket_id, zt.znuny_url
                FROM znuny_articles a
                LEFT JOIN znuny_tickets zt ON zt.znuny_ticket_id = a.znuny_ticket_id
                WHERE a.znuny_ticket_id = ?
                ORDER BY a.article_number
                """,
                (znuny_ticket_id,),
            ).fetchall()

        if not arows:
            continue

        isp_ticket_id = arows[0]["isp_ticket_id"] if hasattr(arows[0], "keys") else arows[0][8]
        znuny_url = arows[0]["znuny_url"] if hasattr(arows[0], "keys") else arows[0][9]

        articles = [
            ZnunyArticle(
                article_number=r["article_number"],
                sender=r["sender"] or "",
                via=r["via"] or "",
                subject=r["subject"] or "",
                created_at=_parse_dt(r["created_at"]),
                created_at_str=r["created_at_str"] or "",
                created_by=r["created_by"] or "",
                body=r["body"] or "",
            )
            for r in arows
        ]

        touched = False
        for article in articles:
            site_visit = parse_site_visit_article(article, znuny_ticket_id)
            if not site_visit:
                continue
            inserted = db.upsert_site_visit(
                znuny_ticket_id=site_visit.znuny_ticket_id,
                article_id=site_visit.article_number,
                site_type=site_visit.site_type,
                service_provider=site_visit.service_provider,
                scheduled_time=site_visit.scheduled_time,
                assigned_to=site_visit.assigned_to,
                visit_date=site_visit.visit_date,
                article_created_at=site_visit.article_created_at,
                ticket_id=isp_ticket_id,
                znuny_url=znuny_url,
                address=site_visit.address,
                customer_name=site_visit.customer_name,
            )
            if inserted:
                extracted += 1
                touched = True

        # Replay follow-up completion for this ticket's pending visits.
        completed += svc._complete_site_visits_by_followup(znuny_ticket_id, articles)

        if touched:
            tickets_touched += 1

    print(f"Site visits extracted/updated: {extracted}")
    print(f"Tickets with new/updated visits: {tickets_touched}")
    print(f"Site visits completed by follow-up: {completed}")

    with db._get_connection() as conn:
        total = conn.execute("SELECT COUNT(DISTINCT znuny_ticket_id) FROM site_visits").fetchone()[0]
    print(f"Distinct tickets now in site_visits: {total}")
    print("Done.")


if __name__ == "__main__":
    main()
