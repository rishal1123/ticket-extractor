"""
Customer Report Service - generates the UD-/DH- customer performance HTML report.

Wraps the standalone analyzer in `customer_analysis/analyze.py` so the main app
can produce the report on a daily schedule or on demand. Generation runs in a
background thread (it fetches ~1000 tickets and takes 1-2 minutes), so callers
trigger it and poll status rather than blocking.

The rendered HTML is written to data/customer_performance_report.html and served
by the reports page (/reports/customer-performance).
"""

import json
import os
import threading
from datetime import datetime, timezone, timedelta

from database import Database
from utils.logger import get_logger
from customer_analysis.analyze import generate_report, normalize_prefixes

MVT = timezone(timedelta(hours=5))
logger = get_logger("customer_report")

_DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data")
REPORT_PATH = os.path.join(_DATA_DIR, "customer_performance_report.html")
DATA_PATH = os.path.join(_DATA_DIR, "customer_report_data.json")
DEFAULT_PREFIXES = ["UD-", "DH-"]
# DB app_settings keys.
PREFIX_SETTING_KEY = "customer_report_prefixes"        # last-used generation prefixes
PRESETS_SETTING_KEY = "customer_report_presets"        # saved report presets (JSON list)


class CustomerReportService:
    """Generate and serve the customer performance report (class-level state)."""

    _lock = threading.Lock()
    _generating = False
    _last_error: str | None = None
    _last_result: dict | None = None

    # Expose the output path as a class attribute so callers (e.g. the scheduler)
    # can reference it without importing the module-level constant.
    REPORT_PATH = REPORT_PATH

    @classmethod
    def is_generating(cls) -> bool:
        return cls._generating

    @classmethod
    def report_exists(cls) -> bool:
        # Require both the standalone HTML and the JSON dataset (the native UI
        # needs the JSON), so a restart self-heals if either is missing.
        return os.path.exists(REPORT_PATH) and os.path.exists(DATA_PATH)

    @classmethod
    def get_prefixes(cls) -> list[str]:
        """Return the configured generation prefixes (DB setting, else default)."""
        try:
            raw = Database().get_setting(PREFIX_SETTING_KEY)
        except Exception:
            raw = None
        prefixes = normalize_prefixes((raw or "").split(",")) if raw else []
        return prefixes or list(DEFAULT_PREFIXES)

    @classmethod
    def set_prefixes(cls, prefixes: list[str]) -> list[str]:
        """Persist the generation prefixes (normalized). Returns the stored list."""
        prefixes = normalize_prefixes(prefixes) or list(DEFAULT_PREFIXES)
        try:
            Database().set_setting(PREFIX_SETTING_KEY, ",".join(prefixes),
                                   "Prefixes used for the customer performance report")
        except Exception as e:
            logger.error(f"Failed to persist customer report prefixes: {e}")
        return prefixes

    @classmethod
    def start_generation(cls, prefixes: list[str] | None = None) -> bool:
        """Start a background generation. Returns False if one is already running.

        If `prefixes` is given it is persisted and used; otherwise the stored
        configured prefixes are used (default UD-/DH-). This lets the daily job
        and on-demand runs share whatever the user last set.
        """
        if prefixes:
            prefixes = cls.set_prefixes(prefixes)
        else:
            prefixes = cls.get_prefixes()
        with cls._lock:
            if cls._generating:
                return False
            cls._generating = True
        threading.Thread(
            target=cls._run,
            args=(prefixes,),
            daemon=True,
            name="CustomerReport",
        ).start()
        return True

    @classmethod
    def _run(cls, prefixes: list[str]):
        logger.info(f"Customer report generation started (prefixes={prefixes})")
        try:
            os.makedirs(_DATA_DIR, exist_ok=True)
            # close=False: keep the shared Znuny HTTP session alive for the sync worker.
            cls._last_result = generate_report(
                REPORT_PATH, prefixes, log=logger.info, close=False, data_path=DATA_PATH)
            cls._last_error = None
            logger.info(f"Customer report generated: {cls._last_result}")
        except Exception as e:
            cls._last_error = str(e)
            logger.error(f"Customer report generation failed: {e}", exc_info=True)
        finally:
            cls._generating = False

    # ---- Report data (for the in-app native renderer) ----
    @classmethod
    def get_data(cls) -> dict | None:
        """Return the generated report data ({generated_at, prefixes, records}) or None."""
        if not os.path.exists(DATA_PATH):
            return None
        try:
            with open(DATA_PATH, encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            logger.error(f"Failed to read customer report data: {e}")
            return None

    # ---- Saved presets ----
    @classmethod
    def get_presets(cls) -> list[dict]:
        """Return the saved report presets (list of {name, prefixes, date_from, date_to})."""
        try:
            raw = Database().get_setting(PRESETS_SETTING_KEY)
            presets = json.loads(raw) if raw else []
            return presets if isinstance(presets, list) else []
        except Exception:
            return []

    @classmethod
    def save_preset(cls, name: str, prefixes, date_from: str = "", date_to: str = "") -> list[dict]:
        """Create or update a preset by name. Returns the full updated list."""
        name = (name or "").strip()
        if not name:
            raise ValueError("Preset name is required")
        prefixes = normalize_prefixes(prefixes if isinstance(prefixes, list) else str(prefixes).split(","))
        if not prefixes:
            raise ValueError("At least one prefix is required")
        preset = {"name": name, "prefixes": prefixes,
                  "date_from": date_from or "", "date_to": date_to or ""}
        presets = [p for p in cls.get_presets() if p.get("name") != name]
        presets.append(preset)
        presets.sort(key=lambda p: p.get("name", "").lower())
        Database().set_setting(PRESETS_SETTING_KEY, json.dumps(presets),
                               "Saved customer performance report presets")
        return presets

    @classmethod
    def delete_preset(cls, name: str) -> list[dict]:
        """Delete a preset by name. Returns the full updated list."""
        presets = [p for p in cls.get_presets() if p.get("name") != name]
        Database().set_setting(PRESETS_SETTING_KEY, json.dumps(presets),
                               "Saved customer performance report presets")
        return presets

    @classmethod
    def status(cls) -> dict:
        exists = os.path.exists(REPORT_PATH)
        generated_at = None
        if exists:
            generated_at = datetime.fromtimestamp(os.path.getmtime(REPORT_PATH), MVT).isoformat()
        return {
            "exists": exists,
            "generated_at": generated_at,
            "generating": cls._generating,
            "prefixes": cls.get_prefixes(),
            "last_error": cls._last_error,
            "last_result": cls._last_result,
        }

    @classmethod
    def get_html(cls) -> str | None:
        if os.path.exists(REPORT_PATH):
            with open(REPORT_PATH, encoding="utf-8") as f:
                return f.read()
        return None
