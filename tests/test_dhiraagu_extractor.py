"""
Regression tests for DhiraaguExtractor's Order # / Service # field mapping.

The portal's list view shows BOTH an Order # (the sequential order id, e.g.
"0129530729") and a Service # (the circuit-style code, e.g. "BB20052084"), and
their column positions have changed with portal upgrades. The mapping into the
Ticket must not move with them:

    Order #   -> Ticket.ticket_id   (this portal's ticket identity)
    Service # -> Ticket.account     (what the Znuny account search and the
                                     ticket formatter's account validation use)

These tests pin that mapping. They touch no browser: both methods under test
take plain dicts, so the scraping layer isn't involved.
"""

from config import PortalConfig
from extractors.dhiraagu import DhiraaguExtractor
from models.ticket import Ticket

ORDER_NUM = "0129530729"
SERVICE_NUM = "BB20052084"

ORDER_ROW = {
    'order_num': ORDER_NUM,
    'service_num': SERVICE_NUM,
    'address': 'H16-14-13',
    'service_type': 'Broadband',
    'order_type': 'New Service',
    'status': 'Pending',
    'portal_created_at': None,
    'kpi': '30 Hr 40 Min',
    'customer_name': None,
    'detail_href': 'https://afas.dhiraagu.com.mv/orders/hdc/12856?activeRelationManager=notes',
}


def _extractor(temp_db) -> DhiraaguExtractor:
    """A DhiraaguExtractor with no browser — the methods under test don't need one."""
    config = PortalConfig(
        name="dhiraagu",
        url="https://afas.dhiraagu.com.mv/login",
        username="user",
        password="pass",
    )
    return DhiraaguExtractor(config, temp_db, headless=True)


class TestDhiraaguFieldMapping:
    """Order # -> ticket_id, Service # -> account."""

    def test_build_ticket_from_order_row(self, temp_db):
        ticket = _extractor(temp_db)._build_ticket_from_order(ORDER_ROW)

        assert ticket is not None
        assert ticket.ticket_id == ORDER_NUM
        assert ticket.account == SERVICE_NUM
        assert ticket.portal == "dhiraagu"
        assert ticket.portal_url == ORDER_ROW['detail_href']

    def test_build_ticket_from_detail_page(self, temp_db, monkeypatch):
        extractor = _extractor(temp_db)

        class _FakePage:
            url = ORDER_ROW['detail_href']

        class _FakeBrowser:
            page = _FakePage()

        extractor.browser = _FakeBrowser()
        monkeypatch.setattr(
            extractor, "_extract_detail_page_data",
            lambda: {'customer_name': 'Abdullah Rasheed', 'building': 'H16',
                     'floor': '14', 'apartment': '13'},
        )

        ticket = extractor._extract_from_detail_page(ORDER_ROW)

        assert ticket is not None
        assert ticket.ticket_id == ORDER_NUM
        assert ticket.account == SERVICE_NUM
        assert ticket.customer_name == 'Abdullah Rasheed'


class TestDhiraaguDetailUrl:
    """detail_url() must build from the order number, i.e. ticket_id."""

    def test_prefers_captured_portal_url(self, temp_db):
        ticket = Ticket(portal="dhiraagu", ticket_id=ORDER_NUM, account=SERVICE_NUM,
                        portal_url=ORDER_ROW['detail_href'])

        assert _extractor(temp_db).detail_url(ticket) == ORDER_ROW['detail_href']

    def test_falls_back_to_order_number_not_service_number(self, temp_db):
        ticket = Ticket(portal="dhiraagu", ticket_id=ORDER_NUM, account=SERVICE_NUM,
                        portal_url=None)

        url = _extractor(temp_db).detail_url(ticket)

        assert url is not None
        assert ORDER_NUM in url
        assert SERVICE_NUM not in url

    def test_returns_none_without_any_id(self, temp_db):
        ticket = Ticket(portal="dhiraagu", ticket_id=None, account=SERVICE_NUM,
                        portal_url=None)

        assert _extractor(temp_db).detail_url(ticket) is None
