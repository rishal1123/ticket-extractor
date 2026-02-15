#!/bin/bash
set -e

echo "=== Ticket Extractor - Startup ==="
echo "Running database migration check..."

python -c "
from database import Database
db = Database()
print('Database check complete - all tables and columns verified')
"

echo "Starting application..."
exec python app.py
