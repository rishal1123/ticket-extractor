"""
Staff Merge Service - Orchestrates previewing and executing staff-name merges.

get_staff_merge_preview/merge_staff_names stay on Database: each is a set of
counts/updates across up to 6 tables keyed by staff name -- legitimate
repository work, not formatting/computation that can be cleanly split into a
"raw fetch + compute" pair the way CSV export or the performance trend were.
This service instead owns the workflow around them: the "can't merge a name
into itself" domain rule (previously duplicated in the controller for both
the preview and execute routes) and the success-message/logging that turns a
raw Database result into what the admin endpoints return.
"""

from database import Database
from utils.logger import get_logger

logger = get_logger("staff_merge_service")


class StaffMergeService:
    """Service for previewing and executing staff-name merges."""

    def __init__(self, db: Database = None):
        self.db = db or Database()

    def preview(self, source_name: str, target_name: str) -> dict:
        """Preview what a merge would affect. Returns {"success": bool, ...}."""
        if source_name == target_name:
            return {"success": False, "message": "Source and target cannot be the same"}

        preview = self.db.get_staff_merge_preview(source_name, target_name)
        return {"success": True, "preview": preview}

    def merge(self, source_name: str, target_name: str) -> dict:
        """Execute a merge across all tables. Returns {"success": bool, ...}."""
        source_name = (source_name or "").strip()
        target_name = (target_name or "").strip()

        if not source_name or not target_name:
            return {"success": False, "message": "Both source and target names are required"}
        if source_name == target_name:
            return {"success": False, "message": "Source and target cannot be the same"}

        result = self.db.merge_staff_names(source_name, target_name)
        logger.info(f"Staff merge completed: '{source_name}' -> '{target_name}', {result['total_updated']} records updated")

        return {
            "success": True,
            "message": f"Merged '{source_name}' into '{target_name}'",
            "result": result,
        }
