"""Tests for services/backup_service.py (moved off Database in the MVC
cleanup's Phase 5 -- filesystem/backup orchestration is a service concern,
not repository data access)."""

import os
import sqlite3
import tempfile

from services.backup_service import BackupService


class TestCreateBackup:
    def test_backup_is_queryable_and_matches_source(self, temp_db, sample_ticket):
        temp_db.upsert_ticket(sample_ticket)

        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            dest_path = f.name
        try:
            BackupService(temp_db).create_backup(dest_path)

            src_conn = sqlite3.connect(temp_db.db_path)
            dest_conn = sqlite3.connect(dest_path)
            try:
                src_count = src_conn.execute("SELECT COUNT(*) FROM tickets").fetchone()[0]
                dest_count = dest_conn.execute("SELECT COUNT(*) FROM tickets").fetchone()[0]
                assert dest_count == src_count == 1

                dest_tables = {
                    row[0] for row in dest_conn.execute(
                        "SELECT name FROM sqlite_master WHERE type='table'"
                    ).fetchall()
                }
                assert "tickets" in dest_tables
                assert "app_settings" in dest_tables
            finally:
                src_conn.close()
                dest_conn.close()
        finally:
            os.unlink(dest_path)
