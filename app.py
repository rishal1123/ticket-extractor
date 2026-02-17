"""
FastAPI Application - Main entry point following MVC architecture.

Structure:
- controllers/ - Route handlers (HTTP layer)
- services/    - Business logic layer
- database.py  - Data access layer (Repository)
- models/      - Data models
- templates/   - Jinja2 views
"""

import os
import sys
import platform
from datetime import datetime, timezone, timedelta
from contextlib import asynccontextmanager

import psutil
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from config import Config, APP_VERSION
from database import Database
from controllers import (
    pages_router,
    api_router,
    admin_router,
    settings_router,
    field_visits_router,
    znuny_only_router
)
from services.scheduler_service import get_scheduler
from utils.logger import setup_logger, get_logger
from middleware.security import SecurityMiddleware

logger = get_logger("app")

MVT = timezone(timedelta(hours=5))


def _dir_size_mb(path: str) -> float:
    """Get total size of a directory in MB."""
    total = 0
    try:
        for dirpath, _, filenames in os.walk(path):
            for f in filenames:
                try:
                    total += os.path.getsize(os.path.join(dirpath, f))
                except OSError:
                    pass
    except OSError:
        pass
    return total / (1024 * 1024)


def _format_ago(iso_str: str) -> str:
    """Format an ISO timestamp as 'Xm ago' or 'Xh ago' relative to now."""
    if not iso_str:
        return "never"
    try:
        dt = datetime.fromisoformat(str(iso_str))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=MVT)
        diff = datetime.now(MVT) - dt
        minutes = diff.total_seconds() / 60
        if minutes < 1:
            return "just now"
        if minutes < 60:
            return f"{int(minutes)}m ago"
        if minutes < 1440:
            return f"{int(minutes // 60)}h {int(minutes % 60)}m ago"
        return f"{int(minutes // 1440)}d ago"
    except Exception:
        return "unknown"


def startup_check(db: Database):
    """Run startup diagnostics and log system state."""
    lines = []
    summary_parts = []

    W = 60  # box width

    def heading(text):
        lines.append(f"  {text}")

    def line(text):
        lines.append(f"    {text}")

    def blank():
        lines.append("")

    # ── Header ──
    lines.append("")
    lines.append("=" * W)
    lines.append(f"  Ticket Extractor v{APP_VERSION} - Startup Check")
    lines.append("=" * W)

    # ── System ──
    proc = psutil.Process()
    mem_mb = proc.memory_info().rss / (1024 * 1024)
    now = datetime.now(MVT)

    heading("System")
    line(f"PID: {os.getpid()} | Python {sys.version.split()[0]} | {platform.system()} {platform.release()}")
    line(f"Memory: {mem_mb:.0f} MB | Time: {now.strftime('%Y-%m-%d %H:%M')} MVT")
    blank()

    # ── Database ──
    db_path = Config.DATABASE_PATH
    db_exists = os.path.exists(db_path)
    db_size_mb = os.path.getsize(db_path) / (1024 * 1024) if db_exists else 0

    heading(f"Database ({os.path.basename(db_path)} - {db_size_mb:.1f} MB)")

    try:
        stats = db.get_stats()
        active = stats.get("total", 0)
        completed = stats.get("completed", 0)
        pending_sv = stats.get("pending_site_visits", 0)
        today_extracted = stats.get("today_extracted_total", 0)

        # Get table counts via direct queries for tables not in get_stats()
        with db._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT COUNT(*) as c FROM znuny_tickets")
            znuny_count = cursor.fetchone()["c"]
            cursor.execute("SELECT COUNT(*) as c FROM znuny_articles")
            article_count = cursor.fetchone()["c"]
            cursor.execute("SELECT COUNT(*) as c FROM site_visits")
            sv_count = cursor.fetchone()["c"]

        line(f"Tickets: {active} active, {completed} completed | Znuny: {znuny_count}")
        line(f"Articles: {article_count:,} | Site Visits: {sv_count} ({pending_sv} pending)")
        line(f"Today: {today_extracted} extracted, {stats.get('today_articles_created', 0)} articles")
        summary_parts.append(f"{active} active tickets")
    except Exception as e:
        line(f"ERROR reading stats: {e}")
        summary_parts.append("DB stats error")
    blank()

    # ── Portal Credentials ──
    heading("Portals")
    try:
        config_settings = db.get_config_settings()
        configured_portals = []
        missing_portals = []
        for portal in ("DHIRAAGU", "OOREDOO", "ROL", "MEDIANET"):
            has_url = bool(config_settings.get(f"{portal}_URL"))
            has_user = bool(config_settings.get(f"{portal}_USERNAME"))
            if has_url and has_user:
                configured_portals.append(portal.capitalize())
                line(f"  + {portal.capitalize():10s} - credentials configured")
            else:
                missing_portals.append(portal.capitalize())
                missing = []
                if not has_url:
                    missing.append("URL")
                if not has_user:
                    missing.append("username")
                line(f"  - {portal.capitalize():10s} - missing {', '.join(missing)}")

        # Znuny
        has_znuny = bool(config_settings.get("ZNUNY_URL")) and bool(config_settings.get("ZNUNY_USERNAME"))
        if has_znuny:
            configured_portals.append("Znuny")
            line(f"  + {'Znuny':10s} - credentials configured")
        else:
            missing_portals.append("Znuny")
            line(f"  - {'Znuny':10s} - missing credentials")

        summary_parts.append(f"{len(configured_portals)} portals configured")
        if missing_portals:
            summary_parts.append(f"missing: {', '.join(missing_portals)}")
    except Exception as e:
        line(f"ERROR reading config: {e}")
    blank()

    # ── Schedule ──
    heading("Schedule")
    ext_interval = Config.get_extraction_interval()
    sync_interval = Config.get_znuny_sync_interval()
    line(f"Extraction: every {ext_interval} min | Znuny sync: every {sync_interval} min")

    try:
        hours = db.get_operating_hours()
        if hours["enabled"]:
            current_hour = now.hour
            within = hours["start_hour"] <= current_hour < hours["end_hour"]
            status = "active" if within else "paused"
            line(f"Operating hours: {hours['start_hour']:02d}:00-{hours['end_hour']:02d}:00 MVT ({status})")
            if not within:
                summary_parts.append("outside operating hours")
        else:
            line("Operating hours: disabled (runs 24/7)")
    except Exception:
        line("Operating hours: unknown")
    blank()

    # ── Browser Sessions ──
    heading("Browser Sessions")
    data_dir = os.path.join(os.path.dirname(__file__), "data")
    sessions_dir = os.path.join(data_dir, "browser_sessions")
    session_parts = []
    for portal in ("dhiraagu", "ooredoo", "rol", "medianet", "znuny"):
        portal_dir = os.path.join(sessions_dir, portal)
        if os.path.isdir(portal_dir):
            size = _dir_size_mb(portal_dir)
            session_parts.append(f"{portal}: {size:.0f} MB")
        else:
            session_parts.append(f"{portal}: --")

    # Print in 2-3 per line
    for i in range(0, len(session_parts), 3):
        line(" | ".join(session_parts[i:i+3]))
    blank()

    # ── Last Activity ──
    heading("Last Activity")
    try:
        last_ext = stats.get("last_extraction", {}) if 'stats' in dir() else db.get_last_extraction_per_portal()
        if last_ext:
            for portal, info in last_ext.items():
                ago = _format_ago(info.get("extracted_at"))
                found = info.get("tickets_found", 0)
                new = info.get("tickets_new", 0)
                status = info.get("status", "")
                if status == "failed":
                    line(f"  {portal.capitalize():10s}: {ago} (FAILED: {info.get('error_message', '')[:40]})")
                else:
                    line(f"  {portal.capitalize():10s}: {ago} ({found} found, {new} new)")
        else:
            line("No extraction history yet")
    except Exception:
        line("Could not read extraction history")
    blank()

    # ── Footer ──
    lines.append("=" * W)
    lines.append(f"  Server: http://{Config.DASHBOARD_HOST}:{Config.DASHBOARD_PORT}")
    lines.append("=" * W)
    lines.append("")

    # Log the full banner
    banner = "\n".join(lines)
    logger.info("Startup diagnostics:\n%s", banner)

    # Write summary to system_logs table
    try:
        summary = " | ".join(summary_parts) if summary_parts else "Startup complete"
        db.log_system("info", "startup", f"v{APP_VERSION} started", summary)
    except Exception:
        pass


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Manage app lifespan - start scheduler on startup."""
    # Startup
    logger.info("Starting Ticket Extractor application")

    # Initialize database
    db = Database()
    logger.info("Database initialized")

    # Run startup diagnostics
    startup_check(db)

    # Start background scheduler
    scheduler = get_scheduler()
    scheduler.start()
    logger.info("Background scheduler started")

    yield

    # Shutdown
    logger.info("Shutting down application")
    scheduler.stop()


def create_app() -> FastAPI:
    """Create and configure the FastAPI application."""
    app = FastAPI(
        title="Ticket Extractor",
        description="Extract tickets from ISP portals and sync with Znuny",
        version="1.0.0",
        lifespan=lifespan
    )

    # Add security middleware (rate limiting, security headers)
    app.add_middleware(SecurityMiddleware)

    # Mount static files
    static_dir = os.path.join(os.path.dirname(__file__), "static")
    if os.path.exists(static_dir):
        app.mount("/static", StaticFiles(directory=static_dir), name="static")

    # Include routers (controllers)
    # Pages router (HTML pages - no prefix)
    app.include_router(pages_router)

    # API routers
    app.include_router(api_router)              # /api/*
    app.include_router(admin_router)            # /api/admin/*
    app.include_router(settings_router)         # /api/settings/*
    app.include_router(field_visits_router)     # /api/field-visits/*
    app.include_router(znuny_only_router)       # /api/znuny-only/*

    return app


# Create application instance
app = create_app()


def run_app():
    """Run the application."""
    import uvicorn

    setup_logger()
    logger.info(f"Starting server at http://{Config.DASHBOARD_HOST}:{Config.DASHBOARD_PORT}")

    uvicorn.run(
        app,
        host=Config.DASHBOARD_HOST,
        port=Config.DASHBOARD_PORT,
        log_level="info"
    )


if __name__ == "__main__":
    run_app()
