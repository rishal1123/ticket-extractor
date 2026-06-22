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

echo "Starting application..."
# Start a virtual X display (Xvfb) so the real Chrome can launch HEADED, which
# is required to pass Cloudflare on Dhiraagu (headless gets challenged). We start
# Xvfb directly and set DISPLAY ourselves — more robust than xvfb-run (no xauth
# dependency). Other portals run headless and are unaffected. If Xvfb is missing
# (e.g. a non-Docker run), just run directly.
if command -v Xvfb >/dev/null 2>&1; then
    echo "Starting Xvfb virtual display on :99..."
    Xvfb :99 -screen 0 1920x1080x24 -nolisten tcp >/tmp/xvfb.log 2>&1 &
    export DISPLAY=:99
    sleep 1
fi
exec python app.py