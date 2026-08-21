import asyncio
from fastapi import APIRouter, Depends, HTTPException, WebSocket, WebSocketDisconnect, Query, status
from jose import jwt, JWTError
from app.config import settings
from app.auth import get_current_user
from app.database import User
from app.schemas.services import ServiceControlInput, ServiceControlResponse

router = APIRouter(prefix="/api/v1/services", tags=["Systemd Core"])

ALLOWED_SERVICES = {"nginx", "docker", "postgresql", "redis-server", "mssql-server", "oracle-xe-21c"}

@router.post("/control", response_model=ServiceControlResponse)
async def control_service(payload: ServiceControlInput, current_user: User = Depends(get_current_user)):
    if payload.service_name not in ALLOWED_SERVICES:
        raise HTTPException(status_code=403, detail="Target system service execution is restricted.")

    process = await asyncio.create_subprocess_exec(
        "systemctl", payload.action, payload.service_name,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    stdout, stderr = await process.communicate()

    if process.returncode != 0:
        raise HTTPException(status_code=500, detail=f"OS Command Error: {stderr.decode().strip()}")

    return {
        "service": payload.service_name,
        "action": payload.action,
        "execution_state": "Completed",
        "system_output": stdout.decode().strip() or f"Service operation {payload.action} executed."
    }

@router.websocket("/stream/{service_name}")
async def stream_live_service_logs(websocket: WebSocket, service_name: str, token: str = Query(...)):
    await websocket.accept()
    try:
        payload = jwt.decode(token, settings.secret_key, algorithms=[settings.algorithm])
        if payload.get("sub") is None or service_name not in ALLOWED_SERVICES:
            await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
            return
    except JWTError:
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
        return

    process = await asyncio.create_subprocess_exec(
        "journalctl", "-u", service_name, "-f", "-n", "10", "--no-pager",
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )

    try:
        while True:
            line = await process.stdout.readline()
            if not line:
                break
            decoded_line = line.decode().strip()
            if decoded_line:
                await websocket.send_json({"service": service_name, "log_entry": decoded_line})
    except WebSocketDisconnect:
        pass
    finally:
        try:
            process.terminate()
            await process.wait()
        except ProcessLookupError:
            pass
