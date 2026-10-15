#!/usr/bin/env bash
# One-shot setup for the demon Pi (Raspberry Pi OS Lite 64-bit).
#
# If you copied this project onto the SD card's boot partition, run:
#     bash /boot/firmware/demon-robot/pi/install.sh
# It copies the project to ~/demon-robot, installs everything, and enables
# the service. Safe to re-run.
set -euo pipefail

SRC="$(cd "$(dirname "$0")/.." && pwd)"
DEST="$HOME/demon-robot"
USER_NAME="$(id -un)"

echo "==> Copying project to $DEST"
mkdir -p "$DEST"
if [ "$SRC" != "$DEST" ]; then
  # FAT boot partition has no permissions; normalise them on the way in.
  cp -r "$SRC"/. "$DEST"/
  find "$DEST" -path "$DEST/.venv" -prune -o -type d -exec chmod 755 {} +
  find "$DEST" -path "$DEST/.venv" -prune -o -type f -exec chmod 644 {} +
  chmod +x "$DEST"/pi/*.sh "$DEST"/bot.py "$DEST"/tools/*.py
fi
[ -f "$DEST/.env" ] || cp "$DEST/.env.example" "$DEST/.env"
chmod 600 "$DEST/.env"

echo "==> Installing system packages"
sudo apt-get update
sudo apt-get install -y --no-install-recommends \
  python3-venv python3-dev build-essential portaudio19-dev libasound2-dev \
  alsa-utils mosquitto-clients usbutils

echo "==> Creating Python environment (this takes a while on a 3A+)"
python3 -m venv "$DEST/.venv"
"$DEST/.venv/bin/pip" install --upgrade pip wheel
"$DEST/.venv/bin/pip" install -r "$DEST/requirements.txt"

echo "==> Letting the bot switch the XVF3800's GPO pins (crystal ball, eyes) without sudo"
echo 'SUBSYSTEM=="usb", ATTRS{idVendor}=="2886", MODE="0666"' | \
  sudo tee /etc/udev/rules.d/99-respeaker.rules >/dev/null
sudo udevadm control --reload-rules && sudo udevadm trigger

echo "==> Turning off Wi-Fi power saving (it adds big latency spikes)"
for c in $(nmcli -t -f NAME,TYPE connection show | awk -F: '$2=="802-11-wireless"{print $1}'); do
  sudo nmcli connection modify "$c" 802-11-wireless.powersave 2 || true
done

echo "==> Freeing RAM: disabling Bluetooth"
sudo systemctl disable --now bluetooth.service hciuart.service 2>/dev/null || true

echo "==> Enabling hardware watchdog (reboots the Pi if it locks up)"
sudo mkdir -p /etc/systemd/system.conf.d
printf '[Manager]\nRuntimeWatchdogSec=15\nRebootWatchdogSec=2min\n' | \
  sudo tee /etc/systemd/system.conf.d/watchdog.conf >/dev/null

echo "==> Installing systemd service"
sed -e "s#@USER@#$USER_NAME#g" -e "s#@DIR@#$DEST#g" "$DEST/pi/demon.service" | \
  sudo tee /etc/systemd/system/demon.service >/dev/null
sudo systemctl daemon-reload
sudo systemctl enable demon.service

if [ -e /dev/snd ] && arecord -l 2>/dev/null | grep -qiE 'xvf|respeaker|array'; then
  echo "==> XVF3800 detected, configuring audio"
  bash "$DEST/pi/audio-setup.sh"
else
  echo "==> XVF3800 not plugged in yet: run  bash ~/demon-robot/pi/audio-setup.sh  once it is."
fi

echo "==> Running offline tests"
(cd "$DEST" && PYTHONPATH=. .venv/bin/python tests/test_offline.py 2>&1 | grep -E 'OK|PASSED|Error|assert' ) || true

cat <<EOF

Done. Next:
  1. nano ~/demon-robot/.env        # API keys, voice ID, MQTT host
  2. bash ~/demon-robot/pi/audio-setup.sh   # if not already done
  3. sudo systemctl start demon && journalctl -u demon -f
EOF
