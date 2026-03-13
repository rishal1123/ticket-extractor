"""
API Controller - Handles JSON API routes.

This module provides RESTful API endpoints for:
- Dashboard statistics
- Ticket management
- Staff performance metrics
- Znuny integration
"""

from fastapi import APIRouter, HTTPException, Query, Request, Depends, UploadFile, File
from fastapi.responses import JSONResponse, StreamingResponse
from typing import Optional, List, Dict, Any

from database import Database
from services import StatsService, ZnunyService, ConfigService
from config import Config, APP_VERSION
from utils.logger import get_logger
from .dependencies import get_db, handle_errors, get_date_filter, DateFilterParams
import threading

# API Router with OpenAPI tags
router = APIRouter(
    prefix="/api",
    tags=["API"],
    responses={
        500: {"description": "Internal server error"},
    }
)
logger = get_logger("api_controller")


def get_stats_service():
    """Get stats service instance."""
    return StatsService()


def get_znuny_service():
    """Get znuny service instance."""
    return ZnunyService()


def get_config_service():
    """Get config service instance."""
    return ConfigService()


# Health endpoint
@router.get(
    "/health",
    tags=["Health"],
    summary="Health Check",
    description="Check system health including database, scheduler, and storage status.",
    response_description="Health status with component checks",
    responses={
        200: {
            "description": "Health check successful",
            "content": {
                "application/json": {
                    "example": {
                        "status": "healthy",
                        "timestamp": "2026-02-07T12:00:00",
                        "version": "1.0.0",
                        "checks": {
                            "database": {"status": "healthy", "tickets_count": 10},
                            "scheduler": {"status": "healthy", "running": True},
                            "storage": {"status": "healthy", "database_size_mb": 0.5}
                        }
                    }
                }
            }
        }
    }
)
async def health_check():
    """
    Health check endpoint for monitoring.
    Returns system status, database connectivity, and service health.
    """
    import os
    from datetime import datetime

    health = {
        "status": "healthy",
        "timestamp": datetime.now().isoformat(),
        "version": APP_VERSION,
        "checks": {}
    }

    # Check database
    try:
        db = get_db()
        stats = db.get_stats()
        health["checks"]["database"] = {
            "status": "healthy",
            "tickets_count": stats.get("total", 0)
        }
    except Exception as e:
        health["checks"]["database"] = {
            "status": "unhealthy",
            "error": str(e)
        }
        health["status"] = "degraded"

    # Check scheduler
    try:
        from services import SchedulerService
        scheduler = SchedulerService()
        scheduler_status = scheduler.get_status()
        health["checks"]["scheduler"] = {
            "status": "healthy" if scheduler_status.get("running") else "stopped",
            "running": scheduler_status.get("running", False),
            "jobs_count": scheduler_status.get("jobs_count", 0)
        }
    except Exception as e:
        health["checks"]["scheduler"] = {
            "status": "unknown",
            "error": str(e)
        }

    # Check disk space (for database)
    try:
        db_path = Config.DATABASE_PATH
        if os.path.exists(db_path):
            db_size = os.path.getsize(db_path)
            health["checks"]["storage"] = {
                "status": "healthy",
                "database_size_mb": round(db_size / (1024 * 1024), 2)
            }
    except Exception as e:
        health["checks"]["storage"] = {
            "status": "unknown",
            "error": str(e)
        }

    return JSONResponse(content=health)


# Stats endpoints
@router.get(
    "/stats",
    tags=["Statistics"],
    summary="Dashboard Statistics",
    description="Get comprehensive dashboard statistics including ticket counts, portal breakdown, and extraction status.",
    responses={
        200: {
            "description": "Dashboard statistics",
            "content": {
                "application/json": {
                    "example": {
                        "total": 25,
                        "completed": 10,
                        "by_portal": {"dhiraagu": 5, "ooredoo": 10, "rol": 5, "medianet": 5},
                        "not_in_znuny": 3,
                        "today_extracted": {"dhiraagu": 2, "ooredoo": 3}
                    }
                }
            }
        }
    }
)
@handle_errors("get stats")
async def get_stats():
    """Get dashboard statistics including ticket counts, portal breakdown, and sync status."""
    service = get_stats_service()
    stats = service.get_dashboard_stats()
    return JSONResponse(content=stats)


# Tickets endpoints
@router.get(
    "/tickets",
    tags=["Tickets"],
    summary="List Tickets",
    description="Retrieve tickets with optional filtering by portal, status, type, and more.",
    responses={
        200: {
            "description": "List of tickets with pagination",
            "content": {
                "application/json": {
                    "example": {
                        "total": 100,
                        "tickets": [
                            {
                                "id": 1,
                                "portal": "dhiraagu",
                                "ticket_id": "0125858440",
                                "customer_name": "Test Customer",
                                "status": "Open"
                            }
                        ]
                    }
                }
            }
        }
    }
)
@handle_errors("get tickets")
async def get_tickets(
    portal: Optional[str] = Query(None, description="Filter by portal (dhiraagu, ooredoo, rol, medianet)"),
    status: Optional[str] = None,
    ticket_type: Optional[str] = None,
    search: Optional[str] = None,
    in_znuny: Optional[bool] = None,
    include_completed: bool = False,
    completed_only: bool = False,
    staff: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    limit: int = Query(default=50, le=1000),
    offset: int = 0,
    db: Database = Depends(get_db)
):
    """Get tickets with optional filters (SQL-level filtering)."""
    # Use database-level filtering for efficiency
    tickets, total = db.get_tickets_filtered(
        portal=portal,
        status=status,
        ticket_type=ticket_type,
        in_znuny=in_znuny,
        staff=staff,
        search=search,
        include_completed=include_completed or completed_only,
        completed_only=completed_only,
        date_from=date_from,
        date_to=date_to,
        limit=limit,
        offset=offset
    )

    return JSONResponse(content={
        "total": total,
        "tickets": [_ticket_to_dict(t) for t in tickets]
    })


@router.get("/tickets/{ticket_id}")
@handle_errors("get ticket")
async def get_ticket(ticket_id: int, db: Database = Depends(get_db)):
    """Get a single ticket by ID, enriched with znuny_tickets metadata."""
    ticket = db.get_ticket_by_id(ticket_id)
    if not ticket:
        raise HTTPException(status_code=404, detail="Ticket not found")
    result = _ticket_to_dict(ticket)
    # Enrich with znuny_tickets metadata (queue, state, owner)
    if ticket.znuny_ticket_id:
        zt = db.get_znuny_ticket_metadata(ticket.znuny_ticket_id)
        if zt:
            result["znuny_state"] = zt.get("state")
            result["znuny_queue"] = zt.get("queue")
            result["znuny_owner"] = zt.get("owner")
            result["znuny_priority"] = zt.get("priority")
    return JSONResponse(content=result)


@router.post("/tickets/{ticket_id}/check-znuny")
@handle_errors("check Znuny")
async def check_ticket_znuny(ticket_id: int):
    """Check if a ticket exists in Znuny."""
    service = get_znuny_service()
    result = service.check_ticket_in_znuny(ticket_id)
    return JSONResponse(content=result)


@router.post("/tickets/{ticket_id}/sync-znuny")
@handle_errors("sync Znuny")
async def sync_ticket_znuny(ticket_id: int):
    """Sync Znuny details for a ticket."""
    service = get_znuny_service()
    result = service.sync_ticket_details(ticket_id)
    return JSONResponse(content=result)


@router.get("/tickets/{ticket_id}/znuny-articles")
@handle_errors("get Znuny articles")
async def get_ticket_znuny_articles(ticket_id: int, db: Database = Depends(get_db)):
    """Get Znuny articles for a ticket."""
    ticket = db.get_ticket_by_id(ticket_id)
    if not ticket:
        raise HTTPException(status_code=404, detail="Ticket not found")

    articles = db.get_znuny_articles(ticket_id=ticket_id)
    if not articles and ticket.znuny_ticket_id:
        articles = db.get_znuny_articles(znuny_ticket_id=ticket.znuny_ticket_id)
    service = get_stats_service()
    time_info = service.calculate_time_to_create(ticket.created_at, ticket.znuny_created_at)

    return JSONResponse(content={
        "znuny_ticket_id": ticket.znuny_ticket_id,
        "znuny_created_at": ticket.znuny_created_at.isoformat() if ticket.znuny_created_at else None,
        "znuny_created_by": ticket.znuny_created_by,
        "portal_created_at": ticket.portal_created_at.isoformat() if ticket.portal_created_at else None,
        **time_info,
        "articles": articles
    })


@router.get("/articles")
@handle_errors("get articles")
async def get_articles(
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    staff: Optional[str] = None,
    limit: int = Query(default=50, le=500),
    offset: int = 0,
    db: Database = Depends(get_db)
):
    """Get articles with optional date and staff filters."""
    result = db.get_articles_filtered(
        date_from=date_from, date_to=date_to,
        staff=staff, limit=limit, offset=offset
    )
    return JSONResponse(content=result)


@router.get("/tickets/{ticket_id}/site-visits")
@handle_errors("get site visits")
async def get_ticket_site_visits(ticket_id: int, db: Database = Depends(get_db)):
    """Get site visits for a ticket."""
    ticket = db.get_ticket_by_id(ticket_id)
    if not ticket:
        raise HTTPException(status_code=404, detail="Ticket not found")

    visits = db.get_site_visits_for_ticket(ticket_id)
    return JSONResponse(content={"visits": visits})


@router.post("/tickets/check-all-znuny")
@handle_errors("check all Znuny status")
async def check_all_znuny_status():
    """Check Znuny status for all tickets not yet marked as in Znuny."""
    service = get_znuny_service()
    result = service.check_all_tickets_in_znuny()
    return JSONResponse(content=result)


# Znuny sync endpoints
@router.get("/znuny-sync-status")
@handle_errors("get Znuny sync status")
async def get_znuny_sync_status():
    """Get Znuny sync status."""
    service = get_znuny_service()
    return JSONResponse(content=service.get_sync_status())


@router.post("/sync-znuny-details")
@handle_errors("sync Znuny details")
async def sync_all_znuny_details():
    """Sync Znuny details for all tickets needing sync."""
    service = get_znuny_service()
    result = service.sync_all_znuny_details()
    return JSONResponse(content=result)


# Staff endpoints
@router.get("/staff-stats")
@handle_errors("get staff stats")
async def get_staff_stats(date_filter: DateFilterParams = Depends(get_date_filter)):
    """Get basic staff statistics."""
    service = get_stats_service()
    stats = service.get_staff_stats(date_from=date_filter.date_from, date_to=date_filter.date_to)
    return JSONResponse(content={"stats": stats})


@router.get("/staff-stats-detailed")
@handle_errors("get detailed staff stats")
async def get_staff_stats_detailed(
    date_filter: DateFilterParams = Depends(get_date_filter),
    exclude_negative: bool = True
):
    """Get detailed staff statistics with on-time metrics.

    Args:
        date_from: Optional start date filter (YYYY-MM-DD)
        date_to: Optional end date filter (YYYY-MM-DD)
        exclude_negative: If True (default), exclude tickets with negative time differences
                          (historical tickets where Znuny existed before extractor)
    """
    service = get_stats_service()
    stats = service.get_staff_stats_detailed(
        date_from=date_filter.date_from, date_to=date_filter.date_to, exclude_negative=exclude_negative
    )
    return JSONResponse(content={"stats": stats})


@router.get("/staff/{name}/tickets")
@handle_errors("get staff tickets")
async def get_staff_tickets(
    name: str,
    date_filter: DateFilterParams = Depends(get_date_filter),
    limit: int = Query(default=50, le=500),
    offset: int = 0
):
    """Get tickets created by a specific staff member."""
    service = get_stats_service()
    result = service.get_staff_tickets(
        name, date_from=date_filter.date_from, date_to=date_filter.date_to,
        limit=limit, offset=offset
    )
    return JSONResponse(content={
        "total": result["total"],
        "tickets": [_ticket_to_dict(t) for t in result["tickets"]]
    })


@router.get("/staff/{name}/performance")
@handle_errors("get staff performance")
async def get_staff_performance(name: str, days: int = 14):
    """Get daily performance trend for a staff member."""
    service = get_stats_service()
    data = service.get_staff_performance(name, days=days)
    return JSONResponse(content={"performance": data})


@router.get("/staff-names")
@handle_errors("get staff names")
async def get_staff_names():
    """Get list of all staff names."""
    service = get_stats_service()
    names = service.get_staff_names()
    return JSONResponse(content={"staff": names})


@router.get("/staff/{name}/znuny-tickets")
@handle_errors("get staff znuny tickets")
async def get_staff_znuny_tickets(
    name: str,
    date_filter: DateFilterParams = Depends(get_date_filter),
    limit: int = Query(default=50, le=500),
    offset: int = 0,
    db: Database = Depends(get_db)
):
    """Get Znuny-only tickets created by a specific staff member."""
    result = db.get_staff_znuny_tickets(
        name, date_from=date_filter.date_from, date_to=date_filter.date_to,
        limit=limit, offset=offset
    )
    return JSONResponse(content=result)


@router.get("/staff/{name}/articles")
@handle_errors("get staff articles")
async def get_staff_articles(
    name: str,
    date_filter: DateFilterParams = Depends(get_date_filter),
    limit: int = Query(default=50, le=500),
    offset: int = 0,
    db: Database = Depends(get_db)
):
    """Get articles created by a specific staff member."""
    result = db.get_staff_articles(
        name, date_from=date_filter.date_from, date_to=date_filter.date_to,
        limit=limit, offset=offset
    )
    return JSONResponse(content=result)


@router.get("/staff-delays")
@handle_errors("get staff delays")
async def get_staff_delays(
    min_delay: int = Query(5, ge=1),
    date_filter: DateFilterParams = Depends(get_date_filter),
    db: Database = Depends(get_db)
):
    """Get delayed tickets grouped by staff."""
    delays = db.get_delayed_tickets_by_staff(min_delay, date_filter.date_from, date_filter.date_to)
    return JSONResponse(content={"delays": delays})


@router.get("/portals")
async def get_portals():
    """Get list of available portals."""
    portals = [
        {"name": "dhiraagu", "label": "Dhiraagu"},
        {"name": "ooredoo", "label": "Ooredoo"},
        {"name": "rol", "label": "ROL"},
        {"name": "medianet", "label": "Medianet"}
    ]
    return JSONResponse(content={"portals": portals})


# Export endpoints
@router.get("/reports/staff-csv")
@handle_errors("export staff CSV")
async def export_staff_csv(date_filter: DateFilterParams = Depends(get_date_filter)):
    """Export staff statistics as CSV."""
    service = get_stats_service()
    csv_content = service.export_staff_csv(date_from=date_filter.date_from, date_to=date_filter.date_to)
    filename = f"staff_report_{date_filter.date_from or 'all'}_{date_filter.date_to or 'all'}.csv"
    return StreamingResponse(
        iter([csv_content]),
        media_type="text/csv",
        headers={"Content-Disposition": f"attachment; filename={filename}"}
    )


@router.get("/tickets-csv")
@handle_errors("export tickets CSV")
async def export_tickets_csv(
    staff: Optional[str] = None,
    date_filter: DateFilterParams = Depends(get_date_filter)
):
    """Export tickets as CSV."""
    service = get_stats_service()
    csv_content = service.export_tickets_csv(
        staff=staff, date_from=date_filter.date_from, date_to=date_filter.date_to
    )
    filename = f"tickets_export_{date_filter.date_from or 'all'}_{date_filter.date_to or 'all'}.csv"
    return StreamingResponse(
        iter([csv_content]),
        media_type="text/csv",
        headers={"Content-Disposition": f"attachment; filename={filename}"}
    )


# Extraction logs endpoint
@router.get("/extraction-logs")
@handle_errors("get extraction logs")
async def get_extraction_logs(limit: int = 100):
    """Get extraction logs."""
    service = get_stats_service()
    logs = service.get_extraction_logs(limit=limit)
    return JSONResponse(content={"logs": logs})


# Config endpoints
@router.get("/config")
@handle_errors("get config")
async def get_config():
    """Get current configuration (passwords masked)."""
    service = get_config_service()
    config = service.get_config(mask_passwords=True)
    return JSONResponse(content={"config": config})


@router.get("/config/raw")
@handle_errors("get raw config")
async def get_config_raw():
    """Get raw configuration including passwords."""
    service = get_config_service()
    config = service.get_config(mask_passwords=False)
    return JSONResponse(content={"config": config})


def _restart_extractions():
    """Restart extraction and Znuny sync in background after config change."""
    from services.scheduler_service import get_scheduler
    try:
        scheduler = get_scheduler()
        logger.info("Config changed - restarting extractions with new credentials")

        scheduler._extraction_job()
        scheduler._znuny_sync_job()
    except Exception as e:
        logger.error(f"Error restarting extractions after config change: {e}")


@router.post("/config")
@handle_errors("update config")
async def update_config(request: Request):
    """Update configuration."""
    data = await request.json()
    service = get_config_service()
    result = service.update_config(data.get('config', {}))
    if result.get("success"):
        _restart_extractions()
    return JSONResponse(content=result)


@router.post("/config/upload")
@handle_errors("upload env file")
async def upload_env_file(file: UploadFile = File(...)):
    """Upload a .env file to replace current configuration."""
    content = await file.read()
    text = content.decode('utf-8')

    # Parse the uploaded .env content
    new_config = {}
    for line in text.splitlines():
        line = line.strip()
        if line and not line.startswith('#') and '=' in line:
            key, value = line.split('=', 1)
            new_config[key.strip()] = value.strip()

    if not new_config:
        return JSONResponse(content={"success": False, "message": "No valid key=value pairs found in file"})

    service = get_config_service()
    result = service.update_config(new_config)
    result["keys_count"] = len(new_config)
    if result.get("success"):
        _restart_extractions()
    return JSONResponse(content=result)


# Portal URL patterns for generating links
PORTAL_URL_PATTERNS = {
    "dhiraagu": "https://afas.dhiraagu.com.mv/orders/hdc/{ticket_id}?activeRelationManager=notes",
    "ooredoo": "https://www.ooredoo.mv/webapps/FMS/public/tickets/ticket_info/{ticket_id}",
    "rol": "https://support.rol.net.mv/staff/index.php?/Tickets/Ticket/View/{ticket_id}/inbox/55/-1/-1",
    # Medianet uses UUID-based URLs that must be captured during extraction
}


def _generate_portal_url(ticket) -> str | None:
    """Generate portal URL for a ticket if not already stored."""
    # If URL is already stored (e.g., Medianet), use it
    if ticket.portal_url:
        return ticket.portal_url

    # Generate URL from pattern for other portals
    pattern = PORTAL_URL_PATTERNS.get(ticket.portal)
    if pattern and ticket.ticket_id:
        return pattern.format(ticket_id=ticket.ticket_id)

    return None


# Helper functions
def _ticket_to_dict(ticket) -> dict:
    """Convert ticket object to dictionary."""
    return {
        "id": ticket.id,
        "portal": ticket.portal,
        "ticket_id": ticket.ticket_id,
        "address": ticket.address,
        "account": ticket.account,
        "customer_name": ticket.customer_name,
        "ticket_type": ticket.ticket_type,
        "portal_created_at": ticket.portal_created_at.isoformat() if ticket.portal_created_at else None,
        "service_type": ticket.service_type,
        "status": ticket.status,
        "kpi": ticket.kpi,
        "notes": ticket.notes,
        "created_at": ticket.created_at.isoformat() if ticket.created_at else None,
        "updated_at": ticket.updated_at.isoformat() if ticket.updated_at else None,
        "completed_at": ticket.completed_at.isoformat() if ticket.completed_at else None,
        "in_znuny": ticket.in_znuny,
        "znuny_ticket_id": ticket.znuny_ticket_id,
        "znuny_created_at": ticket.znuny_created_at.isoformat() if ticket.znuny_created_at else None,
        "znuny_created_by": ticket.znuny_created_by,
        "znuny_address": ticket.znuny_address,
        "portal_url": _generate_portal_url(ticket),
        "znuny_url": ticket.znuny_url,
        "time_to_create_minutes": ticket.time_to_create_minutes
    }
