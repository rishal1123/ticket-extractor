"""
Shared dependencies and utilities for controllers.

This module provides:
- Database dependency injection
- Error handling decorators
- Common query parameter models
- Shared response helpers
"""

import secrets
from functools import wraps
from typing import Optional, Callable, Any
from datetime import datetime

from fastapi import HTTPException, Query, Depends, Header
from pydantic import BaseModel, Field

from database import Database
from utils.logger import get_logger

logger = get_logger("dependencies")


# =============================================================================
# Database Dependency (Singleton pattern)
# =============================================================================

_db_instance: Optional[Database] = None


def get_db() -> Database:
    """
    Get database instance (singleton).
    Use as FastAPI dependency: db: Database = Depends(get_db)
    """
    global _db_instance
    if _db_instance is None:
        _db_instance = Database()
    return _db_instance


def reset_db():
    """Reset database instance (for testing)."""
    global _db_instance
    _db_instance = None


# =============================================================================
# External API Key Guard
# =============================================================================

EXTERNAL_API_KEY_SETTING = "external_api_key"


def verify_external_api_key(
    x_api_key: Optional[str] = Header(default=None, alias="X-API-Key"),
    db: Database = Depends(get_db),
) -> None:
    """Guard for endpoints that expose ticket data to external applications.

    Requires an X-API-Key header matching the key configured via
    POST /api/settings/external-api-key. Fails closed: if no key has been
    configured yet, the endpoint is unreachable (503) rather than open.
    """
    configured_key = db.get_setting(EXTERNAL_API_KEY_SETTING)
    if not configured_key:
        raise HTTPException(status_code=503, detail="External API key not configured")
    if not x_api_key or not secrets.compare_digest(x_api_key, configured_key):
        raise HTTPException(status_code=401, detail="Invalid or missing API key")


# =============================================================================
# Error Handling Decorator
# =============================================================================

def handle_errors(
    operation_name: str = "operation",
    log_errors: bool = True,
    return_detail: bool = True
):
    """
    Decorator for consistent error handling in API endpoints.

    Usage:
        @router.get("/endpoint")
        @handle_errors("get items")
        async def get_items():
            ...

    Args:
        operation_name: Description of the operation for logging
        log_errors: Whether to log errors
        return_detail: Whether to include error details in response
    """
    def decorator(func: Callable):
        @wraps(func)
        async def wrapper(*args, **kwargs):
            try:
                return await func(*args, **kwargs)
            except HTTPException:
                # Re-raise HTTP exceptions as-is
                raise
            except Exception as e:
                if log_errors:
                    logger.error(f"Error in {operation_name}: {type(e).__name__}: {e}")

                detail = str(e) if return_detail else f"Failed to {operation_name}"
                raise HTTPException(status_code=500, detail=detail)

        return wrapper
    return decorator


def handle_not_found(item_type: str = "Item"):
    """
    Decorator to handle not found scenarios.

    Usage:
        @handle_not_found("Ticket")
        async def get_ticket(ticket_id: int):
            ticket = db.get_ticket(ticket_id)
            if not ticket:
                return None  # Will raise 404
            return ticket
    """
    def decorator(func: Callable):
        @wraps(func)
        async def wrapper(*args, **kwargs):
            result = await func(*args, **kwargs)
            if result is None:
                raise HTTPException(status_code=404, detail=f"{item_type} not found")
            return result
        return wrapper
    return decorator


# =============================================================================
# Query Parameter Models
# =============================================================================

class PaginationParams(BaseModel):
    """Common pagination parameters."""
    page: int = Field(default=1, ge=1, description="Page number (1-indexed)")
    page_size: int = Field(default=50, ge=1, le=500, description="Items per page")

    @property
    def offset(self) -> int:
        """Calculate offset for SQL queries."""
        return (self.page - 1) * self.page_size


class DateFilterParams(BaseModel):
    """Common date filter parameters."""
    date_from: Optional[str] = Field(
        default=None,
        description="Start date filter (YYYY-MM-DD)",
        pattern=r"^\d{4}-\d{2}-\d{2}$"
    )
    date_to: Optional[str] = Field(
        default=None,
        description="End date filter (YYYY-MM-DD)",
        pattern=r"^\d{4}-\d{2}-\d{2}$"
    )

    def validate_dates(self) -> bool:
        """Validate that date_from <= date_to if both provided."""
        if self.date_from and self.date_to:
            return self.date_from <= self.date_to
        return True


class TicketFilterParams(BaseModel):
    """Ticket-specific filter parameters."""
    portal: Optional[str] = Field(
        default=None,
        description="Filter by portal (dhiraagu, ooredoo, rol, medianet)"
    )
    status: Optional[str] = Field(default=None, description="Filter by status")
    ticket_type: Optional[str] = Field(default=None, description="Filter by ticket type")
    in_znuny: Optional[bool] = Field(default=None, description="Filter by Znuny status")
    staff: Optional[str] = Field(default=None, description="Filter by staff name")
    search: Optional[str] = Field(default=None, description="Search in ticket ID, address, customer")
    include_completed: bool = Field(default=False, description="Include completed tickets")
    completed_only: bool = Field(default=False, description="Show only completed tickets")


# =============================================================================
# Dependency Functions for FastAPI
# =============================================================================

def get_pagination(
    page: int = Query(1, ge=1, description="Page number"),
    page_size: int = Query(50, ge=1, le=500, description="Items per page")
) -> PaginationParams:
    """FastAPI dependency for pagination parameters."""
    return PaginationParams(page=page, page_size=page_size)


def get_date_filter(
    date_from: Optional[str] = Query(None, description="Start date (YYYY-MM-DD)"),
    date_to: Optional[str] = Query(None, description="End date (YYYY-MM-DD)")
) -> DateFilterParams:
    """FastAPI dependency for date filter parameters."""
    params = DateFilterParams(date_from=date_from, date_to=date_to)
    if not params.validate_dates():
        raise HTTPException(
            status_code=400,
            detail="date_from must be before or equal to date_to"
        )
    return params


# =============================================================================
# Response Helpers
# =============================================================================

def paginated_response(
    items: list,
    total: int,
    pagination: PaginationParams,
    item_key: str = "items"
) -> dict:
    """
    Create a standardized paginated response.

    Args:
        items: List of items for current page
        total: Total count of all items
        pagination: Pagination parameters used
        item_key: Key name for items in response

    Returns:
        Dict with items, total, page info
    """
    total_pages = (total + pagination.page_size - 1) // pagination.page_size

    return {
        item_key: items,
        "total": total,
        "page": pagination.page,
        "page_size": pagination.page_size,
        "total_pages": total_pages,
        "has_next": pagination.page < total_pages,
        "has_prev": pagination.page > 1
    }


def success_response(message: str, data: dict = None) -> dict:
    """Create a standardized success response."""
    response = {"success": True, "message": message}
    if data:
        response.update(data)
    return response


def error_response(message: str, details: dict = None) -> dict:
    """Create a standardized error response."""
    response = {"success": False, "error": message}
    if details:
        response["details"] = details
    return response
