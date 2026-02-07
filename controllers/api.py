"""
API Controller - Handles JSON API routes.

This module provides RESTful API endpoints for:
- Dashboard statistics
- Ticket management
- Staff performance metrics
- Znuny integration
"""

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import JSONResponse, StreamingResponse
from typing import Optional, List, Dict, Any

from database import Database
from services import StatsService, ZnunyService, ConfigService
from utils.logger import get_logger

# API Router with OpenAPI tags
router = APIRouter(
    prefix="/api",
    tags=["API"],
    responses={
        500: {"description": "Internal server error"},
    }
)
logger = get_logger("api_controller")


def get_db():
    """Get database instance."""
    return Database()


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
    from config import Config

    health = {
        "status": "healthy",
        "timestamp": datetime.now().isoformat(),
        "version": Config.APP_VERSION,
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
async def get_stats():
    """Get dashboard statistics including ticket counts, portal breakdown, and sync status."""
    try:
        service = get_stats_service()
        stats = service.get_dashboard_stats()
        return JSONResponse(content=stats)
    except Exception as e:
        logger.error(f"Error getting stats: {e}")
        raise HTTPException(status_code=500, detail=str(e))


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
async def get_tickets(
    portal: Optional[str] = Query(None, description="Filter by portal (dhiraagu, ooredoo, rol, medianet)"),
    status: Optional[str] = None,
    ticket_type: Optional[str] = None,
    search: Optional[str] = None,
    in_znuny: Optional[bool] = None,
    include_completed: bool = False,
    completed_only: bool = False,
    staff: Optional[str] = None,
    limit: int = Query(default=50, le=1000),
    offset: int = 0
):
    """Get tickets with optional filters (SQL-level filtering)."""
    try:
        db = get_db()
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
            limit=limit,
            offset=offset
        )

        return JSONResponse(content={
            "total": total,
            "tickets": [_ticket_to_dict(t) for t in tickets]
        })
    except Exception as e:
        logger.error(f"Error getting tickets: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/tickets/{ticket_id}")
async def get_ticket(ticket_id: int):
    """Get a single ticket by ID."""
    try:
        db = get_db()
        ticket = db.get_ticket_by_id(ticket_id)
        if not ticket:
            raise HTTPException(status_code=404, detail="Ticket not found")
        return JSONResponse(content=_ticket_to_dict(ticket))
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error getting ticket: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/tickets/{ticket_id}/check-znuny")
async def check_ticket_znuny(ticket_id: int):
    """Check if a ticket exists in Znuny."""
    try:
        service = get_znuny_service()
        result = service.check_ticket_in_znuny(ticket_id)
        return JSONResponse(content=result)
    except Exception as e:
        logger.error(f"Error checking Znuny: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/tickets/{ticket_id}/sync-znuny")
async def sync_ticket_znuny(ticket_id: int):
    """Sync Znuny details for a ticket."""
    try:
        service = get_znuny_service()
        result = service.sync_ticket_details(ticket_id)
        return JSONResponse(content=result)
    except Exception as e:
        logger.error(f"Error syncing Znuny: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/tickets/{ticket_id}/znuny-articles")
async def get_ticket_znuny_articles(ticket_id: int):
    """Get Znuny articles for a ticket."""
    try:
        db = get_db()
        ticket = db.get_ticket_by_id(ticket_id)
        if not ticket:
            raise HTTPException(status_code=404, detail="Ticket not found")

        articles = db.get_znuny_articles(ticket_id=ticket_id)
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
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error getting Znuny articles: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/tickets/{ticket_id}/site-visits")
async def get_ticket_site_visits(ticket_id: int):
    """Get site visits for a ticket."""
    try:
        db = get_db()
        ticket = db.get_ticket_by_id(ticket_id)
        if not ticket:
            raise HTTPException(status_code=404, detail="Ticket not found")

        visits = db.get_site_visits_for_ticket(ticket_id)
        return JSONResponse(content={"visits": visits})
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error getting site visits for ticket {ticket_id}: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/tickets/check-all-znuny")
async def check_all_znuny_status():
    """Check Znuny status for all tickets not yet marked as in Znuny."""
    try:
        service = get_znuny_service()
        result = service.check_all_tickets_in_znuny()
        return JSONResponse(content=result)
    except Exception as e:
        logger.error(f"Error checking all Znuny status: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# Znuny sync endpoints
@router.get("/znuny-sync-status")
async def get_znuny_sync_status():
    """Get Znuny sync status."""
    try:
        service = get_znuny_service()
        return JSONResponse(content=service.get_sync_status())
    except Exception as e:
        logger.error(f"Error getting Znuny sync status: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/sync-znuny-details")
async def sync_all_znuny_details():
    """Sync Znuny details for all tickets needing sync."""
    try:
        service = get_znuny_service()
        result = service.sync_all_znuny_details()
        return JSONResponse(content=result)
    except Exception as e:
        logger.error(f"Error syncing Znuny details: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# Staff endpoints
@router.get("/staff-stats")
async def get_staff_stats(
    date_from: Optional[str] = None,
    date_to: Optional[str] = None
):
    """Get basic staff statistics."""
    try:
        service = get_stats_service()
        stats = service.get_staff_stats(date_from=date_from, date_to=date_to)
        return JSONResponse(content={"stats": stats})
    except Exception as e:
        logger.error(f"Error getting staff stats: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/staff-stats-detailed")
async def get_staff_stats_detailed(
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    exclude_negative: bool = True
):
    """Get detailed staff statistics with on-time metrics.

    Args:
        date_from: Optional start date filter (YYYY-MM-DD)
        date_to: Optional end date filter (YYYY-MM-DD)
        exclude_negative: If True (default), exclude tickets with negative time differences
                          (historical tickets where Znuny existed before extractor)
    """
    try:
        service = get_stats_service()
        stats = service.get_staff_stats_detailed(
            date_from=date_from, date_to=date_to, exclude_negative=exclude_negative
        )
        return JSONResponse(content={"stats": stats})
    except Exception as e:
        logger.error(f"Error getting detailed staff stats: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/staff/{name}/tickets")
async def get_staff_tickets(
    name: str,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    limit: int = Query(default=50, le=500),
    offset: int = 0
):
    """Get tickets created by a specific staff member."""
    try:
        service = get_stats_service()
        result = service.get_staff_tickets(
            name, date_from=date_from, date_to=date_to,
            limit=limit, offset=offset
        )
        return JSONResponse(content={
            "total": result["total"],
            "tickets": [_ticket_to_dict(t) for t in result["tickets"]]
        })
    except Exception as e:
        logger.error(f"Error getting staff tickets: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/staff/{name}/performance")
async def get_staff_performance(name: str, days: int = 14):
    """Get daily performance trend for a staff member."""
    try:
        service = get_stats_service()
        data = service.get_staff_performance(name, days=days)
        return JSONResponse(content={"performance": data})
    except Exception as e:
        logger.error(f"Error getting staff performance: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/staff-names")
async def get_staff_names():
    """Get list of all staff names."""
    try:
        service = get_stats_service()
        names = service.get_staff_names()
        return JSONResponse(content={"staff": names})
    except Exception as e:
        logger.error(f"Error getting staff names: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/staff/{name}/znuny-tickets")
async def get_staff_znuny_tickets(
    name: str,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    limit: int = Query(default=50, le=500),
    offset: int = 0
):
    """Get Znuny-only tickets created by a specific staff member."""
    try:
        db = get_db()
        result = db.get_staff_znuny_tickets(
            name, date_from=date_from, date_to=date_to,
            limit=limit, offset=offset
        )
        return JSONResponse(content=result)
    except Exception as e:
        logger.error(f"Error getting staff znuny tickets: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/staff/{name}/articles")
async def get_staff_articles(
    name: str,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    limit: int = Query(default=50, le=500),
    offset: int = 0
):
    """Get articles created by a specific staff member."""
    try:
        db = get_db()
        result = db.get_staff_articles(
            name, date_from=date_from, date_to=date_to,
            limit=limit, offset=offset
        )
        return JSONResponse(content=result)
    except Exception as e:
        logger.error(f"Error getting staff articles: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/staff-delays")
async def get_staff_delays(
    min_delay: int = Query(5, ge=1),
    date_from: Optional[str] = None,
    date_to: Optional[str] = None
):
    """Get delayed tickets grouped by staff."""
    try:
        db = get_db()
        delays = db.get_delayed_tickets_by_staff(min_delay, date_from, date_to)
        return JSONResponse(content={"delays": delays})
    except Exception as e:
        logger.error(f"Error getting staff delays: {e}")
        raise HTTPException(status_code=500, detail=str(e))


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
async def export_staff_csv(
    date_from: Optional[str] = None,
    date_to: Optional[str] = None
):
    """Export staff statistics as CSV."""
    try:
        service = get_stats_service()
        csv_content = service.export_staff_csv(date_from=date_from, date_to=date_to)
        filename = f"staff_report_{date_from or 'all'}_{date_to or 'all'}.csv"
        return StreamingResponse(
            iter([csv_content]),
            media_type="text/csv",
            headers={"Content-Disposition": f"attachment; filename={filename}"}
        )
    except Exception as e:
        logger.error(f"Error exporting staff CSV: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/tickets-csv")
async def export_tickets_csv(
    staff: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None
):
    """Export tickets as CSV."""
    try:
        service = get_stats_service()
        csv_content = service.export_tickets_csv(
            staff=staff, date_from=date_from, date_to=date_to
        )
        filename = f"tickets_export_{date_from or 'all'}_{date_to or 'all'}.csv"
        return StreamingResponse(
            iter([csv_content]),
            media_type="text/csv",
            headers={"Content-Disposition": f"attachment; filename={filename}"}
        )
    except Exception as e:
        logger.error(f"Error exporting tickets CSV: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# Extraction logs endpoint
@router.get("/extraction-logs")
async def get_extraction_logs(limit: int = 100):
    """Get extraction logs."""
    try:
        service = get_stats_service()
        logs = service.get_extraction_logs(limit=limit)
        return JSONResponse(content={"logs": logs})
    except Exception as e:
        logger.error(f"Error getting extraction logs: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# Config endpoints
@router.get("/config")
async def get_config():
    """Get current configuration (passwords masked)."""
    try:
        service = get_config_service()
        config = service.get_config(mask_passwords=True)
        return JSONResponse(content={"config": config})
    except Exception as e:
        logger.error(f"Error getting config: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/config/raw")
async def get_config_raw():
    """Get raw configuration including passwords."""
    try:
        service = get_config_service()
        config = service.get_config(mask_passwords=False)
        return JSONResponse(content={"config": config})
    except Exception as e:
        logger.error(f"Error getting raw config: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/config")
async def update_config(request: Request):
    """Update configuration."""
    try:
        data = await request.json()
        service = get_config_service()
        result = service.update_config(data.get('config', {}))
        return JSONResponse(content=result)
    except Exception as e:
        logger.error(f"Error updating config: {e}")
        raise HTTPException(status_code=500, detail=str(e))


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
        "znuny_url": ticket.znuny_url
    }
