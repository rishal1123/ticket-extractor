#!/bin/bash
set -e

echo "=== Ticket Extractor - Starting ==="
echo "Python $(python --version 2>&1 | cut -d' ' -f2) | PID $$"

# Database init + migration check BEFORE starting the app.
# Database() applies the schema/migrations on construction; running it here (and
# failing fast under `set -e`) guarantees the DB is fully migrated before the
# scheduler/workers come up in app.py.
echo "Checking database (init + migrations)..."
python -c "from database import Database; Database(); print('Database ready')"

echo "Starting application..."
# Run under a virtual X display (xvfb) so the real Chrome can launch HEADED,
# which is required to pass Cloudflare on Dhiraagu (headless gets challenged).
# Other portals run headless and are unaffected. If xvfb is missing (e.g. a
# non-Docker run), fall back to running directly.
if command -v xvfb-run >/dev/null 2>&1; then
    exec xvfb-run -a --server-args="-screen 0 1920x1080x24" python app.py
else
    exec python app.py
fi