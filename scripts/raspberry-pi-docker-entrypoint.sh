#!/usr/bin/env bash
set -euo pipefail

source /opt/ros/humble/setup.bash

if [ -n "${URC_ROS_WS:-}" ] && [ -f "${URC_ROS_WS}/install/setup.bash" ]; then
  source "${URC_ROS_WS}/install/setup.bash"
  echo "[pi-entrypoint] URC ROS2 workspace sourced from ${URC_ROS_WS}"
fi

start_joy_node() {
  local device="$1"
  local topic="$2"
  local name="$3"

  if [ -e "$device" ]; then
    echo "[pi-entrypoint] Starting ${name} controller on ${device} -> ${topic}"
    ros2 run joy joy_node --ros-args \
      -r joy:="${topic}" \
      -p dev:="${device}" \
      -p autorepeat_rate:=20.0 \
      -p deadzone:=0.08 &
  else
    echo "[pi-entrypoint] ${name} controller device not found: ${device}"
  fi
}

start_joy_node "${DRIVE_CONTROLLER:-/dev/input/js0}" "${DRIVE_JOY_TOPIC:-/drive/joy}" "drive"
start_joy_node "${ARM_CONTROLLER:-/dev/input/js1}" "${ARM_JOY_TOPIC:-/arm/joy}" "arm"

cd /workspace/rose-ground-station
exec ./launch.sh
