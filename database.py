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
                    account TEXT,
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

            # Add account column if it doesn't exist (migration)
            try:
                cursor.execute("ALTER TABLE tickets ADD COLUMN account TEXT")
            except:
                pass  # Column already exists

            # Add znuny_created_at column (migration)
            try:
                cursor.execute("ALTER TABLE tickets ADD COLUMN znuny_created_at DATETIME")
            except:
                pass  # Column already exists

            # Add znuny_created_by column (migration)
            try:
                cursor.execute("ALTER TABLE tickets ADD COLUMN znuny_created_by TEXT")
            except:
                pass  # Column already exists

            # Add znuny_address column (migration)
            try:
                cursor.execute("ALTER TABLE tickets ADD COLUMN znuny_address TEXT")
            except:
                pass  # Column already exists

            # Znuny articles table - stores article/note history from Znuny
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS znuny_articles (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ticket_id INTEGER,
                    znuny_ticket_id TEXT NOT NULL,
                    article_number INTEGER,
                    sender TEXT,
                    via TEXT,
                    subject TEXT,
                    created_at DATETIME,
                    created_at_str TEXT,
                    created_by TEXT,
                    fetched_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (ticket_id) REFERENCES tickets(id),
                    UNIQUE(znuny_ticket_id, article_number)
                )
            """)

            # Add created_by column to znuny_articles if it doesn't exist (migration)
            try:
                cursor.execute("ALTER TABLE znuny_articles ADD COLUMN created_by TEXT")
            except:
                pass  # Column already exists

            # Add body column to znuny_articles if it doesn't exist (migration)
            try:
                cursor.execute("ALTER TABLE znuny_articles ADD COLUMN body TEXT")
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
                        account = ?,
                        customer_name = ?,
                        ticket_type = ?,
                        portal_created_at = ?,
                        service_type = ?,
                        status = ?,
                        kpi = ?,
                        notes = ?,
                        updated_at = ?,
                        completed_at = COALESCE(?, completed_at)
                    WHERE id = ?
                """, (
                    ticket.address,
                    ticket.account,
                    ticket.customer_name,
                    ticket.ticket_type,
                    ticket.portal_created_at,
                    ticket.service_type,
                    ticket.status,
                    ticket.kpi,
                    ticket.notes,
                    now_maldives(),
                    ticket.completed_at,  # Set completed_at if provided
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
                        portal, ticket_id, address, account, customer_name, ticket_type,
                        portal_created_at, service_type, status, kpi, notes, completed_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    ticket.portal,
                    ticket.ticket_id,
                    ticket.address,
                    ticket.account,
                    ticket.customer_name,
                    ticket.ticket_type,
                    ticket.portal_created_at,
                    ticket.service_type,
                    ticket.status,
                    ticket.kpi,
                    ticket.notes,
                    ticket.completed_at  # Set completed_at if provided (for closed tickets)
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

    def get_all_tickets(self, portal: str = None, include_completed: bool = False) -> list[Ticket]:
        with self._get_connection() as conn:
            cursor = conn.cursor()

            if portal:
                if include_completed:
                    cursor.execute(
                        "SELECT * FROM tickets WHERE portal = ? ORDER BY updated_at DESC",
                        (portal,)
                    )
                else:
                    cursor.execute(
                        "SELECT * FROM tickets WHERE portal = ? AND completed_at IS NULL ORDER BY updated_at DESC",
                        (portal,)
                    )
            else:
                if include_completed:
                    cursor.execute("SELECT * FROM tickets ORDER BY updated_at DESC")
                else:
                    cursor.execute("SELECT * FROM tickets WHERE completed_at IS NULL ORDER BY updated_at DESC")

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

    def update_znuny_details(self, ticket_id: int, znuny_created_at: datetime = None,
                             znuny_created_by: str = None, znuny_address: str = None):
        """Update Znuny-specific details for a ticket."""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """UPDATE tickets SET
                    znuny_created_at = COALESCE(?, znuny_created_at),
                    znuny_created_by = COALESCE(?, znuny_created_by),
                    znuny_address = COALESCE(?, znuny_address),
                    updated_at = ?
                WHERE id = ?""",
                (znuny_created_at, znuny_created_by, znuny_address, now_maldives(), ticket_id)
            )
            logger.info(f"Updated Znuny details for ticket {ticket_id}: created_by={znuny_created_by}")

    def upsert_znuny_article(self, ticket_id: int, znuny_ticket_id: str, article_number: int,
                             sender: str, via: str, subject: str,
                             created_at: datetime = None, created_at_str: str = None,
                             created_by: str = None, body: str = None):
        """Insert or update a Znuny article."""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                INSERT OR REPLACE INTO znuny_articles
                (ticket_id, znuny_ticket_id, article_number, sender, via, subject, created_at, created_at_str, created_by, body)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (ticket_id, znuny_ticket_id, article_number, sender, via, subject, created_at, created_at_str, created_by, body))

    def get_znuny_articles(self, ticket_id: int = None, znuny_ticket_id: str = None) -> list[dict]:
        """Get Znuny articles for a ticket."""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            if ticket_id:
                cursor.execute(
                    "SELECT * FROM znuny_articles WHERE ticket_id = ? ORDER BY article_number",
                    (ticket_id,)
                )
            elif znuny_ticket_id:
                cursor.execute(
                    "SELECT * FROM znuny_articles WHERE znuny_ticket_id = ? ORDER BY article_number",
                    (znuny_ticket_id,)
                )
            else:
                return []
            return [dict(row) for row in cursor.fetchall()]

    def get_staff_stats(self, date_from: str = None, date_to: str = None) -> dict:
        """Get statistics about staff activity in Znuny.

        Args:
            date_from: Start date in YYYY-MM-DD format (inclusive)
            date_to: End date in YYYY-MM-DD format (inclusive)
        """
        with self._get_connection() as conn:
            cursor = conn.cursor()

            # Build date filter conditions
            date_filter_tickets = ""
            date_filter_articles = ""
            params_tickets = []
            params_articles = []

            if date_from:
                date_filter_tickets += " AND DATE(znuny_created_at) >= ?"
                date_filter_articles += " AND DATE(created_at) >= ?"
                params_tickets.append(date_from)
                params_articles.append(date_from)
            if date_to:
                date_filter_tickets += " AND DATE(znuny_created_at) <= ?"
                date_filter_articles += " AND DATE(created_at) <= ?"
                params_tickets.append(date_to)
                params_articles.append(date_to)

            # Tickets created by staff (from znuny_created_by)
            cursor.execute(f"""
                SELECT znuny_created_by as staff, COUNT(*) as tickets_created
                FROM tickets
                WHERE znuny_created_by IS NOT NULL AND znuny_created_by != ''
                {date_filter_tickets}
                GROUP BY znuny_created_by
                ORDER BY tickets_created DESC
            """, params_tickets)
            tickets_created = {row["staff"]: row["tickets_created"] for row in cursor.fetchall()}

            # Articles/updates count by staff (using created_by field)
            cursor.execute(f"""
                SELECT created_by as staff, COUNT(*) as articles_count
                FROM znuny_articles
                WHERE created_by IS NOT NULL AND created_by != ''
                {date_filter_articles}
                GROUP BY created_by
                ORDER BY articles_count DESC
            """, params_articles)
            articles_by_staff = {row["staff"]: row["articles_count"] for row in cursor.fetchall()}

            # Unique tickets updated by staff
            cursor.execute(f"""
                SELECT created_by as staff, COUNT(DISTINCT znuny_ticket_id) as tickets_updated
                FROM znuny_articles
                WHERE created_by IS NOT NULL AND created_by != ''
                {date_filter_articles}
                GROUP BY created_by
                ORDER BY tickets_updated DESC
            """, params_articles)
            tickets_updated = {row["staff"]: row["tickets_updated"] for row in cursor.fetchall()}

            # Combine all staff names
            all_staff = set(tickets_created.keys()) | set(articles_by_staff.keys())

            staff_stats = []
            for staff in all_staff:
                staff_stats.append({
                    "name": staff,
                    "tickets_created": tickets_created.get(staff, 0),
                    "articles_count": articles_by_staff.get(staff, 0),
                    "tickets_updated": tickets_updated.get(staff, 0)
                })

            # Sort by tickets created, then by articles count
            staff_stats.sort(key=lambda x: (-x["tickets_created"], -x["articles_count"]))

            return {
                "staff": staff_stats,
                "total_tickets_in_znuny": sum(tickets_created.values()),
                "total_articles": sum(articles_by_staff.values()),
                "date_from": date_from,
                "date_to": date_to
            }

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
                INSERT INTO extraction_logs (portal, status, tickets_found, tickets_new, tickets_updated, error_message, extracted_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (portal, status, tickets_found, tickets_new, tickets_updated, error_message, now_maldives()))
            logger.info(f"Logged extraction for {portal}: {status} (found={tickets_found}, new={tickets_new}, updated={tickets_updated}, completed={tickets_completed})")

    def get_extraction_logs(self, limit: int = 100) -> list[dict]:
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT * FROM extraction_logs ORDER BY extracted_at DESC LIMIT ?",
                (limit,)
            )
            return [dict(row) for row in cursor.fetchall()]

    def get_last_extraction_per_portal(self) -> dict:
        """Get the last extraction log for each portal."""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            # Get the most recent extraction log for each portal
            cursor.execute("""
                SELECT e1.*
                FROM extraction_logs e1
                INNER JOIN (
                    SELECT portal, MAX(extracted_at) as max_time
                    FROM extraction_logs
                    GROUP BY portal
                ) e2 ON e1.portal = e2.portal AND e1.extracted_at = e2.max_time
            """)
            return {row["portal"]: {
                "status": row["status"],
                "tickets_found": row["tickets_found"],
                "tickets_new": row["tickets_new"],
                "tickets_updated": row["tickets_updated"],
                "extracted_at": row["extracted_at"],
                "error_message": row["error_message"]
            } for row in cursor.fetchall()}

    def get_stats(self) -> dict:
        with self._get_connection() as conn:
            cursor = conn.cursor()

            # Active tickets by portal (not completed)
            cursor.execute("""
                SELECT portal, COUNT(*) as count FROM tickets WHERE completed_at IS NULL GROUP BY portal
            """)
            by_portal = {row["portal"]: row["count"] for row in cursor.fetchall()}

            # Total active tickets
            cursor.execute("SELECT COUNT(*) as total FROM tickets WHERE completed_at IS NULL")
            total = cursor.fetchone()["total"]

            # Total completed tickets
            cursor.execute("SELECT COUNT(*) as total FROM tickets WHERE completed_at IS NOT NULL")
            completed = cursor.fetchone()["total"]

            # Not in Znuny (active only)
            cursor.execute("SELECT COUNT(*) as count FROM tickets WHERE in_znuny = 0 AND completed_at IS NULL")
            not_in_znuny = cursor.fetchone()["count"]

            # By status (active only)
            cursor.execute("""
                SELECT status, COUNT(*) as count FROM tickets WHERE completed_at IS NULL GROUP BY status
            """)
            by_status = {row["status"] or "Unknown": row["count"] for row in cursor.fetchall()}

            # By ticket type (active only)
            cursor.execute("""
                SELECT ticket_type, COUNT(*) as count FROM tickets WHERE completed_at IS NULL GROUP BY ticket_type
            """)
            by_type = {row["ticket_type"] or "Unknown": row["count"] for row in cursor.fetchall()}

            # Last extraction per portal
            last_extraction = self.get_last_extraction_per_portal()

            return {
                "total": total,
                "completed": completed,
                "by_portal": by_portal,
                "not_in_znuny": not_in_znuny,
                "by_status": by_status,
                "by_type": by_type,
                "last_extraction": last_extraction
            }

    def _row_to_ticket(self, row) -> Ticket:
        keys = row.keys()
        return Ticket(
            id=row["id"],
            portal=row["portal"],
            ticket_id=row["ticket_id"],
            address=row["address"],
            account=row["account"] if "account" in keys else None,
            customer_name=row["customer_name"],
            ticket_type=row["ticket_type"],
            portal_created_at=datetime.fromisoformat(row["portal_created_at"]) if row["portal_created_at"] else None,
            service_type=row["service_type"],
            status=row["status"],
            kpi=row["kpi"],
            notes=row["notes"],
            in_znuny=bool(row["in_znuny"]),
            znuny_ticket_id=row["znuny_ticket_id"],
            znuny_created_at=datetime.fromisoformat(row["znuny_created_at"]) if "znuny_created_at" in keys and row["znuny_created_at"] else None,
            znuny_created_by=row["znuny_created_by"] if "znuny_created_by" in keys else None,
            znuny_address=row["znuny_address"] if "znuny_address" in keys else None,
            created_at=datetime.fromisoformat(row["created_at"]) if row["created_at"] else None,
            updated_at=datetime.fromisoformat(row["updated_at"]) if row["updated_at"] else None,
            completed_at=datetime.fromisoformat(row["completed_at"]) if row["completed_at"] else None
        )
