import asyncio
import os
import pty
import subprocess
import termios
import tty
import signal
from fastapi import APIRouter, WebSocket, WebSocketDisconnect, status
from jose import JWTError, jwt
from app.config import settings

router = APIRouter(prefix="/api/v1/linux", tags=["Linux OS Engine"])

@router.websocket("/terminal")
async def websocket_terminal(websocket: WebSocket):
    """
    Interactive WebSocket-based Linux terminal (TTY).
    Connect via: ws://localhost:8000/api/v1/linux/terminal?token=YOUR_JWT
    """
    # ----------------------------------------------------------
    # 1. Authenticate via JWT token in query string
    # ----------------------------------------------------------
    token = websocket.query_params.get("token")
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
    # 2. Accept the WebSocket connection
    # ----------------------------------------------------------
    await websocket.accept()

    # ----------------------------------------------------------
    # 3. Create a Pseudo-Terminal (PTY)
    # ----------------------------------------------------------
    master_fd, slave_fd = pty.openpty()

    # Set terminal to raw mode (pass control characters directly to the shell)
    old_settings = termios.tcgetattr(slave_fd)
    try:
        tty.setraw(slave_fd)
    except Exception:
        pass

    # ----------------------------------------------------------
    # 4. Spawn Bash process with the PTY
    # ----------------------------------------------------------
    process = await asyncio.create_subprocess_exec(
        "/bin/bash",
        "-i",  # Interactive mode
        stdin=slave_fd,
        stdout=slave_fd,
        stderr=slave_fd,
        preexec_fn=os.setsid,  # Create a new session for signal handling
    )

    # ----------------------------------------------------------
    # 5. Task: Read from PTY and send to WebSocket
    # ----------------------------------------------------------
    async def read_pty():
        loop = asyncio.get_running_loop()
        while True:
            try:
                # Non-blocking read from the PTY master
                data = await loop.run_in_executor(None, os.read, master_fd, 1024)
                if not data:
                    break
                # Send raw bytes to the WebSocket (text or binary)
                await websocket.send_bytes(data)
            except (OSError, WebSocketDisconnect):
                break

    # ----------------------------------------------------------
    # 6. Main loop: Receive keystrokes from WebSocket -> PTY
    # ----------------------------------------------------------
    try:
        read_task = asyncio.create_task(read_pty())

        while True:
            # Receive raw bytes from the client (browser terminal)
            data = await websocket.receive_bytes()
            # Write to the PTY master (injects keystrokes into bash)
            os.write(master_fd, data)

    except WebSocketDisconnect:
        pass
    except Exception as e:
        print(f"Terminal error: {e}")
    finally:
        # ----------------------------------------------------------
        # 7. Cleanup: Restore terminal settings and kill process
        # ----------------------------------------------------------
        # Restore old terminal settings
        try:
            termios.tcsetattr(slave_fd, termios.TCSADRAIN, old_settings)
        except Exception:
            pass

        # Close file descriptors
        os.close(master_fd)
        os.close(slave_fd)

        # Terminate the bash process
        try:
            process.terminate()
            await asyncio.sleep(0.5)
            process.kill()
        except ProcessLookupError:
            pass

        # Cancel the read task
        if read_task and not read_task.done():
            read_task.cancel()
            try:
                await read_task
            except asyncio.CancelledError:
                pass
