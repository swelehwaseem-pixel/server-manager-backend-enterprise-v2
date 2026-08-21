#!/usr/bin/env sh
set -eu

: "${DATABASE_URL:?DATABASE_URL must be set}"

printf '%s\n' "Running database migrations..."
alembic upgrade head

printf '%s\n' "Starting Server Manager Backend..."

if [ "$#" -gt 0 ]; then
  exec "$@"
fi

exec gunicorn app.main:app \
  -w "${WEB_CONCURRENCY:-2}" \
  -k uvicorn.workers.UvicornWorker \
  --bind 0.0.0.0:8000 \
  --timeout 120 \
  --graceful-timeout 30 \
  --access-logfile - \
  --error-logfile -
