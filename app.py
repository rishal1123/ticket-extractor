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
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from config import Config
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

logger = get_logger("app")


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Manage app lifespan - start scheduler on startup."""
    # Startup
    logger.info("Starting Ticket Extractor application")

    # Initialize database
    db = Database()
    logger.info("Database initialized")

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
