"""Tests for scripts/fix_dhiraagu_id_account_swap.py — the startup repair that
un-swaps Dhiraagu ticket_id/account and merges the duplicate rows the swap
created.

The script deletes rows, so each scenario of the incident is exercised against a
real (temp) schema: a swapped duplicate with a wrongly-completed original, a
swapped row with no original, a dump-less row identified by id shape, and rows
that must not be touched.
"""

import sqlite3

import pytest

from database import Database
from scripts import fix_dhiraagu_id_account_swap as fixer

ORDER = "0129530729"
SERVICE = "BB20052084"


def _dump(order: str = ORDER, service: str = SERVICE) -> str:
    """A detail-page dump in the shape capture_raw_dump() produces."""
    return "\n".join([
        "Order number *", order,
        "Service number *", service,
        "Customer name *", "Abdullah Rasheed",
        "Building/Tower *", "H16",
        f"View # {order} - Pending",
        # The page body repeats the labels with no values after the form fields.
        "Order number *", "Service number *", "Customer name *",
    ])


LABEL_ONLY_DUMP = "Order number *\nService number *\nCustomer name *"


@pytest.fixture
def db_path(tmp_path, monkeypatch):
    """A real, migrated DB the script will operate on."""
    path = tmp_path / "tickets.db"
    Database(str(path))
    monkeypatch.setattr(fixer, "DB_PATH", str(path))
    return str(path)


def _insert(path, **fields):
    fields.setdefault("portal", "dhiraagu")
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


def _rows(path, where="portal = 'dhiraagu'"):
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in conn.execute(f"SELECT * FROM tickets WHERE {where} ORDER BY id")]
    finally:
        conn.close()


def _run(monkeypatch, *args):
    monkeypatch.setattr("sys.argv", ["fix_dhiraagu_id_account_swap.py", *args])
    return fixer.main()


class TestClassification:
    """Which rows the repair considers swapped, and why."""

    def test_dump_confirms_swap(self):
        row = {"ticket_id": SERVICE, "account": ORDER, "raw_dump": _dump()}
        assert fixer.classify(row) == ("swapped-dump", ORDER, SERVICE)

    def test_dump_confirms_correct_row(self):
        row = {"ticket_id": ORDER, "account": SERVICE, "raw_dump": _dump()}
        verdict, _, _ = fixer.classify(row)
        assert verdict == "ok"

    def test_label_only_dump_falls_back_and_stays_undecided(self):
        """Pre-form-value dumps pair a label with the NEXT LABEL — never trusted."""
        row = {"ticket_id": ORDER, "account": SERVICE, "raw_dump": LABEL_ONLY_DUMP}
        verdict, order_no, _ = fixer.classify(row)
        assert verdict == "ok"       # shape says correct
        assert order_no is None      # ...but the dump contributed nothing

    @pytest.mark.parametrize("ticket_id, account", [
        ("BB20052084", "0129530729"),              # service <-> 10-digit order
        ("TV008119", "0129440329_R"),              # suffixed order number
        ("FIB9433", "018751157R1"),
        ("HMF0200", "SSNET-SO-2026-09-00069"),     # SSNET-style order number
        ("3333047", "0129459791"),                 # short all-digit account
    ])
    def test_shape_detects_swap_without_a_dump(self, ticket_id, account):
        row = {"ticket_id": ticket_id, "account": account, "raw_dump": None}
        assert fixer.classify(row) == ("swapped-shape", account, ticket_id)

    @pytest.mark.parametrize("ticket_id, account", [
        ("0129530729", "BB20052084"),              # the correct mapping
        ("0129440329_R", "BB20693710"),
        ("SSNET-SO-2026-09-00069", "TV17616526"),
        ("BB17893875_D", "BB17893875"),            # order # that is itself BB…_D
        ("0126941141", "0126941141"),              # both ids identical
        ("0129459791", "3333047"),
    ])
    def test_shape_leaves_correct_rows_alone(self, ticket_id, account):
        row = {"ticket_id": ticket_id, "account": account, "raw_dump": None}
        verdict, _, _ = fixer.classify(row)
        assert verdict != "swapped-shape"


class TestRepair:
    """End-to-end behaviour against a real schema."""

    def test_dry_run_changes_nothing(self, db_path, monkeypatch, capsys):
        _insert(db_path, ticket_id=SERVICE, account=ORDER, raw_dump=_dump())

        assert _run(monkeypatch) == 0

        assert [r["ticket_id"] for r in _rows(db_path)] == [SERVICE]
        assert "DRY-RUN" in capsys.readouterr().out

    def test_renames_in_place_when_no_original_row(self, db_path, monkeypatch):
        _insert(db_path, ticket_id=SERVICE, account=ORDER, raw_dump=_dump(),
                created_at="2026-11-05 09:00:00+05:00")

        assert _run(monkeypatch, "--apply") == 0

        rows = _rows(db_path)
        assert len(rows) == 1
        assert rows[0]["ticket_id"] == ORDER
        assert rows[0]["account"] == SERVICE
        assert rows[0]["created_at"] == "2026-11-05 09:00:00+05:00"

    def test_merges_into_wrongly_completed_original(self, db_path, monkeypatch):
        keeper_id = _insert(
            db_path, ticket_id=ORDER, account=SERVICE,
            created_at="2026-10-01 08:00:00+05:00",
            completed_at="2026-11-04 11:00:00+05:00",
            customer_name="Abdullah Rasheed", status="New",
            in_znuny=1, znuny_ticket_id="2026110412345678",
            znuny_created_at="2026-10-01 08:03:00+05:00",
        )
        dup_id = _insert(
            db_path, ticket_id=SERVICE, account=ORDER, raw_dump=_dump(),
            created_at="2026-11-04 11:05:00+05:00", status="Suspended",
            address="H16-14-13", portal_url="https://afas.dhiraagu.com.mv/orders/hdc/12856",
            in_znuny=1, znuny_ticket_id="2026110499999999",
        )
        conn = sqlite3.connect(db_path)
        conn.execute(
            "INSERT INTO znuny_articles (ticket_id, znuny_ticket_id, article_number) VALUES (?, ?, ?)",
            (dup_id, "2026110499999999", 1))
        conn.execute(
            "INSERT INTO site_visits (ticket_id, znuny_ticket_id, article_id) VALUES (?, ?, ?)",
            (dup_id, "2026110499999999", 2))
        conn.execute("INSERT INTO ticket_notes_history (ticket_id, note) VALUES (?, 'a note')",
                     (dup_id,))
        conn.execute(
            "INSERT INTO ticket_reopens (ticket_id, portal, portal_ticket_id, reopened_at)"
            " VALUES (?, 'dhiraagu', ?, '2026-11-04 11:05:00+05:00')", (dup_id, SERVICE))
        conn.execute("INSERT INTO znuny_tickets (znuny_ticket_id, isp_ticket_id) VALUES (?, ?)",
                     ("2026110499999999", dup_id))
        conn.commit()
        conn.close()

        assert _run(monkeypatch, "--apply") == 0

        rows = _rows(db_path)
        assert len(rows) == 1, "the swapped duplicate should be gone"
        kept = rows[0]
        assert kept["id"] == keeper_id
        assert (kept["ticket_id"], kept["account"]) == (ORDER, SERVICE)
        # Revived, with the original's first-seen time and Znuny link preserved.
        assert kept["completed_at"] is None
        assert kept["created_at"] == "2026-10-01 08:00:00+05:00"
        assert kept["znuny_ticket_id"] == "2026110412345678"
        # ...and the duplicate's fresher portal data folded in.
        assert kept["status"] == "Suspended"
        assert kept["address"] == "H16-14-13"
        assert kept["raw_dump"] == _dump()

        conn = sqlite3.connect(db_path)
        try:
            for table, column in fixer.CHILD_TABLES:
                owners = [r[0] for r in conn.execute(f"SELECT {column} FROM {table}")]
                assert owners == [keeper_id], f"{table}.{column} not re-pointed"
            reopens = conn.execute(
                "SELECT ticket_id, portal_ticket_id FROM ticket_reopens").fetchall()
            assert reopens == [(keeper_id, ORDER)]
        finally:
            conn.close()

    def test_takes_duplicates_znuny_link_only_when_original_has_none(self, db_path, monkeypatch):
        keeper_id = _insert(db_path, ticket_id=ORDER, account=SERVICE,
                            created_at="2026-10-01 08:00:00+05:00",
                            completed_at="2026-11-04 11:00:00+05:00")
        _insert(db_path, ticket_id=SERVICE, account=ORDER, raw_dump=_dump(),
                in_znuny=1, znuny_ticket_id="2026110499999999",
                znuny_created_by="Staff A", znuny_url="https://znuny/ticket/1")

        assert _run(monkeypatch, "--apply") == 0

        kept = _rows(db_path)[0]
        assert kept["id"] == keeper_id
        assert kept["znuny_ticket_id"] == "2026110499999999"
        assert kept["znuny_created_by"] == "Staff A"
        assert kept["in_znuny"] == 1

    def test_leaves_correct_and_undecidable_rows_untouched(self, db_path, monkeypatch):
        _insert(db_path, ticket_id="0129530001", account="BB11110001", raw_dump=_dump("0129530001", "BB11110001"))
        _insert(db_path, ticket_id="0129530002", account="BB11110002")
        _insert(db_path, ticket_id="WEIRD-1", account="ALSO-WEIRD")
        before = _rows(db_path)

        assert _run(monkeypatch, "--apply") == 0

        assert _rows(db_path) == before

    def test_scoped_to_open_tickets_by_default(self, db_path, monkeypatch):
        _insert(db_path, ticket_id=SERVICE, account=ORDER, raw_dump=_dump(),
                completed_at="2026-11-06 10:00:00+05:00")

        assert _run(monkeypatch, "--apply") == 0
        assert _rows(db_path)[0]["ticket_id"] == SERVICE, "completed rows are out of scope"

        assert _run(monkeypatch, "--apply", "--include-completed") == 0
        assert _rows(db_path)[0]["ticket_id"] == ORDER

    def test_is_idempotent(self, db_path, monkeypatch, capsys):
        _insert(db_path, ticket_id=SERVICE, account=ORDER, raw_dump=_dump())

        _run(monkeypatch, "--apply")
        after_first = _rows(db_path)
        capsys.readouterr()

        assert _run(monkeypatch, "--apply") == 0
        assert _rows(db_path) == after_first
        assert "Nothing to fix" in capsys.readouterr().out

    def test_logs_to_system_logs_on_apply(self, db_path, monkeypatch):
        _insert(db_path, ticket_id=SERVICE, account=ORDER, raw_dump=_dump())

        _run(monkeypatch, "--apply")

        conn = sqlite3.connect(db_path)
        try:
            logs = conn.execute(
                "SELECT level, source, message FROM system_logs WHERE source = 'migration.dhiraagu_swap'"
            ).fetchall()
        finally:
            conn.close()
        assert len(logs) == 1
        assert logs[0][0] == "warning"
        assert "1 ticket(s)" in logs[0][2]
