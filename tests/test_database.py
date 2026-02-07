"""
Unit tests for database operations.
"""

import pytest
from datetime import datetime, timedelta

from database import Database, now_maldives


class TestDatabaseInit:
    """Tests for database initialization."""

    def test_database_creates_tables(self, temp_db):
        """Test that database creates all required tables."""
        with temp_db._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
            tables = {row['name'] for row in cursor.fetchall()}

        required_tables = {
            'tickets', 'znuny_articles', 'extraction_logs',
            'login_stats', 'site_visits', 'system_logs'
        }
        assert required_tables.issubset(tables)

    def test_database_creates_indexes(self, temp_db):
        """Test that database creates performance indexes."""
        with temp_db._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT name FROM sqlite_master WHERE type='index'"
            )
            indexes = {row['name'] for row in cursor.fetchall()}

        # Check for key indexes
        assert 'idx_tickets_portal' in indexes
        assert 'idx_tickets_status' in indexes


class TestTicketOperations:
    """Tests for ticket CRUD operations."""

    def test_upsert_new_ticket(self, temp_db, sample_ticket):
        """Test inserting a new ticket."""
        ticket_id, is_new, is_updated = temp_db.upsert_ticket(sample_ticket)

        assert ticket_id > 0
        assert is_new is True
        assert is_updated is False

    def test_upsert_existing_ticket(self, temp_db, sample_ticket):
        """Test updating an existing ticket."""
        # Insert first
        temp_db.upsert_ticket(sample_ticket)

        # Update
        sample_ticket.status = "Closed"
        ticket_id, is_new, is_updated = temp_db.upsert_ticket(sample_ticket)

        assert is_new is False
        assert is_updated is True

    def test_get_ticket_by_id(self, temp_db, sample_ticket):
        """Test retrieving a ticket by ID."""
        ticket_id, _, _ = temp_db.upsert_ticket(sample_ticket)

        retrieved = temp_db.get_ticket_by_id(ticket_id)

        assert retrieved is not None
        assert retrieved.ticket_id == sample_ticket.ticket_id
        assert retrieved.portal == sample_ticket.portal

    def test_get_tickets_with_filters(self, temp_db, sample_tickets):
        """Test retrieving tickets with filters."""
        # Insert all tickets and set Znuny status for some
        for i, ticket in enumerate(sample_tickets):
            ticket_id, _, _ = temp_db.upsert_ticket(ticket)
            # Set in_znuny for tickets 0 and 2 (matching sample_tickets fixture)
            if i % 2 == 0:
                temp_db.update_znuny_status(ticket_id, in_znuny=True, znuny_ticket_id=f"ZNY{i+1}")

        # Filter by portal
        tickets, total = temp_db.get_tickets_filtered(portal="dhiraagu", include_completed=True)
        assert total == 1

        # Filter by in_znuny
        tickets, total = temp_db.get_tickets_filtered(in_znuny=True, include_completed=True)
        assert total == 2  # Tickets 0 and 2

    def test_get_tickets_pagination(self, temp_db, sample_tickets):
        """Test ticket pagination."""
        for ticket in sample_tickets:
            temp_db.upsert_ticket(ticket)

        tickets, total = temp_db.get_tickets_filtered(limit=2, offset=0, include_completed=True)

        assert len(tickets) == 2
        assert total == 4


class TestStatsOperations:
    """Tests for statistics operations."""

    def test_get_stats_empty_db(self, temp_db):
        """Test stats on empty database."""
        stats = temp_db.get_stats()

        assert stats['total'] == 0
        assert stats['completed'] == 0
        assert stats['not_in_znuny'] == 0

    def test_get_stats_with_tickets(self, temp_db, sample_tickets):
        """Test stats with tickets."""
        for ticket in sample_tickets:
            temp_db.upsert_ticket(ticket)

        stats = temp_db.get_stats()

        assert stats['total'] == 4
        assert 'by_portal' in stats
        assert 'dhiraagu' in stats['by_portal']


class TestZnunyOperations:
    """Tests for Znuny-related operations."""

    def test_update_znuny_info(self, temp_db, sample_ticket):
        """Test updating Znuny information."""
        ticket_id, _, _ = temp_db.upsert_ticket(sample_ticket)

        # First set znuny status
        temp_db.update_znuny_status(
            ticket_id=ticket_id,
            in_znuny=True,
            znuny_ticket_id="ZNY12345"
        )

        # Then set details
        temp_db.update_znuny_details(
            ticket_id=ticket_id,
            znuny_created_at=now_maldives(),
            znuny_created_by="Test Staff",
            znuny_address="Znuny Address",
            znuny_url="https://znuny.example.com/ticket/12345"
        )

        ticket = temp_db.get_ticket_by_id(ticket_id)
        assert ticket.in_znuny is True
        assert ticket.znuny_ticket_id == "ZNY12345"
        assert ticket.znuny_created_by == "Test Staff"

    def test_get_znuny_articles(self, temp_db, sample_ticket):
        """Test storing and retrieving Znuny articles."""
        ticket_id, _, _ = temp_db.upsert_ticket(sample_ticket)

        # Add article
        temp_db.upsert_znuny_article(
            ticket_id=ticket_id,
            znuny_ticket_id="ZNY123",
            article_number=1,
            sender="Test Sender",
            via="Phone",
            subject="Test Subject",
            created_at=now_maldives(),
            created_at_str="2026-02-07 12:00:00",
            created_by="Test Staff",
            body="Test article body"
        )

        articles = temp_db.get_znuny_articles(ticket_id)
        assert len(articles) == 1
        assert articles[0]['subject'] == "Test Subject"


class TestExtractionLogs:
    """Tests for extraction log operations."""

    def test_log_extraction(self, temp_db):
        """Test logging an extraction."""
        temp_db.log_extraction(
            portal="dhiraagu",
            status="success",
            tickets_found=10,
            tickets_new=5,
            tickets_updated=5,
            error_message=None
        )

        logs = temp_db.get_extraction_logs(limit=1)
        assert len(logs) == 1
        assert logs[0]['portal'] == "dhiraagu"
        assert logs[0]['status'] == "success"

    def test_get_last_extraction(self, temp_db):
        """Test getting last extraction for a portal."""
        temp_db.log_extraction("dhiraagu", "success", 5, 2, 3)
        temp_db.log_extraction("ooredoo", "success", 3, 1, 2)

        last_per_portal = temp_db.get_last_extraction_per_portal()
        assert "dhiraagu" in last_per_portal
        assert last_per_portal["dhiraagu"]["status"] == "success"


class TestSystemLogs:
    """Tests for system logging operations."""

    def test_log_system_event(self, temp_db):
        """Test logging a system event."""
        temp_db.log_system(
            level="info",
            source="test",
            message="Test log message",
            details="Additional details"
        )

        with temp_db._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM system_logs ORDER BY id DESC LIMIT 1")
            log = cursor.fetchone()

        assert log['level'] == "info"
        assert log['message'] == "Test log message"
