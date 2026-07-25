"""
Config Service - Business logic for configuration management.
Reads/writes portal credentials from the database (app_settings table).
"""

from typing import Dict
from database import Database
from utils.logger import get_logger

logger = get_logger("config_service")


class ConfigService:
    """Service class for configuration management using database storage."""

    # Configuration sections for UI organization
    CONFIG_SECTIONS = {
        'Dhiraagu Portal': ['DHIRAAGU_URL', 'DHIRAAGU_USERNAME', 'DHIRAAGU_PASSWORD'],
        'Ooredoo Portal': ['OOREDOO_URL', 'OOREDOO_USERNAME', 'OOREDOO_PASSWORD'],
        'ROL Portal': ['ROL_URL', 'ROL_USERNAME', 'ROL_PASSWORD'],
        'Medianet Portal': ['MEDIANET_URL', 'MEDIANET_USERNAME', 'MEDIANET_PASSWORD'],
        'Znuny API': ['ZNUNY_URL', 'ZNUNY_USERNAME', 'ZNUNY_PASSWORD'],
        'Znuny Ticket Creation (write)': ['ZNUNY_CREATE_URL', 'ZNUNY_CREATE_USERNAME', 'ZNUNY_CREATE_PASSWORD'],
        'Scheduler settings': ['EXTRACTION_INTERVAL_MINUTES', 'ZNUNY_SYNC_INTERVAL_MINUTES'],
        'Dashboard settings': ['DASHBOARD_HOST', 'DASHBOARD_PORT'],
        'FlareSolverr': ['FLARESOLVERR_URL'],
        'NocBot API (ONT lookup)': ['NOCBOT_URL', 'NOCBOT_API_KEY']
    }

    def __init__(self):
        """Initialize service."""
        self.db = Database()

    def get_config(self, mask_passwords: bool = True) -> Dict[str, str]:
        """
        Read configuration from database.

        Args:
            mask_passwords: If True, replace password values with ********

        Returns:
            Dict of configuration key-value pairs
        """
        config = self.db.get_config_settings()

        if mask_passwords:
            for key in config:
                upper = key.upper()
                if 'PASSWORD' in upper or 'API_KEY' in upper or 'SECRET' in upper:
                    config[key] = '********' if config[key] else ''

        return config

    def update_config(self, new_config: Dict[str, str]) -> Dict:
        """
        Update configuration in database.

        Args:
            new_config: Dict of configuration values to update

        Returns:
            Dict with success status and message
        """
        try:
            # Read existing config to preserve passwords if masked
            existing_config = self.db.get_config_settings()

            # Merge configs - don't overwrite passwords with masked values
            final_config = {}
            for key, value in new_config.items():
                if value == '********' and key in existing_config:
                    final_config[key] = existing_config[key]
                else:
                    final_config[key] = value

            self.db.set_config_settings(final_config)

            logger.info("Configuration updated successfully")
            return {"success": True, "message": "Configuration saved to database"}

        except Exception as e:
            logger.error(f"Error updating config: {e}")
            return {"success": False, "message": str(e)}

    def get_all_keys(self) -> list:
        """Get list of all configuration keys."""
        keys = []
        for section_keys in self.CONFIG_SECTIONS.values():
            keys.extend(section_keys)
        return keys
