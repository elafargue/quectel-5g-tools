#!/bin/bash
# Runs once, after InfluxDB's own first-run setup.
#
# Two things the DOCKER_INFLUXDB_INIT_* variables cannot express:
#
#   * a second bucket. Retention in 2.x belongs to the bucket, never to the
#     measurement, so keeping neighbours for a day while keeping everything
#     else for a month means two buckets and not one.
#   * DBRP mappings. 2.x answers InfluxQL only through them, and without the
#     mappings every panel in the dashboard returns nothing at all -- with no
#     error to say why.
set -euo pipefail

ORG="$DOCKER_INFLUXDB_INIT_ORG"
TOKEN="$DOCKER_INFLUXDB_INIT_ADMIN_TOKEN"

influx bucket create --name systemhealth_short --retention 24h \
    --org "$ORG" --token "$TOKEN"

bucket_id() {
    influx bucket list --name "$1" --org "$ORG" --token "$TOKEN" \
        --hide-headers | awk '{print $1}'
}

MAIN=$(bucket_id systemhealth)
SHORT=$(bucket_id systemhealth_short)

# db + retention policy -> bucket. "autogen" is what an unqualified InfluxQL
# query resolves to, which is what every panel uses. The --rp short mapping
# below is vestigial: it served an earlier design in which the neighbour panel
# qualified the measurement as "short"."quectel_neighbour". Nothing queries it
# now -- the panel uses the systemhealth_short database created further down --
# and it is kept only because an existing deployment may still have it.
influx v1 dbrp create --db systemhealth --rp autogen \
    --bucket-id "$MAIN" --default --org "$ORG" --token "$TOKEN"
influx v1 dbrp create --db systemhealth --rp short \
    --bucket-id "$SHORT" --org "$ORG" --token "$TOKEN"

# And the short bucket as a database in its own right. Grafana's InfluxQL
# datasource carries one database and no retention policy, and the
# "short"."measurement" qualification that would sidestep that is not honoured
# by 2.x's v1 compatibility layer -- it answers with no series and no error.
# A database per bucket is what actually works, so the dashboard points a
# second datasource here.
influx v1 dbrp create --db systemhealth_short --rp autogen \
    --bucket-id "$SHORT" --default --org "$ORG" --token "$TOKEN"

echo "buckets: systemhealth=$MAIN systemhealth_short=$SHORT; DBRPs created"
