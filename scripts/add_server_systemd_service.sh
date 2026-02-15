#!/usr/bin/env bash
set -euo pipefail

SERVICE_NAME="vlcsync-server"
UNIT_PATH="/etc/systemd/system/${SERVICE_NAME}.service"
DEFAULT_PORT="9000"
DEFAULT_SESSION_TTL="1800"

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd -- "${SCRIPT_DIR}/.." && pwd)"
PYTHON_BIN="$(command -v python3 || true)"

if [[ -z "${PYTHON_BIN}" ]]; then
  echo "[error] python3 is not available in PATH." >&2
  exit 1
fi

RUN_USER="${SUDO_USER:-$(id -un)}"
RUN_GROUP="$(id -gn "${RUN_USER}")"
PORT="${1:-$DEFAULT_PORT}"
SESSION_TTL="${2:-$DEFAULT_SESSION_TTL}"

if ! [[ "$PORT" =~ ^[0-9]+$ ]] || (( PORT < 1 || PORT > 65535 )); then
  echo "[error] Invalid port: ${PORT}. Expected 1-65535." >&2
  exit 1
fi

if ! [[ "$SESSION_TTL" =~ ^[0-9]+$ ]] || (( SESSION_TTL < 1 )); then
  echo "[error] Invalid session TTL: ${SESSION_TTL}. Expected positive integer seconds." >&2
  exit 1
fi

if [[ ! -f "${PROJECT_DIR}/server.py" ]]; then
  echo "[error] server.py not found at ${PROJECT_DIR}. Run this script from the project repository." >&2
  exit 1
fi

if [[ "$(id -u)" -ne 0 ]]; then
  echo "[error] This script must run as root (use sudo)." >&2
  exit 1
fi

mkdir -p /var/log/vlcsync
touch /var/log/vlcsync/server.log
chown "${RUN_USER}:${RUN_GROUP}" /var/log/vlcsync/server.log
chmod 0644 /var/log/vlcsync/server.log

cat > "${UNIT_PATH}" <<EOF
[Unit]
Description=VLC Sync Python Server
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=${RUN_USER}
Group=${RUN_GROUP}
WorkingDirectory=${PROJECT_DIR}
ExecStart=${PYTHON_BIN} ${PROJECT_DIR}/server.py ${PORT} --session-ttl-seconds ${SESSION_TTL}
Restart=on-failure
RestartSec=3
NoNewPrivileges=true
PrivateTmp=true
StandardOutput=append:/var/log/vlcsync/server.log
StandardError=append:/var/log/vlcsync/server.log

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload

if systemctl is-enabled --quiet "${SERVICE_NAME}"; then
  systemctl disable "${SERVICE_NAME}" >/dev/null 2>&1 || true
fi

if systemctl is-active --quiet "${SERVICE_NAME}"; then
  systemctl stop "${SERVICE_NAME}" >/dev/null 2>&1 || true
fi

echo "[ok] Installed unit: ${UNIT_PATH}"
echo "[ok] Service is installed but disabled and stopped by default."
echo "[ok] Log file: /var/log/vlcsync/server.log"
echo "[next] Start manually: sudo systemctl start ${SERVICE_NAME}"
echo "[next] Enable on boot (optional): sudo systemctl enable ${SERVICE_NAME}"
echo "[next] View status: sudo systemctl status ${SERVICE_NAME}"
echo "[next] Tail logs: tail -f /var/log/vlcsync/server.log"
