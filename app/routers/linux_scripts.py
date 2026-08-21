import asyncio
import os
import tempfile
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from app.auth import get_current_user
from app.database import User

router = APIRouter(prefix="/api/v1/linux", tags=["Linux OS Engine"])


# ------------------------------------------------------------------
# Pydantic Models
# ------------------------------------------------------------------
class ScriptExecutionInput(BaseModel):
    script_content: str = Field(..., description="Bash script content to execute")
    timeout_seconds: int = Field(30, ge=1, le=300, description="Max execution time in seconds")
    working_directory: str = Field("/tmp", description="Working directory for the script")


# ------------------------------------------------------------------
# Security: Blocked Dangerous Patterns
# ------------------------------------------------------------------
FORBIDDEN_PATTERNS = [
    "rm -rf /", "rm -rf /*", "rm -rf *", "dd if=", "mkfs",
    "shutdown", "reboot", "halt", "poweroff",
    "chmod 777 /", "chown root:", "chown 0:0",
    ":(){ :|:& };:", "forkbomb",
    "> /dev/sd", "> /dev/disk", "mkfs",
    "sudo", "su -", "visudo",
    "echo * > /dev", "cat /dev/urandom > /dev"
]


def _validate_script_safety(script: str):
    """Raise HTTP 403 if the script contains forbidden patterns."""
    script_lower = script.lower()
    for pattern in FORBIDDEN_PATTERNS:
        if pattern in script_lower:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Script blocked: Contains forbidden pattern '{pattern}'"
            )


# ------------------------------------------------------------------
# 1. Execute Script
# ------------------------------------------------------------------
@router.post("/execute")
async def execute_shell_script(
    payload: ScriptExecutionInput,
    current_user: User = Depends(get_current_user)
):
    """
    Execute a Linux shell script securely.
    - Scripts run in a temporary file with a strict timeout.
    - Dangerous commands (e.g., rm -rf /, dd, mkfs) are blocked.
    - Output (stdout/stderr) and exit code are captured and returned.
    """
    # 1. Validate safety
    _validate_script_safety(payload.script_content)

    # 2. Create a temporary script file
    with tempfile.NamedTemporaryFile(mode='w', suffix='.sh', delete=False, dir='/tmp') as f:
        f.write("#!/bin/bash\n")
        f.write("set -e\n")  # Exit on error
        f.write(payload.script_content)
        script_path = f.name

    # 3. Make executable
    os.chmod(script_path, 0o700)

    try:
        # 4. Execute with timeout
        process = await asyncio.create_subprocess_exec(
            "bash", script_path,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=payload.working_directory
        )

        try:
            stdout, stderr = await asyncio.wait_for(
                process.communicate(),
                timeout=payload.timeout_seconds
            )
        except asyncio.TimeoutError:
            process.kill()
            await process.wait()
            raise HTTPException(
                status_code=status.HTTP_408_REQUEST_TIMEOUT,
                detail=f"Script execution timed out after {payload.timeout_seconds}s"
            )

        return {
            "success": process.returncode == 0,
            "exit_code": process.returncode,
            "stdout": stdout.decode().strip(),
            "stderr": stderr.decode().strip()
        }

    finally:
        # 5. Clean up temporary file
        if os.path.exists(script_path):
            os.unlink(script_path)
