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
# Not a pass: a check whose precondition never occurred proved nothing, and
# counting it as one would let the total claim more than was tested.
skip() { echo "  [SKIP] $1"; }

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

# Carriers are the exception, and the reason is collision rather than
# grouping: several secondaries share one poll, and two on the same band used
# to share a series key, so the second write replaced the first. arfcn in the
# key is what keeps both.
for m in quectel_carrier_pcc quectel_carrier_scc; do
    ctags=$(iql systemhealth "SHOW TAG KEYS FROM $m" | values)
    echo "$ctags" | grep -qx arfcn \
        && check 1 "arfcn is a tag on $m (carriers share a poll)" \
        || check 0 "arfcn is a tag on $m (carriers share a poll)"
done

# The collision itself, where the stack has produced one. Only the synthetic
# network (QUECTEL_NETWORK=2) runs two B3 secondaries, at 1350 and 1850; on
# the old tags InfluxDB kept 1850 alone. Roaming reaches it eventually, so on
# a default run this usually has nothing to test -- and says so.
b3=$(iql systemhealth "SELECT count(rsrp) FROM quectel_carrier_scc WHERE band='3' AND rat='lte' AND time > now() - 1h GROUP BY arfcn" \
     | python3 -c 'import json,sys
for r in json.load(sys.stdin).get("results",[]):
    for s in r.get("series",[]): print(s.get("tags",{}).get("arfcn",""))' 2>/dev/null)
if echo "$b3" | grep -qx 1350 || echo "$b3" | grep -qx 1850; then
    { echo "$b3" | grep -qx 1350 && echo "$b3" | grep -qx 1850; } \
        && check 1 "two secondaries on one band are both kept" \
        || check 0 "two secondaries on one band are both kept" \
                 "only arfcn $(echo "$b3" | grep -x '1350\|1850') survived"
else
    skip "two secondaries on one band (synthetic network not visited; QUECTEL_NETWORK=2)"
fi

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
echo "=== The connection mode is recorded, not inferred ==="

# quectel_serving exists so the technology does not have to be guessed from
# which other measurements turned up. On a 3G attach the modem reports neither
# an LTE nor an NR serving cell, so absence-based inference calls a working
# link no reading at all.
echo "$main" | grep -qx quectel_serving \
    && check 1 "quectel_serving is recorded" \
    || check 0 "quectel_serving is recorded"

# technology must be a FIELD, not a tag. As a tag it can only be a series
# name, and the state-timeline panel -- which plots the technology as its
# value and colours it by value mapping -- would render one row per technology
# with nothing in it. That failure looks like an empty panel, with no error.
sfields=$(iql systemhealth "SHOW FIELD KEYS FROM quectel_serving" | values)
echo "$sfields" | grep -q '^technology' \
    && check 1 "technology is a field, so it can be the plotted value" \
    || check 0 "technology is a field, so it can be the plotted value" \
             "fields: $(echo "$sfields" | tr '\n' ' ')"

stags=$(iql systemhealth "SHOW TAG KEYS FROM quectel_serving" | values)
echo "$stags" | grep -qx technology \
    && check 0 "technology is not a tag" \
    || check 1 "technology is not a tag"

# The panel's own query, run as written.
modes=$(iql systemhealth 'SELECT last("technology") FROM "quectel_serving" WHERE time > now() - 1h GROUP BY time(30s) fill(none)' | values)
[ -n "$modes" ] && check 1 "the connection-mode panel's query returns rows" \
               || check 0 "the connection-mode panel's query returns rows"

# Whatever it reports has to be one of the four the panel maps. An unmapped
# value renders as a bare string with no colour, which is how a new
# technology would quietly arrive looking like a glitch.
badmode=$(echo "$modes" | awk '{print $2}' | sort -u \
          | grep -vE '^(WCDMA|LTE|NSA|SA)?$' | head -1)
[ -z "$badmode" ] && check 1 "every technology reported is one the panel maps" \
                  || check 0 "every technology reported is one the panel maps" \
                           "unmapped: $badmode"

echo ""
echo "=== Grafana came up with both datasources and the dashboard ==="
ds=$(curl -s -u admin:admin "$GRAFANA/api/datasources" || true)
echo "$ds" | grep -q quectel-influx \
    && check 1 "the InfluxDB datasource is provisioned" \
    || check 0 "the InfluxDB datasource is provisioned"
echo "$ds" | grep -q quectel-influx-short \
    && check 1 "the short-retention datasource is provisioned" \
    || check 0 "the short-retention datasource is provisioned"
# Nothing may still point at a plugin datasource. Every panel reads InfluxDB
# now; a leftover Infinity target would be an error box on any install
# without the plugin, which is every install.
nonflux=$(printf '%s' "$dash" | python3 -c 'import json,sys
try: d=json.load(sys.stdin)
except Exception: sys.exit(0)
for p in d.get("dashboard",{}).get("panels",[]):
    for t in [p]+p.get("targets",[]):
        ty=(t.get("datasource") or {}).get("type")
        if ty and ty!="influxdb": print(p.get("title"))' | sort -u)
[ -z "$nonflux" ] && check 1 "every panel queries InfluxDB" \
                  || check 0 "every panel queries InfluxDB" "not: $nonflux"

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

# Carrier frequency must plot frequencies that carriers actually use. Grouped
# by fewer tags than identify a carrier, mean() blends two secondaries on one
# band into a frequency nothing transmits on -- 1820 and 1870 MHz became 1850
# and 1851.7 on this stack. Grouped by arfcn, each series is one channel and
# its value never varies. The panel's own query is run, with its macros
# filled in, so a regrouping of the panel is what gets caught.
#
# Series with an empty arfcn are points from before the tag existed and are
# skipped; a series with no arfcn key at all means the panel stopped grouping
# by it, and is checked -- that is the regression.
freqq=$(printf '%s' "$dash" | python3 -c 'import json,sys
try: d=json.load(sys.stdin)["dashboard"]
except Exception: sys.exit(0)
for p in d.get("panels",[]):
    if p.get("title")=="Carrier frequency": print(p["targets"][0]["query"])' \
    | sed -e 's/\$timeFilter/time > now() - 30m/' -e 's/\$__interval/1m/')
blend=$( [ -n "$freqq" ] && iql systemhealth "$freqq" | python3 -c 'import json,sys
n=0
for r in json.load(sys.stdin).get("results",[]):
    for s in r.get("series",[]):
        if s.get("tags",{}).get("arfcn","absent")=="": continue
        n+=1
        v={round(x[1],1) for x in s["values"] if x[1] is not None}
        if len(v)>1: print("%s %s" % (json.dumps(s.get("tags")), sorted(v)))
if n==0: print("no series to check")' 2>/dev/null)
[ -n "$freqq" ] && [ -z "$blend" ] \
    && check 1 "Carrier frequency plots only frequencies a carrier uses" \
    || check 0 "Carrier frequency plots only frequencies a carrier uses" \
             "${blend:-panel query not found}"

# Aggregated carriers must count carriers, not points. count() over a bucket
# counts one point per carrier per poll, so the old query read carriers times
# polls -- 5 to 54 on this stack at 5-minute buckets where the truth was 1 or
# 2 -- and a count that grows with the zoom level says nothing. The panel's
# own queries run at 5-minute buckets, 30 of this stack's polls each, and
# every value must lie within the per-poll maximum the raw points give. The
# targets' aliases must differ too: one query over both measurements used to
# return two series both called "lte".
agg=$(printf '%s' "$dash" | INFLUX="$INFLUX" TOKEN="$TOKEN" python3 -c '
import json,sys,os,urllib.request,urllib.parse
from collections import Counter
def q(s):
    u=os.environ["INFLUX"]+"/query?"+urllib.parse.urlencode({"db":"systemhealth","q":s})
    rq=urllib.request.Request(u,headers={"Authorization":"Token "+os.environ["TOKEN"]})
    return json.load(urllib.request.urlopen(rq,timeout=15)).get("results",[])
try: d=json.load(sys.stdin)["dashboard"]
except Exception: print("no dashboard"); sys.exit()
p=[x for x in d.get("panels",[]) if x.get("title")=="Aggregated carriers"]
if not p: print("panel not found"); sys.exit()
tg=p[0]["targets"]
al=[t.get("alias") for t in tg]
if len(set(al))!=len(al): print("duplicate aliases %s" % al)
truth={}
for m in ("quectel_carrier_pcc","quectel_carrier_scc"):
    c=Counter()
    for r in q("SELECT rsrp, rat FROM %s WHERE time > now() - 30m" % m):
        for s in r.get("series",[]):
            for t,_,rat in s["values"]: c[(t,rat)]+=1
    for (t,rat),n in c.items(): truth[(m,rat)]=max(truth.get((m,rat),0),n)
n=0
for t in tg:
    sq=t["query"].replace("$timeFilter","time > now() - 30m").replace("$__interval","5m")
    for r in q(sq):
        for s in r.get("series",[]):
            k=(s["name"], s.get("tags",{}).get("rat"))
            for _,val in s["values"]:
                if val is None: continue
                n+=1
                if val > truth.get(k,0)+1e-9:
                    print("%s %s: %s > per-poll max %s" % (k[0],k[1],val,truth.get(k)))
if n==0: print("no values to check")
' 2>&1 | sort -u)
[ -z "$agg" ] && check 1 "Aggregated carriers counts carriers, not points" \
              || check 0 "Aggregated carriers counts carriers, not points" \
                       "$(echo "$agg" | head -3 | tr '\n' ';')"

# The Now row, asked the way the browser asks it: the provisioned panels'
# own queries, through Grafana. Network, Mode and RRC are present on every
# poll the router answers -- 3G included -- so each must return a value
# within the recent window. The signal stats are left out on purpose: no NR
# and no LTE are legitimate states, not failures.
nowrow=$(printf '%s' "$dash" | GRAFANA="$GRAFANA" python3 -c '
import json,sys,os,base64,urllib.request
try: d=json.load(sys.stdin)["dashboard"]
except Exception: sys.exit(0)
auth="Basic "+base64.b64encode(b"admin:admin").decode()
for p in d.get("panels",[]):
    if p.get("title") not in ("Network","Mode","RRC"): continue
    body=json.dumps({"queries":p["targets"],"from":"now-5m","to":"now"}).encode()
    rq=urllib.request.Request(os.environ["GRAFANA"]+"/api/ds/query",data=body,
        headers={"Content-Type":"application/json","Authorization":auth})
    try: r=json.load(urllib.request.urlopen(rq,timeout=10))["results"]["A"]
    except Exception as e: print(p["title"],"error",e); continue
    vals=[v for f in r.get("frames",[]) for v in (f.get("data",{}).get("values") or [[]])[-1]]
    print(p["title"], "ok" if vals and vals[-1] not in (None,"") else "empty")' 2>/dev/null)
for t in Network Mode RRC; do
    echo "$nowrow" | grep -qx "$t ok" \
        && check 1 "the Now row's $t answers through Grafana" \
        || check 0 "the Now row's $t answers through Grafana" \
                 "$(echo "$nowrow" | grep "^$t" || echo 'panel not found')"
done

echo ""
echo "=== $pass passed, $fail failed ==="
[ "$fail" -eq 0 ]
