"""
RoSE Ground Station — FastAPI server
Provides MJPEG camera streams, WebSocket telemetry/log feeds, and REST control APIs.
All routes (except /api/auth and the static login page) require a valid bearer token.
"""
import asyncio
import json
import logging
import socket
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional

from fastapi import (
    Depends, FastAPI, HTTPException, Query, Security, WebSocket, WebSocketDisconnect,
)
from fastapi.responses import StreamingResponse, JSONResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from .auth import create_token, validate_token, verify_password
from .config import config
from .ros2_bridge import bridge

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
)
logger = logging.getLogger("api.main")

STATIC_DIR = Path(__file__).parent.parent / "static"

# ---------------------------------------------------------------------------
# Auth dependency
# ---------------------------------------------------------------------------
_bearer = HTTPBearer(auto_error=False)


async def require_auth(
    creds: Optional[HTTPAuthorizationCredentials] = Security(_bearer),
    token: Optional[str] = Query(None, alias="token"),
) -> str:
    """Accept token via Authorization: Bearer header OR ?token= query param."""
    t = (creds.credentials if creds else None) or token
    if not validate_token(t):
        raise HTTPException(status_code=401, detail="Invalid or missing token")
    return t  # type: ignore[return-value]


async def ws_auth(token: Optional[str] = Query(None)) -> str:
    """WebSocket-specific auth — token must come as query param."""
    if not validate_token(token):
        raise HTTPException(status_code=403, detail="Invalid or missing token")
    return token  # type: ignore[return-value]


# ---------------------------------------------------------------------------
# Server info helpers
# ---------------------------------------------------------------------------

def _get_local_ips() -> list[str]:
    ips: list[str] = []
    # Primary interface (connects out to internet routing)
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        primary = s.getsockname()[0]
        s.close()
        if primary and not primary.startswith("127."):
            ips.append(primary)
    except Exception:
        pass

    # All addresses bound to the hostname
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ip = info[4][0]
            if not ip.startswith("127.") and ip not in ips:
                ips.append(ip)
    except Exception:
        pass

    return ips or ["127.0.0.1"]


# ---------------------------------------------------------------------------
# WebSocket manager
# ---------------------------------------------------------------------------

class ConnectionManager:
    def __init__(self):
        self._active: set[WebSocket] = set()
        self._lock = asyncio.Lock()

    async def connect(self, ws: WebSocket):
        await ws.accept()
        async with self._lock:
            self._active.add(ws)

    async def disconnect(self, ws: WebSocket):
        async with self._lock:
            self._active.discard(ws)

    async def broadcast(self, data: dict):
        msg = json.dumps(data)
        dead: list[WebSocket] = []
        async with self._lock:
            targets = list(self._active)
        for ws in targets:
            try:
                await ws.send_text(msg)
            except Exception:
                dead.append(ws)
        for ws in dead:
            await self.disconnect(ws)


telemetry_mgr = ConnectionManager()
log_mgr = ConnectionManager()

# ---------------------------------------------------------------------------
# Background broadcast tasks
# ---------------------------------------------------------------------------

async def _telemetry_broadcast():
    while True:
        try:
            payload = {
                "type": "telemetry",
                "telemetry": bridge.get_telemetry(),
                "gnss": bridge.get_gnss(),
                "payload_connected": bridge.is_payload_connected(),
                "arm_connected": bridge.is_arm_connected(),
                "drive_connected": bridge.is_drive_connected(),
                "subsystems": bridge.get_subsystems(),
                "comms": bridge.get_comms(),
                "payload_arduino": bridge.get_payload_arduino(),
                "life_analysis": bridge.get_life_analysis(),
                "led_controller": bridge.get_led_controller(),
                "motor_telemetry": bridge.get_motor_telemetry(),
                "ts": time.time(),
            }
            await telemetry_mgr.broadcast(payload)
        except Exception as e:
            logger.error(f"Telemetry broadcast error: {e}")
        await asyncio.sleep(0.1)


_log_cursor: int = 0


async def _log_broadcast():
    global _log_cursor
    while True:
        try:
            new_entries = bridge.get_logs_since(_log_cursor)
            if new_entries:
                _log_cursor = new_entries[-1]["id"]
                await log_mgr.broadcast({"type": "logs", "entries": new_entries})
        except Exception as e:
            logger.error(f"Log broadcast error: {e}")
        await asyncio.sleep(0.2)


# ---------------------------------------------------------------------------
# Lifespan
# ---------------------------------------------------------------------------

@asynccontextmanager
async def lifespan(app: FastAPI):
    bridge.add_log("INFO", "API server started", "api")
    ips = _get_local_ips()
    for ip in ips:
        bridge.add_log("INFO", f"Listening on http://{ip}:{config.port}", "api")
    t1 = asyncio.create_task(_telemetry_broadcast())
    t2 = asyncio.create_task(_log_broadcast())
    yield
    t1.cancel()
    t2.cancel()


# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------

app = FastAPI(title="RoSE Ground Station API", version="1.0.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# ---------------------------------------------------------------------------
# Auth — public (no token required)
# ---------------------------------------------------------------------------

class LoginRequest(BaseModel):
    password: str


@app.post("/api/auth", summary="Authenticate with password, receive session token")
async def login(req: LoginRequest):
    if not verify_password(req.password):
        bridge.add_log("WARN", "Failed login attempt", "auth")
        raise HTTPException(status_code=401, detail="Incorrect password")
    token = create_token()
    bridge.add_log("INFO", "New session authenticated", "auth")
    return {"token": token}


# ---------------------------------------------------------------------------
# Info — public (needed before auth to display connection URL on login screen)
# ---------------------------------------------------------------------------

@app.get("/api/info", summary="Server network info")
async def server_info():
    ips = _get_local_ips()
    return {
        "ips": ips,
        "port": config.port,
        "hostname": socket.gethostname(),
        "urls": [f"http://{ip}:{config.port}" for ip in ips],
    }


# ---------------------------------------------------------------------------
# Health — public (used by frontend to verify token is still valid)
# ---------------------------------------------------------------------------

@app.get("/api/health")
async def health(token: str = Depends(require_auth)):
    return {"status": "ok", "ts": time.time()}


# ---------------------------------------------------------------------------
# Camera MJPEG streams  (token via ?token= query param for <img> src)
# ---------------------------------------------------------------------------

async def _mjpeg_generator(camera_id: int):
    interval = 1.0 / config.camera_fps
    while True:
        frame = bridge.get_camera_frame(camera_id)
        if frame:
            yield (
                b"--frame\r\n"
                b"Content-Type: image/jpeg\r\n\r\n"
                + frame
                + b"\r\n"
            )
        await asyncio.sleep(interval)


@app.get("/api/camera/{camera_id}")
async def camera_stream(
    camera_id: int,
    _token: str = Depends(require_auth),
):
    if camera_id not in range(4):
        raise HTTPException(status_code=404, detail="Camera not found")
    return StreamingResponse(
        _mjpeg_generator(camera_id),
        media_type="multipart/x-mixed-replace; boundary=frame",
    )


# ---------------------------------------------------------------------------
# WebSocket endpoints  (token via ?token= query param)
# ---------------------------------------------------------------------------

@app.websocket("/ws/telemetry")
async def ws_telemetry(ws: WebSocket, token: Optional[str] = Query(None)):
    if not validate_token(token):
        await ws.close(code=4001)
        return
    await telemetry_mgr.connect(ws)
    try:
        while True:
            await ws.receive_text()
    except WebSocketDisconnect:
        pass
    finally:
        await telemetry_mgr.disconnect(ws)


@app.websocket("/ws/logs")
async def ws_logs(ws: WebSocket, token: Optional[str] = Query(None)):
    if not validate_token(token):
        await ws.close(code=4001)
        return
    await log_mgr.connect(ws)
    backlog = bridge.get_logs_since(0)[-200:]
    if backlog:
        await ws.send_text(json.dumps({"type": "logs", "entries": backlog}))
    try:
        while True:
            await ws.receive_text()
    except WebSocketDisconnect:
        pass
    finally:
        await log_mgr.disconnect(ws)


# ---------------------------------------------------------------------------
# Protected REST API — all require valid token
# ---------------------------------------------------------------------------

class EStopRequest(BaseModel):
    active: bool = True


@app.post("/api/estop")
async def estop(req: EStopRequest, _t: str = Depends(require_auth)):
    bridge.send_estop(req.active)
    return {"ok": True, "active": req.active}


class WarningConfig(BaseModel):
    soc_warning: Optional[float] = None
    soc_critical: Optional[float] = None
    current_warning: Optional[float] = None
    current_critical: Optional[float] = None
    temperature_warning: Optional[float] = None
    temperature_critical: Optional[float] = None


@app.post("/api/warnings/config")
async def update_warnings(req: WarningConfig, _t: str = Depends(require_auth)):
    if req.soc_warning is not None:
        config.soc_warning.warning = req.soc_warning
    if req.soc_critical is not None:
        config.soc_warning.critical = req.soc_critical
    if req.current_warning is not None:
        config.current_warning.warning = req.current_warning
    if req.current_critical is not None:
        config.current_warning.critical = req.current_critical
    if req.temperature_warning is not None:
        config.temperature_warning.warning = req.temperature_warning
    if req.temperature_critical is not None:
        config.temperature_warning.critical = req.temperature_critical
    bridge.add_log("INFO", "Warning thresholds updated", "api")
    return {
        "ok": True,
        "soc":         {"warning": config.soc_warning.warning,         "critical": config.soc_warning.critical},
        "current":     {"warning": config.current_warning.warning,     "critical": config.current_warning.critical},
        "temperature": {"warning": config.temperature_warning.warning, "critical": config.temperature_warning.critical},
    }


@app.get("/api/warnings/config")
async def get_warnings(_t: str = Depends(require_auth)):
    return {
        "soc":         {"warning": config.soc_warning.warning,         "critical": config.soc_warning.critical},
        "current":     {"warning": config.current_warning.warning,     "critical": config.current_warning.critical},
        "temperature": {"warning": config.temperature_warning.warning, "critical": config.temperature_warning.critical},
    }


@app.get("/api/system")
async def system_overview(_t: str = Depends(require_auth)):
    system = bridge.get_system()
    system["subsystems"] = bridge.get_subsystems()
    system["comms"] = bridge.get_comms()
    system["payload_arduino"] = bridge.get_payload_arduino()
    system["life_analysis"] = bridge.get_life_analysis()
    system["led_controller"] = bridge.get_led_controller()
    system["motor_telemetry"] = bridge.get_motor_telemetry()
    return system


@app.get("/api/subsystems")
async def subsystem_overview(_t: str = Depends(require_auth)):
    return {
        "subsystems": bridge.get_subsystems(),
        "comms": bridge.get_comms(),
    }


@app.post("/api/camera360/capture")
async def capture_360_image(_t: str = Depends(require_auth)):
    try:
        return {"ok": True, "capture": bridge.capture_360_image()}
    except RuntimeError as e:
        raise HTTPException(status_code=503, detail=str(e))


@app.get("/api/files")
async def list_files(_t: str = Depends(require_auth)):
    return JSONResponse(
        status_code=503,
        content={"ok": False, "error": "no_storage", "detail": "No external storage device detected."},
    )


@app.get("/api/payload/status")
async def payload_status(_t: str = Depends(require_auth)):
    return {"connected": bridge.is_payload_connected(), "arduino": bridge.get_payload_arduino()}


class ElevatorCommand(BaseModel):
    direction: str = Field(..., pattern="^(up|down)$")
    steps: int = Field(..., gt=0, le=100000)


@app.post("/api/payload/elevator")
async def payload_elevator(cmd: ElevatorCommand, _t: str = Depends(require_auth)):
    if not bridge.is_payload_connected():
        raise HTTPException(status_code=503, detail="Payload subsystem not connected")
    steps = cmd.steps if cmd.direction == "up" else -cmd.steps
    bridge.send_elevator(steps)
    return {"ok": True, "direction": cmd.direction, "steps": abs(steps)}


class CarouselCommand(BaseModel):
    direction: str = Field(..., pattern="^(cw|ccw)$")
    steps: int = Field(..., gt=0, le=10000)


@app.post("/api/payload/carousel")
async def payload_carousel(cmd: CarouselCommand, _t: str = Depends(require_auth)):
    if not bridge.is_payload_connected():
        raise HTTPException(status_code=503, detail="Payload subsystem not connected")
    steps = cmd.steps if cmd.direction == "cw" else -cmd.steps
    bridge.send_carousel(steps)
    return {"ok": True, "direction": cmd.direction, "steps": abs(steps)}


class AugerCommand(BaseModel):
    speed: float = Field(..., ge=0, le=100)
    enabled: bool


@app.post("/api/payload/auger")
async def payload_auger(cmd: AugerCommand, _t: str = Depends(require_auth)):
    if not bridge.is_payload_connected():
        raise HTTPException(status_code=503, detail="Payload subsystem not connected")
    bridge.send_auger(cmd.speed, cmd.enabled)
    return {"ok": True, "speed": cmd.speed, "enabled": cmd.enabled}


class LifeRadarResult(BaseModel):
    labels: list[str] = Field(default_factory=list)
    values: list[float] = Field(default_factory=list)
    image_data_url: Optional[str] = None
    summary: Optional[str] = None


@app.post("/api/payload/life-analysis/start")
async def start_life_analysis(_t: str = Depends(require_auth)):
    if not bridge.is_payload_connected():
        raise HTTPException(status_code=503, detail="Payload subsystem not connected")
    bridge.start_life_analysis()
    return {"ok": True, "life_analysis": bridge.get_life_analysis()}


@app.post("/api/payload/life-analysis/radar")
async def life_analysis_radar(result: LifeRadarResult, _t: str = Depends(require_auth)):
    if result.labels and len(result.labels) != len(result.values):
        raise HTTPException(status_code=400, detail="labels and values must be the same length")
    radar = result.dict()
    bridge.set_life_analysis_radar(radar)
    return {"ok": True, "life_analysis": bridge.get_life_analysis()}


@app.get("/api/arm/status")
async def arm_status(_t: str = Depends(require_auth)):
    return {"connected": bridge.is_arm_connected()}


class ArmJointCommand(BaseModel):
    base: float = Field(0.0, ge=-180, le=180)
    shoulder: float = Field(0.0, ge=-180, le=180)
    elbow: float = Field(0.0, ge=-180, le=180)
    wrist: float = Field(0.0, ge=-180, le=180)


@app.post("/api/arm/joints")
async def arm_joints(cmd: ArmJointCommand, _t: str = Depends(require_auth)):
    raise HTTPException(status_code=410, detail="Manual arm commands are disabled in this ground station")


@app.get("/api/drive/status")
async def drive_status(_t: str = Depends(require_auth)):
    return {"connected": bridge.is_drive_connected()}


class DriveCommand(BaseModel):
    linear: float = Field(..., ge=-1.0, le=1.0)
    angular: float = Field(..., ge=-1.0, le=1.0)


@app.post("/api/drive/twist")
async def drive_twist(cmd: DriveCommand, _t: str = Depends(require_auth)):
    raise HTTPException(status_code=410, detail="Manual drive commands are disabled in this ground station")


class ClearMotorFaultsRequest(BaseModel):
    group: str
    device_id: int = Field(..., ge=0, le=255, description="SPARK CAN device ID, or 0 for all motors in group")


@app.get("/api/motors")
async def motor_telemetry(_t: str = Depends(require_auth)):
    return {"motor_telemetry": bridge.get_motor_telemetry()}


@app.post("/api/motors/clear_faults")
async def clear_motor_faults(req: ClearMotorFaultsRequest, _t: str = Depends(require_auth)):
    try:
        bridge.clear_motor_faults(req.group, req.device_id)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"ok": True, "group": req.group, "device_id": req.device_id}


# ---------------------------------------------------------------------------
# Static files (served last — login page is public, rest of app gated by JS)
# ---------------------------------------------------------------------------

app.mount("/", StaticFiles(directory=str(STATIC_DIR), html=True), name="static")
