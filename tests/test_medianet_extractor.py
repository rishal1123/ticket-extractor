"""
Regression tests for MedianetExtractor's account-number resolution.

The account number is what the Znuny account search (sync Step 1.7) and the
ticket formatter's ``account_id_for_validation`` both key off, so a NULL
``Ticket.account`` silently breaks matching for that ticket.

The portal has already moved this field once: before the Oct 2026 UI refresh it
was embedded in the contact name ("Rise and Shine (7610023404100858)"); after
it, it sits on its own muted line in the Contact card ("Account-(282326)") —
and renders as the empty "Account-()" when the contact has no account id.

The refresh fix made those two sources mutually exclusive (``if``/``elif`` on
whether *any* muted sibling text was present), so a Contact card whose first
muted span was a phone/email, or whose account line was the empty form, stored
a NULL account even when the number was sitting in the contact name. These
tests pin each source as a real fallback.

No browser is involved: the page is faked at the Playwright handle boundary.
"""

import pytest

from config import PortalConfig
from extractors.medianet import MedianetExtractor

ACCOUNT_16 = "7610023404100858"
ACCOUNT_6 = "282326"


class FakeHandle:
    """Stands in for a Playwright ElementHandle: only ``evaluate`` is used."""

    def __init__(self, muted_texts):
        self._muted_texts = list(muted_texts)

    def evaluate(self, script):
        return list(self._muted_texts)


class FakePage:
    def __init__(self, muted=None, body_text=""):
        # {data_test: [muted span texts]}
        self._muted = muted or {}
        self._body_text = body_text

    def query_selector(self, selector):
        for data_test, texts in self._muted.items():
            if selector == f"[data-test='{data_test}']":
                return FakeHandle(texts)
        return None

    def inner_text(self, selector):
        return self._body_text


class FakeBrowser:
    def __init__(self, page):
        self.page = page


def _extractor(temp_db, muted=None, body_text="") -> MedianetExtractor:
    config = PortalConfig(
        name="medianet",
        url="https://app.crm.com/account/login",
        username="user",
        password="pass",
    )
    ext = MedianetExtractor(config, temp_db, headless=True)
    ext.browser = FakeBrowser(FakePage(muted=muted, body_text=body_text))
    return ext


# Post-refresh Contact card as it appears in a page's plain text, the shape
# MedianetFormatter._contact() parses (source 3).
BODY_POST_REFRESH = f"""Service Request
S41015
Contact
View Contact
RS
Rise and Shine
Account-({ACCOUNT_16})
7771234
HOMEUD-05-02-06, Neighborhood 4
"""


class TestAccountFromMutedLine:
    """Source 1: the muted 'Account-(id)' line of the Contact card."""

    def test_account_line_alone(self, temp_db):
        ext = _extractor(temp_db, muted={'contact': [f"Account-({ACCOUNT_6})"]})

        account, name = ext._resolve_contact("Rise and Shine")

        assert account == ACCOUNT_6
        assert name == "Rise and Shine"

    def test_account_line_after_other_muted_spans(self, temp_db):
        """The old code read only the FIRST muted span and gave up on it."""
        ext = _extractor(temp_db, muted={
            'contact': ["7771234", "someone@example.com", f"Account-({ACCOUNT_6})"],
        })

        account, _ = ext._resolve_contact("Rise and Shine")

        assert account == ACCOUNT_6

    def test_unlabelled_parenthesised_digits_still_match(self, temp_db):
        """Tolerates the label being renamed again, as it was once already."""
        ext = _extractor(temp_db, muted={'contact': [f"({ACCOUNT_6})"]})

        account, _ = ext._resolve_contact("Rise and Shine")

        assert account == ACCOUNT_6

    def test_name_is_stripped_of_parens_even_when_muted_line_wins(self, temp_db):
        ext = _extractor(temp_db, muted={'contact': [f"Account-({ACCOUNT_6})"]})

        account, name = ext._resolve_contact(f"Rise and Shine ({ACCOUNT_16})")

        assert account == ACCOUNT_6
        assert name == "Rise and Shine"


class TestFallbackToContactName:
    """Source 2: the pre-refresh 'Name (id)' layout."""

    def test_empty_account_line_falls_back_to_name(self, temp_db):
        """The portal's 'Account-()' form must not suppress the name source."""
        ext = _extractor(temp_db, muted={'contact': ["Account-()"]})

        account, name = ext._resolve_contact(f"Rise and Shine ({ACCOUNT_16})")

        assert account == ACCOUNT_16
        assert name == "Rise and Shine"

    def test_phone_only_muted_span_falls_back_to_name(self, temp_db):
        ext = _extractor(temp_db, muted={'contact': ["7771234"]})

        account, name = ext._resolve_contact(f"Rise and Shine ({ACCOUNT_16})")

        assert account == ACCOUNT_16
        assert name == "Rise and Shine"

    def test_no_contact_card_at_all_falls_back_to_name(self, temp_db):
        ext = _extractor(temp_db, muted={})

        account, name = ext._resolve_contact(f"Rise and Shine ({ACCOUNT_16})")

        assert account == ACCOUNT_16
        assert name == "Rise and Shine"


class TestFallbackToPageText:
    """Source 3: shape-based parse shared with MedianetFormatter."""

    def test_parses_body_text_when_dom_sources_miss(self, temp_db):
        ext = _extractor(temp_db, muted={}, body_text=BODY_POST_REFRESH)

        account, name = ext._resolve_contact("Rise and Shine")

        assert account == ACCOUNT_16
        assert name == "Rise and Shine"

    def test_agrees_with_the_formatter_on_the_same_text(self, temp_db):
        """Extractor and formatter must not disagree about the account number."""
        from formatter.model.formatters import MedianetFormatter

        ext = _extractor(temp_db, muted={}, body_text=BODY_POST_REFRESH)

        account, _ = ext._resolve_contact("Rise and Shine")

        assert account == MedianetFormatter().account_id_for_validation(BODY_POST_REFRESH)

    def test_contact_card_absent_recovers_both_fields(self, temp_db):
        """The S41882 shape: the card's XHR had not landed, so the DOM gave
        neither name nor account, yet the page text holds both."""
        ext = _extractor(temp_db, muted={}, body_text=BODY_POST_REFRESH)

        account, name = ext._resolve_contact(None)

        assert account == ACCOUNT_16
        assert name == "Rise and Shine"

    def test_dom_name_is_not_overwritten_by_page_text(self, temp_db):
        """The fallback fills gaps; it must not override what the DOM gave."""
        ext = _extractor(temp_db, muted={}, body_text=BODY_POST_REFRESH)

        account, name = ext._resolve_contact("Preferred DOM Name")

        assert account == ACCOUNT_16
        assert name == "Preferred DOM Name"


class TestNoAccountAvailable:
    def test_returns_none_and_keeps_name(self, temp_db):
        ext = _extractor(temp_db, muted={'contact': ["7771234"]}, body_text="Contact\n")

        account, name = ext._resolve_contact("Rise and Shine")

        assert account is None
        assert name == "Rise and Shine"

    def test_handles_missing_contact_name(self, temp_db):
        ext = _extractor(temp_db, muted={}, body_text="")

        account, name = ext._resolve_contact(None)

        assert account is None
        assert name is None


class TestSiblingDetailTexts:
    def test_returns_every_muted_text(self, temp_db):
        ext = _extractor(temp_db, muted={'contact': ["a", "b", "c"]})

        assert ext._get_sibling_detail_texts('contact') == ["a", "b", "c"]

    def test_returns_empty_list_when_field_absent(self, temp_db):
        ext = _extractor(temp_db, muted={})

        assert ext._get_sibling_detail_texts('assignedUserName') == []


class TestWaitForContactCard:
    """The card is read only after its own XHR lands, not after a blind sleep."""

    def test_waits_for_the_contact_selector(self, temp_db):
        ext = _extractor(temp_db)
        waited = []
        ext.browser.page.wait_for_selector = lambda sel, timeout=None: waited.append((sel, timeout))

        assert ext._wait_for_contact_card("S41882") is True
        assert waited == [(MedianetExtractor.CONTACT_NAME_SELECTOR,
                           MedianetExtractor.CONTACT_CARD_TIMEOUT)]

    def test_timeout_is_not_fatal(self, temp_db):
        """A card that never renders still leaves the rest of the page parseable."""
        ext = _extractor(temp_db)

        def boom(sel, timeout=None):
            raise RuntimeError("timeout")

        ext.browser.page.wait_for_selector = boom

        assert ext._wait_for_contact_card("S41882") is False
