"""
Unit tests for API endpoints.
"""

import pytest
import sys
import os

# Add parent directory to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


class TestHealthEndpoint:
    """Tests for health check endpoint."""

    def test_health_returns_200(self, client):
        """Test health endpoint returns 200."""
        response = client.get("/api/health")
        assert response.status_code == 200

    def test_health_returns_status(self, client):
        """Test health endpoint returns status field."""
        response = client.get("/api/health")
        data = response.json()

        assert 'status' in data
        assert data['status'] in ['healthy', 'degraded', 'unhealthy']

    def test_health_includes_checks(self, client):
        """Test health endpoint includes system checks."""
        response = client.get("/api/health")
        data = response.json()

        assert 'checks' in data
        assert 'database' in data['checks']


class TestStatsEndpoint:
    """Tests for stats endpoint."""

    def test_stats_returns_200(self, client):
        """Test stats endpoint returns 200."""
        response = client.get("/api/stats")
        assert response.status_code == 200

    def test_stats_returns_required_fields(self, client):
        """Test stats returns required fields."""
        response = client.get("/api/stats")
        data = response.json()

        required_fields = ['total', 'completed', 'by_portal', 'not_in_znuny']
        for field in required_fields:
            assert field in data


class TestTicketsEndpoint:
    """Tests for tickets endpoint."""

    def test_tickets_returns_200(self, client):
        """Test tickets endpoint returns 200."""
        response = client.get("/api/tickets")
        assert response.status_code == 200

    def test_tickets_returns_list(self, client):
        """Test tickets endpoint returns list structure."""
        response = client.get("/api/tickets")
        data = response.json()

        assert 'tickets' in data
        assert 'total' in data
        assert isinstance(data['tickets'], list)

    def test_tickets_pagination(self, client):
        """Test tickets endpoint pagination."""
        response = client.get("/api/tickets?page=1&page_size=10")
        assert response.status_code == 200

        data = response.json()
        assert len(data['tickets']) <= 10

    def test_tickets_filter_by_portal(self, client):
        """Test tickets filtering by portal."""
        response = client.get("/api/tickets?portal=dhiraagu")
        assert response.status_code == 200


class TestStaffEndpoint:
    """Tests for staff stats endpoint."""

    def test_staff_stats_returns_200(self, client):
        """Test staff stats returns 200."""
        response = client.get("/api/staff-stats")
        assert response.status_code == 200

    def test_staff_stats_detailed_returns_200(self, client):
        """Test detailed staff stats returns 200."""
        response = client.get("/api/staff-stats-detailed")
        assert response.status_code == 200

        data = response.json()
        assert 'stats' in data


class TestFieldVisitsEndpoint:
    """Tests for field visits endpoint."""

    def test_field_visits_returns_200(self, client):
        """Test field visits returns 200."""
        response = client.get("/api/field-visits")
        assert response.status_code == 200

    def test_field_visits_filter_by_status(self, client):
        """Test field visits filtering by status."""
        response = client.get("/api/field-visits?status=pending")
        assert response.status_code == 200


class TestAdminEndpoints:
    """Tests for admin endpoints."""

    def test_scheduler_status_returns_200(self, client):
        """Test scheduler status returns 200."""
        response = client.get("/api/admin/scheduler-status")
        assert response.status_code == 200

        data = response.json()
        assert 'running' in data

    def test_login_summary_returns_200(self, client):
        """Test login summary returns 200."""
        response = client.get("/api/admin/login-summary")
        assert response.status_code == 200

    def test_generate_staff_snapshots_returns_flattened_shape(self, client):
        """success_response's message must stay a string, with the data dict
        flattened into the response — not nested under 'message' as a dict."""
        response = client.post("/api/admin/generate-staff-snapshots")
        assert response.status_code == 200

        data = response.json()
        assert data["success"] is True
        assert isinstance(data["message"], str)
        assert "dates" in data
        assert "staff_rows" in data


class TestErrorHandling:
    """Tests for API error handling."""

    def test_invalid_ticket_id_returns_404(self, client):
        """Test invalid ticket ID returns 404."""
        response = client.get("/api/tickets/99999999")
        assert response.status_code == 404

    def test_invalid_endpoint_returns_404(self, client):
        """Test invalid endpoint returns 404."""
        response = client.get("/api/nonexistent")
        assert response.status_code == 404
