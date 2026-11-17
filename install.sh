#!/bin/bash
set -euo pipefail
if [ "$(id -u)" -ne 0 ]; then
  echo 'Run this installer with sudo.' >&2
  exit 1
fi
cd -- "$(dirname -- "$0")"
if [ ! -f /etc/debian_version ]; then
  echo 'This installer targets Raspberry Pi OS Lite or Debian with systemd.' >&2
  exit 1
fi
if [ -f /etc/pi-nas-drop/config.json ]; then
  echo 'Already configured. This installer will not overwrite your configuration.' >&2
  exit 1
fi
apt-get update
apt-get install -y --no-install-recommends python3 gunicorn cifs-utils ntfs-3g exfatprogs sudo util-linux python3-gphoto2 gphoto2 libmtp-runtime libimage-exiftool-perl
id pidrop >/dev/null 2>&1 || useradd --system --user-group --home-dir /var/lib/pi-nas-drop --shell /usr/sbin/nologin pidrop
install -d -o root -g root -m 0755 /opt/pi-nas-drop /opt/pi-nas-drop/web
install -d -o root -g pidrop -m 0750 /etc/pi-nas-drop
install -d -o pidrop -g pidrop -m 0700 /var/lib/pi-nas-drop
install -d -o root -g root -m 0555 /mnt/pidropnas
install -o root -g root -m 0644 app.py core.py drive_helper.py camera.py camera_worker.py organize.py metadata_worker.py cleanup.py /opt/pi-nas-drop/
install -o root -g root -m 0644 web/index.html web/style.css web/app.js /opt/pi-nas-drop/web/
install -o root -g root -m 0644 udev/99-pitransfer-cameras.rules /etc/udev/rules.d/
udevadm control --reload-rules
python3 setup_config.py
install -o root -g root -m 0644 systemd/pi-nas-drop.service /etc/systemd/system/pi-nas-drop.service
printf '%s\n' 'pidrop ALL=(root) NOPASSWD: /usr/bin/python3 -I /opt/pi-nas-drop/drive_helper.py *' > /etc/sudoers.d/pi-nas-drop
chmod 0440 /etc/sudoers.d/pi-nas-drop
visudo -cf /etc/sudoers.d/pi-nas-drop
systemctl daemon-reload
systemctl enable mnt-pidropnas.mount pi-nas-drop.service
if ! systemctl start mnt-pidropnas.mount; then
  echo 'NAS mount failed. The app will remain unable to copy until the encrypted mount works.' >&2
fi
systemctl start pi-nas-drop.service
echo 'PiTransfer installed. It listens only at 127.0.0.1:8090.'
echo 'SSH tunnel from your computer: ssh -N -L 8090:127.0.0.1:8090 USER@PI_ADDRESS'
echo 'Then open http://127.0.0.1:8090 in your computer browser.'
echo 'Reconnect USB cameras after installation and choose MTP/PTP/File Transfer mode.'
