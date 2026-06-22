"""ISP Ticket Formatter — entry point (web app launcher).

Auto-installs the dependencies in ``requirements.txt`` (if missing), then starts
the Flask web app. Paste an unformatted dashboard dump in the browser; it
auto-detects the ISP, formats a clean standardized ticket block, and lets you
copy it (rich text, bold heading) to the clipboard.

Architecture (MVC; the web app is one of two front ends over the same model):
    model/      domain formatters + TicketModel  (no UI)
    services/   text_to_html (rich clipboard fragment)
    webapp.py   Flask routes + HTML/JS front end
    view/       tkinter desktop front end (legacy; run `python view_app.py`)

Run:  python main.py   ->   http://localhost:8087
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys
from pathlib import Path

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

_ROOT = Path(__file__).resolve().parent
_REQUIREMENTS = _ROOT / "requirements.txt"


def _port() -> int:
    """Resolve the dev-server port from .env / environment (defaults to 8087).

    Reads PORT first, then HOST_PORT (the same var the Docker/Portainer stack
    uses), so local and container runs land on the same port by default.
    """
    return int(os.environ.get("PORT") or os.environ.get("HOST_PORT") or "8087")


def _ensure_requirements() -> None:
    """Install requirements.txt if Flask isn't importable yet.

    Skipped when running in a container (the image already has the deps) by
    setting ``SKIP_PIP_INSTALL=1``.
    """
    if os.environ.get("SKIP_PIP_INSTALL"):
        return
    try:
        import flask  # noqa: F401
        return
    except ImportError:
        pass

    if not _REQUIREMENTS.exists():
        logger.error("requirements.txt not found; cannot auto-install dependencies.")
        sys.exit(1)

    logger.info("Installing dependencies from requirements.txt …")
    subprocess.check_call(
        [sys.executable, "-m", "pip", "install", "-r", str(_REQUIREMENTS)]
    )


def main() -> None:
    _ensure_requirements()

    # Load .env now that python-dotenv is guaranteed installed.
    from dotenv import load_dotenv
    load_dotenv(_ROOT / ".env")

    # Imported only after deps are guaranteed present.
    from webapp import app

    port = _port()
    logger.info("ISP Ticket Formatter running at http://localhost:%d", port)
    app.run(host="0.0.0.0", port=port)


if __name__ == "__main__":
    main()