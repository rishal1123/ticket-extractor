"""
Controllers layer - Route handlers for the application.
Following MVC pattern: Controllers handle HTTP requests and delegate to services.
"""

from fastapi import APIRouter

# Import all routers
from .pages import router as pages_router
from .api import router as api_router
from .admin import router as admin_router

__all__ = [
    'pages_router',
    'api_router',
    'admin_router'
]
