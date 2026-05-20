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
import urllib.parse
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal, Optional

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


def _get_camera_client_ip() -> str:
    """Return the ground-station address reachable from the rover camera service."""
    rover_camera_service_url = config.rover_camera_service_urls[0] if config.rover_camera_service_urls else ""
    rover_url = urllib.parse.urlparse(rover_camera_service_url)
    rover_host = rover_url.hostname
    rover_port = rover_url.port or 8765
    if rover_host:
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
                s.connect((rover_host, rover_port))
                ip = s.getsockname()[0]
                if ip and not ip.startswith("127."):
                    return ip
        except OSError as e:
            bridge.add_log("WARN", f"Could not resolve route to rover camera service: {e}", "camera")
    return _get_local_ips()[0]


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


class DashboardTimer:
    def __init__(self):
        self.elapsed_ms = 0
        self.running = False
        self.started_at = 0.0
        self.laps: list[dict] = []
        self.updated_at = time.time()

    def _elapsed_ms(self) -> int:
        if not self.running:
            return int(self.elapsed_ms)
        return int(self.elapsed_ms + ((time.time() - self.started_at) * 1000))

    def snapshot(self) -> dict:
        return {
            "elapsed_ms": self._elapsed_ms(),
            "running": self.running,
            "laps": self.laps[-10:],
            "updated_at": self.updated_at,
            "server_ts": time.time(),
        }

    def start(self):
        if not self.running:
            self.started_at = time.time()
            self.running = True
            self.updated_at = time.time()

    def pause(self):
        if self.running:
            self.elapsed_ms = self._elapsed_ms()
            self.running = False
            self.updated_at = time.time()

    def stop(self):
        self.elapsed_ms = self._elapsed_ms()
        self.running = False
        self.updated_at = time.time()

    def reset(self):
        self.elapsed_ms = 0
        self.running = False
        self.started_at = time.time()
        self.laps = []
        self.updated_at = time.time()

    def lap(self):
        lap = {
            "index": len(self.laps) + 1,
            "elapsed_ms": self._elapsed_ms(),
            "ts": time.time(),
        }
        self.laps.append(lap)
        self.updated_at = time.time()
        return lap


dashboard_timer = DashboardTimer()


# ---------------------------------------------------------------------------
# Network radio polling
# ---------------------------------------------------------------------------

NETWORK_RADIOS = {
    "m2": {"label": "M2 Rocket", "band": "2.4 GHz"},
    "m900": {"label": "M900 Rocket", "band": "900 MHz"},
}

SNMP_OIDS = {
    "signal_dbm": [
        ".1.3.6.1.4.1.41112.1.4.1.1.3.1",   # UBNT-AirMAX-MIB, common airOS builds
        ".1.3.6.1.4.1.41112.1.4.5.1.5.1",
    ],
    "noise_floor_dbm": [
        ".1.3.6.1.4.1.41112.1.4.1.1.4.1",
        ".1.3.6.1.4.1.41112.1.4.5.1.6.1",
    ],
    "ccq_pct": [
        ".1.3.6.1.4.1.41112.1.4.1.1.7.1",
        ".1.3.6.1.4.1.41112.1.4.5.1.10.1",
    ],
    "tx_errors": [
        ".1.3.6.1.4.1.41112.1.4.1.1.13.1",
    ],
    "rx_errors": [
        ".1.3.6.1.4.1.41112.1.4.1.1.14.1",
    ],
}


class NetworkConfig(BaseModel):
    m2_ip: str = ""
    m900_ip: str = ""


def _network_config_path() -> Path:
    path = Path(config.network_config_path)
    if not path.is_absolute():
        path = Path(__file__).parent.parent / path
    return path


def _load_network_config() -> NetworkConfig:
    path = _network_config_path()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return NetworkConfig()
    return NetworkConfig(
        m2_ip=str(raw.get("m2_ip") or "").strip(),
        m900_ip=str(raw.get("m900_ip") or "").strip(),
    )


def _save_network_config(req: NetworkConfig) -> NetworkConfig:
    clean = NetworkConfig(m2_ip=req.m2_ip.strip(), m900_ip=req.m900_ip.strip())
    path = _network_config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(clean.dict(), indent=2) + "\n", encoding="utf-8")
    return clean


def _safe_host(host: str) -> str:
    host = host.strip()
    if not host:
        return ""
    if len(host) > 253 or any(c.isspace() for c in host):
        raise HTTPException(status_code=400, detail="Rocket IP/host contains invalid characters")
    return host


async def _ping_latency_ms(host: str, timeout_s: float) -> tuple[Optional[float], Optional[str]]:
    if not host:
        return None, None
    cmd = ["ping", "-c", "1", "-W", str(max(1, int(timeout_s * 1000))), host]
    started = time.perf_counter()
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout_s + 0.5)
    except (OSError, asyncio.TimeoutError) as e:
        return None, str(e)
    if proc.returncode != 0:
        msg = (stderr or stdout).decode("utf-8", "ignore").strip().splitlines()
        return None, msg[-1] if msg else "ping failed"
    text = stdout.decode("utf-8", "ignore")
    marker = "time="
    if marker in text:
        part = text.split(marker, 1)[1].split(" ", 1)[0]
        try:
            return round(float(part), 1), None
        except ValueError:
            pass
    return round((time.perf_counter() - started) * 1000.0, 1), None


async def _snmp_get(host: str, oid: str, timeout_s: float) -> Optional[float]:
    cmd = [
        "snmpget",
        "-v2c",
        "-c",
        config.rocket_snmp_community,
        "-Oqv",
        "-t",
        str(max(1, int(timeout_s))),
        "-r",
        "0",
        host,
        oid,
    ]
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout_s + 0.5)
    except (OSError, asyncio.TimeoutError):
        return None
    if proc.returncode != 0:
        return None
    raw = stdout.decode("utf-8", "ignore").strip().strip('"')
    try:
        return float(raw.split()[0])
    except (ValueError, IndexError):
        return None


async def _snmp_first(host: str, oids: list[str], timeout_s: float) -> Optional[float]:
    for oid in oids:
        value = await _snmp_get(host, oid, timeout_s)
        if value is not None:
            return value
    return None


async def _rocket_status(key: str, host: str) -> dict:
    meta = NETWORK_RADIOS[key]
    host = _safe_host(host)
    status = {
        "key": key,
        "label": meta["label"],
        "band": meta["band"],
        "ip": host,
        "configured": bool(host),
        "reachable": False,
        "latency_ms": None,
        "signal_dbm": None,
        "noise_floor_dbm": None,
        "ccq_pct": None,
        "tx_errors": None,
        "rx_errors": None,
        "link_quality_pct": None,
        "status": "unconfigured" if not host else "offline",
        "error": None,
        "updated_at": time.time(),
    }
    if not host:
        return status

    timeout_s = max(0.4, float(config.rocket_poll_timeout_s))
    latency, ping_error = await _ping_latency_ms(host, timeout_s)
    status["latency_ms"] = latency
    status["reachable"] = latency is not None
    status["error"] = ping_error

    snmp_values: dict[str, Optional[float]] = {}
    if shutil_which("snmpget"):
        fields = list(SNMP_OIDS.keys())
        values = await asyncio.gather(*[
            _snmp_first(host, SNMP_OIDS[field], timeout_s)
            for field in fields
        ])
        snmp_values = dict(zip(fields, values))

    for field, value in snmp_values.items():
        if value is not None:
            status[field] = round(value, 1) if field.endswith(("_dbm", "_mbps", "_pct")) else int(value)

    ccq = as_finite_number(status["ccq_pct"])
    if ccq is not None:
        status["link_quality_pct"] = max(0, min(100, round(ccq)))
    elif status["reachable"]:
        latency_score = 100 if latency is None else max(0, min(100, 100 - max(0, latency - 10) * 1.5))
        status["link_quality_pct"] = round(latency_score)

    quality = as_finite_number(status["link_quality_pct"])
    if not status["reachable"]:
        status["status"] = "offline"
    elif quality is None:
        status["status"] = "online"
    elif quality >= 70:
        status["status"] = "good"
    elif quality >= 45:
        status["status"] = "degraded"
    else:
        status["status"] = "poor"
    return status


def as_finite_number(value) -> Optional[float]:
    try:
        n = float(value)
    except (TypeError, ValueError):
        return None
    return n if n == n and abs(n) != float("inf") else None


async def _network_status() -> dict:
    cfg = _load_network_config()
    m2, m900 = await asyncio.gather(
        _rocket_status("m2", cfg.m2_ip),
        _rocket_status("m900", cfg.m900_ip),
    )
    return {
        "config": cfg.dict(),
        "rockets": {"m2": m2, "m900": m900},
        "snmp": {
            "available": bool(shutil_which("snmpget")),
            "community": config.rocket_snmp_community,
        },
        "ts": time.time(),
    }


def shutil_which(cmd: str) -> Optional[str]:
    from shutil import which
    return which(cmd)

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
                "dashboard_timer": dashboard_timer.snapshot(),
                "ts": time.time(),
            }
            await telemetry_mgr.broadcast(payload)
        except Exception as e:
            logger.error(f"Telemetry broadcast error: {e}")
        await asyncio.sleep(1.0)


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
    bridge.add_log(
        "INFO",
        f"Ground station camera return address is {_get_camera_client_ip()} for {', '.join(config.rover_camera_service_urls)}",
        "camera",
    )
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
        "camera_client_ip": _get_camera_client_ip(),
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

async def _mjpeg_generator(camera_id: str):
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
    camera_id: str,
    _token: str = Depends(require_auth),
):
    if not any(str(c.get("id")) == str(camera_id) for c in bridge.get_cameras()):
        raise HTTPException(status_code=404, detail="Camera not found")
    return StreamingResponse(
        _mjpeg_generator(str(camera_id)),
        media_type="multipart/x-mixed-replace; boundary=frame",
    )


@app.get("/api/cameras")
async def list_cameras(
    refresh: bool = False,
    _token: str = Depends(require_auth),
):
    try:
        cameras = bridge.refresh_cameras() if refresh else bridge.get_cameras()
    except RuntimeError as e:
        raise HTTPException(status_code=503, detail=str(e))
    return {"cameras": cameras}


@app.post("/api/cameras/refresh")
async def refresh_cameras(_token: str = Depends(require_auth)):
    try:
        cameras = bridge.refresh_cameras()
    except RuntimeError as e:
        raise HTTPException(status_code=503, detail=str(e))
    return {"cameras": cameras}


@app.post("/api/camera/{camera_id}/start")
async def start_camera(camera_id: str, _token: str = Depends(require_auth)):
    try:
        camera = bridge.start_camera(camera_id, _get_camera_client_ip())
    except RuntimeError as e:
        raise HTTPException(status_code=503, detail=str(e))
    return {"camera": camera}


@app.post("/api/camera/{camera_id}/stop")
async def stop_camera(camera_id: str, _token: str = Depends(require_auth)):
    return {"camera": bridge.stop_camera(camera_id)}


@app.post("/api/camera/{camera_id}/still")
async def capture_camera_still(camera_id: str, _token: str = Depends(require_auth)):
    try:
        return bridge.capture_camera_still(camera_id)
    except RuntimeError as e:
        raise HTTPException(status_code=503, detail=str(e))


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
    try:
        bridge.send_estop(req.active)
    except RuntimeError as e:
        raise HTTPException(status_code=503, detail=str(e))
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


@app.get("/api/network/config")
async def get_network_config(_t: str = Depends(require_auth)):
    return {"config": _load_network_config().dict()}


@app.put("/api/network/config")
async def update_network_config(req: NetworkConfig, _t: str = Depends(require_auth)):
    clean = NetworkConfig(m2_ip=_safe_host(req.m2_ip), m900_ip=_safe_host(req.m900_ip))
    saved = _save_network_config(clean)
    bridge.add_log("INFO", "Network Rocket IPs updated", "network")
    return {"ok": True, "config": saved.dict()}


@app.get("/api/network/status")
async def get_network_status(_t: str = Depends(require_auth)):
    return await _network_status()


@app.get("/api/system")
async def system_overview(_t: str = Depends(require_auth)):
    system = bridge.get_system()
    ips = _get_local_ips()
    system["ground_station_ip"] = ips[0] if ips else "127.0.0.1"
    system["ground_station_ips"] = ips
    system["current_ip"] = system.get("rover_current_ip") or "--"
    system["ips"] = system.get("rover_ips") or []
    system["urls"] = [f"http://{ip}:{config.port}" for ip in ips]
    system["subsystems"] = bridge.get_subsystems()
    system["comms"] = bridge.get_comms()
    system["payload_arduino"] = bridge.get_payload_arduino()
    system["life_analysis"] = bridge.get_life_analysis()
    system["led_controller"] = bridge.get_led_controller()
    system["motor_telemetry"] = bridge.get_motor_telemetry()
    return system


class DashboardTimerAction(BaseModel):
    action: Literal["start", "pause", "resume", "stop", "reset", "lap"]


@app.get("/api/dashboard/timer")
async def get_dashboard_timer(_t: str = Depends(require_auth)):
    return {"timer": dashboard_timer.snapshot()}


@app.post("/api/dashboard/timer")
async def update_dashboard_timer(req: DashboardTimerAction, _t: str = Depends(require_auth)):
    if req.action in ("start", "resume"):
        dashboard_timer.start()
    elif req.action == "pause":
        dashboard_timer.pause()
    elif req.action == "stop":
        dashboard_timer.stop()
    elif req.action == "reset":
        dashboard_timer.reset()
    elif req.action == "lap":
        dashboard_timer.lap()
    return {"ok": True, "timer": dashboard_timer.snapshot()}


@app.get("/api/jetson/temperature")
@app.get("/api/jetson/temperatures")
async def jetson_temperatures(_t: str = Depends(require_auth)):
    return bridge.get_jetson_temperatures()


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
    try:
        storage = bridge.get_rover_storage()
    except RuntimeError as e:
        raise HTTPException(status_code=503, detail=str(e))
    if not storage.get("ok"):
        return JSONResponse(
            status_code=503,
            content={"ok": False, "error": "no_storage", "detail": "No external storage device detected."},
        )
    return storage


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
    try:
        bridge.send_elevator(steps)
    except RuntimeError as e:
        raise HTTPException(status_code=503, detail=str(e))
    return {"ok": True, "direction": cmd.direction, "steps": abs(steps)}


class CarouselCommand(BaseModel):
    direction: str = Field(..., pattern="^(cw|ccw)$")
    steps: int = Field(..., gt=0, le=10000)


@app.post("/api/payload/carousel")
async def payload_carousel(cmd: CarouselCommand, _t: str = Depends(require_auth)):
    if not bridge.is_payload_connected():
        raise HTTPException(status_code=503, detail="Payload subsystem not connected")
    steps = cmd.steps if cmd.direction == "cw" else -cmd.steps
    try:
        bridge.send_carousel(steps)
    except RuntimeError as e:
        raise HTTPException(status_code=503, detail=str(e))
    return {"ok": True, "direction": cmd.direction, "steps": abs(steps)}


class AugerCommand(BaseModel):
    speed: float = Field(..., ge=0, le=100)
    enabled: bool


@app.post("/api/payload/auger")
async def payload_auger(cmd: AugerCommand, _t: str = Depends(require_auth)):
    if not bridge.is_payload_connected():
        raise HTTPException(status_code=503, detail="Payload subsystem not connected")
    try:
        bridge.send_auger(cmd.speed, cmd.enabled)
    except RuntimeError as e:
        raise HTTPException(status_code=503, detail=str(e))
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
    except RuntimeError as e:
        raise HTTPException(status_code=503, detail=str(e))
    return {"ok": True, "group": req.group, "device_id": req.device_id}


# ---------------------------------------------------------------------------
# Static files (served last — login page is public, rest of app gated by JS)
# ---------------------------------------------------------------------------

app.mount("/", StaticFiles(directory=str(STATIC_DIR), html=True), name="static")
