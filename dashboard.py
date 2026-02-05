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
from services.znuny_service import ZnunyService
from config import Config, APP_VERSION
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


def ticket_to_dict_with_urls(ticket) -> dict:
    """Convert ticket to dict with generated portal URL."""
    data = ticket.to_dict()
    data["portal_url"] = _generate_portal_url(ticket)
    return data


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


def sync_znuny_comprehensive():
    """Comprehensive Znuny sync: all tickets, site visits, and ISP ticket linking."""
    logger.info("Starting comprehensive Znuny sync")
    sync_db = Database()

    sync_db.log_system("info", "znuny", "Comprehensive Znuny sync started")

    try:
        znuny_service = ZnunyService(sync_db)
        results = znuny_service.sync_all_site_visits()

        logger.info(f"Znuny sync complete: {results}")
        sync_db.log_system(
            "info" if results["errors"] == 0 else "warning",
            "znuny",
            f"Znuny sync complete: {results['znuny_tickets_found']} tickets, {results['site_visits_extracted']} site visits",
            f"Tickets: {results['znuny_tickets_found']}, ISP synced: {results['isp_tickets_synced']}, "
            f"Site visits: {results['site_visits_extracted']}, Linked: {results['site_visits_linked']}, "
            f"Errors: {results['errors']}"
        )
        return results
    except Exception as e:
        logger.error(f"Comprehensive Znuny sync failed: {e}")
        sync_db.log_system("error", "znuny", f"Znuny sync failed: {e}")
        raise


def scheduled_job():
    """Combined job that runs portal extraction and comprehensive Znuny sync."""
    job_db = Database()
    try:
        run_portal_extraction()
    except Exception as e:
        logger.error(f"Portal extraction failed: {e}")
        job_db.log_system("error", "scheduler", f"Portal extraction crashed: {e}")

    try:
        sync_znuny_comprehensive()
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
templates.env.globals["app_version"] = APP_VERSION

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

        # Get last extraction time
        last_extraction = db.get_last_extraction_per_portal()
        last_sync_time = None
        if last_extraction:
            # Get the most recent extraction time from any portal
            for portal, info in last_extraction.items():
                if info.get("extracted_at"):
                    if last_sync_time is None or info["extracted_at"] > last_sync_time:
                        last_sync_time = info["extracted_at"]

        return JSONResponse(content={
            "in_znuny": in_znuny,
            "not_in_znuny": not_in_znuny,
            "details_synced": details_synced,
            "pending_sync": pending_sync,
            "last_sync_time": last_sync_time
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
            "tickets": [ticket_to_dict_with_urls(t) for t in tickets]
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
            "ticket": ticket_to_dict_with_urls(ticket),
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
                        znuny_created_at=details.created_at,
                        znuny_created_by=details.created_by,
                        znuny_address=details.address,
                        znuny_url=details.znuny_url
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
            znuny_address=details.address,
            znuny_url=details.znuny_url
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


@app.get("/api/tickets/{ticket_id}/site-visits")
async def get_ticket_site_visits(ticket_id: int):
    """Get site visits for a ticket."""
    try:
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


@app.get("/api/staff-stats-detailed")
async def get_staff_stats_detailed(
    date_from: Optional[str] = Query(None, description="Start date (YYYY-MM-DD)"),
    date_to: Optional[str] = Query(None, description="End date (YYYY-MM-DD)")
):
    """Get detailed staff statistics including on-time percentages."""
    try:
        thresholds = db.get_performance_thresholds()
        stats = db.get_staff_detailed_stats(date_from=date_from, date_to=date_to, thresholds=thresholds)
        stats["thresholds"] = thresholds
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
            "tickets": [ticket_to_dict_with_urls(t) for t in result["tickets"]]
        })
    except Exception as e:
        logger.error(f"Error getting staff tickets: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/staff/{staff_name}/performance")
async def get_staff_performance(staff_name: str, days: int = Query(30, ge=1, le=365)):
    """Get performance trend for a staff member."""
    try:
        thresholds = db.get_performance_thresholds()
        trend = db.get_staff_performance_trend(staff_name, days, thresholds)
        return JSONResponse(content={"staff_name": staff_name, "trend": trend, "thresholds": thresholds})
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
                diff = (t.created_at - t.znuny_created_at).total_seconds() / 60
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


# ==================== Field Visits / Site Visits API ====================

@app.get("/field-visits", response_class=HTMLResponse)
async def field_visits_page(request: Request):
    """Render the field visits page."""
    return templates.TemplateResponse(
        "field_visits.html",
        {"request": request, "active_page": "field-visits"}
    )


@app.get("/api/field-visits")
async def get_field_visits(
    date_from: Optional[str] = Query(None),
    date_to: Optional[str] = Query(None),
    assigned_to: Optional[str] = Query(None),
    status: Optional[str] = Query(None),
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0)
):
    """Get site visits with optional filters."""
    try:
        result = db.get_site_visits(
            date_from=date_from, date_to=date_to,
            assigned_to=assigned_to, status=status,
            limit=limit, offset=offset
        )
        return result
    except Exception as e:
        logger.error(f"Error getting field visits: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/field-visits/staff-stats")
async def get_field_visits_staff_stats(
    date_from: Optional[str] = Query(None),
    date_to: Optional[str] = Query(None)
):
    """Get site visit statistics by assigned staff."""
    try:
        stats = db.get_site_visit_staff_stats(date_from, date_to)
        return {"staff": stats}
    except Exception as e:
        logger.error(f"Error getting field visits staff stats: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/field-visits/by-date")
async def get_field_visits_by_date(
    date_from: Optional[str] = Query(None),
    date_to: Optional[str] = Query(None)
):
    """Get site visits aggregated by date."""
    try:
        data = db.get_site_visit_by_date(date_from, date_to)
        return {"dates": data}
    except Exception as e:
        logger.error(f"Error getting field visits by date: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/field-visits/assigned-staff")
async def get_field_visits_assigned_staff():
    """Get list of staff who have been assigned field visits."""
    try:
        with db._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT DISTINCT assigned_to FROM site_visits
                WHERE assigned_to IS NOT NULL AND assigned_to != ''
                ORDER BY assigned_to
            """)
            return {"staff": [row["assigned_to"] for row in cursor.fetchall()]}
    except Exception as e:
        logger.error(f"Error getting assigned staff: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/field-visits/sync")
async def sync_site_visits():
    """
    Sync all site visits from Znuny.
    Searches for site visit tickets, extracts visits, and links to ISP tickets.
    """
    try:
        from services import ZnunyService
        znuny_service = ZnunyService(db)

        def run_sync():
            return znuny_service.sync_all_site_visits()

        # Run in background thread
        import threading
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
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error syncing site visits: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.put("/api/field-visits/{visit_id}")
async def update_site_visit(visit_id: int, request: Request):
    """Update a site visit (scheduled time, assigned_to, etc.)."""
    try:
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

            logger.info(f"Updated site visit {visit_id}: {data}")
            return JSONResponse(content={"success": True, "message": "Site visit updated"})
    except Exception as e:
        logger.error(f"Error updating site visit: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# Admin Authentication
@app.post("/api/admin/login")
async def admin_login(request: Request):
    """Authenticate admin user."""
    try:
        data = await request.json()
        password = data.get("password", "")

        stored_password = db.get_setting("admin_password", "admin123")

        if password == stored_password:
            return JSONResponse(content={"success": True, "message": "Login successful"})
        else:
            return JSONResponse(content={"success": False, "message": "Invalid password"}, status_code=401)
    except Exception as e:
        logger.error(f"Error in admin login: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/admin/change-password")
async def change_admin_password(request: Request):
    """Change admin password."""
    try:
        data = await request.json()
        current_password = data.get("current_password", "")
        new_password = data.get("new_password", "")

        stored_password = db.get_setting("admin_password", "admin123")

        if current_password != stored_password:
            return JSONResponse(content={"success": False, "message": "Current password is incorrect"}, status_code=401)

        if len(new_password) < 4:
            return JSONResponse(content={"success": False, "message": "Password must be at least 4 characters"})

        db.set_setting("admin_password", new_password, "Password for admin panel access")
        logger.info("Admin password changed")
        return JSONResponse(content={"success": True, "message": "Password changed successfully"})
    except Exception as e:
        logger.error(f"Error changing admin password: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# Config API endpoints
@app.get("/api/config")
async def get_config():
    """Get current configuration (passwords masked)."""
    try:
        env_path = os.path.join(os.path.dirname(__file__), ".env")
        config = {}

        if os.path.exists(env_path):
            with open(env_path, 'r') as f:
                for line in f:
                    line = line.strip()
                    if line and not line.startswith('#') and '=' in line:
                        key, value = line.split('=', 1)
                        # Mask password values for display
                        if 'PASSWORD' in key.upper():
                            config[key] = '********' if value else ''
                        else:
                            config[key] = value

        return JSONResponse(content={"config": config})
    except Exception as e:
        logger.error(f"Error reading config: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/config/raw")
async def get_config_raw():
    """Get raw configuration including passwords (use with caution)."""
    try:
        env_path = os.path.join(os.path.dirname(__file__), ".env")
        config = {}

        if os.path.exists(env_path):
            with open(env_path, 'r') as f:
                for line in f:
                    line = line.strip()
                    if line and not line.startswith('#') and '=' in line:
                        key, value = line.split('=', 1)
                        config[key] = value

        return JSONResponse(content={"config": config})
    except Exception as e:
        logger.error(f"Error reading config: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/config")
async def update_config(request: Request):
    """Update configuration in .env file."""
    try:
        data = await request.json()
        new_config = data.get('config', {})

        env_path = os.path.join(os.path.dirname(__file__), ".env")

        # Read existing config to preserve passwords if masked
        existing_config = {}
        if os.path.exists(env_path):
            with open(env_path, 'r') as f:
                for line in f:
                    line = line.strip()
                    if line and not line.startswith('#') and '=' in line:
                        key, value = line.split('=', 1)
                        existing_config[key] = value

        # Merge configs - don't overwrite passwords with masked values
        final_config = {}
        for key, value in new_config.items():
            if value == '********' and key in existing_config:
                final_config[key] = existing_config[key]
            else:
                final_config[key] = value

        # Write config with sections
        sections = {
            'Dhiraagu Portal': ['DHIRAAGU_URL', 'DHIRAAGU_USERNAME', 'DHIRAAGU_PASSWORD'],
            'Ooredoo Portal': ['OOREDOO_URL', 'OOREDOO_USERNAME', 'OOREDOO_PASSWORD'],
            'ROL Portal': ['ROL_URL', 'ROL_USERNAME', 'ROL_PASSWORD'],
            'Medianet Portal': ['MEDIANET_URL', 'MEDIANET_USERNAME', 'MEDIANET_PASSWORD'],
            'Znuny API': ['ZNUNY_URL', 'ZNUNY_USERNAME', 'ZNUNY_PASSWORD'],
            'Scheduler settings': ['EXTRACTION_INTERVAL_MINUTES'],
            'Dashboard settings': ['DASHBOARD_HOST', 'DASHBOARD_PORT']
        }

        with open(env_path, 'w') as f:
            for section_name, keys in sections.items():
                f.write(f"# {section_name}\n")
                for key in keys:
                    if key in final_config:
                        f.write(f"{key}={final_config[key]}\n")
                f.write("\n")

        logger.info("Configuration updated successfully")
        return JSONResponse(content={"success": True, "message": "Configuration saved"})
    except Exception as e:
        logger.error(f"Error updating config: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# ==================== Performance Thresholds API ====================

@app.get("/api/settings/performance-thresholds")
async def get_performance_thresholds():
    """Get performance threshold settings."""
    try:
        db = Database()
        thresholds = db.get_performance_thresholds()
        return JSONResponse(content={"success": True, "thresholds": thresholds})
    except Exception as e:
        logger.error(f"Error getting performance thresholds: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/settings/performance-thresholds")
async def update_performance_thresholds(request: Request):
    """Update performance threshold settings."""
    try:
        data = await request.json()
        db = Database()
        db.set_performance_thresholds(
            good=int(data.get("good", 5)),
            warning=int(data.get("warning", 10)),
            bad=int(data.get("bad", 30)),
            critical=int(data.get("critical", 60))
        )
        logger.info(f"Performance thresholds updated: {data}")
        return JSONResponse(content={"success": True, "message": "Thresholds saved"})
    except Exception as e:
        logger.error(f"Error updating performance thresholds: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/settings")
async def get_all_settings():
    """Get all app settings."""
    try:
        db = Database()
        settings = db.get_all_settings()
        return JSONResponse(content={"success": True, "settings": settings})
    except Exception as e:
        logger.error(f"Error getting settings: {e}")
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
