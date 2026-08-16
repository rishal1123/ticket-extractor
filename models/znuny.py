"""Domain models for Znuny ticket data, shared by znuny_client.py, services/,
and one-off scripts (e.g. scripts/backfill_site_visits.py)."""

from dataclasses import dataclass, field
from datetime import datetime


@dataclass
class ZnunyArticle:
    """Represents an article/note in a Znuny ticket."""
    article_number: int
    sender: str  # Customer/caller for Phone, staff for Internal
    via: str  # Phone, Internal, Email, etc.
    subject: str
    created_at: datetime | None
    created_at_str: str
    created_by: str = ""  # Staff who created the article (from "by X" in detail)
    body: str = ""  # The actual note/article content


@dataclass
class ZnunyTicketDetails:
    """Details fetched from a Znuny ticket."""
    ticket_number: str
    created_at: datetime | None
    created_at_str: str
    created_by: str
    owner: str
    state: str
    queue: str = ""  # Queue assignment from sidebar
    priority: str = ""  # Ticket priority from sidebar
    address: str = ""  # Address from phone ticket or first article
    znuny_url: str = ""  # Direct URL to ticket in Znuny
    articles: list[ZnunyArticle] = field(default_factory=list)
    total_article_count: int = 0  # Total articles on page (before filtering)


@dataclass
class SiteVisit:
    """Represents a parsed OAN Site Visit from a Znuny article."""
    znuny_ticket_id: str
    article_number: int
    article_created_at: datetime | None
    site_type: str = ""
    service_provider: str = ""
    scheduled_time: str = ""  # HHMM or "now"
    assigned_to: str = ""
    visit_date: str = ""  # Date of the visit (from article date)
    address: str = ""
    customer_name: str = ""
