"""
Characterization tests for znuny_client.py's current behavior: pure parsing
helpers, and the caching/search logic built on top of ZnunyClient's
class-level shared state.

These pin down CURRENT behavior before Phase 8 of the MVC cleanup moves that
shared state from class-level to instance-level with an injectable HTTP
client. `_ticket_search`/`_ticket_get` are monkeypatched (rather than faking
httpx.Response objects) since they're the seam between the cache/search logic
under test and the actual REST calls.
"""

import time
from datetime import datetime

import pytest

from models.znuny import ZnunyArticle
from znuny_client import (
    ZnunyClient,
    parse_site_visit_article,
    _title_has_ticket_id,
    _parse_dt,
    _parse_from_name,
    MALDIVES_TZ,
)


def _article(subject="", body="", created_at=None, article_number=1):
    return ZnunyArticle(
        article_number=article_number, sender="agent", via="Internal",
        subject=subject, created_at=created_at, created_at_str="", body=body,
    )


class TestParseDt:
    def test_converts_utc_to_maldives_offset(self):
        # Znuny's system timezone is UTC; the client must convert to +05:00.
        result = _parse_dt("2026-08-15 10:00:00")
        assert result == datetime(2026, 8, 15, 15, 0, 0, tzinfo=MALDIVES_TZ)

    def test_empty_string_returns_none(self):
        assert _parse_dt("") is None

    def test_unparseable_value_returns_none(self):
        assert _parse_dt("not-a-date") is None


class TestTitleHasTicketId:
    def test_exact_token_match(self):
        assert _title_has_ticket_id("Fault report 165 opened", "165") is True

    def test_does_not_match_as_substring_of_longer_id(self):
        assert _title_has_ticket_id("Ticket 1650639 update", "165") is False

    def test_empty_inputs_return_false(self):
        assert _title_has_ticket_id("", "165") is False
        assert _title_has_ticket_id("Ticket 165", "") is False


class TestParseFromName:
    def test_quoted_name_with_email(self):
        assert _parse_from_name('"Mohamed Risal" <mohamed.risal@hdc.com.mv>') == "Mohamed Risal"

    def test_unquoted_name_with_email(self):
        assert _parse_from_name("Aminath Shazra <a@b.mv>") == "Aminath Shazra"

    def test_bare_email_falls_back_to_full_string(self):
        assert _parse_from_name("someone@example.com") == "someone@example.com"

    def test_empty_string(self):
        assert _parse_from_name("") == ""


class TestParseSiteVisitArticle:
    def test_non_site_visit_subject_returns_none(self):
        article = _article(subject="Regular update", body="Site Type: Fault")
        assert parse_site_visit_article(article, "ZNY1") is None

    def test_parses_full_body(self):
        body = (
            "Site Type:  Fault ( no BB)\n"
            "Service Provider: ooredoo\n"
            "Time: 1130\n"
            "Assigned to: @maah\n"
        )
        article = _article(subject="OAN Site Visit Arranged", body=body,
                            created_at=datetime(2026, 8, 15, 11, 30, tzinfo=MALDIVES_TZ))

        visit = parse_site_visit_article(article, "ZNY1")

        assert visit is not None
        assert visit.znuny_ticket_id == "ZNY1"
        assert visit.site_type == "Fault ( no BB)"
        assert visit.service_provider == "ooredoo"
        assert visit.scheduled_time == "11:30"
        assert visit.assigned_to == "maah"
        assert visit.visit_date == "2026-08-15"

    def test_time_now_uses_article_created_at(self):
        body = "Site Type: Fault\nTime: now\nAssigned to: @maah\n"
        article = _article(subject="OAN Site Visit Arranged", body=body,
                            created_at=datetime(2026, 8, 15, 14, 5, tzinfo=MALDIVES_TZ))

        visit = parse_site_visit_article(article, "ZNY1")
        assert visit.scheduled_time == "14:05"

    def test_multi_staff_assigned_to_with_numbered_markers(self):
        body = "Site Type: Fault\nAssigned to: [1] @raidh [2] @ayan\n"
        article = _article(subject="OAN Site Visit Arranged", body=body)

        visit = parse_site_visit_article(article, "ZNY1")
        assert visit.assigned_to == "raidh, ayan"

    def test_service_provider_inferred_from_subject_when_absent(self):
        body = "Site Type: Fault\n"
        article = _article(subject="Ooredoo OAN Site Visit Arranged", body=body)

        visit = parse_site_visit_article(article, "ZNY1")
        assert visit.service_provider == "Ooredoo"


class TestGetOpenTickets:
    def test_caches_result_and_skips_second_search(self, monkeypatch):
        client = ZnunyClient()
        search_calls = []

        def fake_search(**criteria):
            search_calls.append(criteria)
            return ["1"]

        def fake_get(ticket_ids, with_articles=False):
            return [{"TicketID": "1", "TicketNumber": "T1", "Title": "Ticket", "State": "open"}]

        monkeypatch.setattr(client, "_ticket_search", fake_search)
        monkeypatch.setattr(client, "_ticket_get", fake_get)

        first = client.get_open_tickets()
        second = client.get_open_tickets()

        assert len(search_calls) == 1  # second call served from cache
        assert first == second
        assert first[0]["ticket_number"] == "T1"

    def test_force_refresh_bypasses_cache(self, monkeypatch):
        client = ZnunyClient()
        search_calls = []
        monkeypatch.setattr(client, "_ticket_search", lambda **k: search_calls.append(1) or ["1"])
        monkeypatch.setattr(client, "_ticket_get", lambda ids, with_articles=False: [
            {"TicketID": "1", "TicketNumber": "T1", "Title": "Ticket", "State": "open"}
        ])

        client.get_open_tickets()
        client.get_open_tickets(force_refresh=True)

        assert len(search_calls) == 2

    def test_expired_cache_triggers_new_search(self, monkeypatch):
        client = ZnunyClient()
        search_calls = []
        monkeypatch.setattr(client, "_ticket_search", lambda **k: search_calls.append(1) or ["1"])
        monkeypatch.setattr(client, "_ticket_get", lambda ids, with_articles=False: [
            {"TicketID": "1", "TicketNumber": "T1", "Title": "Ticket", "State": "open"}
        ])

        client.get_open_tickets()
        # Simulate TTL expiry by backdating the cache timestamp.
        ZnunyClient._shared_cache_timestamp = time.time() - 361
        client.get_open_tickets()

        assert len(search_calls) == 2


class TestCheckTicketSync:
    def test_found_in_open_cache(self, monkeypatch):
        client = ZnunyClient()
        monkeypatch.setattr(client, "_ticket_search", lambda **k: ["1"])
        monkeypatch.setattr(client, "_ticket_get", lambda ids, with_articles=False: [
            {"TicketID": "1", "TicketNumber": "T1", "Title": "Fault OOR-2001287", "State": "open"}
        ])

        found, number = client.check_ticket_sync("OOR-2001287")
        assert found is True
        assert number == "T1"

    def test_not_found_returns_false_none(self, monkeypatch):
        client = ZnunyClient()
        monkeypatch.setattr(client, "_ticket_search", lambda **k: [])
        monkeypatch.setattr(client, "_ticket_get", lambda ids, with_articles=False: [])

        found, number = client.check_ticket_sync("OOR-9999999")
        assert found is False
        assert number is None

    def test_search_by_title_prefers_open_cache_over_network_search(self, monkeypatch):
        client = ZnunyClient()
        search_calls = []
        monkeypatch.setattr(client, "_ticket_search", lambda **k: search_calls.append(k) or ["1"])
        monkeypatch.setattr(client, "_ticket_get", lambda ids, with_articles=False: [
            {"TicketID": "1", "TicketNumber": "T1", "Title": "Fault OOR-2001287", "State": "open"}
        ])

        client.get_open_tickets()  # warm the cache
        search_calls.clear()
        client.search_by_title("OOR-2001287")

        # Served entirely from the open-ticket cache -- no second TicketSearch call.
        assert search_calls == []


class TestHarvestUserNames:
    def test_harvests_agent_article_sender_name(self):
        client = ZnunyClient()
        ticket = {
            "Owner": "someagent",
            "Article": [
                {"SenderType": "agent", "CreateBy": 42, "From": '"Jane Agent" <jane@example.com>'},
                {"SenderType": "customer", "CreateBy": 99, "From": "customer@example.com"},
            ],
        }

        client._harvest_user_names(ticket)

        assert ZnunyClient._user_names["42"] == "Jane Agent"
        assert "99" not in ZnunyClient._user_names

    def test_bot_owner_login_skips_harvesting(self):
        client = ZnunyClient()
        ticket = {
            "Owner": "writerbot",
            "Article": [
                {"SenderType": "agent", "CreateBy": 42, "From": '"Someone" <s@example.com>'},
            ],
        }

        client._harvest_user_names(ticket)

        assert ZnunyClient._user_names == {}

    def test_name_for_falls_back_when_unseen(self):
        client = ZnunyClient()
        assert client._name_for(999, fallback="unknown") == "unknown"
