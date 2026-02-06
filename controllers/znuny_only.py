"""
Znuny-Only Controller - Handles API routes for Znuny tickets not linked to ISP portals.
"""

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import JSONResponse
from typing import Optional

from database import Database
from utils.logger import get_logger

router = APIRouter(prefix="/api/znuny-only")
logger = get_logger("znuny_only_controller")


def get_db():
    """Get database instance."""
    return Database()


@router.get("/stats")
async def get_znuny_only_stats():
    """Get summary statistics for Znuny-only tickets."""
    try:
        db = get_db()
        stats = db.get_znuny_only_stats()
        return JSONResponse(content=stats)
    except Exception as e:
        logger.error(f"Error getting znuny-only stats: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/tickets")
async def get_znuny_only_tickets(
    state: Optional[str] = Query(None, description="Filter by state (open/closed)"),
    created_by: Optional[str] = Query(None, description="Filter by creator"),
    date_from: Optional[str] = Query(None, description="Start date (YYYY-MM-DD)"),
    date_to: Optional[str] = Query(None, description="End date (YYYY-MM-DD)"),
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0)
):
    """Get Znuny-only tickets with optional filters."""
    try:
        db = get_db()
        result = db.get_znuny_only_tickets(
            state=state,
            created_by=created_by,
            date_from=date_from,
            date_to=date_to,
            limit=limit,
            offset=offset
        )
        return JSONResponse(content=result)
    except Exception as e:
        logger.error(f"Error getting znuny-only tickets: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/staff-stats")
async def get_znuny_only_staff_stats(
    date_from: Optional[str] = Query(None, description="Start date (YYYY-MM-DD)"),
    date_to: Optional[str] = Query(None, description="End date (YYYY-MM-DD)")
):
    """Get staff statistics for Znuny-only tickets."""
    try:
        db = get_db()
        stats = db.get_znuny_only_staff_stats(date_from, date_to)
        return JSONResponse(content={"staff": stats})
    except Exception as e:
        logger.error(f"Error getting znuny-only staff stats: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/staff-names")
async def get_znuny_only_staff_names():
    """Get list of staff names who created Znuny-only tickets."""
    try:
        db = get_db()
        names = db.get_znuny_only_staff_names()
        return JSONResponse(content={"staff": names})
    except Exception as e:
        logger.error(f"Error getting znuny-only staff names: {e}")
        raise HTTPException(status_code=500, detail=str(e))
