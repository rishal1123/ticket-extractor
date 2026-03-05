"""
Security middleware for rate limiting and input sanitization.
"""

import re
import time
import html
from typing import Dict, Optional, Callable
from collections import defaultdict
from functools import wraps

from fastapi import Request, HTTPException
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware

from utils.logger import get_logger

logger = get_logger("security")


class RateLimiter:
    """
    Simple in-memory rate limiter.
    For production, use Redis-based rate limiting.
    """

    def __init__(self, requests_per_minute: int = 60, requests_per_second: int = 10):
        self.requests_per_minute = requests_per_minute
        self.requests_per_second = requests_per_second
        self.requests: Dict[str, list] = defaultdict(list)

    def _cleanup_old_requests(self, client_id: str, window_seconds: int = 60):
        """Remove requests older than the window. Deletes empty entries to prevent unbounded growth."""
        current_time = time.time()
        self.requests[client_id] = [
            req_time for req_time in self.requests[client_id]
            if current_time - req_time < window_seconds
        ]
        if not self.requests[client_id]:
            del self.requests[client_id]

    def is_rate_limited(self, client_id: str) -> tuple[bool, Optional[int]]:
        """
        Check if client is rate limited.
        Returns (is_limited, retry_after_seconds)
        """
        current_time = time.time()

        # Cleanup old requests
        if client_id in self.requests:
            self._cleanup_old_requests(client_id)

        requests = self.requests.get(client_id, [])

        if not requests:
            return False, None

        # Check per-second limit
        recent_second = [r for r in requests if current_time - r < 1]
        if len(recent_second) >= self.requests_per_second:
            return True, 1

        # Check per-minute limit
        if len(requests) >= self.requests_per_minute:
            oldest = min(requests)
            retry_after = int(60 - (current_time - oldest)) + 1
            return True, retry_after

        return False, None

    def record_request(self, client_id: str):
        """Record a request from a client."""
        self.requests[client_id].append(time.time())


# Global rate limiter instance
rate_limiter = RateLimiter(requests_per_minute=120, requests_per_second=20)


class SecurityMiddleware(BaseHTTPMiddleware):
    """
    Security middleware for FastAPI.
    Handles rate limiting and basic security headers.
    """

    # Paths exempt from rate limiting
    EXEMPT_PATHS = {'/api/health', '/static', '/favicon.ico'}

    async def dispatch(self, request: Request, call_next):
        # Get client identifier
        client_id = self._get_client_id(request)

        # Check if path is exempt
        path = request.url.path
        is_exempt = any(path.startswith(p) for p in self.EXEMPT_PATHS)

        # Apply rate limiting for non-exempt paths
        if not is_exempt:
            is_limited, retry_after = rate_limiter.is_rate_limited(client_id)
            if is_limited:
                logger.warning(f"Rate limit exceeded for {client_id}")
                return JSONResponse(
                    status_code=429,
                    content={
                        "detail": "Too many requests",
                        "retry_after": retry_after
                    },
                    headers={"Retry-After": str(retry_after)}
                )

            rate_limiter.record_request(client_id)

        # Process request
        response = await call_next(request)

        # Add security headers
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["X-XSS-Protection"] = "1; mode=block"
        response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"

        # Prevent browser caching for API and page responses
        if not path.startswith("/static"):
            response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
            response.headers["Pragma"] = "no-cache"
            response.headers["Expires"] = "0"

        return response

    def _get_client_id(self, request: Request) -> str:
        """Get client identifier for rate limiting."""
        # Use X-Forwarded-For if behind proxy, otherwise use client host
        forwarded = request.headers.get("X-Forwarded-For")
        if forwarded:
            return forwarded.split(",")[0].strip()
        return request.client.host if request.client else "unknown"


def sanitize_input(value: str, max_length: int = 1000) -> str:
    """
    Sanitize user input to prevent XSS and injection attacks.

    Args:
        value: Input string to sanitize
        max_length: Maximum allowed length

    Returns:
        Sanitized string
    """
    if not value:
        return value

    # Truncate to max length
    value = value[:max_length]

    # HTML escape
    value = html.escape(value)

    # Remove null bytes
    value = value.replace('\x00', '')

    # Remove control characters except newlines and tabs
    value = re.sub(r'[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]', '', value)

    return value


def sanitize_sql_like(value: str) -> str:
    """
    Sanitize input for SQL LIKE queries.
    Escapes special LIKE characters.
    """
    if not value:
        return value

    # Escape LIKE special characters
    value = value.replace('\\', '\\\\')
    value = value.replace('%', '\\%')
    value = value.replace('_', '\\_')

    return value


def validate_ticket_id(ticket_id: str) -> bool:
    """Validate ticket ID format."""
    if not ticket_id:
        return False

    # Allow alphanumeric, hyphens, and underscores
    pattern = r'^[a-zA-Z0-9\-_]+$'
    return bool(re.match(pattern, ticket_id)) and len(ticket_id) <= 50


def validate_portal_name(portal: str) -> bool:
    """Validate portal name."""
    valid_portals = {'dhiraagu', 'ooredoo', 'rol', 'medianet'}
    return portal.lower() in valid_portals


def rate_limit(requests_per_minute: int = 30):
    """
    Decorator for rate limiting specific endpoints.

    Usage:
        @rate_limit(requests_per_minute=10)
        async def my_endpoint():
            ...
    """
    limiter = RateLimiter(requests_per_minute=requests_per_minute)

    def decorator(func: Callable):
        @wraps(func)
        async def wrapper(request: Request, *args, **kwargs):
            client_id = request.client.host if request.client else "unknown"

            is_limited, retry_after = limiter.is_rate_limited(client_id)
            if is_limited:
                raise HTTPException(
                    status_code=429,
                    detail=f"Rate limit exceeded. Retry after {retry_after} seconds."
                )

            limiter.record_request(client_id)
            return await func(request, *args, **kwargs)

        return wrapper
    return decorator
