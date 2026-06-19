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
exec python app.py