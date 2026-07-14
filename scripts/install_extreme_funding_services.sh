#!/usr/bin/env bash
set -euo pipefail

APP_DIR="${APP_DIR:-/root/arbitrage}"

if command -v sudo >/dev/null 2>&1; then
  SUDO="sudo"
else
  SUDO=""
fi

if [ ! -x "$APP_DIR/.venv/bin/python" ]; then
  echo "Missing $APP_DIR/.venv/bin/python; create the project virtualenv first." >&2
  exit 1
fi

services=(
  binance-extreme-funding-scanner
  binance-extreme-funding-strategy
  binance-extreme-funding-dashboard
  mexc-extreme-funding-scanner
  mexc-extreme-funding-strategy
  mexc-extreme-funding-dashboard
)

for service in "${services[@]}"; do
  ${SUDO} install -m 0644 \
    "$APP_DIR/deployment/systemd/${service}.service" \
    "/etc/systemd/system/${service}.service"
done

${SUDO} systemctl daemon-reload
for service in "${services[@]}"; do
  ${SUDO} systemctl enable --now "$service"
done

${SUDO} systemctl --no-pager --full status "${services[@]}"

echo
echo "Installed only the six independent Binance/MEXC extreme-funding services."
