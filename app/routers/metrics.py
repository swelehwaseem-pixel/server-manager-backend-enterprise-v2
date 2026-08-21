import asyncio
from fastapi import APIRouter, Depends, WebSocket, WebSocketDisconnect, Query, status
from jose import jwt, JWTError
import psutil
from app.config import settings
from app.auth import get_current_user
from app.database import User

router = APIRouter(prefix="/api/v1/metrics", tags=["Metrics Core"])

@router.get("/snapshot")
async def get_system_snapshot(current_user: User = Depends(get_current_user)):
    mem = psutil.virtual_memory()
    disk = psutil.disk_usage("/")
    return {
        "ram": {
            "total_gb": round(mem.total / (1024**3), 2),
            "used_gb": round(mem.used / (1024**3), 2),
            "percent": mem.percent
        },
        "disk": {
            "total_gb": round(disk.total / (1024**3), 2),
            "used_gb": round(disk.used / (1024**3), 2),
            "percent": disk.percent
        },
        "operator": current_user.username
    }

@router.websocket("/live")
async def stream_live_metrics(websocket: WebSocket, token: str = Query(...)):
    await websocket.accept()
    try:
        payload = jwt.decode(token, settings.secret_key, algorithms=[settings.algorithm])
        if payload.get("sub") is None:
            await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
            return
    except JWTError:
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
        return

    try:
        while True:
            cpu_per_core = psutil.cpu_percent(interval=None, percpu=True)
            net_io = psutil.net_io_counters()
            await websocket.send_json({
                "cpu_cores": cpu_per_core,
                "network": {"bytes_sent": net_io.bytes_sent, "bytes_recv": net_io.bytes_recv}
            })
            await asyncio.sleep(1)
    except WebSocketDisconnect:
        pass
