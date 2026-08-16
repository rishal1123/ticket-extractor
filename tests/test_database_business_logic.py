"""
Characterization tests for the business-logic methods currently living on
Database that Phase 5 of the MVC cleanup extracts into services/:
get_staff_merge_preview, merge_staff_names.

These pin down CURRENT behavior so the extraction can be verified against an
oracle instead of eyeballing the diff. When a method moves to its new
services/ home, move (or re-target) its tests alongside it. (CSV export and
the staff performance trend have already moved -- see TestExportStaffCsv and
TestStaffPerformanceTrend in test_services.py; create_backup has moved to
services/backup_service.py -- see tests/test_backup_service.py.)
"""

from database import now_maldives


class TestStaffMergePreview:
    def test_preview_counts_isp_tickets(self, temp_db, sample_ticket, seed_ticket):
        seed_ticket(temp_db, sample_ticket, "1", "Source Staff", minutes_to_znuny_create=1)
        seed_ticket(temp_db, sample_ticket, "2", "Source Staff", minutes_to_znuny_create=1)

        preview = temp_db.get_staff_merge_preview("Source Staff", "Target Staff")

        assert preview["source"] == "Source Staff"
        assert preview["target"] == "Target Staff"
        assert preview["affected"]["isp_tickets"] == 2
        assert preview["total_affected"] == 2

    def test_preview_counts_articles(self, temp_db, sample_ticket, seed_ticket):
        ticket_id = seed_ticket(temp_db, sample_ticket, "1", "Article Staff", minutes_to_znuny_create=1)
        temp_db.upsert_znuny_article(
            ticket_id=ticket_id, znuny_ticket_id="ZNY1", article_number=1,
            sender="agent", via="email", subject="test", created_by="Article Staff",
        )

        preview = temp_db.get_staff_merge_preview("Article Staff", "Target Staff")
        assert preview["affected"]["articles"] == 1

    def test_preview_counts_multi_staff_site_visits(self, temp_db, sample_ticket, seed_ticket):
        ticket_id = seed_ticket(temp_db, sample_ticket, "1", "SV Staff", minutes_to_znuny_create=1)
        temp_db.upsert_site_visit(
            znuny_ticket_id="ZNY1", article_id=1, site_type="Installation",
            service_provider="Test ISP", scheduled_time="10:00", assigned_to="SV Staff, Other Staff",
            visit_date="2026-08-15", article_created_at=now_maldives(), ticket_id=ticket_id,
        )

        preview = temp_db.get_staff_merge_preview("SV Staff", "Target Staff")
        assert preview["affected"]["site_visits"] == 1

    def test_preview_zero_when_no_matches(self, temp_db):
        preview = temp_db.get_staff_merge_preview("Nobody", "Target Staff")
        assert preview["total_affected"] == 0


class TestMergeStaffNames:
    def test_merge_updates_isp_tickets(self, temp_db, sample_ticket, seed_ticket):
        ticket_id = seed_ticket(temp_db, sample_ticket, "1", "Old Name", minutes_to_znuny_create=1)

        result = temp_db.merge_staff_names("Old Name", "New Name")

        assert result["updated"]["isp_tickets"] == 1
        assert result["total_updated"] == 1
        ticket = temp_db.get_ticket_by_id(ticket_id)
        assert ticket.znuny_created_by == "New Name"

    def test_merge_updates_articles(self, temp_db, sample_ticket, seed_ticket):
        ticket_id = seed_ticket(temp_db, sample_ticket, "1", "Article Old", minutes_to_znuny_create=1)
        temp_db.upsert_znuny_article(
            ticket_id=ticket_id, znuny_ticket_id="ZNY1", article_number=1,
            sender="agent", via="email", subject="test", created_by="Article Old",
        )

        result = temp_db.merge_staff_names("Article Old", "Article New")

        assert result["updated"]["articles"] == 1
        articles = temp_db.get_znuny_articles(ticket_id=ticket_id)
        assert articles[0]["created_by"] == "Article New"

    def test_merge_multi_staff_site_visit_dedupes_target_already_present(self, temp_db, sample_ticket, seed_ticket):
        ticket_id = seed_ticket(temp_db, sample_ticket, "1", "SV Old", minutes_to_znuny_create=1)
        # Target is already in the comma list -- merge must not produce a duplicate.
        temp_db.upsert_site_visit(
            znuny_ticket_id="ZNY1", article_id=1, site_type="Installation",
            service_provider="Test ISP", scheduled_time="10:00", assigned_to="SV Old, SV New",
            visit_date="2026-08-15", article_created_at=now_maldives(), ticket_id=ticket_id,
        )

        result = temp_db.merge_staff_names("SV Old", "SV New")

        assert result["updated"]["site_visits"] == 1
        with temp_db._get_connection() as conn:
            row = conn.execute("SELECT assigned_to FROM site_visits WHERE znuny_ticket_id = 'ZNY1'").fetchone()
        assert row["assigned_to"] == "SV New"

    def test_merge_performance_daily_overlapping_date_keeps_target_row(self, temp_db):
        with temp_db._get_connection() as conn:
            conn.execute(
                "INSERT INTO staff_performance_daily (staff_name, date, tickets_created, calculated_at) "
                "VALUES (?, ?, ?, ?)",
                ("Perf Old", "2026-08-01", 3, now_maldives()),
            )
            conn.execute(
                "INSERT INTO staff_performance_daily (staff_name, date, tickets_created, calculated_at) "
                "VALUES (?, ?, ?, ?)",
                ("Perf New", "2026-08-01", 7, now_maldives()),
            )

        result = temp_db.merge_staff_names("Perf Old", "Perf New")

        assert result["updated"]["performance_daily"] == 1
        with temp_db._get_connection() as conn:
            rows = conn.execute(
                "SELECT staff_name, tickets_created FROM staff_performance_daily WHERE date = '2026-08-01'"
            ).fetchall()
        # Source row on the overlapping date is dropped; target's own row is untouched.
        assert [dict(r) for r in rows] == [{"staff_name": "Perf New", "tickets_created": 7}]

    def test_merge_performance_daily_unique_date_renamed_to_target(self, temp_db):
        with temp_db._get_connection() as conn:
            conn.execute(
                "INSERT INTO staff_performance_daily (staff_name, date, tickets_created, calculated_at) "
                "VALUES (?, ?, ?, ?)",
                ("Perf Old", "2026-08-02", 4, now_maldives()),
            )

        result = temp_db.merge_staff_names("Perf Old", "Perf New")

        assert result["updated"]["performance_daily"] == 1
        with temp_db._get_connection() as conn:
            row = conn.execute(
                "SELECT staff_name FROM staff_performance_daily WHERE date = '2026-08-02'"
            ).fetchone()
        assert row["staff_name"] == "Perf New"
