"""
Admin Controller - Handles admin panel routes.
"""

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import JSONResponse
from typing import Optional
import threading

from database import Database
from services import ExtractionService, StatsService, ZnunyService
from config import Config
from utils.logger import get_logger

router = APIRouter(prefix="/api/admin")
logger = get_logger("admin_controller")

# Global for extraction status
_extraction_running = False


def get_db():
    """Get database instance."""
    return Database()


@router.get("/scheduler-status")
async def get_scheduler_status():
    """Get scheduler status information."""
    try:
        import schedule
        jobs = schedule.get_jobs()
        next_run = None
        if jobs:
            next_run = str(jobs[0].next_run) if jobs[0].next_run else None

        return JSONResponse(content={
            "running": len(jobs) > 0,
            "interval_minutes": Config.EXTRACTION_INTERVAL_MINUTES,
            "next_run": next_run
        })
    except Exception as e:
        logger.error(f"Error getting scheduler status: {e}")
        return JSONResponse(content={
            "running": False,
            "interval_minutes": Config.EXTRACTION_INTERVAL_MINUTES,
            "next_run": None
        })


@router.post("/trigger-extraction")
async def trigger_extraction():
    """Manually trigger extraction."""
    global _extraction_running

    if _extraction_running:
        return JSONResponse(content={
            "success": False,
            "message": "Extraction already in progress"
        })

    try:
        _extraction_running = True

        def run_extraction():
            global _extraction_running
            try:
                service = ExtractionService()
                service.extract_from_all_portals()

                # Also sync Znuny
                znuny_service = ZnunyService()
                znuny_service.sync_unchecked_tickets()
            finally:
                _extraction_running = False

        thread = threading.Thread(target=run_extraction)
        thread.start()

        return JSONResponse(content={
            "success": True,
            "message": "Extraction triggered"
        })
    except Exception as e:
        _extraction_running = False
        logger.error(f"Error triggering extraction: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/login-summary")
async def get_login_summary():
    """Get login statistics summary."""
    try:
        service = StatsService()
        return JSONResponse(content=service.get_login_summary())
    except Exception as e:
        logger.error(f"Error getting login summary: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/login-stats")
async def get_login_stats(
    limit: int = Query(default=100, le=500)
):
    """Get login event history."""
    try:
        db = get_db()
        logs = db.get_login_stats(limit=limit)
        return JSONResponse(content={"logs": logs})
    except Exception as e:
        logger.error(f"Error getting login stats: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/system-logs")
async def get_system_logs(
    level: Optional[str] = None,
    search: Optional[str] = None,
    limit: int = Query(default=50, le=500),
    offset: int = 0
):
    """Get system logs."""
    try:
        db = get_db()
        result = db.get_system_logs(
            level=level,
            search=search,
            limit=limit,
            offset=offset
        )
        return JSONResponse(content=result)
    except Exception as e:
        logger.error(f"Error getting system logs: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/log-stats")
async def get_log_stats():
    """Get log statistics."""
    try:
        db = get_db()
        stats = db.get_log_stats()
        return JSONResponse(content=stats)
    except Exception as e:
        logger.error(f"Error getting log stats: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/clear-old-logs")
async def clear_old_logs(days: int = 30):
    """Clear logs older than specified days."""
    try:
        db = get_db()
        deleted = db.clear_old_logs(days=days)
        return JSONResponse(content={
            "success": True,
            "deleted": deleted
        })
    except Exception as e:
        logger.error(f"Error clearing old logs: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/delayed-tickets")
async def get_delayed_tickets(
    min_delay: int = Query(default=5, description="Minimum delay in minutes"),
    date_from: Optional[str] = None,
    date_to: Optional[str] = None
):
    """Get analysis of delayed tickets."""
    try:
        service = StatsService()
        data = service.get_delayed_tickets_analysis(
            min_delay_minutes=min_delay,
            date_from=date_from,
            date_to=date_to
        )
        return JSONResponse(content=data)
    except Exception as e:
        logger.error(f"Error getting delayed tickets: {e}")
        raise HTTPException(status_code=500, detail=str(e))
