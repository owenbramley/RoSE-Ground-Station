"""
ROS2 bridge — integrates with rclpy when available, falls back to realistic
mock data so the UI can run without a live ROS2 environment.
"""
import math
import random
import threading
import time
import logging
import base64
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
        self.drive_connected: bool = True
        self.payload_arduino: dict = {
            "connected": True,
            "publisher_active": True,
            "subscriber_active": True,
            "temperature_c": 23.8,
            "moisture_pct": 38.0,
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
                "status": "nominal",
                "connected": True,
                "summary": "Science package online",
                "metrics": [
                    {"label": "Arduino Pub", "value": "Active"},
                    {"label": "Arduino Sub", "value": "Active"},
                    {"label": "Moisture", "value": "38.0 %"},
                ],
            },
            "arm": {
                "label": "Arm",
                "status": "nominal",
                "connected": True,
                "summary": "Manipulator enabled",
                "metrics": [
                    {"label": "Shoulder", "value": "0.0 deg"},
                    {"label": "Elbow", "value": "0.0 deg"},
                    {"label": "Wrist", "value": "0.0 deg"},
                ],
            },
            "drive": {
                "label": "Drive / Chassis",
                "status": "nominal",
                "connected": True,
                "summary": "Mobility controllers online",
                "metrics": [
                    {"label": "Mode", "value": "Manual"},
                    {"label": "Command", "value": "External"},
                    {"label": "CAN", "value": "Nominal"},
                ],
            },
        }
        self.motor_telemetry: dict = {"drive": {}, "arm": {}}
        self.comms: dict = {
            "link_24ghz": {"label": "2.4 GHz", "stability": 86, "rssi_dbm": -58, "latency_ms": 18, "status": "nominal"},
            "link_900mhz": {"label": "900 MHz", "stability": 78, "rssi_dbm": -72, "latency_ms": 42, "status": "nominal"},
        }
        self.led_controller: dict = {
            "connected": True,
            "publisher_active": True,
            "subscriber_active": True,
            "camera_360_connected": True,
            "camera_360_streaming": True,
            "camera_360_mode": "panorama",
            "last_update_s": 0,
            "leds": [
                {"id": 0, "label": "Port Bow", "r": 255, "g": 0, "b": 0, "on": True},
                {"id": 1, "label": "Starboard Bow", "r": 0, "g": 255, "b": 0, "on": True},
                {"id": 2, "label": "Port Stern", "r": 0, "g": 0, "b": 255, "on": False},
                {"id": 3, "label": "Star. Stern", "r": 255, "g": 255, "b": 255, "on": True},
            ],
        }
        self.system: dict = {
            "jetson_temp": 67.3,
            "cpu_percent": 34.0,
            "ram_used_gb": 6.2,
            "ram_total_gb": 16.0,
            "imu_online": True,
            "gnss_module_online": True,
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
            "mode": "live" if ROS2_AVAILABLE else "mock",
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
            mock_motors = {
                "drive": [
                    (1, "front_right"), (2, "back_right"), (3, "front_left"), (4, "back_left"),
                ],
                "arm": [
                    (5, "joint1"), (6, "joint2"), (7, "joint3"), (8, "diff_wrist_1"), (9, "diff_wrist_2"),
                ],
            }
            for group, motors in mock_motors.items():
                store.motor_telemetry.setdefault(group, {})
                for idx, (device_id, name) in enumerate(motors):
                    fault = int(elapsed / 90) % 5 == 2 and idx == 1
                    connected = not (int(elapsed / 70) % 6 == 3 and idx == 0)
                    motor_temp = temp + idx * 0.8 + (2.0 if group == "arm" else 0.0)
                    motor_current = abs(current) / max(1, len(motors)) + idx * 0.25
                    store.motor_telemetry[group][device_id] = {
                        "device_id": device_id,
                        "name": name,
                        "group": group,
                        "connected": connected,
                        "applied_output": round(0.12 * math.sin(elapsed / 8 + idx), 3),
                        "motor_velocity_rpm": round(300 * math.sin(elapsed / 9 + idx), 1),
                        "motor_temperature_c": round(motor_temp, 1),
                        "bus_voltage_v": round(voltage, 2),
                        "motor_current_a": round(motor_current, 2),
                        "faults": 1 << 1 if fault else 0,
                        "sticky_faults": 1 << 9 if fault else 0,
                        "fault_names": ["Overcurrent"] if fault else [],
                        "sticky_fault_names": ["HasReset"] if fault else [],
                        "last_update_s": time.time(),
                        "status": "critical" if fault or not connected else "nominal",
                    }
            payload_temp = 23.8 + 2.5 * math.sin(elapsed / 28) + random.uniform(-0.2, 0.2)
            payload_moisture = 38.0 + 6.0 * math.sin(elapsed / 37) + random.uniform(-0.8, 0.8)
            store.payload_arduino.update({
                "connected": store.payload_connected,
                "publisher_active": store.payload_connected,
                "subscriber_active": store.payload_connected,
                "temperature_c": round(payload_temp, 1),
                "moisture_pct": round(max(0, min(100, payload_moisture)), 1),
                "last_update_s": round(elapsed, 1),
            })
            store.led_controller.update({
                "connected": True,
                "publisher_active": True,
                "subscriber_active": True,
                "camera_360_connected": True,
                "camera_360_streaming": (int(elapsed / 20) % 6) != 5,
                "camera_360_mode": "panorama" if int(elapsed / 30) % 2 == 0 else "inspection",
                "last_update_s": round(elapsed, 1),
            })
            brightness = int(128 + 127 * abs(math.sin(elapsed / 10)))
            store.led_controller["leds"] = [
                {"id": 0, "label": "Port Bow", "r": brightness, "g": 24, "b": 20, "on": True},
                {"id": 1, "label": "Starboard Bow", "r": 20, "g": brightness, "b": 34, "on": True},
                {"id": 2, "label": "Port Stern", "r": 20, "g": 52, "b": brightness, "on": int(elapsed / 12) % 2 == 0},
                {"id": 3, "label": "Star. Stern", "r": brightness, "g": brightness, "b": brightness, "on": True},
            ]

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
            link_24 = max(0, min(100, 86 + 8 * math.sin(elapsed / 35) + random.uniform(-3, 3)))
            link_900 = max(0, min(100, 78 + 10 * math.sin(elapsed / 48) + random.uniform(-4, 4)))
            store.comms["link_24ghz"].update({
                "stability": round(link_24),
                "rssi_dbm": round(-48 - (100 - link_24) * 0.55),
                "latency_ms": round(14 + (100 - link_24) * 0.45),
                "status": "nominal" if link_24 >= 70 else "degraded" if link_24 >= 45 else "critical",
            })
            store.comms["link_900mhz"].update({
                "stability": round(link_900),
                "rssi_dbm": round(-54 - (100 - link_900) * 0.7),
                "latency_ms": round(24 + (100 - link_900) * 0.8),
                "status": "nominal" if link_900 >= 70 else "degraded" if link_900 >= 45 else "critical",
            })
            store.subsystems["payload"]["connected"] = store.payload_connected
            store.subsystems["arm"]["connected"] = store.arm_connected
            store.subsystems["drive"]["connected"] = store.drive_connected
            store.subsystems["payload"]["metrics"] = [
                {"label": "Arduino Pub", "value": "Active" if store.payload_arduino["publisher_active"] else "Offline"},
                {"label": "Arduino Sub", "value": "Active" if store.payload_arduino["subscriber_active"] else "Offline"},
                {"label": "Moisture", "value": f"{store.payload_arduino['moisture_pct']:.1f} %"},
            ]
            store.subsystems["arm"]["metrics"] = [
                {"label": "Mode", "value": "Status only"},
                {"label": "Power", "value": "Online" if store.arm_connected else "Offline"},
                {"label": "Control", "value": "Disabled"},
            ]
            store.subsystems["drive"]["metrics"] = [
                {"label": "Mode", "value": "Manual"},
                {"label": "Command", "value": "External"},
                {"label": "CAN", "value": "Nominal"},
            ]

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
                store.subsystems["payload"]["metrics"] = [
                    {"label": "Arduino Pub", "value": "Active" if pub_active else "Offline"},
                    {"label": "Arduino Sub", "value": "Active" if sub_active else "Offline"},
                    {"label": "Moisture", "value": f"{store.payload_arduino['moisture_pct']:.1f} %"},
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

    def send_estop(self, active: bool = True):
        if self._node:
            self._node.publish_estop(active)
        else:
            store.add_log("WARN" if active else "INFO",
                          f"E-STOP {'ACTIVATED' if active else 'RESET'} (mock)", "estop")

    def send_elevator(self, steps: int):
        direction = "UP" if steps > 0 else "DOWN"
        store.add_log("INFO", f"Elevator step command: {direction} {abs(steps)} steps", "payload")
        if self._node:
            self._node.publish_elevator(float(steps))

    def send_carousel(self, steps: float):
        direction = "CW" if steps > 0 else "CCW"
        store.add_log("INFO", f"Carousel command: {direction} {abs(steps)} steps", "payload")
        if self._node:
            self._node.publish_carousel(steps)

    def send_auger(self, speed: float, enabled: bool):
        store.add_log("INFO", f"Auger command: {'ON' if enabled else 'OFF'} @ {speed:.0f}%", "payload")
        if self._node:
            self._node.publish_auger(speed if enabled else 0.0)

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
        if self._node:
            self._node.publish_clear_motor_faults(group, device_id)
        else:
            with store._lock:
                motors = store.motor_telemetry.get(group, {})
                targets = motors.values() if device_id == 0 else [motors.get(device_id)]
                for motor in targets:
                    if motor:
                        motor["sticky_faults"] = 0
                        motor["sticky_fault_names"] = []
                        motor["faults"] = 0
                        motor["fault_names"] = []
                        motor["status"] = "nominal" if motor.get("connected") else "critical"

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
        if self._node:
            self._node.publish_camera360_capture(command_bytes)

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
