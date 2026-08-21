import json
import os
from typing import List, Optional
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from app.auth import get_current_user
from app.database import User

router = APIRouter(prefix="/api/v1/prometheus", tags=["Prometheus Management"])

# Path to the targets file shared with Prometheus via Docker volume
TARGETS_FILE_PATH = os.getenv("PROMETHEUS_TARGETS_FILE", "/app/targets/targets.json")

class ScrapeTarget(BaseModel):
    targets: List[str] = Field(..., description="List of host:port targets")
    labels: Optional[dict] = Field(None, description="Optional labels for the target")

class TargetUpdateRequest(BaseModel):
    job_name: str = Field(..., min_length=1, max_length=50)
    targets: List[ScrapeTarget]

def _read_targets_file() -> dict:
    """Read the current targets file. Returns empty dict if not exists."""
    if os.path.exists(TARGETS_FILE_PATH):
        with open(TARGETS_FILE_PATH, "r") as f:
            return json.load(f)
    return {}

def _write_targets_file(data: dict):
    """Write the targets file atomically."""
    os.makedirs(os.path.dirname(TARGETS_FILE_PATH), exist_ok=True)
    with open(TARGETS_FILE_PATH, "w") as f:
        json.dump(data, f, indent=2)

@router.get("/targets", response_model=dict)
async def list_all_targets(current_user: User = Depends(get_current_user)):
    """
    List all dynamically configured scrape targets.
    """
    return _read_targets_file()

@router.post("/targets", status_code=status.HTTP_201_CREATED)
async def update_or_create_target(
    payload: TargetUpdateRequest,
    current_user: User = Depends(get_current_user)
):
    """
    Add or completely overwrite a job's target list.
    If the job already exists, it is replaced.
    """
    current_data = _read_targets_file()
    
    # Convert payload to Prometheus file_sd format:
    # [{ "targets": ["host:port"], "labels": {"env": "prod"} }]
    formatted_targets = []
    for t in payload.targets:
        entry = {"targets": t.targets}
        if t.labels:
            entry["labels"] = t.labels
        formatted_targets.append(entry)
    
    current_data[payload.job_name] = formatted_targets
    _write_targets_file(current_data)
    
    return {
        "status": "updated",
        "job": payload.job_name,
        "targets_count": len(formatted_targets)
    }

@router.delete("/targets/{job_name}")
async def delete_target_job(
    job_name: str,
    current_user: User = Depends(get_current_user)
):
    """
    Remove an entire job's target configuration.
    """
    current_data = _read_targets_file()
    if job_name not in current_data:
        raise HTTPException(status_code=404, detail=f"Job '{job_name}' not found")
    
    del current_data[job_name]
    _write_targets_file(current_data)
    return {"status": "deleted", "job": job_name}
