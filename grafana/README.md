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


# Alternative dashboard: InfluxQL + live snapshot

`generate_alternative.py` builds a second, unrelated dashboard
(`quectel-5g-alternative.json`, uid `quectel-5g-alternative`) for an
InfluxDB/Telegraf/Grafana stack. It is written from scratch rather than
re-templated: InfluxQL and PromQL share no syntax, and there is no InfluxQL
source dashboard to clone. `generate.py` and the published dashboard 24835 are
untouched by it.

## Two datasources, because there are two questions

**InfluxDB (InfluxQL)** answers *what has the signal been doing* — the history
rows. It queries the measurements defined by
[`telegraf/quectel.conf`](../telegraf/quectel.conf): `quectel_lte`,
`quectel_nr5g`, `quectel_carrier`, `quectel_neighbour`.

**Infinity** answers *what is it attached to right now* — the whole top row,
read straight from the router's `/cgi-bin/quectel-status` endpoint and stored
nowhere. The carrier and neighbour tables live here deliberately: every
`(pci, arfcn)` pair would otherwise become a series of its own, and on a vessel
under way that set turns over continuously. A live panel costs nothing and is
correct the instant the page loads.

Install the Infinity plugin first:

```bash
grafana-cli plugins install yesoreyeram-infinity-datasource
```

## Generate

```bash
# default: writes quectel-5g-alternative.json with ${DS_INFLUXDB} and
# ${DS_INFINITY} placeholders, ready to import.
python3 generate_alternative.py

# point it at the router's endpoint if it is not on 192.168.8.1
python3 generate_alternative.py --url http://10.0.0.1/cgi-bin/quectel-status

# bind to explicit datasource uids (skips the import prompt)
python3 generate_alternative.py --influxdb-uid <uid> --infinity-uid <uid>

# neighbours live under their own retention policy; name it if it is not
# called "short"
python3 generate_alternative.py --neighbour-rp short
```

The generator validates its own output before writing — duplicate panel ids,
panels running past column 24, targets without a `refId`, and targets pointing
at a different datasource than their panel. Grafana answers all four with a
blank panel and no error message.

## InfluxDB 2.x with InfluxQL

Querying 2.x with InfluxQL needs a DBRP mapping per retention policy, so the
`"short"."quectel_neighbour"` in the neighbour panel resolves to the right
bucket:

```bash
influx v1 dbrp create --db boat --rp autogen --bucket-id <boat bucket id> --default
influx v1 dbrp create --db boat --rp short   --bucket-id <short bucket id>
```

The Grafana datasource then wants Query Language = InfluxQL, the database name
(`boat`), and a v1-compatible auth or token.

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
