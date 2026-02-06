"""
Admin Controller - Handles admin panel routes.
"""

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from typing import Optional
import threading

from database import Database
from services import ExtractionService, StatsService, ZnunyService
from services.scheduler_service import get_scheduler
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
    """Get scheduler status and next run time."""
    try:
        import schedule as sched_lib
        scheduler = get_scheduler()
        status = scheduler.get_status()

        jobs = sched_lib.get_jobs()
        next_run = None
        if jobs:
            next_run = str(jobs[0].next_run) if jobs[0].next_run else None

        return JSONResponse(content={
            "running": status["running"],
            "interval_minutes": status["interval_minutes"],
            "jobs_count": len(jobs),
            "next_run": next_run
        })
    except Exception as e:
        logger.error(f"Error getting scheduler status: {e}")
        return JSONResponse(content={
            "running": False,
            "interval_minutes": Config.EXTRACTION_INTERVAL_MINUTES,
            "jobs_count": 0,
            "next_run": None,
            "error": str(e)
        })


@router.post("/trigger-extraction")
async def trigger_extraction():
    """Manually trigger portal extraction and Znuny sync."""
    try:
        scheduler = get_scheduler()

        # Run extraction and sync in a background thread
        def run_async():
            scheduler.run_portal_extraction()
            scheduler.run_znuny_sync()

        thread = threading.Thread(target=run_async, daemon=True)
        thread.start()

        return JSONResponse(content={
            "success": True,
            "message": "Extraction triggered in background"
        })
    except Exception as e:
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


@router.post("/login")
async def admin_login(request: Request):
    """Authenticate admin user."""
    try:
        data = await request.json()
        password = data.get("password", "").strip()

        db = get_db()
        stored_password = db.get_setting("admin_password", "admin123")

        if password == stored_password:
            return JSONResponse(content={"success": True, "message": "Login successful"})
        else:
            return JSONResponse(content={"success": False, "message": "Invalid password"})
    except Exception as e:
        logger.error(f"Error in admin login: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/change-password")
async def change_admin_password(request: Request):
    """Change admin password."""
    try:
        data = await request.json()
        current_password = data.get("current_password", "")
        new_password = data.get("new_password", "")

        db = get_db()
        stored_password = db.get_setting("admin_password", "admin123")

        if current_password != stored_password:
            return JSONResponse(content={"success": False, "message": "Current password is incorrect"})

        if len(new_password) < 4:
            return JSONResponse(content={"success": False, "message": "Password must be at least 4 characters"})

        db.set_setting("admin_password", new_password, "Password for admin panel access")
        logger.info("Admin password changed")
        return JSONResponse(content={"success": True, "message": "Password changed successfully"})
    except Exception as e:
        logger.error(f"Error changing admin password: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# Settings router (separate prefix for /api/settings routes)
settings_router = APIRouter(prefix="/api/settings")


@settings_router.get("/performance-thresholds")
async def get_performance_thresholds():
    """Get performance threshold settings."""
    try:
        db = get_db()
        thresholds = db.get_performance_thresholds()
        return JSONResponse(content={"success": True, "thresholds": thresholds})
    except Exception as e:
        logger.error(f"Error getting performance thresholds: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@settings_router.post("/performance-thresholds")
async def update_performance_thresholds(request: Request):
    """Update performance threshold settings."""
    try:
        data = await request.json()
        db = get_db()
        db.set_performance_thresholds(
            good=int(data.get("good", 5)),
            warning=int(data.get("warning", 10)),
            bad=int(data.get("bad", 30)),
            critical=int(data.get("critical", 60))
        )
        logger.info(f"Performance thresholds updated: {data}")
        return JSONResponse(content={"success": True, "message": "Thresholds saved"})
    except Exception as e:
        logger.error(f"Error updating performance thresholds: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@settings_router.get("")
async def get_all_settings():
    """Get all app settings."""
    try:
        db = get_db()
        settings = db.get_all_settings()
        return JSONResponse(content={"success": True, "settings": settings})
    except Exception as e:
        logger.error(f"Error getting settings: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# ==================== Staff Management ====================

@router.get("/staff-list")
async def get_staff_list():
    """Get all unique staff names with counts from all tables."""
    try:
        db = get_db()
        staff = db.get_all_staff_names_with_counts()
        return JSONResponse(content={"success": True, "staff": staff})
    except Exception as e:
        logger.error(f"Error getting staff list: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/staff-merge-preview")
async def get_staff_merge_preview(
    source: str = Query(..., description="Staff name to merge from"),
    target: str = Query(..., description="Staff name to merge into")
):
    """Preview the effect of merging one staff name into another."""
    try:
        if source == target:
            return JSONResponse(content={
                "success": False,
                "message": "Source and target cannot be the same"
            })

        db = get_db()
        preview = db.get_staff_merge_preview(source, target)
        return JSONResponse(content={"success": True, "preview": preview})
    except Exception as e:
        logger.error(f"Error getting staff merge preview: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/staff-merge")
async def merge_staff(request: Request):
    """Merge one staff name into another across all tables."""
    try:
        data = await request.json()
        source = data.get("source", "").strip()
        target = data.get("target", "").strip()

        if not source or not target:
            return JSONResponse(content={
                "success": False,
                "message": "Both source and target names are required"
            })

        if source == target:
            return JSONResponse(content={
                "success": False,
                "message": "Source and target cannot be the same"
            })

        db = get_db()
        result = db.merge_staff_names(source, target)

        logger.info(f"Staff merge completed: '{source}' -> '{target}', {result['total_updated']} records updated")

        return JSONResponse(content={
            "success": True,
            "message": f"Merged '{source}' into '{target}'",
            "result": result
        })
    except Exception as e:
        logger.error(f"Error merging staff: {e}")
        raise HTTPException(status_code=500, detail=str(e))
