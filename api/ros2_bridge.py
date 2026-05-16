"""
ROS2 bridge — integrates with rclpy when available, falls back to realistic
mock data so the UI can run without a live ROS2 environment.
"""
import math
import random
import threading
import time
import logging
from datetime import datetime, timezone
from typing import Optional

import cv2
import numpy as np

from .config import config

logger = logging.getLogger("ros2_bridge")

# ---------------------------------------------------------------------------
# Optional ROS2 imports
# ---------------------------------------------------------------------------
try:
    import rclpy
    from rclpy.node import Node
    from rclpy.executors import MultiThreadedExecutor
    from sensor_msgs.msg import Image, NavSatFix, BatteryState
    from std_msgs.msg import Bool, Float32
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
    logger.warning("rclpy not found — running in MOCK mode")


# ---------------------------------------------------------------------------
# Shared data store (thread-safe)
# ---------------------------------------------------------------------------

class _DataStore:
    def __init__(self):
        self._lock = threading.RLock()
        self.camera_frames: dict[int, Optional[bytes]] = {i: None for i in range(4)}
        self.telemetry: dict = {
            "soc": 87.0,
            "current": 8.5,
            "voltage": 24.2,
            "temperature": 42.0,
        }
        self.gnss: dict = {
            "lat": 43.6532,
            "lon": -79.3832,
            "fix": "3D",
            "satellites": 9,
            "valid": True,
        }
        self.payload_connected: bool = True
        self.arm_connected: bool = True
        self.system: dict = {
            "jetson_temp": 67.3,
            "cpu_percent": 34.0,
            "ram_used_gb": 6.2,
            "ram_total_gb": 16.0,
            "leds": [
                {"id": 0, "label": "Port Bow",   "r": 255, "g": 0,   "b": 0,   "on": True},
                {"id": 1, "label": "Starboard Bow","r": 0,   "g": 255, "b": 0,   "on": True},
                {"id": 2, "label": "Port Stern",  "r": 0,   "g": 0,   "b": 255, "on": False},
                {"id": 3, "label": "Star. Stern", "r": 255, "g": 255, "b": 255, "on": True},
            ],
            "imu_online": True,
            "gnss_module_online": True,
            "radio_signal": 72,
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
            return s

    def is_payload_connected(self) -> bool:
        with self._lock:
            return self.payload_connected

    def is_arm_connected(self) -> bool:
        with self._lock:
            return self.arm_connected

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


def _mock_loop():
    """Generate synthetic telemetry, GPS and camera frames."""
    frame_ctr = 0
    t0 = time.time()
    soc = 87.0
    temp = 42.0
    lat = 43.6532
    lon = -79.3832
    toggle_timer = time.time()

    store.add_log("INFO",  "Mock mode active — ROS2 not detected", "bridge")
    store.add_log("WARN",  "Camera topics not available — showing synthetic feeds", "bridge")
    store.add_log("INFO",  "Telemetry simulation started", "bridge")

    while True:
        elapsed = time.time() - t0
        t = time.time()

        # --- cameras ---------------------------------------------------------
        for cam_id in range(4):
            frame = _make_mock_frame(cam_id, frame_ctr)
            store.set_camera_frame(cam_id, frame)
        frame_ctr += 1

        # --- telemetry -------------------------------------------------------
        soc = max(0.0, soc - 0.003 + random.uniform(-0.001, 0.001))
        temp = 42.0 + 8.0 * math.sin(elapsed / 60) + random.uniform(-0.5, 0.5)
        current = 8.5 + 3.0 * math.sin(elapsed / 12) + random.uniform(-0.3, 0.3)
        voltage = 24.2 - (87.0 - soc) * 0.05 + random.uniform(-0.05, 0.05)
        with store._lock:
            store.telemetry = {
                "soc": round(soc, 2),
                "current": round(abs(current), 2),
                "voltage": round(voltage, 2),
                "temperature": round(temp, 1),
            }

        # --- GNSS ------------------------------------------------------------
        lat += random.uniform(-0.000005, 0.000005)
        lon += random.uniform(-0.000005, 0.000005)
        with store._lock:
            store.gnss = {
                "lat": round(lat, 7),
                "lon": round(lon, 7),
                "fix": "3D",
                "satellites": random.randint(8, 12),
                "valid": True,
            }

        # --- system ----------------------------------------------------------
        with store._lock:
            store.system["jetson_temp"] = round(67.0 + 5 * math.sin(elapsed / 90) + random.uniform(-0.3, 0.3), 1)
            store.system["cpu_percent"] = round(34.0 + 20 * abs(math.sin(elapsed / 30)), 1)

        # --- subsystem toggle (simulate connect/disconnect) ------------------
        if t - toggle_timer > 45:
            toggle_timer = t
            with store._lock:
                store.payload_connected = not store.payload_connected
            state = "connected" if store.is_payload_connected() else "disconnected"
            store.add_log("INFO", f"Payload subsystem {state}", "payload")

        # --- occasional log entries ------------------------------------------
        if int(elapsed) % 10 == 0 and int(elapsed) > 0:
            roll = random.random()
            if roll < 0.05:
                store.add_log("ERROR", "CAN bus timeout on motor controller 2", "can")
            elif roll < 0.15:
                store.add_log("WARN", f"Battery temperature elevated: {temp:.1f}°C", "battery")
            elif roll < 0.30:
                store.add_log("DEBUG", f"GNSS fix: {lat:.6f}, {lon:.6f}", "gnss")
            else:
                store.add_log("INFO", f"Telemetry tick — SOC {soc:.1f}%  I={abs(current):.1f}A  T={temp:.1f}°C", "telemetry")

        time.sleep(1.0 / config.camera_fps)


# ---------------------------------------------------------------------------
# ROS2 node (only instantiated when rclpy is available)
# ---------------------------------------------------------------------------

if ROS2_AVAILABLE:
    class _RoverNode(Node):
        def __init__(self):
            super().__init__("rose_ground_station")
            store.add_log("INFO", "ROS2 node initialised", "ros2")

            for i, topic in enumerate(config.camera_topics):
                self.create_subscription(Image, topic, self._make_cam_cb(i), 5)

            self.create_subscription(NavSatFix, config.gnss_topic, self._gnss_cb, 10)
            self.create_subscription(BatteryState, config.battery_topic, self._battery_cb, 10)
            self.create_subscription(Float32, config.jetson_temp_topic, self._jetson_temp_cb, 5)
            self.create_subscription(Bool, config.payload_status_topic, self._payload_status_cb, 5)
            self.create_subscription(Bool, config.arm_status_topic, self._arm_status_cb, 5)

            # publishers
            self._estop_pub = self.create_publisher(Bool, config.estop_topic, 1)
            self._elevator_pub = self.create_publisher(Float32, config.elevator_topic, 10)
            self._carousel_pub = self.create_publisher(Float32, config.carousel_topic, 10)
            self._auger_pub = self.create_publisher(Float32, config.auger_topic, 10)

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

        def _payload_status_cb(self, msg: Bool):
            with store._lock:
                store.payload_connected = msg.data

        def _arm_status_cb(self, msg: Bool):
            with store._lock:
                store.arm_connected = msg.data

        # --- publishers -------------------------------------------------------
        def publish_estop(self, active: bool = True):
            m = Bool()
            m.data = active
            self._estop_pub.publish(m)
            store.add_log("WARN" if active else "INFO",
                          f"E-STOP {'ACTIVATED' if active else 'RESET'}", "estop")

        def publish_elevator(self, mm: float):
            m = Float32()
            m.data = float(mm)
            self._elevator_pub.publish(m)

        def publish_carousel(self, steps: float):
            m = Float32()
            m.data = float(steps)
            self._carousel_pub.publish(m)

        def publish_auger(self, speed: float):
            m = Float32()
            m.data = float(speed)
            self._auger_pub.publish(m)


# ---------------------------------------------------------------------------
# Public bridge object
# ---------------------------------------------------------------------------

class ROS2Bridge:
    def __init__(self):
        self._node: Optional["_RoverNode"] = None  # type: ignore[name-defined]
        if ROS2_AVAILABLE:
            self._init_ros2()
        else:
            t = threading.Thread(target=_mock_loop, daemon=True)
            t.start()

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
            store.add_log("ERROR", f"ROS2 init failed: {e} — switching to mock", "bridge")
            t = threading.Thread(target=_mock_loop, daemon=True)
            t.start()

    # --- data accessors -------------------------------------------------------

    def get_camera_frame(self, camera_id: int) -> Optional[bytes]:
        return store.get_camera_frame(camera_id)

    def get_telemetry(self) -> dict:
        return store.get_telemetry()

    def get_gnss(self) -> dict:
        return store.get_gnss()

    def get_system(self) -> dict:
        return store.get_system()

    def is_payload_connected(self) -> bool:
        return store.is_payload_connected()

    def is_arm_connected(self) -> bool:
        return store.is_arm_connected()

    def get_logs_since(self, since_id: int = 0) -> list:
        return store.get_logs_since(since_id)

    def add_log(self, level: str, message: str, source: str = "api"):
        return store.add_log(level, message, source)

    # --- commands -------------------------------------------------------------

    def send_estop(self, active: bool = True):
        if self._node:
            self._node.publish_estop(active)
        else:
            store.add_log("WARN" if active else "INFO",
                          f"E-STOP {'ACTIVATED' if active else 'RESET'} (mock)", "estop")

    def send_elevator(self, mm: float):
        direction = "UP" if mm > 0 else "DOWN"
        store.add_log("INFO", f"Elevator command: {direction} {abs(mm):.1f} mm", "payload")
        if self._node:
            self._node.publish_elevator(mm)

    def send_carousel(self, steps: float):
        direction = "CW" if steps > 0 else "CCW"
        store.add_log("INFO", f"Carousel command: {direction} {abs(steps)} steps", "payload")
        if self._node:
            self._node.publish_carousel(steps)

    def send_auger(self, speed: float, enabled: bool):
        store.add_log("INFO", f"Auger command: {'ON' if enabled else 'OFF'} @ {speed:.0f}%", "payload")
        if self._node:
            self._node.publish_auger(speed if enabled else 0.0)


# Singleton
bridge = ROS2Bridge()
