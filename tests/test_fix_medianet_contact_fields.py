"""Tests for scripts/fix_medianet_contact_fields.py — the startup repair that
recovers NULL Medianet account/customer_name from a ticket's raw_dump.

The script writes to the tickets table, so each scenario runs against a real
(temp) migrated schema. The safety property that matters most is that it only
ever fills columns that are *empty*: a correctly-extracted row must come out
byte-identical, whatever its dump says.
"""

import sqlite3

import pytest

from database import Database
from scripts import fix_medianet_contact_fields as fixer

ACCOUNT = "7610023404100858"
NAME = "Rise and Shine"


def _dump(name: str = NAME, account: str = ACCOUNT) -> str:
    """A detail-page dump in the post-refresh shape capture_raw_dump() produces."""
    return "\n".join([
        "Medianet Pvt Ltd",
        "Service Request",
        "S41882",
        "Contact",
        "View Contact",
        "RS",
        name,
        f"Account-({account})",
        "7928891",
        "HOMEMACL-A-06-08, Neighborhood 3",
    ])


def _pre_refresh_dump(name: str = NAME, account: str = ACCOUNT) -> str:
    """The pre-Oct-2026 layout: name and account combined on one line."""
    return "\n".join([
        "Service Request",
        "S30001",
        "Contact Details",
        f"{name} ({account})",
        "7928891",
        "HOMEMACL-A-06-08, Neighborhood 3",
    ])


# A dump captured when the page failed to load - no contact block at all.
TRUNCATED_DUMP = "\n".join([
    "Medianet Pvt Ltd", "Setup Walkthrough", "Home", " CRM", "Overview",
    "Service Requests", "Settings", " Help", " Fathmath Shaza Imran", "403",
])


@pytest.fixture
def db_path(tmp_path, monkeypatch):
    """A real, migrated DB the script will operate on."""
    path = tmp_path / "tickets.db"
    Database(str(path))
    monkeypatch.setattr(fixer, "DB_PATH", str(path))
    return str(path)


def _insert(path, **fields):
    fields.setdefault("portal", "medianet")
    fields.setdefault("ticket_id", "S41882")
    cols = ", ".join(fields)
    marks = ", ".join("?" * len(fields))
    conn = sqlite3.connect(path)
    try:
        cur = conn.cursor()
        cur.execute(f"INSERT INTO tickets ({cols}) VALUES ({marks})", tuple(fields.values()))
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def _row(path, row_id):
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    try:
        return dict(conn.execute("SELECT * FROM tickets WHERE id = ?", (row_id,)).fetchone())
    finally:
        conn.close()


def _run(monkeypatch, *args):
    monkeypatch.setattr("sys.argv", ["fix_medianet_contact_fields.py", *args])
    return fixer.main()


class TestRecovery:
    def test_recovers_both_fields_from_post_refresh_dump(self, db_path, monkeypatch):
        """The S41882 shape: the contact card never rendered, so both are NULL."""
        rid = _insert(db_path, account=None, customer_name=None, raw_dump=_dump())

        assert _run(monkeypatch, "--apply") == 0

        row = _row(db_path, rid)
        assert row["account"] == ACCOUNT
        assert row["customer_name"] == NAME

    def test_recovers_account_only_when_name_already_stored(self, db_path, monkeypatch):
        rid = _insert(db_path, account=None, customer_name="Already Known", raw_dump=_dump())

        _run(monkeypatch, "--apply")

        row = _row(db_path, rid)
        assert row["account"] == ACCOUNT
        assert row["customer_name"] == "Already Known"

    def test_recovers_from_pre_refresh_dump(self, db_path, monkeypatch):
        """Older rows captured before the refresh use the combined-line layout."""
        rid = _insert(db_path, ticket_id="S30001", account=None, customer_name=None,
                      raw_dump=_pre_refresh_dump())

        _run(monkeypatch, "--apply")

        row = _row(db_path, rid)
        assert row["account"] == ACCOUNT
        assert row["customer_name"] == NAME

    def test_treats_empty_string_as_missing(self, db_path, monkeypatch):
        rid = _insert(db_path, account="", customer_name="", raw_dump=_dump())

        _run(monkeypatch, "--apply")

        row = _row(db_path, rid)
        assert row["account"] == ACCOUNT
        assert row["customer_name"] == NAME


class TestNeverOverwrites:
    """The core safety property: populated columns are untouchable."""

    def test_correct_row_is_left_exactly_as_is(self, db_path, monkeypatch):
        rid = _insert(db_path, account="111111", customer_name="Stored Name",
                      raw_dump=_dump(name="Dump Name", account="999999"))
        before = _row(db_path, rid)

        _run(monkeypatch, "--apply")

        assert _row(db_path, rid) == before

    def test_row_without_a_dump_is_left_alone(self, db_path, monkeypatch):
        rid = _insert(db_path, account=None, customer_name=None, raw_dump=None)

        _run(monkeypatch, "--apply")

        row = _row(db_path, rid)
        assert row["account"] is None
        assert row["customer_name"] is None

    def test_unparsable_dump_is_left_alone(self, db_path, monkeypatch):
        """Truncated / HTTP-403 dumps have no contact block to recover."""
        rid = _insert(db_path, account=None, customer_name=None, raw_dump=TRUNCATED_DUMP)

        _run(monkeypatch, "--apply")

        row = _row(db_path, rid)
        assert row["account"] is None
        assert row["customer_name"] is None

    def test_other_portals_are_not_touched(self, db_path, monkeypatch):
        rid = _insert(db_path, portal="rol", ticket_id="12345",
                      account=None, customer_name=None, raw_dump=_dump())

        _run(monkeypatch, "--apply")

        row = _row(db_path, rid)
        assert row["account"] is None
        assert row["customer_name"] is None


class TestDryRunAndIdempotence:
    def test_dry_run_changes_nothing(self, db_path, monkeypatch):
        rid = _insert(db_path, account=None, customer_name=None, raw_dump=_dump())
        before = _row(db_path, rid)

        assert _run(monkeypatch) == 0

        assert _row(db_path, rid) == before

    def test_second_apply_is_a_no_op(self, db_path, monkeypatch):
        rid = _insert(db_path, account=None, customer_name=None, raw_dump=_dump())

        _run(monkeypatch, "--apply")
        after_first = _row(db_path, rid)
        _run(monkeypatch, "--apply")

        assert _row(db_path, rid) == after_first

    def test_logs_the_repair_to_system_logs(self, db_path, monkeypatch):
        _insert(db_path, account=None, customer_name=None, raw_dump=_dump())

        _run(monkeypatch, "--apply")

        conn = sqlite3.connect(db_path)
        try:
            sources = [r[0] for r in conn.execute("SELECT source FROM system_logs")]
        finally:
            conn.close()
        assert "migration.medianet_contact" in sources

    def test_clean_db_writes_no_log_entry(self, db_path, monkeypatch):
        """A healthy DB must be a silent no-op, not a log entry every restart."""
        _insert(db_path, account="111111", customer_name="Stored Name", raw_dump=_dump())

        _run(monkeypatch, "--apply")

        conn = sqlite3.connect(db_path)
        try:
            count = conn.execute(
                "SELECT COUNT(*) FROM system_logs WHERE source = 'migration.medianet_contact'"
            ).fetchone()[0]
        finally:
            conn.close()
        assert count == 0


class TestParseContact:
    def test_returns_account_and_name(self):
        assert fixer.parse_contact(fixer.MedianetFormatter(), _dump()) == (ACCOUNT, NAME)

    def test_returns_nones_for_unparsable_text(self):
        assert fixer.parse_contact(fixer.MedianetFormatter(), TRUNCATED_DUMP) == (None, None)

    def test_empty_account_parens_yields_no_account(self):
        """The portal renders "Account-()" for contacts with no account id."""
        account, name = fixer.parse_contact(fixer.MedianetFormatter(), _dump(account=""))
        assert account is None
        assert name == NAME
