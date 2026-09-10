#!/usr/bin/env python3
"""Stand in for a GL-X3000 serving /cgi-bin/quectel-status.

The point of the stack is to exercise the pipeline, and a pipeline fed a
constant produces flat lines that prove nothing. This walks the signal,
hands over between cells, drops the NR leg from time to time and
occasionally refuses to answer at all -- so every panel has something to
draw and every failure path can be seen rather than reasoned about.

Shape and values come from real captures in tests/test-parser: KT Korea
(450/08, LTE B3 anchor with a B8 secondary and an n78 carrier) and Free
Mobile France (208/15, B3 with n28). Nothing here is invented but the noise.
"""
import json
import math
import os
import random
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PORT = int(os.environ.get("PORT", "8080"))
# Fraction of requests answered the way an unreachable modem is: with an
# error rather than with a body that would parse as a cell with no signal.
FAIL_RATE = float(os.environ.get("FAIL_RATE", "0.02"))
# Fraction of the time the NR leg is detached, which is the failure that
# looks healthy on every LTE metric.
NR_DROP_RATE = float(os.environ.get("NR_DROP_RATE", "0.12"))

# Fraction of requests answered as a WCDMA (3G) attach. Real, and the state
# the toolkit handled worst: a 3G serving cell parsed to nothing at all, so a
# working modem read as one attached to nothing. Nothing in the stack
# exercised it because nothing generated it.
WCDMA_RATE = float(os.environ.get("WCDMA_RATE", "0.08"))
SEED = int(os.environ.get("SEED", "20815"))

random.seed(SEED)

NETWORKS = [
    {   # KT Korea, the four-carrier capture from tests/test-parser Test 21.
        # Two secondaries, and the first is intra-band non-contiguous: B3
        # again, on a different EARFCN. Real aggregation does this, and a
        # dashboard that groups carriers by band alone cannot tell the two
        # apart -- which is exactly why the sim needs to reproduce it.
        "operator": "KT", "mcc": 450, "mnc": 8, "mcc_mnc": "45008",
        "lte": {"band": 3, "arfcn": 1550, "freq": 1840.0, "bw": 20},
        "scc": [{"band": 3, "arfcn": 1694, "freq": 1854.4, "bw": 10},
                {"band": 8, "arfcn": 3743, "freq": 954.3, "bw": 10}],
        "nr": {"band": 78, "arfcn": 636672, "freq": 3550.08, "bw": 100,
               "scs": 1},
    },
    {   # Free Mobile France, from Test 20 -- n28, FDD, 15 kHz SCS
        "operator": "Free", "mcc": 208, "mnc": 15, "mcc_mnc": "20815",
        "lte": {"band": 3, "arfcn": 1675, "freq": 1852.5, "bw": 15},
        "scc": [],
        "nr": {"band": 28, "arfcn": 156510, "freq": 782.55, "bw": 10,
               "scs": 0},
    },
]


class Radio:
    """A slow random walk, so graphs bend rather than jump."""

    def __init__(self):
        self.net = 0
        self.lte_rsrp = -85.0
        self.nr_rsrp = -90.0
        self.enodeb = 5727
        self.pci = 264
        self.nr_pci = 740
        self.nr_up = True
        self.on_wcdma = False
        self.psc = 120
        self.t0 = time.time()

    def _walk(self, value, lo, hi, step=1.2):
        value += random.gauss(0, step)
        return max(lo, min(hi, value))

    def step(self):
        elapsed = time.time() - self.t0
        # A long swell under the signal, so a dashboard opened at any moment
        # shows a trend rather than pure noise.
        swell = 6.0 * math.sin(elapsed / 420.0)

        self.lte_rsrp = self._walk(self.lte_rsrp, -115, -65)
        self.nr_rsrp = self._walk(self.nr_rsrp, -120, -70)

        if random.random() < 0.01:          # handover to another site
            self.enodeb = random.randint(4000, 60000)
            self.pci = random.randint(0, 503)
        if random.random() < 0.02:          # NR cell changes
            self.nr_pci = random.randint(0, 1007)
        if random.random() < 0.004:         # roam to the other network
            self.net = 1 - self.net

        self.nr_up = random.random() > NR_DROP_RATE
        # Dropping to 3G takes the NR leg with it, the way it does in life.
        self.on_wcdma = random.random() < WCDMA_RATE
        if self.on_wcdma:
            self.nr_up = False
        return swell

    def status(self):
        swell = self.step()
        n = NETWORKS[self.net]
        lte_rsrp = round(self.lte_rsrp + swell)
        nr_rsrp = round(self.nr_rsrp + swell)

        cell_id = "%X" % ((self.enodeb << 8) | random.randint(1, 20))

        lte = {
            "arfcn": n["lte"]["arfcn"], "band": n["lte"]["band"],
            "bandwidth_dl": 5, "bandwidth_dl_mhz": n["lte"]["bw"],
            "bandwidth_ul": 5, "bandwidth_ul_mhz": n["lte"]["bw"],
            "cell_id": cell_id, "duplex": "FDD",
            "enodeb": self.enodeb,
            "frequency_mhz": n["lte"]["freq"],
            "mcc": n["mcc"], "mnc": n["mnc"], "pci": self.pci,
            "rsrp": lte_rsrp, "rsrq": round(self._walk(-11, -20, -6, 0.6)),
            "rssi": lte_rsrp + random.randint(25, 35),
            "sinr": round(self._walk(12, -5, 30, 1.5)),
            "tac": "%X" % random.randint(1, 4096),
        }
        lte["tac_decimal"] = int(lte["tac"], 16)

        carriers = [dict(lte, role="pcc", rat="lte",
                         bandwidth_mhz=n["lte"]["bw"], state=5)]
        for scc in n["scc"]:
            carriers.append({
                "arfcn": scc["arfcn"], "band": scc["band"],
                "bandwidth_mhz": scc["bw"],
                "frequency_mhz": scc["freq"], "pci": self.pci,
                "rat": "lte", "role": "scc",
                "rsrp": lte_rsrp + random.randint(-8, 8),
                "rsrq": -16, "rssi": -50, "rssnr": 0, "state": 1,
                "ul_configured": 0,
            })

        status = {
            "device": {"manufacturer": "Quectel", "model": "RM520N-GL",
                       "revision": "RM520NGLAAR03A03M4G"},
            "operator": {"mcc_mnc": n["mcc_mnc"], "operator": n["operator"],
                         "operator_short": n["operator"]},
            "serving": {"lte": lte, "mode": "NSA" if self.nr_up else "LTE",
                        "state": "NOCONN"},
            "neighbours": self._neighbours(n, lte_rsrp),
        }

        if self.on_wcdma:
            # A 3G attach has no LTE and no NR serving cell -- that is the
            # whole point, and why the technology cannot be inferred from
            # which of those two measurements turned up. RSCP and Ec/Io, not
            # RSRP and RSRQ, and no SINR field exists at all.
            if random.random() < 0.02:
                self.psc = random.randint(0, 511)
            status["serving"] = {
                "state": "NOCONN",
                "technology": "WCDMA",
                "wcdma": {
                    "mcc": n["mcc"], "mnc": n["mnc"],
                    "lac": "%04X" % random.randint(1, 65535),
                    "cell_id": cell_id,
                    "uarfcn": 10713, "psc": self.psc, "rac": 5,
                    "rscp": round(self._walk(-88, -110, -60, 1.5)),
                    "ecio": round(self._walk(-9, -20, -3, 0.6)),
                },
            }
            status["ca"] = {"pcc": None, "scc": []}
            status["neighbours"] = []
            return status

        # utils.add_technology() derives this on the router; mirror it here so
        # the dev stack exercises the same field the dashboard reads.
        status["serving"]["technology"] = "NSA" if self.nr_up else "LTE"

        if self.nr_up:
            nr = {
                "arfcn": n["nr"]["arfcn"], "band": n["nr"]["band"],
                "bandwidth": 12, "bandwidth_mhz": n["nr"]["bw"],
                "frequency_mhz": n["nr"]["freq"], "mcc": n["mcc"],
                "mnc": n["mnc"], "pci": self.nr_pci, "rsrp": nr_rsrp,
                "rsrq": round(self._walk(-11, -20, -6, 0.6)),
                "scs": n["nr"]["scs"],
                "sinr": round(self._walk(9, -5, 30, 1.5)),
            }
            status["serving"]["nr5g"] = nr
            carriers.append(dict(nr, role="scc", rat="5g"))

        status["ca"] = {"pcc": carriers[0], "scc": carriers[1:]}
        return status

    def _neighbours(self, n, lte_rsrp):
        # The set turns over, which is the whole reason neighbours are routed
        # to a bucket that expires in a day.
        out = []
        for _ in range(random.randint(2, 7)):
            out.append({
                "arfcn": random.choice(
                    [n["lte"]["arfcn"]] + [c["arfcn"] for c in n["scc"]]),
                "pci": random.randint(0, 503),
                "rat": "lte",
                "rsrp": lte_rsrp - random.randint(0, 25),
                "rsrq": random.randint(-20, -8),
                "rssi": -60,
                "scope": random.choice(["intra", "inter"]),
            })
        return out


RADIO = Radio()


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if not self.path.startswith("/cgi-bin/quectel-status"):
            self.send_error(404)
            return

        body = (json.dumps({"error": "cannot read modem"})
                if random.random() < FAIL_RATE
                else json.dumps(RADIO.status(), indent=2, sort_keys=True))
        raw = body.encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def log_message(self, fmt, *args):
        pass   # one line per poll, every 60s, forever, helps nobody


if __name__ == "__main__":
    print(f"fake router on :{PORT}/cgi-bin/quectel-status "
          f"(fail {FAIL_RATE:.0%}, NR down {NR_DROP_RATE:.0%})", flush=True)
    ThreadingHTTPServer(("", PORT), Handler).serve_forever()
