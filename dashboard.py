from fastapi import FastAPI, Request, HTTPException, Query
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from typing import Optional
from contextlib import asynccontextmanager
import os
import threading
import time

import schedule

from database import Database, now_maldives
from znuny_client import ZnunyClient
from config import Config
from utils.logger import get_logger
from extractors import DhiraaguExtractor, OoredooExtractor, ROLExtractor, MedianetExtractor

logger = get_logger("dashboard")

# Global flag for scheduler
scheduler_running = False
scheduler_thread = None


def get_extractor_class(portal_name: str):
    """Get the extractor class for a portal name."""
    extractors = {
        "dhiraagu": DhiraaguExtractor,
        "ooredoo": OoredooExtractor,
        "rol": ROLExtractor,
        "medianet": MedianetExtractor
    }
    return extractors.get(portal_name.lower())


def run_portal_extraction():
    """Run extraction for all configured portals."""
    logger.info("Starting scheduled portal extraction")
    extraction_db = Database()
    extraction_db.log_system("info", "scheduler", "Portal extraction started")
    results = []

    for config in Config.get_all_portals():
        if not config.url or not config.username:
            logger.warning(f"Portal {config.name} not configured, skipping")
            extraction_db.log_system("warning", f"extractor.{config.name}", "Portal not configured, skipping")
            continue

        extractor_class = get_extractor_class(config.name)
        if not extractor_class:
            logger.warning(f"No extractor for {config.name}, skipping")
            continue

        try:
            logger.info(f"Extracting from {config.name}")
            extractor = extractor_class(config, extraction_db, headless=True)
            result = extractor.run()
            results.append(result)
            extraction_db.log_system(
                "info", f"extractor.{config.name}",
                f"Extraction complete: {result.get('tickets_found', 0)} found, {result.get('tickets_new', 0)} new",
                f"Status: {result.get('status')}"
            )
        except Exception as e:
            logger.error(f"Extraction failed for {config.name}: {e}")
            results.append({"portal": config.name, "status": "failed", "error": str(e)})
            extraction_db.log_system("error", f"extractor.{config.name}", f"Extraction failed: {e}")

        time.sleep(2)  # Small delay between portals

    # Summary
    total_found = sum(r.get("tickets_found", 0) for r in results)
    total_new = sum(r.get("tickets_new", 0) for r in results)
    failed = [r["portal"] for r in results if r.get("status") == "failed"]

    logger.info(f"Portal extraction complete: {total_found} found, {total_new} new")
    summary = f"Total: {total_found} found, {total_new} new"
    if failed:
        logger.warning(f"Failed portals: {', '.join(failed)}")
        summary += f", Failed: {', '.join(failed)}"
        extraction_db.log_system("warning", "scheduler", f"Extraction completed with failures", summary)
    else:
        extraction_db.log_system("info", "scheduler", "Extraction completed successfully", summary)

    return results


def sync_znuny_for_tickets():
    """Sync Znuny status and details for tickets not yet checked."""
    logger.info("Starting Znuny sync for unchecked tickets")
    sync_db = Database()
    sync_znuny_client = ZnunyClient()

    tickets = sync_db.get_all_tickets()
    tickets_to_check = [t for t in tickets if not t.in_znuny]

    sync_db.log_system("info", "znuny", f"Znuny sync started for {len(tickets_to_check)} unchecked tickets")

    updated = 0
    errors = 0
    for ticket in tickets_to_check:
        try:
            search_term = ticket.account if ticket.portal == "rol" and ticket.account else ticket.ticket_id
            exists, znuny_ticket_id = sync_znuny_client.check_ticket_sync(search_term)
            if exists:
                sync_db.update_znuny_status(ticket.id, True, znuny_ticket_id)
                updated += 1

                # Fetch details
                details = sync_znuny_client.get_ticket_details(znuny_ticket_id)
                if details:
                    sync_db.update_znuny_details(
                        ticket.id,
                        znuny_created_at=now_maldives(),
                        znuny_created_by=details.created_by,
                        znuny_address=details.address
                    )
                    for article in details.articles:
                        sync_db.upsert_znuny_article(
                            ticket_id=ticket.id,
                            znuny_ticket_id=znuny_ticket_id,
                            article_number=article.article_number,
                            sender=article.sender,
                            via=article.via,
                            subject=article.subject,
                            created_at=article.created_at,
                            created_at_str=article.created_at_str,
                            created_by=article.created_by,
                            body=article.body
                        )
        except Exception as e:
            logger.warning(f"Error checking ticket {ticket.ticket_id}: {e}")
            errors += 1
            continue

    logger.info(f"Znuny sync complete: {updated} tickets found in Znuny")
    sync_db.log_system(
        "info" if errors == 0 else "warning",
        "znuny",
        f"Znuny sync complete: {updated} found, {errors} errors",
        f"Checked: {len(tickets_to_check)}, Updated: {updated}, Errors: {errors}"
    )
    return updated


def scheduled_job():
    """Combined job that runs portal extraction and Znuny sync."""
    job_db = Database()
    try:
        run_portal_extraction()
    except Exception as e:
        logger.error(f"Portal extraction failed: {e}")
        job_db.log_system("error", "scheduler", f"Portal extraction crashed: {e}")

    try:
        sync_znuny_for_tickets()
    except Exception as e:
        logger.error(f"Znuny sync failed: {e}")
        job_db.log_system("error", "scheduler", f"Znuny sync crashed: {e}")


def scheduler_loop():
    """Background scheduler loop."""
    global scheduler_running
    interval = Config.EXTRACTION_INTERVAL_MINUTES
    logger.info(f"Scheduler started with {interval} minute interval")

    scheduler_db = Database()
    scheduler_db.log_system("info", "scheduler", f"Scheduler started with {interval} minute interval")

    # Schedule the job
    schedule.every(interval).minutes.do(scheduled_job)

    # Run immediately on start
    scheduled_job()

    while scheduler_running:
        schedule.run_pending()
        time.sleep(1)

    logger.info("Scheduler stopped")
    scheduler_db.log_system("info", "scheduler", "Scheduler stopped")


def start_scheduler():
    """Start the background scheduler thread."""
    global scheduler_running, scheduler_thread
    if scheduler_thread and scheduler_thread.is_alive():
        logger.info("Scheduler already running")
        return

    scheduler_running = True
    scheduler_thread = threading.Thread(target=scheduler_loop, daemon=True, name="ExtractionScheduler")
    scheduler_thread.start()
    logger.info("Background scheduler thread started")


def stop_scheduler():
    """Stop the background scheduler."""
    global scheduler_running
    scheduler_running = False
    logger.info("Scheduler stop requested")


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Manage app lifespan - start scheduler on startup."""
    start_scheduler()
    yield
    stop_scheduler()


app = FastAPI(title="Ticket Extractor Dashboard", lifespan=lifespan)

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
    return templates.TemplateResponse("dashboard.html", {"request": request, "active_page": "dashboard"})


@app.get("/tickets", response_class=HTMLResponse)
async def tickets_page(request: Request):
    """All tickets page with search and filters."""
    return templates.TemplateResponse("tickets.html", {"request": request, "active_page": "tickets"})


@app.get("/api/stats")
async def get_stats():
    """Get dashboard statistics."""
    try:
        stats = db.get_stats()
        return JSONResponse(content=stats)
    except Exception as e:
        logger.error(f"Error getting stats: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/znuny-sync-status")
async def get_znuny_sync_status():
    """Get Znuny sync status summary."""
    try:
        tickets = db.get_all_tickets(include_completed=False)

        in_znuny = sum(1 for t in tickets if t.in_znuny)
        not_in_znuny = sum(1 for t in tickets if not t.in_znuny)
        details_synced = sum(1 for t in tickets if t.in_znuny and t.znuny_created_by)
        pending_sync = sum(1 for t in tickets if t.in_znuny and not t.znuny_created_by)

        return JSONResponse(content={
            "in_znuny": in_znuny,
            "not_in_znuny": not_in_znuny,
            "details_synced": details_synced,
            "pending_sync": pending_sync
        })
    except Exception as e:
        logger.error(f"Error getting Znuny sync status: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/tickets")
async def get_tickets(
    portal: Optional[str] = Query(None, description="Filter by portal"),
    status: Optional[str] = Query(None, description="Filter by status"),
    ticket_type: Optional[str] = Query(None, description="Filter by ticket type"),
    in_znuny: Optional[bool] = Query(None, description="Filter by Znuny status"),
    search: Optional[str] = Query(None, description="Search term"),
    staff: Optional[str] = Query(None, description="Filter by staff who created the ticket"),
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

        if staff:
            tickets = [t for t in tickets if t.znuny_created_by and staff.lower() == t.znuny_created_by.lower()]

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


@app.get("/admin", response_class=HTMLResponse)
async def admin_page(request: Request):
    """Admin page with login stats and extraction logs."""
    return templates.TemplateResponse("admin.html", {"request": request, "active_page": "admin"})


@app.get("/api/admin/login-stats")
async def get_login_stats(limit: int = Query(100, ge=1, le=500)):
    """Get recent login events."""
    try:
        logs = db.get_login_stats(limit=limit)
        return JSONResponse(content={"logs": logs})
    except Exception as e:
        logger.error(f"Error getting login stats: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/admin/login-summary")
async def get_login_summary():
    """Get login statistics summary."""
    try:
        summary = db.get_login_summary()
        return JSONResponse(content=summary)
    except Exception as e:
        logger.error(f"Error getting login summary: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/admin/scheduler-status")
async def get_scheduler_status():
    """Get scheduler status and next run time."""
    try:
        jobs = schedule.get_jobs()
        next_run = None
        if jobs:
            next_run = str(jobs[0].next_run) if jobs[0].next_run else None

        return JSONResponse(content={
            "running": scheduler_running,
            "interval_minutes": Config.EXTRACTION_INTERVAL_MINUTES,
            "jobs_count": len(jobs),
            "next_run": next_run
        })
    except Exception as e:
        return JSONResponse(content={
            "running": False,
            "interval_minutes": Config.EXTRACTION_INTERVAL_MINUTES,
            "jobs_count": 0,
            "next_run": None,
            "error": str(e)
        })


@app.post("/api/admin/trigger-extraction")
async def trigger_extraction():
    """Manually trigger portal extraction and Znuny sync."""
    try:
        # Run in a background thread to not block the response
        def run_async():
            scheduled_job()

        thread = threading.Thread(target=run_async, daemon=True)
        thread.start()

        return JSONResponse(content={
            "success": True,
            "message": "Extraction triggered in background"
        })
    except Exception as e:
        logger.error(f"Error triggering extraction: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/admin/system-logs")
async def get_system_logs(
    level: Optional[str] = Query(None, description="Filter by log level"),
    source: Optional[str] = Query(None, description="Filter by source"),
    search: Optional[str] = Query(None, description="Search in message/details"),
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0)
):
    """Get system logs with optional filtering."""
    try:
        logs = db.get_system_logs(
            level=level,
            source=source,
            search=search,
            limit=limit,
            offset=offset
        )
        return JSONResponse(content=logs)
    except Exception as e:
        logger.error(f"Error getting system logs: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/admin/log-stats")
async def get_log_stats():
    """Get system log statistics."""
    try:
        stats = db.get_log_stats()
        return JSONResponse(content=stats)
    except Exception as e:
        logger.error(f"Error getting log stats: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/admin/clear-old-logs")
async def clear_old_logs(days: int = Query(30, ge=1, le=365)):
    """Clear system logs older than specified days."""
    try:
        deleted = db.clear_old_logs(days)
        return JSONResponse(content={
            "success": True,
            "deleted": deleted,
            "message": f"Cleared {deleted} logs older than {days} days"
        })
    except Exception as e:
        logger.error(f"Error clearing old logs: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/staff", response_class=HTMLResponse)
async def staff_stats_page(request: Request):
    """Staff statistics page."""
    return templates.TemplateResponse("staff_stats.html", {"request": request, "active_page": "staff"})


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
                        znuny_created_at=now_maldives(),
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
            znuny_created_at=now_maldives(),
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


@app.get("/api/staff-stats-detailed")
async def get_staff_stats_detailed(
    date_from: Optional[str] = Query(None, description="Start date (YYYY-MM-DD)"),
    date_to: Optional[str] = Query(None, description="End date (YYYY-MM-DD)")
):
    """Get detailed staff statistics including on-time percentages."""
    try:
        stats = db.get_staff_detailed_stats(date_from=date_from, date_to=date_to)
        return JSONResponse(content=stats)
    except Exception as e:
        logger.error(f"Error getting detailed staff stats: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/staff/{staff_name}", response_class=HTMLResponse)
async def staff_detail_page(request: Request, staff_name: str):
    """Individual staff detail page."""
    return templates.TemplateResponse("staff_detail.html", {
        "request": request,
        "staff_name": staff_name,
        "active_page": "staff"
    })


@app.get("/api/staff/{staff_name}/tickets")
async def get_staff_tickets(
    staff_name: str,
    date_from: Optional[str] = Query(None),
    date_to: Optional[str] = Query(None),
    limit: int = Query(100, ge=1, le=1000),
    offset: int = Query(0, ge=0)
):
    """Get tickets created by a specific staff member."""
    try:
        result = db.get_staff_tickets(staff_name, date_from, date_to, limit, offset)
        return JSONResponse(content={
            "staff_name": result["staff_name"],
            "total": result["total"],
            "tickets": [t.to_dict() for t in result["tickets"]]
        })
    except Exception as e:
        logger.error(f"Error getting staff tickets: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/staff/{staff_name}/performance")
async def get_staff_performance(staff_name: str, days: int = Query(30, ge=1, le=365)):
    """Get performance trend for a staff member."""
    try:
        trend = db.get_staff_performance_trend(staff_name, days)
        return JSONResponse(content={"staff_name": staff_name, "trend": trend})
    except Exception as e:
        logger.error(f"Error getting staff performance: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/staff-delays")
async def get_staff_delays(
    min_delay: int = Query(5, ge=1),
    date_from: Optional[str] = Query(None),
    date_to: Optional[str] = Query(None)
):
    """Get delayed tickets grouped by staff."""
    try:
        delays = db.get_delayed_tickets_by_staff(min_delay, date_from, date_to)
        return JSONResponse(content={"delays": delays})
    except Exception as e:
        logger.error(f"Error getting staff delays: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/staff-names")
async def get_staff_names():
    """Get list of all staff names."""
    try:
        names = db.get_all_staff_names()
        return JSONResponse(content={"staff": names})
    except Exception as e:
        logger.error(f"Error getting staff names: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/reports/staff-csv")
async def export_staff_csv(
    date_from: Optional[str] = Query(None),
    date_to: Optional[str] = Query(None)
):
    """Export staff statistics as CSV."""
    try:
        csv_content = db.export_staff_stats_csv(date_from, date_to)

        # Generate filename with date range
        filename = "staff_report"
        if date_from:
            filename += f"_{date_from}"
        if date_to:
            filename += f"_to_{date_to}"
        filename += ".csv"

        return StreamingResponse(
            iter([csv_content]),
            media_type="text/csv",
            headers={"Content-Disposition": f"attachment; filename={filename}"}
        )
    except Exception as e:
        logger.error(f"Error exporting staff CSV: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/tickets-csv")
async def export_tickets_csv(
    portal: Optional[str] = Query(None),
    staff: Optional[str] = Query(None),
    date_from: Optional[str] = Query(None),
    date_to: Optional[str] = Query(None),
    include_completed: bool = Query(True)
):
    """Export tickets as CSV."""
    try:
        tickets = db.get_all_tickets(portal=portal, include_completed=include_completed)

        # Apply filters
        if staff:
            tickets = [t for t in tickets if t.znuny_created_by and staff.lower() in t.znuny_created_by.lower()]
        if date_from:
            tickets = [t for t in tickets if t.znuny_created_at and t.znuny_created_at.strftime("%Y-%m-%d") >= date_from]
        if date_to:
            tickets = [t for t in tickets if t.znuny_created_at and t.znuny_created_at.strftime("%Y-%m-%d") <= date_to]

        # Build CSV
        lines = ["Portal,Ticket ID,Customer,Address,Type,Status,Portal Created,Znuny Created,Created By,Time to Create (min)"]
        for t in tickets:
            time_to_create = ""
            if t.created_at and t.znuny_created_at:
                diff = (t.znuny_created_at - t.created_at).total_seconds() / 60
                time_to_create = str(round(diff, 1))

            # Escape commas in fields
            customer = (t.customer_name or "").replace(",", ";")
            address = (t.address or "").replace(",", ";")

            lines.append(f"{t.portal},{t.ticket_id},{customer},{address},{t.ticket_type or ''},{t.status or ''},{t.portal_created_at or ''},{t.znuny_created_at or ''},{t.znuny_created_by or ''},{time_to_create}")

        csv_content = "\n".join(lines)
        filename = f"tickets_export_{date_from or 'all'}_{date_to or 'all'}.csv"

        return StreamingResponse(
            iter([csv_content]),
            media_type="text/csv",
            headers={"Content-Disposition": f"attachment; filename={filename}"}
        )
    except Exception as e:
        logger.error(f"Error exporting tickets CSV: {e}")
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
