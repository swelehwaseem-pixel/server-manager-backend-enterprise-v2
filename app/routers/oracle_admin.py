import asyncio
import os
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field, SecretStr
from app.auth import get_current_user
from app.database import User
from app.schemas.db_admin import DBInstanceControlInput, SilentDBCARequestInput
import oracledb

router = APIRouter(prefix="/api/v1/oracle", tags=["Oracle Engine"])


# ------------------------------------------------------------------
# Security Validation
# ------------------------------------------------------------------
def validate_oracle_home(path: str):
    """
    Restrict Oracle home paths to prevent sudo injection attacks.
    Only allows standard Oracle installation directories.
    """
    allowed_prefixes = [
        "/u01/app/oracle/product/",
        "/u02/app/oracle/product/",
        "/opt/oracle/product/"
    ]
    if not any(path.startswith(prefix) for prefix in allowed_prefixes):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Oracle home path is restricted to standard installation directories."
        )
    if not os.path.isdir(path):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Oracle home directory does not exist on the host system."
        )


# ------------------------------------------------------------------
# 1. Instance Control (Start / Stop)
# ------------------------------------------------------------------
@router.post("/instance-control")
async def control_oracle_instance(
    payload: DBInstanceControlInput,
    current_user: User = Depends(get_current_user)
):
    """
    Start or Stop an Oracle database instance using dbstart/dbshut.
    """
    validate_oracle_home(payload.oracle_home)

    binary_target = "dbstart" if payload.action == "start" else "dbshut"
    binary_path = f"{payload.oracle_home}/bin/{binary_target}"

    custom_env = {
        "ORACLE_HOME": payload.oracle_home,
        "ORACLE_SID": payload.oracle_sid,
        "PATH": f"{payload.oracle_home}/bin:/usr/local/bin:/usr/bin:/bin"
    }

    process = await asyncio.create_subprocess_exec(
        "sudo", "-u", "oracle", binary_path, payload.oracle_home,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=custom_env
    )
    stdout, stderr = await process.communicate()

    if process.returncode != 0:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Oracle Exec Error: {stderr.decode().strip()}"
        )

    return {"status": "Success", "stdout": stdout.decode().strip()}


# ------------------------------------------------------------------
# 2. Execute SQL Query (🔥 Thin Mode - NO Oracle Instant Client!)
# ------------------------------------------------------------------
class OracleSQLQueryInput(BaseModel):
    host: str = Field(..., description="Database host, e.g., 'localhost' or 'oracle-db'")
    port: int = Field(1521, description="Listener port")
    service_name: str = Field(..., description="Oracle service name (CDB or PDB service)")
    username: str = Field(..., description="Database user")
    password: SecretStr = Field(..., description="Database password")
    sql_query: str = Field(..., description="SQL query to execute (SELECT, INSERT, UPDATE, DDL)")
    fetch_limit: int = Field(100, ge=1, le=10000, description="Max rows to return for SELECT queries")


@router.post("/query")
async def execute_oracle_query(
    payload: OracleSQLQueryInput,
    current_user: User = Depends(get_current_user)
):
    """
    Execute a SQL query against an Oracle database (CDB or PDB).
    
    🔥 Uses `oracledb` in THIN MODE (pure Python).
    ✅ NO Oracle Instant Client libraries required on the host or container.
    ✅ Works with any Oracle version 11g+ (including 19c, 21c, 23ai).
    """
    async def async_query():
        # 🔥 EXPLICITLY SET TO THIN MODE
        # This guarantees zero dependency on Oracle Instant Client (.so files)
        oracledb.defaults.driver = "thin"

        dsn = f"{payload.host}:{payload.port}/{payload.service_name}"
        pool = await oracledb.create_pool(
            user=payload.username,
            password=payload.password.get_secret_value(),
            dsn=dsn,
            min=1,
            max=2
        )
        async with pool.acquire() as conn:
            async with conn.cursor() as cursor:
                await cursor.execute(payload.sql_query)

                # Check if it's a SELECT query
                if payload.sql_query.strip().upper().startswith(("SELECT", "WITH")):
                    rows = await cursor.fetchmany(payload.fetch_limit)
                    columns = [col[0] for col in cursor.description] if cursor.description else []
                    result = [dict(zip(columns, row)) for row in rows]
                    return {"type": "read", "rows": result, "count": len(result)}
                else:
                    await conn.commit()
                    return {"type": "write", "rows_affected": cursor.rowcount}

    try:
        result = await async_query()
        return {"success": True, "data": result}
    except oracledb.DatabaseError as e:
        error_obj, = e.args
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Oracle Database Error: {error_obj.message}"
        )
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Oracle Query Error: {str(e)}"
        )


# ------------------------------------------------------------------
# 3. Create Database (CDB / PDB via DBCA)
# ------------------------------------------------------------------
@router.post("/create-database")
async def create_database_silent(
    payload: SilentDBCARequestInput,
    current_user: User = Depends(get_current_user)
):
    """
    Silently create a new Oracle database (CDB or non-CDB) using DBCA.
    Supports multi-tenant (CDB with PDBs) or single-tenant.
    """
    validate_oracle_home(payload.oracle_home)

    dbca_binary = f"{payload.oracle_home}/bin/dbca"
    cmd = [
        "sudo", "-u", "oracle", dbca_binary, "-silent", "-createDatabase",
        "-templateName", "General_Purpose.dbc", "-sid", payload.sid,
        "-gdbname", payload.global_db_name, "-sysPassword", payload.sys_password,
        "-systemPassword", payload.system_password, "-databaseType", "MULTIPURPOSE",
        "-memoryMgmtType", "AUTO_SGA", "-totalMemory", str(payload.total_memory_mb),
        "-responseFile", "NO_VALUE", "-ignorePreReqs"
    ]

    if payload.create_as_cdb:
        cmd.extend([
            "-createAsContainerDatabase", "true",
            "-numberOfPDBs", str(payload.number_of_pdbs),
            "-pdbName", payload.pdb_name,
            "-pdbAdminPassword", payload.pdb_admin_password
        ])
    else:
        cmd.extend(["-createAsContainerDatabase", "false"])

    custom_env = {
        "ORACLE_HOME": payload.oracle_home,
        "PATH": f"{payload.oracle_home}/bin:/usr/bin:/bin"
    }

    try:
        process = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=custom_env
        )
        # Run monitoring in background
        asyncio.create_task(monitor_dbca_process(process, payload.sid))
        return {
            "status": "Processing Spawning",
            "message": f"DBCA initialization tasked for SID: {payload.sid}"
        }
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Process Pipeline Failed: {str(e)}"
        )


async def monitor_dbca_process(process, sid: str):
    """Background task to log DBCA completion."""
    stdout, stderr = await process.communicate()
    print(f"[DBCA Finished] SID: {sid} Code: {process.returncode}")


# ------------------------------------------------------------------
# 4. RMAN Backup (Full / Incremental)
# ------------------------------------------------------------------
class RMANBackupInput(BaseModel):
    oracle_home: str = Field(..., max_length=255)
    oracle_sid: str = Field(..., max_length=50, pattern="^[a-zA-Z0-9_]+$")
    backup_type: str = Field("full", pattern="^(full|incremental)$")
    backup_destination: str = Field(
        "/backup/oracle",
        description="Directory on host for backup files"
    )


@router.post("/rman-backup")
async def rman_backup(
    payload: RMANBackupInput,
    current_user: User = Depends(get_current_user)
):
    """
    Trigger an Oracle RMAN database backup.
    - `full`: Complete database + archivelog backup.
    - `incremental`: Level 1 incremental backup.
    """
    validate_oracle_home(payload.oracle_home)

    if payload.backup_type == "full":
        rman_commands = """
        CONFIGURE CONTROLFILE AUTOBACKUP ON;
        CONFIGURE DEVICE TYPE DISK PARALLELISM 2;
        BACKUP AS BACKUPSET DATABASE PLUS ARCHIVELOG DELETE INPUT;
        """
    else:  # incremental
        rman_commands = """
        CONFIGURE CONTROLFILE AUTOBACKUP ON;
        CONFIGURE DEVICE TYPE DISK PARALLELISM 2;
        BACKUP INCREMENTAL LEVEL 1 DATABASE PLUS ARCHIVELOG DELETE INPUT;
        """

    cmd = [
        "sudo", "-u", "oracle",
        "bash", "-c",
        f"export ORACLE_HOME={payload.oracle_home}; "
        f"export ORACLE_SID={payload.oracle_sid}; "
        f"export PATH=$ORACLE_HOME/bin:$PATH; "
        f"mkdir -p {payload.backup_destination}; "
        f"rman target / <<EOF\n"
        f"CONFIGURE CHANNEL DEVICE TYPE DISK FORMAT '{payload.backup_destination}/%U';\n"
        f"{rman_commands}\n"
        f"EXIT;\nEOF"
    ]

    process = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE
    )
    stdout, stderr = await process.communicate()

    if process.returncode != 0:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"RMAN Error: {stderr.decode().strip()}"
        )

    return {"status": "RMAN backup initiated", "stdout": stdout.decode().strip()}


# ------------------------------------------------------------------
# 5. RMAN Restore + Recovery
# ------------------------------------------------------------------
class RMANRestoreInput(BaseModel):
    oracle_home: str = Field(..., max_length=255)
    oracle_sid: str = Field(..., max_length=50, pattern="^[a-zA-Z0-9_]+$")
    restore_destination: str = Field(
        "/backup/oracle",
        description="Directory containing RMAN backupsets"
    )
    recover_db: bool = Field(True, description="Automatically recover database after restore")
    resetlogs: bool = Field(True, description="Use RESETLOGS when opening (required if recovery performed)")


@router.post("/rman-restore")
async def rman_restore(
    payload: RMANRestoreInput,
    current_user: User = Depends(get_current_user)
):
    """
    Restore and optionally recover an Oracle database from RMAN backups.
    Runs in sequence: STARTUP NOMOUNT → RESTORE CONTROLFILE → MOUNT → RESTORE DATABASE → RECOVER → OPEN.
    """
    validate_oracle_home(payload.oracle_home)

    # Build RMAN restore script
    rman_script = """
    STARTUP NOMOUNT;
    RESTORE CONTROLFILE FROM AUTOBACKUP;
    ALTER DATABASE MOUNT;
    RESTORE DATABASE;
    """

    if payload.recover_db:
        rman_script += """
    RECOVER DATABASE;
    """

    open_cmd = "ALTER DATABASE OPEN RESETLOGS;" if payload.resetlogs else "ALTER DATABASE OPEN;"
    rman_script += f"""
    {open_cmd}
    EXIT;
    """

    cmd = [
        "sudo", "-u", "oracle",
        "bash", "-c",
        f"export ORACLE_HOME={payload.oracle_home}; "
        f"export ORACLE_SID={payload.oracle_sid}; "
        f"export PATH=$ORACLE_HOME/bin:$PATH; "
        f"export ORACLE_HOME={payload.oracle_home}; "
        f"rman target / <<EOF\n"
        f"{rman_script}\n"
        f"EOF"
    ]

    process = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE
    )
    stdout, stderr = await process.communicate()

    if process.returncode != 0:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"RMAN Restore Error: {stderr.decode().strip()}"
        )

    return {"status": "RMAN restore completed", "stdout": stdout.decode().strip()}


# ------------------------------------------------------------------
# 6. EXPDP Backup (Data Pump Export)
# ------------------------------------------------------------------
class EXPDPBackupInput(BaseModel):
    oracle_home: str = Field(..., max_length=255)
    oracle_sid: str = Field(..., max_length=50, pattern="^[a-zA-Z0-9_]+$")
    schemas: str = Field(..., description="Comma-separated schema names, e.g., 'HR,SCOTT'")
    directory_name: str = Field("DATA_PUMP_DIR", description="Oracle directory object")
    dumpfile_name: str = Field("expdp_%U.dmp", description="Dump file pattern")
    logfile_name: str = Field("expdp.log", description="Log file name")
    parallel: int = Field(4, ge=1, le=16, description="Parallel degree")


@router.post("/expdp-backup")
async def expdp_backup(
    payload: EXPDPBackupInput,
    current_user: User = Depends(get_current_user)
):
    """
    Trigger an Oracle Data Pump Export (EXPDP) for specified schemas.
    """
    validate_oracle_home(payload.oracle_home)

    cmd = [
        "sudo", "-u", "oracle",
        "bash", "-c",
        f"export ORACLE_HOME={payload.oracle_home}; "
        f"export ORACLE_SID={payload.oracle_sid}; "
        f"export PATH=$ORACLE_HOME/bin:$PATH; "
        f"expdp {payload.schemas} directory={payload.directory_name} "
        f"dumpfile={payload.dumpfile_name} logfile={payload.logfile_name} "
        f"parallel={payload.parallel} cluster=N"
    ]

    process = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE
    )
    stdout, stderr = await process.communicate()

    if process.returncode != 0:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"EXPDP Error: {stderr.decode().strip()}"
        )

    return {"status": "EXPDP export initiated", "stdout": stdout.decode().strip()}


# ------------------------------------------------------------------
# 7. IMPDP Restore (Data Pump Import)
# ------------------------------------------------------------------
class IMPDPRestoreInput(BaseModel):
    oracle_home: str = Field(..., max_length=255)
    oracle_sid: str = Field(..., max_length=50, pattern="^[a-zA-Z0-9_]+$")
    directory_name: str = Field("DATA_PUMP_DIR", description="Oracle directory containing the dump")
    dumpfile_name: str = Field(..., description="Dump file name, e.g., 'expdp.dmp'")
    logfile_name: str = Field("impdp.log", description="Log file name")
    schemas: str = Field(..., description="Comma-separated schemas to import")
    parallel: int = Field(4, ge=1, le=16, description="Parallel degree")
    table_exists_action: str = Field("SKIP", pattern="^(SKIP|APPEND|TRUNCATE|REPLACE)$")


@router.post("/impdp-restore")
async def impdp_restore(
    payload: IMPDPRestoreInput,
    current_user: User = Depends(get_current_user)
):
    """
    Trigger an Oracle Data Pump Import (IMPDP) to restore schemas from a dump file.
    """
    validate_oracle_home(payload.oracle_home)

    cmd = [
        "sudo", "-u", "oracle",
        "bash", "-c",
        f"export ORACLE_HOME={payload.oracle_home}; "
        f"export ORACLE_SID={payload.oracle_sid}; "
        f"export PATH=$ORACLE_HOME/bin:$PATH; "
        f"impdp {payload.schemas} directory={payload.directory_name} "
        f"dumpfile={payload.dumpfile_name} logfile={payload.logfile_name} "
        f"parallel={payload.parallel} "
        f"table_exists_action={payload.table_exists_action} cluster=N"
    ]

    process = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE
    )
    stdout, stderr = await process.communicate()

    if process.returncode != 0:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"IMPDP Error: {stderr.decode().strip()}"
        )

    return {"status": "IMPDP restore initiated", "stdout": stdout.decode().strip()}
