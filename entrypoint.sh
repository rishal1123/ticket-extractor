#!/bin/bash
set -e

echo "=== Ticket Extractor - Starting ==="
echo "Python $(python --version 2>&1 | cut -d' ' -f2) | PID $$"

exec python app.py
