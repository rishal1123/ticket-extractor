#!/bin/bash
set -e

# =============================================================================
# Dhiraagu browser sidecar
#
# Runs ONE real, HEADED Google Chrome under a virtual display, exposing the
# DevTools/CDP endpoint on :9222 so the app container can drive it remotely
# (playwright connect_over_cdp). A human can simultaneously watch/solve the
# Cloudflare challenge in the SAME browser via noVNC on :6080 — single Chrome,
# single profile, so there's no SingletonLock conflict.
#
# Cloudflare needs: real Chrome, HEADED, same public IP (this runs on the same
# Docker host as the app, so same egress IP), and a persistent profile. The
# profile lives on the mounted /profile volume so cf_clearance survives restarts.
# =============================================================================

echo "=== Dhiraagu browser sidecar - Starting ==="

DISPLAY_NUM=99

# Clear stale X locks from a previous run of this same container (restart keeps
# the filesystem), or Xvfb refuses :99 and Chrome can't launch.
rm -f "/tmp/.X${DISPLAY_NUM}-lock" "/tmp/.X11-unix/X${DISPLAY_NUM}" 2>/dev/null || true

echo "Starting Xvfb on :${DISPLAY_NUM}..."
Xvfb ":${DISPLAY_NUM}" -screen 0 1920x1080x24 -nolisten tcp >/tmp/xvfb.log 2>&1 &
export DISPLAY=":${DISPLAY_NUM}"

# Wait for the X socket to come up before launching anything on it.
for _ in $(seq 1 20); do
    [ -S "/tmp/.X11-unix/X${DISPLAY_NUM}" ] && break
    sleep 0.5
done

# Window manager — without one, Chrome's iframes don't get focus and the
# Cloudflare Turnstile checkbox renders as text only / won't click.
echo "Starting fluxbox..."
fluxbox >/tmp/fluxbox.log 2>&1 &
sleep 1

# noVNC (x11vnc + websockify) so an operator can solve Cloudflare by hand.
NOVNC_PORT="${NOVNC_PORT:-6080}"
if [ -n "${VNC_PASSWORD:-}" ]; then
    echo "Starting x11vnc (password-protected)..."
    x11vnc -display "$DISPLAY" -forever -shared -noxdamage -passwd "$VNC_PASSWORD" \
        -rfbport 5900 -bg -o /tmp/x11vnc.log >/dev/null 2>&1 || echo "WARN: x11vnc failed — see /tmp/x11vnc.log"
else
    echo "WARN: VNC_PASSWORD not set — noVNC will be OPEN with NO password."
    x11vnc -display "$DISPLAY" -forever -shared -noxdamage -nopw \
        -rfbport 5900 -bg -o /tmp/x11vnc.log >/dev/null 2>&1 || echo "WARN: x11vnc failed — see /tmp/x11vnc.log"
fi
if [ -d /usr/share/novnc ] && command -v websockify >/dev/null 2>&1; then
    websockify --web=/usr/share/novnc "$NOVNC_PORT" localhost:5900 >/tmp/novnc.log 2>&1 &
    echo "noVNC ready: open http://<host>:${NOVNC_PORT}/vnc.html to solve Cloudflare by hand"
fi

# Persistent profile + clear any stale Chrome singleton lock (safe — no Chrome
# running yet; otherwise Chrome exits code 21 after a recreate / hostname change).
mkdir -p /profile
find /profile -maxdepth 1 -name 'Singleton*' -delete 2>/dev/null || true

# CDP forwarder: Chrome binds DevTools to LOOPBACK only and ignores
# --remote-debugging-address=0.0.0.0 (a security restriction in recent Chrome), so
# a cross-container connect to :9222 is refused (ECONNREFUSED). socat exposes
# 0.0.0.0:9223 -> 127.0.0.1:9222 so the app can reach it. The app connects by IP to
# :9223; Chrome accepts the IP Host header and builds its ws URL from it, so the
# whole handshake works. Started before Chrome (fork = connects per-request, lazily).
echo "Starting socat CDP forwarder 0.0.0.0:9223 -> 127.0.0.1:9222..."
socat TCP-LISTEN:9223,fork,reuseaddr TCP:127.0.0.1:9222 >/tmp/socat.log 2>&1 &

# Pick the real Google Chrome binary (installed via `playwright install chrome`).
CHROME="$(command -v google-chrome-stable || command -v google-chrome || echo /opt/google/chrome/chrome)"
echo "Launching headed Chrome with CDP on 127.0.0.1:9222 ($CHROME)..."

# Launched directly (NOT via Playwright), so there is no --enable-automation flag
# and navigator.webdriver stays false — better for Cloudflare than a Playwright
# launch. --remote-allow-origins=* is REQUIRED for CDP attach on Chrome 111+.
# Chrome binds :9222 to loopback (see socat note above); the app reaches it via :9223.
exec "$CHROME" \
    --user-data-dir=/profile \
    --remote-debugging-port=9222 \
    --remote-allow-origins=* \
    --no-sandbox \
    --disable-dev-shm-usage \
    --disable-gpu \
    --enable-unsafe-swiftshader \
    --disable-features=IsolateOrigins,site-per-process \
    --disable-blink-features=AutomationControlled \
    --window-size=1920,1080 \
    --no-first-run \
    --no-default-browser-check \
    about:blank