"""
Characterization tests for the business-logic methods currently living on
Database that Phase 5 of the MVC cleanup extracts into services/:
export_staff_stats_csv, get_staff_performance_trend, get_staff_merge_preview,
merge_staff_names, create_backup.

These pin down CURRENT behavior so the extraction can be verified against an
oracle instead of eyeballing the diff. When a method moves to its new
services/ home, move (or re-target) its tests alongside it.
"""

import os
import sqlite3
import tempfile
from datetime import timedelta

from database import now_maldives


def _seed_ticket(db, sample_ticket, ticket_id_suffix, staff, minutes_to_znuny_create):
    """Insert a ticket and give it a znuny_created_by/znuny_created_at pair
    `minutes_to_znuny_create` minutes after its created_at, mirroring how a
    real extraction (upsert_ticket) + Znuny sync (update_znuny_status/
    update_znuny_details) populate a row."""
    sample_ticket.ticket_id = f"CHAR{ticket_id_suffix}"
    ticket_id, _, _ = db.upsert_ticket(sample_ticket)

    now = now_maldives()
    db.update_znuny_status(ticket_id=ticket_id, in_znuny=True, znuny_ticket_id=f"ZNY{ticket_id_suffix}")
    db.update_znuny_details(
        ticket_id=ticket_id,
        znuny_created_at=now + timedelta(minutes=minutes_to_znuny_create),
        znuny_created_by=staff,
        znuny_address="Test Address",
        znuny_url=f"https://znuny.example.com/{ticket_id_suffix}",
    )
    # Backdate created_at to `now` so the time-to-create diff is exactly
    # `minutes_to_znuny_create`, independent of how fast the test runs.
    with db._get_connection() as conn:
        conn.execute("UPDATE tickets SET created_at = ? WHERE id = ?", (now, ticket_id))
    return ticket_id


class TestExportStaffStatsCsv:
    def test_header_row(self, temp_db):
        csv_text = temp_db.export_staff_stats_csv()
        header = csv_text.split("\n")[0]
        assert header == (
            "Staff Name,Tickets Created,Within 5min,Within 10min,Over 10min,"
            "On Time %,Avg Minutes,Articles,Tickets Updated"
        )

    def test_empty_db_returns_header_only(self, temp_db):
        csv_text = temp_db.export_staff_stats_csv()
        assert csv_text.strip() == temp_db.export_staff_stats_csv().split("\n")[0]
        assert len(csv_text.split("\n")) == 1

    def test_row_reflects_seeded_staff(self, temp_db, sample_ticket):
        _seed_ticket(temp_db, sample_ticket, "1", "Staff CSV", minutes_to_znuny_create=2)
        csv_text = temp_db.export_staff_stats_csv()
        lines = csv_text.split("\n")
        assert len(lines) == 2
        assert lines[1].startswith("Staff CSV,1,1,0,0,100.0,2.0,")


class TestStaffPerformanceTrend:
    def test_on_time_ticket_within_default_threshold(self, temp_db, sample_ticket):
        _seed_ticket(temp_db, sample_ticket, "1", "Staff Trend", minutes_to_znuny_create=3)
        trend = temp_db.get_staff_performance_trend("Staff Trend", days=30)

        assert len(trend) == 1
        day = trend[0]
        assert day["tickets_created"] == 1
        assert day["within_5min"] == 1
        assert day["on_time_pct"] == 100.0
        assert day["avg_minutes"] == 3.0

    def test_late_ticket_excluded_from_within_good(self, temp_db, sample_ticket):
        _seed_ticket(temp_db, sample_ticket, "1", "Staff Late", minutes_to_znuny_create=12)
        trend = temp_db.get_staff_performance_trend("Staff Late", days=30)

        assert len(trend) == 1
        day = trend[0]
        assert day["tickets_created"] == 1
        assert day["within_5min"] == 0
        assert day["on_time_pct"] == 0.0

    def test_boundary_at_threshold_excluded_by_float_precision(self, temp_db, sample_ticket):
        """The query is written as an inclusive `<= t_good` boundary, but real
        timestamps carry microseconds, so julianday()'s float arithmetic on a
        "5 minutes exactly" gap comes out as ~5.0000004 minutes, not exactly
        5.0 -- pushing it just over the threshold in practice. Pin this actual
        (not the naively-inclusive-looking) behavior."""
        _seed_ticket(temp_db, sample_ticket, "1", "Staff Boundary", minutes_to_znuny_create=5)
        trend = temp_db.get_staff_performance_trend("Staff Boundary", days=30, thresholds={"good": 5})

        assert trend[0]["within_5min"] == 0

    def test_custom_thresholds_override_default(self, temp_db, sample_ticket):
        _seed_ticket(temp_db, sample_ticket, "1", "Staff Custom", minutes_to_znuny_create=8)
        # Default good=5 would exclude this ticket; a wider threshold includes it.
        trend = temp_db.get_staff_performance_trend("Staff Custom", days=30, thresholds={"good": 10})

        assert trend[0]["within_5min"] == 1

    def test_unknown_staff_returns_empty(self, temp_db):
        assert temp_db.get_staff_performance_trend("Nobody", days=30) == []


class TestStaffMergePreview:
    def test_preview_counts_isp_tickets(self, temp_db, sample_ticket):
        _seed_ticket(temp_db, sample_ticket, "1", "Source Staff", minutes_to_znuny_create=1)
        _seed_ticket(temp_db, sample_ticket, "2", "Source Staff", minutes_to_znuny_create=1)

        preview = temp_db.get_staff_merge_preview("Source Staff", "Target Staff")

        assert preview["source"] == "Source Staff"
        assert preview["target"] == "Target Staff"
        assert preview["affected"]["isp_tickets"] == 2
        assert preview["total_affected"] == 2

    def test_preview_counts_articles(self, temp_db, sample_ticket):
        ticket_id = _seed_ticket(temp_db, sample_ticket, "1", "Article Staff", minutes_to_znuny_create=1)
        temp_db.upsert_znuny_article(
            ticket_id=ticket_id, znuny_ticket_id="ZNY1", article_number=1,
            sender="agent", via="email", subject="test", created_by="Article Staff",
        )

        preview = temp_db.get_staff_merge_preview("Article Staff", "Target Staff")
        assert preview["affected"]["articles"] == 1

    def test_preview_counts_multi_staff_site_visits(self, temp_db, sample_ticket):
        ticket_id = _seed_ticket(temp_db, sample_ticket, "1", "SV Staff", minutes_to_znuny_create=1)
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
    def test_merge_updates_isp_tickets(self, temp_db, sample_ticket):
        ticket_id = _seed_ticket(temp_db, sample_ticket, "1", "Old Name", minutes_to_znuny_create=1)

        result = temp_db.merge_staff_names("Old Name", "New Name")

        assert result["updated"]["isp_tickets"] == 1
        assert result["total_updated"] == 1
        ticket = temp_db.get_ticket_by_id(ticket_id)
        assert ticket.znuny_created_by == "New Name"

    def test_merge_updates_articles(self, temp_db, sample_ticket):
        ticket_id = _seed_ticket(temp_db, sample_ticket, "1", "Article Old", minutes_to_znuny_create=1)
        temp_db.upsert_znuny_article(
            ticket_id=ticket_id, znuny_ticket_id="ZNY1", article_number=1,
            sender="agent", via="email", subject="test", created_by="Article Old",
        )

        result = temp_db.merge_staff_names("Article Old", "Article New")

        assert result["updated"]["articles"] == 1
        articles = temp_db.get_znuny_articles(ticket_id=ticket_id)
        assert articles[0]["created_by"] == "Article New"

    def test_merge_multi_staff_site_visit_dedupes_target_already_present(self, temp_db, sample_ticket):
        ticket_id = _seed_ticket(temp_db, sample_ticket, "1", "SV Old", minutes_to_znuny_create=1)
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


class TestCreateBackup:
    def test_backup_is_queryable_and_matches_source(self, temp_db, sample_ticket):
        temp_db.upsert_ticket(sample_ticket)

        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            dest_path = f.name
        try:
            temp_db.create_backup(dest_path)

            src_conn = sqlite3.connect(temp_db.db_path)
            dest_conn = sqlite3.connect(dest_path)
            try:
                src_count = src_conn.execute("SELECT COUNT(*) FROM tickets").fetchone()[0]
                dest_count = dest_conn.execute("SELECT COUNT(*) FROM tickets").fetchone()[0]
                assert dest_count == src_count == 1

                dest_tables = {
                    row[0] for row in dest_conn.execute(
                        "SELECT name FROM sqlite_master WHERE type='table'"
                    ).fetchall()
                }
                assert "tickets" in dest_tables
                assert "app_settings" in dest_tables
            finally:
                src_conn.close()
                dest_conn.close()
        finally:
            os.unlink(dest_path)
