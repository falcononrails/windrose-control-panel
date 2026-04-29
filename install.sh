#!/usr/bin/env bash
set -euo pipefail

install_dir=${INSTALL_DIR:-/opt/windrose-panel}
env_file=${ENV_FILE:-/etc/windrose-panel.env}
service_file=/etc/systemd/system/windrose-panel.service

if [[ ${EUID:-$(id -u)} -ne 0 ]]; then
  echo "Run as root: sudo ./install.sh" >&2
  exit 1
fi

install -d -m 0755 "$install_dir"
install -m 0755 windrose_panel.py "$install_dir/windrose_panel.py"
install -m 0644 windrose-panel.service "$service_file"

if [[ ! -f "$env_file" ]]; then
  panel_password=$(openssl rand -base64 24)
  panel_secret=$(openssl rand -base64 32)
  cat > "$env_file" <<EOF
PANEL_HOST=0.0.0.0
PANEL_PORT=8790
PANEL_PASSWORD=$panel_password
PANEL_SECRET=$panel_secret
WINDROSE_GAME_DIR=/opt/windrose-direct/server
WINDROSE_BACKUP_DIR=/opt/windrose-backups
WINDROSE_SERVICE=windrose.service
WINDROSE_PLUS_SERVICE=windrose-plus-dashboard.service
SOURCE_RCON_HOST=127.0.0.1
SOURCE_RCON_PORT=27065
EOF
  chmod 0600 "$env_file"
  echo "Created $env_file"
else
  echo "Keeping existing $env_file"
fi

systemctl daemon-reload
systemctl enable --now windrose-panel.service
systemctl --no-pager --full status windrose-panel.service | sed -n '1,24p'

echo
echo "Panel password:"
grep '^PANEL_PASSWORD=' "$env_file" | cut -d= -f2-
