#!/bin/sh
# Assert the pipeline actually works, rather than merely running.
#
# Every check here corresponds to a mistake that is silent in production. A
# Telegraf tag list that indexes the wrong thing costs nothing today and an
# unbounded index in a year. An output filter that is missing writes your data
# twice. A DBRP mapping that does not exist makes every panel return nothing,
# with no error anywhere to say why. None of these announce themselves; all of
# them are one query away from being obvious.
#
# Run after ./up.sh has been going for a minute or so.
set -e
cd "$(dirname "$0")"

INFLUX="${INFLUX:-http://localhost:${QUECTEL_INFLUX_PORT:-18086}}"
TOKEN="${TOKEN:-quectel-dev-token}"
ROUTER="${ROUTER:-http://localhost:${QUECTEL_ROUTER_PORT:-18080}}"
GRAFANA="${GRAFANA:-http://localhost:${QUECTEL_GRAFANA_PORT:-13000}}"

pass=0
fail=0

ok()   { echo "  [OK] $1";   pass=$((pass + 1)); }
bad()  { echo "  [FAIL] $1"; fail=$((fail + 1)); }
check() { if [ "$1" = "1" ]; then ok "$2"; else bad "$2${3:+: $3}"; fi; }

# InfluxQL through the v1 compatibility endpoint -- the same path Grafana
# takes, so a DBRP problem shows up here rather than in a blank panel.
# $1 is the database. The short-retention bucket is reachable as a database of
# its own rather than as a retention policy on systemhealth: 2.x's v1 layer
# ignores the rp parameter for SHOW, and answers "short"."measurement" with no
# series and no error. Grafana has the same constraint, which is why the
# dashboard uses a second datasource for it.
iql() {
    curl -sG "$INFLUX/query" \
        --data-urlencode "db=$1" \
        --data-urlencode "q=$2" \
        -H "Authorization: Token $TOKEN"
}

# Pull the flat list of values out of an InfluxQL response.
values() {
    python3 -c '
import json,sys
try:
    d = json.load(sys.stdin)
except Exception:
    sys.exit(0)
for r in d.get("results", []):
    for s in r.get("series", []):
        for v in s.get("values", []):
            print(v[0] if len(v) == 1 else "\t".join(str(x) for x in v))
'
}

echo "=== The router answers ==="
body=$(curl -s --max-time 10 "$ROUTER/cgi-bin/quectel-status" || true)
# printf, not echo: /bin/sh's echo expands backslash escapes on macOS and on
# dash, so a \n inside any JSON string value arrives as a real newline and the
# document is no longer parseable. The greps below are unaffected -- they match
# on measurement and datasource names, which carry no escapes -- but anything
# handed to a JSON parser has to go through printf.
printf '%s' "$body" | python3 -c 'import json,sys; json.load(sys.stdin)' 2>/dev/null \
    && check 1 "status endpoint returns valid JSON" \
    || check 0 "status endpoint returns valid JSON" "$(echo "$body" | head -c 100)"

echo "$body" | grep -q '"operator"' \
    && check 1 "the operator name is in it" \
    || check 0 "the operator name is in it"
echo "$body" | grep -q '"imei"' \
    && check 0 "the IMEI is not in it" \
    || check 1 "the IMEI is not in it"

echo ""
echo "=== InfluxDB answers InfluxQL (so the DBRP mappings exist) ==="
main=$(iql systemhealth "SHOW MEASUREMENTS" | values)
if [ -z "$main" ]; then
    bad "SHOW MEASUREMENTS returned nothing (no DBRP mapping, or no data yet)"
else
    ok "the default retention policy resolves to a bucket"
fi

for m in quectel_lte quectel_nr5g quectel_carrier_pcc quectel_carrier_scc quectel_operator; do
    echo "$main" | grep -qx "$m" \
        && check 1 "$m has arrived" \
        || check 0 "$m has arrived"
done

echo ""
echo "=== Output routing put neighbours somewhere else ==="
# Both halves matter. In the long bucket they would be kept for as long as
# everything else; missing from the short one they were never routed at all.
echo "$main" | grep -qx quectel_neighbour \
    && check 0 "quectel_neighbour is kept out of the long-retention bucket" \
    || check 1 "quectel_neighbour is kept out of the long-retention bucket"

short=$(iql systemhealth_short "SHOW MEASUREMENTS" | values)
echo "$short" | grep -qx quectel_neighbour \
    && check 1 "quectel_neighbour is in the short-retention bucket" \
    || check 0 "quectel_neighbour is in the short-retention bucket"

echo ""
echo "=== Only bounded dimensions became tags ==="
# The defect this check exists for: cell_id as a tag is one index entry per
# cell ever visited, kept for the life of the bucket.
tags=$(iql systemhealth "SHOW TAG KEYS FROM quectel_lte" | values)
for t in band duplex; do
    echo "$tags" | grep -qx "$t" \
        && check 1 "$t is a tag on quectel_lte" \
        || check 0 "$t is a tag on quectel_lte"
done
for t in cell_id pci tac arfcn; do
    echo "$tags" | grep -qx "$t" \
        && check 0 "$t is NOT a tag on quectel_lte (unbounded)" \
        || check 1 "$t is NOT a tag on quectel_lte (unbounded)"
done

# ...but they are still queryable, which is the other half of the bargain.
fields=$(iql systemhealth "SHOW FIELD KEYS FROM quectel_lte" | values | cut -f1)
for f in rsrp sinr enodeb pci; do
    echo "$fields" | grep -qx "$f" \
        && check 1 "$f is a field on quectel_lte" \
        || check 0 "$f is a field on quectel_lte"
done

echo ""
echo "=== The dashboard's own queries return rows ==="
rsrp=$(iql systemhealth 'SELECT last("rsrp") FROM "quectel_lte"' | values)
[ -n "$rsrp" ] && check 1 "LTE RSRP has a value ($rsrp)" \
               || check 0 "LTE RSRP has a value"

enodeb=$(iql systemhealth 'SELECT last("enodeb") FROM "quectel_lte"' | values)
[ -n "$enodeb" ] && check 1 "the serving site graphs as a number ($enodeb)" \
                 || check 0 "the serving site graphs as a number"

# count() over a field, not distinct() over a tag: InfluxQL's distinct() does
# not operate on tags, and returns nothing at all rather than an error.
nbr=$(iql systemhealth_short 'SELECT count("rsrp") FROM "quectel_neighbour"' | values)
[ -n "$nbr" ] && check 1 "the neighbour panel's query returns rows ($nbr)" \
              || check 0 "the neighbour panel's query returns rows"

op=$(iql systemhealth 'SHOW TAG VALUES FROM "quectel_operator" WITH KEY = "operator"' | values)
[ -n "$op" ] && check 1 "the carrier name is a tag, not a lost string ($op)" \
             || check 0 "the carrier name is a tag, not a lost string"

echo ""
echo "=== Grafana came up with both datasources and the dashboard ==="
ds=$(curl -s -u admin:admin "$GRAFANA/api/datasources" || true)
echo "$ds" | grep -q quectel-influx \
    && check 1 "the InfluxDB datasource is provisioned" \
    || check 0 "the InfluxDB datasource is provisioned"
echo "$ds" | grep -q quectel-influx-short \
    && check 1 "the short-retention datasource is provisioned" \
    || check 0 "the short-retention datasource is provisioned"
echo "$ds" | grep -q quectel-infinity \
    && check 1 "the Infinity datasource is provisioned" \
    || check 0 "the Infinity datasource is provisioned"

dash=$(curl -s -u admin:admin \
    "$GRAFANA/api/dashboards/uid/quectel-5g-alternative" || true)
echo "$dash" | grep -q '"title"' \
    && check 1 "the dashboard is provisioned" \
    || check 0 "the dashboard is provisioned"

# Provisioned is not the same as wired up correctly. The checks above prove
# the short-retention datasource exists and that its query returns rows; the
# panel could still be pointed at the main datasource, where quectel_neighbour
# is deliberately absent, and every assertion so far would still pass while
# the panel read "No data" forever.
#
# The target's uid, not the panel's: Grafana honours the target when the two
# disagree, so checking the panel alone would miss the case that actually
# renders empty.
nbr_ds=$(printf '%s' "$dash" | python3 -c '
import json,sys
try:
    d = json.load(sys.stdin)
except Exception:
    sys.exit(0)
for p in d.get("dashboard", {}).get("panels", []):
    if p.get("title") == "Neighbours reported":
        for t in p.get("targets", []):
            print(t.get("datasource", {}).get("uid", ""))
' || true)
[ "$nbr_ds" = "quectel-influx-short" ] \
    && check 1 "the neighbour panel queries the short-retention datasource" \
    || check 0 "the neighbour panel queries the short-retention datasource" \
             "got '${nbr_ds:-nothing}', wanted 'quectel-influx-short'"

echo ""
echo "=== $pass passed, $fail failed ==="
[ "$fail" -eq 0 ]
