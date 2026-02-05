"""
API Controller - Handles JSON API routes.
"""

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import JSONResponse, StreamingResponse
from typing import Optional

from database import Database
from services import StatsService, ZnunyService, ConfigService
from utils.logger import get_logger

router = APIRouter(prefix="/api")
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


# Stats endpoints
@router.get("/stats")
async def get_stats():
    """Get dashboard statistics."""
    try:
        service = get_stats_service()
        stats = service.get_dashboard_stats()
        return JSONResponse(content=stats)
    except Exception as e:
        logger.error(f"Error getting stats: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# Tickets endpoints
@router.get("/tickets")
async def get_tickets(
    portal: Optional[str] = None,
    status: Optional[str] = None,
    ticket_type: Optional[str] = None,
    search: Optional[str] = None,
    in_znuny: Optional[bool] = None,
    include_completed: bool = False,
    staff: Optional[str] = None,
    limit: int = Query(default=50, le=1000),
    offset: int = 0
):
    """Get tickets with optional filters."""
    try:
        db = get_db()
        # Get all tickets first, then filter in-memory
        all_tickets = db.get_all_tickets(portal=portal, include_completed=include_completed)

        # Apply additional filters
        filtered = all_tickets
        if status:
            filtered = [t for t in filtered if t.status == status]
        if ticket_type:
            filtered = [t for t in filtered if t.ticket_type == ticket_type]
        if in_znuny is not None:
            filtered = [t for t in filtered if t.in_znuny == in_znuny]
        if staff:
            filtered = [t for t in filtered if t.znuny_created_by == staff]
        if search:
            search_lower = search.lower()
            filtered = [t for t in filtered if (
                (t.ticket_id and search_lower in t.ticket_id.lower()) or
                (t.customer_name and search_lower in t.customer_name.lower()) or
                (t.address and search_lower in t.address.lower()) or
                (t.account and search_lower in t.account.lower())
            )]

        total = len(filtered)
        # Apply pagination
        paginated = filtered[offset:offset + limit]

        return JSONResponse(content={
            "total": total,
            "tickets": [_ticket_to_dict(t) for t in paginated]
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
    date_to: Optional[str] = None
):
    """Get detailed staff statistics with on-time metrics."""
    try:
        service = get_stats_service()
        stats = service.get_staff_stats_detailed(date_from=date_from, date_to=date_to)
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
        return JSONResponse(content={"names": names})
    except Exception as e:
        logger.error(f"Error getting staff names: {e}")
        raise HTTPException(status_code=500, detail=str(e))


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
