#!/usr/bin/env bash
# RoSE Ground Station — server launch script
set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

# Optional: source ROS2 environment if available
if [ -f "/opt/ros/humble/setup.bash" ]; then
  source /opt/ros/humble/setup.bash
  echo "[launch] ROS2 Humble sourced"
elif [ -f "/opt/ros/iron/setup.bash" ]; then
  source /opt/ros/iron/setup.bash
  echo "[launch] ROS2 Iron sourced"
else
  echo "[launch] No ROS2 installation found — live ROS2 topics unavailable"
fi

# Source rover workspace overlays when present so custom messages such as
# gnc_interfaces/SparkMotorTelemetry are available to the ground station.
if [ -n "${URC_ROS_WS:-}" ] && [ -f "${URC_ROS_WS}/install/setup.bash" ]; then
  source "${URC_ROS_WS}/install/setup.bash"
  echo "[launch] URC ROS2 workspace sourced from ${URC_ROS_WS}"
elif [ -f "/home/roselab/urc_2026/install/setup.bash" ]; then
  source /home/roselab/urc_2026/install/setup.bash
  echo "[launch] URC ROS2 workspace sourced"
fi

# Activate venv if present
if [ -d "venv" ]; then
  source venv/bin/activate
  echo "[launch] Virtual environment activated"
fi

echo "[launch] Starting RoSE Ground Station API on http://0.0.0.0:${GS_PORT:-8000}"
exec python3 -m uvicorn api.main:app \
  --host 0.0.0.0 \
  --port "${GS_PORT:-8000}" \
  --log-level info
