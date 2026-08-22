from contextlib import asynccontextmanager

from fastapi import (
    FastAPI,
    Depends,
    HTTPException,
    Request,
    status,
)
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


# ------------------------------------------------------------------
# Import ALL routers
# ------------------------------------------------------------------
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
    script_manager,
)


# ------------------------------------------------------------------
# Prometheus Gauges
# ------------------------------------------------------------------
CPU_USAGE = Gauge(
    "server_cpu_usage_percent",
    "Current CPU usage in percent",
    registry=REGISTRY,
)

RAM_USAGE = Gauge(
    "server_ram_usage_percent",
    "Current RAM usage in percent",
    registry=REGISTRY,
)

DISK_USAGE = Gauge(
    "server_disk_usage_percent",
    "Current Disk usage in percent",
    registry=REGISTRY,
)


# ------------------------------------------------------------------
# Lifespan
# ------------------------------------------------------------------
@asynccontextmanager
async def lifespan(app: FastAPI):

    # ==============================================================
    # 1. Legacy SQLite schema creation
    # ==============================================================
    #
    # Production PostgreSQL schema management is handled by Alembic.
    # SQLite development installations retain backwards compatibility.
    #
    if (
        settings.database_url.startswith("sqlite+")
        and settings.database_url.endswith("server_manager.db")
    ):
        from app.database import Base

        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

    # ==============================================================
    # 2. Secure initial superuser bootstrap
    # ==============================================================
    #
    # This code is safe when multiple Gunicorn/Uvicorn workers
    # start at exactly the same time.
    #
    if (
        settings.first_superuser
        and settings.first_superuser_password
    ):

        username = settings.first_superuser

        try:
            async with AsyncSessionLocal() as session:

                # --------------------------------------------------
                # Check whether the user already exists.
                # --------------------------------------------------
                result = await session.execute(
                    select(User).where(
                        User.username == username
                    )
                )

                existing_user = result.scalar_one_or_none()

                if existing_user:

                    print(
                        f"ℹ️  Superuser '{username}' already exists. "
                        "Skipping creation."
                    )

                else:

                    # --------------------------------------------------
                    # Create the initial superuser.
                    # --------------------------------------------------
                    admin_user = User(
                        username=username,
                        hashed_password=SecurityUtils.hash_password(
                            settings.first_superuser_password
                        ),
                    )

                    session.add(admin_user)

                    try:

                        # --------------------------------------------------
                        # Force INSERT now.
                        #
                        # This is important because the unique constraint
                        # must be checked before commit so we can catch a
                        # race between multiple Gunicorn workers.
                        # --------------------------------------------------
                        await session.flush()

                        # --------------------------------------------------
                        # Commit successful creation.
                        # --------------------------------------------------
                        await session.commit()

                        print(
                            f"✅ Superuser '{username}' "
                            "created successfully."
                        )

                    except IntegrityError:

                        # --------------------------------------------------
                        # Another worker created the same user concurrently.
                        #
                        # Roll back this worker's transaction and continue
                        # application startup normally.
                        # --------------------------------------------------
                        await session.rollback()

                        print(
                            f"ℹ️  Superuser '{username}' was created "
                            "by another worker. Skipping."
                        )

        except Exception as exc:

            # ----------------------------------------------------------
            # Unexpected database/bootstrap errors should NOT be hidden.
            # Let the worker fail so Docker/Gunicorn can report it.
            # ----------------------------------------------------------
            print(
                f"❌ Superuser bootstrap failed: "
                f"{type(exc).__name__}: {exc}"
            )

            raise

    else:

        print(
            "⚠️  No FIRST_SUPERUSER env vars set. "
            "Skipping admin creation."
        )

    # ==============================================================
    # 3. Application startup complete
    # ==============================================================
    yield

    # ==============================================================
    # 4. Cleanup
    # ==============================================================
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

    This API provides comprehensive management capabilities for
    enterprise Linux servers.

    ### System & Infrastructure

    - **System Metrics**: Real-time CPU, RAM, Disk monitoring
    - **Systemd Services**: Start, stop, restart, and stream logs
    - **Linux Shell**: Interactive terminal and script execution
    - **File Browser**: List, upload, download, delete, rename, edit

    ### Database Management

    - **Oracle Database**: Execute SQL, start/stop instances,
      create CDB/PDB, RMAN, EXPDP, IMPDP
    - **MS SQL Server**: Execute T-SQL, create/drop databases,
      backup/restore, user management

    ### Observability & Monitoring

    - **Prometheus Targets**: Dynamic scrape target registration
    - **Log Query (Loki)**: Query aggregated logs using LogQL
    - **Grafana**: Metrics and log visualization

    ### Script Management

    - **Upload Scripts**
    - **Execute Scripts**
    - **Live Script Output**
    - **View Scripts**
    - **Delete Scripts**
    - **Toggle Executable Permissions**
    """,
)


# ------------------------------------------------------------------
# CORS Middleware
# ------------------------------------------------------------------
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        origin.strip()
        for origin in settings.cors_origins.split(",")
    ],
    allow_credentials=True,
    allow_methods=[
        "GET",
        "POST",
        "PUT",
        "DELETE",
    ],
    allow_headers=[
        "Authorization",
        "Content-Type",
    ],
)


# ------------------------------------------------------------------
# Global Exception Handler
# ------------------------------------------------------------------
@app.exception_handler(Exception)
async def global_exception_handler(
    request: Request,
    exc: Exception,
):
    return JSONResponse(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        content={
            "success": False,
            "error_class": "InternalExecutionError",
            "message": "System trace variation intercepted.",
        },
    )


# ------------------------------------------------------------------
# Health Check
# ------------------------------------------------------------------
@app.get(
    "/health",
    tags=["System"],
)
async def health_check():
    """
    Liveness probe.

    Confirms that the application process is responding.
    """
    return {
        "status": "healthy",
        "service": "server-manager-backend",
        "version": settings.app_version,
    }


# ------------------------------------------------------------------
# Readiness Check
# ------------------------------------------------------------------
@app.get(
    "/health/ready",
    tags=["System"],
)
async def readiness_check():

    from sqlalchemy import text

    try:

        async with AsyncSessionLocal() as session:

            await session.execute(
                text("SELECT 1")
            )

        return {
            "status": "ready",
            "database": "ok",
            "version": settings.app_version,
        }

    except Exception:

        raise HTTPException(
            status_code=503,
            detail="Database is not ready",
        )


# ------------------------------------------------------------------
# Prometheus Metrics
# ------------------------------------------------------------------
@app.get(
    "/metrics",
    response_class=PlainTextResponse,
    tags=["Observability"],
)
async def get_prometheus_metrics():
    """
    Prometheus metrics endpoint.

    Scraped by Prometheus for CPU, RAM, and Disk metrics.
    """

    CPU_USAGE.set(
        psutil.cpu_percent(interval=None)
    )

    RAM_USAGE.set(
        psutil.virtual_memory().percent
    )

    DISK_USAGE.set(
        psutil.disk_usage("/").percent
    )

    return PlainTextResponse(
        content=generate_latest(REGISTRY).decode("utf-8")
    )


# ------------------------------------------------------------------
# Authentication
# ------------------------------------------------------------------
@app.post(
    "/api/v1/auth/login",
    response_model=TokenResponse,
    tags=["Access Rules"],
)
async def login(
    form_data: OAuth2PasswordRequestForm = Depends(),
    db: AsyncSession = Depends(get_async_db),
):
    """
    Authenticate and receive a JWT access token.

    OAuth2 password flow.
    """

    result = await db.execute(
        select(User).filter(
            User.username == form_data.username
        )
    )

    user = result.scalars().first()

    if not user:

        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid system account credentials.",
        )

    if not SecurityUtils.verify_password(
        form_data.password,
        user.hashed_password,
    ):

        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid system account credentials.",
        )

    access_token = SecurityUtils.create_access_token(
        data={
            "sub": user.username
        }
    )

    return {
        "access_token": access_token,
        "token_type": "bearer",
    }


# ------------------------------------------------------------------
# Include Routers
# ------------------------------------------------------------------

# 1. System Metrics
app.include_router(
    metrics.router
)


# 2. Systemd Services
app.include_router(
    services.router
)


# 3. Oracle Database
app.include_router(
    oracle_admin.router
)


# 4. MS SQL Server
app.include_router(
    mssql_admin.router
)


# 5. Prometheus Targets
app.include_router(
    prometheus_targets.router
)


# 6. Loki Logs
app.include_router(
    logs.router
)


# 7. Linux Scripts / Shell
app.include_router(
    linux_scripts.router
)


# 8. Interactive Terminal
app.include_router(
    terminal.router
)


# 9. File Browser
app.include_router(
    file_browser.router
)


# 10. Script Manager
app.include_router(
    script_manager.router
)


# ------------------------------------------------------------------
# Root Endpoint
# ------------------------------------------------------------------
@app.get(
    "/",
    tags=["System"],
)
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

            {
                "name": "Authentication",
                "path": "/api/v1/auth/login",
            },

            {
                "name": "System Metrics",
                "path": "/api/v1/metrics",
            },

            {
                "name": "Systemd Services",
                "path": "/api/v1/services",
            },

            {
                "name": "Oracle Database",
                "path": "/api/v1/oracle",
            },

            {
                "name": "MS SQL Server",
                "path": "/api/v1/mssql",
            },

            {
                "name": "Prometheus Targets",
                "path": "/api/v1/prometheus",
            },

            {
                "name": "Log Query (Loki)",
                "path": "/api/v1/logs",
            },

            {
                "name": "Linux Script Execution",
                "path": "/api/v1/linux/execute",
            },

            {
                "name": "Interactive Terminal",
                "path": "/api/v1/linux/terminal",
            },

            {
                "name": "File Browser",
                "path": "/api/v1/files",
            },

            {
                "name": "Script Manager",
                "path": "/api/v1/scripts",
            },

        ],
    }


# ------------------------------------------------------------------
# Startup Information
# ------------------------------------------------------------------
print("=" * 60)
print(
    f"🚀 Enterprise Linux Core Engine v{settings.app_version}"
)
print("=" * 60)

print(
    "📡 API Documentation:  "
    "http://localhost:8000/docs"
)

print(
    "📊 Prometheus Metrics: "
    "http://localhost:8000/metrics"
)

print(
    "🩺 Health Check:       "
    "http://localhost:8000/health"
)

print(
    "🔐 Authentication:     "
    "http://localhost:8000/api/v1/auth/login"
)

print("=" * 60)

print("✅ All routers loaded:")

print(
    "   - /api/v1/metrics      "
    "(System Metrics)"
)

print(
    "   - /api/v1/services     "
    "(Systemd Services)"
)

print(
    "   - /api/v1/oracle       "
    "(Oracle Database)"
)

print(
    "   - /api/v1/mssql        "
    "(MS SQL Server)"
)

print(
    "   - /api/v1/prometheus   "
    "(Prometheus Targets)"
)

print(
    "   - /api/v1/logs         "
    "(Loki Logs)"
)

print(
    "   - /api/v1/linux        "
    "(Linux Shell & Scripts)"
)

print(
    "   - /api/v1/files        "
    "(File Browser)"
)

print(
    "   - /api/v1/scripts      "
    "(Script Manager)"
)

print("=" * 60)
