#!/usr/bin/env bash
set -euo pipefail

echo "[1/8] Packages"
apt-get update
apt-get install -y python3

echo "[2/8] Enable configfs + libcomposite at boot"
echo "libcomposite" > /etc/modules-load.d/libcomposite.conf

echo "[3/8] Enable OTG peripheral mode (Pi 4 dwc2)"
CFG="/boot/firmware/config.txt"
if ! grep -qE '^\s*dtoverlay=dwc2' "$CFG"; then
  echo "dtoverlay=dwc2,dr_mode=peripheral" >> "$CFG"
else
  echo "NOTE: dtoverlay=dwc2 already present; verify it includes dr_mode=peripheral"
fi

echo "[4/8] Install gadget creator"
install -m 0755 scripts/kaivm-hid-gadget.py /usr/local/sbin/kaivm-hid-gadget.py

echo "[5/8] udev permissions for /dev/hidg*"
cat >/etc/udev/rules.d/99-hidg.rules <<'RULE'
KERNEL=="hidg*", MODE="0660", GROUP="plugdev"
RULE
udevadm control --reload-rules
udevadm trigger

echo "[6/8] systemd unit"
install -m 0644 scripts/kaivm-hid.service /etc/systemd/system/kaivm-hid.service
systemctl daemon-reload
systemctl enable --now kaivm-hid.service

echo "[7/8] Add current user to plugdev (re-login required)"
if [[ -n "${SUDO_USER:-}" ]]; then
  usermod -aG plugdev "$SUDO_USER" || true
  echo "Added $SUDO_USER to plugdev."
fi

echo "[8/8] Done. Reboot recommended if OTG overlay was newly added."
echo "Verify: ls /sys/class/udc ; ls -l /dev/hidg* ; systemctl status kaivm-hid.service"

