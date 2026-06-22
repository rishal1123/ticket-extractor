"""
Pages Controller - Handles HTML page routes.
"""

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
import os

from config import Config, APP_VERSION
from database import Database
from utils.ticket_formatter import format_ticket_dump

router = APIRouter()

# Setup templates
templates_dir = os.path.join(os.path.dirname(os.path.dirname(__file__)), "templates")
templates = Jinja2Templates(directory=templates_dir)
templates.env.globals["app_version"] = APP_VERSION


def _get_portal_urls():
    """Get portal URLs from config."""
    return Config.get_portal_urls()


@router.get("/", response_class=HTMLResponse)
async def dashboard_page(request: Request):
    """Render the main dashboard page."""
    return templates.TemplateResponse(
        "dashboard.html",
        {
            "request": request,
            "active_page": "dashboard",
            "portal_urls": _get_portal_urls()
        }
    )


@router.get("/tickets", response_class=HTMLResponse)
async def tickets_page(request: Request):
    """Render the tickets list page."""
    return templates.TemplateResponse(
        "tickets.html",
        {"request": request, "active_page": "tickets"}
    )


@router.get("/tickets/{ticket_id}/format", response_class=HTMLResponse)
async def ticket_format_page(request: Request, ticket_id: int):
    """Standalone page (opened in a new tab) showing the standardized, formatted
    ticket block for an ISP ticket, generated from its captured portal dump."""
    db = Database()
    ticket = db.get_ticket_by_id(ticket_id)
    if not ticket:
        result = {"ok": False, "isp": None, "text": "", "missing": [], "warnings": [],
                  "error": "Ticket not found."}
        meta = {"ticket_id": ticket_id, "portal": ""}
    else:
        result = format_ticket_dump(ticket.raw_dump, ticket.portal)
        meta = {"ticket_id": ticket.ticket_id, "portal": ticket.portal,
                "customer_name": ticket.customer_name or ""}
    return templates.TemplateResponse(
        "ticket_format.html",
        {"request": request, "result": result, "meta": meta}
    )


@router.get("/staff", response_class=HTMLResponse)
async def staff_page(request: Request):
    """Render the staff statistics page."""
    return templates.TemplateResponse(
        "staff_stats.html",
        {"request": request, "active_page": "staff"}
    )


@router.get("/staff/{name}", response_class=HTMLResponse)
async def staff_detail_page(request: Request, name: str):
    """Render the individual staff detail page."""
    return templates.TemplateResponse(
        "staff_detail.html",
        {"request": request, "active_page": "staff", "staff_name": name}
    )


@router.get("/field-visits", response_class=HTMLResponse)
async def field_visits_page(request: Request):
    """Render the field visits page."""
    return templates.TemplateResponse(
        "field_visits.html",
        {"request": request, "active_page": "field-visits"}
    )


@router.get("/znuny-tickets", response_class=HTMLResponse)
async def znuny_tickets_page(request: Request):
    """Render the Znuny-only tickets page."""
    return templates.TemplateResponse(
        "znuny_tickets.html",
        {"request": request, "active_page": "znuny-tickets"}
    )


@router.get("/reports", response_class=HTMLResponse)
async def reports_page(request: Request):
    """Render the reports page."""
    return templates.TemplateResponse(
        "reports.html",
        {"request": request, "active_page": "reports"}
    )


@router.get("/customer-report", response_class=HTMLResponse)
async def customer_report_page(request: Request):
    """Render the customer performance report page (its own tab)."""
    return templates.TemplateResponse(
        "customer_report.html",
        {"request": request, "active_page": "customer-report"}
    )


@router.get("/reports/customer-performance", response_class=HTMLResponse)
async def customer_performance_report_view(request: Request):
    """Serve the generated UD-/DH- customer performance report HTML (for the iframe)."""
    from services.customer_report_service import CustomerReportService
    html = CustomerReportService.get_html()
    if html is None:
        return HTMLResponse(
            "<!doctype html><body style='font-family:-apple-system,Segoe UI,sans-serif;"
            "padding:32px;color:#555'>The customer performance report has not been generated "
            "yet. Use the <b>Generate Now</b> button on the Reports page (it takes 1-2 minutes)."
            "</body>"
        )
    return HTMLResponse(html)


@router.get("/admin", response_class=HTMLResponse)
async def admin_page(request: Request):
    """Render the admin panel page."""
    return templates.TemplateResponse(
        "admin.html",
        {"request": request, "active_page": "admin"}
    )


@router.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    """Render the login page."""
    return templates.TemplateResponse(
        "login.html",
        {"request": request, "active_page": "login"}
    )
