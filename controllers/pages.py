"""
Pages Controller - Handles HTML page routes.
"""

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
import os

from config import Config, APP_VERSION

router = APIRouter()

# Setup templates
templates_dir = os.path.join(os.path.dirname(os.path.dirname(__file__)), "templates")
templates = Jinja2Templates(directory=templates_dir)
templates.env.globals["app_version"] = APP_VERSION


def _get_portal_urls():
    """Get portal URLs from config."""
    return {
        "dhiraagu": Config.DHIRAAGU.url,
        "ooredoo": Config.OOREDOO.url,
        "rol": Config.ROL.url,
        "medianet": Config.MEDIANET.url
    }


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
