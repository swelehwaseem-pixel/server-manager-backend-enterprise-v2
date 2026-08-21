from fastapi import APIRouter, Depends, HTTPException, Query, status
from typing import Optional
import httpx
from app.auth import get_current_user
from app.database import User

router = APIRouter(prefix="/api/v1/logs", tags=["Logs Engine"])

# Internal Loki address inside Docker network
LOKI_URL = "http://loki:3100"

@router.get("/query")
async def query_loki_logs(
    query: str = Query(..., description="LogQL query, e.g., '{job=\\\"systemd-logs\\\"}'"),
    start: Optional[int] = Query(None, description="Unix timestamp (nanoseconds) start"),
    end: Optional[int] = Query(None, description="Unix timestamp (nanoseconds) end"),
    limit: int = Query(100, ge=1, le=10000),
    direction: str = Query("backward", regex="^(forward|backward)$"),
    current_user: User = Depends(get_current_user)
):
    """
    Proxy endpoint to query Grafana Loki logs.
    Returns raw JSON from Loki's query_range API.
    """
    params = {
        "query": query,
        "limit": limit,
        "direction": direction,
    }
    if start:
        params["start"] = start
    if end:
        params["end"] = end

    async with httpx.AsyncClient(timeout=30.0) as client:
        try:
            response = await client.get(
                f"{LOKI_URL}/loki/api/v1/query_range",
                params=params
            )
            response.raise_for_status()
            return response.json()
        except httpx.HTTPStatusError as e:
            raise HTTPException(
                status_code=e.response.status_code,
                detail=f"Loki returned an error: {e.response.text}"
            )
        except httpx.RequestError as e:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail=f"Could not reach Loki service: {str(e)}"
            )
