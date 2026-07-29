import os
import sqlite3
from dotenv import load_dotenv

load_dotenv()

# Application version - update this when deploying changes to bust cache
APP_VERSION = "1.10.1"


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

    # Separate, deliberately-distinct credential set for *writing* tickets to
    # Znuny (Admin > Config > "Znuny Ticket Creation"). Kept apart from the
    # read-only ZNUNY_URL/USERNAME/PASSWORD above (used for the sync pipeline)
    # so a write-capable agent account is never assumed by default -- creating
    # tickets is a much more consequential action than reading them.
    @classmethod
    def get_znuny_create_url(cls) -> str:
        return _get("ZNUNY_CREATE_URL")

    @classmethod
    def get_znuny_create_username(cls) -> str:
        return _get("ZNUNY_CREATE_USERNAME")

    @classmethod
    def get_znuny_create_password(cls) -> str:
        return _get("ZNUNY_CREATE_PASSWORD")

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

    @classmethod
    def get_nocbot_url(cls) -> str:
        """Base URL of the NocBot external API (see API_GUIDE.md), used for the
        ONT-exists-in-SMX check. DB (cfg_NOCBOT_URL) takes precedence over the
        .env fallback."""
        return _get("NOCBOT_URL", "http://10.241.1.107:5000")

    @classmethod
    def get_nocbot_api_key(cls) -> str:
        return _get("NOCBOT_API_KEY", "")

    # Portal URLs for template links
    @classmethod
    def get_portal_urls(cls) -> dict:
        return {
            "dhiraagu": _get("DHIRAAGU_URL"),
            "ooredoo": _get("OOREDOO_URL"),
            "rol": _get("ROL_URL"),
            "medianet": _get("MEDIANET_URL"),
        }
