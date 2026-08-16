"""
Characterization tests for controllers/field_visits.py's two endpoints that
currently bypass the Database abstraction with hand-written SQL against
db._get_connection() directly: GET /assigned-staff and PUT /{visit_id}.

These pin down CURRENT behavior before Phase 4 of the MVC cleanup replaces
the raw SQL (including an f-string-built UPDATE) with proper Database
methods.
"""

from database import Database, now_maldives
from config import Config


def _seed_site_visit(isolated_db_path, **overrides):
    db = Database(isolated_db_path)
    fields = dict(
        znuny_ticket_id="ZNY1", article_id=1, site_type="Installation",
        service_provider="Test ISP", scheduled_time="10:00", assigned_to="Staff A",
        visit_date="2026-08-15", article_created_at=now_maldives(),
    )
    fields.update(overrides)
    db.upsert_site_visit(**fields)
    with db._get_connection() as conn:
        row = conn.execute("SELECT id FROM site_visits WHERE znuny_ticket_id = ?", (fields["znuny_ticket_id"],)).fetchone()
    return row["id"]


class TestGetAssignedStaff:
    def test_splits_multi_staff_assigned_to(self, client, isolated_db_path):
        _seed_site_visit(isolated_db_path, znuny_ticket_id="ZNY1", assigned_to="Staff A, Staff B")

        response = client.get("/api/field-visits/assigned-staff")

        assert response.status_code == 200
        assert response.json()["staff"] == ["Staff A", "Staff B"]

    def test_dedupes_and_sorts_across_rows(self, client, isolated_db_path):
        _seed_site_visit(isolated_db_path, znuny_ticket_id="ZNY1", article_id=1, assigned_to="Staff B")
        _seed_site_visit(isolated_db_path, znuny_ticket_id="ZNY2", article_id=2, assigned_to="Staff A, Staff B")

        response = client.get("/api/field-visits/assigned-staff")

        assert response.json()["staff"] == ["Staff A", "Staff B"]

    def test_empty_db_returns_empty_list(self, client):
        response = client.get("/api/field-visits/assigned-staff")
        assert response.status_code == 200
        assert response.json()["staff"] == []


class TestUpdateSiteVisit:
    def test_update_single_field(self, client, isolated_db_path):
        visit_id = _seed_site_visit(isolated_db_path)

        response = client.put(f"/api/field-visits/{visit_id}", json={"status": "completed"})

        assert response.status_code == 200
        assert response.json() == {"success": True, "message": "Site visit updated"}
        db = Database(isolated_db_path)
        with db._get_connection() as conn:
            row = conn.execute("SELECT status, scheduled_time FROM site_visits WHERE id = ?", (visit_id,)).fetchone()
        assert row["status"] == "completed"
        assert row["scheduled_time"] == "10:00"  # untouched field preserved

    def test_update_no_fields_returns_success_false(self, client, isolated_db_path):
        visit_id = _seed_site_visit(isolated_db_path)

        response = client.put(f"/api/field-visits/{visit_id}", json={})

        assert response.status_code == 200
        assert response.json() == {"success": False, "message": "No fields to update"}

    def test_update_nonexistent_visit_returns_not_found_message(self, client):
        response = client.put("/api/field-visits/999999", json={"status": "completed"})

        assert response.status_code == 200
        assert response.json() == {"success": False, "message": "Site visit not found"}

    def test_scheduled_time_change_recalculates_duration_when_completed(self, client, isolated_db_path):
        db = Database(isolated_db_path)
        visit_id = _seed_site_visit(
            isolated_db_path, scheduled_time="10:00", visit_date="2026-08-15",
        )
        with db._get_connection() as conn:
            conn.execute(
                "UPDATE site_visits SET ticket_completed_at = ? WHERE id = ?",
                ("2026-08-15 11:30:00", visit_id),
            )

        response = client.put(f"/api/field-visits/{visit_id}", json={"scheduled_time": "10:00"})

        assert response.status_code == 200
        with db._get_connection() as conn:
            row = conn.execute("SELECT time_taken_minutes FROM site_visits WHERE id = ?", (visit_id,)).fetchone()
        assert row["time_taken_minutes"] == 90

    def test_scheduled_time_change_leaves_duration_null_when_not_completed(self, client, isolated_db_path):
        visit_id = _seed_site_visit(isolated_db_path, scheduled_time="09:00")

        response = client.put(f"/api/field-visits/{visit_id}", json={"scheduled_time": "10:00"})

        assert response.status_code == 200
        db = Database(isolated_db_path)
        with db._get_connection() as conn:
            row = conn.execute("SELECT time_taken_minutes, scheduled_time FROM site_visits WHERE id = ?", (visit_id,)).fetchone()
        assert row["scheduled_time"] == "10:00"
        assert row["time_taken_minutes"] is None
