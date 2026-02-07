"""Custom exception classes for the Ticket Extractor application."""


class TicketExtractorError(Exception):
    """Base exception for all Ticket Extractor errors."""
    pass


class DatabaseError(TicketExtractorError):
    """Raised when a database operation fails."""

    def __init__(self, message: str, operation: str = None, original_error: Exception = None):
        self.operation = operation
        self.original_error = original_error
        super().__init__(message)


class ExtractionError(TicketExtractorError):
    """Raised when ticket extraction fails."""

    def __init__(self, message: str, portal: str = None, original_error: Exception = None):
        self.portal = portal
        self.original_error = original_error
        super().__init__(message)


class LoginError(ExtractionError):
    """Raised when portal login fails."""
    pass


class SessionExpiredError(ExtractionError):
    """Raised when a portal session has expired."""
    pass


class ZnunyError(TicketExtractorError):
    """Raised when Znuny operations fail."""

    def __init__(self, message: str, ticket_id: str = None, original_error: Exception = None):
        self.ticket_id = ticket_id
        self.original_error = original_error
        super().__init__(message)


class ZnunyConnectionError(ZnunyError):
    """Raised when connection to Znuny fails."""
    pass


class ZnunyTicketNotFoundError(ZnunyError):
    """Raised when a ticket is not found in Znuny."""
    pass


class ConfigurationError(TicketExtractorError):
    """Raised when configuration is invalid or missing."""

    def __init__(self, message: str, config_key: str = None):
        self.config_key = config_key
        super().__init__(message)


class ValidationError(TicketExtractorError):
    """Raised when data validation fails."""

    def __init__(self, message: str, field: str = None, value: any = None):
        self.field = field
        self.value = value
        super().__init__(message)
