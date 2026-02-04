"""
Config Service - Business logic for configuration management.
"""

import os
from typing import Dict
from utils.logger import get_logger

logger = get_logger("config_service")


class ConfigService:
    """Service class for configuration management."""

    # Configuration sections for organized .env file
    CONFIG_SECTIONS = {
        'Dhiraagu Portal': ['DHIRAAGU_URL', 'DHIRAAGU_USERNAME', 'DHIRAAGU_PASSWORD'],
        'Ooredoo Portal': ['OOREDOO_URL', 'OOREDOO_USERNAME', 'OOREDOO_PASSWORD'],
        'ROL Portal': ['ROL_URL', 'ROL_USERNAME', 'ROL_PASSWORD'],
        'Medianet Portal': ['MEDIANET_URL', 'MEDIANET_USERNAME', 'MEDIANET_PASSWORD'],
        'Znuny API': ['ZNUNY_URL', 'ZNUNY_USERNAME', 'ZNUNY_PASSWORD'],
        'Scheduler settings': ['EXTRACTION_INTERVAL_MINUTES'],
        'Dashboard settings': ['DASHBOARD_HOST', 'DASHBOARD_PORT']
    }

    def __init__(self, env_path: str = None):
        """Initialize service with optional custom .env path."""
        if env_path:
            self.env_path = env_path
        else:
            self.env_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), ".env")

    def get_config(self, mask_passwords: bool = True) -> Dict[str, str]:
        """
        Read configuration from .env file.

        Args:
            mask_passwords: If True, replace password values with ********

        Returns:
            Dict of configuration key-value pairs
        """
        config = {}

        if os.path.exists(self.env_path):
            with open(self.env_path, 'r') as f:
                for line in f:
                    line = line.strip()
                    if line and not line.startswith('#') and '=' in line:
                        key, value = line.split('=', 1)
                        if mask_passwords and 'PASSWORD' in key.upper():
                            config[key] = '********' if value else ''
                        else:
                            config[key] = value

        return config

    def update_config(self, new_config: Dict[str, str]) -> Dict:
        """
        Update configuration in .env file.

        Args:
            new_config: Dict of configuration values to update

        Returns:
            Dict with success status and message
        """
        try:
            # Read existing config to preserve passwords if masked
            existing_config = self.get_config(mask_passwords=False)

            # Merge configs - don't overwrite passwords with masked values
            final_config = {}
            for key, value in new_config.items():
                if value == '********' and key in existing_config:
                    final_config[key] = existing_config[key]
                else:
                    final_config[key] = value

            # Write config with sections
            with open(self.env_path, 'w') as f:
                for section_name, keys in self.CONFIG_SECTIONS.items():
                    f.write(f"# {section_name}\n")
                    for key in keys:
                        if key in final_config:
                            f.write(f"{key}={final_config[key]}\n")
                    f.write("\n")

            logger.info("Configuration updated successfully")
            return {"success": True, "message": "Configuration saved"}

        except Exception as e:
            logger.error(f"Error updating config: {e}")
            return {"success": False, "message": str(e)}

    def validate_config(self) -> Dict:
        """
        Validate the current configuration.

        Returns:
            Dict with valid status and list of missing/invalid fields
        """
        config = self.get_config(mask_passwords=False)
        issues = []

        # Check required fields for each portal
        required_fields = [
            ('DHIRAAGU_URL', 'DHIRAAGU_USERNAME', 'DHIRAAGU_PASSWORD'),
            ('OOREDOO_URL', 'OOREDOO_USERNAME', 'OOREDOO_PASSWORD'),
            ('ROL_URL', 'ROL_USERNAME', 'ROL_PASSWORD'),
            ('MEDIANET_URL', 'MEDIANET_USERNAME', 'MEDIANET_PASSWORD'),
            ('ZNUNY_URL', 'ZNUNY_USERNAME', 'ZNUNY_PASSWORD'),
        ]

        for portal_fields in required_fields:
            portal_name = portal_fields[0].split('_')[0]
            missing = [f for f in portal_fields if not config.get(f)]
            if missing and any(config.get(f) for f in portal_fields):
                issues.append(f"{portal_name}: Missing {', '.join(missing)}")

        # Check numeric fields
        try:
            interval = int(config.get('EXTRACTION_INTERVAL_MINUTES', 5))
            if interval < 1 or interval > 60:
                issues.append("EXTRACTION_INTERVAL_MINUTES must be between 1 and 60")
        except ValueError:
            issues.append("EXTRACTION_INTERVAL_MINUTES must be a number")

        try:
            port = int(config.get('DASHBOARD_PORT', 8000))
            if port < 1 or port > 65535:
                issues.append("DASHBOARD_PORT must be between 1 and 65535")
        except ValueError:
            issues.append("DASHBOARD_PORT must be a number")

        return {
            "valid": len(issues) == 0,
            "issues": issues
        }

    def get_all_keys(self) -> list:
        """Get list of all configuration keys."""
        keys = []
        for section_keys in self.CONFIG_SECTIONS.values():
            keys.extend(section_keys)
        return keys
