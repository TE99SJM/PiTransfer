#!/bin/bash
# Updates application code/dependencies only; retains config, credentials and history.
set -euo pipefail
if [ "$(id -u)" -ne 0 ]; then
  echo 'Run this updater with sudo.' >&2
  exit 1
fi
cd -- "$(dirname -- "$0")"
if [ ! -f /etc/pi-nas-drop/config.json ] || [ ! -d /opt/pi-nas-drop ]; then
  echo 'No existing installation found. Use install.sh for a new installation.' >&2
  exit 1
fi
python3 - <<'PY'
import ast
import json
from pathlib import Path
for name in ('app.py', 'core.py', 'drive_helper.py', 'camera.py', 'camera_worker.py'):
    ast.parse(Path(name).read_text(), filename=name)
config = json.loads(Path('/etc/pi-nas-drop/config.json').read_text())
history = Path(config['history_path'])
if history.exists() and any(j['state'] in ('queued', 'starting', 'counting', 'copying', 'verifying', 'resuming', 'finalizing') for j in json.loads(history.read_text())):
    raise SystemExit('A transfer is active. Finish or cancel it in PiTransfer before updating.')
PY
apt-get update
apt-get install -y --no-install-recommends python3-gphoto2 gphoto2 libmtp-runtime
python3 -c 'import gphoto2'
stage=$(mktemp -d /opt/pi-nas-drop.update.XXXXXX)
backup="${stage}.previous"
swapped=0
stopped=0
cleanup() {
  result=$?
  if [ "$result" -ne 0 ]; then
    if [ "$swapped" -eq 1 ]; then
      systemctl stop pi-nas-drop.service || true
      mv /opt/pi-nas-drop "${stage}.failed"
      mv "$backup" /opt/pi-nas-drop
      echo 'Update failed; previous application restored.' >&2
    fi
    if [ "$stopped" -eq 1 ]; then
      systemctl start pi-nas-drop.service || true
    fi
  fi
  if [ -d "$stage" ]; then rm -rf -- "$stage"; fi
  exit "$result"
}
trap cleanup EXIT
chmod 0755 "$stage"
install -d -o root -g root -m 0755 "$stage/web"
install -o root -g root -m 0644 app.py core.py drive_helper.py camera.py camera_worker.py "$stage/"
install -o root -g root -m 0644 web/index.html web/style.css web/app.js "$stage/web/"
# Check again because apt may have taken a while. Stop only after transfers finish.
python3 - <<'PY'
import json
from pathlib import Path
c = json.loads(Path('/etc/pi-nas-drop/config.json').read_text())
p = Path(c['history_path'])
if p.exists() and any(j['state'] in ('queued', 'starting', 'counting', 'copying', 'verifying', 'resuming', 'finalizing') for j in json.loads(p.read_text())):
    raise SystemExit('A transfer started during preparation. Finish it before updating.')
PY
systemctl stop pi-nas-drop.service
stopped=1
mv /opt/pi-nas-drop "$backup"
if ! mv "$stage" /opt/pi-nas-drop; then
  mv "$backup" /opt/pi-nas-drop
  exit 1
fi
swapped=1
install -o root -g root -m 0644 udev/99-pitransfer-cameras.rules /etc/udev/rules.d/
udevadm control --reload-rules
systemctl start pi-nas-drop.service
systemctl is-active --quiet pi-nas-drop.service
echo 'PiTransfer updated. Settings, credentials, Tailscale access and transfer history retained.'
echo "Previous application retained at $backup"
echo 'Reconnect the camera, select MTP/PTP/File Transfer mode, then refresh PiTransfer.'
