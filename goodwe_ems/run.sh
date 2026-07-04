#!/usr/bin/env bash
# Add-on entrypoint: read options, then run the coordinator.
# Inside an add-on we talk to HA through the Supervisor proxy using the
# built-in SUPERVISOR_TOKEN (homeassistant_api: true) — no manual token needed.
set -e

OPT=/data/options.json

export GW10_IP="$(jq -r '.gw10_ip' "$OPT")"
export GW20_IP="$(jq -r '.gw20_ip' "$OPT")"
export EMS_APPLY="$(jq -r '.apply' "$OPT")"
export EMS_INTERVAL="$(jq -r '.interval' "$OPT")"
export EMS_HOST="0.0.0.0"
export EMS_PORT="8765"
export DATA_DIR="/data"

# HA sensor push via Supervisor proxy
export HA_URL="http://supervisor/core"
export HA_TOKEN="${SUPERVISOR_TOKEN}"

echo "GoodWe EMS: GW10=${GW10_IP} GW20=${GW20_IP} apply=${EMS_APPLY} interval=${EMS_INTERVAL}s"
exec python /app/monitor.py
