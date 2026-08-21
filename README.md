# Server Manager Backend — Enterprise Linux Management Platform

FastAPI-based backend for enterprise Linux server administration, monitoring, Oracle administration, Microsoft SQL Server administration, Prometheus/Loki integration, WebSocket streaming, and automation.

## Production deployment

The repository now has a single production Compose specification with PostgreSQL, Alembic migrations, Nginx, Prometheus, Grafana, and Loki.

### One-command installation on Ubuntu / Debian / RHEL / Rocky / AlmaLinux / Oracle Linux

```bash
./install.sh
```

The installer:

- Detects the host OS.
- Installs Docker Engine and Compose if needed.
- Creates secure random PostgreSQL, JWT, Grafana, and initial-admin credentials.
- Creates persistent host directories for monitoring targets and database backups.
- Builds the backend image.
- Starts PostgreSQL and waits for it to become healthy.
- Runs `alembic upgrade head` automatically from the backend container entrypoint.
- Starts the full stack and verifies `/health/ready`.
- Installs the `server-manager.service` systemd unit for automatic startup.

### Manual deployment

```bash
cp .env.example .env
# Edit .env and replace all placeholder secrets.
mkdir -p prometheus_targets

docker compose config
docker compose up -d --build
```

Use the RHEL/UBI image explicitly on RHEL-compatible hosts:

```bash
SERVER_MANAGER_DOCKERFILE=Dockerfile.rhel docker compose up -d --build
```

## Database

Production uses PostgreSQL:

```text
postgresql+asyncpg://...
```

SQLite is still supported for local development. Production schema changes are managed with Alembic.

Useful commands:

```bash
alembic upgrade head
alembic revision --autogenerate -m "describe_change"
```

**Important:** The initial migration creates the current `users` table. Existing SQLite deployments are not automatically migrated into PostgreSQL; export/import is required for those installations.

## Health endpoints

- `GET /health` — liveness
- `GET /health/ready` — readiness including database connectivity
- `GET /metrics` — Prometheus metrics

## Deployment files

```text
Dockerfile                 # Ubuntu/Debian production image
Dockerfile.rhel            # RHEL/UBI production image
compose.yaml               # Main production Compose stack
install.sh                 # Fresh-host installer
alembic/                   # Database migrations
deployment/                # systemd/upgrade helpers
nginx/default.conf         # Full Nginx configuration
```

## Oracle and host integration

The backend can manage host-level services and Oracle tooling, but Docker deployments require the appropriate host mounts, Oracle installation, service permissions, and backup directories. The installer creates `/backup/oracle` and `/backup/mssql`, but does not install Oracle Database itself.

## Security notes

- Do not commit `.env`.
- Replace every placeholder secret before exposing the platform.
- Put the API behind HTTPS in production.
- Restrict PostgreSQL, Prometheus, Grafana, and Loki network access in production; they are exposed for convenience in the default Compose file.
- Review and harden the host-level Oracle sudo policy before production use.
