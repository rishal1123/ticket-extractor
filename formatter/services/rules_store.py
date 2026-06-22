"""Persisted, admin-editable validation rules.

Maps each ISP to the checks applied when formatting its tickets:

    {
      "Ooredoo": {
        "atoll": ["Kaafu"],                       # allowed Atoll value(s)
        "zones": {                                # street -> allowed address IDs
          "Phase 1": ["TGR*", "R1B*", "R2B*"],
          "Phase 2": ["UD-*"]
        }
      }
    }

Patterns are glob style (``*`` wildcard, case-insensitive). Stored as JSON so
the /admin page can edit them at runtime; falls back to built-in defaults when
the file doesn't exist yet. The older flat ``{isp: {zone: [...]}}`` shape is
migrated automatically on load/save.
"""

from __future__ import annotations

import json
import logging
import os
import threading
from pathlib import Path

logger = logging.getLogger(__name__)

_ROOT = Path(__file__).resolve().parent.parent
_RULES_PATH = Path(
    os.environ.get("RULES_PATH", str(_ROOT / "config" / "address_rules.json"))
)
_lock = threading.Lock()

# Seeded defaults (used until an admin saves the first time).
DEFAULT_RULES: dict = {
    "Ooredoo": {
        "atoll": ["Kaafu"],
        "zones": {
            "Phase 1": ["TGR*", "R1B*", "R2B*"],
            "Phase 2": ["UD-*"],
        },
    }
}


def load() -> dict:
    """Return the current rules (read fresh each call so admin edits take effect)."""
    try:
        with open(_RULES_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            return sanitize(data)  # normalize shape (migrates old flat rules)
        logger.warning("Rules file %s is not a JSON object; using defaults", _RULES_PATH)
    except FileNotFoundError:
        logger.debug("Rules file %s not found; using defaults", _RULES_PATH)
    except (json.JSONDecodeError, OSError) as exc:
        logger.warning("Could not read rules file %s (%s); using defaults", _RULES_PATH, exc)
    return sanitize(DEFAULT_RULES)


def save(rules: dict) -> None:
    """Atomically persist ``rules`` to the JSON file (creating the dir if needed)."""
    _RULES_PATH.parent.mkdir(parents=True, exist_ok=True)
    with _lock:
        tmp = _RULES_PATH.with_suffix(".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(rules, f, indent=2, ensure_ascii=False)
        os.replace(tmp, _RULES_PATH)
    logger.info("Saved validation rules to %s", _RULES_PATH)


def _clean_list(values: object) -> list[str]:
    if isinstance(values, str):
        values = [values]
    if not isinstance(values, (list, tuple)):
        return []
    return [str(v).strip() for v in values if str(v).strip()]


def _split_entry(entry: object) -> tuple[list, dict]:
    """Return ``(atoll, zones)`` from an ISP entry, migrating the old flat shape.

    New shape: ``{"atoll": [...], "zones": {zone: [...]}}``.
    Old (flat) shape: ``{zone: [...]}`` -> treated entirely as zones.
    """
    if not isinstance(entry, dict):
        return [], {}
    if "zones" in entry or "atoll" in entry:
        zones = entry.get("zones") if isinstance(entry.get("zones"), dict) else {}
        atoll = entry.get("atoll", [])
        return _clean_list(atoll), zones
    return [], entry  # old flat: all keys are zones


def sanitize(raw: object) -> dict:
    """Coerce arbitrary input into the ``{isp: {atoll: [...], zones: {...}}}`` shape.

    Drops empty ISP/zone names and blank patterns so the admin page can't
    persist malformed rules, and migrates the old flat shape.
    """
    clean: dict = {}
    if not isinstance(raw, dict):
        return clean
    for isp, entry in raw.items():
        if not isinstance(isp, str) or not isp.strip():
            continue
        atoll, zones = _split_entry(entry)
        atoll_clean = _clean_list(atoll)
        zone_map: dict[str, list[str]] = {}
        if isinstance(zones, dict):
            for zone, patterns in zones.items():
                if not isinstance(zone, str) or not zone.strip():
                    continue
                pats = _clean_list(patterns)
                if pats:
                    zone_map[zone.strip()] = pats
        if atoll_clean or zone_map:
            clean[isp.strip()] = {"atoll": atoll_clean, "zones": zone_map}
    return clean