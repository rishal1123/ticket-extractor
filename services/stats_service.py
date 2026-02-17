"""
Stats Service - Business logic for statistics and analytics.
"""

from typing import Dict, List, Optional
from datetime import datetime, timedelta
from database import Database
from utils.logger import get_logger

logger = get_logger("stats_service")


class StatsService:
    """Service class for statistics and analytics."""

    def __init__(self, db: Database = None):
        """Initialize service with optional database instance."""
        self.db = db or Database()

    def get_dashboard_stats(self) -> Dict:
        """
        Get statistics for the main dashboard.

        Returns:
            Dict with total, completed, by_portal, not_in_znuny, by_status, by_type
        """
        return self.db.get_stats()

    def get_portal_stats(self) -> Dict[str, int]:
        """
        Get ticket counts per portal.

        Returns:
            Dict keyed by portal name with ticket counts
        """
        stats = self.db.get_stats()
        return stats.get("by_portal", {})

    def get_staff_stats(self, date_from: str = None, date_to: str = None) -> Dict:
        """
        Get basic staff statistics.

        Args:
            date_from: Optional start date (YYYY-MM-DD)
            date_to: Optional end date (YYYY-MM-DD)

        Returns:
            Dict with staff list and totals
        """
        return self.db.get_staff_stats(date_from=date_from, date_to=date_to)

    def get_staff_stats_detailed(self, date_from: str = None, date_to: str = None,
                                    exclude_negative: bool = True) -> Dict:
        """
        Get detailed staff statistics including on-time metrics.

        Args:
            date_from: Optional start date (YYYY-MM-DD)
            date_to: Optional end date (YYYY-MM-DD)
            exclude_negative: If True (default), exclude tickets with negative time differences

        Returns:
            Dict with staff list and totals
        """
        return self.db.get_staff_detailed_stats(
            date_from=date_from, date_to=date_to, exclude_negative=exclude_negative
        )

    def get_staff_tickets(self, staff_name: str, date_from: str = None,
                          date_to: str = None, limit: int = 50, offset: int = 0) -> Dict:
        """
        Get tickets created by a specific staff member.

        Returns:
            Dict with total count and list of tickets
        """
        return self.db.get_staff_tickets(
            staff_name, date_from=date_from, date_to=date_to,
            limit=limit, offset=offset
        )

    def get_staff_performance(self, staff_name: str, days: int = 14) -> List[Dict]:
        """
        Get daily performance trend for a staff member.

        Args:
            staff_name: Name of the staff member
            days: Number of days to look back

        Returns:
            List of daily performance data
        """
        return self.db.get_staff_performance_trend(staff_name, days=days)

    def get_staff_names(self) -> List[str]:
        """Get list of all staff names who have created tickets."""
        return self.db.get_all_staff_names()

    def get_delayed_tickets_analysis(self, min_delay_minutes: int = 5,
                                      date_from: str = None, date_to: str = None) -> List[Dict]:
        """
        Analyze delayed tickets by staff.

        Args:
            min_delay_minutes: Minimum delay threshold
            date_from: Optional start date
            date_to: Optional end date

        Returns:
            List of delay statistics by staff
        """
        return self.db.get_delayed_tickets_by_staff(
            min_delay_minutes=min_delay_minutes,
            date_from=date_from,
            date_to=date_to
        )

    def get_login_summary(self) -> Dict:
        """Get login statistics summary."""
        return self.db.get_login_summary()

    def get_login_stats(self, limit: int = 100) -> List[Dict]:
        """Get login event history."""
        return self.db.get_login_stats(limit=limit)

    def get_extraction_logs(self, limit: int = 100) -> List[Dict]:
        """Get extraction log history."""
        return self.db.get_extraction_logs(limit=limit)

    def calculate_time_to_create(self, created_at: datetime, znuny_created_at: datetime) -> Dict:
        """
        Calculate time difference: created_at (entered extractor) - znuny_created_at (Znuny creation).

        Returns:
            Dict with time_diff (formatted string), time_diff_minutes
        """
        if not created_at or not znuny_created_at:
            return {"time_diff": None, "time_diff_minutes": None}

        diff = znuny_created_at - created_at
        time_diff_minutes = int(diff.total_seconds() / 60)
        hours, minutes = divmod(abs(time_diff_minutes), 60)
        days, hours = divmod(hours, 24)

        if days > 0:
            time_diff = f"{days}d {hours}h {minutes}m"
        elif hours > 0:
            time_diff = f"{hours}h {minutes}m"
        else:
            time_diff = f"{minutes}m"

        return {
            "time_diff": time_diff,
            "time_diff_minutes": time_diff_minutes
        }

    def export_staff_csv(self, date_from: str = None, date_to: str = None) -> str:
        """
        Generate CSV content for staff report.

        Returns:
            CSV content as string
        """
        return self.db.export_staff_stats_csv(date_from=date_from, date_to=date_to)

    def export_tickets_csv(self, staff: str = None, date_from: str = None,
                           date_to: str = None) -> str:
        """
        Generate CSV content for tickets export.

        Returns:
            CSV content as string
        """
        # Get tickets using existing method
        if staff:
            result = self.db.get_staff_tickets(staff, date_from=date_from, date_to=date_to, limit=10000)
            tickets = result.get("tickets", [])
        else:
            tickets = self.db.get_all_tickets(include_completed=True)

        lines = ["Portal,Ticket ID,Customer,Address,Type,Status,Portal Created,"
                 "Znuny Created,Created By,Time to Create (min)"]

        for t in tickets:
            time_to_create = ""
            if t.created_at and t.znuny_created_at:
                diff = (t.znuny_created_at - t.created_at).total_seconds() / 60
                time_to_create = str(round(diff, 1))

            customer = (t.customer_name or "").replace(",", ";")
            address = (t.address or "").replace(",", ";")

            lines.append(
                f"{t.portal},{t.ticket_id},{customer},{address},{t.ticket_type or ''},"
                f"{t.status or ''},{t.portal_created_at or ''},{t.znuny_created_at or ''},"
                f"{t.znuny_created_by or ''},{time_to_create}"
            )

        return "\n".join(lines)
