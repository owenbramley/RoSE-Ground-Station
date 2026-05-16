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
  echo "[launch] No ROS2 installation found — running in mock mode"
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
