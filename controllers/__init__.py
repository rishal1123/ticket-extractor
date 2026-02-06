"""
Controllers layer - Route handlers for the application.
Following MVC pattern: Controllers handle HTTP requests and delegate to services.
"""

from fastapi import APIRouter

# Import all routers
from .pages import router as pages_router
from .api import router as api_router
from .admin import router as admin_router, settings_router
from .field_visits import router as field_visits_router
from .znuny_only import router as znuny_only_router

__all__ = [
    'pages_router',
    'api_router',
    'admin_router',
    'settings_router',
    'field_visits_router',
    'znuny_only_router'
]
