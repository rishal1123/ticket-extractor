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
import time
import threading
from contextlib import asynccontextmanager

import schedule
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from config import Config
from database import Database
from controllers import pages_router, api_router, admin_router
from services import ExtractionService, ZnunyService
from utils.logger import setup_logger, get_logger

logger = get_logger("app")


def scheduled_extraction():
    """Run scheduled extraction task."""
    logger.info("Starting scheduled extraction")
    db = Database()

    try:
        extraction_service = ExtractionService(db)
        results = extraction_service.extract_from_all_portals(headless=True)

        total_found = results.get("total_found", 0)
        total_new = results.get("total_new", 0)

        logger.info(f"Extraction complete: {total_found} found, {total_new} new")

        # Sync Znuny for newly found tickets
        try:
            znuny_service = ZnunyService(db)
            znuny_service.sync_unchecked_tickets()
        except Exception as e:
            logger.error(f"Znuny sync failed: {e}")

    except Exception as e:
        logger.error(f"Scheduled extraction failed: {e}")


def run_scheduler():
    """Run the background scheduler."""
    interval = Config.EXTRACTION_INTERVAL_MINUTES
    logger.info(f"Starting scheduler with {interval} minute interval")

    # Schedule periodic runs
    schedule.every(interval).minutes.do(scheduled_extraction)

    while True:
        schedule.run_pending()
        time.sleep(1)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifespan events."""
    # Startup
    logger.info("Starting Ticket Extractor application")

    # Initialize database
    db = Database()
    logger.info("Database initialized")

    # Run initial extraction
    logger.info("Running initial extraction...")
    threading.Thread(target=scheduled_extraction, daemon=True).start()

    # Start background scheduler
    scheduler_thread = threading.Thread(target=run_scheduler, daemon=True)
    scheduler_thread.start()
    logger.info("Background scheduler started")

    yield

    # Shutdown
    logger.info("Shutting down application")
    schedule.clear()


def create_app() -> FastAPI:
    """Create and configure the FastAPI application."""
    app = FastAPI(
        title="Ticket Extractor",
        description="Extract tickets from ISP portals and sync with Znuny",
        version="1.0.0",
        lifespan=lifespan
    )

    # Mount static files
    static_dir = os.path.join(os.path.dirname(__file__), "static")
    if os.path.exists(static_dir):
        app.mount("/static", StaticFiles(directory=static_dir), name="static")

    # Include routers (controllers)
    app.include_router(pages_router)
    app.include_router(api_router)
    app.include_router(admin_router)

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
