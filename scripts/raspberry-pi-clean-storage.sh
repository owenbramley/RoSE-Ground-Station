#!/usr/bin/env bash
set -euo pipefail

URC_ROS_WS="${URC_ROS_WS:-/home/pi/urc_2026}"

echo "[pi-clean] Cleaning Docker stopped containers and dangling data"
docker container prune -f
docker image prune -f
docker builder prune -f --filter until=168h

if [ -d "${HOME}/.ros/log" ]; then
  echo "[pi-clean] Removing ROS logs older than 14 days"
  find "${HOME}/.ros/log" -type f -mtime +14 -delete
  find "${HOME}/.ros/log" -type d -empty -delete
fi

if [ -f "${URC_ROS_WS}/install/setup.bash" ]; then
  echo "[pi-clean] Removing colcon build/log directories for ${URC_ROS_WS}"
  rm -rf "${URC_ROS_WS}/build" "${URC_ROS_WS}/log"
else
  echo "[pi-clean] Keeping ${URC_ROS_WS}/build because install/setup.bash does not exist yet"
fi

echo "[pi-clean] Done"
