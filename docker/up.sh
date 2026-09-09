#!/bin/sh
# Bring up the development stack.
#
# Regenerates the dashboard first, bound to the provisioned datasource uids
# and the fake router's address: the committed quectel-5g-alternative.json
# carries ${DS_INFLUXDB} placeholders for import, and Grafana's file
# provisioning does not answer import prompts.
set -e
cd "$(dirname "$0")"

mkdir -p grafana/dashboards
python3 ../grafana/generate_alternative.py \
    --influxdb-uid quectel-influx \
    --influxdb-short-uid quectel-influx-short \
    --infinity-uid quectel-infinity \
    --url http://fake-router:8080/cgi-bin/quectel-status \
    --stdout > grafana/dashboards/quectel-5g-alternative.json

echo "dashboard generated; starting stack"
exec docker compose up "$@"
