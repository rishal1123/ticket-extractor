"""
Pytest configuration and fixtures.
"""

import os
import sys
import pytest
import tempfile

# Add parent directory to path for imports
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from database import Database
from config import Config
from models.ticket import Ticket
from datetime import datetime, timezone, timedelta


# Maldives timezone
MVT = timezone(timedelta(hours=5))


@pytest.fixture
def temp_db():
    """Create a temporary database for testing."""
    with tempfile.NamedTemporaryFile(suffix='.db', delete=False) as f:
        db_path = f.name

    db = Database(db_path)
    yield db

    # Cleanup
    try:
        os.unlink(db_path)
    except:
        pass


@pytest.fixture(autouse=True)
def isolated_db_path(monkeypatch):
    """
    Point Config.DATABASE_PATH at a fresh temp file for every test.

    `Database.__init__` defaults to `Config.DATABASE_PATH` whenever it's
    constructed with no argument, and most services/controllers construct
    their own `Database()` this way rather than receiving one via DI. Patching
    the class attribute (not just the get_db() singleton) is what actually
    keeps every code path a test can reach off the real production DB.
    Autouse so a future test that forgets to ask for it is still protected.
    """
    with tempfile.NamedTemporaryFile(suffix='.db', delete=False) as f:
        db_path = f.name

    monkeypatch.setattr(Config, "DATABASE_PATH", db_path)
    Database(db_path)  # create schema up front

    yield db_path

    try:
        os.unlink(db_path)
    except OSError:
        pass


@pytest.fixture
def sample_ticket():
    """Create a sample ticket for testing."""
    return Ticket(
        portal="dhiraagu",
        ticket_id="TEST123",
        address="Test Address, Floor 1",
        account="BB12345",
        customer_name="Test Customer",
        ticket_type="New Service",
        portal_created_at=datetime.now(MVT),
        service_type="Broadband",
        status="Open",
        kpi="24 Hours",
        notes="Test notes",
        created_at=None,
        updated_at=None,
        completed_at=None,
        id=None,
        in_znuny=False,
        znuny_ticket_id=None,
        znuny_created_at=None,
        znuny_created_by=None,
        znuny_address=None,
        znuny_url=None,
        portal_url="https://example.com/ticket/TEST123"
    )


@pytest.fixture
def sample_tickets(sample_ticket):
    """Create multiple sample tickets."""
    tickets = []
    portals = ["dhiraagu", "ooredoo", "rol", "medianet"]

    for i, portal in enumerate(portals):
        ticket = Ticket(
            portal=portal,
            ticket_id=f"TICKET{i+1}",
            address=f"Address {i+1}",
            account=f"ACC{i+1}",
            customer_name=f"Customer {i+1}",
            ticket_type="New Service" if i % 2 == 0 else "Installation",
            portal_created_at=datetime.now(MVT),
            service_type="Broadband",
            status="Open" if i % 2 == 0 else "Pending",
            kpi="24 Hours",
            notes=f"Notes for ticket {i+1}",
            created_at=None,
            updated_at=None,
            completed_at=None,
            id=None,
            in_znuny=i % 2 == 0,
            znuny_ticket_id=f"ZNY{i+1}" if i % 2 == 0 else None,
            znuny_created_at=datetime.now(MVT) if i % 2 == 0 else None,
            znuny_created_by="Test Staff" if i % 2 == 0 else None,
            znuny_address=None,
            znuny_url=None,
            portal_url=f"https://example.com/ticket/TICKET{i+1}"
        )
        tickets.append(ticket)

    return tickets
