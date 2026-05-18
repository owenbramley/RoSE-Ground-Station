"""
ROS2 bridge — integrates with rclpy when available and reports unknown/offline
state when live rover data is not present.
"""
import random
import threading
import time
import logging
import base64
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import cv2
import numpy as np

from .config import config

logger = logging.getLogger("ros2_bridge")

THERMAL_ROOT = Path("/sys/class/thermal")


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

# ---------------------------------------------------------------------------
# Optional ROS2 imports
# ---------------------------------------------------------------------------
try:
    import rclpy
    from rclpy.node import Node
    from rclpy.executors import MultiThreadedExecutor
    from sensor_msgs.msg import Image, NavSatFix, BatteryState
    from std_msgs.msg import Bool, Float32, UInt8MultiArray
    from std_msgs.msg import UInt8
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
    except ImportError:
        CV_BRIDGE_AVAILABLE = False
        _CV_BRIDGE = None
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
        self.camera_frames: dict[int, Optional[bytes]] = {i: None for i in range(4)}
        self.telemetry: dict = {
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
            "last_command_bytes": [],
            "last_image_data_url": None,
        }
        self.subsystems: dict = {
            "payload": {
                "label": "Payload",
                "status": "critical",
                "connected": False,
                "summary": "Awaiting ROS2 status",
                "metrics": [
                    {"label": "Arduino Pub", "value": "No data"},
                    {"label": "Arduino Sub", "value": "No data"},
                    {"label": "Moisture", "value": "--"},
                ],
            },
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
            "imu_online": False,
            "gnss_module_online": False,
            "uptime_s": 0,
        }
        self.logs: list[dict] = []
        self._log_id_counter = 0
        self._start_time = time.time()

    # --- thread-safe accessors ------------------------------------------------

    def get_camera_frame(self, camera_id: int) -> Optional[bytes]:
        with self._lock:
            return self.camera_frames.get(camera_id)

    def set_camera_frame(self, camera_id: int, data: bytes):
        with self._lock:
            self.camera_frames[camera_id] = data

    def get_telemetry(self) -> dict:
        with self._lock:
            return dict(self.telemetry)

    def get_gnss(self) -> dict:
        with self._lock:
            return dict(self.gnss)

    def get_system(self) -> dict:
        with self._lock:
            s = dict(self.system)
            s["uptime_s"] = int(time.time() - self._start_time)
            jetson_temps = _read_jetson_temperatures()
            s["jetson_temperatures"] = jetson_temps
            if jetson_temps["cpu_c"] is not None:
                s["jetson_cpu_temp"] = jetson_temps["cpu_c"]
                s["jetson_temp"] = jetson_temps["cpu_c"]
            if jetson_temps["gpu_c"] is not None:
                s["jetson_gpu_temp"] = jetson_temps["gpu_c"]
            return s

    def is_payload_connected(self) -> bool:
        with self._lock:
            return self.payload_connected

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
            status = "critical" if (msg.faults or msg.sticky_faults or not msg.connected) else "nominal"
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
        faulted = [m for m in motors if m.get("faults") or m.get("sticky_faults")]
        max_temp = max((m.get("motor_temperature_c", 0.0) for m in motors), default=0.0)
        total_current = sum(m.get("motor_current_a", 0.0) for m in connected)
        status = "critical" if faulted else "degraded" if len(connected) < len(motors) else "nominal"
        self.subsystems[group].update({
            "connected": bool(connected),
            "status": status,
            "summary": f"{len(connected)}/{len(motors)} motor controllers online",
            "metrics": [
                {"label": "Motors", "value": f"{len(connected)}/{len(motors)}"},
                {"label": "Current", "value": f"{total_current:.1f} A"},
                {"label": "Faults", "value": str(len(faulted))},
            ],
        })

    def get_comms(self) -> dict:
        with self._lock:
            return {k: dict(v) for k, v in self.comms.items()}

    def get_payload_arduino(self) -> dict:
        with self._lock:
            return dict(self.payload_arduino)

    def get_life_analysis(self) -> dict:
        with self._lock:
            analysis = dict(self.life_analysis)
            analysis["radar"] = dict(analysis["radar"]) if isinstance(analysis["radar"], dict) else analysis["radar"]
            return analysis

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
                {"topic": config.led_arduino_360_capture_topic, "type": "std_msgs/UInt8MultiArray", "purpose": "360 camera servo/capture byte command"},
            ],
            "subscribes": [
                *[
                    {"topic": topic, "type": "sensor_msgs/Image", "purpose": f"{label} camera stream"}
                    for topic, label in zip(config.camera_topics, config.camera_labels)
                ],
                {"topic": config.gnss_topic, "type": "sensor_msgs/NavSatFix", "purpose": "GNSS rover fix"},
                {"topic": config.battery_topic, "type": "sensor_msgs/BatteryState", "purpose": "Battery telemetry"},
                {"topic": config.jetson_temp_topic, "type": "std_msgs/Float32", "purpose": "Jetson temperature"},
                {"topic": config.payload_temperature_topic, "type": "std_msgs/Float32", "purpose": "Payload Arduino temperature"},
                {"topic": config.payload_moisture_topic, "type": "std_msgs/Float32", "purpose": "Payload Arduino moisture"},
                {"topic": config.led_arduino_status_topic, "type": "std_msgs/Bool", "purpose": "LED Arduino online state"},
                {"topic": config.led_arduino_360_camera_topic, "type": "std_msgs/Bool", "purpose": "360 camera online/streaming state"},
                {"topic": config.payload_status_topic, "type": "std_msgs/Bool", "purpose": "Payload subsystem online state"},
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
# Mock data generator (used when ROS2 is unavailable)
# ---------------------------------------------------------------------------

_CAMERA_BG_COLORS = [
    (40, 60, 100),   # Front  — blue
    (60, 40, 40),    # Rear   — red
    (40, 70, 50),    # Left   — green
    (60, 40, 80),    # Right  — purple
]
_CAMERA_LABELS = config.camera_labels


def _make_mock_frame(camera_id: int, frame_counter: int) -> bytes:
    h, w = 360, 640
    bg = np.full((h, w, 3), _CAMERA_BG_COLORS[camera_id], dtype=np.uint8)

    # animated scan-line
    line_y = int((frame_counter * 3) % h)
    bg[max(0, line_y - 1): line_y + 1, :] = [v + 60 for v in _CAMERA_BG_COLORS[camera_id]]

    # noise overlay
    noise = np.random.randint(0, 18, (h, w, 3), dtype=np.uint8)
    bg = cv2.add(bg, noise)

    # label
    label = _CAMERA_LABELS[camera_id]
    cv2.putText(bg, f"CAM {camera_id} — {label}", (12, 34),
                cv2.FONT_HERSHEY_SIMPLEX, 0.9, (200, 200, 200), 2)
    cv2.putText(bg, datetime.now().strftime("%H:%M:%S.%f")[:-3],
                (12, h - 12), cv2.FONT_HERSHEY_SIMPLEX, 0.55, (160, 160, 160), 1)
    cv2.putText(bg, "MOCK", (w - 80, h - 12),
                cv2.FONT_HERSHEY_SIMPLEX, 0.55, (60, 60, 200), 1)

    ok, buf = cv2.imencode(".jpg", bg, [cv2.IMWRITE_JPEG_QUALITY, config.jpeg_quality])
    return buf.tobytes() if ok else b""


def _mock_camera_loop():
    """Generate synthetic camera frames only when explicitly requested."""
    frame_ctr = 0
    store.add_log("WARN", "GS_CAMERA_SOURCE=mock; showing synthetic camera feeds only", "camera")

    while True:
        for cam_id in range(4):
            frame = _make_mock_frame(cam_id, frame_ctr)
            store.set_camera_frame(cam_id, frame)
        frame_ctr += 1
        time.sleep(1.0 / config.camera_fps)


# ---------------------------------------------------------------------------
# UDP/RTP H264 camera receiver
# ---------------------------------------------------------------------------

def _gst_udp_h264_pipeline(port: int) -> str:
    return (
        f"udpsrc port={port} "
        'caps="application/x-rtp,media=video,encoding-name=H264,payload=96" ! '
        "rtph264depay ! "
        "h264parse ! "
        "avdec_h264 ! "
        "videoconvert ! "
        "appsink drop=true max-buffers=1 sync=false"
    )


def _gst_udp_jpeg_command(port: int) -> list[str]:
    return [
        "gst-launch-1.0",
        "-q",
        "udpsrc",
        f"port={port}",
        "caps=application/x-rtp,media=video,encoding-name=H264,payload=96",
        "!",
        "rtph264depay",
        "!",
        "h264parse",
        "!",
        "avdec_h264",
        "!",
        "videoconvert",
        "!",
        "jpegenc",
        f"quality={config.jpeg_quality}",
        "!",
        "fdsink",
        "fd=1",
    ]


def _gst_subprocess_camera_loop(camera_id: int, port: int):
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
        while proc.poll() is None:
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


def _udp_camera_loop(camera_id: int, port: int):
    label = config.camera_labels[camera_id] if camera_id < len(config.camera_labels) else str(camera_id)
    store.add_log("INFO", f"Opening UDP/RTP camera {camera_id} ({label}) on port {port}", "camera")

    while True:
        cap = cv2.VideoCapture(_gst_udp_h264_pipeline(port), cv2.CAP_GSTREAMER)
        if not cap.isOpened():
            store.add_log("WARN", f"OpenCV GStreamer receiver unavailable for camera {camera_id}; trying gst-launch", "camera")
            _gst_subprocess_camera_loop(camera_id, port)
            time.sleep(2.0)
            continue

        store.add_log("INFO", f"Camera {camera_id} receiving UDP/RTP H264 on port {port}", "camera")
        while True:
            ok, frame = cap.read()
            if not ok or frame is None:
                store.add_log("WARN", f"Camera {camera_id} stream lost on port {port}; reconnecting", "camera")
                cap.release()
                time.sleep(1.0)
                break

            ok, buf = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, config.jpeg_quality])
            if ok:
                store.set_camera_frame(camera_id, buf.tobytes())


def _start_udp_camera_receivers():
    if not config.camera_udp_ports:
        store.add_log("WARN", "GS_CAMERA_SOURCE=udp but no UDP ports configured", "camera")
        return

    for camera_id, port in enumerate(config.camera_udp_ports[:4]):
        t = threading.Thread(target=_udp_camera_loop, args=(camera_id, int(port)), daemon=True)
        t.start()


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
                    self.create_subscription(Image, topic, self._make_cam_cb(i), 5)

            self.create_subscription(NavSatFix, config.gnss_topic, self._gnss_cb, 10)
            self.create_subscription(BatteryState, config.battery_topic, self._battery_cb, 10)
            self.create_subscription(Float32, config.jetson_temp_topic, self._jetson_temp_cb, 5)
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
            self._camera360_capture_pub = self.create_publisher(UInt8MultiArray, config.led_arduino_360_capture_topic, 10)
            self._drive_clear_faults_pub = self.create_publisher(UInt8, config.drive_clear_faults_topic, 10)
            self._arm_clear_faults_pub = self.create_publisher(UInt8, config.arm_clear_faults_topic, 10)
            self.create_timer(1.0, self._payload_graph_health_cb)

        def _make_cam_cb(self, cam_id: int):
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

        def _gnss_cb(self, msg: NavSatFix):
            fix_map = {0: "NO_FIX", 1: "2D", 2: "3D"}
            with store._lock:
                store.gnss = {
                    "lat": round(msg.latitude, 7),
                    "lon": round(msg.longitude, 7),
                    "fix": fix_map.get(msg.status.status, "UNKNOWN"),
                    "satellites": 0,
                    "valid": msg.status.status >= 0,
                }

        def _battery_cb(self, msg: BatteryState):
            with store._lock:
                store.telemetry["soc"] = round(msg.percentage * 100, 1)
                store.telemetry["current"] = round(abs(msg.current), 2)
                store.telemetry["voltage"] = round(msg.voltage, 2)

        def _jetson_temp_cb(self, msg: Float32):
            with store._lock:
                store.system["jetson_temp"] = round(msg.data, 1)

        def _payload_temperature_cb(self, msg: Float32):
            with store._lock:
                store.payload_arduino["temperature_c"] = round(float(msg.data), 1)
                store.payload_arduino["last_update_s"] = round(time.time() - store._start_time, 1)

        def _payload_moisture_cb(self, msg: Float32):
            with store._lock:
                store.payload_arduino["moisture_pct"] = round(float(msg.data), 1)
                store.payload_arduino["last_update_s"] = round(time.time() - store._start_time, 1)

        def _led_arduino_status_cb(self, msg: Bool):
            with store._lock:
                store.led_controller["connected"] = msg.data
                store.led_controller["last_update_s"] = round(time.time() - store._start_time, 1)

        def _camera_360_status_cb(self, msg: Bool):
            with store._lock:
                store.led_controller["camera_360_connected"] = msg.data
                store.led_controller["camera_360_streaming"] = msg.data
                store.led_controller["last_update_s"] = round(time.time() - store._start_time, 1)

        def _payload_status_cb(self, msg: Bool):
            with store._lock:
                store.payload_connected = msg.data
                store.subsystems["payload"]["connected"] = msg.data
                store.payload_arduino["connected"] = msg.data

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

        def _payload_graph_health_cb(self):
            pub_active = bool(
                self.get_publishers_info_by_topic(config.payload_temperature_topic)
                or self.get_publishers_info_by_topic(config.payload_moisture_topic)
            )
            sub_active = bool(
                self.get_subscriptions_info_by_topic(config.elevator_topic)
                or self.get_subscriptions_info_by_topic(config.carousel_topic)
                or self.get_subscriptions_info_by_topic(config.auger_topic)
            )
            with store._lock:
                store.payload_arduino["publisher_active"] = pub_active
                store.payload_arduino["subscriber_active"] = sub_active
                store.payload_arduino["connected"] = store.payload_connected and pub_active and sub_active
                moisture = store.payload_arduino.get("moisture_pct")
                moisture_value = f"{moisture:.1f} %" if isinstance(moisture, (int, float)) else "--"
                store.subsystems["payload"]["metrics"] = [
                    {"label": "Arduino Pub", "value": "Active" if pub_active else "Offline"},
                    {"label": "Arduino Sub", "value": "Active" if sub_active else "Offline"},
                    {"label": "Moisture", "value": moisture_value},
                ]

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

        def publish_camera360_capture(self, command_bytes: list[int]):
            m = UInt8MultiArray()
            m.data = [int(b) & 0xFF for b in command_bytes]
            self._publish_redundant(self._camera360_capture_pub, m, "360 camera capture command")

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
        if config.camera_source == "udp":
            _start_udp_camera_receivers()
        elif config.camera_source == "mock":
            threading.Thread(target=_mock_camera_loop, daemon=True).start()
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

    def get_camera_frame(self, camera_id: int) -> Optional[bytes]:
        return store.get_camera_frame(camera_id)

    def get_telemetry(self) -> dict:
        return store.get_telemetry()

    def get_gnss(self) -> dict:
        return store.get_gnss()

    def get_system(self) -> dict:
        return store.get_system()

    def get_jetson_temperatures(self) -> dict:
        return _read_jetson_temperatures()

    def is_payload_connected(self) -> bool:
        return store.is_payload_connected()

    def is_arm_connected(self) -> bool:
        return store.is_arm_connected()

    def is_drive_connected(self) -> bool:
        return store.is_drive_connected()

    def get_subsystems(self) -> dict:
        return store.get_subsystems()

    def get_comms(self) -> dict:
        return store.get_comms()

    def get_payload_arduino(self) -> dict:
        return store.get_payload_arduino()

    def get_life_analysis(self) -> dict:
        return store.get_life_analysis()

    def get_led_controller(self) -> dict:
        return store.get_led_controller()

    def get_topic_overview(self) -> dict:
        return store.get_topic_overview()

    def get_motor_telemetry(self) -> dict:
        return store.get_motor_telemetry()

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

    def send_elevator(self, steps: int):
        direction = "UP" if steps > 0 else "DOWN"
        store.add_log("INFO", f"Elevator step command: {direction} {abs(steps)} steps", "payload")
        self._require_node().publish_elevator(float(steps))

    def send_carousel(self, steps: float):
        direction = "CW" if steps > 0 else "CCW"
        store.add_log("INFO", f"Carousel command: {direction} {abs(steps)} steps", "payload")
        self._require_node().publish_carousel(steps)

    def send_auger(self, speed: float, enabled: bool):
        store.add_log("INFO", f"Auger command: {'ON' if enabled else 'OFF'} @ {speed:.0f}%", "payload")
        self._require_node().publish_auger(speed if enabled else 0.0)

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

    def capture_360_image(self) -> dict:
        command_bytes = [0xA5, 0x36, 0x00, 0x5A, random.randint(0, 255), 0xC3]
        store.add_log(
            "INFO",
            "360 capture command bytes: " + " ".join(f"0x{b:02X}" for b in command_bytes),
            "led_arduino",
        )
        self._require_node().publish_camera360_capture(command_bytes)

        frames = []
        for camera_id in range(4):
            data = store.get_camera_frame(camera_id)
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
        cv2.putText(
            stitched,
            datetime.now().strftime("360 CAPTURE %Y-%m-%d %H:%M:%S"),
            (14, 34),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.75,
            (230, 230, 230),
            2,
        )
        ok, buf = cv2.imencode(".jpg", stitched, [cv2.IMWRITE_JPEG_QUALITY, config.jpeg_quality])
        if not ok:
            raise RuntimeError("Failed to encode stitched 360 image")

        image_data_url = "data:image/jpeg;base64," + base64.b64encode(buf.tobytes()).decode("ascii")
        result = {
            "captured_at": datetime.now(timezone.utc).isoformat(),
            "command_bytes": command_bytes,
            "image_data_url": image_data_url,
            "source_frames": len(frames),
        }
        with store._lock:
            store.camera_360_capture.update({
                "last_capture_s": time.time(),
                "last_command_bytes": command_bytes,
                "last_image_data_url": image_data_url,
            })
        store.add_log("INFO", f"360 image stitched from {len(frames)} camera frames", "camera360")
        return result


# Singleton
bridge = ROS2Bridge()
