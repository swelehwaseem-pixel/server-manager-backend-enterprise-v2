import asyncio
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field, SecretStr
import pyodbc
from app.auth import get_current_user
from app.database import User

router = APIRouter(prefix="/api/v1/mssql", tags=["MSSQL Engine"])


# ------------------------------------------------------------------
# Pydantic Models
# ------------------------------------------------------------------
class MSSQLConnectionConfig(BaseModel):
    server: str = Field(..., description="Hostname or IP, e.g., 'mssql-server'")
    database: str = Field(..., description="Database name")
    username: str = Field(..., description="SQL Server login user")
    password: SecretStr = Field(..., description="SQL Server login password")
    driver: str = Field("ODBC Driver 18 for SQL Server", description="ODBC Driver name")
    encrypt: bool = Field(True, description="Use SSL encryption")
    trust_cert: bool = Field(False, description="Trust server certificate (set True for self-signed)")


class SQLQueryInput(MSSQLConnectionConfig):
    sql_query: str = Field(..., description="T-SQL query to execute (SELECT, INSERT, UPDATE, etc.)")
    fetch_limit: int = Field(100, ge=1, le=10000, description="Max rows to return for SELECT")


class BackupInput(BaseModel):
    server: str
    username: str
    password: SecretStr
    database: str
    backup_path: str = Field(..., description="Full path on host, e.g., '/backups/my_db.bak'")


class RestoreInput(BaseModel):
    server: str
    username: str
    password: SecretStr
    target_database: str = Field(..., description="Name of the database to restore to")
    backup_path: str = Field(..., description="Full path to the .bak file, e.g., '/backups/my_db.bak'")
    replace: bool = Field(True, description="If True, use WITH REPLACE to overwrite existing DB")
    move_files: dict = Field(
        None,
        description="Dict for logical-to-physical file mapping, e.g., {'DataFile1': '/var/opt/mssql/data/restored.mdf'}"
    )


class CreateDatabaseInput(MSSQLConnectionConfig):
    new_database_name: str = Field(..., min_length=3, max_length=128, pattern="^[a-zA-Z0-9_]+$")


class CreateUserInput(MSSQLConnectionConfig):
    new_username: str = Field(..., min_length=3, max_length=128, pattern="^[a-zA-Z0-9_]+$")
    new_password: SecretStr = Field(..., min_length=8)
    database_name: str = Field(..., description="Database to grant access to")
    role: str = Field("db_owner", pattern="^(db_owner|db_datareader|db_datawriter|db_ddladmin)$")


class DropUserInput(MSSQLConnectionConfig):
    username: str = Field(..., description="Login/User to drop")
    database_name: str = Field(..., description="Database containing the user")


# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------
def _get_connection_string(config: MSSQLConnectionConfig) -> str:
    return (
        f"DRIVER={{{config.driver}}};"
        f"SERVER={config.server};"
        f"DATABASE={config.database};"
        f"UID={config.username};"
        f"PWD={config.password.get_secret_value()};"
        f"Encrypt={'yes' if config.encrypt else 'no'};"
        f"TrustServerCertificate={'yes' if config.trust_cert else 'no'};"
    )


# ------------------------------------------------------------------
# 1. Execute T-SQL Query
# ------------------------------------------------------------------
@router.post("/query")
async def execute_sql_query(
    payload: SQLQueryInput,
    current_user: User = Depends(get_current_user)
):
    """Execute a T-SQL query against a MSSQL database."""
    def sync_query():
        conn = pyodbc.connect(_get_connection_string(payload))
        cursor = conn.cursor()
        cursor.execute(payload.sql_query)

        if payload.sql_query.strip().upper().startswith(("SELECT", "WITH", "SHOW")):
            rows = cursor.fetchmany(payload.fetch_limit)
            columns = [column[0] for column in cursor.description] if cursor.description else []
            result = [dict(zip(columns, row)) for row in rows]
            return {"type": "read", "rows": result, "count": len(result)}
        else:
            conn.commit()
            return {"type": "write", "rows_affected": cursor.rowcount}

    try:
        result = await asyncio.to_thread(sync_query)
        return {"success": True, "data": result}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"MSSQL Error: {str(e)}")


# ------------------------------------------------------------------
# 2. Create Database
# ------------------------------------------------------------------
@router.post("/database")
async def create_mssql_database(
    payload: CreateDatabaseInput,
    current_user: User = Depends(get_current_user)
):
    """Create a new database on the MSSQL server."""
    def sync_create_db():
        master_config = payload.model_copy()
        master_config.database = "master"
        conn = pyodbc.connect(_get_connection_string(master_config))
        conn.autocommit = True
        cursor = conn.cursor()
        cursor.execute(f"CREATE DATABASE [{payload.new_database_name}]")
        return {"database": payload.new_database_name, "status": "created"}

    result = await asyncio.to_thread(sync_create_db)
    return {"success": True, "data": result}


# ------------------------------------------------------------------
# 3. Backup Database
# ------------------------------------------------------------------
@router.post("/backup")
async def backup_mssql_database(
    payload: BackupInput,
    current_user: User = Depends(get_current_user)
):
    """Trigger a full database backup using sqlcmd."""
    cmd = [
        "sqlcmd",
        "-S", payload.server,
        "-U", payload.username,
        "-P", payload.password.get_secret_value(),
        "-Q", f"BACKUP DATABASE [{payload.database}] TO DISK = '{payload.backup_path}' WITH FORMAT, MEDIANAME = 'DBSet'"
    ]

    process = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE
    )
    stdout, stderr = await process.communicate()

    if process.returncode != 0:
        raise HTTPException(
            status_code=500,
            detail=f"SQLCMD Error: {stderr.decode().strip() or stdout.decode().strip()}"
        )
    return {"success": True, "message": f"Backup of '{payload.database}' completed to {payload.backup_path}"}


# ------------------------------------------------------------------
# 4. 🔥 NEW: Restore Database
# ------------------------------------------------------------------
@router.post("/restore")
async def restore_mssql_database(
    payload: RestoreInput,
    current_user: User = Depends(get_current_user)
):
    """
    Restore a database from a .bak file using sqlcmd.
    Supports WITH REPLACE and custom file moves.
    """
    restore_cmd = f"RESTORE DATABASE [{payload.target_database}] FROM DISK = '{payload.backup_path}'"

    if payload.replace:
        restore_cmd += " WITH REPLACE"

    if payload.move_files:
        for logical_name, physical_path in payload.move_files.items():
            restore_cmd += f", MOVE '{logical_name}' TO '{physical_path}'"

    restore_cmd += ", STATS=10"

    cmd = [
        "sqlcmd",
        "-S", payload.server,
        "-U", payload.username,
        "-P", payload.password.get_secret_value(),
        "-Q", restore_cmd
    ]

    process = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE
    )
    stdout, stderr = await process.communicate()

    if process.returncode != 0:
        raise HTTPException(
            status_code=500,
            detail=f"SQLCMD Restore Error: {stderr.decode().strip() or stdout.decode().strip()}"
        )
    return {"success": True, "message": f"Database '{payload.target_database}' restored from {payload.backup_path}"}


# ------------------------------------------------------------------
# 5. Drop Database
# ------------------------------------------------------------------
@router.delete("/database")
async def drop_mssql_database(
    payload: MSSQLConnectionConfig,
    target_database: str,
    current_user: User = Depends(get_current_user)
):
    """Drop a database. USE WITH EXTREME CAUTION."""
    def sync_drop_db():
        master_config = payload.model_copy()
        master_config.database = "master"
        conn = pyodbc.connect(_get_connection_string(master_config))
        conn.autocommit = True
        cursor = conn.cursor()
        cursor.execute(f"DROP DATABASE [{target_database}]")
        return {"database": target_database, "status": "dropped"}

    result = await asyncio.to_thread(sync_drop_db)
    return {"success": True, "data": result}


# ------------------------------------------------------------------
# 6. Create User / Login
# ------------------------------------------------------------------
@router.post("/user")
async def create_mssql_user(
    payload: CreateUserInput,
    current_user: User = Depends(get_current_user)
):
    """Create a new SQL Server login and map it to a database user."""
    def sync_create_user():
        master_config = payload.model_copy()
        master_config.database = "master"
        conn = pyodbc.connect(_get_connection_string(master_config))
        conn.autocommit = True
        cursor = conn.cursor()

        cursor.execute(
            f"CREATE LOGIN [{payload.new_username}] WITH PASSWORD = '{payload.new_password.get_secret_value()}', CHECK_POLICY = ON"
        )
        cursor.execute(f"USE [{payload.database_name}]")
        cursor.execute(
            f"CREATE USER [{payload.new_username}] FOR LOGIN [{payload.new_username}]"
        )
        cursor.execute(f"EXEC sp_addrolemember '{payload.role}', '{payload.new_username}'")

        return {
            "login": payload.new_username,
            "database": payload.database_name,
            "role": payload.role,
            "status": "created"
        }

    result = await asyncio.to_thread(sync_create_user)
    return {"success": True, "data": result}


# ------------------------------------------------------------------
# 7. Drop User / Login
# ------------------------------------------------------------------
@router.delete("/user")
async def drop_mssql_user(
    payload: DropUserInput,
    current_user: User = Depends(get_current_user)
):
    """Drop a database user and its associated server login."""
    def sync_drop_user():
        master_config = payload.model_copy()
        master_config.database = "master"
        conn = pyodbc.connect(_get_connection_string(master_config))
        conn.autocommit = True
        cursor = conn.cursor()

        cursor.execute(f"USE [{payload.database_name}]")
        cursor.execute(f"DROP USER IF EXISTS [{payload.username}]")
        cursor.execute(f"DROP LOGIN IF EXISTS [{payload.username}]")

        return {"username": payload.username, "status": "dropped"}

    result = await asyncio.to_thread(sync_drop_user)
    return {"success": True, "data": result}
