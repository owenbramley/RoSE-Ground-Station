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

    # ROS2 topics — cameras. Defaults are intentionally empty; the UI discovers
    # physical cameras from the rover service instead of assuming fixed names.
    camera_topics: list = field(default_factory=lambda: [
        topic.strip()
        for topic in os.environ.get("GS_CAMERA_TOPICS", "").split(",")
        if topic.strip()
    ])
    camera_labels: list = field(default_factory=lambda: [
        label.strip()
        for label in os.environ.get("GS_CAMERA_LABELS", "").split(",")
        if label.strip()
    ])
    camera_source: str = field(default_factory=lambda: os.environ.get("GS_CAMERA_SOURCE", "udp").lower())
    camera_udp_ports: list[int] = field(default_factory=lambda: [
        int(port.strip())
        for port in os.environ.get("GS_CAMERA_UDP_PORTS", "5000,5001,5002,5003,5004,5005,5006,5007").split(",")
        if port.strip()
    ])
    rover_camera_service_urls: list[str] = field(default_factory=lambda: [
        url.strip().rstrip("/")
        for url in os.environ.get(
            "GS_ROVER_CAMERA_SERVICE_URLS",
            os.environ.get(
                "GS_ROVER_CAMERA_SERVICE_URL",
                "http://rover.local:8765,http://rose-rover.local:8765,http://urc-rover.local:8765",
            ),
        ).split(",")
        if url.strip()
    ])
    rover_camera_service_port: int = field(default_factory=lambda: int(os.environ.get("GS_ROVER_CAMERA_SERVICE_PORT", "8765")))
    rover_camera_max_fps: int = field(default_factory=lambda: int(os.environ.get("GS_CAMERA_MAX_FPS", "10")))
    rover_camera_min_fps: int = field(default_factory=lambda: int(os.environ.get("GS_CAMERA_MIN_FPS", "5")))
    rover_camera_max_width: int = field(default_factory=lambda: int(os.environ.get("GS_CAMERA_MAX_WIDTH", "4096")))
    rover_camera_max_height: int = field(default_factory=lambda: int(os.environ.get("GS_CAMERA_MAX_HEIGHT", "2160")))
    rover_camera_min_width: int = field(default_factory=lambda: int(os.environ.get("GS_CAMERA_MIN_WIDTH", "320")))
    rover_camera_min_height: int = field(default_factory=lambda: int(os.environ.get("GS_CAMERA_MIN_HEIGHT", "240")))
    rover_camera_bitrate: int = field(default_factory=lambda: int(os.environ.get("GS_CAMERA_BITRATE", "800")))
    rover_camera_min_bitrate: int = field(default_factory=lambda: int(os.environ.get("GS_CAMERA_MIN_BITRATE", "250")))
    rover_camera_max_total_bitrate: int = field(default_factory=lambda: int(os.environ.get("GS_CAMERA_MAX_TOTAL_BITRATE", "1600")))
    rover_camera_still_max_width: int = field(default_factory=lambda: int(os.environ.get("GS_CAMERA_STILL_MAX_WIDTH", "1920")))
    rover_camera_still_max_height: int = field(default_factory=lambda: int(os.environ.get("GS_CAMERA_STILL_MAX_HEIGHT", "1080")))
    camera_udp_buffer_size: int = field(default_factory=lambda: int(os.environ.get("GS_CAMERA_UDP_BUFFER_SIZE", "262144")))

    # ROS2 topics — sensors
    gnss_topic: str = "/gnss/fix"
    battery_topic: str = "/battery/state"
    imu_topic: str = "/imu/data"
    jetson_temp_topic: str = "/jetson/temperature"
    jetson_ip_topic: str = "/jetson/ip"
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

    # Ground-station network radio polling
    network_config_path: str = field(
        default_factory=lambda: os.environ.get("GS_NETWORK_CONFIG_PATH", "data/network_config.json")
    )
    camera_labels_path: str = field(
        default_factory=lambda: os.environ.get("GS_CAMERA_LABELS_PATH", "data/camera_labels.json")
    )
    rocket_snmp_community: str = field(default_factory=lambda: os.environ.get("GS_ROCKET_SNMP_COMMUNITY", "public"))
    rocket_poll_timeout_s: float = field(default_factory=lambda: float(os.environ.get("GS_ROCKET_POLL_TIMEOUT_S", "1.2")))

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

    # Browser MJPEG quality
    jpeg_quality: int = field(default_factory=lambda: int(os.environ.get("GS_CAMERA_JPEG_QUALITY", "60")))
    camera_fps: int = field(default_factory=lambda: int(os.environ.get("GS_CAMERA_BROWSER_FPS", "10")))


config = Config()
