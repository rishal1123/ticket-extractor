"""
Admin Controller - Handles admin panel routes.
"""

import os
import secrets
import tempfile

from fastapi import APIRouter, HTTPException, Query, Request, Depends, Header
from fastapi.responses import JSONResponse, FileResponse
from starlette.background import BackgroundTask
from typing import Optional

from database import Database, now_maldives
from services import ExtractionService, StatsService, ZnunyService
from services.scheduler_service import get_scheduler
from config import Config
from utils.logger import get_logger
from .dependencies import (
    get_db,
    handle_errors,
    get_date_filter,
    DateFilterParams,
    success_response
)

router = APIRouter(prefix="/api/admin", tags=["Admin"])
logger = get_logger("admin_controller")

# Global for extraction status
_extraction_running = False


@router.get("/flaresolverr-status")
@handle_errors("flaresolverr status")
async def flaresolverr_status():
    """Report FlareSolverr availability (used to bypass Cloudflare for Dhiraagu)."""
    from utils.flaresolverr import FlareSolverrClient
    url = Config.get_flaresolverr_url()
    if not url:
        return JSONResponse(content={
            "configured": False, "url": "", "reachable": False,
            "version": None, "message": "FLARESOLVERR_URL not set",
        })
    health = FlareSolverrClient(url).health()
    return JSONResponse(content={"configured": True, "url": url, **health})


@router.post("/generate-staff-snapshots")
@handle_errors("generate staff snapshots")
async def generate_staff_snapshots(
    date: Optional[str] = Query(None, description="YYYY-MM-DD; omit to regenerate all activity dates"),
    db: Database = Depends(get_db),
):
    """(Re)generate per-staff daily stats snapshots. With ?date=YYYY-MM-DD it
    regenerates that day; with no date it backfills every day that has activity."""
    if date:
        count = db.generate_staff_daily_snapshot(date)
        return success_response({"dates": 1, "date": date, "staff": count})
    dates = db.get_staff_activity_dates()
    total = 0
    for d in dates:
        total += db.generate_staff_daily_snapshot(d)
    return success_response({"dates": len(dates), "staff_rows": total})


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

        result = {
            "running": status["running"],
            "extraction_interval_minutes": status["extraction_interval_minutes"],
            "znuny_sync_interval_minutes": status["znuny_sync_interval_minutes"],
            "jobs_count": len(jobs),
            "next_run": next_run
        }
        if "operating_hours" in status:
            result["operating_hours"] = status["operating_hours"]
            result["within_operating_hours"] = status.get("within_operating_hours", True)
        if "isp_extraction_enabled" in status:
            result["isp_extraction_enabled"] = status["isp_extraction_enabled"]
        if "portals_enabled" in status:
            result["portals_enabled"] = status["portals_enabled"]
        return JSONResponse(content=result)
    except Exception as e:
        logger.error(f"Error getting scheduler status: {e}")
        return JSONResponse(content={
            "running": False,
            "extraction_interval_minutes": Config.get_extraction_interval(),
            "znuny_sync_interval_minutes": Config.get_znuny_sync_interval(),
            "jobs_count": 0,
            "next_run": None,
            "error": str(e)
        })


@router.post("/trigger-extraction")
async def trigger_extraction():
    """Manually trigger portal extraction and Znuny sync."""
    try:
        scheduler = get_scheduler()

        # Signal the persistent worker threads to run
        scheduler._extraction_job()
        scheduler._znuny_sync_job()

        return JSONResponse(content={
            "success": True,
            "message": "Extraction triggered in background"
        })
    except Exception as e:
        logger.error(f"Error triggering extraction: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/login-summary")
@handle_errors("get login summary")
async def get_login_summary():
    """Get login statistics summary."""
    service = StatsService()
    return JSONResponse(content=service.get_login_summary())


@router.get("/login-stats")
@handle_errors("get login stats")
async def get_login_stats(
    limit: int = Query(default=100, le=500),
    db: Database = Depends(get_db)
):
    """Get login event history."""
    logs = db.get_login_stats(limit=limit)
    return JSONResponse(content={"logs": logs})


@router.get("/system-logs")
@handle_errors("get system logs")
async def get_system_logs(
    level: Optional[str] = None,
    search: Optional[str] = None,
    limit: int = Query(default=50, le=500),
    offset: int = 0,
    db: Database = Depends(get_db)
):
    """Get system logs."""
    result = db.get_system_logs(
        level=level,
        search=search,
        limit=limit,
        offset=offset
    )
    return JSONResponse(content=result)


@router.get("/log-stats")
@handle_errors("get log stats")
async def get_log_stats(db: Database = Depends(get_db)):
    """Get log statistics."""
    stats = db.get_log_stats()
    return JSONResponse(content=stats)


@router.post("/clear-old-logs")
@handle_errors("clear old logs")
async def clear_old_logs(days: int = 30, db: Database = Depends(get_db)):
    """Clear logs older than specified days."""
    deleted = db.clear_old_logs(days=days)
    return JSONResponse(content=success_response(
        f"Cleared {deleted} old logs",
        {"deleted": deleted}
    ))


@router.get("/delayed-tickets")
@handle_errors("get delayed tickets")
async def get_delayed_tickets(
    min_delay: int = Query(default=5, description="Minimum delay in minutes"),
    date_filter: DateFilterParams = Depends(get_date_filter)
):
    """Get analysis of delayed tickets."""
    service = StatsService()
    data = service.get_delayed_tickets_analysis(
        min_delay_minutes=min_delay,
        date_from=date_filter.date_from,
        date_to=date_filter.date_to
    )
    return JSONResponse(content=data)


@router.post("/login")
@handle_errors("admin login")
async def admin_login(request: Request, db: Database = Depends(get_db)):
    """Authenticate admin user."""
    data = await request.json()
    password = data.get("password", "").strip()

    stored_password = db.get_setting("admin_password", "admin123")

    if password == stored_password:
        return JSONResponse(content=success_response("Login successful"))
    else:
        return JSONResponse(content={"success": False, "message": "Invalid password"})


@router.post("/change-password")
@handle_errors("change admin password")
async def change_admin_password(request: Request, db: Database = Depends(get_db)):
    """Change admin password."""
    data = await request.json()
    current_password = data.get("current_password", "")
    new_password = data.get("new_password", "")

    stored_password = db.get_setting("admin_password", "admin123")

    if current_password != stored_password:
        return JSONResponse(content={"success": False, "message": "Current password is incorrect"})

    if len(new_password) < 4:
        return JSONResponse(content={"success": False, "message": "Password must be at least 4 characters"})

    db.set_setting("admin_password", new_password, "Password for admin panel access")
    logger.info("Admin password changed")
    return JSONResponse(content=success_response("Password changed successfully"))


@router.get("/download-db")
@handle_errors("download database")
async def download_db(x_admin_password: str = Header(default=""), db: Database = Depends(get_db)):
    """Download a snapshot of the SQLite database (contains portal credentials —
    admin password required via header, not query string, so it isn't logged/cached
    in browser history)."""
    stored_password = db.get_setting("admin_password", "admin123")
    if x_admin_password != stored_password:
        raise HTTPException(status_code=403, detail="Invalid admin password")

    tmp_fd, tmp_path = tempfile.mkstemp(suffix=".db")
    os.close(tmp_fd)
    db.create_backup(tmp_path)

    filename = f"tickets_backup_{now_maldives().strftime('%Y%m%d_%H%M%S')}.db"
    logger.info(f"Database backup downloaded as {filename}")
    return FileResponse(
        tmp_path,
        filename=filename,
        media_type="application/octet-stream",
        background=BackgroundTask(os.remove, tmp_path)
    )


# Settings router (separate prefix for /api/settings routes)
settings_router = APIRouter(prefix="/api/settings")


@settings_router.get("/performance-thresholds")
@handle_errors("get performance thresholds")
async def get_performance_thresholds(db: Database = Depends(get_db)):
    """Get performance threshold settings."""
    thresholds = db.get_performance_thresholds()
    return JSONResponse(content={"success": True, "thresholds": thresholds})


@settings_router.post("/performance-thresholds")
@handle_errors("update performance thresholds")
async def update_performance_thresholds(request: Request, db: Database = Depends(get_db)):
    """Update performance threshold settings."""
    data = await request.json()
    db.set_performance_thresholds(
        good=int(data.get("good", 5)),
        warning=int(data.get("warning", 10)),
        bad=int(data.get("bad", 30)),
        critical=int(data.get("critical", 60))
    )
    logger.info(f"Performance thresholds updated: {data}")
    return JSONResponse(content=success_response("Thresholds saved"))


@settings_router.get("/operating-hours")
@handle_errors("get operating hours")
async def get_operating_hours(db: Database = Depends(get_db)):
    """Get operating hours settings."""
    hours = db.get_operating_hours()
    return JSONResponse(content={"success": True, "hours": hours})


@settings_router.post("/operating-hours")
@handle_errors("update operating hours")
async def update_operating_hours(request: Request, db: Database = Depends(get_db)):
    """Update operating hours settings."""
    data = await request.json()
    db.set_operating_hours(
        enabled=bool(data.get("enabled", False)),
        start_hour=int(data.get("start_hour", 7)),
        end_hour=int(data.get("end_hour", 22))
    )
    logger.info(f"Operating hours updated: enabled={data.get('enabled')}, {data.get('start_hour')}:00-{data.get('end_hour')}:00")
    return JSONResponse(content=success_response("Operating hours saved"))


@settings_router.get("/container-restart")
@handle_errors("get container restart settings")
async def get_container_restart(db: Database = Depends(get_db)):
    """Get daily container restart settings."""
    settings = db.get_container_restart_settings()
    return JSONResponse(content={"success": True, "settings": settings})


@settings_router.post("/container-restart")
@handle_errors("update container restart settings")
async def update_container_restart(request: Request, db: Database = Depends(get_db)):
    """Update daily container restart settings."""
    data = await request.json()
    db.set_container_restart_settings(
        enabled=bool(data.get("enabled", False)),
        hour=int(data.get("hour", 8))
    )
    logger.info(f"Container restart settings updated: enabled={data.get('enabled')}, hour={data.get('hour')}:00")
    return JSONResponse(content=success_response("Container restart settings saved"))


@settings_router.get("/memory-limits")
@handle_errors("get memory limits")
async def get_memory_limits(db: Database = Depends(get_db)):
    """Per-portal browser memory limit (MB)."""
    return JSONResponse(content={"success": True, "limits": db.get_memory_limits()})


@settings_router.post("/memory-limits")
@handle_errors("update memory limits")
async def update_memory_limits(request: Request, db: Database = Depends(get_db)):
    """Update per-portal browser memory limits (MB). Body: {"limits": {"ooredoo": 1500, ...}}."""
    data = await request.json()
    limits = data.get("limits", data) or {}
    db.set_memory_limits(limits)
    logger.info(f"Browser memory limits updated: {limits}")
    return JSONResponse(content=success_response("Memory limits saved"))


@settings_router.get("/isp-extraction")
@handle_errors("get ISP extraction setting")
async def get_isp_extraction(db: Database = Depends(get_db)):
    """Get whether scheduled ISP portal extraction is enabled."""
    return JSONResponse(content={"success": True, "enabled": db.get_isp_extraction_enabled()})


@settings_router.post("/isp-extraction")
@handle_errors("update ISP extraction setting")
async def update_isp_extraction(request: Request, db: Database = Depends(get_db)):
    """Enable/disable scheduled ISP portal extraction (scraping ISP portals)."""
    data = await request.json()
    enabled = bool(data.get("enabled", True))
    db.set_isp_extraction_enabled(enabled)
    logger.info(f"ISP extraction {'enabled' if enabled else 'disabled'} via config")
    return JSONResponse(content=success_response(
        f"ISP checking {'enabled' if enabled else 'disabled'}"
    ))


@settings_router.get("/portals")
@handle_errors("get portal toggles")
async def get_portal_toggles(db: Database = Depends(get_db)):
    """Get per-portal extraction enabled state for all configured portals."""
    from config import Config
    portals = [
        {"name": p.name, "enabled": db.get_portal_enabled(p.name)}
        for p in Config.get_all_portals()
    ]
    return JSONResponse(content={"success": True, "portals": portals})


@settings_router.post("/portals")
@handle_errors("update portal toggles")
async def update_portal_toggles(request: Request, db: Database = Depends(get_db)):
    """Enable/disable extraction per portal. Body: {"portals": {"dhiraagu": true, ...}}."""
    data = await request.json()
    portals = data.get("portals", {}) or {}
    for name, enabled in portals.items():
        db.set_portal_enabled(name, bool(enabled))
    logger.info(f"Per-portal extraction toggles updated: {portals}")
    return JSONResponse(content=success_response("Portal settings saved"))


@settings_router.get("/external-api-key")
@handle_errors("get external API key setting")
async def get_external_api_key(db: Database = Depends(get_db)):
    """API key required by other applications to call /api/external/open-tickets."""
    key = db.get_setting("external_api_key")
    return JSONResponse(content={"success": True, "configured": bool(key), "key": key})


@settings_router.post("/external-api-key")
@handle_errors("update external API key")
async def update_external_api_key(request: Request, db: Database = Depends(get_db)):
    """Set (or regenerate, if body is empty/omits "key") the external API key."""
    data = await request.json() if await request.body() else {}
    key = (data.get("key") or "").strip() or secrets.token_urlsafe(32)
    db.set_setting("external_api_key", key, description="API key for /api/external/open-tickets")
    logger.info("External API key updated")
    return JSONResponse(content=success_response("External API key saved", {"key": key}))


@settings_router.get("")
@handle_errors("get all settings")
async def get_all_settings(db: Database = Depends(get_db)):
    """Get all app settings."""
    settings = db.get_all_settings()
    return JSONResponse(content={"success": True, "settings": settings})


# ==================== Staff Management ====================

@router.get("/staff-list")
@handle_errors("get staff list")
async def get_staff_list(db: Database = Depends(get_db)):
    """Get all unique staff names with counts from all tables."""
    staff = db.get_all_staff_names_with_counts()
    return JSONResponse(content={"success": True, "staff": staff})


@router.get("/staff-merge-preview")
@handle_errors("get staff merge preview")
async def get_staff_merge_preview(
    source: str = Query(..., description="Staff name to merge from"),
    target: str = Query(..., description="Staff name to merge into"),
    db: Database = Depends(get_db)
):
    """Preview the effect of merging one staff name into another."""
    if source == target:
        return JSONResponse(content={
            "success": False,
            "message": "Source and target cannot be the same"
        })

    preview = db.get_staff_merge_preview(source, target)
    return JSONResponse(content={"success": True, "preview": preview})


@router.post("/staff-merge")
@handle_errors("merge staff")
async def merge_staff(request: Request, db: Database = Depends(get_db)):
    """Merge one staff name into another across all tables."""
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

    result = db.merge_staff_names(source, target)

    logger.info(f"Staff merge completed: '{source}' -> '{target}', {result['total_updated']} records updated")

    return JSONResponse(content={
        "success": True,
        "message": f"Merged '{source}' into '{target}'",
        "result": result
    })


# ==================== Reports ====================

@router.get("/report-portal-stats")
@handle_errors("get portal stats")
async def get_report_portal_stats(
    date_filter: DateFilterParams = Depends(get_date_filter),
    db: Database = Depends(get_db)
):
    """Get ticket statistics by portal for reporting."""
    stats = db.get_report_portal_stats(date_filter.date_from, date_filter.date_to)
    return JSONResponse(content=stats)
