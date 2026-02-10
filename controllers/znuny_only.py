"""
Znuny-Only Controller - Handles API routes for Znuny tickets not linked to ISP portals.
"""

from fastapi import APIRouter, HTTPException, Query, Depends
from fastapi.responses import JSONResponse
from typing import Optional

from database import Database
from utils.logger import get_logger
from .dependencies import get_db, handle_errors, get_date_filter, DateFilterParams

router = APIRouter(prefix="/api/znuny-only")
logger = get_logger("znuny_only_controller")


@router.get("/stats")
@handle_errors("get znuny-only stats")
async def get_znuny_only_stats(db: Database = Depends(get_db)):
    """Get summary statistics for Znuny-only tickets."""
    stats = db.get_znuny_only_stats()
    return JSONResponse(content=stats)


@router.get("/tickets")
@handle_errors("get znuny-only tickets")
async def get_znuny_only_tickets(
    state: Optional[str] = Query(None, description="Filter by state (open/closed)"),
    created_by: Optional[str] = Query(None, description="Filter by creator"),
    linked: Optional[str] = Query(None, description="Filter by ISP link (yes/no)"),
    date_filter: DateFilterParams = Depends(get_date_filter),
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
    db: Database = Depends(get_db)
):
    """Get Znuny tickets with optional filters."""
    result = db.get_znuny_only_tickets(
        state=state,
        created_by=created_by,
        date_from=date_filter.date_from,
        date_to=date_filter.date_to,
        linked=linked,
        limit=limit,
        offset=offset
    )
    return JSONResponse(content=result)


@router.get("/staff-stats")
@handle_errors("get znuny-only staff stats")
async def get_znuny_only_staff_stats(
    date_filter: DateFilterParams = Depends(get_date_filter),
    db: Database = Depends(get_db)
):
    """Get staff statistics for Znuny-only tickets."""
    stats = db.get_znuny_only_staff_stats(date_filter.date_from, date_filter.date_to)
    return JSONResponse(content={"staff": stats})


@router.get("/staff-names")
@handle_errors("get znuny-only staff names")
async def get_znuny_only_staff_names(db: Database = Depends(get_db)):
    """Get list of staff names who created Znuny-only tickets."""
    names = db.get_znuny_only_staff_names()
    return JSONResponse(content={"staff": names})
