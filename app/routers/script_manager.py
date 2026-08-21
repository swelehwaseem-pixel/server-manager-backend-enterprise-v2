import os
import asyncio
import tempfile
import shutil
from datetime import datetime
from typing import List, Optional
from fastapi import APIRouter, Depends, HTTPException, UploadFile, File, WebSocket, WebSocketDisconnect, status
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field
from app.auth import get_current_user
from app.database import User
import stat
import mimetypes

router = APIRouter(prefix="/api/v1/scripts", tags=["Script Manager"])

# ------------------------------------------------------------------
# Configuration
# ------------------------------------------------------------------
# Directory where scripts are stored
SCRIPTS_DIR = os.getenv("SCRIPTS_DIR", "/app/scripts")

# Allowed script extensions
ALLOWED_EXTENSIONS = {'.sh', '.py', '.pl', '.rb', '.js', '.php', '.go', '.rs', '.bash'}

# Maximum script size (5MB)
MAX_SCRIPT_SIZE = 5 * 1024 * 1024

# Default timeout for script execution (seconds)
DEFAULT_TIMEOUT = 60

# Ensure scripts directory exists
os.makedirs(SCRIPTS_DIR, exist_ok=True)


# ------------------------------------------------------------------
# Pydantic Models
# ------------------------------------------------------------------
class ScriptInfo(BaseModel):
    name: str
    path: str
    size: int
    size_human: str
    permissions: str
    owner: str
    group: str
    modified: str
    extension: str
    is_executable: bool


class ScriptExecutionInput(BaseModel):
    script_name: str = Field(..., description="Name of the script to execute")
    args: List[str] = Field([], description="Command-line arguments to pass to the script")
    timeout: int = Field(DEFAULT_TIMEOUT, ge=1, le=300, description="Execution timeout in seconds")
    working_directory: str = Field("/tmp", description="Working directory for execution")


class ScriptUploadResponse(BaseModel):
    success: bool
    name: str
    path: str
    size: int
    message: str


# ------------------------------------------------------------------
# Helper Functions
# ------------------------------------------------------------------
def _validate_script_name(name: str) -> str:
    """Validate script name for security."""
    # Prevent path traversal
    if ".." in name or "/" in name or "\\" in name:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid script name. Path traversal not allowed."
        )
    # Check extension
    ext = os.path.splitext(name)[1].lower()
    if ext not in ALLOWED_EXTENSIONS:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Unsupported script type. Allowed: {', '.join(ALLOWED_EXTENSIONS)}"
        )
    return name


def _get_script_info(script_path: str) -> ScriptInfo:
    """Get script metadata."""
    stat_info = os.stat(script_path)
    size = stat_info.st_size
    size_human = _human_readable_size(size)
    
    # Permissions as string
    perms = _permission_string(stat_info.st_mode)
    
    # Owner and group
    import pwd, grp
    try:
        owner = pwd.getpwuid(stat_info.st_uid).pw_name
    except KeyError:
        owner = str(stat_info.st_uid)
    try:
        group = grp.getgrgid(stat_info.st_gid).gr_name
    except KeyError:
        group = str(stat_info.st_gid)
    
    # Modified time
    modified = datetime.fromtimestamp(stat_info.st_mtime).isoformat()
    
    # Extension
    ext = os.path.splitext(script_path)[1].lower()
    
    # Is executable
    is_executable = bool(stat_info.st_mode & stat.S_IXUSR)
    
    return ScriptInfo(
        name=os.path.basename(script_path),
        path=script_path,
        size=size,
        size_human=size_human,
        permissions=perms,
        owner=owner,
        group=group,
        modified=modified,
        extension=ext,
        is_executable=is_executable
    )


def _human_readable_size(size: int) -> str:
    """Convert bytes to human readable format."""
    for unit in ['B', 'KB', 'MB', 'GB', 'TB']:
        if size < 1024.0:
            return f"{size:.1f} {unit}"
        size /= 1024.0
    return f"{size:.1f} PB"


def _permission_string(mode: int) -> str:
    """Convert stat mode to permission string."""
    perms = []
    for i in range(9):
        perms.append('r' if mode & (1 << (8 - i)) else '-')
    return ''.join(perms)


def _get_interpreter(script_path: str) -> str:
    """Determine interpreter based on shebang or extension."""
    try:
        with open(script_path, 'r') as f:
            first_line = f.readline().strip()
            if first_line.startswith('#!'):
                return first_line[2:].split()[0]
    except Exception:
        pass
    
    # Fallback based on extension
    ext = os.path.splitext(script_path)[1].lower()
    interpreters = {
        '.sh': '/bin/bash',
        '.py': '/usr/bin/python3',
        '.pl': '/usr/bin/perl',
        '.rb': '/usr/bin/ruby',
        '.js': '/usr/bin/node',
        '.php': '/usr/bin/php',
        '.go': '/usr/bin/go',
        '.rs': '/usr/bin/rustc',
        '.bash': '/bin/bash'
    }
    return interpreters.get(ext, '/bin/bash')


# ------------------------------------------------------------------
# 1. Upload Script
# ------------------------------------------------------------------
@router.post("/upload", response_model=ScriptUploadResponse)
async def upload_script(
    file: UploadFile = File(...),
    current_user: User = Depends(get_current_user)
):
    """
    Upload a new script file.
    Supports: .sh, .py, .pl, .rb, .js, .php, .go, .rs, .bash
    """
    # Validate filename
    filename = _validate_script_name(file.filename)
    
    # Check file size
    content = await file.read()
    if len(content) > MAX_SCRIPT_SIZE:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail=f"Script too large. Max size: {MAX_SCRIPT_SIZE} bytes"
        )
    
    # Save the script
    script_path = os.path.join(SCRIPTS_DIR, filename)
    
    # Check if script already exists
    if os.path.exists(script_path):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Script '{filename}' already exists"
        )
    
    # Write file
    try:
        with open(script_path, 'wb') as f:
            f.write(content)
        
        # Make executable by default
        os.chmod(script_path, 0o755)
        
        return ScriptUploadResponse(
            success=True,
            name=filename,
            path=script_path,
            size=len(content),
            message=f"Script '{filename}' uploaded successfully"
        )
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to upload script: {str(e)}"
        )


# ------------------------------------------------------------------
# 2. List Scripts
# ------------------------------------------------------------------
@router.get("/list")
async def list_scripts(
    current_user: User = Depends(get_current_user)
):
    """
    List all available scripts with metadata.
    """
    scripts = []
    for filename in os.listdir(SCRIPTS_DIR):
        script_path = os.path.join(SCRIPTS_DIR, filename)
        if os.path.isfile(script_path):
            try:
                scripts.append(_get_script_info(script_path))
            except Exception:
                # Skip files we can't read
                continue
    
    return {
        "success": True,
        "total": len(scripts),
        "scripts": sorted(scripts, key=lambda x: x.name.lower())
    }


# ------------------------------------------------------------------
# 3. View Script Content
# ------------------------------------------------------------------
@router.get("/view/{script_name}")
async def view_script(
    script_name: str,
    current_user: User = Depends(get_current_user)
):
    """
    View the content of a script.
    """
    # Validate and get script path
    validated_name = _validate_script_name(script_name)
    script_path = os.path.join(SCRIPTS_DIR, validated_name)
    
    if not os.path.exists(script_path):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Script '{script_name}' not found"
        )
    
    if not os.path.isfile(script_path):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"'{script_name}' is not a file"
        )
    
    # Check file size
    file_size = os.path.getsize(script_path)
    if file_size > MAX_SCRIPT_SIZE:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail="Script is too large to view"
        )
    
    try:
        with open(script_path, 'r') as f:
            content = f.read()
        return {
            "success": True,
            "name": script_name,
            "path": script_path,
            "content": content,
            "size": file_size,
            "size_human": _human_readable_size(file_size)
        }
    except UnicodeDecodeError:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Script is not a valid text file"
        )


# ------------------------------------------------------------------
# 4. Execute Script (Batch Mode)
# ------------------------------------------------------------------
@router.post("/execute")
async def execute_script(
    payload: ScriptExecutionInput,
    current_user: User = Depends(get_current_user)
):
    """
    Execute a script and return the complete output after completion.
    Good for short-running scripts.
    """
    # Validate script
    validated_name = _validate_script_name(payload.script_name)
    script_path = os.path.join(SCRIPTS_DIR, validated_name)
    
    if not os.path.exists(script_path):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Script '{payload.script_name}' not found"
        )
    
    # Check if executable
    if not os.access(script_path, os.X_OK):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Script '{payload.script_name}' is not executable"
        )
    
    # Build command
    interpreter = _get_interpreter(script_path)
    cmd = [interpreter, script_path] + payload.args
    
    # Execute script
    try:
        process = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=payload.working_directory
        )
        
        try:
            stdout, stderr = await asyncio.wait_for(
                process.communicate(),
                timeout=payload.timeout
            )
        except asyncio.TimeoutError:
            process.kill()
            await process.wait()
            raise HTTPException(
                status_code=status.HTTP_408_REQUEST_TIMEOUT,
                detail=f"Script execution timed out after {payload.timeout}s"
            )
        
        return {
            "success": process.returncode == 0,
            "exit_code": process.returncode,
            "stdout": stdout.decode().strip(),
            "stderr": stderr.decode().strip(),
            "script": payload.script_name,
            "execution_time": payload.timeout
        }
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Script execution failed: {str(e)}"
        )


# ------------------------------------------------------------------
# 5. Execute Script with Live Output (WebSocket)
# 🔥 This is what you need for live streaming!
# ------------------------------------------------------------------
@router.websocket("/stream/{script_name}")
async def stream_script_output(
    websocket: WebSocket,
    script_name: str,
    token: str,
    args: str = "",
    timeout: int = DEFAULT_TIMEOUT,
    working_directory: str = "/tmp"
):
    """
    Execute a script and stream output in real-time via WebSocket.
    Perfect for long-running scripts where you want to see output live.
    """
    # ----------------------------------------------------------
    # 1. Authentication via JWT token in query string
    # ----------------------------------------------------------
    from jose import JWTError, jwt
    from app.config import settings
    
    if not token:
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION, reason="Missing token")
        return
    
    try:
        payload = jwt.decode(token, settings.secret_key, algorithms=[settings.algorithm])
        username = payload.get("sub")
        if not username:
            await websocket.close(code=status.WS_1008_POLICY_VIOLATION, reason="Invalid token payload")
            return
    except JWTError:
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION, reason="Invalid token")
        return
    
    # ----------------------------------------------------------
    # 2. Validate script
    # ----------------------------------------------------------
    validated_name = _validate_script_name(script_name)
    script_path = os.path.join(SCRIPTS_DIR, validated_name)
    
    if not os.path.exists(script_path):
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION, reason="Script not found")
        return
    
    if not os.access(script_path, os.X_OK):
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION, reason="Script not executable")
        return
    
    # Parse arguments
    arg_list = args.split() if args else []
    
    # ----------------------------------------------------------
    # 3. Accept WebSocket connection
    # ----------------------------------------------------------
    await websocket.accept()
    
    # Send start message
    await websocket.send_json({
        "type": "start",
        "script": script_name,
        "message": f"Executing script '{script_name}'..."
    })
    
    # ----------------------------------------------------------
    # 4. Execute script with live output streaming
    # ----------------------------------------------------------
    interpreter = _get_interpreter(script_path)
    cmd = [interpreter, script_path] + arg_list
    
    try:
        process = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=working_directory
        )
        
        # ----------------------------------------------------------
        # 5. Stream stdout and stderr in real-time
        # ----------------------------------------------------------
        async def read_stream(stream, stream_type):
            while True:
                line = await stream.readline()
                if not line:
                    break
                try:
                    decoded_line = line.decode().strip()
                    if decoded_line:
                        await websocket.send_json({
                            "type": stream_type,
                            "data": decoded_line,
                            "timestamp": datetime.now().isoformat()
                        })
                except Exception:
                    # Handle binary data or encoding errors
                    pass
        
        # Create tasks for stdout and stderr
        stdout_task = asyncio.create_task(read_stream(process.stdout, "stdout"))
        stderr_task = asyncio.create_task(read_stream(process.stderr, "stderr"))
        
        # Wait for process to complete with timeout
        try:
            return_code = await asyncio.wait_for(process.wait(), timeout=timeout)
            
            # Wait for streams to finish
            await stdout_task
            await stderr_task
            
            # Send completion message
            await websocket.send_json({
                "type": "complete",
                "exit_code": return_code,
                "success": return_code == 0,
                "message": f"Script completed with exit code {return_code}"
            })
            
        except asyncio.TimeoutError:
            # Kill the process on timeout
            process.kill()
            await process.wait()
            stdout_task.cancel()
            stderr_task.cancel()
            
            await websocket.send_json({
                "type": "error",
                "message": f"Script execution timed out after {timeout}s"
            })
            await websocket.close(code=status.WS_1008_POLICY_VIOLATION, reason="Timeout")
            return
        
    except Exception as e:
        await websocket.send_json({
            "type": "error",
            "message": f"Script execution failed: {str(e)}"
        })
        await websocket.close(code=status.WS_1011_INTERNAL_ERROR)
        return
    
    finally:
        # Clean up
        if not websocket.client_state.disconnected:
            await websocket.close()


# ------------------------------------------------------------------
# 6. Delete Script
# ------------------------------------------------------------------
@router.delete("/delete/{script_name}")
async def delete_script(
    script_name: str,
    current_user: User = Depends(get_current_user)
):
    """
    Delete a script.
    """
    validated_name = _validate_script_name(script_name)
    script_path = os.path.join(SCRIPTS_DIR, validated_name)
    
    if not os.path.exists(script_path):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Script '{script_name}' not found"
        )
    
    try:
        os.remove(script_path)
        return {
            "success": True,
            "name": script_name,
            "message": f"Script '{script_name}' deleted successfully"
        }
    except PermissionError:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Permission denied to delete '{script_name}'"
        )


# ------------------------------------------------------------------
# 7. Make Script Executable / Non-Executable
# ------------------------------------------------------------------
@router.post("/toggle-executable/{script_name}")
async def toggle_executable(
    script_name: str,
    executable: bool = True,
    current_user: User = Depends(get_current_user)
):
    """
    Toggle the executable flag on a script.
    """
    validated_name = _validate_script_name(script_name)
    script_path = os.path.join(SCRIPTS_DIR, validated_name)
    
    if not os.path.exists(script_path):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Script '{script_name}' not found"
        )
    
    try:
        mode = 0o755 if executable else 0o644
        os.chmod(script_path, mode)
        return {
            "success": True,
            "name": script_name,
            "executable": executable,
            "message": f"Script '{script_name}' {'made executable' if executable else 'made non-executable'}"
        }
    except PermissionError:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Permission denied to change permissions"
        )
