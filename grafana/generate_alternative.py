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
DEFAULT_INFLUX_SHORT_INPUT = "${DS_INFLUXDB_SHORT}"

DEFAULT_STATUS_URL = "http://192.168.8.1/cgi-bin/quectel-status"
# The InfluxQL database name, which a DBRP mapping resolves to a bucket.
DEFAULT_DATABASE = "systemhealth"
# Neighbours live in a shorter-lived bucket of their own; see
# telegraf/quectel.conf. InfluxDB 2.x expires whole buckets and never single
# measurements, so the split is the only way to keep them short -- and it has
# to be reached as a separate database, because 2.x's v1 layer answers a
# "short"."measurement" qualification with no series and no error.

OUT = Path(__file__).resolve().parent / "quectel-5g-alternative.json"

# lua/quectel/thresholds.lua
RSRP_STEPS = [("red", None), ("orange", -100), ("yellow", -90), ("green", -80)]
RSRQ_STEPS = [("red", None), ("orange", -15), ("yellow", -12), ("green", -10)]
SINR_STEPS = [("red", None), ("orange", 0), ("yellow", 13), ("green", 20)]


# ---------------------------------------------------------------------------
# glossary
# ---------------------------------------------------------------------------
#
# Panel descriptions render as markdown behind the small "i" in a panel's
# corner. They are written for the person reading the dashboard rather than
# the person who built it, so they expand the acronym before using it:
# somebody turning an antenna by what the screen says should not have to know
# what RSRQ stands for to know whether the number in front of them is bad.
#
# The three signal metrics are defined once and composed into every panel that
# shows them, so the explanation of RSRP cannot drift between the live stat and
# the history graph. The thresholds quoted are RSRP_STEPS / RSRQ_STEPS /
# SINR_STEPS above -- change those and these sentences become wrong, which is
# the one coupling here worth remembering.

RSRP_DOC = (
    "**RSRP** is raw signal strength, in dBm: how much of the tower's signal "
    "reaches the antenna. It is always negative and closer to zero is "
    "stronger -- green from -80, red below -100."
)

SINR_DOC = (
    "**SINR** is signal *quality*, in dB: how far the wanted signal stands "
    "above noise and interference. This is the one that predicts speed -- "
    "green from 20, red below 0, where the link struggles however healthy "
    "RSRP looks."
)

RSRQ_DOC = (
    "**RSRQ** is quality in dB adjusted for how busy the cell is. It sags "
    "when the tower is congested while RSRP holds steady, so a fall here "
    "with no fall in strength usually means other users rather than worse "
    "aim. Green from -10, red below -15."
)

LIVE_DOC = (
    "Read live from the router each time the dashboard refreshes, and stored "
    "nowhere -- this is *now*, not the last scrape."
)

GAPS_DOC = (
    "Gaps are polls where nothing was reported, drawn as a break rather than "
    "as a zero -- a reading nobody took is not a reading of zero."
)


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
         text_mode="value", no_value="—", decimals=None,
         string_value=False, description="") -> dict:
    """string_value picks up a text field rather than a number.

    reduceOptions.fields defaults to "", which Grafana reads as *numeric
    fields only*. A frame carrying one string column then has nothing to
    reduce and the panel shows its noValue text -- so an operator name
    renders as "no service" while the query behind it is returning "KT"
    perfectly well. "/.*/" matches every field regardless of type.
    """
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
        "description": description,
        "gridPos": gridpos,
        "datasource": ds,
        "targets": targets,
        "fieldConfig": {"defaults": field, "overrides": []},
        "options": {
            "reduceOptions": {"calcs": ["lastNotNull"],
                              "fields": "/.*/" if string_value else "",
                              "values": False},
            "orientation": "auto",
            "textMode": text_mode,
            "colorMode": "value" if thresholds else "none",
            "graphMode": "none",
            "justifyMode": "auto",
        },
    }


def table(title, gridpos, targets, ds, overrides=None, transformations=None,
          description="") -> dict:
    return {
        "type": "table",
        "title": title,
        "description": description,
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
               fill=10, min_=None, max_=None, description="") -> dict:
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
        "description": description,
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


def state_timeline(title, gridpos, targets, ds, thresholds=None,
                   description="") -> dict:
    return {
        "type": "state-timeline",
        "title": title,
        "description": description,
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

def build_panels(iu: str, fu: str, url: str, su: str) -> list:
    """iu = InfluxDB uid, fu = Infinity uid, url = status endpoint,
    su = uid of the datasource holding the short-retention neighbour bucket."""
    panels: list = []

    # -- Now ----------------------------------------------------------------
    # Read live from the router. Nothing in this row is stored anywhere, which
    # is the point: it is the answer to "what is it on, right now", and it is
    # correct the instant the page loads rather than as of the last scrape.
    panels.append(row("Now", 0))

    panels.append(stat(
        "Network", gp(0, 1, 4, 4),
        [inf(fu, "A", url, "operator", [col("operator", "Operator")])],
        infinity(fu), text_mode="value", no_value="no service",
        string_value=True,
        description=(
            "The mobile network the modem is registered with. " + LIVE_DOC +
            "\n\nReads *no service* when the modem is not registered at all "
            "-- searching, out of coverage, or no usable SIM.")))

    panels.append(stat(
        "Mode", gp(4, 1, 3, 4),
        [inf(fu, "A", url, "serving", [col("mode", "Mode")])],
        infinity(fu), no_value="—", string_value=True,
        description=(
            "How 5G is attached. `NSA` (non-standalone) is 5G carried on top "
            "of an LTE anchor and is the usual case; `SA` is standalone 5G "
            "with no LTE underneath.\n\nA dash means there is no 5G leg at "
            "all and the modem is on LTE alone -- the same condition the NR "
            "panels show as *no NR*.")))

    panels.append(stat(
        "RRC", gp(7, 1, 3, 4),
        [inf(fu, "A", url, "serving", [col("state", "State")])],
        infinity(fu), no_value="—", string_value=True,
        description=(
            "The radio connection state the modem reports: `CONNECT` while a "
            "link is actively carrying data, `NOCONN` when it is camped on a "
            "cell with nothing to send, `SEARCH` while looking for one, "
            "`LIMSRV` for limited service (emergency calls only).\n\n"
            "`NOCONN` is **not** a fault. An idle link sits there most of the "
            "time; it says nothing about signal quality.")))

    # The two numbers you actually steer by. Thresholds are the ones
    # 5g-monitor colours its output with, so a green here is a green there.
    panels.append(stat(
        "NR RSRP", gp(10, 1, 4, 4),
        [inf(fu, "A", url, "serving.nr5g", [col("rsrp", "RSRP", "number")])],
        infinity(fu), unit="dBm", thresholds=RSRP_STEPS, no_value="no NR",
        description=(
            RSRP_DOC + "\n\nThis is the 5G carrier. Reads *no NR* whenever "
            "the 5G leg is detached and the modem is running on LTE alone.\n\n"
            + LIVE_DOC)))

    panels.append(stat(
        "NR SINR", gp(14, 1, 4, 4),
        [inf(fu, "A", url, "serving.nr5g", [col("sinr", "SINR", "number")])],
        infinity(fu), unit="dB", thresholds=SINR_STEPS, no_value="no NR",
        description=(
            SINR_DOC + "\n\nThis is the 5G carrier, and it is the value to "
            "aim a directional antenna by -- the one `5g-monitor` turns into "
            "beeps so you can point without watching a screen.\n\n"
            + LIVE_DOC)))

    panels.append(stat(
        "LTE RSRP", gp(18, 1, 3, 4),
        [inf(fu, "A", url, "serving.lte", [col("rsrp", "RSRP", "number")])],
        infinity(fu), unit="dBm", thresholds=RSRP_STEPS, no_value="—",
        description=(
            RSRP_DOC + "\n\nThis is the 4G carrier. In NSA mode it is also "
            "the anchor the 5G leg is bolted to, so it is worth watching even "
            "when 5G is doing the work: lose the anchor and the 5G goes with "
            "it.\n\n" + LIVE_DOC)))

    panels.append(stat(
        "LTE SINR", gp(21, 1, 3, 4),
        [inf(fu, "A", url, "serving.lte", [col("sinr", "SINR", "number")])],
        infinity(fu), unit="dB", thresholds=SINR_STEPS, no_value="—",
        description=SINR_DOC + "\n\nThis is the 4G carrier.\n\n" + LIVE_DOC))

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
        transformations=[
            {"id": "merge", "options": {}},
            _order("Role", "RAT", "Band", "PCI", "MHz", "BW", "RSRP", "SINR"),
        ],
        overrides=[
            _width("Role", 60), _width("RAT", 55), _width("Band", 60),
            _width("PCI", 60), _width("MHz", 80, decimals=1),
            _width("BW", 60),
            _colour_override("RSRP", RSRP_STEPS, "dBm", 80),
            _colour_override("SINR", SINR_STEPS, "dB", 70),
        ],
        description=(
            "Every carrier the modem is using at once. Mobile networks bond "
            "several channels together (carrier aggregation) to go faster, so "
            "**more rows here generally means more throughput** -- this is "
            "often what explains a speed change the signal numbers do not."
            "\n\n"
            "- **Role** -- `pcc` is the primary carrier, `scc` a secondary "
            "added on top.\n"
            "- **Band** -- the block of spectrum. Low bands (LTE 20, 5G n28) "
            "travel far and pass through obstacles; high ones (LTE 7, n78) "
            "are much faster but shorter-ranged.\n"
            "- **PCI** -- identifies which cell of that band, out of 504 "
            "possible codes. A change of PCI is a change of cell.\n"
            "- **MHz / BW** -- centre frequency and channel width. Wider is "
            "faster.\n\n" + LIVE_DOC)))

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
        transformations=[
            _order("RAT", "Scope", "ARFCN", "PCI", "RSRP", "RSRQ"),
            {"id": "sortBy", "options": {
                "fields": {}, "sort": [{"field": "RSRP", "desc": True}]}},
        ],
        overrides=[
            _width("RAT", 55), _width("Scope", 70), _width("ARFCN", 80),
            _width("PCI", 60),
            _colour_override("RSRP", RSRP_STEPS, "dBm", 85),
            _colour_override("RSRQ", RSRQ_STEPS, "dB", 80),
        ],
        description=(
            "Other cells the modem can hear but is **not** using, strongest "
            "first. These are the candidates it would hand over to, so the "
            "list is a measure of how much company you have: a healthy list "
            "means somewhere to go if the serving cell fades, and a list "
            "thinning towards empty is the first sign of running out of "
            "coverage.\n\n"
            "- **Scope** -- `intra` is a neighbour on the same frequency as "
            "the serving cell, `inter` one on a different frequency.\n"
            "- **ARFCN** -- the channel number the cell transmits on.\n"
            "- **PCI** -- identifies the cell within that channel.\n\n"
            + LIVE_DOC + " The history of this list is summarised by "
            "*Neighbours reported* below; the rows themselves are never "
            "kept.")))

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
        influx(iu), unit="dBm", thresholds=RSRP_STEPS,
        description=(
            RSRP_DOC + "\n\nOne line per band, so a handover onto different "
            "spectrum shows as one line stopping and another starting rather "
            "than as a jump in a single line. " + GAPS_DOC)))

    panels.append(timeseries(
        "SINR", gp(12, 15, 12, 8),
        [iql(iu, "A",
             'SELECT mean("sinr") FROM "quectel_nr5g" WHERE $timeFilter '
             'GROUP BY time($__interval), "band" fill(none)', "NR $tag_band"),
         iql(iu, "B",
             'SELECT mean("sinr") FROM "quectel_lte" WHERE $timeFilter '
             'GROUP BY time($__interval), "band" fill(none)', "LTE B$tag_band")],
        influx(iu), unit="dB", thresholds=SINR_STEPS,
        description=(
            SINR_DOC + "\n\nWorth reading against RSRP to its left: **"
            "strength holding steady while quality falls is interference, "
            "not a pointing problem**, and no amount of turning the antenna "
            "will fix it. Both falling together is a coverage or aim "
            "problem. " + GAPS_DOC)))

    panels.append(timeseries(
        "RSRQ", gp(0, 23, 12, 6),
        [iql(iu, "A",
             'SELECT mean("rsrq") FROM "quectel_nr5g" WHERE $timeFilter '
             'GROUP BY time($__interval) fill(none)', "NR"),
         iql(iu, "B",
             'SELECT mean("rsrq") FROM "quectel_lte" WHERE $timeFilter '
             'GROUP BY time($__interval) fill(none)', "LTE")],
        influx(iu), unit="dB", thresholds=RSRQ_STEPS,
        description=RSRQ_DOC + "\n\n" + GAPS_DOC))

    # What was carrying traffic, as blocks rather than as numbers on an axis:
    # a handover reads as a change of block, and losing a carrier as a row
    # that stops.
    #
    # Carriers, not bands, and grouped by role as well -- intra-band
    # aggregation is ordinary (KT runs B3 at EARFCN 1550 and 1694 at once),
    # and grouping on band alone gives two rows both labelled "lte 3" with
    # nothing to tell them apart. Role has to appear in the alias too, not
    # just in the GROUP BY: the alias replaces the series name outright, so a
    # tag missing from it is invisible however the query grouped.
    #
    # Rows are every carrier seen anywhere in the window, not the ones up
    # right now, so widening the time range adds rows for carriers long since
    # left behind. That is the intent for a history panel -- the top row is
    # where "right now" lives.
    panels.append(state_timeline(
        "Carriers in use", gp(12, 23, 12, 6),
        [iql(iu, "A",
             'SELECT last("rsrp") FROM "quectel_carrier_pcc", '
             '"quectel_carrier_scc" WHERE $timeFilter '
             'GROUP BY time($__interval), "role", "rat", "band" fill(none)',
             "$tag_role $tag_rat $tag_band")],
        influx(iu), thresholds=RSRP_STEPS,
        description=(
            "Which carriers were actually in use over time, as blocks rather "
            "than lines. Each row is one carrier and its colour is that "
            "carrier's RSRP on the usual scale, so a row that turns red was "
            "still in use but weak.\n\n"
            "**Row labels read \"role technology band\".** `pcc lte 3` is the "
            "primary carrier on LTE band 3; `scc 5g 78` a secondary on 5G "
            "n78. Role is part of the label because intra-band aggregation is "
            "ordinary -- two carriers can sit on the same band at once, and "
            "without the role there would be nothing to tell them apart."
            "\n\n"
            "**The number inside each block is that carrier's RSRP in dBm** "
            "-- signal strength, always negative, closer to zero is stronger, "
            "green from -80 and red below -100 like everywhere else here. It "
            "is not a count, a band number or an id. The unit is left off so "
            "that narrow blocks stay readable, and Grafana drops the number "
            "altogether when a block is too narrow to fit it; the colour "
            "carries the same reading either way.\n\n"
            "**A row that stops is a carrier that was dropped; a row that "
            "starts is one that was added.** That is the quickest read of a "
            "handover or of the 5G leg detaching.\n\n"
            "Rows are every carrier seen anywhere in the selected time range, "
            "not the ones up right now, so widening the range adds rows for "
            "carriers long since left behind. *Connected carriers* at the top "
            "of the dashboard is where \"right now\" lives.")))

    # -- Where we were ------------------------------------------------------
    panels.append(row("Cells and coverage", 29))

    # A moving vessel changes site constantly; this is the record of it.
    #
    # As a value rather than as a series per site: cell_id is a different
    # string for every cell ever visited, so grouping by it would need it as a
    # tag, and a tag lives as long as the bucket -- two years of every cell on
    # the voyage. enodeb is the same identity as a number, so the graph steps
    # at each handover and the index stays bounded.
    panels.append(timeseries(
        "Serving site (eNodeB)", gp(0, 30, 12, 6),
        [iql(iu, "A",
             'SELECT last("enodeb") FROM "quectel_lte" WHERE $timeFilter '
             'GROUP BY time($__interval) fill(previous)', "eNodeB")],
        influx(iu), fill=0,
        description=(
            "Which LTE base station is serving the modem, as its numeric "
            "identity (the eNodeB id, shared by all cells on one mast).\n\n"
            "**The value on the axis means nothing as a quantity -- read the "
            "steps, not the height.** Each step is a handover to a different "
            "site. A flat line is a link sitting still on one mast; frequent "
            "steps mean the vessel is moving between them, and a step that "
            "coincides with a dip in RSRP above is a handover you felt.")))

    # How much company the serving cell has. A thinning neighbour list is the
    # early sign of running out of coverage, and it moves before RSRP does.
    #
    # Its own datasource, because neighbours live in a bucket that expires in a
    # day and an InfluxQL datasource carries exactly one database. The
    # "short"."quectel_neighbour" qualification that would avoid a second
    # datasource is not honoured by InfluxDB 2.x's v1 layer -- it answers with
    # no series and no error at all.
    #
    # count() over a field rather than distinct() over a tag: pci is a tag
    # here, and InfluxQL's distinct() does not operate on tags. It returns
    # nothing, silently, which is a slow way to find out.
    #
    # fill(none), not fill(0). $__interval is sized to the panel width and is
    # routinely finer than the poll interval, so most buckets contain no
    # sample at all. fill(0) draws those as "no neighbours" -- a measurement
    # nobody took, rendered as a reading of zero, which is the same lie
    # get_status() and 5g-collectd go to some trouble to avoid.
    panels.append(timeseries(
        "Neighbours reported", gp(12, 30, 12, 6),
        [iql(su, "A",
             'SELECT count("rsrp") FROM "quectel_neighbour" '
             'WHERE $timeFilter GROUP BY time($__interval), "scope" '
             'fill(none)',
             "$tag_scope")],
        influx(su), fill=30,
        description=(
            "How many neighbouring cells the modem could hear at each poll, "
            "split into `intra` (same frequency as the serving cell) and "
            "`inter` (a different one). The cells themselves are listed in "
            "*Neighbour cells* at the top of the dashboard.\n\n"
            "**A thinning count is an early warning of running out of "
            "coverage, and it usually moves before RSRP does** -- you lose "
            "the alternatives before you lose the cell you are on.\n\n"
            "Gaps are polls that returned no sample, drawn as a break rather "
            "than as zero: no neighbours reported and no reading taken are "
            "different things.\n\n"
            "Kept for 24 hours only -- neighbour lists churn constantly on a "
            "moving vessel, so they live in a short-retention bucket of their "
            "own. Ranges longer than a day will look empty here.")))

    # count() over a field, never distinct() over a tag. `band` is a tag here,
    # and InfluxQL's distinct() does not operate on tags -- it answers with no
    # series at all, so the panel reads "No data" while the measurement behind
    # it is perfectly well populated. Each carrier contributes one point per
    # poll, so counting a field it always carries counts the carriers.
    panels.append(timeseries(
        "Aggregated carriers", gp(0, 36, 12, 6),
        [iql(iu, "A",
             'SELECT count("rsrp") FROM "quectel_carrier_pcc", '
             '"quectel_carrier_scc" WHERE $timeFilter '
             'GROUP BY time($__interval), "rat" fill(none)',
             "$tag_rat")],
        influx(iu), fill=30, min_=0,
        description=(
            "How many carriers were bonded together at each poll, split by "
            "technology. This is the count behind the *Connected carriers* "
            "table at the top.\n\n"
            "More carriers means more spectrum in use and, broadly, more "
            "speed, so **a drop here often explains a slowdown that the "
            "signal panels do not** -- the network can withdraw a carrier "
            "under load while every dB stays exactly where it was.")))

    # Frequency rather than band number: on a boat the interesting question is
    # usually whether it fell back to low band, and megahertz answers that
    # without having to remember which band is which.
    panels.append(timeseries(
        "Carrier frequency", gp(12, 36, 12, 6),
        [iql(iu, "A",
             'SELECT mean("frequency_mhz") FROM "quectel_carrier_pcc", '
             '"quectel_carrier_scc" WHERE $timeFilter '
             'GROUP BY time($__interval), "rat", "band" fill(none)',
             "$tag_rat $tag_band")],
        # Grafana has no megahertz unit and "hertz" would label 1840 MHz as
        # 1840 Hz. A custom suffix is the honest option.
        influx(iu), unit="suffix:MHz",
        description=(
            "The centre frequency of each carrier in use. Shown as megahertz "
            "rather than as a band number because the useful question at sea "
            "is usually *did it fall back to low band*, and that is easier to "
            "see on an axis than to remember band by band.\n\n"
            "Roughly: **below 1000 MHz** travels a long way and passes "
            "through obstacles but is slow; **1800-2600 MHz** is the middle "
            "ground; **3400-3800 MHz** (n78) is the fastest and the shortest-"
            "ranged. A line dropping to the bottom of the chart is the modem "
            "trading speed for reach, which is usually the right call and "
            "always worth knowing about.")))

    return panels


def _width(field: str, px: int, decimals: int | None = None) -> dict:
    """Pin a table column's width.

    Grafana sizes table columns to their content and lets the total overflow
    the panel, which puts the right-hand columns behind a horizontal scrollbar
    -- and RSRP and SINR, the two worth looking at, are the rightmost. Eight
    auto-sized columns do not fit in half a 24-unit row; eight pinned ones do.
    """
    props = [{"id": "custom.width", "value": px}]
    if decimals is not None:
        props.append({"id": "decimals", "value": decimals})
    return {"matcher": {"id": "byName", "options": field}, "properties": props}


def _order(*names: str) -> dict:
    """Lay table columns out in the given order.

    Without this Grafana uses whatever order the frame carries, which after a
    merge is alphabetical -- BW, Band, MHz, PCI, RAT -- readable only by
    accident, and it puts the identity columns after the measurements.
    """
    return {"id": "organize",
            "options": {"indexByName": {n: i for i, n in enumerate(names)},
                        "excludeByName": {}, "renameByName": {}}}


def _colour_override(field: str, thresholds, unit: str,
                     width: int | None = None) -> dict:
    props = [
        {"id": "unit", "value": unit},
        {"id": "thresholds", "value": steps(thresholds)},
        {"id": "custom.cellOptions", "value": {"type": "color-text"}},
    ]
    if width is not None:
        props.append({"id": "custom.width", "value": width})
    return {"matcher": {"id": "byName", "options": field}, "properties": props}


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
                    short_uid: str) -> dict:
    # The two InfluxDB uids must differ, and nothing downstream would say so.
    # quectel_neighbour is namedrop'd out of the main bucket by
    # telegraf/quectel.conf, so pointing the neighbour panel at the main
    # datasource queries a measurement that is deliberately not there: the
    # panel reads "No data", Grafana reports no error, and the dashboard looks
    # built correctly. Exactly the silent-blank-panel failure _validate()
    # exists for, arriving through the one door it cannot see -- it inspects
    # panels, and by then both uids are simply strings that happen to match.
    #
    # Grafana's import dialog is the likelier route in than this CLI: both
    # inputs declare pluginId "influxdb", so it offers two dropdowns listing
    # the same datasources, and picking one twice is an easy mistake to make
    # and a hard one to see.
    if short_uid == influx_uid:
        raise SystemExit(
            f"the short-retention uid and the main InfluxDB uid are both "
            f"{influx_uid!r}; they must be different datasources. The "
            f"neighbour bucket is a database of its own (systemhealth_short) "
            f"because quectel_neighbour is kept out of the main one -- "
            f"pointing both at it leaves 'Neighbours reported' permanently "
            f"empty with no error to say why.")

    panels = build_panels(influx_uid, infinity_uid, url, short_uid)
    _assign_ids(panels)
    _validate(panels)

    dash: dict = {
        "title": "Quectel 5G — Alternative (InfluxQL + live)",
        "description": (
            "Live radio snapshot read from the router plus signal history "
            "from InfluxDB. Every panel carries an explanation behind the "
            "small \"i\" in its top-left corner -- hover it if a number or "
            "an acronym is unfamiliar. In short: RSRP is how strong the "
            "signal is, SINR is how clean it is and is the one that "
            "predicts speed, and the number of carriers is how much spectrum "
            "is in use. Generated by "
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

    # An __inputs entry per uid still left as a placeholder, rather than the
    # whole block or none of it.
    #
    # It used to be all-or-nothing: __inputs was emitted only when all three
    # uids were at their defaults. --influxdb-short-uid arrived after the
    # other two flags, which quietly turned the two-flag invocation the README
    # documented into a broken dashboard -- the short uid stayed the literal
    # string "${DS_INFLUXDB_SHORT}", and with no __inputs entry to resolve it
    # Grafana cannot match that to a datasource and falls back to the default
    # one. The neighbour panel lands on the main bucket, where
    # quectel_neighbour is deliberately absent, and reads "No data" with
    # nothing anywhere to say why.
    #
    # The placeholder string is its own marker: a uid still equal to its
    # DEFAULT_*_INPUT was never bound and so needs an import prompt, and one
    # that was bound needs no entry. Binding some and being asked for the
    # rest is a legitimate thing to want, and either way no ${...} can now
    # reach a dashboard without an __inputs entry that resolves it.
    declared = [
        spec for uid, spec in (
            (influx_uid,
             {"name": "DS_INFLUXDB", "label": INFLUX_PLUGIN_NAME,
              "description": "InfluxDB, queried with InfluxQL",
              "type": "datasource", "pluginId": INFLUX_PLUGIN_ID,
              "pluginName": INFLUX_PLUGIN_NAME}),
            (short_uid,
             {"name": "DS_INFLUXDB_SHORT",
              "label": INFLUX_PLUGIN_NAME + " (short)",
              "description": "InfluxDB database holding the short-retention "
                             "neighbour bucket",
              "type": "datasource", "pluginId": INFLUX_PLUGIN_ID,
              "pluginName": INFLUX_PLUGIN_NAME}),
            (infinity_uid,
             {"name": "DS_INFINITY", "label": INFINITY_PLUGIN_NAME,
              "description": "Infinity, for the live status endpoint",
              "type": "datasource", "pluginId": INFINITY_PLUGIN_ID,
              "pluginName": INFINITY_PLUGIN_NAME}),
        ) if uid == "${" + spec["name"] + "}"
    ]

    if declared:
        dash["__inputs"] = declared
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
    ap.add_argument("--influxdb-short-uid", default=DEFAULT_INFLUX_SHORT_INPUT,
                    help="datasource uid for the short-retention neighbour "
                         "bucket (a second InfluxDB datasource, since an "
                         "InfluxQL one carries only a single database)")
    args = ap.parse_args()

    # Which uids need an import prompt is decided per uid inside
    # build_dashboard, from whether each is still its placeholder.
    dash = build_dashboard(args.influxdb_uid, args.infinity_uid,
                           args.url, args.influxdb_short_uid)
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
