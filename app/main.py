from contextlib import asynccontextmanager
from fastapi import FastAPI, Depends, HTTPException, Request, status
from fastapi.responses import JSONResponse, PlainTextResponse
from fastapi.security import OAuth2PasswordRequestForm
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy.future import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.exc import IntegrityError
from prometheus_client import generate_latest, Gauge, REGISTRY
import psutil

from app.config import settings
from app.database import (
    engine,
    get_async_db,
    AsyncSessionLocal,
)
from app.models.user import User
from app.auth import SecurityUtils
from app.schemas.auth import TokenResponse

# 🔥 Import ALL routers (Complete Enterprise Suite)
from app.routers import (
    metrics,
    services,
    oracle_admin,
    prometheus_targets,
    mssql_admin,
    logs,
    linux_scripts,
    terminal,
    file_browser,
    script_manager,  # 🔥 NEW: Script Management
)

# ------------------------------------------------------------------
# Prometheus Gauges (System Metrics)
# ------------------------------------------------------------------
CPU_USAGE = Gauge('server_cpu_usage_percent', 'Current CPU usage in percent', registry=REGISTRY)
RAM_USAGE = Gauge('server_ram_usage_percent', 'Current RAM usage in percent', registry=REGISTRY)
DISK_USAGE = Gauge('server_disk_usage_percent', 'Current Disk usage in percent', registry=REGISTRY)


# ------------------------------------------------------------------
# Lifespan: Database creation + Secure Admin Bootstrapping
# ------------------------------------------------------------------
@asynccontextmanager
async def lifespan(app: FastAPI):
    # Schema management is owned by Alembic in production.
    # Legacy SQLite installs can still opt into create_all explicitly.
    if settings.database_url.startswith("sqlite+") and settings.database_url.endswith("server_manager.db"):
        # Existing development deployments retain backwards compatibility.
        from app.database import Base
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

    # Create initial superuser ONLY if env vars are provided.
# Safe when multiple Gunicorn workers start simultaneously.
async with AsyncSessionLocal() as session:
    async with session.begin():
        if settings.first_superuser and settings.first_superuser_password:

            result = await session.execute(
                select(User).filter(
                    User.username == settings.first_superuser
                )
            )

            existing_user = result.scalars().first()

            if existing_user:
                print(
                    f"ℹ️  Superuser '{settings.first_superuser}' "
                    "already exists. Skipping creation."
                )
            else:
                try:
                    async with session.begin_nested():
                        admin_user = User(
                            username=settings.first_superuser,
                            hashed_password=SecurityUtils.hash_password(
                                settings.first_superuser_password
                            )
                        )

                        session.add(admin_user)

                        # Force INSERT inside the savepoint.
                        await session.flush()

                    print(
                        f"✅ Superuser '{settings.first_superuser}' "
                        "created successfully."
                    )

                except IntegrityError:
                    # Another Gunicorn worker created the user
                    # concurrently. The savepoint is rolled back,
                    # while the outer transaction remains usable.
                    print(
                        f"ℹ️  Superuser '{settings.first_superuser}' "
                        "was created by another worker. Skipping."
                    )

        else:
            print(
                "⚠️  No FIRST_SUPERUSER env vars set. "
                "Skipping admin creation."
            )
            
    yield

    # 3. Cleanup on shutdown
    await engine.dispose()


# ------------------------------------------------------------------
# FastAPI Application
# ------------------------------------------------------------------
app = FastAPI(
    title="Enterprise Linux Core Engine",
    version=settings.app_version,
    lifespan=lifespan,
    description="""
    ## Enterprise Linux Server Management Suite
    
    This API provides comprehensive management capabilities for enterprise Linux servers:
    
    ### System & Infrastructure
    - **System Metrics**: Real-time CPU, RAM, Disk monitoring via Prometheus
    - **Systemd Services**: Start, stop, restart, and stream logs
    - **Linux Shell**: Interactive terminal (WebSocket) and script execution
    - **File Browser**: List, upload, download, delete, rename, edit files
    
    ### Database Management
    - **Oracle Database**: Execute queries, start/stop, create CDB/PDB, RMAN, EXPDP, IMPDP
    - **MS SQL Server**: Execute queries, create/drop databases, backup/restore, user management
    
    ### Observability & Monitoring
    - **Prometheus Targets**: Dynamic scrape target registration
    - **Log Query (Loki)**: Query aggregated logs using LogQL
    - **Grafana**: Pre-configured dashboards for metrics and logs
    
    ### Script Management
    - **Upload Scripts**: Upload shell scripts (sh, py, pl, rb, js, php, go, rs)
    - **Execute Scripts**: Run scripts with live output streaming (WebSocket)
    - **Manage Scripts**: List, view, delete, toggle executable permissions
    """
)

# ------------------------------------------------------------------
# CORS Middleware (Strict, reads from .env)
# ------------------------------------------------------------------
app.add_middleware(
    CORSMiddleware,
    allow_origins=[origin.strip() for origin in settings.cors_origins.split(",")],
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "DELETE"],
    allow_headers=["Authorization", "Content-Type"],
)

# ------------------------------------------------------------------
# Global Exception Handler (Prevents stack trace leaks)
# ------------------------------------------------------------------
@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    return JSONResponse(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        content={
            "success": False,
            "error_class": "InternalExecutionError",
            "message": "System trace variation intercepted."
        }
    )


# ------------------------------------------------------------------
# 🩺 Healthcheck Endpoint (For Docker/Kubernetes)
# ------------------------------------------------------------------
@app.get("/health", tags=["System"])
async def health_check():
    """Liveness probe: the application process is responding."""
    return {"status": "healthy", "service": "server-manager-backend", "version": settings.app_version}


@app.get("/health/ready", tags=["System"])
async def readiness_check():
    """Readiness probe: verifies the application can reach its database."""
    from sqlalchemy import text

    try:
        async with AsyncSessionLocal() as session:
            await session.execute(text("SELECT 1"))
        return {"status": "ready", "database": "ok", "version": settings.app_version}
    except Exception:
        raise HTTPException(status_code=503, detail="Database is not ready")


# ------------------------------------------------------------------
# Prometheus Metrics Endpoint
# ------------------------------------------------------------------
@app.get("/metrics", response_class=PlainTextResponse, tags=["Observability"])
async def get_prometheus_metrics():
    """
    Prometheus metrics endpoint.
    Scraped by Prometheus for monitoring CPU, RAM, and Disk usage.
    """
    CPU_USAGE.set(psutil.cpu_percent(interval=None))
    RAM_USAGE.set(psutil.virtual_memory().percent)
    DISK_USAGE.set(psutil.disk_usage("/").percent)
    return PlainTextResponse(content=generate_latest(REGISTRY).decode("utf-8"))


# ------------------------------------------------------------------
# Authentication: Login (JWT Token Generation)
# ------------------------------------------------------------------
@app.post("/api/v1/auth/login", response_model=TokenResponse, tags=["Access Rules"])
async def login(
    form_data: OAuth2PasswordRequestForm = Depends(),
    db: AsyncSession = Depends(get_async_db)
):
    """
    Authenticate and receive a JWT access token.
    
    - Use OAuth2 password flow
    - Returns Bearer token for subsequent API calls
    - Token expires based on ACCESS_TOKEN_EXPIRE_MINUTES setting
    """
    result = await db.execute(select(User).filter(User.username == form_data.username))
    user = result.scalars().first()

    if not user or not SecurityUtils.verify_password(form_data.password, user.hashed_password):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid system account credentials."
        )

    access_token = SecurityUtils.create_access_token(data={"sub": user.username})
    return {"access_token": access_token, "token_type": "bearer"}


# ------------------------------------------------------------------
# Include ALL Routers (Complete Enterprise Suite)
# ------------------------------------------------------------------

# 1. System & Infrastructure
app.include_router(metrics.router)                    # /api/v1/metrics
#   - GET  /snapshot          - System metrics snapshot
#   - WS   /live              - Live metrics streaming

# 2. Systemd Service Management
app.include_router(services.router)                   # /api/v1/services
#   - POST /control           - Start/Stop/Restart services
#   - WS   /stream/{name}     - Live service log streaming

# 3. Oracle Database Management
app.include_router(oracle_admin.router)               # /api/v1/oracle
#   - POST /query              - Execute SQL queries (Thin Mode - NO Oracle Client required)
#   - POST /instance-control   - Start/Stop database instances
#   - POST /create-database    - Create CDB/PDB via DBCA
#   - POST /rman-backup        - RMAN Full/Incremental backups
#   - POST /rman-restore       - RMAN restore with recovery
#   - POST /expdp-backup       - EXPDP Data Pump exports
#   - POST /impdp-restore      - IMPDP Data Pump imports

# 4. MS SQL Server Management
app.include_router(mssql_admin.router)                # /api/v1/mssql
#   - POST /query              - Execute T-SQL queries
#   - POST /database           - Create database
#   - POST /backup             - Full database backup
#   - POST /restore            - Restore database from .bak
#   - DELETE /database         - Drop database
#   - POST /user               - Create user with role
#   - DELETE /user             - Drop user

# 5. Prometheus Dynamic Target Management
app.include_router(prometheus_targets.router)         # /api/v1/prometheus
#   - GET  /targets            - List all targets
#   - POST /targets            - Register/update targets
#   - DELETE /targets/{job}    - Delete job targets

# 6. Log Aggregation (Grafana Loki)
app.include_router(logs.router)                       # /api/v1/logs
#   - GET /query               - Query Loki using LogQL

# 7. Linux OS Management
app.include_router(linux_scripts.router)              # /api/v1/linux/execute
#   - POST /execute            - Execute bash scripts securely (batch mode)

# 8. Interactive Terminal (WebSocket)
app.include_router(terminal.router)                   # /api/v1/linux/terminal
#   - WS  /terminal            - Full PTY terminal with copy/paste support

# 9. File Browser
app.include_router(file_browser.router)               # /api/v1/files
#   - GET  /list/{path}        - List directory contents
#   - GET  /download/{path}    - Download file (streaming)
#   - POST /upload/{path}      - Upload file(s)
#   - DELETE /delete/{path}    - Delete file/directory
#   - POST /rename             - Rename/move file/directory
#   - POST /move               - Move file/directory
#   - POST /create-file        - Create file with content
#   - POST /create-directory   - Create directory
#   - GET  /read/{path}        - Read file content
#   - POST /edit               - Edit file content

# 10. 🔥 Script Manager (NEW)
app.include_router(script_manager.router)             # /api/v1/scripts
#   - POST /upload             - Upload script file
#   - GET  /list               - List all scripts with metadata
#   - GET  /view/{name}        - View script content
#   - POST /execute            - Execute script (batch mode)
#   - WS   /stream/{name}      - Execute with live output streaming
#   - DELETE /delete/{name}    - Delete script
#   - POST /toggle-executable/{name} - Make script executable/non-executable


# ------------------------------------------------------------------
# Root Endpoint (API Information)
# ------------------------------------------------------------------
@app.get("/", tags=["System"])
async def root():
    """
    Root endpoint with API information.
    """
    return {
        "service": "Enterprise Linux Core Engine",
        "version": settings.app_version,
        "docs": "/docs",
        "health": "/health",
        "metrics": "/metrics",
        "api_prefix": "/api/v1",
        "modules": [
            {"name": "Authentication", "path": "/api/v1/auth/login"},
            {"name": "System Metrics", "path": "/api/v1/metrics"},
            {"name": "Systemd Services", "path": "/api/v1/services"},
            {"name": "Oracle Database", "path": "/api/v1/oracle"},
            {"name": "MS SQL Server", "path": "/api/v1/mssql"},
            {"name": "Prometheus Targets", "path": "/api/v1/prometheus"},
            {"name": "Log Query (Loki)", "path": "/api/v1/logs"},
            {"name": "Linux Script Execution", "path": "/api/v1/linux/execute"},
            {"name": "Interactive Terminal", "path": "/api/v1/linux/terminal"},
            {"name": "File Browser", "path": "/api/v1/files"},
            {"name": "Script Manager", "path": "/api/v1/scripts"},
        ]
    }


# ------------------------------------------------------------------
# Optional: Startup Message
# ------------------------------------------------------------------
print("=" * 60)
print(
    f"🚀 Enterprise Linux Core Engine v{settings.app_version}"
)
print("=" * 60)
print(f"📡 API Documentation:  http://localhost:8000/docs")
print(f"📊 Prometheus Metrics: http://localhost:8000/metrics")
print(f"🩺 Health Check:       http://localhost:8000/health")
print(f"🔐 Authentication:     http://localhost:8000/api/v1/auth/login")
print("=" * 60)
print("✅ All routers loaded:")
print("   - /api/v1/metrics      (System Metrics)")
print("   - /api/v1/services     (Systemd Services)")
print("   - /api/v1/oracle       (Oracle Database)")
print("   - /api/v1/mssql        (MS SQL Server)")
print("   - /api/v1/prometheus   (Prometheus Targets)")
print("   - /api/v1/logs         (Loki Logs)")
print("   - /api/v1/linux        (Linux Shell & Scripts)")
print("   - /api/v1/files        (File Browser)")
print("   - /api/v1/scripts      (Script Manager) 🔥 NEW")
print("=" * 60)
