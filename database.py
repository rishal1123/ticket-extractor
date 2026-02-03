import sqlite3
from datetime import datetime, timezone, timedelta
from typing import Optional
from contextlib import contextmanager

from models.ticket import Ticket
from config import Config
from utils.logger import get_logger

logger = get_logger("database")

# Maldives timezone (UTC+5)
MVT = timezone(timedelta(hours=5))


def now_maldives() -> datetime:
    """Get current time in Maldives timezone."""
    return datetime.now(MVT)


class Database:
    def __init__(self, db_path: str = None):
        self.db_path = db_path or Config.DATABASE_PATH
        self._init_db()

    @contextmanager
    def _get_connection(self):
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        except Exception as e:
            conn.rollback()
            raise e
        finally:
            conn.close()

    def _init_db(self):
        logger.info(f"Initializing database at {self.db_path}")
        with self._get_connection() as conn:
            cursor = conn.cursor()

            # Tickets table
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS tickets (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    portal TEXT NOT NULL,
                    ticket_id TEXT NOT NULL,
                    address TEXT,
                    customer_name TEXT,
                    ticket_type TEXT,
                    portal_created_at DATETIME,
                    service_type TEXT,
                    status TEXT,
                    kpi TEXT,
                    notes TEXT,
                    in_znuny BOOLEAN DEFAULT 0,
                    znuny_ticket_id TEXT,
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    completed_at DATETIME,
                    UNIQUE(portal, ticket_id)
                )
            """)

            # Add completed_at column if it doesn't exist (migration for existing DBs)
            try:
                cursor.execute("ALTER TABLE tickets ADD COLUMN completed_at DATETIME")
            except:
                pass  # Column already exists

            # Rename time to portal_created_at if needed (migration)
            try:
                cursor.execute("ALTER TABLE tickets ADD COLUMN portal_created_at DATETIME")
            except:
                pass  # Column already exists

            # Notes history table
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS ticket_notes_history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ticket_id INTEGER,
                    note TEXT,
                    recorded_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (ticket_id) REFERENCES tickets(id)
                )
            """)

            # Extraction logs table
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS extraction_logs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    portal TEXT NOT NULL,
                    status TEXT,
                    tickets_found INTEGER DEFAULT 0,
                    tickets_new INTEGER DEFAULT 0,
                    tickets_updated INTEGER DEFAULT 0,
                    error_message TEXT,
                    extracted_at DATETIME DEFAULT CURRENT_TIMESTAMP
                )
            """)

            logger.info("Database initialized successfully")

    def upsert_ticket(self, ticket: Ticket) -> tuple[int, bool, bool]:
        """
        Insert or update a ticket.
        Returns: (ticket_id, is_new, is_updated)
        """
        with self._get_connection() as conn:
            cursor = conn.cursor()

            # Check if ticket exists
            cursor.execute(
                "SELECT id, notes FROM tickets WHERE portal = ? AND ticket_id = ?",
                (ticket.portal, ticket.ticket_id)
            )
            existing = cursor.fetchone()

            if existing:
                existing_id = existing["id"]
                existing_notes = existing["notes"]

                # Update existing ticket
                cursor.execute("""
                    UPDATE tickets SET
                        address = ?,
                        customer_name = ?,
                        ticket_type = ?,
                        portal_created_at = ?,
                        service_type = ?,
                        status = ?,
                        kpi = ?,
                        notes = ?,
                        updated_at = ?
                    WHERE id = ?
                """, (
                    ticket.address,
                    ticket.customer_name,
                    ticket.ticket_type,
                    ticket.portal_created_at,
                    ticket.service_type,
                    ticket.status,
                    ticket.kpi,
                    ticket.notes,
                    now_maldives(),
                    existing_id
                ))

                # Track note changes
                if ticket.notes and ticket.notes != existing_notes:
                    cursor.execute(
                        "INSERT INTO ticket_notes_history (ticket_id, note) VALUES (?, ?)",
                        (existing_id, ticket.notes)
                    )
                    logger.debug(f"Note updated for ticket {ticket.ticket_id}")

                is_updated = cursor.rowcount > 0
                return existing_id, False, is_updated
            else:
                # Insert new ticket
                cursor.execute("""
                    INSERT INTO tickets (
                        portal, ticket_id, address, customer_name, ticket_type,
                        portal_created_at, service_type, status, kpi, notes
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    ticket.portal,
                    ticket.ticket_id,
                    ticket.address,
                    ticket.customer_name,
                    ticket.ticket_type,
                    ticket.portal_created_at,
                    ticket.service_type,
                    ticket.status,
                    ticket.kpi,
                    ticket.notes
                ))

                new_id = cursor.lastrowid

                # Record initial note if exists
                if ticket.notes:
                    cursor.execute(
                        "INSERT INTO ticket_notes_history (ticket_id, note) VALUES (?, ?)",
                        (new_id, ticket.notes)
                    )

                logger.info(f"New ticket added: {ticket.portal}/{ticket.ticket_id}")
                return new_id, True, False

    def get_all_tickets(self, portal: str = None) -> list[Ticket]:
        with self._get_connection() as conn:
            cursor = conn.cursor()

            if portal:
                cursor.execute(
                    "SELECT * FROM tickets WHERE portal = ? ORDER BY updated_at DESC",
                    (portal,)
                )
            else:
                cursor.execute("SELECT * FROM tickets ORDER BY updated_at DESC")

            rows = cursor.fetchall()
            return [self._row_to_ticket(row) for row in rows]

    def get_ticket_by_id(self, ticket_id: int) -> Optional[Ticket]:
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM tickets WHERE id = ?", (ticket_id,))
            row = cursor.fetchone()
            return self._row_to_ticket(row) if row else None

    def get_ticket_notes_history(self, ticket_id: int) -> list[dict]:
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT note, recorded_at FROM ticket_notes_history WHERE ticket_id = ? ORDER BY recorded_at DESC",
                (ticket_id,)
            )
            return [{"note": row["note"], "recorded_at": row["recorded_at"]} for row in cursor.fetchall()]

    def update_znuny_status(self, ticket_id: int, in_znuny: bool, znuny_ticket_id: str = None):
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "UPDATE tickets SET in_znuny = ?, znuny_ticket_id = ?, updated_at = ? WHERE id = ?",
                (in_znuny, znuny_ticket_id, now_maldives(), ticket_id)
            )
            logger.info(f"Updated Znuny status for ticket {ticket_id}: in_znuny={in_znuny}")

    def get_active_ticket_ids(self, portal: str) -> set[str]:
        """Get all ticket IDs for a portal that are not marked as complete."""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT ticket_id FROM tickets WHERE portal = ? AND status != 'Complete'",
                (portal,)
            )
            return {row["ticket_id"] for row in cursor.fetchall()}

    def mark_tickets_complete(self, portal: str, ticket_ids: list[str]) -> int:
        """Mark tickets as complete when they disappear from the portal."""
        if not ticket_ids:
            return 0

        with self._get_connection() as conn:
            cursor = conn.cursor()
            now = now_maldives()
            placeholders = ",".join("?" * len(ticket_ids))
            cursor.execute(f"""
                UPDATE tickets
                SET status = 'Complete', updated_at = ?, completed_at = ?
                WHERE portal = ? AND ticket_id IN ({placeholders}) AND status != 'Complete'
            """, [now, now, portal] + list(ticket_ids))
            count = cursor.rowcount
            if count > 0:
                logger.info(f"Marked {count} tickets as complete for {portal}")
            return count

    def log_extraction(self, portal: str, status: str, tickets_found: int = 0,
                       tickets_new: int = 0, tickets_updated: int = 0,
                       tickets_completed: int = 0, error_message: str = None):
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                INSERT INTO extraction_logs (portal, status, tickets_found, tickets_new, tickets_updated, error_message)
                VALUES (?, ?, ?, ?, ?, ?)
            """, (portal, status, tickets_found, tickets_new, tickets_updated, error_message))
            logger.info(f"Logged extraction for {portal}: {status} (found={tickets_found}, new={tickets_new}, updated={tickets_updated}, completed={tickets_completed})")

    def get_extraction_logs(self, limit: int = 100) -> list[dict]:
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT * FROM extraction_logs ORDER BY extracted_at DESC LIMIT ?",
                (limit,)
            )
            return [dict(row) for row in cursor.fetchall()]

    def get_stats(self) -> dict:
        with self._get_connection() as conn:
            cursor = conn.cursor()

            # Total tickets by portal
            cursor.execute("""
                SELECT portal, COUNT(*) as count FROM tickets GROUP BY portal
            """)
            by_portal = {row["portal"]: row["count"] for row in cursor.fetchall()}

            # Total tickets
            cursor.execute("SELECT COUNT(*) as total FROM tickets")
            total = cursor.fetchone()["total"]

            # Not in Znuny
            cursor.execute("SELECT COUNT(*) as count FROM tickets WHERE in_znuny = 0")
            not_in_znuny = cursor.fetchone()["count"]

            # By status
            cursor.execute("""
                SELECT status, COUNT(*) as count FROM tickets GROUP BY status
            """)
            by_status = {row["status"] or "Unknown": row["count"] for row in cursor.fetchall()}

            # By ticket type
            cursor.execute("""
                SELECT ticket_type, COUNT(*) as count FROM tickets GROUP BY ticket_type
            """)
            by_type = {row["ticket_type"] or "Unknown": row["count"] for row in cursor.fetchall()}

            return {
                "total": total,
                "by_portal": by_portal,
                "not_in_znuny": not_in_znuny,
                "by_status": by_status,
                "by_type": by_type
            }

    def _row_to_ticket(self, row) -> Ticket:
        return Ticket(
            id=row["id"],
            portal=row["portal"],
            ticket_id=row["ticket_id"],
            address=row["address"],
            customer_name=row["customer_name"],
            ticket_type=row["ticket_type"],
            portal_created_at=datetime.fromisoformat(row["portal_created_at"]) if row["portal_created_at"] else None,
            service_type=row["service_type"],
            status=row["status"],
            kpi=row["kpi"],
            notes=row["notes"],
            in_znuny=bool(row["in_znuny"]),
            znuny_ticket_id=row["znuny_ticket_id"],
            created_at=datetime.fromisoformat(row["created_at"]) if row["created_at"] else None,
            updated_at=datetime.fromisoformat(row["updated_at"]) if row["updated_at"] else None,
            completed_at=datetime.fromisoformat(row["completed_at"]) if row["completed_at"] else None
        )
