"""
ROS2 bridge — integrates with rclpy when available and reports unknown/offline
state when live rover data is not present.
"""
import threading
import time
import logging
import base64
import math
import os
import shutil
import subprocess
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import cv2
import numpy as np

from .config import config

logger = logging.getLogger("ros2_bridge")

THERMAL_ROOT = Path("/sys/class/thermal")
TELEMETRY_STALE_S = 5.0
SENSOR_ONLINE_TIMEOUT_S = 5.0


def _read_temperature_c(path: Path) -> Optional[float]:
    try:
        raw = path.read_text(encoding="utf-8").strip()
        value = float(raw)
    except (OSError, ValueError):
        return None
    if abs(value) > 1000:
        value /= 1000.0
    return round(value, 1)


def _read_jetson_temperatures(root: Path = THERMAL_ROOT) -> dict:
    zones: list[dict] = []
    try:
        zone_paths = sorted(root.glob("thermal_zone*"))
    except OSError:
        zone_paths = []

    for zone_path in zone_paths:
        zone_type = ""
        try:
            zone_type = zone_path.joinpath("type").read_text(encoding="utf-8").strip()
        except OSError:
            pass
        temp_c = _read_temperature_c(zone_path / "temp")
        if temp_c is None:
            continue
        zones.append({
            "zone": zone_path.name,
            "type": zone_type or zone_path.name,
            "temp_c": temp_c,
        })

    def pick_temp(*needles: str) -> Optional[float]:
        for zone in zones:
            zone_type = str(zone["type"]).lower()
            if any(needle in zone_type for needle in needles):
                return zone["temp_c"]
        return None

    cpu_c = pick_temp("cpu")
    gpu_c = pick_temp("gpu")
    return {
        "available": bool(zones),
        "source": str(root),
        "cpu_c": cpu_c,
        "gpu_c": gpu_c,
        "zones": zones,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }


def _unique_topics(*groups: list[str]) -> list[str]:
    topics: list[str] = []
    seen: set[str] = set()
    for group in groups:
        for topic in group:
            clean = str(topic or "").strip()
            if clean and clean not in seen:
                topics.append(clean)
                seen.add(clean)
    return topics


def _yaw_deg_from_quaternion(x: float, y: float, z: float, w: float) -> float:
    siny_cosp = 2.0 * (w * z + x * y)
    cosy_cosp = 1.0 - 2.0 * (y * y + z * z)
    return (math.degrees(math.atan2(siny_cosp, cosy_cosp)) + 360.0) % 360.0

# ---------------------------------------------------------------------------
# Optional ROS2 imports
# ---------------------------------------------------------------------------
try:
    import rclpy
    from rclpy.node import Node
    from rclpy.executors import MultiThreadedExecutor
    from sensor_msgs.msg import Image, NavSatFix, BatteryState, Imu
    from std_msgs.msg import Bool, Float32, String, UInt8
    try:
        from gnc_interfaces.msg import SparkMotorTelemetry
        SPARK_TELEMETRY_AVAILABLE = True
    except ImportError:
        SparkMotorTelemetry = None
        SPARK_TELEMETRY_AVAILABLE = False
    try:
        from cv_bridge import CvBridge
        _CV_BRIDGE = CvBridge()
        CV_BRIDGE_AVAILABLE = True
    except Exception as e:
        CV_BRIDGE_AVAILABLE = False
        _CV_BRIDGE = None
        logger.warning(f"cv_bridge unavailable; using raw Image conversion fallback: {e}")
    ROS2_AVAILABLE = True
    logger.info("ROS2 (rclpy) found — running in live mode")
except ImportError:
    ROS2_AVAILABLE = False
    SPARK_TELEMETRY_AVAILABLE = False
    logger.warning("rclpy not found — ROS2 live data unavailable")


# ---------------------------------------------------------------------------
# Shared data store (thread-safe)
# ---------------------------------------------------------------------------

class _DataStore:
    def __init__(self):
        self._lock = threading.RLock()
        self.camera_frames: dict[str, Optional[bytes]] = {}
        self.camera_frame_seq: dict[str, int] = {}
        self.cameras: dict[str, dict] = {}
        self.telemetry: dict = {
            "soc": None,
            "current": None,
            "voltage": None,
            "temperature": None,
        }
        self.telemetry_updated_at: dict[str, Optional[float]] = {
            "soc": None,
            "current": None,
            "voltage": None,
            "temperature": None,
        }
        self.gnss: dict = {
            "lat": None,
            "lon": None,
            "fix": "NO_DATA",
            "satellites": None,
            "valid": False,
            "connected": False,
            "source": None,
            "last_update_s": None,
            "updated_at": None,
            "heading_deg": None,
            "heading_source": None,
        }
        self.payload_connected: bool = False
        self.arm_connected: bool = False
        self.drive_connected: bool = False
        self.payload_arduino: dict = {
            "connected": False,
            "publisher_active": False,
            "subscriber_active": False,
            "temperature_c": None,
            "moisture_pct": None,
            "last_update_s": 0,
        }
        self.life_analysis: dict = {
            "running": False,
            "last_started_s": None,
            "radar": None,
        }
        self.camera_360_capture: dict = {
            "last_capture_s": None,
            "last_servo_angles": [],
            "last_image_data_url": None,
        }
        self.subsystems: dict = {
            "arm": {
                "label": "Arm",
                "status": "critical",
                "connected": False,
                "summary": "Awaiting ROS2 status",
                "metrics": [
                    {"label": "Mode", "value": "No data"},
                    {"label": "Power", "value": "No data"},
                    {"label": "Control", "value": "No data"},
                ],
            },
            "drive": {
                "label": "Drive / Chassis",
                "status": "critical",
                "connected": False,
                "summary": "Awaiting ROS2 status",
                "metrics": [
                    {"label": "Mode", "value": "No data"},
                    {"label": "Command", "value": "No data"},
                    {"label": "CAN", "value": "No data"},
                ],
            },
        }
        self.motor_telemetry: dict = {"drive": {}, "arm": {}}
        self.comms: dict = {
            "link_24ghz": {"label": "2.4 GHz", "stability": None, "rssi_dbm": None, "latency_ms": None, "status": "unknown"},
            "link_900mhz": {"label": "900 MHz", "stability": None, "rssi_dbm": None, "latency_ms": None, "status": "unknown"},
        }
        self.led_controller: dict = {
            "connected": False,
            "publisher_active": False,
            "subscriber_active": False,
            "camera_360_connected": False,
            "camera_360_streaming": False,
            "camera_360_mode": None,
            "last_update_s": 0,
            "leds": [],
        }
        self.system: dict = {
            "jetson_temp": None,
            "jetson_ip": None,
            "rover_current_ip": None,
            "rover_ips": [],
            "imu_online": False,
            "imu_last_update_s": None,
            "imu_updated_at": None,
            "gnss_module_online": False,
            "uptime_s": 0,
        }
        self.logs: list[dict] = []
        self._log_id_counter = 0
        self._start_time = time.time()

    # --- thread-safe accessors ------------------------------------------------

    def get_camera_frame(self, camera_id: str) -> Optional[bytes]:
        with self._lock:
            return self.camera_frames.get(str(camera_id))

    def get_camera_frame_packet(self, camera_id: str) -> tuple[Optional[bytes], int]:
        with self._lock:
            camera_id = str(camera_id)
            return self.camera_frames.get(camera_id), int(self.camera_frame_seq.get(camera_id, 0))

    def set_camera_frame(self, camera_id: str, data: bytes):
        with self._lock:
            camera_id = str(camera_id)
            self.camera_frames[camera_id] = data
            self.camera_frame_seq[camera_id] = int(self.camera_frame_seq.get(camera_id, 0)) + 1
            if camera_id in self.cameras:
                self.cameras[camera_id]["last_frame_s"] = time.time()

    def clear_camera_frame(self, camera_id: str):
        with self._lock:
            camera_id = str(camera_id)
            self.camera_frames[camera_id] = None
            self.camera_frame_seq[camera_id] = int(self.camera_frame_seq.get(camera_id, 0)) + 1

    def set_cameras(self, cameras: list[dict]):
        with self._lock:
            existing = self.cameras
            merged: dict[str, dict] = {}
            for camera in cameras:
                camera_id = str(camera.get("id") or camera.get("device") or "")
                if not camera_id:
                    continue
                prior = existing.get(camera_id, {})
                stable_key = str(camera.get("stable_key") or prior.get("stable_key") or camera_id)
                streaming = bool(camera.get("streaming")) if "streaming" in camera else bool(prior.get("streaming", False))
                merged[camera_id] = {
                    **prior,
                    **camera,
                    "id": camera_id,
                    "stable_key": stable_key,
                    "streaming": streaming,
                    "port": camera.get("port", prior.get("port")),
                    "last_frame_s": prior.get("last_frame_s"),
                }
                self.camera_frames.setdefault(camera_id, None)
                self.camera_frame_seq.setdefault(camera_id, 0)
            self.cameras = merged

    def update_camera(self, camera_id: str, **updates):
        with self._lock:
            camera_id = str(camera_id)
            camera = dict(self.cameras.get(camera_id, {"id": camera_id, "label": camera_id}))
            camera.update(updates)
            self.cameras[camera_id] = camera
            self.camera_frames.setdefault(camera_id, None)

    def get_cameras(self) -> list[dict]:
        with self._lock:
            return [dict(v) for v in sorted(self.cameras.values(), key=lambda c: str(c.get("label") or c.get("id")))]

    def set_camera_label(self, camera_id: str, label: str):
        with self._lock:
            camera_id = str(camera_id)
            camera = dict(self.cameras.get(camera_id, {"id": camera_id}))
            camera["label"] = label
            camera["custom_label"] = label
            self.cameras[camera_id] = camera
            self.camera_frames.setdefault(camera_id, None)

    def get_telemetry(self) -> dict:
        with self._lock:
            telemetry = dict(self.telemetry)
            now = time.time()
            meta = {}
            for key, updated_at in self.telemetry_updated_at.items():
                age_s = (now - float(updated_at)) if isinstance(updated_at, (int, float)) else None
                meta[key] = {
                    "updated_at": datetime.fromtimestamp(updated_at, timezone.utc).isoformat()
                    if isinstance(updated_at, (int, float)) else None,
                    "age_s": round(age_s, 1) if age_s is not None else None,
                    "stale": age_s is None or age_s > TELEMETRY_STALE_S,
                }
            telemetry["_meta"] = meta
            return telemetry

    def set_telemetry_field(self, key: str, value):
        if key not in self.telemetry:
            return
        if isinstance(value, (int, float)) and not math.isfinite(float(value)):
            return
        with self._lock:
            self.telemetry[key] = value
            self.telemetry_updated_at[key] = time.time()

    def get_gnss(self) -> dict:
        with self._lock:
            g = dict(self.gnss)
            last_update = g.get("last_update_s")
            connected = (
                isinstance(last_update, (int, float))
                and time.time() - float(last_update) <= SENSOR_ONLINE_TIMEOUT_S
            )
            g["connected"] = connected
            if not connected:
                g.update({"valid": False, "fix": "NO_DATA", "lat": None, "lon": None})
            elif not g.get("valid"):
                g["fix"] = "PENDING"
            return g

    def get_system(self) -> dict:
        with self._lock:
            s = dict(self.system)
            now = time.time()
            imu_last_update = s.get("imu_last_update_s")
            gnss_last_update = self.gnss.get("last_update_s")
            s["imu_online"] = (
                isinstance(imu_last_update, (int, float))
                and now - float(imu_last_update) <= SENSOR_ONLINE_TIMEOUT_S
            )
            s["gnss_module_online"] = (
                isinstance(gnss_last_update, (int, float))
                and now - float(gnss_last_update) <= SENSOR_ONLINE_TIMEOUT_S
            )
            s["uptime_s"] = int(time.time() - self._start_time)
            return s

    def is_arm_connected(self) -> bool:
        with self._lock:
            return self.arm_connected

    def is_drive_connected(self) -> bool:
        with self._lock:
            return self.drive_connected

    def get_subsystems(self) -> dict:
        with self._lock:
            subsystems = {k: dict(v) for k, v in self.subsystems.items()}
            for value in subsystems.values():
                value["metrics"] = [dict(m) for m in value.get("metrics", [])]
            return subsystems

    def get_motor_telemetry(self) -> dict:
        with self._lock:
            return {
                group: {str(device_id): dict(motor) for device_id, motor in motors.items()}
                for group, motors in self.motor_telemetry.items()
            }

    def update_motor_telemetry(self, msg):
        with self._lock:
            group = str(msg.group or "unknown")
            if group not in self.motor_telemetry:
                self.motor_telemetry[group] = {}
            faults = list(getattr(msg, "fault_names", []) or [])
            sticky_faults = list(getattr(msg, "sticky_fault_names", []) or [])
            status = "online" if msg.connected else "offline"
            self.motor_telemetry[group][int(msg.device_id)] = {
                "device_id": int(msg.device_id),
                "name": msg.name,
                "group": group,
                "connected": bool(msg.connected),
                "applied_output": round(float(msg.applied_output), 3),
                "motor_velocity_rpm": round(float(msg.motor_velocity_rpm), 1),
                "motor_temperature_c": round(float(msg.motor_temperature_c), 1),
                "bus_voltage_v": round(float(msg.bus_voltage_v), 2),
                "motor_current_a": round(float(msg.motor_current_a), 2),
                "faults": int(msg.faults),
                "sticky_faults": int(msg.sticky_faults),
                "fault_names": faults,
                "sticky_fault_names": sticky_faults,
                "last_update_s": float(msg.last_update_s),
                "status": status,
            }
            self._update_motor_subsystem_summary(group)

    def _update_motor_subsystem_summary(self, group: str):
        motors = list(self.motor_telemetry.get(group, {}).values())
        if not motors or group not in self.subsystems:
            return
        connected = [m for m in motors if m.get("connected")]
        total_current = sum(m.get("motor_current_a", 0.0) for m in connected)
        status = "online" if connected else "offline"
        self.subsystems[group].update({
            "connected": bool(connected),
            "status": status,
            "summary": f"{len(connected)}/{len(motors)} motor controllers online",
            "metrics": [
                {"label": "Motors", "value": f"{len(connected)}/{len(motors)}"},
                {"label": "Current", "value": f"{total_current:.1f} A"},
                {"label": "Status", "value": "Online" if connected else "Offline"},
            ],
        })

    def get_comms(self) -> dict:
        with self._lock:
            return {k: dict(v) for k, v in self.comms.items()}

    def get_led_controller(self) -> dict:
        with self._lock:
            controller = dict(self.led_controller)
            controller["leds"] = [dict(led) for led in self.led_controller.get("leds", [])]
            return controller

    def get_topic_overview(self) -> dict:
        return {
            "mode": "live" if ROS2_AVAILABLE else "ros2_unavailable",
            "publishes": [
                {"topic": config.estop_topic, "type": "std_msgs/Bool", "purpose": "Emergency stop state"},
                {"topic": config.elevator_topic, "type": "std_msgs/Float32", "purpose": "Payload elevator step command"},
                {"topic": config.carousel_topic, "type": "std_msgs/Float32", "purpose": "Payload carousel step command"},
                {"topic": config.auger_topic, "type": "std_msgs/Float32", "purpose": "Payload auger speed command"},
            ],
            "subscribes": [
                *[
                    {"topic": topic, "type": "sensor_msgs/Image", "purpose": f"{label} camera stream"}
                    for topic, label in zip(config.camera_topics, config.camera_labels)
                ],
                *[
                    {"topic": topic, "type": "sensor_msgs/NavSatFix", "purpose": "GNSS rover fix"}
                    for topic in _unique_topics(config.gnss_topics, [config.gnss_topic])
                ],
                *[
                    {"topic": topic, "type": "sensor_msgs/Imu", "purpose": "Rover heading from IMU orientation"}
                    for topic in _unique_topics(config.imu_topics, [config.imu_topic])
                ],
                {"topic": config.battery_topic, "type": "sensor_msgs/BatteryState", "purpose": "Battery telemetry"},
                {"topic": config.jetson_temp_topic, "type": "std_msgs/Float32", "purpose": "Jetson temperature"},
                {"topic": config.jetson_ip_topic, "type": "std_msgs/String", "purpose": "Jetson IP on the rover/ground-station network"},
                {"topic": config.led_arduino_status_topic, "type": "std_msgs/Bool", "purpose": "LED Arduino online state"},
                {"topic": config.led_arduino_360_camera_topic, "type": "std_msgs/Bool", "purpose": "360 camera online/streaming state"},
                {"topic": config.arm_status_topic, "type": "std_msgs/Bool", "purpose": "Arm subsystem online state"},
                {"topic": config.drive_status_topic, "type": "std_msgs/Bool", "purpose": "Drive subsystem online state"},
                {"topic": config.link_24ghz_topic, "type": "std_msgs/Float32", "purpose": "2.4 GHz link stability"},
                {"topic": config.link_900mhz_topic, "type": "std_msgs/Float32", "purpose": "900 MHz link stability"},
            ],
        }

    def add_log(self, level: str, message: str, source: str = "system"):
        with self._lock:
            self._log_id_counter += 1
            entry = {
                "id": self._log_id_counter,
                "ts": datetime.now(timezone.utc).isoformat(),
                "level": level.upper(),
                "source": source,
                "msg": message,
            }
            self.logs.append(entry)
            if len(self.logs) > 2000:
                self.logs = self.logs[-2000:]
            return entry

    def get_logs_since(self, since_id: int = 0) -> list:
        with self._lock:
            return [e for e in self.logs if e["id"] > since_id]


store = _DataStore()


# ---------------------------------------------------------------------------
# UDP/RTP H264 camera receiver and rover camera control
# ---------------------------------------------------------------------------

_receiver_lock = threading.RLock()
_receiver_stop_events: dict[str, threading.Event] = {}
_receiver_threads: dict[str, threading.Thread] = {}
_last_camera_client_ip: Optional[str] = None
_rover_camera_service_url: Optional[str] = None
_rover_camera_service_lock = threading.RLock()


def _rover_camera_service_candidates() -> list[str]:
    candidates: list[str] = []

    with _rover_camera_service_lock:
        cached_url = _rover_camera_service_url
    if cached_url:
        candidates.append(cached_url)

    with store._lock:
        rover_ips = [
            str(ip).strip()
            for ip in store.system.get("rover_ips", [])
            if str(ip).strip()
        ]
        jetson_ip = str(store.system.get("jetson_ip") or "").strip()
    if jetson_ip and jetson_ip not in rover_ips:
        rover_ips.insert(0, jetson_ip)

    for rover_ip in rover_ips:
        url = f"http://{rover_ip}:{config.rover_camera_service_port}"
        if url not in candidates:
            candidates.append(url)

    for url in config.rover_camera_service_urls:
        if url and url not in candidates:
            candidates.append(url)

    return candidates


def _rover_request(method: str, path: str, payload: Optional[dict] = None, timeout: float = 3.0) -> dict:
    global _rover_camera_service_url
    data = None
    headers = {"Accept": "application/json"}
    if payload is not None:
        import json
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"

    errors: list[str] = []
    raw = ""
    for base_url in _rover_camera_service_candidates():
        url = base_url + path
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as res:
                raw = res.read().decode("utf-8")
            with _rover_camera_service_lock:
                if _rover_camera_service_url != base_url:
                    _rover_camera_service_url = base_url
                    store.add_log("INFO", f"Using rover camera service at {base_url}", "camera")
            break
        except urllib.error.URLError as e:
            errors.append(f"{url}: {e}")
    else:
        attempted = "; ".join(errors) if errors else "no rover camera service URLs configured"
        raise RuntimeError(f"Rover camera service unavailable; tried {attempted}")

    if not raw:
        return {}
    import json
    return json.loads(raw)


def _active_rover_camera_service_url() -> str:
    with _rover_camera_service_lock:
        if _rover_camera_service_url:
            return _rover_camera_service_url
    candidates = _rover_camera_service_candidates()
    if not candidates:
        raise RuntimeError("No rover camera service URL configured")
    return candidates[0]

def _gst_udp_h264_pipeline(port: int) -> str:
    low_latency_queue = "queue max-size-buffers=1 max-size-bytes=0 max-size-time=0 leaky=downstream"
    return (
        f"udpsrc port={port} buffer-size={config.camera_udp_buffer_size} "
        'caps="application/x-rtp,media=video,encoding-name=H264,payload=96" ! '
        f"{low_latency_queue} ! "
        "rtph264depay ! "
        f"{low_latency_queue} ! "
        "h264parse ! "
        f"{low_latency_queue} ! "
        "avdec_h264 ! "
        f"{low_latency_queue} ! "
        "videoconvert ! "
        "appsink drop=true max-buffers=1 sync=false"
    )


def _gst_udp_jpeg_command(port: int) -> list[str]:
    low_latency_queue = [
        "queue",
        "max-size-buffers=1",
        "max-size-bytes=0",
        "max-size-time=0",
        "leaky=downstream",
    ]
    return [
        "gst-launch-1.0",
        "-q",
        "udpsrc",
        f"port={port}",
        f"buffer-size={config.camera_udp_buffer_size}",
        "caps=application/x-rtp,media=video,encoding-name=H264,payload=96",
        "!",
        *low_latency_queue,
        "!",
        "rtph264depay",
        "!",
        *low_latency_queue,
        "!",
        "h264parse",
        "!",
        *low_latency_queue,
        "!",
        "avdec_h264",
        "!",
        *low_latency_queue,
        "!",
        "videoconvert",
        "!",
        "jpegenc",
        f"quality={config.jpeg_quality}",
        "!",
        "fdsink",
        "fd=1",
    ]


def _gst_subprocess_camera_loop(camera_id: str, port: int, stop_event: threading.Event):
    if not shutil.which("gst-launch-1.0"):
        store.add_log("ERROR", "gst-launch-1.0 not found; install GStreamer to receive UDP cameras", "camera")
        time.sleep(5.0)
        return

    store.add_log("INFO", f"Camera {camera_id} using gst-launch receiver on port {port}", "camera")
    proc = subprocess.Popen(
        _gst_udp_jpeg_command(port),
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        bufsize=0,
    )

    assert proc.stdout is not None
    buffer = bytearray()
    last_frame_s = time.time()
    try:
        while proc.poll() is None and not stop_event.is_set():
            chunk = proc.stdout.read(4096)
            if not chunk:
                time.sleep(0.01)
                continue

            buffer.extend(chunk)
            while True:
                start = buffer.find(b"\xff\xd8")
                end = buffer.find(b"\xff\xd9", start + 2) if start >= 0 else -1
                if start < 0:
                    del buffer[:-1]
                    break
                if end < 0:
                    if start > 0:
                        del buffer[:start]
                    break

                frame = bytes(buffer[start:end + 2])
                del buffer[:end + 2]
                store.set_camera_frame(camera_id, frame)
                last_frame_s = time.time()

            if time.time() - last_frame_s > 5.0:
                store.add_log("WARN", f"Camera {camera_id} has not received a JPEG frame in 5 seconds", "camera")
                last_frame_s = time.time()
    finally:
        proc.terminate()
        try:
            proc.communicate(timeout=1.0)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.communicate()


def _udp_camera_loop(camera_id: str, port: int, stop_event: threading.Event):
    camera = next((c for c in store.get_cameras() if c.get("id") == str(camera_id)), {})
    label = camera.get("label") or str(camera_id)
    store.add_log("INFO", f"Opening UDP/RTP camera {camera_id} ({label}) on port {port}", "camera")

    while not stop_event.is_set():
        cap = cv2.VideoCapture(_gst_udp_h264_pipeline(port), cv2.CAP_GSTREAMER)
        if not cap.isOpened():
            store.add_log("WARN", f"OpenCV GStreamer receiver unavailable for camera {camera_id}; trying gst-launch", "camera")
            _gst_subprocess_camera_loop(str(camera_id), port, stop_event)
            time.sleep(2.0)
            continue

        store.add_log("INFO", f"Camera {camera_id} receiving UDP/RTP H264 on port {port}", "camera")
        while not stop_event.is_set():
            ok, frame = cap.read()
            if not ok or frame is None:
                store.add_log("WARN", f"Camera {camera_id} stream lost on port {port}; reconnecting", "camera")
                cap.release()
                time.sleep(1.0)
                break

            ok, buf = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, config.jpeg_quality])
            if ok:
                store.set_camera_frame(str(camera_id), buf.tobytes())
        cap.release()
    store.clear_camera_frame(str(camera_id))
    store.add_log("INFO", f"Camera {camera_id} receiver stopped", "camera")


def _camera_port(camera_id: str) -> int:
    cameras = store.get_cameras()
    ids = [str(c.get("id")) for c in cameras]
    try:
        index = ids.index(str(camera_id))
    except ValueError:
        index = 0
    if not config.camera_udp_ports:
        raise RuntimeError("GS_CAMERA_SOURCE=udp but no UDP ports configured")
    if index >= len(config.camera_udp_ports):
        raise RuntimeError("Not enough GS_CAMERA_UDP_PORTS configured for discovered cameras")
    return int(config.camera_udp_ports[index])


def _clamp_int(value: float, low: int, high: int) -> int:
    low = int(low)
    high = max(low, int(high))
    return max(low, min(high, int(round(value))))


def _camera_stable_key(camera: dict) -> str:
    for key in ("stable_key", "serial", "serial_number", "device_path", "path", "device", "bus_info", "uid", "id"):
        value = str(camera.get(key) or "").strip()
        if value:
            return value
    return str(camera)


def _zed_group_key(camera: dict) -> Optional[str]:
    text = " ".join(
        str(camera.get(key) or "")
        for key in ("label", "name", "model", "device", "path", "id", "stable_key")
    ).lower()
    if "zed" not in text:
        return None
    serial = str(camera.get("serial") or camera.get("serial_number") or "").strip()
    if serial:
        return f"zed:{serial}"
    return "zed:default"


def _normalize_discovered_cameras(cameras: list[dict]) -> list[dict]:
    normalized: list[dict] = []
    seen_zed: set[str] = set()
    for camera in cameras:
        item = dict(camera)
        item["stable_key"] = _camera_stable_key(item)
        zed_key = _zed_group_key(item)
        if zed_key:
            if zed_key in seen_zed:
                store.add_log("INFO", f"Suppressing duplicate ZED stereo feed {item.get('id')}", "camera")
                continue
            seen_zed.add(zed_key)
            item["stable_key"] = zed_key
            item.setdefault("label", "ZED Camera")
        normalized.append(item)
    return normalized


def _active_camera_ids(include_camera_id: Optional[str] = None) -> list[str]:
    ids = [
        str(camera.get("id"))
        for camera in store.get_cameras()
        if camera.get("streaming") and camera.get("id") is not None
    ]
    if include_camera_id:
        include_camera_id = str(include_camera_id)
        if include_camera_id not in ids:
            ids.append(include_camera_id)
    return ids


def _camera_stream_budget(stream_count: int) -> dict:
    stream_count = max(1, int(stream_count))
    max_bitrate = max(1, int(config.rover_camera_bitrate))
    total_bitrate = max(0, int(config.rover_camera_max_total_bitrate))
    if total_bitrate:
        fair_share = max(1, total_bitrate // stream_count)
        preferred_min = max(1, int(config.rover_camera_min_bitrate))
        # The global camera budget protects rover control traffic, so it must
        # win over the per-stream minimum when many cameras are active.
        bitrate = min(max_bitrate, fair_share)
        if fair_share >= preferred_min:
            bitrate = max(preferred_min, bitrate)
    else:
        bitrate = max_bitrate

    ratio = max(0.1, min(1.0, bitrate / max_bitrate))
    scale = ratio ** 0.5
    max_fps = max(1, int(config.rover_camera_max_fps))
    min_fps = max(1, min(int(config.rover_camera_min_fps), max_fps))
    max_width = max(1, int(config.rover_camera_max_width))
    max_height = max(1, int(config.rover_camera_max_height))
    min_width = max(1, min(int(config.rover_camera_min_width), max_width))
    min_height = max(1, min(int(config.rover_camera_min_height), max_height))

    return {
        "max_fps": _clamp_int(max_fps * scale, min_fps, max_fps),
        "max_width": _clamp_int(max_width * scale, min_width, max_width),
        "max_height": _clamp_int(max_height * scale, min_height, max_height),
        "bitrate": int(bitrate),
        "stream_count": stream_count,
        "total_bitrate": total_bitrate,
    }


def _camera_stream_payload(camera_id: str, client_ip: str, stream_count: int) -> dict:
    budget = _camera_stream_budget(stream_count)
    return {
        "client_ip": client_ip,
        "port": _camera_port(camera_id),
        "max_fps": budget["max_fps"],
        "max_width": budget["max_width"],
        "max_height": budget["max_height"],
        "bitrate": budget["bitrate"],
    }


def _start_udp_camera_receiver(camera_id: str, port: int):
    with _receiver_lock:
        prior = _receiver_stop_events.get(str(camera_id))
        thread = _receiver_threads.get(str(camera_id))
        if prior and thread and thread.is_alive():
            return
        stop_event = threading.Event()
        _receiver_stop_events[str(camera_id)] = stop_event
        t = threading.Thread(target=_udp_camera_loop, args=(str(camera_id), int(port), stop_event), daemon=True)
        _receiver_threads[str(camera_id)] = t
        t.start()


def _stop_udp_camera_receiver(camera_id: str):
    with _receiver_lock:
        stop_event = _receiver_stop_events.pop(str(camera_id), None)
        _receiver_threads.pop(str(camera_id), None)
    if stop_event:
        stop_event.set()


# ---------------------------------------------------------------------------
# ROS2 node (only instantiated when rclpy is available)
# ---------------------------------------------------------------------------

if ROS2_AVAILABLE:
    class _RoverNode(Node):
        def __init__(self):
            super().__init__("rose_ground_station")
            store.add_log("INFO", "ROS2 node initialised", "ros2")

            if config.camera_source == "ros2":
                for i, topic in enumerate(config.camera_topics):
                    self.create_subscription(Image, topic, self._make_cam_cb(f"ros{i}"), 5)

            for topic in _unique_topics(config.gnss_topics, [config.gnss_topic]):
                self.create_subscription(NavSatFix, topic, self._make_gnss_cb(topic), 10)
                store.add_log("INFO", f"Subscribed to GNSS topic {topic}", "ros2")
            for topic in _unique_topics(config.imu_topics, [config.imu_topic]):
                self.create_subscription(Imu, topic, self._make_imu_cb(topic), 10)
                store.add_log("INFO", f"Subscribed to IMU heading topic {topic}", "ros2")
            self.create_subscription(BatteryState, config.battery_topic, self._battery_cb, 10)
            self.create_subscription(Float32, config.jetson_temp_topic, self._jetson_temp_cb, 5)
            self.create_subscription(String, config.jetson_ip_topic, self._jetson_ip_cb, 5)
            self.create_subscription(Float32, config.payload_temperature_topic, self._payload_temperature_cb, 5)
            self.create_subscription(Float32, config.payload_moisture_topic, self._payload_moisture_cb, 5)
            self.create_subscription(Bool, config.led_arduino_status_topic, self._led_arduino_status_cb, 5)
            self.create_subscription(Bool, config.led_arduino_360_camera_topic, self._camera_360_status_cb, 5)
            self.create_subscription(Bool, config.payload_status_topic, self._payload_status_cb, 5)
            self.create_subscription(Bool, config.arm_status_topic, self._arm_status_cb, 5)
            self.create_subscription(Bool, config.drive_status_topic, self._drive_status_cb, 5)
            if SPARK_TELEMETRY_AVAILABLE:
                self.create_subscription(SparkMotorTelemetry, config.drive_motor_telemetry_topic, self._motor_telemetry_cb, 20)
                self.create_subscription(SparkMotorTelemetry, config.arm_motor_telemetry_topic, self._motor_telemetry_cb, 20)
            else:
                store.add_log(
                    "WARN",
                    "gnc_interfaces/SparkMotorTelemetry not available; motor telemetry disabled until the ROS2 workspace is sourced",
                    "ros2",
                )
            self.create_subscription(Float32, config.link_24ghz_topic, self._make_link_cb("link_24ghz"), 5)
            self.create_subscription(Float32, config.link_900mhz_topic, self._make_link_cb("link_900mhz"), 5)

            # publishers
            self._estop_pub = self.create_publisher(Bool, config.estop_topic, 1)
            self._elevator_pub = self.create_publisher(Float32, config.elevator_topic, 10)
            self._carousel_pub = self.create_publisher(Float32, config.carousel_topic, 10)
            self._auger_pub = self.create_publisher(Float32, config.auger_topic, 10)
            self._drive_clear_faults_pub = self.create_publisher(UInt8, config.drive_clear_faults_topic, 10)
            self._arm_clear_faults_pub = self.create_publisher(UInt8, config.arm_clear_faults_topic, 10)
            self.create_timer(1.0, self._payload_graph_health_cb)

        def _make_cam_cb(self, cam_id: str):
            def cb(msg: Image):
                try:
                    if CV_BRIDGE_AVAILABLE:
                        frame = _CV_BRIDGE.imgmsg_to_cv2(msg, "bgr8")
                    else:
                        arr = np.frombuffer(msg.data, dtype=np.uint8)
                        frame = arr.reshape(msg.height, msg.width, -1)
                        if msg.encoding == "rgb8":
                            frame = cv2.cvtColor(frame, cv2.COLOR_RGB2BGR)
                    ok, buf = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, config.jpeg_quality])
                    if ok:
                        store.set_camera_frame(cam_id, buf.tobytes())
                except Exception as e:
                    store.add_log("ERROR", f"Camera {cam_id} decode error: {e}", "ros2")
            return cb

        def _make_gnss_cb(self, topic: str):
            def cb(msg: NavSatFix):
                lat = float(msg.latitude)
                lon = float(msg.longitude)
                valid_position = math.isfinite(lat) and math.isfinite(lon) and abs(lat) <= 90 and abs(lon) <= 180
                valid_fix = int(msg.status.status) >= 0 and valid_position
                if msg.status.status == 2:
                    fix = "GBAS"
                elif msg.status.status == 1:
                    fix = "SBAS"
                elif msg.status.status == 0:
                    fix = "FIX"
                else:
                    fix = "PENDING"

                with store._lock:
                    prior_heading = store.gnss.get("heading_deg")
                    prior_heading_source = store.gnss.get("heading_source")
                    store.gnss.update({
                        "lat": round(lat, 7) if valid_position else None,
                        "lon": round(lon, 7) if valid_position else None,
                        "altitude_m": round(float(msg.altitude), 2) if math.isfinite(float(msg.altitude)) else None,
                        "fix": fix,
                        "fix_status": int(msg.status.status),
                        "satellites": store.gnss.get("satellites"),
                        "valid": valid_fix,
                        "connected": True,
                        "source": topic,
                        "last_update_s": time.time(),
                        "updated_at": datetime.now(timezone.utc).isoformat(),
                        "heading_deg": prior_heading,
                        "heading_source": prior_heading_source,
                    })
                    store.system["gnss_module_online"] = True
            return cb

        def _make_imu_cb(self, topic: str):
            def cb(msg: Imu):
                q = msg.orientation
                quat = [float(q.x), float(q.y), float(q.z), float(q.w)]
                if not all(math.isfinite(v) for v in quat) or all(abs(v) < 1e-9 for v in quat):
                    return
                heading = round(_yaw_deg_from_quaternion(*quat), 1)
                with store._lock:
                    store.gnss["heading_deg"] = heading
                    store.gnss["heading_source"] = topic
                    store.system["imu_last_update_s"] = time.time()
                    store.system["imu_updated_at"] = datetime.now(timezone.utc).isoformat()
                    store.system["imu_online"] = True
            return cb

        def _battery_cb(self, msg: BatteryState):
            store.set_telemetry_field("soc", round(msg.percentage * 100, 1))
            store.set_telemetry_field("current", round(abs(msg.current), 2))
            store.set_telemetry_field("voltage", round(msg.voltage, 2))
            temperature = float(getattr(msg, "temperature", float("nan")))
            if math.isfinite(temperature):
                store.set_telemetry_field("temperature", round(temperature, 1))

        def _jetson_temp_cb(self, msg: Float32):
            raw_temp = float(msg.data)
            if not math.isfinite(raw_temp):
                return
            temp = round(raw_temp, 1)
            with store._lock:
                store.system["jetson_temp"] = temp
            store.set_telemetry_field("temperature", temp)

        def _jetson_ip_cb(self, msg: String):
            ip = str(msg.data or "").strip()
            with store._lock:
                store.system["jetson_ip"] = ip or None
                store.system["rover_current_ip"] = ip or None
                store.system["rover_ips"] = [ip] if ip else []

        def _led_arduino_status_cb(self, msg: Bool):
            with store._lock:
                store.led_controller["connected"] = msg.data
                store.led_controller["last_update_s"] = round(time.time() - store._start_time, 1)

        def _camera_360_status_cb(self, msg: Bool):
            with store._lock:
                store.led_controller["camera_360_connected"] = msg.data
                store.led_controller["camera_360_streaming"] = msg.data
                store.led_controller["last_update_s"] = round(time.time() - store._start_time, 1)

        def _arm_status_cb(self, msg: Bool):
            with store._lock:
                store.arm_connected = msg.data
                store.subsystems["arm"]["connected"] = msg.data

        def _drive_status_cb(self, msg: Bool):
            with store._lock:
                store.drive_connected = msg.data
                store.subsystems["drive"]["connected"] = msg.data

        def _motor_telemetry_cb(self, msg):
            store.update_motor_telemetry(msg)

        def _make_link_cb(self, link_key: str):
            def cb(msg: Float32):
                stability = max(0, min(100, float(msg.data)))
                status = "nominal" if stability >= 70 else "degraded" if stability >= 45 else "critical"
                with store._lock:
                    store.comms[link_key]["stability"] = round(stability)
                    store.comms[link_key]["status"] = status
            return cb

        # --- publishers -------------------------------------------------------
        def _publish_redundant(self, publisher, msg, label: str):
            count = max(1, int(config.command_publish_redundancy))
            for i in range(count):
                publisher.publish(msg)
                if i < count - 1:
                    time.sleep(config.command_publish_spacing_s)
            store.add_log("DEBUG", f"{label} published x{count}", "ros2")

        def publish_estop(self, active: bool = True):
            m = Bool()
            m.data = active
            self._publish_redundant(self._estop_pub, m, "E-STOP command")
            store.add_log("WARN" if active else "INFO",
                          f"E-STOP {'ACTIVATED' if active else 'RESET'}", "estop")

        def publish_elevator(self, mm: float):
            m = Float32()
            m.data = float(mm)
            self._publish_redundant(self._elevator_pub, m, "Elevator command")

        def publish_carousel(self, steps: float):
            m = Float32()
            m.data = float(steps)
            self._publish_redundant(self._carousel_pub, m, "Carousel command")

        def publish_auger(self, speed: float):
            m = Float32()
            m.data = float(speed)
            self._publish_redundant(self._auger_pub, m, "Auger command")

        def publish_clear_motor_faults(self, group: str, device_id: int):
            m = UInt8()
            m.data = int(device_id) & 0xFF
            if group == "drive":
                publisher = self._drive_clear_faults_pub
            elif group == "arm":
                publisher = self._arm_clear_faults_pub
            else:
                raise ValueError(f"unknown motor group: {group}")
            self._publish_redundant(publisher, m, f"Clear {group} SPARK faults for id {device_id}")


# ---------------------------------------------------------------------------
# Public bridge object
# ---------------------------------------------------------------------------

class ROS2Bridge:
    def __init__(self):
        self._node: Optional["_RoverNode"] = None  # type: ignore[name-defined]
        if config.camera_source not in ("ros2", "udp"):
            store.add_log("ERROR", f"Unknown GS_CAMERA_SOURCE={config.camera_source!r}; expected 'udp' or 'ros2'", "camera")
        if ROS2_AVAILABLE:
            self._init_ros2()
        else:
            store.add_log("ERROR", "ROS2 unavailable; live rover topics are not connected", "bridge")

    def _init_ros2(self):
        try:
            rclpy.init()
            self._node = _RoverNode()
            executor = MultiThreadedExecutor()
            executor.add_node(self._node)
            t = threading.Thread(target=executor.spin, daemon=True)
            t.start()
            store.add_log("INFO", "ROS2 executor running", "bridge")
        except Exception as e:
            store.add_log("ERROR", f"ROS2 init failed: {e}; live rover topics are not connected", "bridge")

    # --- data accessors -------------------------------------------------------

    def get_camera_frame(self, camera_id: str) -> Optional[bytes]:
        return store.get_camera_frame(str(camera_id))

    def get_camera_frame_packet(self, camera_id: str) -> tuple[Optional[bytes], int]:
        return store.get_camera_frame_packet(str(camera_id))

    def get_cameras(self) -> list[dict]:
        return store.get_cameras()

    def set_camera_label(self, camera_id: str, label: str):
        store.set_camera_label(camera_id, label)

    def native_camera(self, camera_id: str) -> dict:
        camera_id = str(camera_id)
        if config.camera_source != "udp":
            raise RuntimeError("Browser decode is only available when GS_CAMERA_SOURCE=udp")

        res = _rover_request("GET", "/cameras", timeout=5.0)
        cameras = _normalize_discovered_cameras(res.get("cameras", []))
        store.set_cameras(cameras)
        camera = next((c for c in store.get_cameras() if str(c.get("id")) == camera_id), None)
        if not camera:
            raise RuntimeError(f"Camera {camera_id} not found")

        with _receiver_lock:
            receiver_active = camera_id in _receiver_stop_events
        if receiver_active:
            try:
                _rover_request("POST", f"/cameras/{urllib.parse.quote(camera_id, safe='')}/stop", {}, timeout=3.0)
            except RuntimeError as e:
                store.add_log("WARN", f"Could not stop rover UDP stream before browser decode: {e}", "camera")

        _stop_udp_camera_receiver(camera_id)
        store.update_camera(camera_id, streaming=False, stream_budget=None, transport="direct-mjpeg")
        store.clear_camera_frame(camera_id)

        base_url = _active_rover_camera_service_url()
        stream_query = urllib.parse.urlencode({
            "low_latency": "1",
            "max_bitrate": int(config.rover_camera_bitrate),
            "max_fps": int(config.rover_camera_max_fps),
        })
        url = f"{base_url}/cameras/{urllib.parse.quote(camera_id, safe='')}/native.mjpg?{stream_query}"
        urls = [
            f"{candidate}/cameras/{urllib.parse.quote(camera_id, safe='')}/native.mjpg?{stream_query}"
            for candidate in _rover_camera_service_candidates()
        ]
        store.add_log("INFO", f"Prepared camera {camera_id} for direct browser MJPEG decode", "camera")
        return {
            "url": url,
            "urls": urls,
            "camera_id": camera_id,
            "transport": "direct-mjpeg",
            "content_type": "multipart/x-mixed-replace",
            "proxied": False,
        }

    def native_camera_url(self, camera_id: str) -> str:
        return self.native_camera(camera_id)["url"]

    def refresh_cameras(self) -> list[dict]:
        if config.camera_source == "ros2":
            cameras = []
            for i, topic in enumerate(config.camera_topics):
                label = config.camera_labels[i] if i < len(config.camera_labels) else topic.rsplit("/", 1)[-1]
                cameras.append({"id": f"ros{i}", "label": label, "topic": topic, "source": "ros2"})
            store.set_cameras(cameras)
            return store.get_cameras()
        res = _rover_request("GET", "/cameras", timeout=5.0)
        cameras = _normalize_discovered_cameras(res.get("cameras", []))
        store.set_cameras(cameras)
        store.add_log("INFO", f"Discovered {len(cameras)} rover camera(s)", "camera")
        return store.get_cameras()

    def start_camera(self, camera_id: str, client_ip: str) -> dict:
        global _last_camera_client_ip
        camera_id = str(camera_id)
        _last_camera_client_ip = client_ip
        if config.camera_source == "ros2":
            store.update_camera(camera_id, streaming=True)
            return next((c for c in store.get_cameras() if c.get("id") == camera_id), {"id": camera_id})
        active_ids = _active_camera_ids(camera_id)
        stream_count = len(active_ids)
        payload = _camera_stream_payload(camera_id, client_ip, stream_count)
        port = int(payload["port"])
        res = _rover_request("POST", f"/cameras/{urllib.parse.quote(camera_id)}/start", payload, timeout=8.0)
        _start_udp_camera_receiver(camera_id, port)
        camera = res.get("camera", {"id": camera_id})
        budget = _camera_stream_budget(stream_count)
        camera.update({"port": port, "streaming": True, "stream_budget": budget})
        store.update_camera(camera_id, **camera)
        self._rebalance_udp_camera_streams(client_ip)
        store.add_log(
            "INFO",
            f"Started rover camera {camera_id} on UDP port {port} at up to "
            f"{payload['max_width']}x{payload['max_height']} {payload['max_fps']} fps, {payload['bitrate']} kbps",
            "camera",
        )
        return next((c for c in store.get_cameras() if c.get("id") == camera_id), {"id": camera_id})

    def stop_camera(self, camera_id: str) -> dict:
        camera_id = str(camera_id)
        if config.camera_source == "udp":
            try:
                _rover_request("POST", f"/cameras/{urllib.parse.quote(camera_id)}/stop", {}, timeout=3.0)
            except RuntimeError as e:
                store.add_log("WARN", str(e), "camera")
            _stop_udp_camera_receiver(camera_id)
        store.update_camera(camera_id, streaming=False, stream_budget=None)
        store.clear_camera_frame(camera_id)
        if config.camera_source == "udp" and _last_camera_client_ip:
            self._rebalance_udp_camera_streams(_last_camera_client_ip)
        store.add_log("INFO", f"Stopped camera {camera_id}", "camera")
        return next((c for c in store.get_cameras() if c.get("id") == camera_id), {"id": camera_id, "streaming": False})

    def reset_camera(self, camera_id: str) -> dict:
        camera_id = str(camera_id)
        if config.camera_source != "udp":
            store.update_camera(camera_id, streaming=False, stream_budget=None)
            store.clear_camera_frame(camera_id)
            return {"id": camera_id, "streaming": False, "reset": "local"}

        reset_error = None
        try:
            res = _rover_request("POST", f"/cameras/{urllib.parse.quote(camera_id, safe='')}/reset", {}, timeout=4.0)
            store.add_log("INFO", f"Reset rover camera {camera_id}", "camera")
            camera = res.get("camera", {"id": camera_id})
        except RuntimeError as e:
            reset_error = str(e)
            try:
                res = _rover_request("POST", f"/cameras/{urllib.parse.quote(camera_id, safe='')}/stop", {}, timeout=3.0)
                store.add_log("WARN", f"Camera reset endpoint unavailable for {camera_id}; used stop fallback", "camera")
                camera = res.get("camera", {"id": camera_id})
            except RuntimeError as stop_error:
                store.add_log("WARN", f"Could not reset camera {camera_id}: {reset_error}; stop fallback failed: {stop_error}", "camera")
                camera = {"id": camera_id, "reset_error": reset_error, "stop_error": str(stop_error)}

        camera.update({"streaming": False, "stream_budget": None})
        store.update_camera(camera_id, **camera)
        store.clear_camera_frame(camera_id)
        return camera

    def _rebalance_udp_camera_streams(self, client_ip: str):
        if not config.rover_camera_max_total_bitrate:
            return
        active_ids = _active_camera_ids()
        if len(active_ids) <= 1:
            return
        stream_count = len(active_ids)
        budget = _camera_stream_budget(stream_count)
        for active_id in active_ids:
            payload = _camera_stream_payload(active_id, client_ip, stream_count)
            try:
                res = _rover_request("POST", f"/cameras/{urllib.parse.quote(active_id)}/start", payload, timeout=8.0)
                _start_udp_camera_receiver(active_id, int(payload["port"]))
                camera = res.get("camera", {"id": active_id})
                camera.update({"port": int(payload["port"]), "streaming": True, "stream_budget": budget})
                store.update_camera(active_id, **camera)
            except RuntimeError as e:
                store.add_log("WARN", f"Could not rebalance camera {active_id}: {e}", "camera")

    def capture_camera_still(self, camera_id: str) -> dict:
        camera_id = str(camera_id)
        paused_ids = [str(c.get("id")) for c in store.get_cameras() if c.get("streaming") and str(c.get("id")) != camera_id]
        for paused_id in paused_ids:
            self.stop_camera(paused_id)

        if config.camera_source == "udp":
            payload = {
                "max_width": config.rover_camera_still_max_width,
                "max_height": config.rover_camera_still_max_height,
            }
            try:
                res = _rover_request("POST", f"/cameras/{urllib.parse.quote(camera_id)}/still", payload, timeout=15.0)
                image = res.get("image", {})
            except RuntimeError as e:
                data = store.get_camera_frame(camera_id)
                if not data:
                    raise
                store.add_log("WARN", f"Rover still endpoint failed for {camera_id}; using latest stream frame: {e}", "camera")
                image = {
                    "camera_id": camera_id,
                    "image_data_url": "data:image/jpeg;base64," + base64.b64encode(data).decode("ascii"),
                    "content_type": "image/jpeg",
                    "captured_at": datetime.now(timezone.utc).isoformat(),
                    "fallback": "latest_stream_frame",
                }
        else:
            data = store.get_camera_frame(camera_id)
            if not data:
                raise RuntimeError("No camera frame available for still capture")
            image = {
                "camera_id": camera_id,
                "image_data_url": "data:image/jpeg;base64," + base64.b64encode(data).decode("ascii"),
                "content_type": "image/jpeg",
                "captured_at": datetime.now(timezone.utc).isoformat(),
            }

        store.add_log("INFO", f"Captured HD still from camera {camera_id}; paused {len(paused_ids)} stream(s)", "camera")
        return {"image": image, "paused_camera_ids": paused_ids}

    def get_telemetry(self) -> dict:
        return store.get_telemetry()

    def get_gnss(self) -> dict:
        return store.get_gnss()

    def get_system(self) -> dict:
        system = store.get_system()
        if config.camera_source == "udp":
            try:
                rover_system = _rover_request("GET", "/system", timeout=2.0)
            except RuntimeError:
                rover_system = {}
            if rover_system:
                system["rover_system"] = rover_system
                system["jetson_cpu_temp"] = rover_system.get("rover_cpu_temp")
                system["jetson_gpu_temp"] = rover_system.get("rover_gpu_temp")
                system["jetson_temp"] = rover_system.get("rover_cpu_temp")
                system["rover_current_ip"] = rover_system.get("rover_current_ip")
                system["rover_ips"] = rover_system.get("rover_ips", [])
        else:
            system["jetson_cpu_temp"] = system.get("jetson_temp")
            system["rover_current_ip"] = system.get("jetson_ip")
            system["rover_ips"] = [system["jetson_ip"]] if system.get("jetson_ip") else []
        return system

    def get_jetson_temperatures(self) -> dict:
        if config.camera_source == "udp":
            return _rover_request("GET", "/system", timeout=2.0)
        system = store.get_system()
        return {
            "rover_cpu_temp": system.get("jetson_temp"),
            "rover_gpu_temp": None,
            "rover_current_ip": None,
            "rover_ips": [],
        }

    def is_arm_connected(self) -> bool:
        return store.is_arm_connected()

    def is_drive_connected(self) -> bool:
        return store.is_drive_connected()

    def get_subsystems(self) -> dict:
        return store.get_subsystems()

    def get_comms(self) -> dict:
        return store.get_comms()

    def get_led_controller(self) -> dict:
        return store.get_led_controller()

    def get_topic_overview(self) -> dict:
        return store.get_topic_overview()

    def get_motor_telemetry(self) -> dict:
        return store.get_motor_telemetry()

    def get_rover_storage(self, path: Optional[str] = None) -> dict:
        query = f"?path={urllib.parse.quote(path)}" if path else ""
        return _rover_request("GET", f"/storage{query}", timeout=8.0)

    def get_logs_since(self, since_id: int = 0) -> list:
        return store.get_logs_since(since_id)

    def add_log(self, level: str, message: str, source: str = "api"):
        return store.add_log(level, message, source)

    # --- commands -------------------------------------------------------------

    def _require_node(self):
        if not self._node:
            raise RuntimeError("ROS2 bridge is not connected")
        return self._node

    def send_estop(self, active: bool = True):
        self._require_node().publish_estop(active)

    def clear_motor_faults(self, group: str, device_id: int):
        group = group.lower()
        if group not in ("drive", "arm"):
            raise ValueError("group must be 'drive' or 'arm'")
        if device_id < 0 or device_id > 255:
            raise ValueError("device_id must fit in uint8")
        store.add_log(
            "WARN",
            f"Clear SPARK sticky faults requested for {group} motor id {device_id or 'ALL'}",
            "can",
        )
        self._require_node().publish_clear_motor_faults(group, device_id)

    def start_life_analysis(self):
        now = time.time()
        with store._lock:
            store.life_analysis["running"] = True
            store.life_analysis["last_started_s"] = now
        store.add_log("INFO", "Payload life analysis start requested", "payload")

    def set_life_analysis_radar(self, radar: dict):
        with store._lock:
            store.life_analysis["radar"] = dict(radar)
            store.life_analysis["running"] = False
        store.add_log("INFO", "Payload life analysis radar output received", "payload")

    def capture_360_image(self, camera_id: Optional[str] = None) -> dict:
        camera_id = str(camera_id or "").strip()
        if config.camera_source == "udp":
            cameras = store.get_cameras() or self.refresh_cameras()
            if not camera_id:
                camera_id = str(next((c.get("id") for c in cameras if c.get("id")), "") or "")
            if not camera_id:
                raise RuntimeError("No rover camera available for 360 capture")
            was_streaming = any(str(c.get("id")) == camera_id and c.get("streaming") for c in cameras)

            payload = {
                "max_width": config.rover_camera_still_max_width,
                "max_height": config.rover_camera_still_max_height,
            }
            try:
                res = _rover_request(
                    "POST",
                    f"/cameras/{urllib.parse.quote(camera_id, safe='')}/panorama",
                    payload,
                    timeout=float(os.environ.get("GS_CAMERA360_TIMEOUT_S", "260")),
                )
            finally:
                if was_streaming and _last_camera_client_ip:
                    try:
                        self.start_camera(camera_id, _last_camera_client_ip)
                    except RuntimeError as exc:
                        store.add_log("WARN", f"Could not resume camera {camera_id} after panorama: {exc}", "camera")
            result = res.get("panorama", {})
            if not result.get("image_data_url"):
                raise RuntimeError("Rover panorama endpoint did not return an image")
            source_frames = int(result.get("frame_count") or len(result.get("steps") or []))
        else:
            frames = []
            for camera in store.get_cameras():
                if camera_id and str(camera.get("id")) != camera_id:
                    continue
                data = store.get_camera_frame(str(camera.get("id")))
                if not data:
                    continue
                arr = np.frombuffer(data, dtype=np.uint8)
                frame = cv2.imdecode(arr, cv2.IMREAD_COLOR)
                if frame is not None:
                    frames.append(frame)

            if not frames:
                raise RuntimeError("No camera frames available for 360 capture")

            target_h = min(frame.shape[0] for frame in frames)
            resized = []
            for frame in frames:
                scale = target_h / frame.shape[0]
                target_w = max(1, int(frame.shape[1] * scale))
                resized.append(cv2.resize(frame, (target_w, target_h), interpolation=cv2.INTER_AREA))

            stitched = cv2.hconcat(resized)
            ok, buf = cv2.imencode(".jpg", stitched, [cv2.IMWRITE_JPEG_QUALITY, config.jpeg_quality])
            if not ok:
                raise RuntimeError("Failed to encode stitched 360 image")

            result = {
                "captured_at": datetime.now(timezone.utc).isoformat(),
                "camera_id": camera_id,
                "image_data_url": "data:image/jpeg;base64," + base64.b64encode(buf.tobytes()).decode("ascii"),
                "source_frames": len(frames),
            }
            source_frames = len(frames)

        with store._lock:
            store.camera_360_capture.update({
                "last_capture_s": time.time(),
                "last_servo_angles": [int(step.get("servo_angle", 0)) for step in result.get("steps", [])],
                "last_image_data_url": result.get("image_data_url"),
            })
        store.add_log("INFO", f"360 image stitched from {source_frames} frame(s)", "camera360")
        return result


# Singleton
bridge = ROS2Bridge()
