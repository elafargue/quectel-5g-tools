# Grafana dashboard generator

`generate.py` builds the Quectel 5G / ISP Monitor dashboard JSON from
code so the layout, queries and thresholds stay in one place and the
JSON published to grafana.com (id `24835`) is reproducible.

Layout: three rows.

- **Connectivity** — ping current / availability / DNS for a configurable
  upstream target set (default: `1.1.1.1`, `8.8.8.8`, `facebook.com`,
  `reddit.com`, `google.com`, `sindro.me`).
- **Radio** — `modem_*` series from `lua/prometheus-collectors/quectel.lua`:
  Connected bands table, EnodeB / PCI / Freq, Band Status state-timeline,
  per-RAT SINR / RSRP / RSRQ.
- **Watchdog** — `quectel_watchdog_*` series from
  `lua/prometheus-collectors/quectel-watchdog.lua`: NR-attached
  current + 24 h %, CA depth, consecutive failed cycles, last recovery
  duration, state-file freshness, NR-attached time series, recovery
  actions per day by stage.

## Setup

No dependencies — Python 3 stdlib only (`grafanalib` was tried and
abandoned; it emits panel JSON that Grafana 11 silently truncates).

## Generate

```bash
# default: writes quectel-5g-monitor.json with a ${DS_VICTORIAMETRICS}
# datasource input placeholder (ready to upload to grafana.com or
# import into any Grafana instance).
python3 generate.py

# stream to stdout instead.
python3 generate.py --stdout

# bind to an explicit datasource uid (skip the import prompt — only
# useful when you know the target instance):
python3 generate.py --datasource-uid <uid>
```

The output is a top-level dashboard JSON (no `{"dashboard": ...,
"meta": ...}` wrapper). Import it via Grafana's UI ("Import" → upload
file) or push it to grafana.com.

## Push directly to a Grafana instance

```bash
export GRAFANA_URL=https://grafana.example
export GRAFANA_TOKEN=glsa_xxx        # or pass --token / --token-file

python3 generate.py --push
# resolved datasource uid <uid> from https://grafana.example
# pushed: uid=quectel-5g-monitor version=N → https://grafana.example/d/...
```

`--push` POSTs to `/api/dashboards/db`. If `--datasource-uid` isn't
explicit, the script queries `/api/datasources`, picks the first
`victoriametrics-metrics-datasource`, and bakes that uid in before
uploading. Pass `--no-overwrite` to fail on existing-uid collisions
instead of bumping the version.

`--push` deliberately does not also overwrite
`quectel-5g-monitor.json` — the bound-uid copy isn't what should be
checked in. To refresh the checked-in JSON, run `generate.py` (or
`generate.py --stdout > quectel-5g-monitor.json`) without `--push`.

## Data source assumptions

Datasource type: `victoriametrics-metrics-datasource`. Prometheus also
works but you may need to swap the type in the generated JSON before
importing — VictoriaMetrics-specific query options (e.g. `qryType` on
template variables) are tolerated by Prometheus but unused.

Metric sources expected:

| Metric prefix          | Producer                                       |
|------------------------|------------------------------------------------|
| `ping_*`               | telegraf `ping` plugin                         |
| `dns_query_*`          | telegraf `dns_query` plugin                    |
| `modem_*`              | `lua/prometheus-collectors/quectel.lua`        |
| `quectel_watchdog_*`   | `lua/prometheus-collectors/quectel-watchdog.lua` |

Template variables:

- `$router` — `label_values(ping_average_response_ms, host)` (multi).
- `$target` — `label_values(ping_average_response_ms, url)`.


# Alternative dashboard: InfluxQL history and latest poll

`generate_alternative.py` builds a second, unrelated dashboard
(`quectel-5g-alternative.json`, uid `quectel-5g-alternative`) for an
InfluxDB/Telegraf/Grafana stack. It is written from scratch rather than
re-templated: InfluxQL and PromQL share no syntax, and there is no InfluxQL
source dashboard to clone. `generate.py` and the published dashboard 24835 are
untouched by it.

## One plugin, and why the top row is not live

Every panel reads InfluxDB, through the measurements defined by
[`telegraf/quectel.conf`](../telegraf/quectel.conf): `quectel_lte`,
`quectel_nr5g`, `quectel_serving`, `quectel_carrier_pcc`/`_scc`,
`quectel_operator`, and `quectel_neighbour` in its short-retention bucket.

The *Now* row used to read the router's `/cgi-bin/quectel-status` directly,
through the Infinity plugin. Every value it showed is in InfluxDB too, and
reading it there:

- costs the router's AT bus nothing -- each refresh used to fire ten
  requests at the endpoint, from every open browser;
- needs no plugin, and no route from Grafana to the router;
- cannot turn "cannot read modem" into "no service". Infinity answers a
  missing JSON path with a query error, and the stat then showed its
  no-value text as though it were a reading.

What it gives up is freshness. The row is the **newest poll**, up to a poll
interval old, not a live read -- and aiming an antenna is what `5g-monitor`
is for.

Each *Now* query looks back a fixed window, `--recent-window`, rather than
over the dashboard's time range. Over the range, `last()` would keep showing
the final reading hours after polling stopped; bounded to about one poll, the
row goes blank instead. The window must cover Telegraf's poll interval plus
its `flush_interval` -- 75s by default, for 60s polls and a 10s flush -- and
every second beyond that is time a dropped carrier stays on screen, because
InfluxQL cannot select "only the newest poll". The two tables therefore carry
a **Seen** column: a row noticeably older than the rest has just gone.

## Generate

```bash
# default: writes quectel-5g-alternative.json with two placeholders --
# ${DS_INFLUXDB} and ${DS_INFLUXDB_SHORT} -- so importing it asks for two
# datasources. This is the copy to check in.
python3 generate_alternative.py

# bind to explicit datasource uids (skips the import prompt). Both are
# InfluxDB, and the short-retention one is not optional -- see "Two InfluxDB
# datasources" below for why the neighbour panels need their own.
python3 generate_alternative.py \
    --influxdb-uid <main uid> \
    --influxdb-short-uid <short-retention uid>

# if Telegraf polls at other than 60s, size the Now window to match:
# poll interval plus flush_interval
python3 generate_alternative.py --recent-window 40s
```

Binding only some of them is fine: whichever you leave out keeps its
placeholder and is asked for at import. Binding both InfluxDB uids to the
same datasource is refused outright, because the neighbour panel would then
query a measurement that is deliberately not in the main bucket and read "No
data" with nothing to say why.

### On Grafana 12 and later, bind the uids -- do not use the import prompt

**Grafana 13's import page cannot map two datasource inputs of the same
plugin.** `DS_INFLUXDB` and `DS_INFLUXDB_SHORT` are both `influxdb`; the form
accepts a different datasource in each picker and then applies the *first*
one's value to both. The neighbour panel silently lands on the main
datasource, where `quectel_neighbour` is deliberately absent, and reads "No
data". Nothing reports an error, and the picker you set is not what gets
saved.

Reproduced on 13.0.2 against a form verified to hold `DS_INFLUXDB = InfluxDB`
and `DS_INFLUXDB_SHORT = InfluxDB (short retention)`: every panel imported
onto `quectel-influx`, none onto `quectel-influx-short`. The same file with
the same clicks on 11.6.11 imports correctly, and the HTTP API maps it
correctly on both -- it is the import form, not the dashboard.

So on 12 or later, generate with both uids bound as above. That emits no
`__inputs` at all, so there is no prompt to get wrong. Read the uids off
`/api/datasources`:

```bash
curl -s -u admin:admin http://localhost:3000/api/datasources \
    | python3 -c 'import json,sys
for d in json.load(sys.stdin): print(d["uid"], d["name"])'
```

If you have already imported one the broken way, the repair is to open
*Neighbours reported*, set its datasource to the short-retention one, and
save -- or re-import a uid-bound copy over it.

The generator validates its own output before writing — duplicate panel ids,
panels running past column 24, targets without a `refId`, and targets pointing
at a different datasource than their panel. Grafana answers all four with a
blank panel and no error message.

## Two InfluxDB datasources, because there are two databases

Retention in InfluxDB 2.x belongs to the bucket, not to the measurement, so
neighbours -- whose `(pci, arfcn)` series turn over continuously on a moving
vessel -- live in a bucket that expires in a day while everything else is
kept. [`telegraf/quectel.conf`](../telegraf/quectel.conf) routes
`quectel_neighbour` there and `namedrop`s it from the main bucket.

A Grafana InfluxQL datasource carries exactly one database and no way to name
a retention policy per panel. The `"short"."quectel_neighbour"` qualification
that would have avoided a second datasource **is not honoured by 2.x's v1
compatibility layer** -- it answers with no series and no error at all. So the
short bucket has to be reachable as a database in its own right, and the
dashboard points a second datasource at it.

**Check before creating any mapping — you probably need none.** InfluxDB
auto-generates a read-only *virtual* DBRP for every bucket that has no
explicit one, with the database named after the bucket and `autogen` as the
default policy. That is exactly what this dashboard queries, so on a stock
2.x both databases already answer:

```bash
influx v1 dbrp list
```

Look for `systemhealth` and `systemhealth_short` in either table. If they are
under `VIRTUAL DBRP MAPPINGS (READ-ONLY)`, there is nothing to do. Only if a
bucket is missing from both — an older 2.x predating virtual mappings — create
it explicitly:

```bash
influx bucket list                       # note the two bucket ids
influx v1 dbrp create --db systemhealth --rp autogen \
    --bucket-id <systemhealth id> --default
influx v1 dbrp create --db systemhealth_short --rp autogen \
    --bucket-id <systemhealth_short id> --default
```

Note that creating an explicit mapping *replaces* the bucket's virtual one, so
a half-finished set is worse than none: `docker/` creates all of them together
for that reason. A `--db systemhealth --rp short` mapping belonged to an
earlier design that qualified the measurement; nothing queries it now.

And two Grafana datasources, both Query Language = InfluxQL with a
v1-compatible auth or token:

| Grafana datasource | Database             | Used by                    |
|--------------------|----------------------|----------------------------|
| main               | `systemhealth`       | every panel but the two neighbour ones |
| short retention    | `systemhealth_short` | *Neighbour cells*, *Neighbours reported* |

`docker/` builds exactly this, in
[`docker/influxdb/init/10-buckets-and-dbrp.sh`](../docker/influxdb/init/10-buckets-and-dbrp.sh)
and
[`docker/grafana/provisioning/datasources/datasources.yml`](../docker/grafana/provisioning/datasources/datasources.yml),
and `docker/verify.sh` asserts the neighbour panel is actually wired to the
second one -- provisioned is not the same as wired up.

**If the two neighbour panels are empty and every other panel works**, this
is where to look: they are on the main datasource. Open one and check.

## Thresholds

RSRP, RSRQ and SINR colours come from `lua/quectel/thresholds.lua`, so a green
here means what a green means in `5g-monitor`.

## 5g-collectd is not part of this

Every panel here reads either InfluxDB measurements written by
[`telegraf/quectel.conf`](../telegraf/quectel.conf) or the router's JSON
endpoint. Nothing reads what `5g-collectd` produces.

That is deliberate rather than an omission. `5g-collectd` carries a strict
subset — numbers only, no operator name and no neighbour list, because
collectd cannot express either — and running both would put two slightly
different copies of the same signal readings into InfluxDB, sampled moments
apart, certain to disagree eventually with nothing to say which is right.
Telegraf also derives collectd measurement names from the collectd type rather
than from anything this repository chooses, so panels against them cannot be
written blind in any case.

Pick one path. This one when there is a server running Telegraf; `5g-collectd`
when the router should keep its own data with no server at all, or to feed
LuCI's graphs.
