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
        self._logging_error = False  # Guard against infinite recursion
        self._init_db()

    def _log_db_error(self, operation: str, error: Exception, context: str = None):
        """Log database error to both logger and system_logs table (if possible)."""
        error_msg = f"{operation} failed: {type(error).__name__}: {error}"
        if context:
            error_msg += f" (context: {context})"
        logger.error(error_msg)

        # Try to log to system_logs table, but avoid infinite recursion
        if not self._logging_error:
            self._logging_error = True
            try:
                self.log_system("error", "database", error_msg, context)
            except Exception:
                pass  # Can't log to DB, already logged to file
            finally:
                self._logging_error = False

    @contextmanager
    def _get_connection(self):
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
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

            # Add portal_url column (migration)
            try:
                cursor.execute("ALTER TABLE tickets ADD COLUMN portal_url TEXT")
            except:
                pass  # Column already exists

            # Add znuny_url column (migration)
            try:
                cursor.execute("ALTER TABLE tickets ADD COLUMN znuny_url TEXT")
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

            # Performance indexes
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_tickets_portal ON tickets(portal)")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_tickets_completed ON tickets(completed_at)")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_tickets_in_znuny ON tickets(in_znuny)")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_tickets_portal_created ON tickets(portal_created_at)")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_tickets_znuny_created_by ON tickets(znuny_created_by)")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_extraction_logs_portal ON extraction_logs(portal)")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_extraction_logs_extracted_at ON extraction_logs(extracted_at)")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_login_stats_portal ON login_stats(portal)")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_login_stats_created_at ON login_stats(created_at)")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_staff_performance_daily ON staff_performance_daily(staff_name, date)")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_system_logs_created_at ON system_logs(created_at)")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_system_logs_level ON system_logs(level)")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_system_logs_source ON system_logs(source)")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_site_visits_assigned ON site_visits(assigned_to)")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_site_visits_date ON site_visits(visit_date)")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_site_visits_status ON site_visits(status)")

            # Migration: Add znuny_url to site_visits
            try:
                cursor.execute("ALTER TABLE site_visits ADD COLUMN znuny_url TEXT")
            except:
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

            # Indexes for znuny_tickets
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_znuny_tickets_state ON znuny_tickets(state)")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_znuny_tickets_created_by ON znuny_tickets(created_by)")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_znuny_tickets_created_at ON znuny_tickets(created_at)")

            # Performance indexes for tickets table
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_tickets_portal ON tickets(portal)")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_tickets_status ON tickets(status)")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_tickets_created_at ON tickets(created_at)")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_tickets_completed_at ON tickets(completed_at)")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_tickets_in_znuny ON tickets(in_znuny)")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_tickets_znuny_created_by ON tickets(znuny_created_by)")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_tickets_portal_ticket ON tickets(portal, ticket_id)")

            # Indexes for znuny_articles
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_znuny_articles_ticket ON znuny_articles(ticket_id)")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_znuny_articles_created_by ON znuny_articles(created_by)")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_znuny_articles_created_at ON znuny_articles(created_at)")

            # Indexes for site_visits
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_site_visits_status ON site_visits(status)")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_site_visits_assigned_to ON site_visits(assigned_to)")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_site_visits_visit_date ON site_visits(visit_date)")

            # Indexes for extraction_logs
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_extraction_logs_portal ON extraction_logs(portal)")
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_extraction_logs_extracted_at ON extraction_logs(extracted_at)")

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
                    ticket.portal_url,
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
                        completed_at, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                conditions.append("DATE(created_at) >= ?")
                params.append(date_from)

            if date_to:
                conditions.append("DATE(created_at) <= ?")
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
                conditions.append("DATE(za.created_at) >= ?")
                params.append(date_from)
            if date_to:
                conditions.append("DATE(za.created_at) <= ?")
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

            # First get znuny_ticket_ids for tickets being completed (for site visit completion)
            cursor.execute(f"""
                SELECT znuny_ticket_id FROM tickets
                WHERE portal = ? AND ticket_id IN ({placeholders})
                    AND status != 'Complete' AND znuny_ticket_id IS NOT NULL
            """, [portal] + list(ticket_ids))
            znuny_ids = [row["znuny_ticket_id"] for row in cursor.fetchall()]

            # Mark tickets as complete
            cursor.execute(f"""
                UPDATE tickets
                SET status = 'Complete', updated_at = ?, completed_at = ?
                WHERE portal = ? AND ticket_id IN ({placeholders}) AND status != 'Complete'
            """, [now, now, portal] + list(ticket_ids))
            count = cursor.rowcount

            # Also complete any pending site visits for these tickets
            # Duration = completion_time - (visit_date + scheduled_time)
            for znuny_id in znuny_ids:
                cursor.execute("""
                    UPDATE site_visits
                    SET ticket_completed_at = ?,
                        status = 'completed',
                        time_taken_minutes = CASE
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
                        END,
                        updated_at = ?
                    WHERE znuny_ticket_id = ? AND status = 'pending'
                """, (now, now, now, znuny_id))

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

            # Get today's date in MVT
            today = now_maldives().date().isoformat()

            # Today's extractions per portal (tickets first seen today)
            cursor.execute("""
                SELECT portal, COUNT(*) as count FROM tickets
                WHERE DATE(created_at) = ?
                GROUP BY portal
            """, (today,))
            today_extracted = {row["portal"]: row["count"] for row in cursor.fetchall()}
            today_extracted_total = sum(today_extracted.values())

            # Today's Znuny entries (tickets entered to Znuny today)
            cursor.execute("""
                SELECT COUNT(*) as count FROM tickets
                WHERE DATE(znuny_created_at) = ?
            """, (today,))
            today_znuny_entries = cursor.fetchone()["count"]

            # Znuny entries per portal today
            cursor.execute("""
                SELECT portal, COUNT(*) as count FROM tickets
                WHERE DATE(znuny_created_at) = ?
                GROUP BY portal
            """, (today,))
            today_znuny_by_portal = {row["portal"]: row["count"] for row in cursor.fetchall()}

            # Total open tickets in Znuny (active tickets that are in Znuny)
            cursor.execute("""
                SELECT COUNT(*) as count FROM tickets
                WHERE in_znuny = 1 AND completed_at IS NULL
            """)
            open_in_znuny = cursor.fetchone()["count"]

            # Site visits stats
            cursor.execute("""
                SELECT COUNT(*) as count FROM site_visits
                WHERE DATE(created_at) = ?
            """, (today,))
            today_site_visits_created = cursor.fetchone()["count"]

            cursor.execute("""
                SELECT COUNT(*) as count FROM site_visits
                WHERE status = 'completed' AND DATE(visit_date) = ?
            """, (today,))
            today_site_visits_completed = cursor.fetchone()["count"]

            cursor.execute("""
                SELECT COUNT(*) as count FROM site_visits
                WHERE status = 'pending'
            """)
            pending_site_visits = cursor.fetchone()["count"]

            # Today's articles created in Znuny
            cursor.execute("""
                SELECT COUNT(*) as count FROM znuny_articles
                WHERE DATE(created_at) = ?
            """, (today,))
            today_articles_created = cursor.fetchone()["count"]

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
                "today_articles_created": today_articles_created
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
                WHERE DATE(created_at) = ?
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
                date_filter += " AND DATE(znuny_created_at) >= ?"
                params.append(date_from)
            if date_to:
                date_filter += " AND DATE(znuny_created_at) <= ?"
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
                article_date_filter += " AND DATE(created_at) >= ?"
                article_params.append(date_from)
            if date_to:
                article_date_filter += " AND DATE(created_at) <= ?"
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
                znuny_only_date_filter += " AND DATE(created_at) >= ?"
                znuny_only_params.append(date_from)
            if date_to:
                znuny_only_date_filter += " AND DATE(created_at) <= ?"
                znuny_only_params.append(date_to)

            cursor.execute(f"""
                SELECT created_by as staff, COUNT(*) as znuny_only_count
                FROM znuny_tickets
                WHERE created_by IS NOT NULL AND created_by != ''
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
                        "tickets_updated": 0,
                        "znuny_only_count": 0
                    }
                staff_tickets[staff]["site_visits_total"] = row["site_visits_total"]
                staff_tickets[staff]["site_visits_completed"] = row["site_visits_completed"] or 0
                staff_tickets[staff]["avg_visit_time"] = round(row["avg_visit_time"], 1) if row["avg_visit_time"] else 0

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
                query += " AND DATE(znuny_created_at) >= ?"
                params.append(date_from)
            if date_to:
                query += " AND DATE(znuny_created_at) <= ?"
                params.append(date_to)

            # Get total count
            count_query = query.replace("SELECT *,", "SELECT COUNT(*) FROM (SELECT *,").replace("FROM tickets", "FROM tickets) sub")
            cursor.execute(count_query.split("FROM (SELECT")[0] + " FROM tickets WHERE znuny_created_by = ?" +
                          (" AND DATE(znuny_created_at) >= ?" if date_from else "") +
                          (" AND DATE(znuny_created_at) <= ?" if date_to else ""),
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
                    DATE(znuny_created_at) as date,
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
                    AND DATE(znuny_created_at) >= DATE('now', ? || ' days')
                GROUP BY DATE(znuny_created_at)
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
                query += " AND DATE(znuny_created_at) >= ?"
                params.append(date_from)
            if date_to:
                query += " AND DATE(znuny_created_at) <= ?"
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
                          ticket_id: int = None, znuny_url: str = None) -> int:
        """Insert or update a site visit record."""
        now = now_maldives()
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                INSERT INTO site_visits
                    (ticket_id, znuny_ticket_id, article_id, site_type, service_provider,
                     scheduled_time, assigned_to, visit_date, article_created_at, znuny_url, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(znuny_ticket_id, article_id) DO UPDATE SET
                    site_type = excluded.site_type,
                    service_provider = excluded.service_provider,
                    scheduled_time = excluded.scheduled_time,
                    assigned_to = excluded.assigned_to,
                    visit_date = excluded.visit_date,
                    article_created_at = excluded.article_created_at,
                    znuny_url = excluded.znuny_url,
                    updated_at = excluded.updated_at
            """, (ticket_id, znuny_ticket_id, article_id, site_type, service_provider,
                  scheduled_time, assigned_to, visit_date, article_created_at, znuny_url, now, now))
            return cursor.lastrowid

    def update_site_visit_completion(self, znuny_ticket_id: str, completed_at: datetime):
        """Update site visits when ticket is completed."""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            # Calculate duration from scheduled visit time to completion time
            # Duration = completed_at - (visit_date + scheduled_time)
            cursor.execute("""
                UPDATE site_visits
                SET ticket_completed_at = ?,
                    status = 'completed',
                    time_taken_minutes = CASE
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
                    END,
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
            cursor.execute("""
                UPDATE site_visits
                SET ticket_completed_at = ?,
                    status = 'completed',
                    time_taken_minutes = CASE
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
                    END,
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
            cursor.execute("""
                UPDATE site_visits
                SET status = 'completed',
                    ticket_completed_at = ?,
                    time_taken_minutes = CASE
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
                    END,
                    updated_at = ?
                WHERE znuny_ticket_id = ? AND status = 'pending'
            """, (complete_time, complete_time, now, znuny_ticket_id))
            count = cursor.rowcount
            if count > 0:
                logger.info(f"Marked {count} site visits as completed for closed ticket {znuny_ticket_id}")
            return count

    def get_synced_znuny_ticket_ids(self) -> set:
        """
        Get set of Znuny ticket IDs that have been fully synced.
        A ticket is considered synced if it has at least one site visit extracted.
        This is used to skip re-processing tickets during sync.
        """
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT DISTINCT znuny_ticket_id FROM site_visits
                WHERE znuny_ticket_id IS NOT NULL
            """)
            return {row[0] for row in cursor.fetchall()}

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
                "znuny_url": row["znuny_url"]
            } for row in cursor.fetchall()]

    def get_site_visits(self, date_from: str = None, date_to: str = None,
                        assigned_to: str = None, status: str = None,
                        limit: int = 100, offset: int = 0) -> dict:
        """Get site visits with optional filters."""
        with self._get_connection() as conn:
            cursor = conn.cursor()

            query = """
                SELECT sv.*, t.portal, t.address, t.customer_name, t.ticket_id as portal_ticket_id
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
                query += " AND sv.assigned_to = ?"
                params.append(assigned_to)
            if status:
                query += " AND sv.status = ?"
                params.append(status)

            # Get total count
            count_query = query.replace("SELECT sv.*, t.portal, t.address, t.customer_name, t.ticket_id as portal_ticket_id", "SELECT COUNT(*)")
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
                    "portal": row["portal"],
                    "address": row["address"],
                    "customer_name": row["customer_name"],
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

            return [{
                "assigned_to": row["assigned_to"],
                "total_visits": row["total_visits"],
                "completed": row["completed"] or 0,
                "pending": row["pending"] or 0,
                "avg_time_minutes": round(row["avg_time"], 1) if row["avg_time"] else None,
                "min_time_minutes": row["min_time"],
                "max_time_minutes": row["max_time"]
            } for row in cursor.fetchall()]

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

            # Today's counts
            cursor.execute("""
                SELECT level, COUNT(*) as count FROM system_logs
                WHERE DATE(created_at) = DATE('now')
                GROUP BY level
            """)
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

    def clear_old_logs(self, days: int = 30) -> int:
        """Clear system logs older than specified days. Returns count deleted."""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                DELETE FROM system_logs
                WHERE created_at < datetime('now', ? || ' days')
            """, (f"-{days}",))
            deleted = cursor.rowcount
            logger.info(f"Cleared {deleted} system logs older than {days} days")
            return deleted

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
        """Get performance threshold settings."""
        return {
            "good": int(self.get_setting("perf_threshold_good", "5")),
            "warning": int(self.get_setting("perf_threshold_warning", "10")),
            "bad": int(self.get_setting("perf_threshold_bad", "30")),
            "critical": int(self.get_setting("perf_threshold_critical", "60"))
        }

    def set_performance_thresholds(self, good: int, warning: int, bad: int, critical: int):
        """Set performance threshold settings."""
        self.set_setting("perf_threshold_good", str(good))
        self.set_setting("perf_threshold_warning", str(warning))
        self.set_setting("perf_threshold_bad", str(bad))
        self.set_setting("perf_threshold_critical", str(critical))

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

    def upsert_znuny_only_ticket(self, data: dict) -> int:
        """Insert or update a Znuny-only ticket (not linked to ISP)."""
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
                    (znuny_ticket_id, title, state, queue, priority, created_at, created_by,
                     closed_at, time_to_close_minutes, article_count, last_article_by,
                     last_article_at, znuny_url, first_seen_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(znuny_ticket_id) DO UPDATE SET
                    title = excluded.title,
                    state = excluded.state,
                    queue = excluded.queue,
                    priority = excluded.priority,
                    closed_at = excluded.closed_at,
                    time_to_close_minutes = excluded.time_to_close_minutes,
                    article_count = excluded.article_count,
                    last_article_by = excluded.last_article_by,
                    last_article_at = excluded.last_article_at,
                    znuny_url = excluded.znuny_url,
                    updated_at = excluded.updated_at
            """, (
                data.get("znuny_ticket_id"),
                data.get("title"),
                data.get("state"),
                data.get("queue"),
                data.get("priority"),
                data.get("created_at"),
                data.get("created_by"),
                data.get("closed_at"),
                time_to_close,
                data.get("article_count", 0),
                data.get("last_article_by"),
                data.get("last_article_at"),
                data.get("znuny_url"),
                now,
                now
            ))
            return cursor.lastrowid

    def get_znuny_only_tickets(self, state: str = None, created_by: str = None,
                                date_from: str = None, date_to: str = None,
                                limit: int = 100, offset: int = 0) -> dict:
        """Get Znuny-only tickets with optional filters."""
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

            if date_from:
                query += " AND DATE(created_at) >= ?"
                params.append(date_from)

            if date_to:
                query += " AND DATE(created_at) <= ?"
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
                "priority": row["priority"],
                "created_at": row["created_at"],
                "created_by": row["created_by"],
                "closed_at": row["closed_at"],
                "time_to_close_minutes": row["time_to_close_minutes"],
                "article_count": row["article_count"],
                "last_article_by": row["last_article_by"],
                "last_article_at": row["last_article_at"],
                "znuny_url": row["znuny_url"],
                "first_seen_at": row["first_seen_at"],
                "updated_at": row["updated_at"]
            } for row in cursor.fetchall()]

            return {"total": total, "tickets": tickets}

    def get_znuny_only_stats(self) -> dict:
        """Get summary statistics for Znuny-only tickets."""
        with self._get_connection() as conn:
            cursor = conn.cursor()

            # Total count
            cursor.execute("SELECT COUNT(*) as total FROM znuny_tickets")
            total = cursor.fetchone()["total"]

            # Open count (not closed/resolved)
            cursor.execute("""
                SELECT COUNT(*) as count FROM znuny_tickets
                WHERE state IS NULL OR LOWER(state) NOT IN ('closed', 'resolved')
            """)
            open_count = cursor.fetchone()["count"]

            # Closed count
            cursor.execute("""
                SELECT COUNT(*) as count FROM znuny_tickets
                WHERE LOWER(state) IN ('closed', 'resolved')
            """)
            closed_count = cursor.fetchone()["count"]

            # Today's date in MVT
            today = now_maldives().date().isoformat()

            # Today's new (first seen today)
            cursor.execute("""
                SELECT COUNT(*) as count FROM znuny_tickets
                WHERE DATE(first_seen_at) = ?
            """, (today,))
            today_new = cursor.fetchone()["count"]

            # Avg time to close (for closed tickets)
            cursor.execute("""
                SELECT AVG(time_to_close_minutes) as avg_time FROM znuny_tickets
                WHERE time_to_close_minutes IS NOT NULL
            """)
            avg_close_time = cursor.fetchone()["avg_time"]

            return {
                "total": total,
                "open": open_count,
                "closed": closed_count,
                "today_new": today_new,
                "avg_close_time_minutes": round(avg_close_time, 1) if avg_close_time else None
            }

    def get_znuny_only_staff_stats(self, date_from: str = None, date_to: str = None) -> list:
        """Get staff statistics for Znuny-only tickets."""
        with self._get_connection() as conn:
            cursor = conn.cursor()

            query = """
                SELECT
                    created_by,
                    COUNT(*) as total_tickets,
                    SUM(CASE WHEN LOWER(state) IN ('closed', 'resolved') THEN 1 ELSE 0 END) as closed_tickets,
                    AVG(CASE WHEN time_to_close_minutes IS NOT NULL THEN time_to_close_minutes END) as avg_close_time,
                    SUM(article_count) as total_articles
                FROM znuny_tickets
                WHERE created_by IS NOT NULL AND created_by != ''
            """
            params = []

            if date_from:
                query += " AND DATE(created_at) >= ?"
                params.append(date_from)
            if date_to:
                query += " AND DATE(created_at) <= ?"
                params.append(date_to)

            query += " GROUP BY created_by ORDER BY total_tickets DESC"
            cursor.execute(query, params)

            return [{
                "created_by": row["created_by"],
                "total_tickets": row["total_tickets"],
                "closed_tickets": row["closed_tickets"] or 0,
                "open_tickets": row["total_tickets"] - (row["closed_tickets"] or 0),
                "avg_close_time_minutes": round(row["avg_close_time"], 1) if row["avg_close_time"] else None,
                "total_articles": row["total_articles"] or 0
            } for row in cursor.fetchall()]

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

            # 4. Site visits (assigned_to)
            cursor.execute("""
                SELECT assigned_to as name, COUNT(*) as count
                FROM site_visits
                WHERE assigned_to IS NOT NULL AND assigned_to != ''
                GROUP BY assigned_to
            """)
            for row in cursor.fetchall():
                name = row["name"]
                if name not in staff_data:
                    staff_data[name] = {"name": name, "isp_tickets": 0, "znuny_tickets": 0, "articles": 0, "site_visits": 0}
                staff_data[name]["site_visits"] = row["count"]

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

            # Count affected site visits
            cursor.execute("""
                SELECT COUNT(*) as count FROM site_visits
                WHERE assigned_to = ?
            """, (source_name,))
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

            # 5. Update site visits (assigned_to)
            cursor.execute("""
                UPDATE site_visits SET assigned_to = ?, updated_at = ?
                WHERE assigned_to = ?
            """, (target_name, now_maldives(), source_name))
            result["updated"]["site_visits"] = cursor.rowcount

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
                query += " AND DATE(created_at) >= ?"
                params.append(date_from)
            if date_to:
                query += " AND DATE(created_at) <= ?"
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
                query += " AND DATE(a.created_at) >= ?"
                params.append(date_from)
            if date_to:
                query += " AND DATE(a.created_at) <= ?"
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
                query += " AND DATE(created_at) >= ?"
                params.append(date_from)
            if date_to:
                query += " AND DATE(created_at) <= ?"
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
