import sqlite3
import threading
from datetime import datetime, timezone, timedelta
from typing import Optional
from contextlib import contextmanager

from models.ticket import Ticket
from config import Config
from utils.logger import get_logger

logger = get_logger("database")

# Maldives timezone (UTC+5)
MVT = timezone(timedelta(hours=5))

# Tickets left open longer than this are treated as stale-cleanup outliers and
# excluded from "Avg Close" so one ancient ticket closed today can't skew the
# average to weeks/months (30 days).
CLOSE_AVG_CAP_MINUTES = 30 * 24 * 60

# Reusable SQL fragment for calculating site visit duration in minutes.
# Computes: completion_time - (visit_date + scheduled_time)
# The completion time parameter must be bound twice (once for the CASE, once for julianday).
# Usage: Pass the completion time as a parameter, referenced by {completion_param}.
# The fragment expects the site_visits columns: scheduled_time, visit_date.
_DURATION_SQL = """
    CASE
        WHEN scheduled_time GLOB '[0-9][0-9]:[0-9][0-9]*' OR scheduled_time GLOB '[0-9][0-9][0-9][0-9]'
        THEN CAST(
            (julianday(SUBSTR(?, 1, 19)) - julianday(visit_date || ' ' ||
                CASE
                    WHEN LENGTH(scheduled_time) = 4 THEN SUBSTR(scheduled_time, 1, 2) || ':' || SUBSTR(scheduled_time, 3, 2) || ':00'
                    WHEN LENGTH(scheduled_time) <= 5 THEN scheduled_time || ':00'
                    ELSE SUBSTR(scheduled_time, 1, 8)
                END
            )) * 24 * 60 AS INTEGER
        )
        ELSE NULL
    END
""".strip()


def now_maldives() -> datetime:
    """Get current time in Maldives timezone."""
    return datetime.now(MVT)


class Database:
    def __init__(self, db_path: str = None):
        self.db_path = db_path or Config.DATABASE_PATH
        self._logging_lock = threading.Lock()  # Thread-safe guard against recursive logging
        self._init_db()

    def _log_db_error(self, operation: str, error: Exception, context: str = None):
        """Log database error to both logger and system_logs table (if possible)."""
        error_msg = f"{operation} failed: {type(error).__name__}: {error}"
        if context:
            error_msg += f" (context: {context})"
        logger.error(error_msg)

        # Try to log to system_logs table, but avoid infinite recursion
        if self._logging_lock.acquire(blocking=False):
            try:
                self.log_system("error", "database", error_msg, context)
            except Exception:
                pass  # Can't log to DB, already logged to file
            finally:
                self._logging_lock.release()

    @contextmanager
    def _get_connection(self):
        conn = sqlite3.connect(self.db_path, timeout=10.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        try:
            yield conn
            conn.commit()
        except Exception as e:
            conn.rollback()
            logger.error(f"Database error (rolled back): {type(e).__name__}: {e}")
            raise e
        finally:
            conn.close()

    def _init_db(self):
        logger.info(f"Initializing database at {self.db_path}")
        try:
            self._create_tables()
        except Exception as e:
            logger.error(f"Failed to initialize database: {type(e).__name__}: {e}")
            raise

    def _create_tables(self):
        """Create database tables and run migrations."""
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
            except sqlite3.OperationalError:
                pass  # Column already exists

            # Raw portal detail-page text captured at initial extraction, used by
            # the ticket formatter (migration for existing DBs)
            try:
                cursor.execute("ALTER TABLE tickets ADD COLUMN raw_dump TEXT")
            except sqlite3.OperationalError:
                pass  # Column already exists

            # Rename time to portal_created_at if needed (migration)
            try:
                cursor.execute("ALTER TABLE tickets ADD COLUMN portal_created_at DATETIME")
            except sqlite3.OperationalError:
                pass  # Column already exists

            # Add account column if it doesn't exist (migration)
            try:
                cursor.execute("ALTER TABLE tickets ADD COLUMN account TEXT")
            except sqlite3.OperationalError:
                pass  # Column already exists

            # Add znuny_created_at column (migration)
            try:
                cursor.execute("ALTER TABLE tickets ADD COLUMN znuny_created_at DATETIME")
            except sqlite3.OperationalError:
                pass  # Column already exists

            # Add znuny_created_by column (migration)
            try:
                cursor.execute("ALTER TABLE tickets ADD COLUMN znuny_created_by TEXT")
            except sqlite3.OperationalError:
                pass  # Column already exists

            # Add znuny_address column (migration)
            try:
                cursor.execute("ALTER TABLE tickets ADD COLUMN znuny_address TEXT")
            except sqlite3.OperationalError:
                pass  # Column already exists

            # Add portal_url column (migration)
            try:
                cursor.execute("ALTER TABLE tickets ADD COLUMN portal_url TEXT")
            except sqlite3.OperationalError:
                pass  # Column already exists

            # Add znuny_url column (migration)
            try:
                cursor.execute("ALTER TABLE tickets ADD COLUMN znuny_url TEXT")
            except sqlite3.OperationalError:
                pass  # Column already exists

            # Add znuny_search_count column (migration) - tracks account search attempts
            try:
                cursor.execute("ALTER TABLE tickets ADD COLUMN znuny_search_count INTEGER DEFAULT 0")
            except sqlite3.OperationalError:
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
            except sqlite3.OperationalError:
                pass  # Column already exists

            # Add body column to znuny_articles if it doesn't exist (migration)
            try:
                cursor.execute("ALTER TABLE znuny_articles ADD COLUMN body TEXT")
            except sqlite3.OperationalError:
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

            # Login stats table for tracking portal session activity
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS login_stats (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    portal TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    session_id TEXT,
                    success BOOLEAN DEFAULT 1,
                    error_message TEXT,
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP
                )
            """)

            # Staff performance daily log table
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS staff_performance_daily (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    staff_name TEXT NOT NULL,
                    date DATE NOT NULL,
                    tickets_created INTEGER DEFAULT 0,
                    tickets_within_5min INTEGER DEFAULT 0,
                    tickets_within_10min INTEGER DEFAULT 0,
                    tickets_over_10min INTEGER DEFAULT 0,
                    avg_time_to_create_minutes REAL,
                    total_articles INTEGER DEFAULT 0,
                    tickets_updated INTEGER DEFAULT 0,
                    calculated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE(staff_name, date)
                )
            """)

            # Site-visit columns for the daily snapshot (migration)
            for _col in ("site_visits_total", "site_visits_completed"):
                try:
                    cursor.execute(f"ALTER TABLE staff_performance_daily ADD COLUMN {_col} INTEGER DEFAULT 0")
                except sqlite3.OperationalError:
                    pass  # Column already exists

            # System logs table for application-wide logging
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS system_logs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    level TEXT NOT NULL,
                    source TEXT NOT NULL,
                    message TEXT NOT NULL,
                    details TEXT,
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP
                )
            """)

            # Site visits table for tracking OAN site visit articles
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS site_visits (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ticket_id INTEGER,
                    znuny_ticket_id TEXT NOT NULL,
                    article_id INTEGER,
                    site_type TEXT,
                    service_provider TEXT,
                    scheduled_time TEXT,
                    assigned_to TEXT,
                    visit_date DATE,
                    article_created_at DATETIME,
                    ticket_completed_at DATETIME,
                    time_taken_minutes INTEGER,
                    status TEXT DEFAULT 'pending',
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (ticket_id) REFERENCES tickets(id),
                    UNIQUE(znuny_ticket_id, article_id)
                )
            """)

            # App settings table for configurable parameters
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS app_settings (
                    key TEXT PRIMARY KEY,
                    value TEXT,
                    description TEXT,
                    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
                )
            """)

            # Insert default settings if not exist
            default_settings = [
                ('perf_threshold_good', '5', 'Time in minutes considered "on time" (green)'),
                ('perf_threshold_warning', '10', 'Time in minutes considered "warning" (yellow)'),
                ('perf_threshold_bad', '30', 'Time in minutes considered "late" (orange)'),
                ('perf_threshold_critical', '60', 'Time in minutes considered "critical" (red)'),
                ('admin_password', 'admin123', 'Password for admin panel access'),
            ]
            for key, value, desc in default_settings:
                cursor.execute("""
                    INSERT OR IGNORE INTO app_settings (key, value, description)
                    VALUES (?, ?, ?)
                """, (key, value, desc))

            # Performance indexes (consolidated - all indexes defined here)

            # Migration: Add znuny_url to site_visits
            try:
                cursor.execute("ALTER TABLE site_visits ADD COLUMN znuny_url TEXT")
            except sqlite3.OperationalError:
                pass  # Column already exists

            # Migration: Add address and customer_name to site_visits
            for col in ["address TEXT", "customer_name TEXT"]:
                try:
                    cursor.execute(f"ALTER TABLE site_visits ADD COLUMN {col}")
                except sqlite3.OperationalError:
                    pass  # Column already exists

            # Znuny-only tickets table (tickets not linked to any ISP portal)
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS znuny_tickets (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    znuny_ticket_id TEXT UNIQUE NOT NULL,
                    title TEXT,
                    state TEXT,
                    queue TEXT,
                    priority TEXT,
                    created_at DATETIME,
                    created_by TEXT,
                    closed_at DATETIME,
                    time_to_close_minutes REAL,
                    article_count INTEGER DEFAULT 0,
                    last_article_by TEXT,
                    last_article_at DATETIME,
                    znuny_url TEXT,
                    first_seen_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
                )
            """)

            # Migration: Add isp_ticket_id to znuny_tickets (link to ISP portal ticket)
            try:
                cursor.execute("ALTER TABLE znuny_tickets ADD COLUMN isp_ticket_id INTEGER REFERENCES tickets(id)")
            except sqlite3.OperationalError:
                pass  # Column already exists

            # Migration: Add owner column to znuny_tickets
            try:
                cursor.execute("ALTER TABLE znuny_tickets ADD COLUMN owner TEXT")
            except sqlite3.OperationalError:
                pass  # Column already exists

            # Indexes for znuny_tickets
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_znuny_tickets_state ON znuny_tickets(state)")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_znuny_tickets_created_by ON znuny_tickets(created_by)")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_znuny_tickets_created_at ON znuny_tickets(created_at)")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_znuny_tickets_isp ON znuny_tickets(isp_ticket_id)")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_znuny_tickets_queue ON znuny_tickets(queue)")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_znuny_tickets_owner ON znuny_tickets(owner)")

            # --- All performance indexes (single consolidated block) ---

            # tickets table
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_tickets_portal ON tickets(portal)")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_tickets_status ON tickets(status)")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_tickets_created_at ON tickets(created_at)")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_tickets_completed_at ON tickets(completed_at)")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_tickets_in_znuny ON tickets(in_znuny)")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_tickets_znuny_created_by ON tickets(znuny_created_by)")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_tickets_znuny_created_at ON tickets(znuny_created_at)")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_tickets_portal_ticket ON tickets(portal, ticket_id)")
            # Composite index for sync queries (active + not in znuny)
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_tickets_active_sync ON tickets(completed_at, in_znuny)")
            # Composite index for detail sync queries
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_tickets_znuny_details ON tickets(in_znuny, znuny_ticket_id, znuny_created_by)")
            # Composite index for znuny_ticket_id lookups
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_tickets_znuny_ticket_id ON tickets(znuny_ticket_id)")

            # znuny_articles table
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_znuny_articles_ticket ON znuny_articles(ticket_id)")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_znuny_articles_created_by ON znuny_articles(created_by)")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_znuny_articles_created_at ON znuny_articles(created_at)")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_znuny_articles_znuny_ticket ON znuny_articles(znuny_ticket_id)")

            # site_visits table
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_site_visits_status ON site_visits(status)")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_site_visits_assigned_to ON site_visits(assigned_to)")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_site_visits_visit_date ON site_visits(visit_date)")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_site_visits_znuny_ticket ON site_visits(znuny_ticket_id)")
            # Composite for pending visit lookups
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_site_visits_pending ON site_visits(znuny_ticket_id, status)")

            # extraction_logs table
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_extraction_logs_portal ON extraction_logs(portal)")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_extraction_logs_extracted_at ON extraction_logs(extracted_at)")

            # login_stats table
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_login_stats_portal ON login_stats(portal)")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_login_stats_created_at ON login_stats(created_at)")

            # staff_performance_daily table
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_staff_performance_daily ON staff_performance_daily(staff_name, date)")

            # system_logs table
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_system_logs_created_at ON system_logs(created_at)")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_system_logs_level ON system_logs(level)")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_system_logs_source ON system_logs(source)")

            # Migration: Clean up duplicate site visits (same ticket+date, different articles)
            # Keep the row with assigned_to data, delete the empty one
            cursor.execute("""
                DELETE FROM site_visits WHERE id IN (
                    SELECT s1.id FROM site_visits s1
                    INNER JOIN site_visits s2
                        ON s1.znuny_ticket_id = s2.znuny_ticket_id
                        AND s1.visit_date = s2.visit_date
                        AND s1.article_id != s2.article_id
                    WHERE (s1.assigned_to IS NULL OR s1.assigned_to = '')
                      AND s2.assigned_to IS NOT NULL AND s2.assigned_to != ''
                )
            """)
            if cursor.rowcount > 0:
                logger.info(f"Cleaned up {cursor.rowcount} duplicate site visit rows")

            logger.info("Database initialized successfully")

    def upsert_ticket(self, ticket: Ticket) -> tuple[int, bool, bool]:
        """
        Insert or update a ticket.
        Returns: (ticket_id, is_new, is_updated)
        """
        try:
            return self._upsert_ticket_impl(ticket)
        except Exception as e:
            self._log_db_error("upsert_ticket", e, f"portal={ticket.portal}, ticket_id={ticket.ticket_id}")
            raise

    def _upsert_ticket_impl(self, ticket: Ticket) -> tuple[int, bool, bool]:
        """Internal implementation of upsert_ticket."""
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
                        portal_url = COALESCE(?, portal_url),
                        raw_dump = COALESCE(?, raw_dump),
                        updated_at = ?,
                        completed_at = ?
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
                    ticket.portal_url,
                    ticket.raw_dump,
                    now_maldives(),
                    ticket.completed_at,  # Set completed_at if provided
                    existing_id
                ))

                # Track note changes
                if ticket.notes and ticket.notes != existing_notes:
                    cursor.execute(
                        "INSERT INTO ticket_notes_history (ticket_id, note, recorded_at) VALUES (?, ?, ?)",
                        (existing_id, ticket.notes, now_maldives())
                    )
                    logger.debug(f"Note updated for ticket {ticket.ticket_id}")

                is_updated = cursor.rowcount > 0
                return existing_id, False, is_updated
            else:
                # Insert new ticket with explicit Maldives timestamps
                current_time = now_maldives()
                cursor.execute("""
                    INSERT INTO tickets (
                        portal, ticket_id, address, account, customer_name, ticket_type,
                        portal_created_at, service_type, status, kpi, notes, portal_url,
                        raw_dump, completed_at, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                    ticket.portal_url,
                    ticket.raw_dump,
                    ticket.completed_at,  # Set completed_at if provided (for closed tickets)
                    current_time,
                    current_time
                ))

                new_id = cursor.lastrowid

                # Record initial note if exists
                if ticket.notes:
                    cursor.execute(
                        "INSERT INTO ticket_notes_history (ticket_id, note, recorded_at) VALUES (?, ?, ?)",
                        (new_id, ticket.notes, current_time)
                    )

                logger.info(f"New ticket added: {ticket.portal}/{ticket.ticket_id}")
                return new_id, True, False

    def get_all_tickets(self, portal: str = None, include_completed: bool = False) -> list[Ticket]:
        with self._get_connection() as conn:
            cursor = conn.cursor()

            if portal:
                if include_completed:
                    cursor.execute(
                        "SELECT * FROM tickets WHERE portal = ? ORDER BY created_at DESC",
                        (portal,)
                    )
                else:
                    cursor.execute(
                        "SELECT * FROM tickets WHERE portal = ? AND completed_at IS NULL ORDER BY created_at DESC",
                        (portal,)
                    )
            else:
                if include_completed:
                    cursor.execute("SELECT * FROM tickets ORDER BY created_at DESC")
                else:
                    cursor.execute("SELECT * FROM tickets WHERE completed_at IS NULL ORDER BY created_at DESC")

            rows = cursor.fetchall()
            return [self._row_to_ticket(row) for row in rows]

    def get_tickets_filtered(
        self,
        portal: str = None,
        status: str = None,
        ticket_type: str = None,
        in_znuny: bool = None,
        staff: str = None,
        search: str = None,
        include_completed: bool = False,
        completed_only: bool = False,
        date_from: str = None,
        date_to: str = None,
        limit: int = 100,
        offset: int = 0
    ) -> tuple[list[Ticket], int]:
        """Get tickets with SQL-level filtering. Returns (tickets, total_count)."""
        with self._get_connection() as conn:
            cursor = conn.cursor()

            # Build WHERE clause dynamically
            conditions = []
            params = []

            # Completion status filter
            if completed_only:
                conditions.append("completed_at IS NOT NULL")
            elif not include_completed:
                conditions.append("completed_at IS NULL")

            if portal:
                conditions.append("portal = ?")
                params.append(portal)

            if status:
                conditions.append("status LIKE ?")
                params.append(f"%{status}%")

            if ticket_type:
                conditions.append("ticket_type LIKE ?")
                params.append(f"%{ticket_type}%")

            if in_znuny is not None:
                conditions.append("in_znuny = ?")
                params.append(1 if in_znuny else 0)

            if staff:
                conditions.append("LOWER(znuny_created_by) = LOWER(?)")
                params.append(staff)

            if search:
                conditions.append(
                    "(ticket_id LIKE ? OR customer_name LIKE ? OR address LIKE ? OR account LIKE ?)"
                )
                search_param = f"%{search}%"
                params.extend([search_param, search_param, search_param, search_param])

            if date_from:
                conditions.append("DATE(substr(created_at, 1, 19)) >= ?")
                params.append(date_from)

            if date_to:
                conditions.append("DATE(substr(created_at, 1, 19)) <= ?")
                params.append(date_to)

            # Build query
            where_clause = " AND ".join(conditions) if conditions else "1=1"

            # Get total count
            count_query = f"SELECT COUNT(*) FROM tickets WHERE {where_clause}"
            cursor.execute(count_query, params)
            total = cursor.fetchone()[0]

            # Get paginated results
            query = f"""
                SELECT * FROM tickets
                WHERE {where_clause}
                ORDER BY created_at DESC
                LIMIT ? OFFSET ?
            """
            cursor.execute(query, params + [limit, offset])
            rows = cursor.fetchall()

            return [self._row_to_ticket(row) for row in rows], total

    def get_ticket_by_id(self, ticket_id: int) -> Optional[Ticket]:
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM tickets WHERE id = ?", (ticket_id,))
            row = cursor.fetchone()
            return self._row_to_ticket(row) if row else None

    def get_ticket_by_portal_id(self, portal: str, portal_ticket_id: str) -> Optional[Ticket]:
        """Find a ticket by portal name and portal's ticket ID."""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT * FROM tickets WHERE portal = ? AND (ticket_id = ? OR account = ?)",
                (portal, portal_ticket_id, portal_ticket_id)
            )
            row = cursor.fetchone()
            return self._row_to_ticket(row) if row else None

    def get_unchecked_tickets(self) -> list[Ticket]:
        """Get active tickets that haven't been checked in Znuny yet (in_znuny=0, not completed)."""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT * FROM tickets
                WHERE in_znuny = 0 AND completed_at IS NULL
                ORDER BY created_at DESC
            """)
            return [self._row_to_ticket(row) for row in cursor.fetchall()]

    def get_completed_not_in_znuny(self, limit: int = 5) -> list[Ticket]:
        """Get completed tickets not found in Znuny that have account numbers.

        Excludes tickets already searched 3+ times (those get marked rejected).
        Capped at `limit` per cycle.
        """
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT * FROM tickets
                WHERE in_znuny = 0
                AND completed_at IS NOT NULL
                AND account IS NOT NULL AND account != ''
                AND COALESCE(znuny_search_count, 0) < 3
                ORDER BY completed_at DESC
                LIMIT ?
            """, (limit,))
            return [self._row_to_ticket(row) for row in cursor.fetchall()]

    def increment_znuny_search_count(self, ticket_id: int) -> int:
        """Increment the account search counter for a ticket. Returns new count."""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                UPDATE tickets
                SET znuny_search_count = COALESCE(znuny_search_count, 0) + 1,
                    updated_at = ?
                WHERE id = ?
            """, (now_maldives(), ticket_id))
            cursor.execute("SELECT znuny_search_count FROM tickets WHERE id = ?", (ticket_id,))
            row = cursor.fetchone()
            return row["znuny_search_count"] if row else 0

    def mark_ticket_rejected(self, ticket_id: int):
        """Mark a ticket as rejected on portal (not found in Znuny after 3 searches)."""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                UPDATE tickets
                SET status = 'Rejected on Portal',
                    updated_at = ?
                WHERE id = ?
            """, (now_maldives(), ticket_id))
            logger.info(f"Marked ticket {ticket_id} as rejected on portal")

    def get_sync_status_counts(self) -> dict:
        """Get Znuny sync status counts in a single query."""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT
                    COUNT(*) as total_active,
                    SUM(CASE WHEN in_znuny = 1 THEN 1 ELSE 0 END) as in_znuny,
                    SUM(CASE WHEN in_znuny = 0 THEN 1 ELSE 0 END) as not_in_znuny,
                    SUM(CASE WHEN in_znuny = 1 AND znuny_created_by IS NOT NULL AND znuny_created_by != '' THEN 1 ELSE 0 END) as with_details,
                    MAX(CASE WHEN in_znuny = 1 AND znuny_created_by IS NOT NULL THEN updated_at ELSE NULL END) as last_sync_time
                FROM tickets
                WHERE completed_at IS NULL
            """)
            row = cursor.fetchone()
            return {
                "total_active": row["total_active"] or 0,
                "in_znuny": row["in_znuny"] or 0,
                "not_in_znuny": row["not_in_znuny"] or 0,
                "with_details": row["with_details"] or 0,
                "needing_sync": (row["in_znuny"] or 0) - (row["with_details"] or 0),
                "last_sync_time": row["last_sync_time"]
            }

    def get_active_linked_isp_tickets(self) -> list[Ticket]:
        """Active ISP tickets already linked to Znuny (in_znuny=1, not completed).
        Used by the 1-minute quick check to refresh their latest Znuny comment."""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT * FROM tickets
                WHERE in_znuny = 1 AND znuny_ticket_id IS NOT NULL AND completed_at IS NULL
                ORDER BY updated_at DESC
            """)
            return [self._row_to_ticket(row) for row in cursor.fetchall()]

    def set_ticket_raw_dump(self, ticket_id: int, raw_dump: str):
        """Store the raw portal detail-page text for a ticket (for the formatter)."""
        with self._get_connection() as conn:
            conn.cursor().execute(
                "UPDATE tickets SET raw_dump = ?, updated_at = ? WHERE id = ?",
                (raw_dump, now_maldives(), ticket_id),
            )

    def get_isp_tickets_needing_dump(self, portal: str = None) -> list[Ticket]:
        """Active, not-in-Znuny ISP tickets missing a raw_dump — used to backfill
        formatter data for tickets captured before raw_dump existed."""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            q = ("SELECT * FROM tickets WHERE completed_at IS NULL AND in_znuny = 0 "
                 "AND (raw_dump IS NULL OR raw_dump = '')")
            params = []
            if portal:
                q += " AND portal = ?"
                params.append(portal)
            q += " ORDER BY created_at DESC"
            cursor.execute(q, params)
            return [self._row_to_ticket(row) for row in cursor.fetchall()]

    def get_tickets_needing_znuny_details(self) -> list[Ticket]:
        """Get tickets that have znuny_ticket_id but no znuny_created_by (need detail sync)."""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT * FROM tickets
                WHERE in_znuny = 1 AND znuny_ticket_id IS NOT NULL
                AND (znuny_created_by IS NULL OR znuny_created_by = '')
                AND completed_at IS NULL
            """)
            return [self._row_to_ticket(row) for row in cursor.fetchall()]

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
                             znuny_created_by: str = None, znuny_address: str = None,
                             znuny_url: str = None):
        """Update Znuny-specific details for a ticket."""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """UPDATE tickets SET
                    znuny_created_at = COALESCE(?, znuny_created_at),
                    znuny_created_by = COALESCE(?, znuny_created_by),
                    znuny_address = COALESCE(?, znuny_address),
                    znuny_url = COALESCE(?, znuny_url),
                    updated_at = ?
                WHERE id = ?""",
                (znuny_created_at, znuny_created_by, znuny_address, znuny_url, now_maldives(), ticket_id)
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

    def get_articles_filtered(self, date_from: str = None, date_to: str = None,
                              staff: str = None, limit: int = 100, offset: int = 0) -> dict:
        """Get articles with optional date and staff filters."""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            conditions = []
            params = []

            if date_from:
                conditions.append("DATE(substr(za.created_at, 1, 19)) >= ?")
                params.append(date_from)
            if date_to:
                conditions.append("DATE(substr(za.created_at, 1, 19)) <= ?")
                params.append(date_to)
            if staff:
                conditions.append("LOWER(za.created_by) = LOWER(?)")
                params.append(staff)

            where_clause = " AND ".join(conditions) if conditions else "1=1"

            # Count
            cursor.execute(f"SELECT COUNT(*) FROM znuny_articles za WHERE {where_clause}", params)
            total = cursor.fetchone()[0]

            # Get articles with ticket info
            cursor.execute(f"""
                SELECT za.*, t.portal, t.ticket_id as portal_ticket_id, t.address, t.customer_name
                FROM znuny_articles za
                LEFT JOIN tickets t ON za.ticket_id = t.id
                WHERE {where_clause}
                ORDER BY za.created_at DESC
                LIMIT ? OFFSET ?
            """, params + [limit, offset])

            articles = [dict(row) for row in cursor.fetchall()]
            return {"total": total, "articles": articles}

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
                date_filter_tickets += " AND DATE(substr(znuny_created_at, 1, 19)) >= ?"
                date_filter_articles += " AND DATE(substr(created_at, 1, 19)) >= ?"
                params_tickets.append(date_from)
                params_articles.append(date_from)
            if date_to:
                date_filter_tickets += " AND DATE(substr(znuny_created_at, 1, 19)) <= ?"
                date_filter_articles += " AND DATE(substr(created_at, 1, 19)) <= ?"
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

            # Combined articles query: get both articles_count and tickets_updated in one query
            cursor.execute(f"""
                SELECT created_by as staff,
                       COUNT(*) as articles_count,
                       COUNT(DISTINCT znuny_ticket_id) as tickets_updated
                FROM znuny_articles
                WHERE created_by IS NOT NULL AND created_by != ''
                {date_filter_articles}
                GROUP BY created_by
            """, params_articles)
            articles_by_staff = {}
            tickets_updated = {}
            for row in cursor.fetchall():
                articles_by_staff[row["staff"]] = row["articles_count"]
                tickets_updated[row["staff"]] = row["tickets_updated"]

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

    def touch_tickets_seen(self, portal: str, ticket_ids: list[str]) -> int:
        """Presence-only update: refresh updated_at for tickets still on the portal.

        Does NOT overwrite any other fields. Used when we deliberately skip
        re-extracting already-known tickets to reduce portal load (their content
        updates come from Znuny instead). Returns number of rows touched.
        """
        if not ticket_ids:
            return 0
        with self._get_connection() as conn:
            cursor = conn.cursor()
            placeholders = ",".join("?" * len(ticket_ids))
            cursor.execute(
                f"UPDATE tickets SET updated_at = ? WHERE portal = ? AND ticket_id IN ({placeholders})",
                [now_maldives(), portal, *ticket_ids]
            )
            return cursor.rowcount

    def get_portal_urls_for_tickets(self, portal: str, ticket_ids: list[str]) -> dict[str, str]:
        """Get portal_url for given ticket IDs. Returns {ticket_id: portal_url}."""
        if not ticket_ids:
            return {}
        with self._get_connection() as conn:
            cursor = conn.cursor()
            placeholders = ",".join("?" * len(ticket_ids))
            cursor.execute(
                f"SELECT ticket_id, portal_url FROM tickets WHERE portal = ? AND ticket_id IN ({placeholders})",
                [portal] + list(ticket_ids)
            )
            return {row["ticket_id"]: row["portal_url"] for row in cursor.fetchall() if row["portal_url"]}

    def update_ticket_notes_bulk(self, portal: str, notes_map: dict[str, str]) -> int:
        """Update notes for multiple tickets before marking them complete.

        Args:
            portal: Portal name
            notes_map: {ticket_id: notes_text}

        Returns: Number of tickets updated
        """
        if not notes_map:
            return 0
        with self._get_connection() as conn:
            cursor = conn.cursor()
            now = now_maldives()
            count = 0
            for ticket_id, notes in notes_map.items():
                cursor.execute(
                    "UPDATE tickets SET notes = ?, updated_at = ? WHERE portal = ? AND ticket_id = ?",
                    (notes, now, portal, ticket_id)
                )
                count += cursor.rowcount
            return count

    def mark_tickets_complete(self, portal: str, ticket_ids: list[str]) -> int:
        """Mark tickets as complete when they disappear from the portal."""
        if not ticket_ids:
            return 0

        with self._get_connection() as conn:
            cursor = conn.cursor()
            now = now_maldives()
            placeholders = ",".join("?" * len(ticket_ids))

            # Mark tickets as complete
            cursor.execute(f"""
                UPDATE tickets
                SET status = 'Complete', updated_at = ?, completed_at = ?
                WHERE portal = ? AND ticket_id IN ({placeholders}) AND status != 'Complete'
            """, [now, now, portal] + list(ticket_ids))
            count = cursor.rowcount

            # Note: Site visit completion is handled by Znuny sync (Step 1.6)
            # when the Znuny ticket itself closes, not when the ISP ticket disappears.

            if count > 0:
                logger.info(f"Marked {count} tickets as complete for {portal}")
            return count

    def log_extraction(self, portal: str, status: str, tickets_found: int = 0,
                       tickets_new: int = 0, tickets_updated: int = 0,
                       tickets_completed: int = 0, error_message: str = None):
        try:
            with self._get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute("""
                    INSERT INTO extraction_logs (portal, status, tickets_found, tickets_new, tickets_updated, error_message, extracted_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                """, (portal, status, tickets_found, tickets_new, tickets_updated, error_message, now_maldives()))
                logger.info(f"Logged extraction for {portal}: {status} (found={tickets_found}, new={tickets_new}, updated={tickets_updated}, completed={tickets_completed})")
        except Exception as e:
            # Log error but don't fail - extraction logging shouldn't break extraction
            logger.error(f"Failed to log extraction for {portal}: {type(e).__name__}: {e}")

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
            today = now_maldives().date().isoformat()

            # Single query to get all ticket stats at once
            # Combines: by_portal, total, completed, not_in_znuny, by_status, by_type,
            # today_extracted, today_znuny, open_in_znuny
            cursor.execute("""
                SELECT
                    portal,
                    status,
                    ticket_type,
                    in_znuny,
                    completed_at IS NOT NULL as is_completed,
                    DATE(substr(created_at, 1, 19)) = ? as is_today_extracted,
                    DATE(substr(znuny_created_at, 1, 19)) = ? as is_today_znuny,
                    DATE(substr(completed_at, 1, 19)) = ? as is_today_completed,
                    COUNT(*) as cnt
                FROM tickets
                GROUP BY portal, status, ticket_type, in_znuny, is_completed,
                         is_today_extracted, is_today_znuny, is_today_completed
            """, (today, today, today))

            by_portal = {}
            by_status = {}
            by_type = {}
            total = 0
            completed = 0
            not_in_znuny = 0
            open_in_znuny = 0
            today_extracted = {}
            today_extracted_total = 0
            today_znuny_entries = 0
            today_znuny_by_portal = {}
            today_completed = 0

            for row in cursor.fetchall():
                cnt = row["cnt"]
                portal = row["portal"]
                is_completed = row["is_completed"]
                status = row["status"] or "Unknown"
                ticket_type = row["ticket_type"] or "Unknown"
                in_znuny = row["in_znuny"]

                if is_completed:
                    completed += cnt
                    if row["is_today_completed"]:
                        today_completed += cnt
                else:
                    # Active ticket stats
                    total += cnt
                    by_portal[portal] = by_portal.get(portal, 0) + cnt
                    by_status[status] = by_status.get(status, 0) + cnt
                    by_type[ticket_type] = by_type.get(ticket_type, 0) + cnt
                    if not in_znuny:
                        not_in_znuny += cnt
                    else:
                        open_in_znuny += cnt

                # Today stats (apply to both active and completed)
                if row["is_today_extracted"]:
                    today_extracted[portal] = today_extracted.get(portal, 0) + cnt
                    today_extracted_total += cnt
                if row["is_today_znuny"]:
                    today_znuny_entries += cnt
                    today_znuny_by_portal[portal] = today_znuny_by_portal.get(portal, 0) + cnt

            # Single query for all site visit stats
            cursor.execute("""
                SELECT
                    SUM(CASE WHEN DATE(substr(created_at, 1, 19)) = ? THEN 1 ELSE 0 END) as today_created,
                    SUM(CASE WHEN status = 'completed' AND DATE(visit_date) = ? THEN 1 ELSE 0 END) as today_completed,
                    SUM(CASE WHEN status = 'pending' THEN 1 ELSE 0 END) as pending
                FROM site_visits
            """, (today, today))
            sv_row = cursor.fetchone()
            today_site_visits_created = sv_row["today_created"] or 0
            today_site_visits_completed = sv_row["today_completed"] or 0
            pending_site_visits = sv_row["pending"] or 0

            # Today's articles
            cursor.execute("""
                SELECT COUNT(*) as count FROM znuny_articles
                WHERE DATE(substr(created_at, 1, 19)) = ?
            """, (today,))
            today_articles_created = cursor.fetchone()["count"]

            # Last extraction per portal
            last_extraction = self.get_last_extraction_per_portal()

            return {
                "total": total,
                "completed": completed,
                "by_portal": by_portal,
                "not_in_znuny": not_in_znuny,
                "by_status": by_status,
                "by_type": by_type,
                "last_extraction": last_extraction,
                "open_in_znuny": open_in_znuny,
                "today_extracted": today_extracted,
                "today_extracted_total": today_extracted_total,
                "today_znuny_entries": today_znuny_entries,
                "today_znuny_by_portal": today_znuny_by_portal,
                "today_site_visits_created": today_site_visits_created,
                "today_site_visits_completed": today_site_visits_completed,
                "pending_site_visits": pending_site_visits,
                "today_articles_created": today_articles_created,
                "today_completed": today_completed
            }

    def log_login_event(self, portal: str, event_type: str, session_id: str = None,
                         success: bool = True, error_message: str = None):
        """Log a login-related event (login_attempt, login_success, login_failed, session_reused)."""
        try:
            with self._get_connection() as conn:
                cursor = conn.cursor()
                cursor.execute("""
                    INSERT INTO login_stats (portal, event_type, session_id, success, error_message, created_at)
                    VALUES (?, ?, ?, ?, ?, ?)
                """, (portal, event_type, session_id, success, error_message, now_maldives()))
                logger.info(f"Logged {event_type} for {portal}")
        except Exception as e:
            # Log error but don't fail - login logging shouldn't break extraction
            logger.error(f"Failed to log login event for {portal}: {type(e).__name__}: {e}")

    def get_login_stats(self, limit: int = 100) -> list[dict]:
        """Get recent login events."""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT * FROM login_stats ORDER BY created_at DESC LIMIT ?",
                (limit,)
            )
            return [dict(row) for row in cursor.fetchall()]

    def get_login_summary(self) -> dict:
        """Get login statistics summary per portal."""
        with self._get_connection() as conn:
            cursor = conn.cursor()

            # Total logins per portal
            cursor.execute("""
                SELECT portal,
                       SUM(CASE WHEN event_type = 'login_success' THEN 1 ELSE 0 END) as total_logins,
                       SUM(CASE WHEN event_type = 'login_failed' THEN 1 ELSE 0 END) as failed_logins,
                       SUM(CASE WHEN event_type = 'session_reused' THEN 1 ELSE 0 END) as sessions_reused
                FROM login_stats
                GROUP BY portal
            """)
            by_portal = {}
            for row in cursor.fetchall():
                by_portal[row["portal"]] = {
                    "total_logins": row["total_logins"],
                    "failed_logins": row["failed_logins"],
                    "sessions_reused": row["sessions_reused"]
                }

            # Last login/session event per portal
            cursor.execute("""
                SELECT l1.*
                FROM login_stats l1
                INNER JOIN (
                    SELECT portal, MAX(created_at) as max_time
                    FROM login_stats
                    GROUP BY portal
                ) l2 ON l1.portal = l2.portal AND l1.created_at = l2.max_time
            """)
            last_events = {}
            for row in cursor.fetchall():
                last_events[row["portal"]] = {
                    "event_type": row["event_type"],
                    "created_at": row["created_at"],
                    "success": bool(row["success"])
                }

            # Today's stats
            today = now_maldives().strftime("%Y-%m-%d")
            cursor.execute("""
                SELECT
                    SUM(CASE WHEN event_type = 'login_success' THEN 1 ELSE 0 END) as logins_today,
                    SUM(CASE WHEN event_type = 'session_reused' THEN 1 ELSE 0 END) as sessions_reused_today
                FROM login_stats
                WHERE DATE(substr(created_at, 1, 19)) = ?
            """, (today,))
            today_row = cursor.fetchone()

            return {
                "by_portal": by_portal,
                "last_events": last_events,
                "today": {
                    "logins": today_row["logins_today"] or 0,
                    "sessions_reused": today_row["sessions_reused_today"] or 0
                }
            }

    def get_staff_detailed_stats(self, date_from: str = None, date_to: str = None,
                                   thresholds: dict = None, exclude_negative: bool = True) -> dict:
        """Get detailed staff statistics including on-time percentages.

        Args:
            date_from: Optional start date filter (YYYY-MM-DD)
            date_to: Optional end date filter (YYYY-MM-DD)
            thresholds: Optional dict with 'good', 'warning', 'bad', 'critical' thresholds
            exclude_negative: If True (default), exclude tickets with negative time differences
                              (historical tickets where Znuny ticket existed before extractor)
        """
        # Use default thresholds if not provided
        if thresholds is None:
            thresholds = {"good": 5, "warning": 10, "bad": 30, "critical": 60}

        t_good = thresholds.get("good", 5)
        t_warning = thresholds.get("warning", 10)

        with self._get_connection() as conn:
            cursor = conn.cursor()

            # Build date filter
            date_filter = ""
            params = [t_good, t_good, t_warning, t_warning]
            if date_from:
                date_filter += " AND DATE(substr(znuny_created_at, 1, 19)) >= ?"
                params.append(date_from)
            if date_to:
                date_filter += " AND DATE(substr(znuny_created_at, 1, 19)) <= ?"
                params.append(date_to)

            # Add filter for negative time if exclude_negative is True
            negative_filter = ""
            if exclude_negative:
                negative_filter = " AND (julianday(substr(znuny_created_at, 1, 19)) - julianday(substr(created_at, 1, 19))) * 24 * 60 >= 0"

            # Get tickets with time-to-create calculation
            # Time diff = znuny_created_at - created_at (positive = Znuny created after extractor saw it)
            cursor.execute(f"""
                SELECT
                    znuny_created_by as staff,
                    COUNT(*) as total_tickets,
                    SUM(CASE WHEN
                        (julianday(substr(znuny_created_at, 1, 19)) - julianday(substr(created_at, 1, 19))) * 24 * 60 >= 0
                        AND (julianday(substr(znuny_created_at, 1, 19)) - julianday(substr(created_at, 1, 19))) * 24 * 60 <= ?
                        THEN 1 ELSE 0 END) as within_good,
                    SUM(CASE WHEN
                        (julianday(substr(znuny_created_at, 1, 19)) - julianday(substr(created_at, 1, 19))) * 24 * 60 > ?
                        AND (julianday(substr(znuny_created_at, 1, 19)) - julianday(substr(created_at, 1, 19))) * 24 * 60 <= ?
                        THEN 1 ELSE 0 END) as within_warning,
                    SUM(CASE WHEN
                        (julianday(substr(znuny_created_at, 1, 19)) - julianday(substr(created_at, 1, 19))) * 24 * 60 > ?
                        THEN 1 ELSE 0 END) as over_warning,
                    AVG((julianday(substr(znuny_created_at, 1, 19)) - julianday(substr(created_at, 1, 19))) * 24 * 60) as avg_minutes,
                    COUNT(*) as tickets_with_valid_time
                FROM tickets
                WHERE znuny_created_by IS NOT NULL
                    AND znuny_created_by != ''
                    AND znuny_created_at IS NOT NULL
                    AND created_at IS NOT NULL
                    {negative_filter}
                    {date_filter}
                GROUP BY znuny_created_by
            """, params)

            staff_tickets = {}
            for row in cursor.fetchall():
                valid_tickets = row["tickets_with_valid_time"] or 0
                staff_tickets[row["staff"]] = {
                    "tickets_created": row["total_tickets"],
                    "within_5min": row["within_good"] or 0,
                    "within_10min": row["within_warning"] or 0,
                    "over_10min": row["over_warning"] or 0,
                    "avg_minutes": round(row["avg_minutes"], 1) if row["avg_minutes"] else 0,
                    "on_time_pct": round((row["within_good"] or 0) / valid_tickets * 100, 1) if valid_tickets > 0 else 0
                }

            # Get articles count
            article_params = []
            article_date_filter = ""
            if date_from:
                article_date_filter += " AND DATE(substr(created_at, 1, 19)) >= ?"
                article_params.append(date_from)
            if date_to:
                article_date_filter += " AND DATE(substr(created_at, 1, 19)) <= ?"
                article_params.append(date_to)

            cursor.execute(f"""
                SELECT created_by as staff, COUNT(*) as articles_count,
                       COUNT(DISTINCT znuny_ticket_id) as tickets_updated
                FROM znuny_articles
                WHERE created_by IS NOT NULL AND created_by != ''
                {article_date_filter}
                GROUP BY created_by
            """, article_params)

            for row in cursor.fetchall():
                staff = row["staff"]
                if staff not in staff_tickets:
                    staff_tickets[staff] = {
                        "tickets_created": 0,
                        "within_5min": 0,
                        "within_10min": 0,
                        "over_10min": 0,
                        "avg_minutes": 0,
                        "on_time_pct": 0
                    }
                staff_tickets[staff]["articles_count"] = row["articles_count"]
                staff_tickets[staff]["tickets_updated"] = row["tickets_updated"]

            # Get Znuny-only tickets count per staff
            znuny_only_params = []
            znuny_only_date_filter = ""
            if date_from:
                znuny_only_date_filter += " AND DATE(substr(created_at, 1, 19)) >= ?"
                znuny_only_params.append(date_from)
            if date_to:
                znuny_only_date_filter += " AND DATE(substr(created_at, 1, 19)) <= ?"
                znuny_only_params.append(date_to)

            cursor.execute(f"""
                SELECT created_by as staff, COUNT(*) as znuny_only_count
                FROM znuny_tickets
                WHERE created_by IS NOT NULL AND created_by != ''
                    AND isp_ticket_id IS NULL
                {znuny_only_date_filter}
                GROUP BY created_by
            """, znuny_only_params)

            for row in cursor.fetchall():
                staff = row["staff"]
                if staff not in staff_tickets:
                    staff_tickets[staff] = {
                        "tickets_created": 0,
                        "within_5min": 0,
                        "within_10min": 0,
                        "over_10min": 0,
                        "avg_minutes": 0,
                        "on_time_pct": 0,
                        "articles_count": 0,
                        "tickets_updated": 0
                    }
                staff_tickets[staff]["znuny_only_count"] = row["znuny_only_count"]

            # Get site visits stats per staff
            site_visits_params = []
            site_visits_date_filter = ""
            if date_from:
                site_visits_date_filter += " AND DATE(visit_date) >= ?"
                site_visits_params.append(date_from)
            if date_to:
                site_visits_date_filter += " AND DATE(visit_date) <= ?"
                site_visits_params.append(date_to)

            cursor.execute(f"""
                SELECT
                    assigned_to as staff,
                    COUNT(*) as site_visits_total,
                    SUM(CASE WHEN status = 'completed' THEN 1 ELSE 0 END) as site_visits_completed,
                    AVG(CASE WHEN status = 'completed' AND time_taken_minutes IS NOT NULL
                        THEN time_taken_minutes ELSE NULL END) as avg_visit_time
                FROM site_visits
                WHERE assigned_to IS NOT NULL AND assigned_to != ''
                {site_visits_date_filter}
                GROUP BY assigned_to
            """, site_visits_params)

            for row in cursor.fetchall():
                # Split multi-staff assigned_to and attribute visit to each person
                names = [n.strip() for n in (row["staff"] or "").split(",") if n.strip()]
                for staff in names:
                    if staff not in staff_tickets:
                        staff_tickets[staff] = {
                            "tickets_created": 0,
                            "within_5min": 0,
                            "within_10min": 0,
                            "over_10min": 0,
                            "avg_minutes": 0,
                            "on_time_pct": 0,
                            "articles_count": 0,
                            "tickets_updated": 0,
                            "znuny_only_count": 0
                        }
                    prev_total = staff_tickets[staff].get("site_visits_total", 0)
                    prev_completed = staff_tickets[staff].get("site_visits_completed", 0)
                    prev_avg = staff_tickets[staff].get("avg_visit_time", 0)
                    new_total = row["site_visits_total"]
                    new_completed = row["site_visits_completed"] or 0
                    new_avg = round(row["avg_visit_time"], 1) if row["avg_visit_time"] else 0
                    combined_total = prev_total + new_total
                    # Weighted average of visit times
                    if prev_total and new_avg and prev_avg:
                        combined_avg = round((prev_avg * prev_total + new_avg * new_total) / combined_total, 1)
                    elif new_avg:
                        combined_avg = new_avg
                    else:
                        combined_avg = prev_avg
                    staff_tickets[staff]["site_visits_total"] = combined_total
                    staff_tickets[staff]["site_visits_completed"] = prev_completed + new_completed
                    staff_tickets[staff]["avg_visit_time"] = combined_avg

            # Build final list sorted by tickets created
            staff_list = []
            for name, stats in staff_tickets.items():
                staff_list.append({
                    "name": name,
                    "tickets_created": stats.get("tickets_created", 0),
                    "within_5min": stats.get("within_5min", 0),
                    "within_10min": stats.get("within_10min", 0),
                    "over_10min": stats.get("over_10min", 0),
                    "avg_minutes": stats.get("avg_minutes", 0),
                    "on_time_pct": stats.get("on_time_pct", 0),
                    "articles_count": stats.get("articles_count", 0),
                    "tickets_updated": stats.get("tickets_updated", 0),
                    "znuny_only_count": stats.get("znuny_only_count", 0),
                    "site_visits_total": stats.get("site_visits_total", 0),
                    "site_visits_completed": stats.get("site_visits_completed", 0),
                    "avg_visit_time": stats.get("avg_visit_time", 0)
                })

            staff_list.sort(key=lambda x: (-x["tickets_created"], -x["on_time_pct"]))

            return {
                "staff": staff_list,
                "total_tickets": sum(s["tickets_created"] for s in staff_list),
                "total_articles": sum(s["articles_count"] for s in staff_list),
                "total_znuny_only": sum(s["znuny_only_count"] for s in staff_list),
                "total_site_visits": sum(s["site_visits_total"] for s in staff_list),
                "total_site_visits_completed": sum(s["site_visits_completed"] for s in staff_list),
                "date_from": date_from,
                "date_to": date_to
            }

    def get_staff_summary(self, date_from: str = None, date_to: str = None) -> dict:
        """Clean per-staff summary for the staff page. Returns, per staff member
        within the optional date range:
          - tickets_created: all Znuny tickets they created (linked + orphan),
            counted from znuny_tickets by creator (no double counting)
          - articles_created: articles they authored (znuny_articles by author)
          - site_visits_total / site_visits_completed: visits assigned to them

        Each metric is scoped by its own natural date column (ticket created_at,
        article created_at, visit_date). Staff who appear in any one source are
        included."""
        with self._get_connection() as conn:
            cursor = conn.cursor()

            staff = {}

            def rec(name):
                if name not in staff:
                    staff[name] = {
                        "name": name,
                        "tickets_created": 0,
                        "articles_created": 0,
                        "site_visits_total": 0,
                        "site_visits_completed": 0,
                    }
                return staff[name]

            def dfilter(col):
                sql, p = "", []
                if date_from:
                    sql += f" AND DATE(substr({col}, 1, 19)) >= ?"
                    p.append(date_from)
                if date_to:
                    sql += f" AND DATE(substr({col}, 1, 19)) <= ?"
                    p.append(date_to)
                return sql, p

            # Tickets created (all Znuny tickets by creator). Scoped by created
            # OR closed in range so a ticket created earlier but closed in the
            # period (e.g. closed today) is still counted, attributed to its
            # creator.
            sql, p = self._znuny_date_scope(date_from, date_to)
            cursor.execute(f"""
                SELECT created_by AS name, COUNT(*) AS n
                FROM znuny_tickets
                WHERE created_by IS NOT NULL AND created_by != ''{sql}
                GROUP BY created_by
            """, p)
            for row in cursor.fetchall():
                rec(row["name"])["tickets_created"] = row["n"]

            # Articles created (by author)
            sql, p = dfilter("created_at")
            cursor.execute(f"""
                SELECT created_by AS name, COUNT(*) AS n
                FROM znuny_articles
                WHERE created_by IS NOT NULL AND created_by != ''{sql}
                GROUP BY created_by
            """, p)
            for row in cursor.fetchall():
                rec(row["name"])["articles_created"] = row["n"]

            # Site visits assigned (assigned_to may list several staff, comma-separated)
            sql, p = dfilter("visit_date")
            cursor.execute(f"""
                SELECT assigned_to AS name,
                       COUNT(*) AS total,
                       SUM(CASE WHEN status = 'completed' THEN 1 ELSE 0 END) AS completed
                FROM site_visits
                WHERE assigned_to IS NOT NULL AND assigned_to != ''{sql}
                GROUP BY assigned_to
            """, p)
            for row in cursor.fetchall():
                names = [n.strip() for n in (row["name"] or "").split(",") if n.strip()]
                for nm in names:
                    r = rec(nm)
                    r["site_visits_total"] += row["total"]
                    r["site_visits_completed"] += (row["completed"] or 0)

            result = list(staff.values())
            result.sort(key=lambda x: (x["tickets_created"], x["articles_created"], x["site_visits_total"]), reverse=True)

            # Distinct site-visit totals for the summary card: a visit shared by
            # several staff counts once here, even though it appears in each
            # assignee's per-staff row. (Tickets/articles have a single
            # creator/author, so their totals equal the per-staff sums.)
            sv_sql, sv_p = dfilter("visit_date")
            cursor.execute(f"""
                SELECT COUNT(*) AS total,
                       SUM(CASE WHEN status = 'completed' THEN 1 ELSE 0 END) AS completed
                FROM site_visits
                WHERE assigned_to IS NOT NULL AND assigned_to != ''{sv_sql}
            """, sv_p)
            sv = cursor.fetchone()

            return {
                "staff": result,
                "total_tickets": sum(s["tickets_created"] for s in result),
                "total_articles": sum(s["articles_created"] for s in result),
                "total_site_visits": sv["total"] or 0,
                "total_site_visits_completed": sv["completed"] or 0,
                "total_staff": len(result),
                "date_from": date_from,
                "date_to": date_to,
            }

    @staticmethod
    def _compute_staff_day(cursor, date_str: str) -> dict:
        """Compute per-staff activity for a single day, attributed to that day:
        tickets CREATED that day, articles authored that day, site visits assigned
        for that visit_date. Returns {name: {tickets, articles, sv_total, sv_done}}.
        Shared by snapshot generation and the live "today" path so both agree."""
        agg = {}

        def rec(name):
            if name not in agg:
                agg[name] = {"tickets": 0, "articles": 0, "sv_total": 0, "sv_done": 0}
            return agg[name]

        cursor.execute("""
            SELECT created_by AS name, COUNT(*) AS n FROM znuny_tickets
            WHERE created_by IS NOT NULL AND created_by != '' AND DATE(substr(created_at, 1, 19)) = ?
            GROUP BY created_by
        """, (date_str,))
        for row in cursor.fetchall():
            rec(row["name"])["tickets"] = row["n"]

        cursor.execute("""
            SELECT created_by AS name, COUNT(*) AS n FROM znuny_articles
            WHERE created_by IS NOT NULL AND created_by != '' AND DATE(substr(created_at, 1, 19)) = ?
            GROUP BY created_by
        """, (date_str,))
        for row in cursor.fetchall():
            rec(row["name"])["articles"] = row["n"]

        cursor.execute("""
            SELECT assigned_to AS name,
                   COUNT(*) AS total,
                   SUM(CASE WHEN status = 'completed' THEN 1 ELSE 0 END) AS completed
            FROM site_visits
            WHERE assigned_to IS NOT NULL AND assigned_to != '' AND DATE(visit_date) = ?
            GROUP BY assigned_to
        """, (date_str,))
        for row in cursor.fetchall():
            for nm in [n.strip() for n in (row["name"] or "").split(",") if n.strip()]:
                r = rec(nm)
                r["sv_total"] += row["total"]
                r["sv_done"] += (row["completed"] or 0)

        return agg

    def cleanup_site_visit_assignees(self) -> dict:
        """One-time fix: strip '[n]' list markers (e.g. "[1] @raidh [2] @ayan")
        from site_visits.assigned_to — these created a bogus "[1]" staff member.
        Then drop bogus snapshot rows and rebuild the affected days so the real
        assignees get credited. Idempotent — safe to run more than once."""
        import re

        def _clean(raw: str) -> str:
            names = []
            for part in re.split(r'[,@]', raw or ''):
                c = re.sub(r'\[\s*\d+\s*\]', ' ', part)   # remove [1], [2], ...
                c = re.sub(r'\s+', ' ', c).strip(' .,;:-')
                if c and not c.isdigit():
                    names.append(c)
            return ", ".join(names)

        fixed = []
        affected_dates = set()
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT id, assigned_to, visit_date FROM site_visits WHERE assigned_to LIKE '%[%'")
            for r in cursor.fetchall():
                new = _clean(r["assigned_to"])
                if new != (r["assigned_to"] or ""):
                    cursor.execute(
                        "UPDATE site_visits SET assigned_to = ?, updated_at = ? WHERE id = ?",
                        (new, now_maldives(), r["id"])
                    )
                    fixed.append({"id": r["id"], "old": r["assigned_to"], "new": new})
                    if r["visit_date"]:
                        affected_dates.add(str(r["visit_date"])[:10])
            # Drop any leftover bogus snapshot rows whose staff name is a marker.
            cursor.execute("DELETE FROM staff_performance_daily WHERE staff_name LIKE '%[%'")
            snap_deleted = cursor.rowcount

        # Rebuild affected days from the cleaned data (own connection; runs after
        # the UPDATE above has committed).
        for d in sorted(affected_dates):
            try:
                self.generate_staff_daily_snapshot(d)
            except Exception:
                pass

        return {
            "rows_fixed": len(fixed),
            "details": fixed,
            "snapshot_rows_deleted": snap_deleted,
            "dates_regenerated": sorted(affected_dates),
        }

    def generate_staff_daily_snapshot(self, date_str: str) -> int:
        """(Re)generate the per-staff snapshot rows in staff_performance_daily for
        a single day. Replaces any existing rows for the day. Returns staff count."""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            now = now_maldives()
            agg = self._compute_staff_day(cursor, date_str)

            # Replace the day's rows so staff who dropped to zero don't linger
            cursor.execute("DELETE FROM staff_performance_daily WHERE date = ?", (date_str,))
            for name, v in agg.items():
                cursor.execute("""
                    INSERT INTO staff_performance_daily
                        (staff_name, date, tickets_created, total_articles,
                         site_visits_total, site_visits_completed, calculated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                """, (name, date_str, v["tickets"], v["articles"],
                      v["sv_total"], v["sv_done"], now))
            logger.info(f"Staff daily snapshot for {date_str}: {len(agg)} staff")
            return len(agg)

    def get_staff_activity_dates(self) -> list:
        """Distinct dates (YYYY-MM-DD) on which any staff activity exists, for backfill."""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT DISTINCT DATE(substr(created_at, 1, 19)) d FROM znuny_tickets WHERE created_at IS NOT NULL
                UNION SELECT DISTINCT DATE(substr(created_at, 1, 19)) FROM znuny_articles WHERE created_at IS NOT NULL
                UNION SELECT DISTINCT DATE(visit_date) FROM site_visits WHERE visit_date IS NOT NULL
            """)
            return sorted(d for (d,) in cursor.fetchall() if d)

    def get_staff_summary_snapshot(self, date_from: str = None, date_to: str = None) -> dict:
        """Per-staff summary for the staff page. Hybrid by design:
          - prior days (before today) are read from the generated daily snapshots
            (staff_performance_daily) — cheap and stable
          - TODAY is computed live from the source tables, so it reflects activity
            since the last hourly snapshot

        Both halves use the same per-day attribution (tickets created that day,
        articles authored that day, visits assigned that day), so a multi-day
        range is the additive sum and never double-counts today."""
        today = now_maldives().date().isoformat()
        # Snapshots cover strictly-before-today; cap date_to accordingly.
        snap_to = date_to
        if snap_to is None or snap_to >= today:
            snap_to = (now_maldives().date() - timedelta(days=1)).isoformat()
        include_today = (
            (date_to is None or date_to >= today) and
            (date_from is None or date_from <= today)
        )

        with self._get_connection() as conn:
            cursor = conn.cursor()

            agg = {}

            def rec(name):
                if name not in agg:
                    agg[name] = {
                        "name": name,
                        "tickets_created": 0,
                        "articles_created": 0,
                        "site_visits_total": 0,
                        "site_visits_completed": 0,
                    }
                return agg[name]

            # Prior days from snapshots (only if the range starts on/before snap_to)
            if date_from is None or date_from <= snap_to:
                where, params = "WHERE date <= ?", [snap_to]
                if date_from:
                    where += " AND date >= ?"
                    params.append(date_from)
                cursor.execute(f"""
                    SELECT staff_name AS name,
                           SUM(tickets_created) AS tickets_created,
                           SUM(total_articles) AS articles_created,
                           SUM(site_visits_total) AS site_visits_total,
                           SUM(site_visits_completed) AS site_visits_completed
                    FROM staff_performance_daily
                    {where}
                    GROUP BY staff_name
                """, params)
                for row in cursor.fetchall():
                    r = rec(row["name"])
                    r["tickets_created"] += row["tickets_created"] or 0
                    r["articles_created"] += row["articles_created"] or 0
                    r["site_visits_total"] += row["site_visits_total"] or 0
                    r["site_visits_completed"] += row["site_visits_completed"] or 0

            # Today computed live
            if include_today:
                for name, v in self._compute_staff_day(cursor, today).items():
                    r = rec(name)
                    r["tickets_created"] += v["tickets"]
                    r["articles_created"] += v["articles"]
                    r["site_visits_total"] += v["sv_total"]
                    r["site_visits_completed"] += v["sv_done"]

            result = [s for s in agg.values()
                      if s["tickets_created"] or s["articles_created"] or s["site_visits_total"]]
            result.sort(key=lambda x: (x["tickets_created"], x["articles_created"], x["site_visits_total"]),
                        reverse=True)

            return {
                "staff": result,
                "total_tickets": sum(s["tickets_created"] for s in result),
                "total_articles": sum(s["articles_created"] for s in result),
                "total_site_visits": sum(s["site_visits_total"] for s in result),
                "total_site_visits_completed": sum(s["site_visits_completed"] for s in result),
                "total_staff": len(result),
                "date_from": date_from,
                "date_to": date_to,
            }

    def get_staff_daily_breakdown(self, staff_name: str, date_from: str = None, date_to: str = None) -> list:
        """Per-day Tickets/Articles/Site-visits for one staff member: prior days
        from snapshots, today live. Newest day first."""
        today = now_maldives().date().isoformat()
        snap_to = date_to
        if snap_to is None or snap_to >= today:
            snap_to = (now_maldives().date() - timedelta(days=1)).isoformat()
        include_today = (
            (date_to is None or date_to >= today) and
            (date_from is None or date_from <= today)
        )
        rows = {}
        with self._get_connection() as conn:
            cursor = conn.cursor()
            if date_from is None or date_from <= snap_to:
                where, params = "WHERE staff_name = ? AND date <= ?", [staff_name, snap_to]
                if date_from:
                    where += " AND date >= ?"
                    params.append(date_from)
                cursor.execute(f"""
                    SELECT date, tickets_created, total_articles,
                           site_visits_total, site_visits_completed
                    FROM staff_performance_daily {where}
                """, params)
                for r in cursor.fetchall():
                    rows[r["date"]] = {
                        "date": r["date"],
                        "tickets_created": r["tickets_created"] or 0,
                        "articles_created": r["total_articles"] or 0,
                        "site_visits_total": r["site_visits_total"] or 0,
                        "site_visits_completed": r["site_visits_completed"] or 0,
                    }
            if include_today:
                v = self._compute_staff_day(cursor, today).get(staff_name)
                if v:
                    rows[today] = {
                        "date": today,
                        "tickets_created": v["tickets"],
                        "articles_created": v["articles"],
                        "site_visits_total": v["sv_total"],
                        "site_visits_completed": v["sv_done"],
                    }
        return sorted(rows.values(), key=lambda x: x["date"], reverse=True)

    def get_staff_tickets(self, staff_name: str, date_from: str = None, date_to: str = None,
                          limit: int = 100, offset: int = 0) -> dict:
        """Get all tickets created by a specific staff member."""
        with self._get_connection() as conn:
            cursor = conn.cursor()

            # Build query with calculated time_to_create_minutes
            query = """
                SELECT *,
                    CASE WHEN znuny_created_at IS NOT NULL AND created_at IS NOT NULL
                         THEN (julianday(substr(znuny_created_at, 1, 19)) - julianday(substr(created_at, 1, 19))) * 24 * 60
                         ELSE NULL END as time_to_create_minutes
                FROM tickets
                WHERE znuny_created_by = ?
            """
            params = [staff_name]

            if date_from:
                query += " AND DATE(substr(znuny_created_at, 1, 19)) >= ?"
                params.append(date_from)
            if date_to:
                query += " AND DATE(substr(znuny_created_at, 1, 19)) <= ?"
                params.append(date_to)

            # Get total count
            count_query = query.replace("SELECT *,", "SELECT COUNT(*) FROM (SELECT *,").replace("FROM tickets", "FROM tickets) sub")
            cursor.execute(count_query.split("FROM (SELECT")[0] + " FROM tickets WHERE znuny_created_by = ?" +
                          (" AND DATE(substr(znuny_created_at, 1, 19)) >= ?" if date_from else "") +
                          (" AND DATE(substr(znuny_created_at, 1, 19)) <= ?" if date_to else ""),
                          params[:len([p for p in [staff_name, date_from, date_to] if p])])
            total = cursor.fetchone()[0]

            # Get tickets with pagination
            query += " ORDER BY znuny_created_at DESC LIMIT ? OFFSET ?"
            params.extend([limit, offset])
            cursor.execute(query, params)

            # Convert rows to tickets with time_to_create_minutes
            tickets = []
            for row in cursor.fetchall():
                ticket = self._row_to_ticket(row)
                # Add pre-calculated time_to_create_minutes
                time_minutes = row["time_to_create_minutes"]
                ticket.time_to_create_minutes = round(time_minutes, 1) if time_minutes is not None else None
                tickets.append(ticket)

            return {
                "staff_name": staff_name,
                "total": total,
                "tickets": tickets
            }

    def get_staff_performance_trend(self, staff_name: str, days: int = 30, thresholds: dict = None) -> list:
        """Get daily performance trend for a staff member."""
        # Use default threshold if not provided
        if thresholds is None:
            thresholds = self.get_performance_thresholds()
        t_good = thresholds.get("good", 5)

        with self._get_connection() as conn:
            cursor = conn.cursor()

            cursor.execute("""
                SELECT
                    DATE(substr(znuny_created_at, 1, 19)) as date,
                    COUNT(*) as tickets_created,
                    SUM(CASE WHEN
                        (julianday(substr(znuny_created_at, 1, 19)) - julianday(substr(created_at, 1, 19))) * 24 * 60 >= 0
                        AND (julianday(substr(znuny_created_at, 1, 19)) - julianday(substr(created_at, 1, 19))) * 24 * 60 <= ?
                        THEN 1 ELSE 0 END) as within_good,
                    AVG(CASE WHEN
                        (julianday(substr(znuny_created_at, 1, 19)) - julianday(substr(created_at, 1, 19))) * 24 * 60 >= 0
                        THEN (julianday(substr(znuny_created_at, 1, 19)) - julianday(substr(created_at, 1, 19))) * 24 * 60
                        ELSE NULL END) as avg_minutes,
                    SUM(CASE WHEN
                        (julianday(substr(znuny_created_at, 1, 19)) - julianday(substr(created_at, 1, 19))) * 24 * 60 >= 0
                        THEN 1 ELSE 0 END) as valid_tickets
                FROM tickets
                WHERE znuny_created_by = ?
                    AND znuny_created_at IS NOT NULL
                    AND created_at IS NOT NULL
                    AND DATE(substr(znuny_created_at, 1, 19)) >= DATE('now', ? || ' days')
                GROUP BY DATE(substr(znuny_created_at, 1, 19))
                ORDER BY date DESC
            """, (t_good, staff_name, -days))

            results = []
            for row in cursor.fetchall():
                valid = row["valid_tickets"] or 0
                results.append({
                    "date": row["date"],
                    "tickets_created": row["tickets_created"],
                    "within_5min": row["within_good"] or 0,
                    "on_time_pct": round((row["within_good"] or 0) / valid * 100, 1) if valid > 0 else 0,
                    "avg_minutes": round(row["avg_minutes"], 1) if row["avg_minutes"] else 0
                })
            return results

    def get_delayed_tickets_by_staff(self, min_delay_minutes: int = 5,
                                      date_from: str = None, date_to: str = None) -> list:
        """Get all delayed tickets grouped by staff."""
        with self._get_connection() as conn:
            cursor = conn.cursor()

            query = """
                SELECT
                    znuny_created_by as staff,
                    COUNT(*) as delayed_count,
                    AVG((julianday(substr(znuny_created_at, 1, 19)) - julianday(substr(created_at, 1, 19))) * 24 * 60) as avg_delay,
                    MAX((julianday(substr(znuny_created_at, 1, 19)) - julianday(substr(created_at, 1, 19))) * 24 * 60) as max_delay
                FROM tickets
                WHERE znuny_created_by IS NOT NULL
                    AND znuny_created_by != ''
                    AND znuny_created_at IS NOT NULL
                    AND created_at IS NOT NULL
                    AND (julianday(substr(znuny_created_at, 1, 19)) - julianday(substr(created_at, 1, 19))) * 24 * 60 > ?
            """
            params = [min_delay_minutes]

            if date_from:
                query += " AND DATE(substr(znuny_created_at, 1, 19)) >= ?"
                params.append(date_from)
            if date_to:
                query += " AND DATE(substr(znuny_created_at, 1, 19)) <= ?"
                params.append(date_to)

            query += " GROUP BY znuny_created_by ORDER BY delayed_count DESC"
            cursor.execute(query, params)

            return [{
                "staff": row["staff"],
                "delayed_count": row["delayed_count"],
                "avg_delay_minutes": round(row["avg_delay"], 1) if row["avg_delay"] else 0,
                "max_delay_minutes": round(row["max_delay"], 1) if row["max_delay"] else 0
            } for row in cursor.fetchall()]

    def get_all_staff_names(self) -> list:
        """Get list of all unique staff names."""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT DISTINCT znuny_created_by as staff FROM tickets
                WHERE znuny_created_by IS NOT NULL AND znuny_created_by != ''
                UNION
                SELECT DISTINCT created_by as staff FROM znuny_articles
                WHERE created_by IS NOT NULL AND created_by != ''
                ORDER BY staff
            """)
            return [row["staff"] for row in cursor.fetchall()]

    def export_staff_stats_csv(self, date_from: str = None, date_to: str = None) -> str:
        """Export staff statistics as CSV string."""
        stats = self.get_staff_detailed_stats(date_from, date_to)

        lines = ["Staff Name,Tickets Created,Within 5min,Within 10min,Over 10min,On Time %,Avg Minutes,Articles,Tickets Updated"]
        for s in stats["staff"]:
            lines.append(f"{s['name']},{s['tickets_created']},{s['within_5min']},{s['within_10min']},{s['over_10min']},{s['on_time_pct']},{s['avg_minutes']},{s['articles_count']},{s['tickets_updated']}")

        return "\n".join(lines)

    # ==================== Site Visits ====================

    def upsert_site_visit(self, znuny_ticket_id: str, article_id: int, site_type: str,
                          service_provider: str, scheduled_time: str, assigned_to: str,
                          visit_date: str, article_created_at: datetime,
                          ticket_id: int = None, znuny_url: str = None,
                          address: str = None, customer_name: str = None) -> int:
        """Insert or update a site visit record.

        Dedup: if a visit already exists for the same ticket+date from a different
        article that has richer data (non-empty assigned_to), skip this insert.
        """
        now = now_maldives()
        with self._get_connection() as conn:
            # Dedup check: skip if a better record already exists for same ticket+date
            if visit_date:
                cursor = conn.cursor()
                cursor.execute("""
                    SELECT id FROM site_visits
                    WHERE znuny_ticket_id = ? AND visit_date = ? AND article_id != ?
                      AND assigned_to IS NOT NULL AND assigned_to != ''
                """, (znuny_ticket_id, visit_date, article_id))
                if cursor.fetchone():
                    # A richer visit record already exists for this ticket+date
                    return 0
            cursor = conn.cursor()
            cursor.execute("""
                INSERT INTO site_visits
                    (ticket_id, znuny_ticket_id, article_id, site_type, service_provider,
                     scheduled_time, assigned_to, visit_date, article_created_at, znuny_url,
                     address, customer_name, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(znuny_ticket_id, article_id) DO UPDATE SET
                    site_type = excluded.site_type,
                    service_provider = excluded.service_provider,
                    scheduled_time = excluded.scheduled_time,
                    assigned_to = excluded.assigned_to,
                    visit_date = excluded.visit_date,
                    article_created_at = excluded.article_created_at,
                    znuny_url = excluded.znuny_url,
                    address = excluded.address,
                    customer_name = excluded.customer_name,
                    updated_at = excluded.updated_at
            """, (ticket_id, znuny_ticket_id, article_id, site_type, service_provider,
                  scheduled_time, assigned_to, visit_date, article_created_at, znuny_url,
                  address, customer_name, now, now))
            return cursor.lastrowid

    def update_site_visit_completion(self, znuny_ticket_id: str, completed_at: datetime):
        """Update site visits when ticket is completed."""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            # Calculate duration from scheduled visit time to completion time
            # Duration = completed_at - (visit_date + scheduled_time)
            cursor.execute(f"""
                UPDATE site_visits
                SET ticket_completed_at = ?,
                    status = 'completed',
                    time_taken_minutes = {_DURATION_SQL},
                    updated_at = ?
                WHERE znuny_ticket_id = ? AND status = 'pending'
            """, (completed_at, completed_at, now_maldives(), znuny_ticket_id))

    def complete_site_visit_by_followup(self, znuny_ticket_id: str, article_id: int,
                                         followup_article_time: datetime):
        """Mark a site visit as completed when a follow-up article is found."""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            # Calculate duration from scheduled visit time to followup article time
            # Duration = followup_time - (visit_date + scheduled_time)
            cursor.execute(f"""
                UPDATE site_visits
                SET ticket_completed_at = ?,
                    status = 'completed',
                    time_taken_minutes = {_DURATION_SQL},
                    updated_at = ?
                WHERE znuny_ticket_id = ? AND article_id = ? AND status = 'pending'
            """, (followup_article_time, followup_article_time, now_maldives(),
                  znuny_ticket_id, article_id))
            return cursor.rowcount > 0

    def get_pending_site_visits_for_ticket(self, znuny_ticket_id: str) -> list:
        """Get pending site visits for a Znuny ticket."""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT id, article_id, article_created_at, assigned_to
                FROM site_visits
                WHERE znuny_ticket_id = ? AND status = 'pending'
            """, (znuny_ticket_id,))
            return [dict(row) for row in cursor.fetchall()]

    def has_pending_site_visits(self, znuny_ticket_id: str) -> bool:
        """Check if a Znuny ticket has any pending site visits."""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT 1 FROM site_visits
                WHERE znuny_ticket_id = ? AND status = 'pending'
                LIMIT 1
            """, (znuny_ticket_id,))
            return cursor.fetchone() is not None

    def get_znuny_ids_with_pending_visits(self) -> set:
        """Get set of Znuny ticket IDs that have pending site visits."""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT DISTINCT znuny_ticket_id FROM site_visits
                WHERE znuny_ticket_id IS NOT NULL AND status = 'pending'
            """)
            return {row[0] for row in cursor.fetchall()}

    def complete_site_visits_for_closed_ticket(self, znuny_ticket_id: str,
                                                completed_at: datetime = None) -> int:
        """Mark all pending site visits as completed when Znuny ticket is closed.

        Args:
            znuny_ticket_id: The Znuny ticket ID
            completed_at: Optional completion time (e.g., last article time).
                         If not provided, uses current time.
        """
        with self._get_connection() as conn:
            cursor = conn.cursor()
            complete_time = completed_at if completed_at else now_maldives()
            now = now_maldives()
            # Calculate duration from scheduled visit time to completion time
            # Duration = complete_time - (visit_date + scheduled_time)
            cursor.execute(f"""
                UPDATE site_visits
                SET status = 'completed',
                    ticket_completed_at = ?,
                    time_taken_minutes = {_DURATION_SQL},
                    updated_at = ?
                WHERE znuny_ticket_id = ? AND status = 'pending'
            """, (complete_time, complete_time, now, znuny_ticket_id))
            count = cursor.rowcount
            if count > 0:
                logger.info(f"Marked {count} site visits as completed for closed ticket {znuny_ticket_id}")
            return count

    def get_synced_znuny_ticket_ids(self) -> set:
        """
        Get set of ALL Znuny ticket IDs that have been processed.
        Includes tickets with site visits, stored articles, or captured as znuny-only.
        This is used to skip re-processing tickets during sync.
        """
        with self._get_connection() as conn:
            cursor = conn.cursor()
            # Combine all sources of processed Znuny tickets
            cursor.execute("""
                SELECT DISTINCT znuny_ticket_id FROM site_visits
                WHERE znuny_ticket_id IS NOT NULL
                UNION
                SELECT DISTINCT znuny_ticket_id FROM znuny_articles
                WHERE znuny_ticket_id IS NOT NULL
                UNION
                SELECT DISTINCT znuny_ticket_id FROM znuny_tickets
                WHERE znuny_ticket_id IS NOT NULL
                UNION
                SELECT DISTINCT znuny_ticket_id FROM tickets
                WHERE znuny_ticket_id IS NOT NULL AND znuny_created_by IS NOT NULL
            """)
            return {row[0] for row in cursor.fetchall()}

    def get_known_article_counts(self) -> dict:
        """
        Get known article counts per Znuny ticket for change detection.
        Returns {znuny_ticket_id: max_article_number} from znuny_articles table.
        """
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT znuny_ticket_id, COUNT(*) as cnt, MAX(article_number) as max_num
                FROM znuny_articles
                WHERE znuny_ticket_id IS NOT NULL
                GROUP BY znuny_ticket_id
            """)
            return {row["znuny_ticket_id"]: {"count": row["cnt"], "max_num": row["max_num"]}
                    for row in cursor.fetchall()}

    def get_site_visits_for_ticket(self, ticket_id: int) -> list:
        """Get all site visits for a ticket (by internal ticket_id)."""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT * FROM site_visits
                WHERE ticket_id = ?
                ORDER BY visit_date DESC, scheduled_time DESC
            """, (ticket_id,))
            return [{
                "id": row["id"],
                "znuny_ticket_id": row["znuny_ticket_id"],
                "article_id": row["article_id"],
                "site_type": row["site_type"],
                "service_provider": row["service_provider"],
                "scheduled_time": row["scheduled_time"],
                "assigned_to": row["assigned_to"],
                "visit_date": row["visit_date"],
                "article_created_at": row["article_created_at"],
                "ticket_completed_at": row["ticket_completed_at"],
                "time_taken_minutes": row["time_taken_minutes"],
                "status": row["status"],
                "znuny_url": row["znuny_url"],
                "address": row["address"],
                "customer_name": row["customer_name"]
            } for row in cursor.fetchall()]

    def get_site_visits(self, date_from: str = None, date_to: str = None,
                        assigned_to: str = None, status: str = None,
                        limit: int = 100, offset: int = 0) -> dict:
        """Get site visits with optional filters."""
        with self._get_connection() as conn:
            cursor = conn.cursor()

            query = """
                SELECT sv.*, t.portal, t.address as ticket_address, t.customer_name as ticket_customer_name, t.ticket_id as portal_ticket_id
                FROM site_visits sv
                LEFT JOIN tickets t ON sv.ticket_id = t.id
                WHERE 1=1
            """
            params = []

            if date_from:
                query += " AND DATE(sv.visit_date) >= ?"
                params.append(date_from)
            if date_to:
                query += " AND DATE(sv.visit_date) <= ?"
                params.append(date_to)
            if assigned_to:
                # Match individual staff within comma-separated assigned_to
                query += " AND (sv.assigned_to = ? OR sv.assigned_to LIKE ? OR sv.assigned_to LIKE ? OR sv.assigned_to LIKE ?)"
                params.extend([assigned_to, f"{assigned_to}, %", f"%, {assigned_to}", f"%, {assigned_to}, %"])
            if status:
                query += " AND sv.status = ?"
                params.append(status)

            # Get total count
            count_query = query.replace("SELECT sv.*, t.portal, t.address as ticket_address, t.customer_name as ticket_customer_name, t.ticket_id as portal_ticket_id", "SELECT COUNT(*)")
            cursor.execute(count_query, params)
            total = cursor.fetchone()[0]

            # Get visits with pagination
            query += " ORDER BY sv.visit_date DESC, sv.scheduled_time DESC LIMIT ? OFFSET ?"
            params.extend([limit, offset])
            cursor.execute(query, params)

            visits = []
            for row in cursor.fetchall():
                visits.append({
                    "id": row["id"],
                    "ticket_id": row["ticket_id"],
                    "znuny_ticket_id": row["znuny_ticket_id"],
                    "article_id": row["article_id"],
                    "site_type": row["site_type"],
                    "service_provider": row["service_provider"],
                    "scheduled_time": row["scheduled_time"],
                    "assigned_to": row["assigned_to"],
                    "visit_date": row["visit_date"],
                    "article_created_at": row["article_created_at"],
                    "ticket_completed_at": row["ticket_completed_at"],
                    "time_taken_minutes": row["time_taken_minutes"],
                    "status": row["status"],
                    "znuny_url": row["znuny_url"],
                    "address": row["address"],
                    "customer_name": row["customer_name"],
                    "portal": row["portal"],
                    "ticket_address": row["ticket_address"],
                    "ticket_customer_name": row["ticket_customer_name"],
                    "portal_ticket_id": row["portal_ticket_id"],
                    "created_at": row["created_at"]
                })

            return {"total": total, "visits": visits}

    def get_site_visit_staff_stats(self, date_from: str = None, date_to: str = None) -> list:
        """Get site visit statistics by assigned staff."""
        with self._get_connection() as conn:
            cursor = conn.cursor()

            query = """
                SELECT
                    assigned_to,
                    COUNT(*) as total_visits,
                    SUM(CASE WHEN status = 'completed' THEN 1 ELSE 0 END) as completed,
                    SUM(CASE WHEN status = 'pending' THEN 1 ELSE 0 END) as pending,
                    AVG(CASE WHEN time_taken_minutes IS NOT NULL THEN time_taken_minutes END) as avg_time,
                    MIN(CASE WHEN time_taken_minutes IS NOT NULL THEN time_taken_minutes END) as min_time,
                    MAX(CASE WHEN time_taken_minutes IS NOT NULL THEN time_taken_minutes END) as max_time
                FROM site_visits
                WHERE assigned_to IS NOT NULL AND assigned_to != ''
            """
            params = []

            if date_from:
                query += " AND DATE(visit_date) >= ?"
                params.append(date_from)
            if date_to:
                query += " AND DATE(visit_date) <= ?"
                params.append(date_to)

            query += " GROUP BY assigned_to ORDER BY total_visits DESC"
            cursor.execute(query, params)

            # Post-process: split multi-staff assigned_to and re-aggregate per individual
            staff_agg = {}
            for row in cursor.fetchall():
                names = [n.strip() for n in (row["assigned_to"] or "").split(",") if n.strip()]
                if not names:
                    continue
                for name in names:
                    if name not in staff_agg:
                        staff_agg[name] = {"total": 0, "completed": 0, "pending": 0, "times": []}
                    staff_agg[name]["total"] += row["total_visits"]
                    staff_agg[name]["completed"] += row["completed"] or 0
                    staff_agg[name]["pending"] += row["pending"] or 0
                    # Collect individual times for accurate avg/min/max
                    if row["avg_time"] is not None:
                        staff_agg[name]["times"].append((row["avg_time"], row["total_visits"], row["min_time"], row["max_time"]))

            result = []
            for name, data in staff_agg.items():
                # Weighted average, overall min/max
                avg_time = None
                min_time = None
                max_time = None
                if data["times"]:
                    total_weight = sum(t[1] for t in data["times"])
                    avg_time = round(sum(t[0] * t[1] for t in data["times"]) / total_weight, 1) if total_weight else None
                    min_time = min(t[2] for t in data["times"] if t[2] is not None) if any(t[2] is not None for t in data["times"]) else None
                    max_time = max(t[3] for t in data["times"] if t[3] is not None) if any(t[3] is not None for t in data["times"]) else None
                result.append({
                    "assigned_to": name,
                    "total_visits": data["total"],
                    "completed": data["completed"],
                    "pending": data["pending"],
                    "avg_time_minutes": avg_time,
                    "min_time_minutes": min_time,
                    "max_time_minutes": max_time
                })
            result.sort(key=lambda x: x["total_visits"], reverse=True)
            return result

    def get_site_visit_by_date(self, date_from: str = None, date_to: str = None) -> list:
        """Get site visits aggregated by date."""
        with self._get_connection() as conn:
            cursor = conn.cursor()

            query = """
                SELECT
                    visit_date,
                    COUNT(*) as total_visits,
                    SUM(CASE WHEN status = 'completed' THEN 1 ELSE 0 END) as completed,
                    SUM(CASE WHEN status = 'pending' THEN 1 ELSE 0 END) as pending,
                    AVG(CASE WHEN time_taken_minutes IS NOT NULL THEN time_taken_minutes END) as avg_time
                FROM site_visits
                WHERE visit_date IS NOT NULL
            """
            params = []

            if date_from:
                query += " AND DATE(visit_date) >= ?"
                params.append(date_from)
            if date_to:
                query += " AND DATE(visit_date) <= ?"
                params.append(date_to)

            query += " GROUP BY visit_date ORDER BY visit_date DESC LIMIT 30"
            cursor.execute(query, params)

            return [{
                "date": row["visit_date"],
                "total_visits": row["total_visits"],
                "completed": row["completed"] or 0,
                "pending": row["pending"] or 0,
                "avg_time_minutes": round(row["avg_time"], 1) if row["avg_time"] else None
            } for row in cursor.fetchall()]

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
            znuny_url=row["znuny_url"] if "znuny_url" in keys else None,
            portal_url=row["portal_url"] if "portal_url" in keys else None,
            raw_dump=row["raw_dump"] if "raw_dump" in keys else None,
            created_at=datetime.fromisoformat(row["created_at"]) if row["created_at"] else None,
            updated_at=datetime.fromisoformat(row["updated_at"]) if row["updated_at"] else None,
            completed_at=datetime.fromisoformat(row["completed_at"]) if row["completed_at"] else None
        )

    # ==================== System Logs ====================

    def log_system(self, level: str, source: str, message: str, details: str = None):
        """
        Log a system event.
        level: 'info', 'warning', 'error', 'debug'
        source: module/component name (e.g., 'extractor.medianet', 'znuny', 'scheduler')
        message: short description of the event
        details: optional additional details (JSON or text)
        """
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                INSERT INTO system_logs (level, source, message, details, created_at)
                VALUES (?, ?, ?, ?, ?)
            """, (level.lower(), source, message, details, now_maldives().isoformat()))

    def get_system_logs(
        self,
        level: str = None,
        source: str = None,
        search: str = None,
        limit: int = 100,
        offset: int = 0
    ) -> dict:
        """Get system logs with optional filtering."""
        with self._get_connection() as conn:
            cursor = conn.cursor()

            query = "SELECT * FROM system_logs WHERE 1=1"
            params = []

            if level:
                query += " AND level = ?"
                params.append(level.lower())

            if source:
                query += " AND source LIKE ?"
                params.append(f"%{source}%")

            if search:
                query += " AND (message LIKE ? OR details LIKE ?)"
                params.extend([f"%{search}%", f"%{search}%"])

            # Get total count
            count_query = query.replace("SELECT *", "SELECT COUNT(*)")
            cursor.execute(count_query, params)
            total = cursor.fetchone()[0]

            # Get logs with pagination
            query += " ORDER BY created_at DESC LIMIT ? OFFSET ?"
            params.extend([limit, offset])
            cursor.execute(query, params)

            logs = [{
                "id": row["id"],
                "level": row["level"],
                "source": row["source"],
                "message": row["message"],
                "details": row["details"],
                "created_at": row["created_at"]
            } for row in cursor.fetchall()]

            return {"logs": logs, "total": total}

    def get_log_stats(self) -> dict:
        """Get system log statistics."""
        with self._get_connection() as conn:
            cursor = conn.cursor()

            # Count by level
            cursor.execute("""
                SELECT level, COUNT(*) as count FROM system_logs
                GROUP BY level
            """)
            by_level = {row["level"]: row["count"] for row in cursor.fetchall()}

            # Count by source (top 10)
            cursor.execute("""
                SELECT source, COUNT(*) as count FROM system_logs
                GROUP BY source ORDER BY count DESC LIMIT 10
            """)
            by_source = [{
                "source": row["source"],
                "count": row["count"]
            } for row in cursor.fetchall()]

            # Today's counts (use MVT date, not UTC)
            today_date = now_maldives().date().isoformat()
            cursor.execute("""
                SELECT level, COUNT(*) as count FROM system_logs
                WHERE DATE(substr(created_at, 1, 19)) = ?
                GROUP BY level
            """, (today_date,))
            today = {row["level"]: row["count"] for row in cursor.fetchall()}

            # Total count
            cursor.execute("SELECT COUNT(*) as total FROM system_logs")
            total = cursor.fetchone()["total"]

            return {
                "by_level": by_level,
                "by_source": by_source,
                "today": today,
                "total": total
            }

    def clear_old_logs(self, days: int = 2) -> dict:
        """Clear all log tables older than specified days. Returns counts per table."""
        results = {}
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cutoff = f"-{days}"
            for table, col in [
                ("system_logs", "created_at"),
                ("extraction_logs", "extracted_at"),
                ("login_stats", "created_at"),
            ]:
                cursor.execute(f"""
                    DELETE FROM {table}
                    WHERE {col} < datetime('now', ? || ' days')
                """, (cutoff,))
                results[table] = cursor.rowcount
        total = sum(results.values())
        if total > 0:
            logger.info(f"Cleared old logs ({days}d): {results}")
        return results

    # ==================== App Settings ====================

    def get_setting(self, key: str, default: str = None) -> str:
        """Get a single setting value."""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT value FROM app_settings WHERE key = ?", (key,))
            row = cursor.fetchone()
            return row["value"] if row else default

    def set_setting(self, key: str, value: str, description: str = None):
        """Set a single setting value."""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            if description:
                cursor.execute("""
                    INSERT OR REPLACE INTO app_settings (key, value, description, updated_at)
                    VALUES (?, ?, ?, ?)
                """, (key, value, description, now_maldives()))
            else:
                cursor.execute("""
                    INSERT INTO app_settings (key, value, updated_at)
                    VALUES (?, ?, ?)
                    ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at
                """, (key, value, now_maldives()))

    def get_all_settings(self) -> dict:
        """Get all settings as a dictionary."""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT key, value, description FROM app_settings")
            return {
                row["key"]: {"value": row["value"], "description": row["description"]}
                for row in cursor.fetchall()
            }

    def get_performance_thresholds(self) -> dict:
        """Get performance threshold settings (single query instead of 4)."""
        defaults = {
            "perf_threshold_good": "5",
            "perf_threshold_warning": "10",
            "perf_threshold_bad": "30",
            "perf_threshold_critical": "60"
        }
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT key, value FROM app_settings WHERE key IN (?, ?, ?, ?)",
                tuple(defaults.keys())
            )
            found = {row["key"]: row["value"] for row in cursor.fetchall()}
        return {
            "good": int(found.get("perf_threshold_good", defaults["perf_threshold_good"])),
            "warning": int(found.get("perf_threshold_warning", defaults["perf_threshold_warning"])),
            "bad": int(found.get("perf_threshold_bad", defaults["perf_threshold_bad"])),
            "critical": int(found.get("perf_threshold_critical", defaults["perf_threshold_critical"]))
        }

    def set_performance_thresholds(self, good: int, warning: int, bad: int, critical: int):
        """Set performance threshold settings (single transaction)."""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            for key, val in [
                ("perf_threshold_good", good), ("perf_threshold_warning", warning),
                ("perf_threshold_bad", bad), ("perf_threshold_critical", critical)
            ]:
                cursor.execute("""
                    INSERT INTO app_settings (key, value, updated_at)
                    VALUES (?, ?, ?)
                    ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at
                """, (key, str(val), now_maldives()))

    # ==================== Operating Hours ====================

    def get_operating_hours(self) -> dict:
        """Get operating hours settings."""
        defaults = {
            "operating_hours_enabled": "0",
            "operating_hours_start": "7",
            "operating_hours_end": "22"
        }
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT key, value FROM app_settings WHERE key IN (?, ?, ?)",
                tuple(defaults.keys())
            )
            found = {row["key"]: row["value"] for row in cursor.fetchall()}
        return {
            "enabled": found.get("operating_hours_enabled", defaults["operating_hours_enabled"]) == "1",
            "start_hour": int(found.get("operating_hours_start", defaults["operating_hours_start"])),
            "end_hour": int(found.get("operating_hours_end", defaults["operating_hours_end"]))
        }

    def set_operating_hours(self, enabled: bool, start_hour: int, end_hour: int):
        """Set operating hours settings."""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            for key, val in [
                ("operating_hours_enabled", "1" if enabled else "0"),
                ("operating_hours_start", str(start_hour)),
                ("operating_hours_end", str(end_hour))
            ]:
                cursor.execute("""
                    INSERT INTO app_settings (key, value, updated_at)
                    VALUES (?, ?, ?)
                    ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at
                """, (key, val, now_maldives()))

    # ==================== Browser Memory Limits ====================

    # Per-portal default browser memory limit (MB). The browser is reset when its
    # Chrome process tree exceeds this. Dhiraagu's default is higher (Cloudflare
    # headed Chrome); in Docker Dhiraagu runs in the sidecar and is bounded by that
    # container's mem_limit instead — this app-side value applies to local fallback.
    MEMORY_LIMIT_DEFAULTS = {"dhiraagu": 2000, "ooredoo": 1500, "rol": 1500, "medianet": 1500}

    def get_memory_limits(self) -> dict:
        """Per-portal browser memory limit (MB), config override or default."""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT key, value FROM app_settings WHERE key IN (%s)" %
                ",".join("?" * len(self.MEMORY_LIMIT_DEFAULTS)),
                tuple(f"mem_limit_{p}" for p in self.MEMORY_LIMIT_DEFAULTS)
            )
            found = {row["key"]: row["value"] for row in cursor.fetchall()}
        out = {}
        for portal, default in self.MEMORY_LIMIT_DEFAULTS.items():
            try:
                out[portal] = int(found.get(f"mem_limit_{portal}", default))
            except (TypeError, ValueError):
                out[portal] = default
        return out

    def set_memory_limits(self, limits: dict):
        """Set per-portal browser memory limits (MB). Ignores unknown portals and
        clamps to a sane range (256MB..8192MB)."""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            for portal, mb in (limits or {}).items():
                p = str(portal).lower()
                if p not in self.MEMORY_LIMIT_DEFAULTS:
                    continue
                try:
                    val = max(256, min(8192, int(mb)))
                except (TypeError, ValueError):
                    continue
                cursor.execute("""
                    INSERT INTO app_settings (key, value, updated_at)
                    VALUES (?, ?, ?)
                    ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at
                """, (f"mem_limit_{p}", str(val), now_maldives()))

    # ==================== ISP Extraction Toggle ====================

    def get_isp_extraction_enabled(self) -> bool:
        """Whether scheduled ISP portal extraction (scraping) is enabled. Default True."""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT value FROM app_settings WHERE key = ?", ("isp_extraction_enabled",))
            row = cursor.fetchone()
        if row is None:
            return True  # default: enabled
        return row["value"] == "1"

    def set_isp_extraction_enabled(self, enabled: bool):
        """Enable/disable scheduled ISP portal extraction."""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                INSERT INTO app_settings (key, value, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at
            """, ("isp_extraction_enabled", "1" if enabled else "0", now_maldives()))

    def get_portal_enabled(self, portal: str) -> bool:
        """Whether extraction for a specific ISP portal is enabled. Default True."""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT value FROM app_settings WHERE key = ?",
                (f"portal_enabled_{portal.lower()}",)
            )
            row = cursor.fetchone()
        if row is None:
            return True  # default: enabled
        return row["value"] == "1"

    def set_portal_enabled(self, portal: str, enabled: bool):
        """Enable/disable extraction for a specific ISP portal."""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                INSERT INTO app_settings (key, value, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at
            """, (f"portal_enabled_{portal.lower()}", "1" if enabled else "0", now_maldives()))

    # ==================== Portal Config (DB-stored) ====================

    def get_config_settings(self) -> dict:
        """Get all portal/app config settings (cfg_* keys) as a flat dict."""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT key, value FROM app_settings WHERE key LIKE 'cfg_%'")
            return {row["key"].replace("cfg_", "", 1): row["value"] for row in cursor.fetchall()}

    def set_config_settings(self, config: dict):
        """Bulk upsert portal/app config settings."""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            for key, value in config.items():
                db_key = f"cfg_{key}" if not key.startswith("cfg_") else key
                cursor.execute("""
                    INSERT INTO app_settings (key, value, updated_at)
                    VALUES (?, ?, ?)
                    ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at
                """, (db_key, value, now_maldives()))

    # ==================== Znuny-Only Tickets ====================

    def is_ticket_linked_to_isp(self, znuny_ticket_id: str) -> bool:
        """Check if a Znuny ticket is linked to any ISP portal ticket."""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT 1 FROM tickets
                WHERE znuny_ticket_id = ?
                LIMIT 1
            """, (znuny_ticket_id,))
            return cursor.fetchone() is not None

    def get_znuny_ticket_metadata(self, znuny_ticket_id: str) -> dict | None:
        """Get queue/state/owner metadata for a Znuny ticket."""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT state, queue, owner, priority FROM znuny_tickets WHERE znuny_ticket_id = ?",
                (znuny_ticket_id,)
            )
            row = cursor.fetchone()
            if row:
                return {"state": row["state"], "queue": row["queue"], "owner": row["owner"], "priority": row["priority"]}
            return None

    def get_znuny_states_for(self, znuny_ticket_ids: list) -> dict:
        """Batch-fetch Znuny state for many ticket numbers. Returns
        {znuny_ticket_id: state}. Used to enrich the ISP tickets list with the
        linked Znuny ticket's open/closed state."""
        ids = [str(i) for i in znuny_ticket_ids if i]
        if not ids:
            return {}
        out = {}
        with self._get_connection() as conn:
            cursor = conn.cursor()
            for i in range(0, len(ids), 500):
                chunk = ids[i:i + 500]
                ph = ",".join("?" * len(chunk))
                cursor.execute(
                    f"SELECT znuny_ticket_id, state FROM znuny_tickets WHERE znuny_ticket_id IN ({ph})",
                    chunk,
                )
                for row in cursor.fetchall():
                    out[row["znuny_ticket_id"]] = row["state"]
        return out

    def upsert_znuny_only_ticket(self, data: dict) -> int:
        """Insert or update a Znuny ticket. Stores ALL Znuny tickets (ISP-linked and orphan)."""
        now = now_maldives()
        with self._get_connection() as conn:
            cursor = conn.cursor()

            # Calculate time_to_close if closed
            time_to_close = None
            if data.get("closed_at") and data.get("created_at"):
                try:
                    created = data["created_at"]
                    closed = data["closed_at"]
                    if isinstance(created, str):
                        created = datetime.fromisoformat(created.replace('Z', '+00:00'))
                    if isinstance(closed, str):
                        closed = datetime.fromisoformat(closed.replace('Z', '+00:00'))
                    time_to_close = (closed - created).total_seconds() / 60
                except Exception:
                    pass

            cursor.execute("""
                INSERT INTO znuny_tickets
                    (znuny_ticket_id, title, state, queue, priority, owner, created_at, created_by,
                     closed_at, time_to_close_minutes, article_count, last_article_by,
                     last_article_at, znuny_url, isp_ticket_id, first_seen_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(znuny_ticket_id) DO UPDATE SET
                    title = excluded.title,
                    state = COALESCE(excluded.state, znuny_tickets.state),
                    queue = COALESCE(excluded.queue, znuny_tickets.queue),
                    priority = COALESCE(excluded.priority, znuny_tickets.priority),
                    owner = COALESCE(excluded.owner, znuny_tickets.owner),
                    -- created_at/created_by are authoritative from Znuny; refresh
                    -- them so a later sync corrects any stale earlier value
                    -- (e.g. wrong created date or an unresolved creator name).
                    created_at = COALESCE(excluded.created_at, znuny_tickets.created_at),
                    created_by = CASE
                        WHEN excluded.created_by IS NOT NULL AND excluded.created_by != ''
                        THEN excluded.created_by ELSE znuny_tickets.created_by END,
                    closed_at = excluded.closed_at,
                    time_to_close_minutes = excluded.time_to_close_minutes,
                    article_count = excluded.article_count,
                    last_article_by = excluded.last_article_by,
                    last_article_at = excluded.last_article_at,
                    znuny_url = excluded.znuny_url,
                    isp_ticket_id = COALESCE(excluded.isp_ticket_id, znuny_tickets.isp_ticket_id),
                    updated_at = excluded.updated_at
            """, (
                data.get("znuny_ticket_id"),
                data.get("title"),
                data.get("state"),
                data.get("queue"),
                data.get("priority"),
                data.get("owner"),
                data.get("created_at"),
                data.get("created_by"),
                data.get("closed_at"),
                time_to_close,
                data.get("article_count", 0),
                data.get("last_article_by"),
                data.get("last_article_at"),
                data.get("znuny_url"),
                data.get("isp_ticket_id"),
                now,
                now
            ))
            return cursor.lastrowid

    @staticmethod
    def _znuny_date_scope(date_from: str = None, date_to: str = None):
        """Build a WHERE fragment matching tickets whose created_at OR closed_at
        falls within [date_from, date_to]. This makes a date range include both
        tickets created in the period and tickets closed in the period (so
        closing work on older tickets is counted). Returns ('', []) when no
        bounds are given. closed_at IS NULL rows naturally fail the closed_at
        comparison, so they only match via created_at."""
        if not date_from and not date_to:
            return "", []

        def bounds(col):
            # substr(...,1,19) takes the MVT wall-clock part; bare DATE() on a
            # +05:00 timestamp converts to UTC and shifts early-morning tickets to
            # the previous day.
            parts, p = [], []
            if date_from:
                parts.append(f"DATE(substr({col}, 1, 19)) >= ?")
                p.append(date_from)
            if date_to:
                parts.append(f"DATE(substr({col}, 1, 19)) <= ?")
                p.append(date_to)
            return "(" + " AND ".join(parts) + ")", p

        created_sql, created_p = bounds("created_at")
        closed_sql, closed_p = bounds("closed_at")
        return f" AND ({created_sql} OR {closed_sql})", created_p + closed_p

    def get_znuny_only_tickets(self, state: str = None, created_by: str = None,
                                queue: str = None, owner: str = None,
                                date_from: str = None, date_to: str = None,
                                linked: str = None,
                                limit: int = 100, offset: int = 0) -> dict:
        """Get Znuny tickets with optional filters. Use linked='yes'/'no' to filter by ISP link."""
        with self._get_connection() as conn:
            cursor = conn.cursor()

            query = "SELECT * FROM znuny_tickets WHERE 1=1"
            params = []

            if state:
                if state.lower() == "open":
                    query += " AND (state IS NULL OR LOWER(state) NOT IN ('closed', 'resolved'))"
                elif state.lower() == "closed":
                    query += " AND LOWER(state) IN ('closed', 'resolved')"
                else:
                    query += " AND LOWER(state) = LOWER(?)"
                    params.append(state)

            if created_by:
                query += " AND LOWER(created_by) = LOWER(?)"
                params.append(created_by)

            if queue:
                query += " AND LOWER(queue) = LOWER(?)"
                params.append(queue)

            if owner:
                query += " AND LOWER(owner) = LOWER(?)"
                params.append(owner)

            date_sql, date_params = self._znuny_date_scope(date_from, date_to)
            query += date_sql
            params.extend(date_params)

            if linked == "yes":
                query += " AND isp_ticket_id IS NOT NULL"
            elif linked == "no":
                query += " AND isp_ticket_id IS NULL"

            # Get total count
            count_query = query.replace("SELECT *", "SELECT COUNT(*)")
            cursor.execute(count_query, params)
            total = cursor.fetchone()[0]

            # Get paginated results
            query += " ORDER BY created_at DESC LIMIT ? OFFSET ?"
            params.extend([limit, offset])
            cursor.execute(query, params)

            tickets = [{
                "id": row["id"],
                "znuny_ticket_id": row["znuny_ticket_id"],
                "title": row["title"],
                "state": row["state"],
                "queue": row["queue"],
                "priority": row["priority"],
                "owner": row["owner"],
                "created_at": row["created_at"],
                "created_by": row["created_by"],
                "closed_at": row["closed_at"],
                "time_to_close_minutes": row["time_to_close_minutes"],
                "article_count": row["article_count"],
                "last_article_by": row["last_article_by"],
                "last_article_at": row["last_article_at"],
                "znuny_url": row["znuny_url"],
                "isp_ticket_id": row["isp_ticket_id"],
                "first_seen_at": row["first_seen_at"],
                "updated_at": row["updated_at"]
            } for row in cursor.fetchall()]

            return {"total": total, "tickets": tickets}

    def get_znuny_only_stats(self, date_from: str = None, date_to: str = None) -> dict:
        """Get summary statistics for Znuny tickets (all + linked/unlinked breakdown).

        When date_from/date_to are given, the total/open/closed/linked/unlinked and
        avg-close counts are scoped to tickets whose created_at falls in that range
        (matching the staff-stats and tickets tables on the page)."""
        with self._get_connection() as conn:
            cursor = conn.cursor()

            # Build a shared WHERE clause so all aggregates use the same date scope
            # (matches tickets created OR closed within the range).
            date_sql, params = self._znuny_date_scope(date_from, date_to)
            where = "WHERE 1=1" + date_sql

            # All counts in one query
            cursor.execute(f"""
                SELECT
                    COUNT(*) as total,
                    SUM(CASE WHEN state IS NULL OR LOWER(state) NOT IN ('closed', 'resolved') THEN 1 ELSE 0 END) as open_count,
                    SUM(CASE WHEN LOWER(state) IN ('closed', 'resolved') THEN 1 ELSE 0 END) as closed_count,
                    SUM(CASE WHEN isp_ticket_id IS NOT NULL THEN 1 ELSE 0 END) as linked,
                    SUM(CASE WHEN isp_ticket_id IS NULL THEN 1 ELSE 0 END) as unlinked
                FROM znuny_tickets
                {where}
            """, params)
            row = cursor.fetchone()
            total = row["total"]
            open_count = row["open_count"]
            closed_count = row["closed_count"]
            linked = row["linked"]
            unlinked = row["unlinked"]

            # Today's date in MVT
            today = now_maldives().date().isoformat()

            # Today's new (first seen today) - always all-time "today", not date-scoped
            cursor.execute("""
                SELECT COUNT(*) as count FROM znuny_tickets
                WHERE DATE(substr(first_seen_at, 1, 19)) = ?
            """, (today,))
            today_new = cursor.fetchone()["count"]

            # Avg time to close. Exclude stale-cleanup outliers (tickets left open
            # longer than CLOSE_AVG_CAP) so one ancient ticket closed in-period
            # doesn't blow the average to weeks/months — keeps it a typical figure.
            cursor.execute(f"""
                SELECT AVG(time_to_close_minutes) as avg_time FROM znuny_tickets
                {where} AND time_to_close_minutes IS NOT NULL
                    AND time_to_close_minutes <= {CLOSE_AVG_CAP_MINUTES}
            """, params)
            avg_close_time = cursor.fetchone()["avg_time"]

            return {
                "total": total,
                "open": open_count,
                "closed": closed_count,
                "linked": linked,
                "unlinked": unlinked,
                "today_new": today_new,
                "avg_close_time_minutes": round(avg_close_time, 1) if avg_close_time else None
            }

    def get_znuny_queue_names(self) -> list:
        """Get distinct queue names from znuny_tickets."""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT DISTINCT queue FROM znuny_tickets
                WHERE queue IS NOT NULL AND queue != ''
                ORDER BY queue
            """)
            return [row["queue"] for row in cursor.fetchall()]

    def get_znuny_owner_names(self) -> list:
        """Get distinct owner names from znuny_tickets."""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT DISTINCT owner FROM znuny_tickets
                WHERE owner IS NOT NULL AND owner != ''
                ORDER BY owner
            """)
            return [row["owner"] for row in cursor.fetchall()]

    def get_znuny_only_staff_stats(self, date_from: str = None, date_to: str = None) -> list:
        """Per-staff Znuny performance for the date range:
        - tickets *created by* the staff member (creator attribution, scoped by
          created/closed in range, with open/closed/avg-close breakdown)
        - articles *created by* the staff member (author attribution, scoped by
          the article's own created_at)

        Staff who only wrote articles (no tickets created) in the range still
        appear, and vice versa."""
        with self._get_connection() as conn:
            cursor = conn.cursor()

            staff = {}

            def blank(name):
                return {
                    "created_by": name,
                    "total_tickets": 0,
                    "closed_tickets": 0,
                    "open_tickets": 0,
                    "avg_close_time_minutes": None,
                    "total_articles": 0,
                }

            # Tickets created by each staff member (creator attribution)
            date_sql, params = self._znuny_date_scope(date_from, date_to)
            cursor.execute(f"""
                SELECT
                    created_by,
                    COUNT(*) as total_tickets,
                    SUM(CASE WHEN LOWER(state) IN ('closed', 'resolved') THEN 1 ELSE 0 END) as closed_tickets,
                    AVG(CASE WHEN time_to_close_minutes IS NOT NULL AND time_to_close_minutes <= {CLOSE_AVG_CAP_MINUTES} THEN time_to_close_minutes END) as avg_close_time
                FROM znuny_tickets
                WHERE created_by IS NOT NULL AND created_by != ''
                {date_sql}
                GROUP BY created_by
            """, params)
            for row in cursor.fetchall():
                name = row["created_by"]
                rec = blank(name)
                closed = row["closed_tickets"] or 0
                rec["total_tickets"] = row["total_tickets"]
                rec["closed_tickets"] = closed
                rec["open_tickets"] = row["total_tickets"] - closed
                rec["avg_close_time_minutes"] = round(row["avg_close_time"], 1) if row["avg_close_time"] else None
                staff[name] = rec

            # Articles created by each staff member (author attribution, scoped by
            # the article's own created_at)
            art_parts, art_params = [], []
            if date_from:
                art_parts.append("DATE(substr(created_at, 1, 19)) >= ?")
                art_params.append(date_from)
            if date_to:
                art_parts.append("DATE(substr(created_at, 1, 19)) <= ?")
                art_params.append(date_to)
            art_where = (" AND " + " AND ".join(art_parts)) if art_parts else ""
            cursor.execute(f"""
                SELECT created_by, COUNT(*) as cnt
                FROM znuny_articles
                WHERE created_by IS NOT NULL AND created_by != ''
                {art_where}
                GROUP BY created_by
            """, art_params)
            for row in cursor.fetchall():
                name = row["created_by"]
                if name not in staff:
                    staff[name] = blank(name)
                staff[name]["total_articles"] = row["cnt"]

            result = list(staff.values())
            result.sort(key=lambda x: (x["total_tickets"], x["total_articles"]), reverse=True)
            return result

    def get_znuny_only_staff_names(self) -> list:
        """Get list of staff names who created Znuny-only tickets."""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT DISTINCT created_by FROM znuny_tickets
                WHERE created_by IS NOT NULL AND created_by != ''
                ORDER BY created_by
            """)
            return [row["created_by"] for row in cursor.fetchall()]

    def mark_znuny_tickets_closed(self, open_znuny_ids: set) -> int:
        """Mark ISP-linked znuny_tickets as closed if they are no longer in the
        OAN open tickets list. Returns count of tickets marked as closed.

        Only ISP-linked tickets are touched: the OAN open list is authoritative
        only for them. Non-ISP tickets (orphans and creator-swept tickets from
        other services) have their state set authoritatively by the creator sweep
        (from Znuny StateType), so they must not be force-closed just because they
        aren't in the OAN open list."""
        if not open_znuny_ids:
            return 0

        with self._get_connection() as conn:
            cursor = conn.cursor()
            now = now_maldives()

            # Find tickets that were open but are no longer in the open list
            placeholders = ",".join("?" * len(open_znuny_ids))
            cursor.execute(f"""
                UPDATE znuny_tickets
                SET state = 'closed',
                    closed_at = ?,
                    time_to_close_minutes = CASE
                        WHEN created_at IS NOT NULL THEN
                            (julianday(?) - julianday(created_at)) * 24 * 60
                        ELSE NULL
                    END,
                    updated_at = ?
                WHERE znuny_ticket_id NOT IN ({placeholders})
                    AND isp_ticket_id IS NOT NULL
                    AND (state IS NULL OR LOWER(state) NOT IN ('closed', 'resolved'))
            """, [now, now, now] + list(open_znuny_ids))

            count = cursor.rowcount
            if count > 0:
                logger.info(f"Marked {count} znuny_tickets as closed")
            return count

    # ==================== Staff Management ====================

    def get_all_staff_names_with_counts(self) -> list:
        """
        Get all unique staff names from all tables with counts per source.
        Returns list of dicts with: name, isp_tickets, znuny_tickets, articles, site_visits, total
        """
        with self._get_connection() as conn:
            cursor = conn.cursor()

            # Collect staff names from all sources
            staff_data = {}

            # 1. ISP tickets (znuny_created_by)
            cursor.execute("""
                SELECT znuny_created_by as name, COUNT(*) as count
                FROM tickets
                WHERE znuny_created_by IS NOT NULL AND znuny_created_by != ''
                GROUP BY znuny_created_by
            """)
            for row in cursor.fetchall():
                name = row["name"]
                if name not in staff_data:
                    staff_data[name] = {"name": name, "isp_tickets": 0, "znuny_tickets": 0, "articles": 0, "site_visits": 0}
                staff_data[name]["isp_tickets"] = row["count"]

            # 2. Znuny-only tickets (created_by)
            cursor.execute("""
                SELECT created_by as name, COUNT(*) as count
                FROM znuny_tickets
                WHERE created_by IS NOT NULL AND created_by != ''
                GROUP BY created_by
            """)
            for row in cursor.fetchall():
                name = row["name"]
                if name not in staff_data:
                    staff_data[name] = {"name": name, "isp_tickets": 0, "znuny_tickets": 0, "articles": 0, "site_visits": 0}
                staff_data[name]["znuny_tickets"] = row["count"]

            # 3. Articles (created_by)
            cursor.execute("""
                SELECT created_by as name, COUNT(*) as count
                FROM znuny_articles
                WHERE created_by IS NOT NULL AND created_by != ''
                GROUP BY created_by
            """)
            for row in cursor.fetchall():
                name = row["name"]
                if name not in staff_data:
                    staff_data[name] = {"name": name, "isp_tickets": 0, "znuny_tickets": 0, "articles": 0, "site_visits": 0}
                staff_data[name]["articles"] = row["count"]

            # 4. Site visits (assigned_to) - split multi-staff names
            cursor.execute("""
                SELECT assigned_to as name, COUNT(*) as count
                FROM site_visits
                WHERE assigned_to IS NOT NULL AND assigned_to != ''
                GROUP BY assigned_to
            """)
            for row in cursor.fetchall():
                names = [n.strip() for n in (row["name"] or "").split(",") if n.strip()]
                for name in names:
                    if name not in staff_data:
                        staff_data[name] = {"name": name, "isp_tickets": 0, "znuny_tickets": 0, "articles": 0, "site_visits": 0}
                    staff_data[name]["site_visits"] += row["count"]

            # Calculate totals and convert to list
            result = []
            for name, data in staff_data.items():
                data["total"] = data["isp_tickets"] + data["znuny_tickets"] + data["articles"] + data["site_visits"]
                result.append(data)

            # Sort by total descending
            result.sort(key=lambda x: x["total"], reverse=True)
            return result

    def get_staff_merge_preview(self, source_name: str, target_name: str) -> dict:
        """
        Preview what will be affected by merging source_name into target_name.
        Returns counts of records that will be updated in each table.
        """
        with self._get_connection() as conn:
            cursor = conn.cursor()

            preview = {
                "source": source_name,
                "target": target_name,
                "affected": {}
            }

            # Count affected ISP tickets
            cursor.execute("""
                SELECT COUNT(*) as count FROM tickets
                WHERE znuny_created_by = ?
            """, (source_name,))
            preview["affected"]["isp_tickets"] = cursor.fetchone()["count"]

            # Count affected Znuny-only tickets (created_by)
            cursor.execute("""
                SELECT COUNT(*) as count FROM znuny_tickets
                WHERE created_by = ?
            """, (source_name,))
            preview["affected"]["znuny_tickets_created"] = cursor.fetchone()["count"]

            # Count affected Znuny-only tickets (last_article_by)
            cursor.execute("""
                SELECT COUNT(*) as count FROM znuny_tickets
                WHERE last_article_by = ?
            """, (source_name,))
            preview["affected"]["znuny_tickets_last_article"] = cursor.fetchone()["count"]

            # Count affected articles
            cursor.execute("""
                SELECT COUNT(*) as count FROM znuny_articles
                WHERE created_by = ?
            """, (source_name,))
            preview["affected"]["articles"] = cursor.fetchone()["count"]

            # Count affected site visits (including multi-staff assignments)
            cursor.execute("""
                SELECT COUNT(*) as count FROM site_visits
                WHERE assigned_to = ? OR assigned_to LIKE ? OR assigned_to LIKE ? OR assigned_to LIKE ?
            """, (source_name, f"{source_name}, %", f"%, {source_name}", f"%, {source_name}, %"))
            preview["affected"]["site_visits"] = cursor.fetchone()["count"]

            # Count affected staff performance daily
            cursor.execute("""
                SELECT COUNT(*) as count FROM staff_performance_daily
                WHERE staff_name = ?
            """, (source_name,))
            preview["affected"]["performance_daily"] = cursor.fetchone()["count"]

            # Calculate total
            preview["total_affected"] = sum(preview["affected"].values())

            return preview

    def merge_staff_names(self, source_name: str, target_name: str) -> dict:
        """
        Merge source_name into target_name across all tables.
        Returns counts of records updated in each table.
        """
        with self._get_connection() as conn:
            cursor = conn.cursor()

            result = {
                "source": source_name,
                "target": target_name,
                "updated": {}
            }

            # 1. Update ISP tickets (znuny_created_by)
            cursor.execute("""
                UPDATE tickets SET znuny_created_by = ?, updated_at = ?
                WHERE znuny_created_by = ?
            """, (target_name, now_maldives(), source_name))
            result["updated"]["isp_tickets"] = cursor.rowcount

            # 2. Update Znuny-only tickets (created_by)
            cursor.execute("""
                UPDATE znuny_tickets SET created_by = ?, updated_at = ?
                WHERE created_by = ?
            """, (target_name, now_maldives(), source_name))
            result["updated"]["znuny_tickets_created"] = cursor.rowcount

            # 3. Update Znuny-only tickets (last_article_by)
            cursor.execute("""
                UPDATE znuny_tickets SET last_article_by = ?, updated_at = ?
                WHERE last_article_by = ?
            """, (target_name, now_maldives(), source_name))
            result["updated"]["znuny_tickets_last_article"] = cursor.rowcount

            # 4. Update articles (created_by)
            cursor.execute("""
                UPDATE znuny_articles SET created_by = ?
                WHERE created_by = ?
            """, (target_name, source_name))
            result["updated"]["articles"] = cursor.rowcount

            # 5. Update site visits (assigned_to) - handle multi-staff assignments
            now = now_maldives()
            # Exact match (single staff)
            cursor.execute("""
                UPDATE site_visits SET assigned_to = ?, updated_at = ?
                WHERE assigned_to = ?
            """, (target_name, now, source_name))
            sv_count = cursor.rowcount
            # Multi-staff: replace source_name within comma-separated values
            cursor.execute("""
                SELECT id, assigned_to FROM site_visits
                WHERE assigned_to LIKE ? OR assigned_to LIKE ? OR assigned_to LIKE ?
            """, (f"{source_name}, %", f"%, {source_name}", f"%, {source_name}, %"))
            for row in cursor.fetchall():
                names = [n.strip() for n in row["assigned_to"].split(",")]
                new_names = [target_name if n == source_name else n for n in names]
                # Deduplicate (in case target already in the list)
                seen = set()
                deduped = [n for n in new_names if n not in seen and not seen.add(n)]
                cursor.execute("UPDATE site_visits SET assigned_to = ?, updated_at = ? WHERE id = ?",
                               (", ".join(deduped), now, row["id"]))
                sv_count += 1
            result["updated"]["site_visits"] = sv_count

            # 6. Handle staff performance daily - need to merge or delete
            # First check if target already has entries for same dates
            cursor.execute("""
                SELECT date FROM staff_performance_daily
                WHERE staff_name = ?
            """, (source_name,))
            source_dates = [row["date"] for row in cursor.fetchall()]

            cursor.execute("""
                SELECT date FROM staff_performance_daily
                WHERE staff_name = ?
            """, (target_name,))
            target_dates = set(row["date"] for row in cursor.fetchall())

            # For dates where both exist, delete source (target keeps its data)
            # For dates where only source exists, update to target name
            overlapping_dates = [d for d in source_dates if d in target_dates]
            unique_dates = [d for d in source_dates if d not in target_dates]

            if overlapping_dates:
                cursor.execute(f"""
                    DELETE FROM staff_performance_daily
                    WHERE staff_name = ? AND date IN ({','.join('?' * len(overlapping_dates))})
                """, [source_name] + overlapping_dates)

            if unique_dates:
                cursor.execute(f"""
                    UPDATE staff_performance_daily SET staff_name = ?
                    WHERE staff_name = ? AND date IN ({','.join('?' * len(unique_dates))})
                """, [target_name, source_name] + unique_dates)

            result["updated"]["performance_daily"] = len(source_dates)

            # Calculate total
            result["total_updated"] = sum(result["updated"].values())

            logger.info(f"Merged staff '{source_name}' into '{target_name}': {result['total_updated']} records updated")

            return result

    def get_staff_znuny_tickets(self, staff_name: str, date_from: str = None, date_to: str = None,
                                 limit: int = 50, offset: int = 0) -> dict:
        """Get Znuny-only tickets created by a specific staff member."""
        with self._get_connection() as conn:
            cursor = conn.cursor()

            query = """
                SELECT * FROM znuny_tickets
                WHERE created_by = ?
            """
            params = [staff_name]

            if date_from:
                query += " AND DATE(substr(created_at, 1, 19)) >= ?"
                params.append(date_from)
            if date_to:
                query += " AND DATE(substr(created_at, 1, 19)) <= ?"
                params.append(date_to)

            # Get total count
            count_query = query.replace("SELECT *", "SELECT COUNT(*)")
            cursor.execute(count_query, params)
            total = cursor.fetchone()[0]

            # Get paginated results
            query += " ORDER BY created_at DESC LIMIT ? OFFSET ?"
            params.extend([limit, offset])
            cursor.execute(query, params)

            tickets = [{
                "id": row["id"],
                "znuny_ticket_id": row["znuny_ticket_id"],
                "title": row["title"],
                "state": row["state"],
                "queue": row["queue"],
                "owner": row["owner"],
                "created_at": row["created_at"],
                "created_by": row["created_by"],
                "closed_at": row["closed_at"],
                "time_to_close_minutes": row["time_to_close_minutes"],
                "article_count": row["article_count"],
                "znuny_url": row["znuny_url"]
            } for row in cursor.fetchall()]

            return {"total": total, "tickets": tickets}

    def get_staff_articles(self, staff_name: str, date_from: str = None, date_to: str = None,
                           limit: int = 50, offset: int = 0) -> dict:
        """Get articles created by a specific staff member."""
        with self._get_connection() as conn:
            cursor = conn.cursor()

            query = """
                SELECT a.*, t.portal, t.ticket_id as portal_ticket_id, t.customer_name
                FROM znuny_articles a
                LEFT JOIN tickets t ON a.ticket_id = t.id
                WHERE a.created_by = ?
            """
            params = [staff_name]

            if date_from:
                query += " AND DATE(substr(a.created_at, 1, 19)) >= ?"
                params.append(date_from)
            if date_to:
                query += " AND DATE(substr(a.created_at, 1, 19)) <= ?"
                params.append(date_to)

            # Get total count
            count_query = query.replace("SELECT a.*, t.portal, t.ticket_id as portal_ticket_id, t.customer_name",
                                         "SELECT COUNT(*)")
            cursor.execute(count_query, params)
            total = cursor.fetchone()[0]

            # Get paginated results
            query += " ORDER BY a.created_at DESC LIMIT ? OFFSET ?"
            params.extend([limit, offset])
            cursor.execute(query, params)

            articles = [{
                "id": row["id"],
                "znuny_ticket_id": row["znuny_ticket_id"],
                "article_number": row["article_number"],
                "subject": row["subject"],
                "sender": row["sender"],
                "via": row["via"],
                "created_at": row["created_at"],
                "created_by": row["created_by"],
                "portal": row["portal"],
                "portal_ticket_id": row["portal_ticket_id"],
                "customer_name": row["customer_name"]
            } for row in cursor.fetchall()]

            return {"total": total, "articles": articles}

    def get_report_portal_stats(self, date_from: str = None, date_to: str = None) -> dict:
        """Get ticket statistics by portal for reporting."""
        with self._get_connection() as conn:
            cursor = conn.cursor()

            query = """
                SELECT
                    portal,
                    COUNT(*) as total,
                    SUM(CASE WHEN in_znuny = 1 THEN 1 ELSE 0 END) as in_znuny,
                    SUM(CASE WHEN in_znuny = 0 OR in_znuny IS NULL THEN 1 ELSE 0 END) as pending,
                    SUM(CASE WHEN completed_at IS NOT NULL THEN 1 ELSE 0 END) as completed
                FROM tickets
                WHERE 1=1
            """
            params = []

            if date_from:
                query += " AND DATE(substr(created_at, 1, 19)) >= ?"
                params.append(date_from)
            if date_to:
                query += " AND DATE(substr(created_at, 1, 19)) <= ?"
                params.append(date_to)

            query += " GROUP BY portal ORDER BY total DESC"
            cursor.execute(query, params)

            portals = [{
                "portal": row["portal"],
                "total": row["total"],
                "in_znuny": row["in_znuny"] or 0,
                "pending": row["pending"] or 0,
                "completed": row["completed"] or 0
            } for row in cursor.fetchall()]

            return {"portals": portals}
