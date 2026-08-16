"""
Unit tests for service layer.
"""

import pytest
from unittest.mock import Mock, patch
from datetime import datetime, timedelta, timezone

from services.stats_service import StatsService
from services.config_service import ConfigService


MVT = timezone(timedelta(hours=5))


class TestStatsService:
    """Tests for StatsService."""

    def test_get_dashboard_stats(self, temp_db, sample_tickets):
        """Test dashboard stats calculation."""
        # Insert test data
        for ticket in sample_tickets:
            temp_db.upsert_ticket(ticket)

        service = StatsService(temp_db)
        stats = service.get_dashboard_stats()

        assert 'total' in stats
        assert 'by_portal' in stats
        assert 'not_in_znuny' in stats

    def test_get_staff_stats(self, temp_db, sample_tickets):
        """Test staff stats calculation."""
        for ticket in sample_tickets:
            temp_db.upsert_ticket(ticket)

        service = StatsService(temp_db)
        stats = service.get_staff_stats()

        assert 'staff' in stats
        # Staff with znuny entries should appear
        assert isinstance(stats['staff'], list)

    def test_get_staff_stats_with_date_filter(self, temp_db, sample_tickets):
        """Test staff stats with date filtering."""
        for ticket in sample_tickets:
            temp_db.upsert_ticket(ticket)

        service = StatsService(temp_db)

        # Filter for today
        today = datetime.now(MVT).strftime('%Y-%m-%d')
        stats = service.get_staff_stats(date_from=today, date_to=today)

        assert 'staff' in stats


class TestConfigService:
    """Tests for ConfigService."""

    def test_get_config_masks_passwords(self):
        """Test that passwords are masked in config output."""
        service = ConfigService()
        config = service.get_config(mask_passwords=True)

        # Check that password fields are masked
        for key, value in config.items():
            if 'PASSWORD' in key:
                assert value == '********' or value == ''

    def test_get_config_shows_passwords(self):
        """Test that passwords are shown when requested."""
        service = ConfigService()
        config = service.get_config(mask_passwords=False)

        # Config should be returned (may be empty if no .env)
        assert isinstance(config, dict)

    def test_get_all_keys(self):
        """Test getting all config keys."""
        service = ConfigService()
        keys = service.get_all_keys()

        assert isinstance(keys, list)
        # Should include portal configs
        expected_keys = ['DHIRAAGU_URL', 'OOREDOO_URL', 'ZNUNY_URL']
        for key in expected_keys:
            assert key in keys


class TestStatsServiceEdgeCases:
    """Edge case tests for StatsService."""

    def test_empty_database_stats(self, temp_db):
        """Test stats on empty database."""
        service = StatsService(temp_db)
        stats = service.get_dashboard_stats()

        assert stats['total'] == 0
        assert stats['completed'] == 0

    def test_staff_stats_no_znuny_data(self, temp_db, sample_ticket):
        """Test staff stats when no Znuny data exists."""
        sample_ticket.in_znuny = False
        sample_ticket.znuny_created_by = None
        temp_db.upsert_ticket(sample_ticket)

        service = StatsService(temp_db)
        stats = service.get_staff_stats()

        # Should return empty staff list or handle gracefully
        assert 'staff' in stats
