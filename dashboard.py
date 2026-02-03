from fastapi import FastAPI, Request, HTTPException, Query
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from fastapi.responses import HTMLResponse, JSONResponse
from typing import Optional
import os

from database import Database
from znuny_client import ZnunyClient
from config import Config
from utils.logger import get_logger

logger = get_logger("dashboard")

app = FastAPI(title="Ticket Extractor Dashboard")

# Get the directory where this file is located
BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# Mount static files
app.mount("/static", StaticFiles(directory=os.path.join(BASE_DIR, "static")), name="static")

# Templates
templates = Jinja2Templates(directory=os.path.join(BASE_DIR, "templates"))

# Database instance
db = Database()

# Znuny client
znuny_client = ZnunyClient()


@app.get("/", response_class=HTMLResponse)
async def dashboard(request: Request):
    """Main dashboard page."""
    return templates.TemplateResponse("dashboard.html", {"request": request})


@app.get("/api/stats")
async def get_stats():
    """Get dashboard statistics."""
    try:
        stats = db.get_stats()
        return JSONResponse(content=stats)
    except Exception as e:
        logger.error(f"Error getting stats: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/tickets")
async def get_tickets(
    portal: Optional[str] = Query(None, description="Filter by portal"),
    status: Optional[str] = Query(None, description="Filter by status"),
    ticket_type: Optional[str] = Query(None, description="Filter by ticket type"),
    in_znuny: Optional[bool] = Query(None, description="Filter by Znuny status"),
    search: Optional[str] = Query(None, description="Search term"),
    limit: int = Query(100, ge=1, le=1000),
    offset: int = Query(0, ge=0)
):
    """Get tickets with optional filters."""
    try:
        tickets = db.get_all_tickets(portal=portal)

        # Apply additional filters
        if status:
            tickets = [t for t in tickets if t.status and status.lower() in t.status.lower()]

        if ticket_type:
            tickets = [t for t in tickets if t.ticket_type and ticket_type.lower() in t.ticket_type.lower()]

        if in_znuny is not None:
            tickets = [t for t in tickets if t.in_znuny == in_znuny]

        if search:
            search_lower = search.lower()
            tickets = [t for t in tickets if (
                (t.ticket_id and search_lower in t.ticket_id.lower()) or
                (t.customer_name and search_lower in t.customer_name.lower()) or
                (t.address and search_lower in t.address.lower())
            )]

        # Pagination
        total = len(tickets)
        tickets = tickets[offset:offset + limit]

        return JSONResponse(content={
            "total": total,
            "tickets": [t.to_dict() for t in tickets]
        })
    except Exception as e:
        logger.error(f"Error getting tickets: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/tickets/{ticket_id}")
async def get_ticket(ticket_id: int):
    """Get a single ticket by ID."""
    try:
        ticket = db.get_ticket_by_id(ticket_id)
        if not ticket:
            raise HTTPException(status_code=404, detail="Ticket not found")

        notes_history = db.get_ticket_notes_history(ticket_id)

        return JSONResponse(content={
            "ticket": ticket.to_dict(),
            "notes_history": notes_history
        })
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error getting ticket {ticket_id}: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/tickets/{ticket_id}/check-znuny")
async def check_znuny_status(ticket_id: int):
    """Check if a ticket exists in Znuny and update its status."""
    try:
        ticket = db.get_ticket_by_id(ticket_id)
        if not ticket:
            raise HTTPException(status_code=404, detail="Ticket not found")

        # Check in Znuny
        exists, znuny_ticket_id = await znuny_client.check_ticket_in_znuny(
            ticket.ticket_id,
            ticket.customer_name
        )

        # Update database
        db.update_znuny_status(ticket_id, exists, znuny_ticket_id)

        return JSONResponse(content={
            "in_znuny": exists,
            "znuny_ticket_id": znuny_ticket_id
        })
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error checking Znuny status for ticket {ticket_id}: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/tickets/check-all-znuny")
async def check_all_znuny_status():
    """Check Znuny status for all tickets not yet marked as in Znuny."""
    try:
        tickets = db.get_all_tickets()
        tickets_to_check = [t for t in tickets if not t.in_znuny]

        updated = 0
        for ticket in tickets_to_check:
            try:
                exists, znuny_ticket_id = await znuny_client.check_ticket_in_znuny(
                    ticket.ticket_id,
                    ticket.customer_name
                )
                if exists:
                    db.update_znuny_status(ticket.id, True, znuny_ticket_id)
                    updated += 1
            except Exception as e:
                logger.warning(f"Error checking ticket {ticket.ticket_id}: {e}")
                continue

        return JSONResponse(content={
            "checked": len(tickets_to_check),
            "updated": updated
        })
    except Exception as e:
        logger.error(f"Error checking all Znuny status: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/extraction-logs")
async def get_extraction_logs(limit: int = Query(50, ge=1, le=500)):
    """Get extraction logs."""
    try:
        logs = db.get_extraction_logs(limit=limit)
        return JSONResponse(content={"logs": logs})
    except Exception as e:
        logger.error(f"Error getting extraction logs: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/portals")
async def get_portals():
    """Get list of available portals."""
    portals = [
        {"name": "dhiraagu", "label": "Dhiraagu"},
        {"name": "ooredoo", "label": "Ooredoo"},
        {"name": "rol", "label": "ROL"},
        {"name": "medianet", "label": "Medianet"}
    ]
    return JSONResponse(content={"portals": portals})


def run_dashboard():
    """Run the dashboard server."""
    import uvicorn
    uvicorn.run(
        app,
        host=Config.DASHBOARD_HOST,
        port=Config.DASHBOARD_PORT,
        log_level="info"
    )


if __name__ == "__main__":
    run_dashboard()
