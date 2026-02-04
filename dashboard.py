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


def get_znuny_search_term(ticket) -> str:
    """Get the appropriate search term for Znuny based on portal.

    For ROL: Use account (display ID like ROL250141) since that's what appears in Znuny titles.
    For others: Use ticket_id.
    """
    if ticket.portal == "rol" and ticket.account:
        return ticket.account
    return ticket.ticket_id


@app.get("/", response_class=HTMLResponse)
async def dashboard(request: Request):
    """Main dashboard page."""
    return templates.TemplateResponse("dashboard.html", {"request": request})


@app.get("/tickets", response_class=HTMLResponse)
async def tickets_page(request: Request):
    """All tickets page with search and filters."""
    return templates.TemplateResponse("tickets.html", {"request": request})


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
    include_completed: Optional[bool] = Query(False, description="Include completed tickets"),
    completed_only: Optional[bool] = Query(False, description="Show only completed tickets"),
    limit: int = Query(100, ge=1, le=1000),
    offset: int = Query(0, ge=0)
):
    """Get tickets with optional filters."""
    try:
        tickets = db.get_all_tickets(portal=portal, include_completed=include_completed or completed_only)

        # Filter for completed only if requested
        if completed_only:
            tickets = [t for t in tickets if t.completed_at is not None]

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

        # Check in Znuny (use account for ROL, ticket_id for others)
        search_term = get_znuny_search_term(ticket)
        exists, znuny_ticket_id = await znuny_client.check_ticket_in_znuny(
            search_term,
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
                search_term = get_znuny_search_term(ticket)
                exists, znuny_ticket_id = await znuny_client.check_ticket_in_znuny(
                    search_term,
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


@app.get("/staff", response_class=HTMLResponse)
async def staff_stats_page(request: Request):
    """Staff statistics page."""
    return templates.TemplateResponse("staff_stats.html", {"request": request})


@app.get("/api/staff-stats")
async def get_staff_stats(
    date_from: Optional[str] = Query(None, description="Start date (YYYY-MM-DD)"),
    date_to: Optional[str] = Query(None, description="End date (YYYY-MM-DD)")
):
    """Get staff statistics from Znuny data with optional date filtering."""
    try:
        stats = db.get_staff_stats(date_from=date_from, date_to=date_to)
        return JSONResponse(content=stats)
    except Exception as e:
        logger.error(f"Error getting staff stats: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/sync-znuny-details")
async def sync_znuny_details():
    """Fetch detailed Znuny data (creator, articles) for all tickets in Znuny."""
    try:
        tickets = db.get_all_tickets()
        tickets_in_znuny = [t for t in tickets if t.in_znuny and t.znuny_ticket_id]

        synced = 0
        articles_added = 0

        for ticket in tickets_in_znuny:
            try:
                # Fetch ticket details from Znuny
                details = znuny_client.get_ticket_details(ticket.znuny_ticket_id)
                if details:
                    # Update ticket with Znuny creation info
                    db.update_znuny_details(
                        ticket.id,
                        znuny_created_at=details.created_at,
                        znuny_created_by=details.created_by,
                        znuny_address=details.address
                    )

                    # Store articles
                    for article in details.articles:
                        db.upsert_znuny_article(
                            ticket_id=ticket.id,
                            znuny_ticket_id=ticket.znuny_ticket_id,
                            article_number=article.article_number,
                            sender=article.sender,
                            via=article.via,
                            subject=article.subject,
                            created_at=article.created_at,
                            created_at_str=article.created_at_str,
                            created_by=article.created_by,
                            body=article.body
                        )
                        articles_added += 1

                    synced += 1
            except Exception as e:
                logger.warning(f"Error syncing ticket {ticket.znuny_ticket_id}: {e}")
                continue

        # Clear cache after sync
        znuny_client.clear_cache()

        return JSONResponse(content={
            "tickets_synced": synced,
            "articles_added": articles_added
        })
    except Exception as e:
        logger.error(f"Error syncing Znuny details: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/tickets/{ticket_id}/sync-znuny")
async def sync_single_ticket_znuny(ticket_id: int):
    """Fetch detailed Znuny data for a single ticket."""
    try:
        ticket = db.get_ticket_by_id(ticket_id)
        if not ticket:
            raise HTTPException(status_code=404, detail="Ticket not found")

        if not ticket.in_znuny or not ticket.znuny_ticket_id:
            return JSONResponse(content={
                "success": False,
                "message": "Ticket not in Znuny"
            })

        # Fetch ticket details from Znuny
        details = znuny_client.get_ticket_details(ticket.znuny_ticket_id)
        if not details:
            return JSONResponse(content={
                "success": False,
                "message": "Could not fetch ticket details from Znuny"
            })

        # Update ticket with Znuny creation info
        db.update_znuny_details(
            ticket.id,
            znuny_created_at=details.created_at,
            znuny_created_by=details.created_by,
            znuny_address=details.address
        )

        # Store articles
        articles_added = 0
        for article in details.articles:
            db.upsert_znuny_article(
                ticket_id=ticket.id,
                znuny_ticket_id=ticket.znuny_ticket_id,
                article_number=article.article_number,
                sender=article.sender,
                via=article.via,
                subject=article.subject,
                created_at=article.created_at,
                created_at_str=article.created_at_str,
                created_by=article.created_by,
                body=article.body
            )
            articles_added += 1

        return JSONResponse(content={
            "success": True,
            "articles_synced": articles_added,
            "znuny_created_by": details.created_by,
            "znuny_address": details.address
        })
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error syncing Znuny for ticket {ticket_id}: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/tickets/{ticket_id}/znuny-articles")
async def get_ticket_znuny_articles(ticket_id: int):
    """Get Znuny articles for a ticket."""
    try:
        ticket = db.get_ticket_by_id(ticket_id)
        if not ticket:
            raise HTTPException(status_code=404, detail="Ticket not found")

        articles = db.get_znuny_articles(ticket_id=ticket_id)

        # Calculate time difference if both times are available
        time_diff = None
        time_diff_minutes = None
        if ticket.portal_created_at and ticket.znuny_created_at:
            diff = ticket.znuny_created_at - ticket.portal_created_at
            time_diff_minutes = int(diff.total_seconds() / 60)
            hours, minutes = divmod(abs(time_diff_minutes), 60)
            days, hours = divmod(hours, 24)
            if days > 0:
                time_diff = f"{days}d {hours}h {minutes}m"
            elif hours > 0:
                time_diff = f"{hours}h {minutes}m"
            else:
                time_diff = f"{minutes}m"

        return JSONResponse(content={
            "znuny_ticket_id": ticket.znuny_ticket_id,
            "znuny_created_at": ticket.znuny_created_at.isoformat() if ticket.znuny_created_at else None,
            "znuny_created_by": ticket.znuny_created_by,
            "portal_created_at": ticket.portal_created_at.isoformat() if ticket.portal_created_at else None,
            "time_diff": time_diff,
            "time_diff_minutes": time_diff_minutes,
            "articles": articles
        })
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error getting Znuny articles for ticket {ticket_id}: {e}")
        raise HTTPException(status_code=500, detail=str(e))


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
