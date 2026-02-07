"""
Field Visits Controller - Handles site visit API routes.
"""

import threading
from fastapi import APIRouter, HTTPException, Query, Request, Depends
from fastapi.responses import JSONResponse
from typing import Optional

from database import Database, now_maldives
from services import ZnunyService
from utils.logger import get_logger
from .dependencies import get_db, handle_errors, get_date_filter, DateFilterParams

router = APIRouter(prefix="/api/field-visits")
logger = get_logger("field_visits_controller")


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
    with db._get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT DISTINCT assigned_to FROM site_visits
            WHERE assigned_to IS NOT NULL AND assigned_to != ''
            ORDER BY assigned_to
        """)
        return {"staff": [row["assigned_to"] for row in cursor.fetchall()]}


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
    with db._get_connection() as conn:
        cursor = conn.cursor()

        # Build update query based on provided fields
        updates = []
        params = []

        if "scheduled_time" in data:
            updates.append("scheduled_time = ?")
            params.append(data["scheduled_time"])

        if "assigned_to" in data:
            updates.append("assigned_to = ?")
            params.append(data["assigned_to"])

        if "status" in data:
            updates.append("status = ?")
            params.append(data["status"])

        if not updates:
            return JSONResponse(content={"success": False, "message": "No fields to update"})

        updates.append("updated_at = ?")
        params.append(now_maldives())
        params.append(visit_id)

        cursor.execute(f"""
            UPDATE site_visits
            SET {', '.join(updates)}
            WHERE id = ?
        """, params)

        if cursor.rowcount == 0:
            return JSONResponse(content={"success": False, "message": "Site visit not found"})

        # Recalculate duration if scheduled_time was changed and visit is completed
        if "scheduled_time" in data:
            cursor.execute("""
                UPDATE site_visits
                SET time_taken_minutes = CASE
                    WHEN ticket_completed_at IS NOT NULL AND visit_date IS NOT NULL AND scheduled_time IS NOT NULL THEN
                        CAST(
                            (julianday(SUBSTR(ticket_completed_at, 1, 19)) - julianday(visit_date || ' ' ||
                                CASE
                                    WHEN LENGTH(scheduled_time) <= 5 THEN scheduled_time || ':00'
                                    ELSE SUBSTR(scheduled_time, 1, 8)
                                END
                            )) * 24 * 60 AS INTEGER
                        )
                    ELSE time_taken_minutes
                END
                WHERE id = ?
            """, (visit_id,))

        logger.info(f"Updated site visit {visit_id}: {data}")
        return JSONResponse(content={"success": True, "message": "Site visit updated"})
