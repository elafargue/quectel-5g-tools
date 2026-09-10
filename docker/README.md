# Development stack

A whole TIG stack with a modem-shaped thing at the end of it.

The pipeline this repo feeds has four moving parts and none of them can be
tested alone. A Telegraf parser config is only right if InfluxDB ends up
holding what the dashboard queries; the dashboard's queries are only right if
they match the tags Telegraf actually wrote; and the DBRP mappings that let
InfluxDB 2.x answer InfluxQL at all are invisible until a panel comes back
empty. This runs the lot against a fake router, so the chain can be exercised
without a boat, a modem, or a SIM.

```bash
./up.sh              # regenerates the dashboard, then docker compose up
./verify.sh          # assert the data arrived, and arrived shaped correctly
docker compose down -v
```

Grafana lands on <http://localhost:13000> with anonymous admin. The
credentials in `compose.yml` are deliberately worthless.

The ports are deliberately not the obvious ones: a machine that does this kind
of work usually already has an InfluxDB on 8086 and a Grafana on 3000, and a
development stack that cannot start beside the real thing is not much of a
development stack. `QUECTEL_GRAFANA_PORT`, `QUECTEL_INFLUX_PORT` and
`QUECTEL_ROUTER_PORT` override them, and `verify.sh` reads the same
variables.

## What is in it

| | |
|---|---|
| **fake-router** | Stands in for a GL-X3000 on `:8080`. Walks the signal, hands over between sites, drops the NR leg, and refuses to answer 2% of the time. |
| **influxdb** | 2.7, with `systemhealth` (30d) and `systemhealth_short` (24h), and the DBRP mappings that make InfluxQL work. |
| **telegraf** | `telegraf/quectel.conf` verbatim, pointed at the fake router and polling every 10s instead of 60. |
| **grafana** | 11.6, no plugins, both InfluxDB datasources and the dashboard provisioned. |

## The fake router earns its place

A pipeline fed a constant produces flat lines that prove nothing. This one
moves: a slow random walk with a long swell under it, handovers to new sites,
a roam across three networks, a neighbour list that turns over, an NR leg
that comes and goes, and the occasional drop to 3G.

Values and shapes come from the real captures in `tests/test-parser` — KT
Korea and Free Mobile France — and nothing is invented but the noise, with one
exception labelled as such in `serve.py`: a third network with two secondaries
on the same band, which no capture has shown and which is the only thing that
exercises the carrier series key. Knobs in `compose.yml` worth turning:

- `FAIL_RATE` — how often it answers `{"error": "cannot read modem"}` instead
  of a reading. Watch the graphs go to gaps rather than to zero.
- `NR_DROP_RATE` — how often the NR leg is detached. Every LTE metric stays
  healthy through it, which is the whole reason `5g-watchdog` exists.
- `WCDMA_RATE` — how often it answers as a 3G attach: no LTE cell, no NR, no
  carriers. The connection-mode panel should show 3G, not a gap.
- `QUECTEL_NETWORK` — which network to start on (0 KT, 1 Free, 2 synthetic).
  Roaming is rare, so a check that needs one network wants this rather than a
  wait: `QUECTEL_NETWORK=2 docker compose up -d fake-router`.

## verify.sh is the point

Every check corresponds to a mistake that is silent in production:

- **Tags that should not be tags.** `cell_id` as a tag is one index entry per
  cell ever visited, kept for the life of the bucket. It costs nothing today
  and an unbounded index in a year. The script asserts `band` and `duplex`
  *are* tags and that `cell_id`, `pci`, `tac` and `arfcn` are *not* — while
  still being present as fields.
- **Output routing.** Telegraf outputs are global. The script asserts
  `quectel_neighbour` is in the short bucket *and* absent from the long one:
  missing from the first means it was never routed, present in the second
  means it will be kept for as long as everything else.
- **DBRP mappings.** Without them 2.x answers InfluxQL with nothing at all,
  and no error anywhere says why. The script queries through the same v1
  endpoint Grafana uses, so the failure surfaces here instead of as a blank
  panel.
- **The dashboard's own queries**, run as written, including the neighbour
  panel's cross-retention-policy one.

## Trying a panel change

The dashboard provider has `allowUiUpdates: true`, so the stack is somewhere
to try an edit in the browser before writing it into
`grafana/generate_alternative.py`. Edits made in the UI live only in the
container's database — `./up.sh` regenerates from the generator and
`docker compose down -v` discards them. The generator stays the source of
truth.

## What building this caught

The stack paid for itself before it was finished. Every one of these was in
code already written and reviewed, and every one is silent in production —
which is the argument for having somewhere to run the whole chain.

**A detached NR leg stopped all data.** Telegraf's `json_v2` fails the *entire*
parse when one path is missing, not just the block that names it. `serving.nr5g`
is absent whenever NR detaches, so an NR drop would have stopped LTE signal,
carriers and neighbours from being recorded too — at exactly the moment worth
recording. Fixed with `optional = true` on every object.

**Several `json_v2` blocks in one input do not make several measurements.**
They merge: the last `measurement_name` wins and every tag from every block
lands on every metric as a cross product. The result was a single measurement
called `quectel_device` carrying neighbour PCIs as tags on the serving cell. It
parses without complaint. Fixed by using one `[[inputs.http]]` per measurement.

**A metric whose every key is a tag has no fields, and is dropped in silence.**
`quectel_operator` listed all three of its keys as tags and simply never
appeared. Fixed by leaving one as a field.

**`"short"."measurement"` is not honoured by InfluxDB 2.x's v1 layer.** It
answers with no series and no error, and `SHOW MEASUREMENTS` ignores the `rp`
parameter as well. The dashboard's neighbour panel would have been permanently
empty. Fixed with a DBRP mapping that exposes the short bucket as a database of
its own, and a second Grafana datasource pointing at it — an InfluxQL
datasource carries exactly one database and no way to name a policy per panel.

**`distinct()` does not operate on tags.** `count(distinct("pci"))` over a tag
returns nothing rather than erroring. Fixed by counting a field instead.

None of these produce an error anyone would see on a boat. They produce a
dashboard that is merely empty, which looks exactly like a quiet radio.
