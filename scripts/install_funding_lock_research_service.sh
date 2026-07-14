#!/usr/bin/env bash
set -euo pipefail

APP_DIR="${APP_DIR:-/root/arbitrage}"
APP_USER="${APP_USER:-root}"
SERVICE_NAME="funding-lock-research"

if command -v sudo >/dev/null 2>&1; then
  SUDO="sudo"
else
  SUDO=""
fi

cd "$APP_DIR"

if [ ! -x "$APP_DIR/.venv/bin/python" ]; then
  python3 -m venv "$APP_DIR/.venv"
fi

"$APP_DIR/.venv/bin/python" -m pip install -r requirements.txt

${SUDO} install -m 0644 \
  "$APP_DIR/deployment/systemd/${SERVICE_NAME}.service" \
  "/etc/systemd/system/${SERVICE_NAME}.service"

if [ "$APP_USER" != "root" ]; then
  ${SUDO} sed -i "s/^User=.*/User=${APP_USER}/" "/etc/systemd/system/${SERVICE_NAME}.service"
fi

${SUDO} systemctl daemon-reload
${SUDO} systemctl enable "$SERVICE_NAME"
${SUDO} systemctl restart "$SERVICE_NAME"
${SUDO} systemctl --no-pager --full status "$SERVICE_NAME"

echo
echo "Installed ${SERVICE_NAME}. Existing scanner and strategy services were not restarted."
