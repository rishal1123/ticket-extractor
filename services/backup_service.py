"""
Backup Service - Database snapshot creation.
"""

import sqlite3

from database import Database
from utils.logger import get_logger

logger = get_logger("backup_service")


class BackupService:
    """Service for creating consistent database snapshots."""

    def __init__(self, db: Database = None):
        self.db = db or Database()

    def create_backup(self, dest_path: str):
        """Snapshot the live database to dest_path via SQLite's online backup
        API (transactionally consistent even while the app is writing to it,
        unlike a plain file copy)."""
        source = sqlite3.connect(self.db.db_path)
        try:
            dest = sqlite3.connect(dest_path)
            try:
                source.backup(dest)
            finally:
                dest.close()
        finally:
            source.close()
