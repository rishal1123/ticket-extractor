#!/bin/bash
set -e

echo "=== Ticket Extractor - Starting ==="
echo "Python $(python --version 2>&1 | cut -d' ' -f2) | PID $$"

# Auto-install/upgrade Python deps from requirements.txt on every start, so a
# Portainer redeploy that updates the code (volume/repo) also picks up new
# dependencies without rebuilding the image. Best-effort: if offline, fall back
# to the deps baked into the image. Fast when already satisfied.
if [ -f requirements.txt ]; then
    echo "Installing/updating dependencies from requirements.txt..."
    pip install --no-cache-dir -r requirements.txt || echo "WARN: pip install failed; using baked-in deps"
fi

# Database init + migration check BEFORE starting the app.
# Database() applies the schema/migrations on construction; running it here (and
# failing fast under `set -e`) guarantees the DB is fully migrated before the
# scheduler/workers come up in app.py.
echo "Checking database (init + migrations)..."
python -c "from database import Database; Database(); print('Database ready')"

# One-time migration: clear stale Dhiraagu raw_dumps captured before the
# Filament form-field capture fix. The order detail page is a FORM — its field
# values live in <input>/<select> controls, which page.inner_text("body") does
# NOT return (it yields only text nodes = the LABELS). So v2-era dumps were
# label-only ("Order number *\nService number *\n..."), and the formatter paired
# each label with the next label ("Customer Name: Contact number *"). The new
# DhiraaguExtractor.capture_raw_dump() reads each control's value (selects use
# their selected option text) and emits label+value lines. Re-capture only fills
# tickets whose raw_dump is empty, so the broken v2 dumps must be cleared. Bumped
# to v3 to force that clear. Guarded by an app_settings flag so a restart never
# wipes freshly re-captured dumps.
echo "Checking one-time Dhiraagu raw_dump migration..."
python - <<'PY'
from config import Config
import sqlite3
FLAG = "migration_clear_dhiraagu_dumps_v3"
conn = sqlite3.connect(Config.DATABASE_PATH)
try:
    cur = conn.cursor()
    cur.execute("SELECT value FROM app_settings WHERE key = ?", (FLAG,))
    if cur.fetchone():
        print("Dhiraagu raw_dump migration already applied; skipping")
    else:
        cur.execute("UPDATE tickets SET raw_dump = NULL WHERE portal = 'dhiraagu'")
        cleared = cur.rowcount
        cur.execute(
            "INSERT OR REPLACE INTO app_settings (key, value, description) VALUES (?, ?, ?)",
            (FLAG, "1", "Cleared Dhiraagu raw_dumps for the Filament form-field value capture fix (v3)"),
        )
        conn.commit()
        print(f"Cleared raw_dump on {cleared} Dhiraagu ticket(s) for re-capture")
finally:
    conn.close()
PY

# One-time migration: unlink stale Znuny data left on already-reopened tickets.
# Before this fix, a ticket reopen reset created_at to the new extraction time but
# left the PRIOR (pre-close) znuny_created_at/znuny_ticket_id/etc. in place, pairing
# a stale znuny_created_at with the new created_at — producing a negative "time to
# create" that the dashboard renders as "-". Reopen now clears these fields itself,
# so this is a one-time repair for tickets that were already stuck in that state
# from a PAST reopen (their znuny_created_at predates their last_reopened_at).
# Clearing the link makes the next Znuny sync cycle re-search and re-link them
# correctly. Guarded by an app_settings flag so a restart never re-clears fresh
# links established after this migration runs.
echo "Checking one-time stale-reopen Znuny link migration..."
python - <<'PY'
from config import Config
import sqlite3
FLAG = "migration_clear_stale_reopen_znuny_v1"
conn = sqlite3.connect(Config.DATABASE_PATH)
try:
    cur = conn.cursor()
    cur.execute("SELECT value FROM app_settings WHERE key = ?", (FLAG,))
    if cur.fetchone():
        print("Stale-reopen Znuny link migration already applied; skipping")
    else:
        cur.execute("""
            UPDATE tickets SET
                in_znuny = 0,
                znuny_ticket_id = NULL,
                znuny_created_at = NULL,
                znuny_created_by = NULL,
                znuny_address = NULL,
                znuny_url = NULL,
                znuny_search_count = 0
            WHERE completed_at IS NULL
              AND reopen_count > 0
              AND znuny_created_at IS NOT NULL
              AND last_reopened_at IS NOT NULL
              AND znuny_created_at < last_reopened_at
        """)
        cleared = cur.rowcount
        cur.execute(
            "INSERT OR REPLACE INTO app_settings (key, value, description) VALUES (?, ?, ?)",
            (FLAG, "1", "Cleared stale pre-reopen Znuny links so affected tickets re-link and get a correct time-to-create"),
        )
        conn.commit()
        print(f"Cleared stale Znuny link on {cleared} reopened ticket(s) for re-sync")
finally:
    conn.close()
PY

# Remove stale Chrome profile locks (SingletonLock/SingletonCookie/SingletonSocket)
# left in the persistent browser sessions by a previous container. These live on
# the named volume, so on a container RECREATE the new container has a different
# hostname and Chrome refuses to open the profile ("in use by another Chrome
# process ... on another computer"), exiting with code 21 — which is exactly why
# Dhiraagu's headed Chrome fails to launch and noVNC shows a black screen. No
# Chrome is running yet at this point, so clearing them is safe; Chrome recreates
# them on launch.
if [ -d /app/data/browser_sessions ]; then
    echo "Clearing stale browser profile locks in browser_sessions..."
    # Chrome: Singleton*  |  Firefox (Dhiraagu): lock / .parentlock
    find /app/data/browser_sessions -maxdepth 2 \
        \( -name 'Singleton*' -o -name 'lock' -o -name '.parentlock' \) \
        -print -delete 2>/dev/null || true
fi

echo "Starting application..."
# Start a virtual X display (Xvfb) so the real Chrome can launch HEADED, which
# is required to pass Cloudflare on Dhiraagu (headless gets challenged). We start
# Xvfb directly and set DISPLAY ourselves — more robust than xvfb-run (no xauth
# dependency). Other portals run headless and are unaffected. If Xvfb is missing
# (e.g. a non-Docker run), just run directly.
#
# IMPORTANT: with `restart: unless-stopped`, Docker restarts the SAME container
# (filesystem preserved), so a stale /tmp/.X99-lock + /tmp/.X11-unix/X99 socket
# from the previous run survive. Xvfb then refuses to start on :99 ("server
# already active"), exits, and DISPLAY=:99 points at a dead server — Chrome then
# fails with "Missing X server or $DISPLAY" and Dhiraagu breaks on every restart
# after the first. So we clean stale locks first and VERIFY Xvfb is actually up
# before exporting DISPLAY (don't leave DISPLAY set pointing at nothing).
# Dhiraagu runs Firefox HEADED under this virtual display; noVNC lets an operator
# solve its Cloudflare challenge by hand. The other portals run headless.
DISPLAY_NUM=99
if command -v Xvfb >/dev/null 2>&1; then
    echo "Starting Xvfb virtual display on :${DISPLAY_NUM}..."
    # Clear stale lock/socket left by a previous run of this same container.
    rm -f "/tmp/.X${DISPLAY_NUM}-lock" "/tmp/.X11-unix/X${DISPLAY_NUM}" 2>/dev/null || true

    Xvfb ":${DISPLAY_NUM}" -screen 0 1920x1080x24 -nolisten tcp >/tmp/xvfb.log 2>&1 &
    XVFB_PID=$!

    # Wait (up to ~10s) for the X socket to appear AND the process to still be alive.
    xvfb_ready=false
    for _ in $(seq 1 20); do
        if [ -S "/tmp/.X11-unix/X${DISPLAY_NUM}" ] && kill -0 "$XVFB_PID" 2>/dev/null; then
            xvfb_ready=true
            break
        fi
        sleep 0.5
    done

    if [ "$xvfb_ready" = true ]; then
        export DISPLAY=":${DISPLAY_NUM}"
        echo "Xvfb ready on DISPLAY=${DISPLAY} (pid ${XVFB_PID})"
    else
        # Leave DISPLAY UNSET rather than pointing at a dead server. Dhiraagu will
        # fail to bypass Cloudflare, but the rest of the app still runs headless.
        echo "WARN: Xvfb did not come up on :${DISPLAY_NUM} — see /tmp/xvfb.log. DISPLAY left unset."
        cat /tmp/xvfb.log 2>/dev/null || true
    fi
fi

# Start a lightweight window manager (fluxbox) on the virtual display. Without a
# WM, Chrome's windows/iframes don't receive proper focus and some interactive
# widgets fail to render or can't be clicked — notably the Cloudflare Turnstile
# "Verify you are human" checkbox (it shows as text only, with no clickable box).
# fluxbox gives the browser a managed, focusable window so the checkbox renders and
# can be solved via noVNC. Best-effort: never blocks the app.
if [ -n "${DISPLAY:-}" ] && command -v fluxbox >/dev/null 2>&1; then
    echo "Starting fluxbox window manager on ${DISPLAY}..."
    fluxbox >/tmp/fluxbox.log 2>&1 &
    sleep 1
fi

# Start noVNC (x11vnc + websockify) so an operator can open a browser, SEE the
# headed Chrome running on the virtual display, and manually solve a Cloudflare
# challenge (Turnstile/CAPTCHA) that the automatic bypass can't. It drives the
# SAME persistent browser the extractor uses, so the resulting cf_clearance is
# saved to the session and reused by subsequent automated cycles. Only meaningful
# when the display is up. Best-effort: failures here never block the app.
if [ -n "${DISPLAY:-}" ] && command -v x11vnc >/dev/null 2>&1; then
    NOVNC_PORT="${NOVNC_PORT:-6080}"
    if [ -n "${VNC_PASSWORD:-}" ]; then
        echo "Starting x11vnc (password-protected) on display ${DISPLAY}..."
        x11vnc -display "$DISPLAY" -forever -shared -noxdamage -passwd "$VNC_PASSWORD" \
            -rfbport 5900 -bg -o /tmp/x11vnc.log >/dev/null 2>&1 || \
            echo "WARN: x11vnc failed to start — see /tmp/x11vnc.log"
    else
        echo "WARN: VNC_PASSWORD not set — noVNC will be OPEN with NO password. Set VNC_PASSWORD to secure it."
        x11vnc -display "$DISPLAY" -forever -shared -noxdamage -nopw \
            -rfbport 5900 -bg -o /tmp/x11vnc.log >/dev/null 2>&1 || \
            echo "WARN: x11vnc failed to start — see /tmp/x11vnc.log"
    fi

    # websockify serves the noVNC web client and bridges it to x11vnc on :5900.
    if [ -d /usr/share/novnc ] && command -v websockify >/dev/null 2>&1; then
        websockify --web=/usr/share/novnc "$NOVNC_PORT" localhost:5900 >/tmp/novnc.log 2>&1 &
        echo "noVNC ready: open http://<host>:${NOVNC_PORT}/vnc.html to drive the headed Chrome"
    else
        echo "WARN: noVNC web client (/usr/share/novnc) or websockify missing — VNC available on raw port 5900 only."
    fi
fi

exec python app.py