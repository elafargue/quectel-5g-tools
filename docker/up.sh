#!/bin/sh
# Bring up the development stack.
#
# Regenerates the dashboard first, bound to the provisioned datasource uids:
# the committed quectel-5g-alternative.json carries ${DS_INFLUXDB}
# placeholders for import, and Grafana's file provisioning does not answer
# import prompts. The Now-row window is sized for this stack's 10s polls --
# 10s interval plus 10s flush, and a little margin -- where the committed
# 75s suits a deployed 60s poll and would keep a dropped carrier on screen
# here for seven polls.
set -e
cd "$(dirname "$0")"

mkdir -p grafana/dashboards
python3 ../grafana/generate_alternative.py \
    --influxdb-uid quectel-influx \
    --influxdb-short-uid quectel-influx-short \
    --recent-window 25s \
    --poll-interval 10s \
    --stdout > grafana/dashboards/quectel-5g-alternative.json

echo "dashboard generated; starting stack"
exec docker compose up "$@"
