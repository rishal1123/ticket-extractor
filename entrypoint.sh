#!/bin/bash

# Create .env in data volume from example if it doesn't exist
if [ ! -f /app/data/.env ]; then
    echo "No .env found in data volume, creating from example..."
    cp /app/.env.example /app/data/.env
fi

exec "$@"
