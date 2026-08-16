"""
Unit tests exercising ZnunyClient's real HTTP layer via an injected
httpx.Client (httpx.MockTransport), rather than monkeypatching
_ticket_search/_ticket_get like tests/test_znuny_client.py does. This is
the concrete deliverable of Phase 8's testability refactor: ZnunyClient(
http_client=...) now accepts a client instead of always reaching for the
lazily-created class-level shared one, so tests never touch the network.
"""

import json

import httpx
import pytest

from znuny_client import ZnunyClient


def _client_with_handler(handler):
    transport = httpx.MockTransport(handler)
    http_client = httpx.Client(transport=transport)
    return ZnunyClient(http_client=http_client)


class TestGetOpenTicketsOverHttp:
    def test_fetches_and_summarizes_open_tickets(self):
        calls = []

        def handler(request: httpx.Request) -> httpx.Response:
            calls.append(request.url.path)
            if request.url.path.endswith("/TicketSearch"):
                return httpx.Response(200, json={"TicketID": ["101"]})
            if "/Ticket/101" in request.url.path:
                return httpx.Response(200, json={"Ticket": {
                    "TicketID": "101", "TicketNumber": "T101",
                    "Title": "Fault report", "State": "open",
                    "Queue": "OAN", "Owner": "agent1", "Priority": "3 normal",
                }})
            return httpx.Response(404)

        client = _client_with_handler(handler)
        tickets = client.get_open_tickets()

        assert len(tickets) == 1
        assert tickets[0]["ticket_number"] == "T101"
        assert tickets[0]["queue"] == "OAN"
        assert any(p.endswith("/TicketSearch") for p in calls)

    def test_empty_search_returns_empty_list(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"TicketID": []})

        client = _client_with_handler(handler)
        assert client.get_open_tickets() == []

    def test_error_response_returns_empty_list(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"Error": {"ErrorMessage": "boom"}})

        client = _client_with_handler(handler)
        assert client.get_open_tickets() == []


class TestGetTicketDetailsOverHttp:
    def test_fetches_and_parses_details_with_articles(self):
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("/TicketSearch"):
                body = json.loads(request.content)
                assert body.get("TicketNumber") == "T200"
                return httpx.Response(200, json={"TicketID": ["55"]})
            if "/Ticket/55" in request.url.path:
                return httpx.Response(200, json={"Ticket": {
                    "TicketID": "55", "TicketNumber": "T200",
                    "Title": "Installation", "State": "open", "Owner": "agent2",
                    "Queue": "OAN", "Priority": "3 normal", "CreateBy": 7,
                    "Created": "2026-08-15 09:00:00",
                    "Article": [
                        {
                            "ArticleNumber": 1, "SenderType": "agent",
                            "From": '"Jane Agent" <jane@example.com>',
                            "CommunicationChannelID": 3, "Subject": "Internal note",
                            "CreateTime": "2026-08-15 09:05:00", "Body": "Note body",
                            "CreateBy": 7,
                        },
                    ],
                }})
            return httpx.Response(404)

        client = _client_with_handler(handler)
        details = client.get_ticket_details("T200")

        assert details is not None
        assert details.ticket_number == "T200"
        assert details.owner == "agent2"
        assert details.created_by == "Jane Agent"  # harvested from the agent article
        assert len(details.articles) == 1
        assert details.articles[0].subject == "Internal note"
        assert details.articles[0].via == "Internal"

    def test_unresolvable_ticket_number_returns_none(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"TicketID": []})

        client = _client_with_handler(handler)
        assert client.get_ticket_details("T-DOES-NOT-EXIST") is None

    def test_second_call_within_ttl_is_served_from_cache(self):
        call_count = {"TicketSearch": 0, "TicketGet": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("/TicketSearch"):
                call_count["TicketSearch"] += 1
                return httpx.Response(200, json={"TicketID": ["9"]})
            call_count["TicketGet"] += 1
            return httpx.Response(200, json={"Ticket": {
                "TicketID": "9", "TicketNumber": "T900", "Title": "x",
                "State": "open", "Owner": "agent3", "Article": [],
            }})

        client = _client_with_handler(handler)
        first = client.get_ticket_details("T900")
        second = client.get_ticket_details("T900")

        assert first is second  # same cached object, not just equal
        assert call_count["TicketGet"] == 1


class TestSearchClosedByAccountOverHttp:
    def test_returns_matches_from_fulltext_search(self):
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("/TicketSearch"):
                body = json.loads(request.content)
                assert body.get("Fulltext") == "*ACC123*"
                return httpx.Response(200, json={"TicketID": ["1"]})
            return httpx.Response(200, json={"Ticket": {
                "TicketID": "1", "TicketNumber": "T1", "Title": "Relocation ACC123",
                "Created": "2026-08-10 12:00:00",
            }})

        client = _client_with_handler(handler)
        results = client.search_closed_by_account("ACC123")

        assert len(results) == 1
        assert results[0]["ticket_number"] == "T1"
        assert results[0]["created_at"] is not None

    def test_empty_account_returns_empty_without_request(self):
        def handler(request: httpx.Request) -> httpx.Response:
            pytest.fail("should not make a request for an empty account")

        client = _client_with_handler(handler)
        assert client.search_closed_by_account("") == []

    def test_filters_by_ticket_id_when_provided(self):
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("/TicketSearch"):
                return httpx.Response(200, json={"TicketID": ["1", "2"]})
            tid = request.url.path.rsplit("/", 1)[-1]
            titles = {"1": "Fault OOR-100", "2": "Fault OOR-200"}
            return httpx.Response(200, json={"Ticket": [
                {"TicketID": t, "TicketNumber": f"T{t}", "Title": titles[t]}
                for t in tid.split(",")
            ]})

        client = _client_with_handler(handler)
        results = client.search_closed_by_account("ACC1", ticket_id="OOR-100")

        assert len(results) == 1
        assert results[0]["ticket_number"] == "T1"
