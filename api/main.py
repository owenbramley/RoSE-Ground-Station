"""
RoSE Ground Station — FastAPI server
Provides MJPEG camera streams, WebSocket telemetry/log feeds, and REST control APIs.
The UI/API are passwordless; active UI clients are capped to protect the radio link.
"""
import asyncio
import fcntl
import json
import logging
import os
import socket
import struct
import threading
import time
import urllib.parse
import urllib.request
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal, Optional

from fastapi import (
    Depends, FastAPI, HTTPException, Query, Request, Security, WebSocket, WebSocketDisconnect,
)
from fastapi.responses import StreamingResponse, JSONResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from .auth import create_token
from .config import config
from .ros2_bridge import bridge

_camera_control_lock = threading.RLock()
MAX_UI_CLIENTS = 3
CLIENT_TIMEOUT_S = 20.0

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
)
logger = logging.getLogger("api.main")

STATIC_DIR = Path(__file__).parent.parent / "static"
DEFAULT_STORAGE_ROOTS = ("/media", "/mnt", "/run/media")
MAX_TEXT_FILES_PER_DEVICE = 200
MAX_TEXT_FILE_BYTES = 1024 * 1024

# ---------------------------------------------------------------------------
# Auth dependency
# ---------------------------------------------------------------------------
_bearer = HTTPBearer(auto_error=False)


async def require_auth(
    creds: Optional[HTTPAuthorizationCredentials] = Security(_bearer),
    token: Optional[str] = Query(None, alias="token"),
) -> str:
    """Passwordless mode keeps this dependency for route compatibility."""
    return (creds.credentials if creds else None) or token or ""


async def ws_auth(token: Optional[str] = Query(None)) -> str:
    """Passwordless mode keeps this dependency for route compatibility."""
    return token or ""


# ---------------------------------------------------------------------------
# Server info helpers
# ---------------------------------------------------------------------------

def _append_ip(ips: list[str], ip: str) -> None:
    if not ip or ip.startswith("127.") or ip == "0.0.0.0":
        return
    if ip not in ips:
        ips.append(ip)


def _interface_ipv4_addresses() -> list[str]:
    """Read LAN IPv4 addresses directly from interfaces.

    The Pi often has a rover/router LAN but no internet default route. Interface
    enumeration keeps /api/info useful in that field setup.
    """
    ips: list[str] = []
    try:
        interfaces = socket.if_nameindex()
    except OSError:
        return ips

    for _, name in interfaces:
        if name == "lo":
            continue
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
                ifreq = struct.pack("256s", name[:15].encode("utf-8"))
                res = fcntl.ioctl(s.fileno(), 0x8915, ifreq)  # SIOCGIFADDR
                _append_ip(ips, socket.inet_ntoa(res[20:24]))
        except OSError:
            continue
    return ips


def _get_local_ips() -> list[str]:
    ips: list[str] = _interface_ipv4_addresses()

    # All addresses bound to the hostname
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            _append_ip(ips, info[4][0])
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
# External storage helpers
# ---------------------------------------------------------------------------

def _configured_storage_roots() -> list[Path]:
    roots = [
        Path(root.strip())
        for root in os.environ.get("GS_STORAGE_ROOTS", "").split(",")
        if root.strip()
    ]
    roots.extend(Path(root) for root in DEFAULT_STORAGE_ROOTS)
    unique: list[Path] = []
    seen: set[str] = set()
    for root in roots:
        key = str(root)
        if key not in seen:
            unique.append(root)
            seen.add(key)
    return unique


def _mounts_from_proc() -> list[dict]:
    try:
        lines = Path("/proc/mounts").read_text(encoding="utf-8").splitlines()
    except OSError:
        return []

    ignored_fs = {
        "autofs", "binfmt_misc", "bpf", "cgroup", "cgroup2", "debugfs", "devpts", "devtmpfs",
        "efivarfs", "fusectl", "mqueue", "overlay", "proc", "pstore", "securityfs", "squashfs",
        "sysfs", "tmpfs", "tracefs",
    }
    mounts: list[dict] = []
    for line in lines:
        parts = line.split()
        if len(parts) < 3:
            continue
        source, mount_point, fstype = parts[:3]
        mount_point = mount_point.replace("\\040", " ")
        if fstype in ignored_fs:
            continue
        is_external_path = mount_point.startswith(("/media/", "/mnt/", "/run/media/", "/Volumes/"))
        is_removable_block = source.startswith(("/dev/sd", "/dev/mmcblk", "/dev/disk/by-id/usb", "/dev/disk/by-label"))
        if not (is_external_path or is_removable_block):
            continue
        path = Path(mount_point)
        if path.exists() and path.is_dir():
            mounts.append({"source": source, "mount": path, "fstype": fstype, "label": path.name})
    return mounts


def _storage_mounts() -> list[dict]:
    mounts: list[dict] = []
    seen: set[str] = set()

    for proc_mount in _mounts_from_proc():
        mount = proc_mount["mount"]
        try:
            key = str(mount.resolve())
        except OSError:
            continue
        if key in seen:
            continue
        mounts.append(proc_mount)
        seen.add(key)

    for root in _configured_storage_roots():
        if not root.is_dir():
            continue

        candidates: list[Path]
        if str(root) in DEFAULT_STORAGE_ROOTS:
            candidates = []
            try:
                children = sorted(root.iterdir(), key=lambda p: p.name.lower())
            except (OSError, PermissionError):
                continue
            for child in children:
                if not child.is_dir():
                    continue
                if root.name in ("media", "run") or child.name == os.environ.get("USER"):
                    try:
                        candidates.extend(
                            grandchild
                            for grandchild in sorted(child.iterdir(), key=lambda p: p.name.lower())
                            if grandchild.is_dir()
                        )
                    except (OSError, PermissionError):
                        continue
                else:
                    candidates.append(child)
        else:
            candidates = [root]

        for mount in candidates:
            try:
                key = str(mount.resolve())
            except OSError:
                continue
            if key in seen:
                continue
            try:
                next(mount.iterdir())
            except (StopIteration, OSError, PermissionError):
                continue
            mounts.append({"source": "", "mount": mount, "fstype": "", "label": mount.name})
            seen.add(key)

    return mounts


def _text_files_for_mount(mount: Path) -> list[dict]:
    files: list[dict] = []
    for current_root, dirnames, filenames in os.walk(mount):
        dirnames[:] = [
            name for name in dirnames
            if not name.startswith(".") and name not in {"System Volume Information", "$RECYCLE.BIN"}
        ]
        current_path = Path(current_root)
        for filename in filenames:
            if not filename.lower().endswith(".txt"):
                continue
            path = current_path / filename
            try:
                stat = path.stat()
                relative_path = path.relative_to(mount)
            except OSError:
                continue
            files.append({
                "name": filename,
                "path": str(path),
                "relative_path": str(relative_path),
                "size_bytes": stat.st_size,
                "modified_at": stat.st_mtime,
            })
            if len(files) >= MAX_TEXT_FILES_PER_DEVICE:
                return sorted(files, key=lambda item: item["modified_at"], reverse=True)
    return sorted(files, key=lambda item: item["modified_at"], reverse=True)


def _read_text_file(mount: Path, relative_path: str) -> tuple[str, bool]:
    path = (mount / relative_path).resolve()
    mount_root = mount.resolve()
    if mount_root not in path.parents and path != mount_root:
        raise ValueError("File path escapes storage device")

    size = path.stat().st_size
    with path.open("rb") as f:
        data = f.read(MAX_TEXT_FILE_BYTES)
    return data.decode("utf-8", errors="replace"), size > MAX_TEXT_FILE_BYTES


def _scan_local_storage(requested_path: Optional[str] = None) -> dict:
    devices: list[dict] = []
    selected: Optional[tuple[Path, dict]] = None

    for mount_info in _storage_mounts():
        mount = mount_info["mount"]
        text_files = _text_files_for_mount(mount)
        device = {
            "label": mount_info.get("label") or mount.name,
            "mount": str(mount),
            "source": mount_info.get("source") or "",
            "fstype": mount_info.get("fstype") or "",
            "text_files": text_files,
        }
        devices.append(device)
        try:
            requested = Path(requested_path).resolve() if requested_path else None
        except OSError:
            requested = None
        for text_file in text_files:
            if requested is not None:
                candidate = (mount / text_file["relative_path"]).resolve()
                if candidate == requested:
                    selected = (mount, text_file)
                    continue
            if requested is None and (selected is None or text_file["modified_at"] > selected[1]["modified_at"]):
                selected = (mount, text_file)

    if not devices:
        return {"ok": False, "error": "no_storage", "devices": []}

    response = {"ok": True, "devices": devices, "selected_file": None, "content": ""}
    if selected:
        mount, selected_file = selected
        try:
            content, truncated = _read_text_file(mount, selected_file["relative_path"])
            selected_file = dict(selected_file)
            selected_file["truncated"] = truncated
            response["selected_file"] = selected_file
            response["content"] = content
        except (OSError, UnicodeError, ValueError) as e:
            response["selected_file"] = dict(selected_file)
            response["error"] = f"Could not read selected text file: {e}"
    return response


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


class UIClientManager:
    def __init__(self, max_clients: int = MAX_UI_CLIENTS):
        self.max_clients = int(max_clients)
        self._clients: dict[str, dict] = {}
        self._lock = asyncio.Lock()

    def _prune_locked(self):
        now = time.time()
        expired = [
            client_id
            for client_id, info in self._clients.items()
            if now - float(info.get("last_seen", 0)) > CLIENT_TIMEOUT_S
        ]
        for client_id in expired:
            self._clients.pop(client_id, None)

    async def connect(self, client_id: str, request: Request) -> dict:
        client_id = client_id.strip()[:120]
        if not client_id:
            raise HTTPException(status_code=400, detail="Missing client_id")
        async with self._lock:
            self._prune_locked()
            is_existing = client_id in self._clients
            if not is_existing and len(self._clients) >= self.max_clients:
                raise HTTPException(
                    status_code=429,
                    detail=f"Ground station UI is full ({self.max_clients}/{self.max_clients} clients connected).",
                )
            self._clients[client_id] = {
                "client_id": client_id,
                "ip": request.client.host if request.client else "",
                "user_agent": request.headers.get("user-agent", "")[:180],
                "connected_at": self._clients.get(client_id, {}).get("connected_at", time.time()),
                "last_seen": time.time(),
            }
            return self.snapshot_locked()

    async def heartbeat(self, client_id: str) -> dict:
        async with self._lock:
            self._prune_locked()
            if client_id not in self._clients:
                raise HTTPException(status_code=409, detail="Client session is not registered")
            self._clients[client_id]["last_seen"] = time.time()
            return self.snapshot_locked()

    async def disconnect(self, client_id: str):
        async with self._lock:
            self._clients.pop(client_id, None)

    async def touch(self, client_id: str):
        async with self._lock:
            if client_id in self._clients:
                self._clients[client_id]["last_seen"] = time.time()

    async def snapshot(self) -> dict:
        async with self._lock:
            self._prune_locked()
            return self.snapshot_locked()

    def snapshot_locked(self) -> dict:
        return {
            "max_clients": self.max_clients,
            "active_count": len(self._clients),
            "clients": list(self._clients.values()),
        }


ui_clients = UIClientManager()


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
LEGACY_NETWORK_CONFIG_PATH = "data/network_config.json"

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
    return _repo_relative_path(config.network_config_path)


def _repo_relative_path(path_value: str) -> Path:
    path = Path(path_value)
    if not path.is_absolute():
        path = Path(__file__).parent.parent / path
    return path


def _load_network_config() -> NetworkConfig:
    paths = [_network_config_path()]
    legacy_path = _repo_relative_path(LEGACY_NETWORK_CONFIG_PATH)
    if legacy_path not in paths:
        paths.append(legacy_path)

    for path in paths:
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            continue
        except (OSError, json.JSONDecodeError):
            continue
        return NetworkConfig(
            m2_ip=str(raw.get("m2_ip") or "").strip(),
            m900_ip=str(raw.get("m900_ip") or "").strip(),
        )
    return NetworkConfig()


def _save_network_config(req: NetworkConfig) -> NetworkConfig:
    clean = NetworkConfig(m2_ip=req.m2_ip.strip(), m900_ip=req.m900_ip.strip())
    path = _network_config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(f"{path.suffix}.tmp")
    tmp_path.write_text(json.dumps(clean.dict(), indent=2) + "\n", encoding="utf-8")
    tmp_path.replace(path)
    return clean


def _camera_labels_path() -> Path:
    path = Path(config.camera_labels_path)
    if not path.is_absolute():
        path = Path(__file__).parent.parent / path
    return path


def _camera_settings_path() -> Path:
    path = Path(config.camera_settings_path)
    if not path.is_absolute():
        path = Path(__file__).parent.parent / path
    return path


def _load_camera_settings() -> dict:
    path = _camera_settings_path()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return raw if isinstance(raw, dict) else {}


def _save_camera_settings(settings: dict) -> dict:
    path = _camera_settings_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(settings, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return settings


def _apply_camera_settings(settings: dict):
    max_bitrate = settings.get("max_bitrate_kbps")
    try:
        max_bitrate = int(max_bitrate)
    except (TypeError, ValueError):
        return
    config.rover_camera_bitrate = max(50, min(10000, max_bitrate))


def _load_camera_labels() -> dict[str, str]:
    path = _camera_labels_path()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(raw, dict):
        return {}
    labels: dict[str, str] = {}
    for camera_id, label in raw.items():
        clean_id = str(camera_id).strip()
        clean_label = str(label).strip()
        if clean_id and clean_label:
            labels[clean_id] = clean_label[:80]
    return labels


def _save_camera_labels(labels: dict[str, str]) -> dict[str, str]:
    path = _camera_labels_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(labels, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return labels


def _apply_camera_labels(cameras: list[dict]) -> list[dict]:
    labels = _load_camera_labels()
    settings = _load_camera_settings()
    rotations = settings.get("camera_rotations") if isinstance(settings.get("camera_rotations"), dict) else {}
    for camera in cameras:
        camera_id = str(camera.get("id") or "")
        stable_key = str(camera.get("stable_key") or camera_id)
        label = labels.get(stable_key) or labels.get(camera_id)
        if label:
            camera["label"] = label
            camera["custom_label"] = label
        camera["stable_key"] = stable_key
        if stable_key in rotations or camera_id in rotations:
            camera["rotation"] = int(rotations.get(stable_key, rotations.get(camera_id, 0))) % 360
    return cameras


def _safe_host(host: str) -> str:
    host = host.strip()
    if not host:
        return ""
    if len(host) > 253 or any(c.isspace() for c in host):
        raise HTTPException(status_code=400, detail="Rocket IP/host contains invalid characters")
    return host


def _tcp_latency_ms(host: str, timeout_s: float) -> tuple[Optional[float], Optional[str]]:
    last_error: Optional[str] = None
    for port in (80, 443, 22):
        started = time.perf_counter()
        try:
            with socket.create_connection((host, port), timeout=timeout_s):
                return round((time.perf_counter() - started) * 1000.0, 1), None
        except OSError as e:
            last_error = f"tcp/{port}: {e}"
    return None, last_error or "tcp reachability failed"


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
    except OSError as e:
        latency, tcp_error = await asyncio.to_thread(_tcp_latency_ms, host, timeout_s)
        if latency is not None:
            return latency, None
        return None, tcp_error or str(e)
    except asyncio.TimeoutError as e:
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


def _ber_len(length: int) -> bytes:
    if length < 0x80:
        return bytes([length])
    raw = int(length).to_bytes((int(length).bit_length() + 7) // 8, "big")
    return bytes([0x80 | len(raw)]) + raw


def _ber_tlv(tag: int, payload: bytes) -> bytes:
    return bytes([tag]) + _ber_len(len(payload)) + payload


def _ber_int(value: int) -> bytes:
    raw = int(value).to_bytes(max(1, (int(value).bit_length() + 8) // 8), "big", signed=True)
    while len(raw) > 1 and raw[0] == 0x00 and raw[1] < 0x80:
        raw = raw[1:]
    return _ber_tlv(0x02, raw)


def _ber_oid(oid: str) -> bytes:
    parts = [int(p) for p in oid.strip(".").split(".") if p]
    if len(parts) < 2:
        raise ValueError(f"invalid SNMP OID: {oid}")
    encoded = bytearray([parts[0] * 40 + parts[1]])
    for part in parts[2:]:
        stack = [part & 0x7F]
        part >>= 7
        while part:
            stack.append(0x80 | (part & 0x7F))
            part >>= 7
        encoded.extend(reversed(stack))
    return _ber_tlv(0x06, bytes(encoded))


def _ber_read(buf: bytes, pos: int = 0) -> tuple[int, bytes, int]:
    if pos + 2 > len(buf):
        raise ValueError("truncated SNMP response")
    tag = buf[pos]
    pos += 1
    length = buf[pos]
    pos += 1
    if length & 0x80:
        size = length & 0x7F
        if size == 0 or pos + size > len(buf):
            raise ValueError("invalid SNMP length")
        length = int.from_bytes(buf[pos:pos + size], "big")
        pos += size
    end = pos + length
    if end > len(buf):
        raise ValueError("truncated SNMP value")
    return tag, buf[pos:end], end


def _ber_value_to_number(tag: int, payload: bytes) -> Optional[float]:
    if tag in (0x02, 0x41, 0x42, 0x43, 0x46):
        signed = tag == 0x02
        return float(int.from_bytes(payload or b"\x00", "big", signed=signed))
    if tag == 0x04:
        text = payload.decode("utf-8", "ignore").strip()
        try:
            return float(text.split()[0])
        except (ValueError, IndexError):
            return None
    return None


def _snmp_get_builtin(host: str, oid: str, timeout_s: float) -> Optional[float]:
    request_id = int((time.time() * 1000) % 0x7FFFFFFF)
    varbind = _ber_tlv(0x30, _ber_oid(oid) + _ber_tlv(0x05, b""))
    varbinds = _ber_tlv(0x30, varbind)
    pdu = _ber_tlv(0xA0, _ber_int(request_id) + _ber_int(0) + _ber_int(0) + varbinds)
    message = _ber_tlv(
        0x30,
        _ber_int(1) + _ber_tlv(0x04, config.rocket_snmp_community.encode("utf-8")) + pdu,
    )

    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.settimeout(timeout_s)
            sock.sendto(message, (host, 161))
            data, _ = sock.recvfrom(4096)
    except OSError:
        return None

    try:
        tag, outer, _ = _ber_read(data)
        if tag != 0x30:
            return None
        _, _, pos = _ber_read(outer, 0)      # version
        _, _, pos = _ber_read(outer, pos)    # community
        pdu_tag, pdu_body, _ = _ber_read(outer, pos)
        if pdu_tag not in (0xA2, 0xA0):
            return None
        _, _, p = _ber_read(pdu_body, 0)     # request id
        _, err_payload, p = _ber_read(pdu_body, p)
        if int.from_bytes(err_payload or b"\x00", "big", signed=True) != 0:
            return None
        _, _, p = _ber_read(pdu_body, p)     # error index
        list_tag, varbind_list, _ = _ber_read(pdu_body, p)
        if list_tag != 0x30:
            return None
        vb_tag, varbind_body, _ = _ber_read(varbind_list, 0)
        if vb_tag != 0x30:
            return None
        _, _, vb_pos = _ber_read(varbind_body, 0)
        value_tag, value_payload, _ = _ber_read(varbind_body, vb_pos)
        return _ber_value_to_number(value_tag, value_payload)
    except (ValueError, IndexError):
        return None


async def _snmp_get(host: str, oid: str, timeout_s: float) -> Optional[float]:
    if not shutil_which("snmpget"):
        return await asyncio.to_thread(_snmp_get_builtin, host, oid, timeout_s)

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
            "available": True,
            "backend": "snmpget" if shutil_which("snmpget") else "builtin",
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

def _telemetry_payload() -> dict:
    return {
        "type": "telemetry",
        "telemetry": bridge.get_telemetry(),
        "gnss": bridge.get_gnss(),
        "arm_connected": bridge.is_arm_connected(),
        "drive_connected": bridge.is_drive_connected(),
        "subsystems": bridge.get_subsystems(),
        "comms": bridge.get_comms(),
        "led_controller": bridge.get_led_controller(),
        "motor_telemetry": bridge.get_motor_telemetry(),
        "dashboard_timer": dashboard_timer.snapshot(),
        "ts": time.time(),
    }


async def _telemetry_broadcast():
    while True:
        try:
            await telemetry_mgr.broadcast(_telemetry_payload())
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
    _apply_camera_settings(_load_camera_settings())
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
    password: str = ""


@app.post("/api/auth", summary="Authenticate with password, receive session token")
async def login(req: LoginRequest):
    token = create_token()
    bridge.add_log("INFO", "Passwordless session token issued", "auth")
    return {"token": token}


class ClientSessionRequest(BaseModel):
    client_id: str


@app.post("/api/session/connect")
async def connect_ui_client(req: ClientSessionRequest, request: Request):
    session = await ui_clients.connect(req.client_id, request)
    return {"ok": True, "session": session}


@app.post("/api/session/heartbeat")
async def heartbeat_ui_client(req: ClientSessionRequest):
    session = await ui_clients.heartbeat(req.client_id)
    return {"ok": True, "session": session}


@app.post("/api/session/disconnect")
async def disconnect_ui_client(req: ClientSessionRequest):
    await ui_clients.disconnect(req.client_id)
    return {"ok": True}


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
        "client_limit": await ui_clients.snapshot(),
    }


# ---------------------------------------------------------------------------
# Health — public (used by frontend to verify token is still valid)
# ---------------------------------------------------------------------------

@app.get("/api/health")
async def health(token: str = Depends(require_auth)):
    return {"status": "ok", "ts": time.time(), "client_limit": await ui_clients.snapshot()}


# ---------------------------------------------------------------------------
# Camera MJPEG streams  (token via ?token= query param for <img> src)
# ---------------------------------------------------------------------------

async def _mjpeg_generator(camera_id: str):
    idle_sleep = max(0.01, min(0.1, 1.0 / max(1, config.camera_fps * 2)))
    last_seq = -1
    while True:
        frame, seq = bridge.get_camera_frame_packet(camera_id)
        if frame and seq != last_seq:
            last_seq = seq
            yield (
                b"--frame\r\n"
                b"Cache-Control: no-store\r\n"
                b"Content-Type: image/jpeg\r\n\r\n"
                + frame
                + b"\r\n"
            )
        await asyncio.sleep(idle_sleep)


def _native_mjpeg_proxy_generator(rover_url: str):
    req = urllib.request.Request(
        rover_url,
        headers={
            "Accept": "multipart/x-mixed-replace,image/jpeg,*/*",
            "Cache-Control": "no-cache",
            "User-Agent": "RoSE-Ground-Station/1.0",
        },
    )
    with urllib.request.urlopen(req, timeout=10) as res:
        while True:
            chunk = res.read(65536)
            if not chunk:
                break
            yield chunk


@app.get("/api/camera/{camera_id}")
async def camera_stream(
    camera_id: str,
    _token: str = Depends(require_auth),
):
    raise HTTPException(
        status_code=410,
        detail="Raspberry Pi camera decode is disabled. Use /api/camera/{camera_id}/native for local browser decode.",
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
    return {"cameras": _apply_camera_labels(cameras)}


@app.post("/api/cameras/refresh")
async def refresh_cameras(_token: str = Depends(require_auth)):
    try:
        cameras = bridge.refresh_cameras()
    except RuntimeError as e:
        raise HTTPException(status_code=503, detail=str(e))
    return {"cameras": _apply_camera_labels(cameras)}


class CameraLabelRequest(BaseModel):
    label: str = Field("", max_length=80)


@app.post("/api/camera/{camera_id}/label")
async def update_camera_label(camera_id: str, req: CameraLabelRequest, _token: str = Depends(require_auth)):
    camera_id = str(camera_id)
    label = req.label.strip()
    camera = next((c for c in bridge.get_cameras() if str(c.get("id")) == camera_id), {"id": camera_id})
    stable_key = str(camera.get("stable_key") or camera_id)
    labels = _load_camera_labels()
    if label:
        labels[stable_key] = label
        labels[camera_id] = label
    else:
        labels.pop(stable_key, None)
        labels.pop(camera_id, None)
    _save_camera_labels(labels)

    fallback = str(camera.get("device") or camera.get("id") or camera_id)
    bridge.set_camera_label(camera_id, label or fallback)
    camera = next((c for c in bridge.get_cameras() if str(c.get("id")) == camera_id), {"id": camera_id})
    return {"camera": _apply_camera_labels([camera])[0], "labels": labels}


class CameraOrientationRequest(BaseModel):
    rotation: int = Field(0, ge=0, le=359)


@app.post("/api/camera/{camera_id}/orientation")
async def update_camera_orientation(camera_id: str, req: CameraOrientationRequest, _token: str = Depends(require_auth)):
    camera_id = str(camera_id)
    camera = next((c for c in bridge.get_cameras() if str(c.get("id")) == camera_id), {"id": camera_id})
    stable_key = str(camera.get("stable_key") or camera_id)
    rotation = int(req.rotation) % 360
    settings = _load_camera_settings()
    rotations = settings.get("camera_rotations")
    if not isinstance(rotations, dict):
        rotations = {}
    if rotation:
        rotations[stable_key] = rotation
        rotations[camera_id] = rotation
    else:
        rotations.pop(stable_key, None)
        rotations.pop(camera_id, None)
    settings["camera_rotations"] = rotations
    _save_camera_settings(settings)
    camera["rotation"] = rotation
    return {"ok": True, "camera": _apply_camera_labels([camera])[0]}


class CameraSettingsRequest(BaseModel):
    max_bitrate_kbps: int = Field(..., ge=50, le=10000)


@app.get("/api/camera-settings")
async def get_camera_settings(_token: str = Depends(require_auth)):
    settings = _load_camera_settings()
    _apply_camera_settings(settings)
    return {
        "settings": {
            "max_bitrate_kbps": int(config.rover_camera_bitrate),
            "min_bitrate_kbps": int(config.rover_camera_min_bitrate),
            "max_total_bitrate_kbps": int(config.rover_camera_max_total_bitrate),
            "units": "kbps",
        }
    }


@app.put("/api/camera-settings")
async def update_camera_settings(req: CameraSettingsRequest, _token: str = Depends(require_auth)):
    settings = _load_camera_settings()
    settings["max_bitrate_kbps"] = int(req.max_bitrate_kbps)
    _save_camera_settings(settings)
    _apply_camera_settings(settings)
    bridge.add_log("INFO", f"Camera max bitrate set to {config.rover_camera_bitrate} kbps", "camera")
    return await get_camera_settings(_token)


@app.post("/api/camera/{camera_id}/start")
async def start_camera(camera_id: str, _token: str = Depends(require_auth)):
    raise HTTPException(
        status_code=410,
        detail="Raspberry Pi camera decode is disabled. Use /api/camera/{camera_id}/native for local browser decode.",
    )


@app.post("/api/camera/{camera_id}/native")
async def native_camera(camera_id: str, _token: str = Depends(require_auth)):
    try:
        with _camera_control_lock:
            stream = bridge.native_camera(camera_id)
    except RuntimeError as e:
        raise HTTPException(status_code=503, detail=str(e))
    token_query = urllib.parse.urlencode({"token": _token}) if _token else ""
    proxy_url = f"/api/camera/{urllib.parse.quote(str(camera_id), safe='')}/native.mjpg"
    if token_query:
        proxy_url = f"{proxy_url}?{token_query}"
    direct_urls = [url for url in stream.get("urls", []) if url]
    if stream.get("url") and stream.get("url") not in direct_urls:
        direct_urls.insert(0, stream["url"])
    stream["direct_url"] = direct_urls[0] if direct_urls else stream.get("url")
    stream["direct_urls"] = direct_urls
    stream["proxy_url"] = proxy_url
    stream["url"] = stream["direct_url"]
    stream["urls"] = direct_urls
    stream["proxied"] = False
    stream["transport"] = "direct-mjpeg"
    return stream


@app.get("/api/camera/{camera_id}/native.mjpg")
async def native_camera_proxy(camera_id: str, _token: str = Depends(require_auth)):
    try:
        with _camera_control_lock:
            stream = bridge.native_camera(camera_id)
    except RuntimeError as e:
        raise HTTPException(status_code=503, detail=str(e))
    rover_url = stream.get("url")
    if not rover_url:
        raise HTTPException(status_code=503, detail="Rover camera service did not return a stream URL")
    try:
        return StreamingResponse(
            _native_mjpeg_proxy_generator(rover_url),
            media_type="multipart/x-mixed-replace",
            headers={"Cache-Control": "no-store"},
        )
    except OSError as e:
        raise HTTPException(status_code=503, detail=f"Rover MJPEG stream unavailable: {e}")


@app.post("/api/camera/{camera_id}/stop")
async def stop_camera(camera_id: str, _token: str = Depends(require_auth)):
    with _camera_control_lock:
        camera = bridge.stop_camera(camera_id)
    return {"camera": _apply_camera_labels([camera])[0]}


@app.post("/api/camera/{camera_id}/reset")
async def reset_camera(camera_id: str, _token: str = Depends(require_auth)):
    with _camera_control_lock:
        camera = bridge.reset_camera(camera_id)
    return {"ok": True, "camera": _apply_camera_labels([camera])[0]}


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
async def ws_telemetry(ws: WebSocket, token: Optional[str] = Query(None), client_id: Optional[str] = Query(None)):
    await telemetry_mgr.connect(ws)
    try:
        while True:
            if client_id:
                await ui_clients.touch(client_id)
            await ws.receive_text()
    except WebSocketDisconnect:
        pass
    finally:
        await telemetry_mgr.disconnect(ws)


@app.websocket("/ws/logs")
async def ws_logs(ws: WebSocket, token: Optional[str] = Query(None), client_id: Optional[str] = Query(None)):
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
    try:
        saved = _save_network_config(clean)
    except OSError as e:
        path = _network_config_path()
        bridge.add_log("ERROR", f"Could not save Network Rocket IPs to {path}: {e}", "network")
        raise HTTPException(status_code=500, detail=f"Could not save rocket IPs to {path}: {e}") from e
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
    system["led_controller"] = bridge.get_led_controller()
    system["motor_telemetry"] = bridge.get_motor_telemetry()
    return system


@app.get("/api/telemetry")
async def telemetry_snapshot(_t: str = Depends(require_auth)):
    return _telemetry_payload()


@app.get("/api/gps")
async def gps_status(_t: str = Depends(require_auth)):
    return {"gnss": bridge.get_gnss()}


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
async def list_files(path: Optional[str] = Query(None), _t: str = Depends(require_auth)):
    storage = await asyncio.to_thread(_scan_local_storage, path)
    if storage.get("ok") and (not path or storage.get("selected_file")):
        return storage

    try:
        storage = await asyncio.to_thread(bridge.get_rover_storage, path)
    except RuntimeError as e:
        logger.debug("Rover storage lookup failed after local scan: %s", e)
        storage = {"ok": False, "error": "no_storage"}
    if not storage.get("ok"):
        return JSONResponse(
            status_code=503,
            content={"ok": False, "error": "no_storage", "detail": "No external storage device detected."},
        )
    return storage


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
