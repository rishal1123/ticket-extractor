"""
Extraction Service - Business logic for portal ticket extraction.
"""

from typing import Dict, List, Optional, Type
from config import Config, PortalConfig
from database import Database
from extractors import DhiraaguExtractor, OoredooExtractor, ROLExtractor, MedianetExtractor
from extractors.base import BaseExtractor
from utils.logger import get_logger

logger = get_logger("extraction_service")


class ExtractionService:
    """Service class for managing ticket extraction from portals."""

    PORTAL_EXTRACTORS: Dict[str, Type[BaseExtractor]] = {
        "dhiraagu": DhiraaguExtractor,
        "ooredoo": OoredooExtractor,
        "rol": ROLExtractor,
        "medianet": MedianetExtractor
    }

    def __init__(self, db: Database = None):
        """Initialize service with optional database instance."""
        self.db = db or Database()

    def get_extractor_class(self, portal_name: str) -> Optional[Type[BaseExtractor]]:
        """Get the extractor class for a portal name."""
        return self.PORTAL_EXTRACTORS.get(portal_name.lower())

    def get_configured_portals(self) -> List[str]:
        """Get list of configured portal names."""
        configured = []
        for portal_config in Config.get_all_portals():
            if portal_config.url and portal_config.username:
                configured.append(portal_config.name)
        return configured

    def extract_from_portal(self, portal_name: str, headless: bool = True) -> Dict:
        """
        Extract tickets from a single portal.

        Returns:
            Dict with keys: status, portal, tickets_found, tickets_new,
                           tickets_updated, tickets_completed, error
        """
        # Get portal configuration
        config = Config.get_portal_by_name(portal_name)
        if not config:
            return {
                "status": "failed",
                "portal": portal_name,
                "tickets_found": 0,
                "tickets_new": 0,
                "tickets_updated": 0,
                "tickets_completed": 0,
                "error": f"Unknown portal: {portal_name}"
            }

        if not config.url or not config.username:
            return {
                "status": "failed",
                "portal": portal_name,
                "tickets_found": 0,
                "tickets_new": 0,
                "tickets_updated": 0,
                "tickets_completed": 0,
                "error": f"Portal {portal_name} not configured"
            }

        extractor_class = self.get_extractor_class(portal_name)
        if not extractor_class:
            return {
                "status": "failed",
                "portal": portal_name,
                "tickets_found": 0,
                "tickets_new": 0,
                "tickets_updated": 0,
                "tickets_completed": 0,
                "error": f"No extractor for portal: {portal_name}"
            }

        try:
            # Create extractor with config and db (as per BaseExtractor interface)
            extractor = extractor_class(config, self.db, headless=headless)
            # run() handles login, extraction, saving to db, and logging
            result = extractor.run()
            return result

        except Exception as e:
            logger.error(f"Extraction failed for {portal_name}: {e}")
            self.db.log_extraction(portal_name, "failed", error_message=str(e))
            return {
                "status": "failed",
                "portal": portal_name,
                "tickets_found": 0,
                "tickets_new": 0,
                "tickets_updated": 0,
                "tickets_completed": 0,
                "error": str(e)
            }

    def extract_from_all_portals(self, headless: bool = True) -> Dict:
        """
        Extract tickets from all configured portals.

        Returns:
            Dict with overall stats and per-portal results
        """
        results = {
            "total_found": 0,
            "total_new": 0,
            "total_updated": 0,
            "total_completed": 0,
            "portals": {}
        }

        for portal in self.get_configured_portals():
            logger.info(f"Extracting from {portal}")
            portal_result = self.extract_from_portal(portal, headless)
            results["portals"][portal] = portal_result
            results["total_found"] += portal_result.get("tickets_found", 0)
            results["total_new"] += portal_result.get("tickets_new", 0)
            results["total_updated"] += portal_result.get("tickets_updated", 0)
            results["total_completed"] += portal_result.get("tickets_completed", 0)

        logger.info(f"Portal extraction complete: {results['total_found']} found, {results['total_new']} new")
        return results

    def get_extraction_logs(self, limit: int = 100) -> List[Dict]:
        """Get extraction log history."""
        return self.db.get_extraction_logs(limit=limit)
