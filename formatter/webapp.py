"""ISP Ticket Formatter — web app (Flask front end over the MVC model).

This is an alternative "view" to the tkinter desktop app (``main.py``). It reuses
the exact same domain layer (``model/``) — detection + formatting are unchanged —
and exposes them over HTTP so the formatter can run as a container (e.g. deployed
through Portainer) and be used from any browser.

Endpoints:
    GET  /             -> the single-page UI
    POST /api/format   -> {raw, manual} -> {isp, text, html, manual_fields, ...}
    GET  /admin        -> password-gated editor for address-validation rules
    POST /admin/login  -> set the admin session
    GET  /admin/logout -> clear the admin session
    GET  /api/rules    -> current rules (admin only)
    POST /api/rules    -> save rules (admin only)
    GET  /healthz      -> liveness probe for the container

Run locally:  python webapp.py
In container:  gunicorn -b 0.0.0.0:8000 webapp:app
"""

from __future__ import annotations

import logging
import os
from functools import wraps

from flask import (
    Flask, jsonify, redirect, render_template, request, session, url_for,
)

from model import TicketModel
from services import rules_store
from services.clipboard import text_to_html
from services.znuny import ZnunyService

# Load .env so gunicorn-served runs (Docker/Portainer) pick up config too.
try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:  # pragma: no cover - dotenv optional at runtime
    pass

# Configure logging once for the whole app (works under gunicorn too, since this
# module is the import target). Level via LOG_LEVEL (default INFO).
logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

app = Flask(__name__)
# Don't let browsers cache JS/CSS — so UI changes always take effect on reload.
app.config["SEND_FILE_MAX_AGE_DEFAULT"] = 0
# Session signing key; set SECRET_KEY in .env to keep admin logins across restarts.
app.secret_key = os.environ.get("SECRET_KEY") or os.urandom(32)

ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "admin")

_model = TicketModel()
_znuny = ZnunyService()


# --------------------------------------------------------------------------- #
# Formatter UI + API
# --------------------------------------------------------------------------- #
@app.get("/")
def index():
    return render_template("index.html")


@app.post("/api/format")
def api_format():
    """Format a pasted dump. Mirrors the controller's handle_change logic."""
    data = request.get_json(silent=True) or {}
    raw = (data.get("raw") or "").strip()
    manual = data.get("manual") or {}
    # Coerce manual values to strings; the model expects ``str.strip()``-able values.
    manual = {str(k): ("" if v is None else str(v)) for k, v in manual.items()}

    result = _model.build(
        raw, manual,
        address_rules=rules_store.load(),
        znuny=_znuny if _znuny.enabled else None,
    )

    if not raw:
        status, level = "Waiting for paste…", "info"
    elif not result.detected:
        logger.info("Format request: ISP not detected (%d chars)", len(raw))
        status, level = "Could not detect ISP.", "error"
    elif result.missing_keys:
        labels = ", ".join(result.missing_labels())
        status, level = f"Detected: {result.isp} • enter {labels} above ⤴", "warn"
    elif result.warnings:
        logger.warning("Validation warning for %s: %s", result.isp, result.warnings[0])
        status, level = f"Detected: {result.isp} • {result.warnings[0]}", "warn"
    else:
        status, level = f"Detected: {result.isp} • ready to copy ✓", "ok"

    return jsonify(
        isp=result.isp,
        text=result.text,
        # Rich HTML (bold heading) so the browser can put it on the clipboard,
        # exactly like the desktop ClipboardService does.
        html=text_to_html(result.text) if result.complete or result.detected else "",
        manual_fields=[list(f) for f in result.manual_fields],
        missing_keys=list(result.missing_keys),
        warnings=list(result.warnings),
        flagged=list(result.flagged),
        complete=result.complete,
        status=status,
        level=level,
    )


# --------------------------------------------------------------------------- #
# Admin: address-validation rules
# --------------------------------------------------------------------------- #
def _require_admin(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        if not session.get("admin"):
            logger.warning("Unauthorized admin access to %s from %s",
                           request.path, request.remote_addr)
            return jsonify(error="unauthorized"), 401
        return fn(*args, **kwargs)

    return wrapper


@app.get("/admin")
def admin():
    return render_template("admin.html", authed=bool(session.get("admin")))


@app.post("/admin/login")
def admin_login():
    if request.form.get("password", "") == ADMIN_PASSWORD:
        session["admin"] = True
        logger.info("Admin login succeeded from %s", request.remote_addr)
        return redirect(url_for("admin"))
    logger.warning("Admin login failed from %s", request.remote_addr)
    return render_template("admin.html", authed=False, error="Incorrect password."), 401


@app.get("/admin/logout")
def admin_logout():
    session.pop("admin", None)
    logger.info("Admin logout from %s", request.remote_addr)
    return redirect(url_for("admin"))


@app.get("/api/rules")
@_require_admin
def get_rules():
    return jsonify(rules_store.load())


@app.post("/api/rules")
@_require_admin
def save_rules():
    rules = rules_store.sanitize(request.get_json(silent=True))
    rules_store.save(rules)
    logger.info("Address rules saved from %s (%d ISP(s))", request.remote_addr, len(rules))
    return jsonify(ok=True, rules=rules)


@app.get("/healthz")
def healthz():
    return jsonify(status="ok")


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8000, debug=True)