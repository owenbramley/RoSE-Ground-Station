# RoSE Ground Station Raspberry Pi Setup

This guide installs the RoSE ground station and the `urc_2026` ROS 2 workspace on Raspberry Pi OS Lite, then starts everything automatically on boot.

Raspberry Pi OS is Debian-based, while ROS 2 Humble is normally packaged for Ubuntu 22.04. For Raspberry Pi OS Lite, the most reliable setup is Docker with the official ROS 2 Humble image.

## Assumptions

- Raspberry Pi OS Lite is already installed.
- Use the 64-bit Raspberry Pi OS image.
- The Pi uses Ethernet only.
- The Pi username is `pi`. If your username is different, replace `pi` in the commands.
- The ground station repo will live at `/home/pi/rose-ground-station`.
- The rover ROS workspace will live at `/home/pi/urc_2026`.
- The rover and Pi are on the same Ethernet network.

## 1. Update the Pi

```bash
sudo apt update
sudo apt full-upgrade -y
sudo apt install -y git curl joystick evtest avahi-daemon
sudo reboot
```

## 2. Set a Hostname

```bash
sudo hostnamectl set-hostname rose-groundstation
sudo nano /etc/hosts
```

Make sure this line exists:

```text
127.0.1.1 rose-groundstation
```

Enable `.local` discovery:

```bash
sudo systemctl enable --now avahi-daemon
```

After reboot, the web UI should be reachable as:

```text
http://rose-groundstation.local:8000
```

## 3. Disable Wi-Fi and Bluetooth

Edit the Raspberry Pi firmware config:

```bash
sudo nano /boot/firmware/config.txt
```

Add these lines at the bottom:

```ini
dtoverlay=disable-wifi
dtoverlay=disable-bt
```

Disable Bluetooth services:

```bash
sudo systemctl disable --now bluetooth 2>/dev/null || true
sudo systemctl disable --now hciuart 2>/dev/null || true
sudo systemctl mask bluetooth 2>/dev/null || true
```

Reboot:

```bash
sudo reboot
```

## 4. Install Docker

```bash
curl -fsSL https://get.docker.com | sh
sudo usermod -aG docker,input,dialout,plugdev pi
sudo reboot
```

After reboot, verify Docker works:

```bash
docker --version
docker run --rm hello-world
```

## 5. Clone the Repositories

Clone the ground station:

```bash
cd /home/pi
git clone <GROUND_STATION_REPO_URL> rose-ground-station
cd /home/pi/rose-ground-station
chmod +x launch.sh scripts/raspberry-pi-docker-entrypoint.sh scripts/raspberry-pi-clean-storage.sh
```

Clone `urc_2026`:

```bash
cd /home/pi
git clone <URC_2026_REPO_URL> urc_2026
```

Replace the two repo URLs with the real GitHub URLs.

## 6. Configure the Environment

Copy the included environment file:

```bash
sudo cp /home/pi/rose-ground-station/raspberry-pi.env /etc/rose-ground-station.env
sudo nano /etc/rose-ground-station.env
```

At minimum, check these values:

```bash
ROS_DOMAIN_ID=0
ROS_LOCALHOST_ONLY=0
URC_ROS_WS=/home/pi/urc_2026
DRIVE_CONTROLLER=/dev/input/js0
ARM_CONTROLLER=/dev/input/js1
```

`ROS_DOMAIN_ID` must match the rover.

The camera list is discovered dynamically. The ground station first uses any rover IP published on ROS, then tries these rover camera service hostnames:

```text
http://rover.local:8765
http://rose-rover.local:8765
http://urc-rover.local:8765
```

You do not need to declare camera names, resolutions, individual camera URLs, or a hardcoded rover IP in the env file. If your rover uses a different hostname, set `GS_ROVER_CAMERA_SERVICE_URLS` in `/etc/rose-ground-station.env`.

## 7. Build the Docker Image

```bash
cd /home/pi/rose-ground-station
docker build -f Dockerfile.raspberry-pi -t rose-ground-station:pi .
```

## 8. Build the `urc_2026` ROS 2 Workspace

Build it inside a ROS 2 Humble Docker container:

```bash
docker run -it --rm \
  --net=host \
  --env-file /etc/rose-ground-station.env \
  -v /home/pi/urc_2026:/home/pi/urc_2026 \
  -v /home/pi/rose-ground-station:/workspace/rose-ground-station \
  rose-ground-station:pi \
  bash -lc "source /opt/ros/humble/setup.bash && cd /home/pi/urc_2026 && colcon build"
```

If the workspace has extra package dependencies, install them in the image or run `rosdep` from inside the container before building.

Do not use `--symlink-install` on the Pi if you want automatic cleanup. A normal `colcon build` makes `install/` self-contained, so `build/` and `log/` can be removed afterward.

Optional dependency install before building:

```bash
docker run -it --rm \
  --net=host \
  --env-file /etc/rose-ground-station.env \
  -v /home/pi/urc_2026:/home/pi/urc_2026 \
  -v /home/pi/rose-ground-station:/workspace/rose-ground-station \
  rose-ground-station:pi \
  bash -lc "source /opt/ros/humble/setup.bash && cd /home/pi/urc_2026 && rosdep update && rosdep install --from-paths src --ignore-src -r -y"
```

## 9. Test Controllers

Plug in both controllers, then run:

```bash
ls /dev/input/js*
jstest /dev/input/js0
jstest /dev/input/js1
```

For stable controller names, check:

```bash
ls -l /dev/input/by-id/
```

If stable paths exist, edit `/etc/rose-ground-station.env` and use those instead of `/dev/input/js0` and `/dev/input/js1`:

```bash
DRIVE_CONTROLLER=/dev/input/by-id/<drive-controller>
ARM_CONTROLLER=/dev/input/by-id/<arm-controller>
```

## 10. Test the Ground Station Manually

```bash
docker run -it --rm \
  --name rose-ground-station \
  --net=host \
  --privileged \
  --env-file /etc/rose-ground-station.env \
  -v /dev/input:/dev/input \
  -v /home/pi/urc_2026:/home/pi/urc_2026 \
  -v /home/pi/rose-ground-station:/workspace/rose-ground-station \
  rose-ground-station:pi
```

From a laptop on the same Ethernet network, open:

```text
http://rose-groundstation.local:8000
```

Check that ROS 2 can see rover topics from inside the container:

```bash
docker exec -it rose-ground-station bash
source /opt/ros/humble/setup.bash
source /home/pi/urc_2026/install/setup.bash
ros2 topic list
```

Stop the manual test:

```bash
docker stop rose-ground-station
```

## 11. Start on Boot with systemd

Create the service:

```bash
sudo nano /etc/systemd/system/rose-ground-station.service
```

Paste:

```ini
[Unit]
Description=RoSE Ground Station Docker Service
After=docker.service network-online.target
Wants=docker.service network-online.target

[Service]
Type=simple
User=pi
Group=docker
Restart=always
RestartSec=5
EnvironmentFile=/etc/rose-ground-station.env
ExecStartPre=-/usr/bin/docker rm -f rose-ground-station
ExecStartPre=/home/pi/rose-ground-station/scripts/raspberry-pi-clean-storage.sh
ExecStart=/usr/bin/docker run --rm \
  --name rose-ground-station \
  --net=host \
  --privileged \
  --env-file /etc/rose-ground-station.env \
  -v /dev/input:/dev/input \
  -v /home/pi/urc_2026:/home/pi/urc_2026 \
  -v /home/pi/rose-ground-station:/workspace/rose-ground-station \
  rose-ground-station:pi
ExecStop=/usr/bin/docker stop rose-ground-station

[Install]
WantedBy=multi-user.target
```

Enable and start it:

```bash
sudo systemctl daemon-reload
sudo systemctl enable rose-ground-station
sudo systemctl start rose-ground-station
```

Check logs:

```bash
journalctl -u rose-ground-station -f
```

The cleanup step removes stopped Docker containers, dangling Docker images, old Docker build cache, old ROS logs, and the `urc_2026/build` plus `urc_2026/log` directories after a valid `urc_2026/install/setup.bash` exists. It does not delete `urc_2026/src` or `urc_2026/install`.

## 12. Optional Daily Storage Cleanup

The boot service already runs cleanup before starting. If the Pi stays on for long periods, add a daily cleanup timer too.

Create the service:

```bash
sudo nano /etc/systemd/system/rose-ground-station-clean.service
```

Paste:

```ini
[Unit]
Description=RoSE Ground Station Storage Cleanup

[Service]
Type=oneshot
User=pi
Group=docker
EnvironmentFile=/etc/rose-ground-station.env
ExecStart=/home/pi/rose-ground-station/scripts/raspberry-pi-clean-storage.sh
```

Create the timer:

```bash
sudo nano /etc/systemd/system/rose-ground-station-clean.timer
```

Paste:

```ini
[Unit]
Description=Daily RoSE Ground Station Storage Cleanup

[Timer]
OnCalendar=daily
Persistent=true

[Install]
WantedBy=timers.target
```

Enable it:

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now rose-ground-station-clean.timer
```

## 13. Updating Later

Update the ground station:

```bash
cd /home/pi/rose-ground-station
git pull
docker build -f Dockerfile.raspberry-pi -t rose-ground-station:pi .
sudo systemctl restart rose-ground-station
```

Update `urc_2026`:

```bash
cd /home/pi/urc_2026
git pull
docker run -it --rm \
  --net=host \
  --env-file /etc/rose-ground-station.env \
  -v /home/pi/urc_2026:/home/pi/urc_2026 \
  -v /home/pi/rose-ground-station:/workspace/rose-ground-station \
  rose-ground-station:pi \
  bash -lc "source /opt/ros/humble/setup.bash && cd /home/pi/urc_2026 && colcon build"
sudo systemctl restart rose-ground-station
```

After a successful `urc_2026` rebuild, you can immediately reclaim build space:

```bash
/home/pi/rose-ground-station/scripts/raspberry-pi-clean-storage.sh
```

## Notes

- The ground station joins the rover ROS 2 network when `ROS_DOMAIN_ID` matches and both machines can reach each other over Ethernet.
- Two controllers are supported. The entrypoint starts one `joy_node` for `/drive/joy` and one for `/arm/joy`.
- If `/dev/input/js0` and `/dev/input/js1` swap, use `/dev/input/by-id/` paths in `/etc/rose-ground-station.env`.
- If the web UI loads but ROS data is missing, first check `ROS_DOMAIN_ID`, Ethernet IP reachability, and whether `/home/pi/urc_2026/install/setup.bash` exists.
