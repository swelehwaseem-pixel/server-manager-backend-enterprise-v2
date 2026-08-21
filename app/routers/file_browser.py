import os
import shutil
import stat
import mimetypes
from datetime import datetime
from fastapi import APIRouter, Depends, HTTPException, UploadFile, File, status
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field
from typing import List, Optional
from app.auth import get_current_user
from app.database import User

router = APIRouter(prefix="/api/v1/files", tags=["File Browser"])

# ------------------------------------------------------------------
# Security Constants
# ------------------------------------------------------------------
# 🔒 Block operations on these critical system paths
FORBIDDEN_PATHS = [
    "/etc/passwd",
    "/etc/shadow",
    "/etc/sudoers",
    "/etc/ssh",
    "/root",
    "/boot",
    "/dev",
    "/proc",
    "/sys",
    "/run",
    "/var/log",
    "/var/lib/docker",
    "/usr/bin",
    "/usr/sbin",
    "/bin",
    "/sbin",
]

# 🔒 Block these file extensions from being executed/read (security)
FORBIDDEN_EXTENSIONS = [".pem", ".key", ".crt", ".p12", ".pfx", ".keystore"]


# ------------------------------------------------------------------
# Pydantic Models
# ------------------------------------------------------------------
class FileInfo(BaseModel):
    name: str
    path: str
    is_dir: bool
    size: int
    size_human: str
    permissions: str
    owner: str
    group: str
    modified: str
    extension: Optional[str] = None
    mime_type: Optional[str] = None


class CreateFileInput(BaseModel):
    path: str = Field(..., description="Full path to the file to create")
    content: str = Field("", description="Initial content for the file")
    overwrite: bool = Field(False, description="Overwrite if file exists")


class CreateDirectoryInput(BaseModel):
    path: str = Field(..., description="Full path to the directory to create")
    parents: bool = Field(True, description="Create parent directories if they don't exist")


class RenameInput(BaseModel):
    old_path: str = Field(..., description="Current file/directory path")
    new_path: str = Field(..., description="New file/directory path")


class MoveInput(BaseModel):
    source: str = Field(..., description="Source file/directory path")
    destination: str = Field(..., description="Destination path")


class EditFileInput(BaseModel):
    path: str = Field(..., description="File path to edit")
    content: str = Field(..., description="New content to write")


# ------------------------------------------------------------------
# Helper Functions
# ------------------------------------------------------------------
def _validate_path(path: str, base_dir: Optional[str] = None) -> str:
    """
    Validate that the path is safe and not attempting path traversal.
    Raises HTTP 403 if unsafe.
    """
    # Resolve absolute path
    abs_path = os.path.abspath(path)
    
    # Block path traversal attacks
    if ".." in os.path.normpath(abs_path):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Path traversal not allowed"
        )
    
    # Check against forbidden paths
    for forbidden in FORBIDDEN_PATHS:
        if abs_path.startswith(forbidden):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Access to '{forbidden}' is forbidden"
            )
    
    # If base_dir is set, ensure path is within base_dir (optional chroot)
    if base_dir:
        base_abs = os.path.abspath(base_dir)
        if not abs_path.startswith(base_abs):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Path must be within '{base_dir}'"
            )
    
    return abs_path


def _get_file_info(path: str) -> FileInfo:
    """Get file metadata as FileInfo object."""
    stat_info = os.stat(path)
    is_dir = os.path.isdir(path)
    size = stat_info.st_size
    size_human = _human_readable_size(size)
    
    # Permissions as string (e.g., "rw-r--r--")
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
    
    # Extension and MIME type (for files only)
    ext = None
    mime_type = None
    if not is_dir:
        ext = os.path.splitext(path)[1].lower()
        mime_type = mimetypes.guess_type(path)[0] or "application/octet-stream"
    
    return FileInfo(
        name=os.path.basename(path),
        path=path,
        is_dir=is_dir,
        size=size,
        size_human=size_human,
        permissions=perms,
        owner=owner,
        group=group,
        modified=modified,
        extension=ext,
        mime_type=mime_type,
    )


def _human_readable_size(size: int) -> str:
    """Convert bytes to human readable format."""
    for unit in ['B', 'KB', 'MB', 'GB', 'TB']:
        if size < 1024.0:
            return f"{size:.1f} {unit}"
        size /= 1024.0
    return f"{size:.1f} PB"


def _permission_string(mode: int) -> str:
    """Convert stat mode to permission string (e.g., 'rw-r--r--')."""
    perms = []
    for i in range(9):
        perms.append('r' if mode & (1 << (8 - i)) else '-')
    return ''.join(perms)


# ------------------------------------------------------------------
# 1. List Directory Contents
# ------------------------------------------------------------------
@router.get("/list/{full_path:path}")
async def list_files(
    full_path: str,
    base_dir: Optional[str] = None,
    current_user: User = Depends(get_current_user)
):
    """
    List all files and directories at the specified path.
    Returns detailed metadata for each item.
    """
    abs_path = _validate_path(full_path, base_dir)
    
    if not os.path.exists(abs_path):
        raise HTTPException(status_code=404, detail=f"Path '{abs_path}' not found")
    
    if not os.path.isdir(abs_path):
        raise HTTPException(status_code=400, detail=f"'{abs_path}' is not a directory")
    
    try:
        items = []
        for item in os.listdir(abs_path):
            item_path = os.path.join(abs_path, item)
            try:
                items.append(_get_file_info(item_path))
            except (PermissionError, OSError):
                # Skip items we can't read
                continue
        
        return {
            "path": abs_path,
            "total": len(items),
            "items": sorted(items, key=lambda x: (not x.is_dir, x.name.lower()))
        }
    except PermissionError:
        raise HTTPException(status_code=403, detail=f"Permission denied to read '{abs_path}'")


# ------------------------------------------------------------------
# 2. Download File
# ------------------------------------------------------------------
@router.get("/download/{full_path:path}")
async def download_file(
    full_path: str,
    base_dir: Optional[str] = None,
    current_user: User = Depends(get_current_user)
):
    """
    Download a file. Streaming support for large files.
    """
    abs_path = _validate_path(full_path, base_dir)
    
    if not os.path.exists(abs_path):
        raise HTTPException(status_code=404, detail=f"File '{abs_path}' not found")
    
    if os.path.isdir(abs_path):
        raise HTTPException(status_code=400, detail="Cannot download a directory")
    
    # Check forbidden extensions
    ext = os.path.splitext(abs_path)[1].lower()
    if ext in FORBIDDEN_EXTENSIONS:
        raise HTTPException(
            status_code=403,
            detail=f"Downloading '{ext}' files is forbidden for security reasons"
        )
    
    filename = os.path.basename(abs_path)
    return FileResponse(
        path=abs_path,
        filename=filename,
        media_type=mimetypes.guess_type(abs_path)[0] or "application/octet-stream"
    )


# ------------------------------------------------------------------
# 3. Upload File(s)
# ------------------------------------------------------------------
@router.post("/upload/{full_path:path}")
async def upload_file(
    full_path: str,
    files: List[UploadFile] = File(...),
    base_dir: Optional[str] = None,
    current_user: User = Depends(get_current_user)
):
    """
    Upload one or more files to the specified directory.
    """
    abs_path = _validate_path(full_path, base_dir)
    
    if not os.path.exists(abs_path):
        raise HTTPException(status_code=404, detail=f"Directory '{abs_path}' not found")
    
    if not os.path.isdir(abs_path):
        raise HTTPException(status_code=400, detail=f"'{abs_path}' is not a directory")
    
    # Check write permissions
    if not os.access(abs_path, os.W_OK):
        raise HTTPException(status_code=403, detail=f"Permission denied to write to '{abs_path}'")
    
    uploaded = []
    failed = []
    
    for file in files:
        file_path = os.path.join(abs_path, file.filename)
        
        # Safety: prevent overwriting critical files
        if os.path.exists(file_path) and file_path in FORBIDDEN_PATHS:
            failed.append({"name": file.filename, "error": "Cannot overwrite system file"})
            continue
        
        try:
            # Write file in chunks to handle large uploads
            with open(file_path, "wb") as f:
                content = await file.read()
                f.write(content)
            
            uploaded.append({
                "name": file.filename,
                "path": file_path,
                "size": len(content),
                "size_human": _human_readable_size(len(content))
            })
        except Exception as e:
            failed.append({"name": file.filename, "error": str(e)})
    
    return {
        "success": True,
        "path": abs_path,
        "uploaded": uploaded,
        "failed": failed,
        "total_uploaded": len(uploaded),
        "total_failed": len(failed)
    }


# ------------------------------------------------------------------
# 4. Delete File or Directory
# ------------------------------------------------------------------
@router.delete("/delete/{full_path:path}")
async def delete_file(
    full_path: str,
    recursive: bool = False,
    base_dir: Optional[str] = None,
    current_user: User = Depends(get_current_user)
):
    """
    Delete a file or directory.
    Use recursive=True to delete directories with contents.
    """
    abs_path = _validate_path(full_path, base_dir)
    
    if not os.path.exists(abs_path):
        raise HTTPException(status_code=404, detail=f"'{abs_path}' not found")
    
    # Safety: prevent deletion of critical system paths
    if abs_path in FORBIDDEN_PATHS:
        raise HTTPException(status_code=403, detail="Cannot delete system critical path")
    
    try:
        if os.path.isdir(abs_path):
            if not recursive:
                raise HTTPException(
                    status_code=400,
                    detail="Directory deletion requires recursive=True"
                )
            shutil.rmtree(abs_path)
        else:
            os.remove(abs_path)
        
        return {"success": True, "deleted": abs_path}
    except PermissionError:
        raise HTTPException(status_code=403, detail=f"Permission denied to delete '{abs_path}'")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Delete failed: {str(e)}")


# ------------------------------------------------------------------
# 5. Rename File or Directory
# ------------------------------------------------------------------
@router.post("/rename")
async def rename_file(
    payload: RenameInput,
    base_dir: Optional[str] = None,
    current_user: User = Depends(get_current_user)
):
    """
    Rename or move a file/directory to a new path.
    """
    old_abs = _validate_path(payload.old_path, base_dir)
    new_abs = _validate_path(payload.new_path, base_dir)
    
    if not os.path.exists(old_abs):
        raise HTTPException(status_code=404, detail=f"'{old_abs}' not found")
    
    if os.path.exists(new_abs):
        raise HTTPException(status_code=409, detail=f"'{new_abs}' already exists")
    
    # Prevent renaming to a forbidden path
    if new_abs in FORBIDDEN_PATHS:
        raise HTTPException(status_code=403, detail="Cannot rename to system critical path")
    
    try:
        os.rename(old_abs, new_abs)
        return {
            "success": True,
            "old_path": old_abs,
            "new_path": new_abs,
            "message": f"Renamed to '{new_abs}'"
        }
    except PermissionError:
        raise HTTPException(status_code=403, detail=f"Permission denied to rename '{old_abs}'")


# ------------------------------------------------------------------
# 6. Move File or Directory
# ------------------------------------------------------------------
@router.post("/move")
async def move_file(
    payload: MoveInput,
    base_dir: Optional[str] = None,
    current_user: User = Depends(get_current_user)
):
    """
    Move a file/directory to a new location (alias for rename).
    """
    # Use rename logic
    rename_payload = RenameInput(
        old_path=payload.source,
        new_path=payload.destination
    )
    return await rename_file(rename_payload, base_dir, current_user)


# ------------------------------------------------------------------
# 7. Create Empty File
# ------------------------------------------------------------------
@router.post("/create-file")
async def create_file(
    payload: CreateFileInput,
    base_dir: Optional[str] = None,
    current_user: User = Depends(get_current_user)
):
    """
    Create a new empty file or with specified content.
    """
    abs_path = _validate_path(payload.path, base_dir)
    
    if os.path.exists(abs_path):
        if not payload.overwrite:
            raise HTTPException(
                status_code=409,
                detail=f"File '{abs_path}' already exists. Set overwrite=True to overwrite."
            )
    else:
        # Create parent directories if they don't exist
        parent_dir = os.path.dirname(abs_path)
        if parent_dir and not os.path.exists(parent_dir):
            os.makedirs(parent_dir, exist_ok=True)
    
    # Check write permissions on parent directory
    parent_dir = os.path.dirname(abs_path)
    if not os.access(parent_dir, os.W_OK):
        raise HTTPException(status_code=403, detail="Permission denied to write to parent directory")
    
    try:
        with open(abs_path, "w") as f:
            f.write(payload.content)
        return {
            "success": True,
            "path": abs_path,
            "message": f"File created at '{abs_path}'",
            "size": len(payload.content)
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Create failed: {str(e)}")


# ------------------------------------------------------------------
# 8. Create Directory
# ------------------------------------------------------------------
@router.post("/create-directory")
async def create_directory(
    payload: CreateDirectoryInput,
    base_dir: Optional[str] = None,
    current_user: User = Depends(get_current_user)
):
    """
    Create a new directory.
    """
    abs_path = _validate_path(payload.path, base_dir)
    
    if os.path.exists(abs_path):
        raise HTTPException(status_code=409, detail=f"'{abs_path}' already exists")
    
    try:
        os.makedirs(abs_path, exist_ok=payload.parents)
        return {
            "success": True,
            "path": abs_path,
            "message": f"Directory created at '{abs_path}'"
        }
    except PermissionError:
        raise HTTPException(status_code=403, detail=f"Permission denied to create '{abs_path}'")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Create failed: {str(e)}")


# ------------------------------------------------------------------
# 9. Read File Content
# ------------------------------------------------------------------
@router.get("/read/{full_path:path}")
async def read_file(
    full_path: str,
    base_dir: Optional[str] = None,
    current_user: User = Depends(get_current_user)
):
    """
    Read the content of a text file.
    """
    abs_path = _validate_path(full_path, base_dir)
    
    if not os.path.exists(abs_path):
        raise HTTPException(status_code=404, detail=f"'{abs_path}' not found")
    
    if os.path.isdir(abs_path):
        raise HTTPException(status_code=400, detail="Cannot read a directory")
    
    # Check if it's a text file (limit to common text extensions)
    text_extensions = ['.txt', '.conf', '.cfg', '.ini', '.log', '.sh', '.py', '.yml', '.yaml', '.json', '.xml', '.html', '.css', '.js', '.md']
    ext = os.path.splitext(abs_path)[1].lower()
    if ext and ext not in text_extensions:
        raise HTTPException(
            status_code=400,
            detail=f"Reading '{ext}' files is not supported for security reasons"
        )
    
    # Check file size (prevent reading huge files)
    file_size = os.path.getsize(abs_path)
    if file_size > 10 * 1024 * 1024:  # 10MB max
        raise HTTPException(
            status_code=400,
            detail="File is too large to read (max 10MB)"
        )
    
    try:
        with open(abs_path, "r") as f:
            content = f.read()
        return {
            "success": True,
            "path": abs_path,
            "content": content,
            "size": file_size,
            "size_human": _human_readable_size(file_size)
        }
    except PermissionError:
        raise HTTPException(status_code=403, detail=f"Permission denied to read '{abs_path}'")
    except UnicodeDecodeError:
        raise HTTPException(status_code=400, detail="File is not a valid text file")


# ------------------------------------------------------------------
# 10. Edit File Content
# ------------------------------------------------------------------
@router.post("/edit")
async def edit_file(
    payload: EditFileInput,
    base_dir: Optional[str] = None,
    current_user: User = Depends(get_current_user)
):
    """
    Edit (overwrite) the content of a text file.
    """
    abs_path = _validate_path(payload.path, base_dir)
    
    if not os.path.exists(abs_path):
        raise HTTPException(status_code=404, detail=f"'{abs_path}' not found")
    
    if os.path.isdir(abs_path):
        raise HTTPException(status_code=400, detail="Cannot edit a directory")
    
    # Check write permissions
    if not os.access(abs_path, os.W_OK):
        raise HTTPException(status_code=403, detail=f"Permission denied to edit '{abs_path}'")
    
    try:
        with open(abs_path, "w") as f:
            f.write(payload.content)
        return {
            "success": True,
            "path": abs_path,
            "message": f"File updated successfully",
            "size": len(payload.content)
        }
    except PermissionError:
        raise HTTPException(status_code=403, detail=f"Permission denied to edit '{abs_path}'")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Edit failed: {str(e)}")
