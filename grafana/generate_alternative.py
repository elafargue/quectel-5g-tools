#!/usr/bin/env python3
"""Generate the alternative Quectel dashboard: InfluxQL history and latest poll.

`generate.py` builds the published dashboard (grafana.com id 24835) against
VictoriaMetrics/PromQL, by re-templating an upstream source dashboard. This is
a separate artifact for a different stack and is built from scratch, because
InfluxQL and PromQL share no syntax and there is no InfluxQL source to clone.

Everything comes from InfluxDB, fed by `telegraf/quectel.conf`, which polls
the router's JSON endpoint. Two databases -- neighbours live in a bucket that
expires in a day -- so two InfluxDB datasources, but one plugin.

The top row used to read the router directly through the Infinity plugin.
Every value it showed is also in InfluxDB, and reading it there costs the
router's AT bus nothing, needs no plugin and no route from Grafana to the
router, and cannot turn the endpoint's "cannot read modem" into "no service"
-- which the Infinity path did, because Infinity answers a missing JSON path
with a query error and the stat falls back to its no-value text. What the
row gives up is freshness: it is the newest poll, up to a poll interval old,
not a live read. Aiming an antenna is 5g-monitor's job, not a dashboard's.

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
import re
import sys
from pathlib import Path

INFLUX_PLUGIN_ID = "influxdb"
INFLUX_PLUGIN_NAME = "InfluxDB"

DEFAULT_INFLUX_INPUT = "${DS_INFLUXDB}"
DEFAULT_INFLUX_SHORT_INPUT = "${DS_INFLUXDB_SHORT}"

# How far back the "Now" row looks for the newest poll.
#
# It has to cover Telegraf's poll interval plus its flush_interval: a poll
# becomes visible up to flush_interval after it is taken, so at the moment
# the dashboard asks, the newest one can be interval + flush old -- 60s + 10s
# with telegraf/quectel.conf and Telegraf's default flush. Any shorter and the
# row blinks empty between polls.
#
# Every second longer is time a carrier that has just been dropped stays on
# screen. InfluxQL cannot select "only the newest poll" -- it cannot treat
# time as a value to compare against -- so the window is the only lever.
# Measured on the dev stack with a 90s window over 10s polls: 11 carrier rows
# where 4 were current, 33 neighbours where about 7 were. --recent-window
# sets it; docker/up.sh passes 25s for the stack's 10s polls.
DEFAULT_RECENT_WINDOW = "75s"

# Telegraf's poll interval, used as every history panel's minimum interval.
#
# Below one poll per bucket, fill(null) would put a null between every pair
# of polls and the history would flicker. Above it, a bucket without a point
# genuinely means no sample -- which is what fill(null) must be free to say.
# fill(none) cannot say it at all: Grafana joins every row onto one time
# axis, a row with no point at some time gets an *undefined* there, and the
# state timeline carries a row's last state across undefined until that row
# has another point. A carrier that left at 13:15 was drawn in use until the
# end of the range -- on the boat's 24h view, every carrier of the day at
# once. A line graph does the same -- points separated only by missing
# timestamps are joined, so an NR leg detached for four hours was drawn as a
# straight line across them -- and fill(previous) carried the last serving
# site across any stretch without data. Polls land in the first 20s of each
# minute on the boat, so buckets aligned on the minute hold exactly one each.
# docker/up.sh passes 10s.
DEFAULT_POLL_INTERVAL = "60s"
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

# Connection mode is a category, not a measurement, so it gets value mappings
# rather than thresholds: a colour per technology, ordered worst to best so
# the timeline reads at a glance without consulting a legend.
TECH_COLOURS = [
    ("WCDMA", "3G", "red"),
    ("LTE", "4G LTE", "yellow"),
    ("NSA", "5G NSA", "green"),
    ("SA", "5G SA", "blue"),
]
TECH_MAPPING = {"type": "value", "options": {
    v: {"text": t, "color": c, "index": i}
    for i, (v, t, c) in enumerate(TECH_COLOURS)}}


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
# shows them, so the explanation of RSRP cannot drift between the Now stat and
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

def recent_doc(window: str) -> str:
    """The note every "Now" panel carries about how current it is."""
    return (
        f"The newest poll in the last {window}, from InfluxDB. The router is "
        "polled on an interval -- every 60 seconds as deployed -- so this can "
        "be up to about that old: the latest reading, not a live one.")


def seen_doc(window: str, what: str) -> str:
    """Why the tables carry an age column, and how to read it."""
    return (
        f"**Seen** is how old each row is. The table is every {what} heard in "
        f"the last {window}, each at its own latest reading, so a row "
        "noticeably older than the rest is one that has just gone and will "
        "age out shortly.")

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


def steps(pairs) -> dict:
    return {
        "mode": "absolute",
        "steps": [
            {"color": c, "value": None if i == 0 else v}
            for i, (c, v) in enumerate(pairs)
        ],
    }


def iql(uid: str, ref: str, query: str, alias: str | None = None,
        fmt: str = "time_series") -> dict:
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
        # "table" for the two Now tables: tags come back as columns, one row
        # per series, which is what LIMIT 1 per series wants.
        "resultFormat": fmt,
    }
    if alias:
        t["alias"] = alias
    return t


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
         string_value=False, description="", mappings=None) -> dict:
    """string_value picks up a text field rather than a number.

    reduceOptions.fields defaults to "", which Grafana reads as *numeric
    fields only*. A frame carrying one string column then has nothing to
    reduce and the panel shows its noValue text -- so an operator name
    renders as "no service" while the query behind it is returning "KT"
    perfectly well.

    Every field except Time, though, not every field. An InfluxDB
    time-series frame carries its timestamp as a field too, and "/.*/" put
    it on the panel as a second value -- "2026-09-10 17:06:30" above "KT".
    The Infinity frames this replaced had no time column, so the regression
    arrived with the switch and only showed once rendered.
    """
    field: dict = {
        "custom": {},
        "mappings": mappings or [],
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
                              "fields": "/^(?!Time$).*$/" if string_value
                                        else "",
                              "values": False},
            "orientation": "auto",
            "textMode": text_mode,
            "colorMode": "value" if (thresholds or mappings) else "none",
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
               fill=10, min_=None, max_=None, description="",
               stack=False) -> dict:
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
            "stacking": {"group": "A",
                         "mode": "normal" if stack else "none"},
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
                   description="", mappings=None) -> dict:
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
                "mappings": mappings or [],
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

def build_panels(iu: str, su: str, window: str,
                 poll: str = DEFAULT_POLL_INTERVAL) -> list:
    """iu = InfluxDB uid, su = uid of the datasource holding the
    short-retention neighbour bucket, window = how far back the Now row looks
    for the newest poll (DEFAULT_RECENT_WINDOW explains the choice)."""
    panels: list = []
    recent = recent_doc(window)
    since = f"time > now() - {window}"

    # -- Now ----------------------------------------------------------------
    # The newest poll, from InfluxDB. A fixed window rather than $timeFilter,
    # and the difference matters: last() over the dashboard's range would keep
    # showing the final reading hours after polling stopped, as if it were
    # current. Bounded to about one poll, the row goes blank instead -- a
    # reading nobody took is not shown as one.
    panels.append(row("Now", 0))

    # operator_short rather than the long name: it is a field, so last()
    # returns the single newest value without a GROUP BY. The long name is a
    # tag, and grouping by it would show two operators side by side for the
    # length of the window after every roam.
    panels.append(stat(
        "Network", gp(0, 1, 4, 4),
        [iql(iu, "A", 'SELECT last("operator_short") FROM "quectel_operator" '
                      f'WHERE {since}')],
        influx(iu), no_value="no reading", string_value=True,
        description=(
            "The mobile network the modem is registered with. " + recent +
            "\n\n*no reading* means no operator arrived in that time: the "
            "modem is not registered -- searching, out of coverage, no usable "
            "SIM -- or the router could not be read at all. *RRC* beside it "
            "tells those apart: it still has a state when the modem is merely "
            "unregistered, and goes blank too only when nothing could be "
            "read.")))

    # technology, not mode. mode is absent on an LTE-only attach, and last()
    # over the window would then keep showing the NSA from before the NR leg
    # dropped until it aged out. technology is present whenever there is a
    # cell, so the change shows on the very next poll.
    panels.append(stat(
        "Mode", gp(4, 1, 3, 4),
        [iql(iu, "A", 'SELECT last("technology") FROM "quectel_serving" '
                      f'WHERE {since}')],
        influx(iu), no_value="no reading", string_value=True,
        mappings=[TECH_MAPPING],
        description=(
            "Which radio technology the modem is attached to: **3G**, **4G "
            "LTE**, **5G NSA** (5G riding on an LTE anchor, the usual case) "
            "or **5G SA** (standalone 5G). *Connection mode* further down is "
            "the same value over time.\n\n" + recent + "\n\n"
            "*no reading* means no poll arrived, or the modem had no cell at "
            "all. Being on LTE alone is not blank here -- it reads 4G LTE.")))

    panels.append(stat(
        "RRC", gp(7, 1, 3, 4),
        [iql(iu, "A", 'SELECT last("state") FROM "quectel_serving" '
                      f'WHERE {since}')],
        influx(iu), no_value="no reading", string_value=True,
        description=(
            "The radio connection state the modem reports: `CONNECT` while a "
            "link is actively carrying data, `NOCONN` when it is camped on a "
            "cell with nothing to send, `SEARCH` while looking for one, "
            "`LIMSRV` for limited service (emergency calls only).\n\n"
            "`NOCONN` is **not** a fault. An idle link sits there most of the "
            "time; it says nothing about signal quality.\n\n" + recent +
            " *no reading* means no poll arrived: the router or the modem "
            "could not be read.")))

    # The two numbers you actually steer by. Thresholds are the ones
    # 5g-monitor colours its output with, so a green here is a green there.
    panels.append(stat(
        "NR RSRP", gp(10, 1, 4, 4),
        [iql(iu, "A", f'SELECT last("rsrp") FROM "quectel_nr5g" WHERE {since}')],
        influx(iu), unit="dBm", thresholds=RSRP_STEPS, no_value="no NR",
        description=(
            RSRP_DOC + "\n\nThis is the 5G carrier. Reads *no NR* when no 5G "
            "reading arrived in the window: the 5G leg is detached, or the "
            "router could not be read -- *Mode* tells you which.\n\n" + recent
            + " After the 5G leg drops, its last value stays here until it "
            "ages out of that window.")))

    panels.append(stat(
        "NR SINR", gp(14, 1, 4, 4),
        [iql(iu, "A", f'SELECT last("sinr") FROM "quectel_nr5g" WHERE {since}')],
        influx(iu), unit="dB", thresholds=SINR_STEPS, no_value="no NR",
        description=(
            SINR_DOC + "\n\nThis is the 5G carrier, and it is the value to "
            "aim a directional antenna by -- but aim by `5g-monitor`, which "
            "reads the modem directly and beeps it, not by this: " + recent)))

    panels.append(stat(
        "LTE RSRP", gp(18, 1, 3, 4),
        [iql(iu, "A", f'SELECT last("rsrp") FROM "quectel_lte" WHERE {since}')],
        influx(iu), unit="dBm", thresholds=RSRP_STEPS, no_value="no LTE",
        description=(
            RSRP_DOC + "\n\nThis is the 4G carrier. In NSA mode it is also "
            "the anchor the 5G leg is bolted to, so it is worth watching even "
            "when 5G is doing the work: lose the anchor and the 5G goes with "
            "it. Reads *no LTE* on a 3G attach, where there is no LTE cell, "
            "or when nothing could be read -- *Mode* tells you which.\n\n"
            + recent)))

    panels.append(stat(
        "LTE SINR", gp(21, 1, 3, 4),
        [iql(iu, "A", f'SELECT last("sinr") FROM "quectel_lte" WHERE {since}')],
        influx(iu), unit="dB", thresholds=SINR_STEPS, no_value="no LTE",
        description=SINR_DOC + "\n\nThis is the 4G carrier.\n\n" + recent))

    # The aggregated carriers, what 5g-info prints as its CA table.
    #
    # One row per carrier, each at its own latest point: GROUP BY every tag
    # that identifies a carrier, then LIMIT 1 per series. arfcn is in the
    # group because two secondaries can share role, rat and band, and before
    # arfcn was a tag the second one's points overwrote the first's. It is
    # then excluded from display -- MHz already tells the two apart.
    panels.append(table(
        "Connected carriers", gp(0, 5, 12, 9),
        [iql(iu, "A",
             'SELECT "pci", "frequency_mhz", "bandwidth_mhz", "rsrp", "sinr" '
             'FROM "quectel_carrier_pcc", "quectel_carrier_scc" '
             f'WHERE {since} GROUP BY "role", "rat", "band", "arfcn" '
             'ORDER BY time DESC LIMIT 1', fmt="table")],
        influx(iu),
        transformations=[
            {"id": "sortBy", "options": {
                "fields": {}, "sort": [{"field": "role"}]}},
            _organize([("role", "Role"), ("rat", "RAT"), ("band", "Band"),
                       ("pci", "PCI"), ("frequency_mhz", "MHz"),
                       ("bandwidth_mhz", "BW"), ("rsrp", "RSRP"),
                       ("sinr", "SINR"), ("Time", "Seen")],
                      exclude=("arfcn",)),
        ],
        overrides=[
            _width("Role", 50), _width("RAT", 45), _width("Band", 50),
            _width("PCI", 50), _width("MHz", 70, decimals=1),
            _width("BW", 45),
            _colour_override("RSRP", RSRP_STEPS, "dBm", 70),
            _colour_override("SINR", SINR_STEPS, "dB", 60),
            _seen_override(),
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
            "are much faster but shorter-ranged. Two rows can share a band.\n"
            "- **PCI** -- identifies which cell of that band, out of 504 "
            "possible codes. A change of PCI is a change of cell.\n"
            "- **MHz / BW** -- centre frequency and channel width. Wider is "
            "faster.\n\n" + seen_doc(window, "carrier") + "\n\n" + recent)))

    # The neighbour list, from the short-retention bucket -- which, for the
    # first time, is read row by row rather than only counted.
    panels.append(table(
        "Neighbour cells", gp(12, 5, 12, 9),
        [iql(su, "A",
             'SELECT "rsrp", "rsrq" FROM "quectel_neighbour" '
             f'WHERE {since} GROUP BY "rat", "scope", "arfcn", "pci" '
             'ORDER BY time DESC LIMIT 1', fmt="table")],
        influx(su),
        transformations=[
            {"id": "sortBy", "options": {
                "fields": {}, "sort": [{"field": "rsrp", "desc": True}]}},
            _organize([("rat", "RAT"), ("scope", "Scope"), ("arfcn", "ARFCN"),
                       ("pci", "PCI"), ("rsrp", "RSRP"), ("rsrq", "RSRQ"),
                       ("Time", "Seen")]),
        ],
        overrides=[
            _width("RAT", 50), _width("Scope", 60), _width("ARFCN", 70),
            _width("PCI", 50),
            _colour_override("RSRP", RSRP_STEPS, "dBm", 75),
            _colour_override("RSRQ", RSRQ_STEPS, "dB", 70),
            _seen_override(),
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
            + seen_doc(window, "neighbour") + " Neighbours turn over fast on "
            "a moving vessel, so expect more of these than in the carriers "
            "table.\n\n" + recent + " Kept for 24 hours in a short-retention "
            "bucket of its own; *Neighbours reported* below is that history as "
            "a count. 3G neighbours are not listed: the modem reports them in "
            "two layouts that cannot be told apart, so only their channel is "
            "recorded.")))

    # -- Signal history -----------------------------------------------------
    panels.append(row("Signal history", 14))

    panels.append(timeseries(
        "RSRP", gp(0, 15, 12, 8),
        [iql(iu, "A",
             'SELECT mean("rsrp") FROM "quectel_nr5g" WHERE $timeFilter '
             'GROUP BY time($__interval), "band" fill(null)', "NR $tag_band"),
         iql(iu, "B",
             'SELECT mean("rsrp") FROM "quectel_lte" WHERE $timeFilter '
             'GROUP BY time($__interval), "band" fill(null)', "LTE B$tag_band")],
        influx(iu), unit="dBm", thresholds=RSRP_STEPS,
        description=(
            RSRP_DOC + "\n\nOne line per band, so a handover onto different "
            "spectrum shows as one line stopping and another starting rather "
            "than as a jump in a single line. " + GAPS_DOC)))

    panels.append(timeseries(
        "SINR", gp(12, 15, 12, 8),
        [iql(iu, "A",
             'SELECT mean("sinr") FROM "quectel_nr5g" WHERE $timeFilter '
             'GROUP BY time($__interval), "band" fill(null)', "NR $tag_band"),
         iql(iu, "B",
             'SELECT mean("sinr") FROM "quectel_lte" WHERE $timeFilter '
             'GROUP BY time($__interval), "band" fill(null)', "LTE B$tag_band")],
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
             'GROUP BY time($__interval) fill(null)', "NR"),
         iql(iu, "B",
             'SELECT mean("rsrq") FROM "quectel_lte" WHERE $timeFilter '
             'GROUP BY time($__interval) fill(null)', "LTE")],
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
    # And arfcn, once it became a tag: two *secondaries* on one band share
    # role as well, so without it their points land in one series and last()
    # keeps one of them -- one row where the carriers table above shows two.
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
             'GROUP BY time($__interval), "role", "rat", "band", "arfcn" '
             'fill(null)',
             "$tag_role $tag_rat $tag_band $tag_arfcn")],
        influx(iu), thresholds=RSRP_STEPS,
        description=(
            "Which carriers were actually in use over time, as blocks rather "
            "than lines. Each row is one carrier and its colour is that "
            "carrier's RSRP on the usual scale, so a row that turns red was "
            "still in use but weak.\n\n"
            "**Row labels read \"role technology band channel\".** "
            "`pcc lte 3 1550` is the primary carrier on LTE band 3, channel "
            "(EARFCN) 1550; `scc 5g 78 636672` a secondary on 5G n78. Role and "
            "channel are both there because intra-band aggregation is "
            "ordinary -- two or three carriers can sit on one band at once, "
            "and the channel is what tells two secondaries apart. Rows from "
            "before an install began recording the channel show it blank."
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
             'GROUP BY time($__interval) fill(null)', "eNodeB")],
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
    # Neighbours per poll, not points per bucket -- the fix Aggregated
    # carriers below has, for the same reason. count() over a display bucket
    # counted neighbours times polls: 0 to 64 on the dev stack's 5-minute
    # buckets where the truth was 1 to 6 per poll, and on the boat's 24h view,
    # whose half-width panel gets 2-minute buckets, about double. One poll's
    # neighbours share one timestamp -- they come from one json_v2 parse -- so
    # the inner time(1s) grouping counts per poll, and the outer query
    # averages those per bucket. The inner query keeps fill(none): its empty
    # buckets are the gaps between polls, not missing readings.
    #
    # Stacked. Per-poll counting cannot produce a zero: a poll that heard no
    # inter-frequency neighbours has no inter points to count, so inter shows
    # a gap -- 295 of 361 minutes on the boat, nearly all of them polls that
    # did happen, while intra carried on. Drawn side by side that reads as
    # missing data. Stacked, the top of the stack is the total heard: an
    # inter gap leaves intra as the top, which is the right total, and a poll
    # that returned nothing still blanks both.
    #
    # fill(null), not fill(0) and not fill(none). With the minimum interval at
    # one poll, a bucket without a sample really had none: fill(null) draws it
    # as a break. fill(0) would draw it as "no neighbours" -- a measurement
    # nobody took, rendered as a reading of zero, which is the same lie
    # get_status() and 5g-collectd go to some trouble to avoid -- and
    # fill(none) drew nothing at all, because Grafana carried the line across
    # the gap. DEFAULT_POLL_INTERVAL has the detail.
    panels.append(timeseries(
        "Neighbours reported", gp(12, 30, 12, 6),
        [iql(su, "A",
             'SELECT mean("n") FROM ('
             'SELECT count("rsrp") AS "n" FROM "quectel_neighbour" '
             'WHERE $timeFilter GROUP BY time(1s), "scope" fill(none)'
             ') WHERE $timeFilter '
             'GROUP BY time($__interval), "scope" fill(null)',
             "$tag_scope")],
        influx(su), fill=30, stack=True,
        description=(
            "How many neighbouring cells the modem could hear at each poll, "
            "split into `intra` (same frequency as the serving cell) and "
            "`inter` (a different one), **stacked, so the top of the stack is "
            "the total heard**. The cells themselves are listed in *Neighbour "
            "cells* at the top of the dashboard.\n\n"
            "**A thinning count is an early warning of running out of "
            "coverage, and it usually moves before RSRP does** -- you lose "
            "the alternatives before you lose the cell you are on.\n\n"
            "**A gap in `inter` while `intra` carries on means no neighbours "
            "were heard on other frequencies** -- there are none to count, so "
            "there is no line, and the top of the stack is `intra` alone, "
            "which is the right total. A gap in both is a poll that returned "
            "nothing: the router or the modem could not be read, which is "
            "not the same as hearing no one.\n\n"
            "At wider zoom each point averages the polls inside it, so 3.5 "
            "means it heard 3 and 4 by turns.\n\n"
            "Kept for 24 hours only -- neighbour lists churn constantly on a "
            "moving vessel, so they live in a short-retention bucket of their "
            "own. Ranges longer than a day will look empty here.")))

    # Carriers per poll, not points per bucket.
    #
    # count() over a display bucket counts every point in it, and each carrier
    # writes one point per poll -- so this panel used to read carriers times
    # polls, growing with the zoom: 5 to 54 on the dev stack's 5-minute
    # buckets where the truth was 1 or 2. The inner query counts per poll
    # instead. A poll's carriers share one timestamp exactly -- they come from
    # one json_v2 parse -- so time(1s) buckets hold one poll each. The outer
    # query averages those per display bucket: exact while a bucket holds one
    # poll, and honest when it holds many, where 2.5 means time spent at both
    # 2 and 3. Checked over 365 days on the stack: the 1s inner grouping took
    # 0.06s and hit no bucket limit.
    #
    # One target per measurement, stacked. Primary and secondary carriers are
    # separate measurements, and one query over both came back as two series
    # both labelled "lte". Stacked, the top of the stack is the total.
    #
    # count() over a field, never distinct() over a tag: InfluxQL's distinct()
    # does not operate on tags and answers with no series at all.
    def per_poll(measurement: str, ref: str, role: str) -> dict:
        return iql(iu, ref,
                   'SELECT mean("n") FROM ('
                   f'SELECT count("rsrp") AS "n" FROM "{measurement}" '
                   'WHERE $timeFilter GROUP BY time(1s), "rat" fill(none)'
                   ') WHERE $timeFilter '
                   'GROUP BY time($__interval), "rat" fill(null)',
                   "$tag_rat " + role)

    panels.append(timeseries(
        "Aggregated carriers", gp(0, 36, 12, 6),
        [per_poll("quectel_carrier_pcc", "A", "primary"),
         per_poll("quectel_carrier_scc", "B", "secondary")],
        influx(iu), fill=30, min_=0, stack=True,
        description=(
            "How many carriers were bonded together, split by technology and "
            "by role -- primary or secondary -- and stacked, so the top of the "
            "stack is the total. This is the count behind the *Connected "
            "carriers* table at the top.\n\n"
            "More carriers means more spectrum in use and, broadly, more "
            "speed, so **a drop here often explains a slowdown that the "
            "signal panels do not** -- the network can withdraw a carrier "
            "under load while every dB stays exactly where it was.\n\n"
            "At wider zoom each point averages the polls inside it, so 2.5 "
            "means it spent time at both 2 and 3 carriers. A gap is a poll "
            "with no carriers reported: the modem was on 3G, where there is "
            "no aggregation, or could not be read -- *Connection mode* tells "
            "which.")))

    # Frequency rather than band number: on a boat the interesting question is
    # usually whether it fell back to low band, and megahertz answers that
    # without having to remember which band is which.
    #
    # Grouped by arfcn, and it has to be. Grouped by fewer tags than identify
    # a carrier, mean() blends two secondaries on one band: 1820 and 1870 MHz
    # plotted as 1850 and 1851.7 on the dev stack -- frequencies nothing
    # transmits on. Before arfcn was a tag one of the pair was simply lost;
    # after, a coarser grouping averages them. One channel per series, the
    # value is constant and mean() is exact. docker/verify.sh holds this.
    panels.append(timeseries(
        "Carrier frequency", gp(12, 36, 12, 6),
        [iql(iu, "A",
             'SELECT mean("frequency_mhz") FROM "quectel_carrier_pcc", '
             '"quectel_carrier_scc" WHERE $timeFilter '
             'GROUP BY time($__interval), "rat", "band", "arfcn" '
             'fill(null)',
             "$tag_rat $tag_band $tag_arfcn")],
        # Grafana has no megahertz unit and "hertz" would label 1840 MHz as
        # 1840 Hz. A custom suffix is the honest option.
        influx(iu), unit="suffix:MHz",
        description=(
            "The centre frequency of each carrier in use, one line per "
            "channel, labelled technology, band and channel number. Shown as megahertz "
            "rather than as a band number because the useful question at sea "
            "is usually *did it fall back to low band*, and that is easier to "
            "see on an axis than to remember band by band.\n\n"
            "Roughly: **below 1000 MHz** travels a long way and passes "
            "through obstacles but is slow; **1800-2600 MHz** is the middle "
            "ground; **3400-3800 MHz** (n78) is the fastest and the shortest-"
            "ranged. A line dropping to the bottom of the chart is the modem "
            "trading speed for reach, which is usually the right call and "
            "always worth knowing about.")))

    # -- Connection mode ----------------------------------------------------
    #
    # What the modem was attached to over time, as blocks. Reads
    # quectel_serving, which exists precisely so this question does not have
    # to be answered by inference: a 3G attach reports neither an LTE nor an
    # NR serving cell, so deriving the technology from which of those
    # measurements turned up files a working 3G link as "no reading" --
    # indistinguishable from an unreachable router, which is the one thing
    # this toolkit refuses to confuse.
    #
    # One row, not one per technology: the technology is the plotted *value*,
    # and the value mappings below turn each string into a label and a colour.
    # That needs technology to be a field rather than a tag -- a tag can only
    # be a series name, which would have made this a row per technology with
    # some other field plotted inside it. quectel_serving carries no tags for
    # exactly this reason.
    #
    # last(), not mean(): it is a string.
    #
    # fill(none) for the usual reason -- a bucket with no poll in it is a gap,
    # not a technology.
    panels.append(state_timeline(
        "Connection mode", gp(0, 42, 24, 6),
        [iql(iu, "A",
             'SELECT last("technology") FROM "quectel_serving" '
             'WHERE $timeFilter GROUP BY time($__interval) fill(null)',
             "Mode")],
        influx(iu),
        description=(
            "Which radio technology the modem was actually attached to, as "
            "blocks over time. One row per technology; a row that stops is a "
            "technology it left.\n\n"
            "- **3G** (`WCDMA`) -- the fallback. Slow, and it has **no SINR "
            "at all**, so the SINR panels and `5g-monitor`'s beeps go quiet "
            "rather than wrong while it lasts. Signal is reported as RSCP and "
            "Ec/Io instead, which are related to RSRP and RSRQ but are not "
            "the same quantities.\n"
            "- **4G LTE** -- LTE with no 5G leg attached.\n"
            "- **5G NSA** -- 5G riding on an LTE anchor, the usual case.\n"
            "- **5G SA** -- standalone 5G, no LTE underneath.\n\n"
            "**Gaps are not a technology.** A break means no sample arrived "
            "for that interval -- the router was unreachable or the modem "
            "could not be read -- which is a different thing from being "
            "attached to nothing, and neither is drawn as a mode.\n\n"
            "Recorded by the router rather than inferred here: on a 3G attach "
            "the modem reports no LTE and no NR serving cell, so guessing the "
            "technology from which measurements exist would call a working "
            "link no reading at all."),
        mappings=[TECH_MAPPING]))

    # Every history panel gets a minimum interval of one poll. It is set here
    # rather than per panel so a new history panel cannot forget it: without
    # it, fill(null) would break the line between every pair of polls.
    for p in panels:
        if any("$__interval" in t.get("query", "") for t in p.get("targets", [])):
            p["interval"] = poll

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


def _organize(columns: list, exclude: tuple = ()) -> dict:
    """Rename table columns and lay them out in the given order.

    `columns` is (source name, shown name) pairs. InfluxQL's table format
    names columns after tag keys and field keys -- role, frequency_mhz --
    and Grafana orders them however the frame arrives, which puts identity
    columns among the measurements.
    """
    return {"id": "organize",
            "options": {
                "indexByName": {src: i for i, (src, _) in enumerate(columns)},
                "renameByName": {src: shown for src, shown in columns},
                "excludeByName": {name: True for name in exclude}}}


def _seen_override() -> dict:
    """The row's timestamp, shown as its age: "a few seconds ago"."""
    return {"matcher": {"id": "byName", "options": "Seen"},
            "properties": [{"id": "unit", "value": "dateTimeFromNow"},
                           {"id": "custom.width", "value": 115}]}


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


def build_dashboard(influx_uid: str, short_uid: str,
                    window: str = DEFAULT_RECENT_WINDOW,
                    poll: str = DEFAULT_POLL_INTERVAL) -> dict:
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

    panels = build_panels(influx_uid, short_uid, window, poll)
    _assign_ids(panels)
    _validate(panels)

    dash: dict = {
        "title": "Quectel 5G — Alternative (InfluxQL)",
        "description": (
            "The newest poll and the signal history, all from InfluxDB. "
            "Every panel carries an explanation behind the "
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
        # Refreshing queries InfluxDB only, never the router, so it costs the
        # AT bus nothing; the Now row changes once per poll however often it
        # runs.
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
    # The description doubles as the picker's *placeholder* on the import
    # page -- Grafana's processInputs assigns it to inputModel.info, which
    # DataSourcePicker renders as placeholder text. A noun phrase there
    # ("InfluxDB, queried with InfluxQL") sits in the box looking exactly like
    # a value that has already been chosen, on a form whose pickers all start
    # empty. Phrased as an instruction it cannot be mistaken for one.
    #
    # Grafana 11.6 refuses to submit with any picker unselected ("A data
    # source is required" under all three, verified), so a misread costs only
    # a moment there. It is not known that every version enforces that, and
    # an unresolved ${...} falls back to the *default* datasource silently --
    # which for the main input is usually right by accident and for the
    # short-retention one is exactly the mis-wiring this dashboard cannot
    # afford.
    declared = [
        spec for uid, spec in (
            (influx_uid,
             {"name": "DS_INFLUXDB", "label": INFLUX_PLUGIN_NAME,
              "description": "Select the InfluxDB datasource on database "
                             "systemhealth",
              "type": "datasource", "pluginId": INFLUX_PLUGIN_ID,
              "pluginName": INFLUX_PLUGIN_NAME}),
            (short_uid,
             {"name": "DS_INFLUXDB_SHORT",
              "label": INFLUX_PLUGIN_NAME + " (short)",
              "description": "Select the OTHER InfluxDB datasource, the one "
                             "on database systemhealth_short -- not the main "
                             "one",
              "type": "datasource", "pluginId": INFLUX_PLUGIN_ID,
              "pluginName": INFLUX_PLUGIN_NAME}),
        ) if uid == "${" + spec["name"] + "}"
    ]

    if declared:
        dash["__inputs"] = declared
        dash["__requires"] = [
            {"type": "grafana", "id": "grafana", "name": "Grafana",
             "version": "11.0.0"},
            {"type": "datasource", "id": INFLUX_PLUGIN_ID,
             "name": INFLUX_PLUGIN_NAME, "version": "1.0.0"},
        ]
    return dash


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--stdout", action="store_true",
                    help="write to stdout instead of the JSON file")
    ap.add_argument("--influxdb-uid", default=DEFAULT_INFLUX_INPUT,
                    help="bind to an explicit InfluxDB datasource uid")
    ap.add_argument("--poll-interval", default=DEFAULT_POLL_INTERVAL,
                    help="Telegraf's poll interval, the history panels' "
                         "minimum interval: 60s as deployed (the default), "
                         "10s for the dev stack")
    ap.add_argument("--recent-window", default=DEFAULT_RECENT_WINDOW,
                    help="how far back the Now row looks for the newest poll: "
                         "Telegraf's poll interval plus its flush_interval, "
                         "e.g. 75s for 60s polls (the default), 25s for the "
                         "dev stack's 10s")
    ap.add_argument("--influxdb-short-uid", default=DEFAULT_INFLUX_SHORT_INPUT,
                    help="datasource uid for the short-retention neighbour "
                         "bucket (a second InfluxDB datasource, since an "
                         "InfluxQL one carries only a single database)")
    args = ap.parse_args()

    # Which uids need an import prompt is decided per uid inside
    # build_dashboard, from whether each is still its placeholder.
    # It goes into every Now query verbatim, so it has to be an InfluxQL
    # duration and nothing else.
    if not re.fullmatch(r"[1-9][0-9]*[smh]", args.recent_window):
        raise SystemExit(f"--recent-window must be an InfluxQL duration such "
                         f"as 75s or 2m, not {args.recent_window!r}")
    if not re.fullmatch(r"[1-9][0-9]*[smh]", args.poll_interval):
        raise SystemExit(f"--poll-interval must be an InfluxQL duration such "
                         f"as 60s, not {args.poll_interval!r}")
    dash = build_dashboard(args.influxdb_uid, args.influxdb_short_uid,
                           args.recent_window, args.poll_interval)
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
