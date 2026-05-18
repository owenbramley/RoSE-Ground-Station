import os
from dataclasses import dataclass, field


@dataclass
class WarningLevel:
    warning: float
    critical: float


@dataclass
class Config:
    # Server
    host: str = "0.0.0.0"
    port: int = field(default_factory=lambda: int(os.environ.get("GS_PORT", "8000")))

    # ROS2 topics — cameras
    camera_topics: list = field(default_factory=lambda: [
        "/camera/front/image_raw",
        "/camera/rear/image_raw",
        "/camera/left/image_raw",
        "/camera/right/image_raw",
    ])
    camera_labels: list = field(default_factory=lambda: [
        "FRONT", "REAR", "LEFT", "RIGHT"
    ])
    camera_source: str = field(default_factory=lambda: os.environ.get("GS_CAMERA_SOURCE", "udp").lower())
    camera_udp_ports: list[int] = field(default_factory=lambda: [
        int(port.strip())
        for port in os.environ.get("GS_CAMERA_UDP_PORTS", "5000,5001,5002,5003").split(",")
        if port.strip()
    ])

    # ROS2 topics — sensors
    gnss_topic: str = "/gnss/fix"
    battery_topic: str = "/battery/state"
    imu_topic: str = "/imu/data"
    jetson_temp_topic: str = "/jetson/temperature"
    payload_temperature_topic: str = "/payload/arduino/temperature"
    payload_moisture_topic: str = "/payload/arduino/moisture"
    led_arduino_status_topic: str = "/led_arduino/status"
    led_arduino_360_camera_topic: str = "/led_arduino/camera_360/status"
    led_arduino_360_capture_topic: str = "/led_arduino/camera_360/capture_cmd"

    # ROS2 topics — subsystem status
    payload_status_topic: str = "/payload/status"
    arm_status_topic: str = "/arm/status"
    drive_status_topic: str = "/drive/status"
    drive_motor_telemetry_topic: str = "/drive/spark_motor_telemetry"
    arm_motor_telemetry_topic: str = "/arm/spark_motor_telemetry"
    drive_clear_faults_topic: str = "/drive/spark_clear_faults"
    arm_clear_faults_topic: str = "/arm/spark_clear_faults"

    # ROS2 topics — radio link health
    link_24ghz_topic: str = "/comms/link_24ghz"
    link_900mhz_topic: str = "/comms/link_900mhz"

    # ROS2 topics — control
    estop_topic: str = "/estop"
    elevator_topic: str = "/payload/elevator/cmd"
    carousel_topic: str = "/payload/carousel/cmd"
    auger_topic: str = "/payload/auger/cmd"

    # Command reliability. Commands are intentionally published more than once
    # because the rover link can be lossy and these messages are operator intent.
    command_publish_redundancy: int = 3
    command_publish_spacing_s: float = 0.025

    # Default warning levels (can be overridden per-session via API)
    soc_warning: WarningLevel = field(
        default_factory=lambda: WarningLevel(warning=30.0, critical=15.0)
    )
    current_warning: WarningLevel = field(
        default_factory=lambda: WarningLevel(warning=20.0, critical=30.0)
    )
    temperature_warning: WarningLevel = field(
        default_factory=lambda: WarningLevel(warning=60.0, critical=80.0)
    )

    # MJPEG quality
    jpeg_quality: int = 80
    camera_fps: int = 25


config = Config()
