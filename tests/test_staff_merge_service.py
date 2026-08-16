"""Tests for services/staff_merge_service.py -- the orchestration layer
(same-name validation, message-building, logging) that used to live directly
in controllers/admin.py's staff-merge-preview/staff-merge routes."""

from services.staff_merge_service import StaffMergeService


class TestPreview:
    def test_rejects_same_source_and_target(self, temp_db):
        result = StaffMergeService(temp_db).preview("Same Name", "Same Name")
        assert result == {"success": False, "message": "Source and target cannot be the same"}

    def test_returns_preview_dict_on_success(self, temp_db, sample_ticket, seed_ticket):
        seed_ticket(temp_db, sample_ticket, "1", "Source Staff", minutes_to_znuny_create=1)

        result = StaffMergeService(temp_db).preview("Source Staff", "Target Staff")

        assert result["success"] is True
        assert result["preview"]["source"] == "Source Staff"
        assert result["preview"]["affected"]["isp_tickets"] == 1


class TestMerge:
    def test_rejects_missing_source_or_target(self, temp_db):
        assert StaffMergeService(temp_db).merge("", "Target") == {
            "success": False, "message": "Both source and target names are required"
        }
        assert StaffMergeService(temp_db).merge("Source", "") == {
            "success": False, "message": "Both source and target names are required"
        }

    def test_rejects_same_source_and_target(self, temp_db):
        result = StaffMergeService(temp_db).merge("Same Name", "Same Name")
        assert result == {"success": False, "message": "Source and target cannot be the same"}

    def test_strips_whitespace_from_names(self, temp_db, sample_ticket, seed_ticket):
        seed_ticket(temp_db, sample_ticket, "1", "Old Name", minutes_to_znuny_create=1)

        result = StaffMergeService(temp_db).merge("  Old Name  ", "  New Name  ")

        assert result["success"] is True
        assert result["message"] == "Merged 'Old Name' into 'New Name'"

    def test_executes_merge_and_returns_result(self, temp_db, sample_ticket, seed_ticket):
        ticket_id = seed_ticket(temp_db, sample_ticket, "1", "Old Name", minutes_to_znuny_create=1)

        result = StaffMergeService(temp_db).merge("Old Name", "New Name")

        assert result["success"] is True
        assert result["result"]["updated"]["isp_tickets"] == 1
        ticket = temp_db.get_ticket_by_id(ticket_id)
        assert ticket.znuny_created_by == "New Name"
