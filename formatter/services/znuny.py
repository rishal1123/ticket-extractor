"""Znuny (OTRS-fork) address verification via the Generic Interface REST API.

In this Znuny instance, a ticket's **CustomerID holds the address** (e.g.
``UD-05-03-08``). So "verify the address" means: does a ticket exist whose
CustomerID exactly equals the detected address? We answer that with the standard
``GenericTicketConnectorREST`` web service:

    POST {base}/Session       {UserLogin, Password}                    -> {SessionID}
    POST {base}/TicketSearch  {SessionID, CustomerID: <addr>}          -> {TicketID: [...]}
    POST {base}/TicketSearch  {SessionID, CustomerUserLogin: <acct>}   -> {TicketID: [...]}

``TicketSearch`` is an exact (case-insensitive) match per field, so a non-empty
result means:
  * by CustomerID  -> the address exists in Znuny (verify_address), and
  * by CustomerUserLogin -> the account already has a ticket (account_exists),
    which the formatter surfaces as a "possible relocation".

Configuration (env / .env) — disabled unless URL + web service + credentials are
set, so the app runs fine without it:

    ZNUNY_URL            Base, including /otrs  (e.g. https://host/otrs)
    ZNUNY_WEBSERVICE     Web service name (default GenericTicketConnectorREST)
    ZNUNY_USER           Agent UserLogin
    ZNUNY_PASSWORD       Agent password
    ZNUNY_SESSION_ROUTE  default /Session
    ZNUNY_SEARCH_ROUTE   default /TicketSearch
    ZNUNY_TIMEOUT        request timeout seconds (default 5)
    ZNUNY_VERIFY_SSL     "false" to skip TLS verification (self-signed cert)
    ZNUNY_CACHE_TTL      seconds to cache a result (default 60)

Pure stdlib (urllib) — no third-party HTTP dependency.
"""

from __future__ import annotations

import json
import logging
import os
import ssl
import threading
import time
import urllib.request
from typing import Optional

logger = logging.getLogger(__name__)


class ZnunyService:
    """Verifies a detected address against Znuny ticket CustomerIDs (cached, fail-soft)."""

    def __init__(self) -> None:
        base = os.environ.get("ZNUNY_URL", "").strip().rstrip("/")
        ws = os.environ.get("ZNUNY_WEBSERVICE", "GenericTicketConnectorREST").strip()
        self.user = os.environ.get("ZNUNY_USER", "")
        self.password = os.environ.get("ZNUNY_PASSWORD", "")
        self.session_route = os.environ.get("ZNUNY_SESSION_ROUTE", "/Session")
        self.search_route = os.environ.get("ZNUNY_SEARCH_ROUTE", "/TicketSearch")
        self.timeout = float(os.environ.get("ZNUNY_TIMEOUT", "5"))
        self.verify_ssl = os.environ.get("ZNUNY_VERIFY_SSL", "true").lower() not in (
            "0", "false", "no",
        )
        self._ttl = float(os.environ.get("ZNUNY_CACHE_TTL", "60"))
        self._base = f"{base}/nph-genericinterface.pl/Webservice/{ws}" if base and ws else ""
        self.enabled = bool(self._base and self.user and self.password)

        self._sid: Optional[str] = None
        self._cache: dict[str, tuple[float, bool]] = {}
        self._lock = threading.Lock()

        if self.enabled:
            logger.info("Znuny address verification enabled (%s)", self._base)
        else:
            logger.debug(
                "Znuny address verification disabled "
                "(set ZNUNY_URL, ZNUNY_WEBSERVICE, ZNUNY_USER, ZNUNY_PASSWORD)"
            )

    def verify_address(self, address_id: str) -> Optional[bool]:
        """True if a ticket's CustomerID equals this address exactly, False if none, None if unverifiable."""
        return self._lookup("CustomerID", address_id)

    def account_exists(self, account_id: str) -> Optional[bool]:
        """True if a ticket exists for this account (CustomerUserLogin) — i.e. a possible relocation."""
        return self._lookup("CustomerUserLogin", account_id)

    def _lookup(self, field: str, value: str) -> Optional[bool]:
        value = (value or "").strip()
        if not self.enabled or not value:
            return None
        key = f"{field}:{value}"
        now = time.time()
        cached = self._cache.get(key)
        if cached and cached[0] > now:
            return cached[1]
        result = self._search_exists(field, value)
        if result is not None:  # don't cache transient errors
            self._cache[key] = (now + self._ttl, result)
        return result

    # ------------------------------------------------------------------ internals
    def _ctx(self):
        if self._base.lower().startswith("https") and not self.verify_ssl:
            return ssl._create_unverified_context()
        return None

    def _post(self, route: str, payload: dict) -> dict:
        req = urllib.request.Request(
            self._base + route, data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"}, method="POST",
        )
        with urllib.request.urlopen(req, timeout=self.timeout, context=self._ctx()) as resp:
            return json.loads(resp.read().decode("utf-8", "replace"))

    def _session(self, force: bool = False) -> Optional[str]:
        with self._lock:
            if self._sid and not force:
                return self._sid
            resp = self._post(self.session_route, {"UserLogin": self.user, "Password": self.password})
            self._sid = resp.get("SessionID") if isinstance(resp, dict) else None
            if not self._sid:
                logger.warning("Znuny SessionCreate failed: %s", resp)
            return self._sid

    def _search_exists(self, field: str, value: str) -> Optional[bool]:
        try:
            sid = self._session()
            if not sid:
                return None
            resp = self._post(self.search_route, {"SessionID": sid, field: value})
            if self._auth_failed(resp):  # expired session — recreate once and retry
                sid = self._session(force=True)
                if not sid:
                    return None
                resp = self._post(self.search_route, {"SessionID": sid, field: value})
            if isinstance(resp, dict) and resp.get("Error"):
                logger.warning("Znuny TicketSearch error for %s=%s: %s", field, value, resp["Error"])
                return None
            ids = resp.get("TicketID") if isinstance(resp, dict) else None
            return bool(ids)
        except Exception as exc:  # network / HTTP / JSON — fail soft
            logger.warning("Znuny search failed for %s=%s: %s", field, value, exc)
            return None

    @staticmethod
    def _auth_failed(resp: object) -> bool:
        return (
            isinstance(resp, dict)
            and isinstance(resp.get("Error"), dict)
            and "Auth" in str(resp["Error"].get("ErrorCode", ""))
        )