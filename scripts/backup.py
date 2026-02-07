#!/usr/bin/env python3
"""
Database Backup Script

Creates timestamped backups of the SQLite database.
Can be run manually or scheduled via cron/Task Scheduler.

Usage:
    python scripts/backup.py                    # Backup to default location
    python scripts/backup.py --output /path    # Backup to custom location
    python scripts/backup.py --keep 7          # Keep only last 7 backups
"""

import os
import sys
import shutil
import argparse
from datetime import datetime
from pathlib import Path

# Add parent directory to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent))

from config import Config


def get_backup_filename() -> str:
    """Generate backup filename with timestamp."""
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    return f"tickets_backup_{timestamp}.db"


def backup_database(output_dir: str = None, keep_count: int = None) -> str:
    """
    Create a backup of the database.

    Args:
        output_dir: Directory to store backup (default: ./backups)
        keep_count: Number of backups to keep (None = keep all)

    Returns:
        Path to the created backup file
    """
    db_path = Config.DATABASE_PATH

    if not os.path.exists(db_path):
        raise FileNotFoundError(f"Database not found: {db_path}")

    # Default backup directory
    if output_dir is None:
        output_dir = os.path.join(os.path.dirname(db_path), "backups")

    # Create backup directory if needed
    os.makedirs(output_dir, exist_ok=True)

    # Generate backup path
    backup_filename = get_backup_filename()
    backup_path = os.path.join(output_dir, backup_filename)

    # Copy database file
    shutil.copy2(db_path, backup_path)

    # Get backup size
    backup_size = os.path.getsize(backup_path)
    size_mb = backup_size / (1024 * 1024)

    print(f"Backup created: {backup_path}")
    print(f"Size: {size_mb:.2f} MB")

    # Cleanup old backups if keep_count specified
    if keep_count is not None and keep_count > 0:
        cleanup_old_backups(output_dir, keep_count)

    return backup_path


def cleanup_old_backups(backup_dir: str, keep_count: int):
    """
    Remove old backups, keeping only the most recent ones.

    Args:
        backup_dir: Directory containing backups
        keep_count: Number of backups to keep
    """
    # Get all backup files
    backup_files = []
    for f in os.listdir(backup_dir):
        if f.startswith("tickets_backup_") and f.endswith(".db"):
            full_path = os.path.join(backup_dir, f)
            backup_files.append((full_path, os.path.getmtime(full_path)))

    # Sort by modification time (newest first)
    backup_files.sort(key=lambda x: x[1], reverse=True)

    # Remove old backups
    removed_count = 0
    for backup_path, _ in backup_files[keep_count:]:
        try:
            os.remove(backup_path)
            removed_count += 1
            print(f"Removed old backup: {os.path.basename(backup_path)}")
        except Exception as e:
            print(f"Failed to remove {backup_path}: {e}")

    if removed_count > 0:
        print(f"Cleaned up {removed_count} old backup(s)")


def list_backups(backup_dir: str = None):
    """List all existing backups."""
    if backup_dir is None:
        backup_dir = os.path.join(os.path.dirname(Config.DATABASE_PATH), "backups")

    if not os.path.exists(backup_dir):
        print("No backups found.")
        return

    backup_files = []
    for f in os.listdir(backup_dir):
        if f.startswith("tickets_backup_") and f.endswith(".db"):
            full_path = os.path.join(backup_dir, f)
            size = os.path.getsize(full_path) / (1024 * 1024)
            mtime = datetime.fromtimestamp(os.path.getmtime(full_path))
            backup_files.append((f, size, mtime))

    if not backup_files:
        print("No backups found.")
        return

    # Sort by date (newest first)
    backup_files.sort(key=lambda x: x[2], reverse=True)

    print(f"\nBackups in {backup_dir}:")
    print("-" * 60)
    for filename, size, mtime in backup_files:
        print(f"  {filename}  ({size:.2f} MB)  {mtime.strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"\nTotal: {len(backup_files)} backup(s)")


def restore_backup(backup_path: str, force: bool = False) -> bool:
    """
    Restore database from a backup.

    Args:
        backup_path: Path to backup file
        force: Skip confirmation prompt

    Returns:
        True if restore successful
    """
    db_path = Config.DATABASE_PATH

    if not os.path.exists(backup_path):
        raise FileNotFoundError(f"Backup not found: {backup_path}")

    if not force:
        confirm = input(f"This will replace {db_path} with {backup_path}. Continue? [y/N] ")
        if confirm.lower() != 'y':
            print("Restore cancelled.")
            return False

    # Create backup of current database before restore
    if os.path.exists(db_path):
        pre_restore_backup = db_path + ".pre_restore"
        shutil.copy2(db_path, pre_restore_backup)
        print(f"Current database backed up to: {pre_restore_backup}")

    # Restore from backup
    shutil.copy2(backup_path, db_path)
    print(f"Database restored from: {backup_path}")

    return True


def main():
    parser = argparse.ArgumentParser(description="Database backup utility")
    parser.add_argument("--output", "-o", help="Backup output directory")
    parser.add_argument("--keep", "-k", type=int, help="Number of backups to keep")
    parser.add_argument("--list", "-l", action="store_true", help="List existing backups")
    parser.add_argument("--restore", "-r", help="Restore from backup file")
    parser.add_argument("--force", "-f", action="store_true", help="Skip confirmation for restore")

    args = parser.parse_args()

    try:
        if args.list:
            list_backups(args.output)
        elif args.restore:
            restore_backup(args.restore, args.force)
        else:
            backup_database(args.output, args.keep)
    except Exception as e:
        print(f"Error: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
