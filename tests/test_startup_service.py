"""Tests for services/startup_service.py (extracted from app.py in the MVC
cleanup so the entrypoint just orchestrates instead of owning the startup
diagnostics banner)."""

from datetime import timedelta

from database import now_maldives
from services.startup_service import run_startup_check, _format_ago, _dir_size_mb


class TestRunStartupCheck:
    def test_runs_without_error_on_empty_db(self, temp_db):
        run_startup_check(temp_db)  # should not raise

    def test_logs_summary_to_system_logs(self, temp_db):
        run_startup_check(temp_db)
        with temp_db._get_connection() as conn:
            row = conn.execute(
                "SELECT source, message FROM system_logs WHERE source = 'startup' ORDER BY id DESC LIMIT 1"
            ).fetchone()
        assert row is not None
        assert "started" in row["message"]

    def test_reflects_seeded_ticket_counts(self, temp_db, sample_ticket):
        temp_db.upsert_ticket(sample_ticket)
        run_startup_check(temp_db)  # should not raise with a non-empty DB


class TestFormatAgo:
    def test_none_returns_never(self):
        assert _format_ago(None) == "never"

    def test_just_now(self):
        assert _format_ago(now_maldives().isoformat()) == "just now"

    def test_minutes_ago(self):
        ts = (now_maldives() - timedelta(minutes=5)).isoformat()
        assert _format_ago(ts) == "5m ago"

    def test_hours_ago(self):
        ts = (now_maldives() - timedelta(hours=2, minutes=15)).isoformat()
        assert _format_ago(ts) == "2h 15m ago"

    def test_invalid_string_returns_unknown(self):
        assert _format_ago("not-a-date") == "unknown"


class TestDirSizeMb:
    def test_nonexistent_dir_returns_zero(self, tmp_path):
        assert _dir_size_mb(str(tmp_path / "does-not-exist")) == 0

    def test_sums_file_sizes(self, tmp_path):
        (tmp_path / "a.txt").write_bytes(b"x" * 1024)
        (tmp_path / "b.txt").write_bytes(b"x" * 1024)
        size_mb = _dir_size_mb(str(tmp_path))
        assert abs(size_mb - (2048 / (1024 * 1024))) < 1e-6
