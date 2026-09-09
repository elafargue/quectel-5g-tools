#!/usr/bin/env python3
"""Generate the alternative Quectel dashboard: InfluxQL history + live snapshot.

`generate.py` builds the published dashboard (grafana.com id 24835) against
VictoriaMetrics/PromQL, by re-templating an upstream source dashboard. This is
a separate artifact for a different stack and is built from scratch, because
InfluxQL and PromQL share no syntax and there is no InfluxQL source to clone.

Two datasources, because the dashboard answers two different questions:

  * **InfluxDB (InfluxQL)** for history -- what the signal has been doing.
    Fed by `telegraf/quectel.conf`, which polls the router's JSON endpoint.
  * **Infinity** for the snapshot -- what the radio is attached to *right now*,
    read straight from the router and never stored. The cell and neighbour
    lists belong here: every (pci, arfcn) pair would otherwise be a series of
    its own, and on a vessel under way that set turns over continuously.

The history panels query the measurements defined in telegraf/quectel.conf --
quectel_lte, quectel_nr5g, quectel_carrier, quectel_neighbour -- and not the
collectd-sourced series from 5g-collectd. Telegraf's collectd parser derives
measurement names from the collectd type rather than from anything this
repository chooses, so those names depend on the local Telegraf config and
cannot be written blind.

Thresholds match lua/quectel/thresholds.lua, so a colour here means what the
same colour means in 5g-monitor.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

INFLUX_PLUGIN_ID = "influxdb"
INFLUX_PLUGIN_NAME = "InfluxDB"
INFINITY_PLUGIN_ID = "yesoreyeram-infinity-datasource"
INFINITY_PLUGIN_NAME = "Infinity"

DEFAULT_INFLUX_INPUT = "${DS_INFLUXDB}"
DEFAULT_INFINITY_INPUT = "${DS_INFINITY}"

DEFAULT_STATUS_URL = "http://192.168.8.1/cgi-bin/quectel-status"
# The InfluxQL database name, which a DBRP mapping resolves to a bucket.
DEFAULT_DATABASE = "systemhealth"
# Neighbours live under their own retention policy, mapped to a shorter-lived
# bucket; see telegraf/quectel.conf. InfluxDB 2.x expires whole buckets, never
# single measurements, so this split is the only way to keep them short.
DEFAULT_NEIGHBOUR_RP = "short"

OUT = Path(__file__).resolve().parent / "quectel-5g-alternative.json"

# lua/quectel/thresholds.lua
RSRP_STEPS = [("red", None), ("orange", -100), ("yellow", -90), ("green", -80)]
RSRQ_STEPS = [("red", None), ("orange", -15), ("yellow", -12), ("green", -10)]
SINR_STEPS = [("red", None), ("orange", 0), ("yellow", 13), ("green", 20)]


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def gp(x: int, y: int, w: int, h: int) -> dict:
    return {"h": h, "w": w, "x": x, "y": y}


def influx(uid: str) -> dict:
    return {"type": INFLUX_PLUGIN_ID, "uid": uid}


def infinity(uid: str) -> dict:
    return {"type": INFINITY_PLUGIN_ID, "uid": uid}


def steps(pairs) -> dict:
    return {
        "mode": "absolute",
        "steps": [
            {"color": c, "value": None if i == 0 else v}
            for i, (c, v) in enumerate(pairs)
        ],
    }


def iql(uid: str, ref: str, query: str, alias: str | None = None) -> dict:
    """A raw InfluxQL target.

    Raw rather than the builder form: the builder cannot express the GROUP BY
    on a tag alongside $__interval without the UI rewriting it, and these
    queries are meant to be read in the file rather than in a query editor.
    """
    t = {
        "datasource": influx(uid),
        "refId": ref,
        "query": query,
        "rawQuery": True,
        "resultFormat": "time_series",
    }
    if alias:
        t["alias"] = alias
    return t


def inf(uid: str, ref: str, url: str, selector: str, columns: list) -> dict:
    """An Infinity target reading one path out of the status JSON.

    `parser: backend` so the parsing happens in Grafana's backend rather than
    the browser: it keeps the router's address out of the page and lets the
    panel work when the browser cannot reach the router directly.
    """
    return {
        "datasource": infinity(uid),
        "refId": ref,
        "type": "json",
        "source": "url",
        "format": "table",
        "parser": "backend",
        "url": url,
        "url_options": {"method": "GET", "data": ""},
        "root_selector": selector,
        "columns": columns,
        "filters": [],
    }


def col(selector: str, text: str, kind: str = "string") -> dict:
    return {"selector": selector, "text": text, "type": kind}


def row(title: str, y: int, collapsed: bool = False) -> dict:
    return {
        "type": "row",
        "title": title,
        "gridPos": gp(0, y, 24, 1),
        "collapsed": collapsed,
        "panels": [],
    }


def stat(title, gridpos, targets, ds, unit=None, thresholds=None,
         text_mode="value", no_value="—", decimals=None) -> dict:
    field: dict = {
        "custom": {},
        "mappings": [],
        "noValue": no_value,
    }
    if unit:
        field["unit"] = unit
    if decimals is not None:
        field["decimals"] = decimals
    field["thresholds"] = (
        steps(thresholds) if thresholds else {"mode": "absolute",
                                              "steps": [{"color": "text",
                                                         "value": None}]}
    )
    return {
        "type": "stat",
        "title": title,
        "gridPos": gridpos,
        "datasource": ds,
        "targets": targets,
        "fieldConfig": {"defaults": field, "overrides": []},
        "options": {
            "reduceOptions": {"calcs": ["lastNotNull"], "fields": "",
                              "values": False},
            "orientation": "auto",
            "textMode": text_mode,
            "colorMode": "value" if thresholds else "none",
            "graphMode": "none",
            "justifyMode": "auto",
        },
    }


def table(title, gridpos, targets, ds, overrides=None, transformations=None) -> dict:
    return {
        "type": "table",
        "title": title,
        "gridPos": gridpos,
        "datasource": ds,
        "targets": targets,
        "fieldConfig": {
            "defaults": {
                "custom": {"align": "auto", "cellOptions": {"type": "auto"},
                           "inspect": False},
                "mappings": [],
                "thresholds": {"mode": "absolute",
                               "steps": [{"color": "text", "value": None}]},
            },
            "overrides": overrides or [],
        },
        "options": {"showHeader": True, "cellHeight": "sm",
                    "footer": {"show": False, "reducer": ["sum"],
                               "countRows": False, "fields": ""}},
        "transformations": transformations or [],
    }


def timeseries(title, gridpos, targets, ds, unit=None, thresholds=None,
               fill=10, min_=None, max_=None) -> dict:
    field: dict = {
        "custom": {
            "drawStyle": "line",
            "lineInterpolation": "linear",
            "lineWidth": 1,
            "fillOpacity": fill,
            "gradientMode": "opacity",
            "spanNulls": False,
            "showPoints": "never",
            "pointSize": 5,
            "axisPlacement": "auto",
            "axisLabel": "",
            "scaleDistribution": {"type": "linear"},
            "hideFrom": {"legend": False, "tooltip": False, "viz": False},
            "insertNulls": False,
            "stacking": {"group": "A", "mode": "none"},
            "thresholdsStyle": {"mode": "off"},
        },
        "mappings": [],
        "color": {"mode": "palette-classic"},
        "thresholds": steps(thresholds) if thresholds else {
            "mode": "absolute", "steps": [{"color": "green", "value": None}]},
    }
    if unit:
        field["unit"] = unit
    if min_ is not None:
        field["min"] = min_
    if max_ is not None:
        field["max"] = max_
    return {
        "type": "timeseries",
        "title": title,
        "gridPos": gridpos,
        "datasource": ds,
        "targets": targets,
        "fieldConfig": {"defaults": field, "overrides": []},
        "options": {
            "legend": {"calcs": ["min", "mean", "max", "lastNotNull"],
                       "displayMode": "table", "placement": "bottom",
                       "showLegend": True},
            "tooltip": {"mode": "multi", "sort": "desc"},
        },
    }


def state_timeline(title, gridpos, targets, ds, thresholds=None) -> dict:
    return {
        "type": "state-timeline",
        "title": title,
        "gridPos": gridpos,
        "datasource": ds,
        "targets": targets,
        "fieldConfig": {
            "defaults": {
                "custom": {"lineWidth": 0, "fillOpacity": 80,
                           "spanNulls": False,
                           "insertNulls": False,
                           "hideFrom": {"legend": False, "tooltip": False,
                                        "viz": False}},
                "mappings": [],
                "color": {"mode": "continuous-GrYlRd"},
                "thresholds": steps(thresholds) if thresholds else {
                    "mode": "absolute", "steps": [{"color": "green",
                                                   "value": None}]},
            },
            "overrides": [],
        },
        "options": {
            "mergeValues": True,
            "showValue": "auto",
            "alignValue": "center",
            "rowHeight": 0.9,
            "legend": {"displayMode": "list", "placement": "bottom",
                       "showLegend": True},
            "tooltip": {"mode": "single", "sort": "none"},
        },
    }


# ---------------------------------------------------------------------------
# panels
# ---------------------------------------------------------------------------

def build_panels(iu: str, fu: str, url: str, rp: str) -> list:
    """iu = InfluxDB uid, fu = Infinity uid, url = status endpoint, rp = neighbour RP."""
    panels: list = []

    # -- Now ----------------------------------------------------------------
    # Read live from the router. Nothing in this row is stored anywhere, which
    # is the point: it is the answer to "what is it on, right now", and it is
    # correct the instant the page loads rather than as of the last scrape.
    panels.append(row("Now", 0))

    panels.append(stat(
        "Network", gp(0, 1, 4, 4),
        [inf(fu, "A", url, "operator", [col("operator", "Operator")])],
        infinity(fu), text_mode="value", no_value="no service"))

    panels.append(stat(
        "Mode", gp(4, 1, 3, 4),
        [inf(fu, "A", url, "serving", [col("mode", "Mode")])],
        infinity(fu), no_value="—"))

    panels.append(stat(
        "RRC", gp(7, 1, 3, 4),
        [inf(fu, "A", url, "serving", [col("state", "State")])],
        infinity(fu), no_value="—"))

    # The two numbers you actually steer by. Thresholds are the ones
    # 5g-monitor colours its output with, so a green here is a green there.
    panels.append(stat(
        "NR RSRP", gp(10, 1, 4, 4),
        [inf(fu, "A", url, "serving.nr5g", [col("rsrp", "RSRP", "number")])],
        infinity(fu), unit="dBm", thresholds=RSRP_STEPS, no_value="no NR"))

    panels.append(stat(
        "NR SINR", gp(14, 1, 4, 4),
        [inf(fu, "A", url, "serving.nr5g", [col("sinr", "SINR", "number")])],
        infinity(fu), unit="dB", thresholds=SINR_STEPS, no_value="no NR"))

    panels.append(stat(
        "LTE RSRP", gp(18, 1, 3, 4),
        [inf(fu, "A", url, "serving.lte", [col("rsrp", "RSRP", "number")])],
        infinity(fu), unit="dBm", thresholds=RSRP_STEPS, no_value="—"))

    panels.append(stat(
        "LTE SINR", gp(21, 1, 3, 4),
        [inf(fu, "A", url, "serving.lte", [col("sinr", "SINR", "number")])],
        infinity(fu), unit="dB", thresholds=SINR_STEPS, no_value="—"))

    # The aggregated carriers, which is what 5g-info prints as its CA table.
    # Two targets because the primary is an object and the secondaries an
    # array; the merge transformation stacks them into one table.
    carrier_cols = [
        col("role", "Role"), col("rat", "RAT"), col("band", "Band", "number"),
        col("pci", "PCI", "number"),
        col("frequency_mhz", "MHz", "number"),
        col("bandwidth_mhz", "BW", "number"),
        col("rsrp", "RSRP", "number"), col("sinr", "SINR", "number"),
    ]
    panels.append(table(
        "Connected carriers", gp(0, 5, 12, 9),
        [inf(fu, "A", url, "ca.pcc", carrier_cols),
         inf(fu, "B", url, "ca.scc", carrier_cols)],
        infinity(fu),
        transformations=[{"id": "merge", "options": {}}],
        overrides=[
            _colour_override("RSRP", RSRP_STEPS, "dBm"),
            _colour_override("SINR", SINR_STEPS, "dB"),
        ]))

    # Neighbours: live only. Storing them is what would grow the index without
    # bound, so this panel is the reason the endpoint exists.
    panels.append(table(
        "Neighbour cells", gp(12, 5, 12, 9),
        [inf(fu, "A", url, "neighbours", [
            col("rat", "RAT"), col("scope", "Scope"),
            col("arfcn", "ARFCN", "number"), col("pci", "PCI", "number"),
            col("rsrp", "RSRP", "number"), col("rsrq", "RSRQ", "number"),
        ])],
        infinity(fu),
        transformations=[{"id": "sortBy", "options": {
            "fields": {}, "sort": [{"field": "RSRP", "desc": True}]}}],
        overrides=[_colour_override("RSRP", RSRP_STEPS, "dBm")]))

    # -- Signal history -----------------------------------------------------
    panels.append(row("Signal history", 14))

    panels.append(timeseries(
        "RSRP", gp(0, 15, 12, 8),
        [iql(iu, "A",
             'SELECT mean("rsrp") FROM "quectel_nr5g" WHERE $timeFilter '
             'GROUP BY time($__interval), "band" fill(none)', "NR $tag_band"),
         iql(iu, "B",
             'SELECT mean("rsrp") FROM "quectel_lte" WHERE $timeFilter '
             'GROUP BY time($__interval), "band" fill(none)', "LTE B$tag_band")],
        influx(iu), unit="dBm", thresholds=RSRP_STEPS))

    panels.append(timeseries(
        "SINR", gp(12, 15, 12, 8),
        [iql(iu, "A",
             'SELECT mean("sinr") FROM "quectel_nr5g" WHERE $timeFilter '
             'GROUP BY time($__interval), "band" fill(none)', "NR $tag_band"),
         iql(iu, "B",
             'SELECT mean("sinr") FROM "quectel_lte" WHERE $timeFilter '
             'GROUP BY time($__interval), "band" fill(none)', "LTE B$tag_band")],
        influx(iu), unit="dB", thresholds=SINR_STEPS))

    panels.append(timeseries(
        "RSRQ", gp(0, 23, 12, 6),
        [iql(iu, "A",
             'SELECT mean("rsrq") FROM "quectel_nr5g" WHERE $timeFilter '
             'GROUP BY time($__interval) fill(none)', "NR"),
         iql(iu, "B",
             'SELECT mean("rsrq") FROM "quectel_lte" WHERE $timeFilter '
             'GROUP BY time($__interval) fill(none)', "LTE")],
        influx(iu), unit="dB", thresholds=RSRQ_STEPS))

    # Which bands were carrying traffic, as bands rather than as numbers on an
    # axis. A handover reads as a change of block.
    panels.append(state_timeline(
        "Bands in use", gp(12, 23, 12, 6),
        [iql(iu, "A",
             'SELECT last("rsrp") FROM "quectel_carrier" WHERE $timeFilter '
             'GROUP BY time($__interval), "rat", "band" fill(none)',
             "$tag_rat $tag_band")],
        influx(iu), thresholds=RSRP_STEPS))

    # -- Where we were ------------------------------------------------------
    panels.append(row("Cells and coverage", 29))

    # A moving vessel changes site constantly; this is the record of it.
    panels.append(state_timeline(
        "Serving site (eNodeB)", gp(0, 30, 12, 6),
        [iql(iu, "A",
             'SELECT last("rsrp") FROM "quectel_lte" WHERE $timeFilter '
             'GROUP BY time($__interval), "cell_id" fill(none)', "$tag_cell_id")],
        influx(iu), thresholds=RSRP_STEPS))

    # How much company the serving cell has. A thinning neighbour list is the
    # early sign of running out of coverage, and it moves before RSRP does.
    panels.append(timeseries(
        "Neighbours visible", gp(12, 30, 12, 6),
        [iql(iu, "A",
             'SELECT count(distinct("pci")) FROM "%s"."quectel_neighbour" '
             'WHERE $timeFilter GROUP BY time($__interval), "scope" fill(0)'
             % rp,
             "$tag_scope")],
        influx(iu), fill=30))

    panels.append(timeseries(
        "Aggregated carriers", gp(0, 36, 12, 6),
        [iql(iu, "A",
             'SELECT count(distinct("band")) FROM "quectel_carrier" '
             'WHERE $timeFilter GROUP BY time($__interval), "rat" fill(0)',
             "$tag_rat")],
        influx(iu), fill=30, min_=0))

    # Frequency rather than band number: on a boat the interesting question is
    # usually whether it fell back to low band, and megahertz answers that
    # without having to remember which band is which.
    panels.append(timeseries(
        "Carrier frequency", gp(12, 36, 12, 6),
        [iql(iu, "A",
             'SELECT mean("frequency_mhz") FROM "quectel_carrier" '
             'WHERE $timeFilter GROUP BY time($__interval), "rat", "band" '
             'fill(none)', "$tag_rat $tag_band")],
        # Grafana has no megahertz unit and "hertz" would label 1840 MHz as
        # 1840 Hz. A custom suffix is the honest option.
        influx(iu), unit="suffix:MHz"))

    return panels


def _colour_override(field: str, thresholds, unit: str) -> dict:
    return {
        "matcher": {"id": "byName", "options": field},
        "properties": [
            {"id": "unit", "value": unit},
            {"id": "thresholds", "value": steps(thresholds)},
            {"id": "custom.cellOptions",
             "value": {"type": "color-text"}},
        ],
    }


# ---------------------------------------------------------------------------
# dashboard
# ---------------------------------------------------------------------------

def _assign_ids(panels: list, start: int = 1) -> int:
    next_id = start
    for panel in panels:
        panel["id"] = next_id
        next_id += 1
        if panel.get("type") == "row":
            next_id = _assign_ids(panel.get("panels", []) or [], next_id)
    return next_id


def _validate(panels: list, seen: set | None = None) -> None:
    """Catch the mistakes Grafana answers with a blank panel and no error.

    A duplicate panel id silently drops one of the pair on import; a panel
    running past column 24 wraps somewhere unintended; a target with no refId
    is skipped without complaint. None of these fail loudly enough to notice
    while editing a 45KB file by hand.
    """
    seen = seen if seen is not None else set()
    for p in panels:
        pid = p.get("id")
        if pid in seen:
            raise SystemExit(f"duplicate panel id {pid} ({p.get('title')!r})")
        seen.add(pid)

        g = p["gridPos"]
        if g["x"] + g["w"] > 24:
            raise SystemExit(
                f"panel {p.get('title')!r} runs past column 24 "
                f"(x={g['x']} w={g['w']})")

        if p.get("type") == "row":
            _validate(p.get("panels", []) or [], seen)
            continue

        if not p.get("datasource"):
            raise SystemExit(f"panel {p.get('title')!r} has no datasource")
        refs = set()
        for t in p.get("targets", []) or []:
            ref = t.get("refId")
            if not ref:
                raise SystemExit(f"panel {p.get('title')!r} has a target with no refId")
            if ref in refs:
                raise SystemExit(f"panel {p.get('title')!r} reuses refId {ref}")
            refs.add(ref)
            if t.get("datasource", {}).get("type") != p["datasource"]["type"]:
                raise SystemExit(
                    f"panel {p.get('title')!r} target {ref} points at a "
                    f"different datasource than the panel")


def build_dashboard(influx_uid: str, infinity_uid: str, url: str,
                    rp: str, include_inputs: bool) -> dict:
    panels = build_panels(influx_uid, infinity_uid, url, rp)
    _assign_ids(panels)
    _validate(panels)

    dash: dict = {
        "title": "Quectel 5G — Alternative (InfluxQL + live)",
        "description": (
            "Live radio snapshot read from the router plus signal history from "
            "InfluxDB. Generated by "
            "quectel-5g-tools/grafana/generate_alternative.py."
        ),
        "uid": "quectel-5g-alternative",
        "tags": ["5g", "lte", "quectel", "openwrt", "influxdb"],
        "timezone": "browser",
        # The snapshot row is only as current as the dashboard refresh, and the
        # endpoint is cheap because the router caches AT reads.
        "refresh": "30s",
        "schemaVersion": 42,
        "editable": True,
        "panels": panels,
        "time": {"from": "now-6h", "to": "now"},
        "annotations": {"list": [{
            "builtIn": 1,
            "datasource": {"type": "grafana", "uid": "-- Grafana --"},
            "enable": True, "hide": True,
            "iconColor": "rgba(0, 211, 255, 1)",
            "name": "Annotations & Alerts", "type": "dashboard",
        }]},
        "templating": {"list": []},
        "links": [],
    }

    if include_inputs:
        dash["__inputs"] = [
            {"name": "DS_INFLUXDB", "label": INFLUX_PLUGIN_NAME,
             "description": "InfluxDB, queried with InfluxQL",
             "type": "datasource", "pluginId": INFLUX_PLUGIN_ID,
             "pluginName": INFLUX_PLUGIN_NAME},
            {"name": "DS_INFINITY", "label": INFINITY_PLUGIN_NAME,
             "description": "Infinity, for the live status endpoint",
             "type": "datasource", "pluginId": INFINITY_PLUGIN_ID,
             "pluginName": INFINITY_PLUGIN_NAME},
        ]
        dash["__requires"] = [
            {"type": "grafana", "id": "grafana", "name": "Grafana",
             "version": "11.0.0"},
            {"type": "datasource", "id": INFLUX_PLUGIN_ID,
             "name": INFLUX_PLUGIN_NAME, "version": "1.0.0"},
            {"type": "datasource", "id": INFINITY_PLUGIN_ID,
             "name": INFINITY_PLUGIN_NAME, "version": "2.0.0"},
        ]
    return dash


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--stdout", action="store_true",
                    help="write to stdout instead of the JSON file")
    ap.add_argument("--influxdb-uid", default=DEFAULT_INFLUX_INPUT,
                    help="bind to an explicit InfluxDB datasource uid")
    ap.add_argument("--infinity-uid", default=DEFAULT_INFINITY_INPUT,
                    help="bind to an explicit Infinity datasource uid")
    ap.add_argument("--url", default=DEFAULT_STATUS_URL,
                    help="the router's JSON status endpoint")
    ap.add_argument("--neighbour-rp", default=DEFAULT_NEIGHBOUR_RP,
                    help="retention policy holding quectel_neighbour")
    args = ap.parse_args()

    # Explicit uids mean the dashboard is bound to one instance, so the import
    # prompt would have nothing to ask about.
    include_inputs = (args.influxdb_uid == DEFAULT_INFLUX_INPUT
                      and args.infinity_uid == DEFAULT_INFINITY_INPUT)

    dash = build_dashboard(args.influxdb_uid, args.infinity_uid,
                           args.url, args.neighbour_rp, include_inputs)
    text = json.dumps(dash, indent=2, sort_keys=False) + "\n"

    if args.stdout:
        sys.stdout.write(text)
    else:
        OUT.write_text(text)
        print(f"wrote {OUT} ({len(text)} bytes, {_count(dash['panels'])} panels)",
              file=sys.stderr)
    return 0


def _count(panels: list) -> int:
    n = 0
    for p in panels:
        n += 1
        n += _count(p.get("panels", []) or [])
    return n


if __name__ == "__main__":
    raise SystemExit(main())
