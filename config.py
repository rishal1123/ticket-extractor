import os
import sqlite3
from dotenv import load_dotenv

load_dotenv()

# Application version - update this when deploying changes to bust cache
APP_VERSION = "1.7.1"


def _load_db_config() -> dict:
    """Load config from app_settings DB table using raw sqlite3 (avoids circular imports)."""
    db_path = os.getenv("DATABASE_PATH", os.path.join(os.path.dirname(__file__), "tickets.db"))
    config = {}
    try:
        if os.path.exists(db_path):
            conn = sqlite3.connect(db_path)
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute("SELECT key, value FROM app_settings WHERE key LIKE 'cfg_%'")
            for row in cursor.fetchall():
                key = row["key"].replace("cfg_", "", 1)
                config[key] = row["value"]
            conn.close()
    except Exception:
        pass
    return config


def _get(key: str, default: str = "") -> str:
    """Get config value: DB first, then .env, then default."""
    db_config = _load_db_config()
    if key in db_config and db_config[key]:
        return db_config[key]
    return os.getenv(key, default)


class PortalConfig:
    def __init__(self, name: str, url: str, username: str, password: str):
        self.name = name
        self.url = url
        self.username = username
        self.password = password


class Config:
    # Database path (env-only, needed before DB exists)
    DATABASE_PATH = os.getenv("DATABASE_PATH", os.path.join(os.path.dirname(__file__), "tickets.db"))

    # Dashboard settings (env-only, needed at startup before DB)
    DASHBOARD_HOST = os.getenv("DASHBOARD_HOST", "0.0.0.0")
    DASHBOARD_PORT = int(os.getenv("DASHBOARD_PORT", "8000"))

    @classmethod
    def _make_portal(cls, name: str) -> PortalConfig:
        prefix = name.upper()
        return PortalConfig(
            name=name,
            url=_get(f"{prefix}_URL"),
            username=_get(f"{prefix}_USERNAME"),
            password=_get(f"{prefix}_PASSWORD"),
        )

    @classmethod
    def get_all_portals(cls) -> list[PortalConfig]:
        return [cls._make_portal(n) for n in ("dhiraagu", "ooredoo", "rol", "medianet")]

    @classmethod
    def get_portal_by_name(cls, name: str) -> PortalConfig | None:
        if name.lower() in ("dhiraagu", "ooredoo", "rol", "medianet"):
            return cls._make_portal(name.lower())
        return None

    @classmethod
    def get_znuny_url(cls) -> str:
        return _get("ZNUNY_URL")

    @classmethod
    def get_znuny_username(cls) -> str:
        return _get("ZNUNY_USERNAME")

    @classmethod
    def get_znuny_password(cls) -> str:
        return _get("ZNUNY_PASSWORD")

    @classmethod
    def get_extraction_interval(cls) -> int:
        return int(_get("EXTRACTION_INTERVAL_MINUTES", "5"))

    @classmethod
    def get_znuny_sync_interval(cls) -> int:
        return int(_get("ZNUNY_SYNC_INTERVAL_MINUTES", "3"))

    @classmethod
    def get_flaresolverr_url(cls) -> str:
        """Return FlareSolverr base URL, or empty string if not configured.
        DB (cfg_FLARESOLVERR_URL) takes precedence over the .env fallback."""
        return _get("FLARESOLVERR_URL", "")

    # Portal URLs for template links
    @classmethod
    def get_portal_urls(cls) -> dict:
        return {
            "dhiraagu": _get("DHIRAAGU_URL"),
            "ooredoo": _get("OOREDOO_URL"),
            "rol": _get("ROL_URL"),
            "medianet": _get("MEDIANET_URL"),
        }
