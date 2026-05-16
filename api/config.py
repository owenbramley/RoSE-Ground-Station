from dataclasses import dataclass, field


@dataclass
class WarningLevel:
    warning: float
    critical: float


@dataclass
class Config:
    # Server
    host: str = "0.0.0.0"
    port: int = 8000

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

    # ROS2 topics — sensors
    gnss_topic: str = "/gnss/fix"
    battery_topic: str = "/battery/state"
    imu_topic: str = "/imu/data"
    jetson_temp_topic: str = "/jetson/temperature"

    # ROS2 topics — subsystem status
    payload_status_topic: str = "/payload/status"
    arm_status_topic: str = "/arm/status"

    # ROS2 topics — control
    estop_topic: str = "/estop"
    elevator_topic: str = "/payload/elevator/cmd"
    carousel_topic: str = "/payload/carousel/cmd"
    auger_topic: str = "/payload/auger/cmd"

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
