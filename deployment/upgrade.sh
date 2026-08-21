#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."

echo "Pulling/building latest Server Manager images..."
docker compose pull --ignore-buildable || true
docker compose build --pull

echo "Starting updated stack and applying Alembic migrations..."
docker compose up -d

echo "Checking application health..."
for _ in {1..30}; do
  if curl -fsS http://127.0.0.1/health >/dev/null 2>&1; then
    echo "Server Manager is healthy."
    exit 0
  fi
  sleep 2
done

echo "Health check failed. Inspect: docker compose logs server-manager-backend"
exit 1
