"""
Field Visits Controller - Handles site visit API routes.
"""

import threading
from fastapi import APIRouter, HTTPException, Query, Request, Depends
from fastapi.responses import JSONResponse
from typing import Optional

from database import Database
from services import ZnunyService
from .dependencies import get_db, handle_errors, get_date_filter, DateFilterParams

router = APIRouter(prefix="/api/field-visits")


@router.get("")
@handle_errors("get field visits")
async def get_field_visits(
    date_filter: DateFilterParams = Depends(get_date_filter),
    assigned_to: Optional[str] = Query(None),
    status: Optional[str] = Query(None),
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
    db: Database = Depends(get_db)
):
    """Get site visits with optional filters."""
    result = db.get_site_visits(
        date_from=date_filter.date_from, date_to=date_filter.date_to,
        assigned_to=assigned_to, status=status,
        limit=limit, offset=offset
    )
    return result


@router.get("/staff-stats")
@handle_errors("get field visits staff stats")
async def get_field_visits_staff_stats(
    date_filter: DateFilterParams = Depends(get_date_filter),
    db: Database = Depends(get_db)
):
    """Get site visit statistics by assigned staff."""
    stats = db.get_site_visit_staff_stats(date_filter.date_from, date_filter.date_to)
    return {"staff": stats}


@router.get("/by-date")
@handle_errors("get field visits by date")
async def get_field_visits_by_date(
    date_filter: DateFilterParams = Depends(get_date_filter),
    db: Database = Depends(get_db)
):
    """Get site visits aggregated by date."""
    data = db.get_site_visit_by_date(date_filter.date_from, date_filter.date_to)
    return {"dates": data}


@router.get("/assigned-staff")
@handle_errors("get assigned staff")
async def get_field_visits_assigned_staff(db: Database = Depends(get_db)):
    """Get list of staff who have been assigned field visits."""
    return {"staff": db.get_assigned_staff_names()}


@router.post("/sync")
@handle_errors("sync site visits")
async def sync_site_visits(db: Database = Depends(get_db)):
    """
    Sync all site visits from Znuny.
    Searches for site visit tickets, extracts visits, and links to ISP tickets.
    """
    znuny_service = ZnunyService(db)

    def run_sync():
        return znuny_service.sync_all_site_visits()

    # Run in background thread
    result_container = {}

    def sync_thread():
        try:
            result_container["result"] = run_sync()
        except Exception as e:
            result_container["error"] = str(e)

    thread = threading.Thread(target=sync_thread)
    thread.start()
    thread.join(timeout=120)  # Wait up to 2 minutes

    if "error" in result_container:
        raise HTTPException(status_code=500, detail=result_container["error"])

    return JSONResponse(content={
        "success": True,
        **result_container.get("result", {})
    })


@router.put("/{visit_id}")
@handle_errors("update site visit")
async def update_site_visit(visit_id: int, request: Request, db: Database = Depends(get_db)):
    """Update a site visit (scheduled time, assigned_to, etc.)."""
    data = await request.json()
    result = db.update_site_visit(visit_id, data)
    return JSONResponse(content=result)
