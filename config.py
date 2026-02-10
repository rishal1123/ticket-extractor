import os
from dotenv import load_dotenv

# Load .env from ENV_FILE_PATH if set (Docker), otherwise default .env
_env_path = os.getenv("ENV_FILE_PATH")
if _env_path:
    load_dotenv(_env_path)
else:
    load_dotenv()

# Application version - update this when deploying changes to bust cache
APP_VERSION = "1.0.8"


class PortalConfig:
    def __init__(self, name: str, url: str, username: str, password: str):
        self.name = name
        self.url = url
        self.username = username
        self.password = password


class Config:
    # Portal configurations
    DHIRAAGU = PortalConfig(
        name="dhiraagu",
        url=os.getenv("DHIRAAGU_URL", ""),
        username=os.getenv("DHIRAAGU_USERNAME", ""),
        password=os.getenv("DHIRAAGU_PASSWORD", "")
    )

    OOREDOO = PortalConfig(
        name="ooredoo",
        url=os.getenv("OOREDOO_URL", ""),
        username=os.getenv("OOREDOO_USERNAME", ""),
        password=os.getenv("OOREDOO_PASSWORD", "")
    )

    ROL = PortalConfig(
        name="rol",
        url=os.getenv("ROL_URL", ""),
        username=os.getenv("ROL_USERNAME", ""),
        password=os.getenv("ROL_PASSWORD", "")
    )

    MEDIANET = PortalConfig(
        name="medianet",
        url=os.getenv("MEDIANET_URL", ""),
        username=os.getenv("MEDIANET_USERNAME", ""),
        password=os.getenv("MEDIANET_PASSWORD", "")
    )

    # Znuny API configuration
    ZNUNY_URL = os.getenv("ZNUNY_URL", "")
    ZNUNY_USERNAME = os.getenv("ZNUNY_USERNAME", "")
    ZNUNY_PASSWORD = os.getenv("ZNUNY_PASSWORD", "")

    # Scheduler settings
    EXTRACTION_INTERVAL_MINUTES = int(os.getenv("EXTRACTION_INTERVAL_MINUTES", "5"))
    ZNUNY_SYNC_INTERVAL_MINUTES = int(os.getenv("ZNUNY_SYNC_INTERVAL_MINUTES", "3"))

    # Dashboard settings
    DASHBOARD_HOST = os.getenv("DASHBOARD_HOST", "0.0.0.0")
    DASHBOARD_PORT = int(os.getenv("DASHBOARD_PORT", "8000"))

    # Database (can be overridden via DATABASE_PATH env var for Docker)
    DATABASE_PATH = os.getenv("DATABASE_PATH", os.path.join(os.path.dirname(__file__), "tickets.db"))

    @classmethod
    def get_all_portals(cls) -> list[PortalConfig]:
        return [cls.DHIRAAGU, cls.OOREDOO, cls.ROL, cls.MEDIANET]

    @classmethod
    def get_portal_by_name(cls, name: str) -> PortalConfig | None:
        portals = {
            "dhiraagu": cls.DHIRAAGU,
            "ooredoo": cls.OOREDOO,
            "rol": cls.ROL,
            "medianet": cls.MEDIANET
        }
        return portals.get(name.lower())
