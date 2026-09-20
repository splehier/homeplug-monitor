# HomePlug link monitor

Polls a HomePlug AV powerline adapter, serves a status page, records history,
and publishes the link state to MQTT with Home Assistant discovery.

Powerline links fail quietly. An adapter drops off the network, or renegotiates
down to a fraction of its rate, and nothing announces it — the first sign is
usually that something upstairs has been slow for a week. This watches the link
continuously and tells you when it changes.

**Read-only.** It issues `VS_NW_INFO` and `VS_SW_VER` and nothing else. There
is no code path here that can re-pair, rekey, reset or reconfigure an adapter.

## Compatibility

This talks to adapters through [`plctool`](https://github.com/qca/open-plc-utils)
from Qualcomm's open-plc-utils, so what matters is the **chipset, not the
brand**. TP-Link, Devolo, Netgear, Zyxel, Solwise and others all ship
QCA-based HomePlug adapters and are equally likely to work.

| | |
| --- | --- |
| **Works** | HomePlug AV / AV2 adapters on Qualcomm Atheros silicon. `plctool` documents QCA6410, QCA7000 and QCA7420; its chipset table also recognises INT6000/6300/6400, QCA6411, QCA7005, QCA7006AQ, QCA7450/7451 and QCA7500. |
| **Might work** | AR7400 and QCA7450 parts through `amptool`, and legacy Intellon parts through `int6k`. Both are siblings of `plctool` with the same command shape and output format, so setting `PLC_PLCTOOL` to one of them should be enough. Untested — reports welcome. |
| **Will not work** | **G.hn adapters.** Devolo Magic, Zyxel G.hn and anything else sold as "G.hn" use a different standard entirely. `plctool` cannot address them at all and every poll will report "cannot read adapter". |

Any number of adapters is supported, not just a pair — see
[More than two adapters](#more-than-two-adapters).

## Requirements

- A Linux host on the **same layer-2 segment** as the powerline adapter. A
  routed hop kills this: the protocol is raw ethernet frames (ethertype
  `0x88E1`), not IP.
- Docker with **host networking**. The default bridge NATs at layer 3 and drops
  the frames, so `network_mode: host` is a requirement rather than a
  convenience. This also means it cannot run under Docker Desktop on macOS or
  Windows, where containers sit in a NATed VM.
- `CAP_NET_RAW` and `CAP_NET_ADMIN`, granted in the compose file. The container
  still runs with `no-new-privileges` and a read-only root filesystem.

## Quick start

```bash
git clone https://github.com/USER/homeplug-monitor.git
cd homeplug-monitor

cp plc-monitor.env.example plc-monitor.env
$EDITOR plc-monitor.env          # set PLC_IFACE, at minimum

docker compose up -d --build
docker compose logs -f
```

Then open `http://<host>:8087`.

### Find the interface name first

```bash
ip -br link
```

Usually `eth0`, `enp3s0` or `bond0`. On a Synology NAS with Virtual Machine
Manager installed, DSM switches to Open vSwitch and the name gains an `ovs_`
prefix — use `ovs_eth0` or `ovs_bond0`, not the underlying interface.

A wrong `PLC_IFACE` is the single most common cause of "cannot read adapter".

### Verify it can reach the adapter

```bash
docker exec plc-monitor plctool -i eth0 -t 1000 -m local
```

If that prints a network and station list, the container can see the adapter
and any remaining problem is configuration. If it prints nothing, the interface
name or the VLAN is wrong.

## Configuration

Everything is an environment variable, read from `plc-monitor.env`.

| Variable | Default | Meaning |
| --- | --- | --- |
| `PLC_IFACE` | `enp3s0` | Interface on the adapter's layer-2 segment. **Set this.** |
| `PLC_POLL_INTERVAL` | `60` | Seconds between reads |
| `PLC_TIMEOUT_MS` | `1000` | plctool read timeout |
| `PLC_EXPECTED_STATIONS` | `1` | Partner adapters expected — *partners*, so a pair is 1 |
| `PLC_MARGINAL_MBPS` | `50` | Below this the link counts as weak |
| `PLC_MAX_MBPS` | `1300` | Gauge fallback only; the scale auto-fits the last 24h |
| `PLC_DB_PATH` | *(next to the script)* | **Must be `/data/plc-history.db` under Docker** — the rest of the filesystem is read-only |
| `PLC_RETENTION_DAYS` | `14` | History window |
| `PLC_VERSION_EVERY` | `60` | Polls between firmware reads |
| `PLC_HTTP_HOST` / `PLC_HTTP_PORT` | `0.0.0.0` / `8087` | Web service |
| `PLC_MQTT_HOST` | *(empty)* | Leave empty to run without MQTT |
| `PLC_MQTT_PORT` / `_USER` / `_PASS` | `1883` | Broker |
| `PLC_MQTT_PREFIX` | `plcmon/powerline` | Topic prefix |
| `PLC_MQTT_DISCOVERY_PREFIX` | `homeassistant` | Discovery prefix |
| `PLC_DEVICE_NAME` / `_ID` | `Powerline link` | Home Assistant device |
| `PLC_DEVICE_MANUFACTURER` | *(empty)* | Optional label on the HA device card |
| `PLC_PLCTOOL` | *(auto)* | Path to plctool, or to `amptool` / `int6k` |
| `PLC_LOG_LEVEL` | `INFO` | |

Values are read by Docker Compose's `env_file`, which **does not strip
quotes**. Write `PLC_DB_PATH=/data/plc-history.db`, not
`PLC_DB_PATH="/data/plc-history.db"` — the quotes become part of the value.

## What it reports

| State | Meaning |
| --- | --- |
| **Linked** | All expected partners present, both directions above the marginal threshold |
| **Weak link** | Partner present but the *weaker* direction is below `PLC_MARGINAL_MBPS` |
| **Degraded** | Fewer partners than `PLC_EXPECTED_STATIONS` |
| **No partner** | The adapter formed a network and nothing joined it |
| **Cannot read adapter** | `plctool` failed — wrong interface, missing capability, or dead adapter |

The last row is deliberately not "no partner". A monitor that cannot read the
adapter knows nothing about the powerline, and reporting a link failure there
is a false alarm. Over MQTT it publishes as *unavailable*, not *off*.

Link health is judged on the **weaker direction**, because 152 Mbit/s out and
8 Mbit/s back is a broken link, not a healthy one.

## The page

Laid out after [btop++](https://github.com/aristocratos/btop): four framed
panels — **link**, **rate**, **adapters**, **events** — distinguished by frame
colour, with titles notched into the top border, in btop's default palette.

The rate panel mirrors transmit above receive about a centre line, the way
btop's net box draws download over upload, and colours each graph with a fixed
vertical gradient that the data rises into rather than colouring by value. The
shaded band is the per-bucket minimum and maximum, so a link swinging between
15 and 68 Mbit/s looks different from one holding steady at 40. Hovering reads
out any bucket. Failed reads and partner-less stretches are tinted into the
graph itself, so an outage is visible where it happened. `1` `6` `2` `7` switch
range.

No external assets, no web fonts, nothing loaded from a CDN.

## History and outages

Every poll is one row in a SQLite file. Bucketing happens in SQL, so a week
still returns a few hundred points rather than tens of thousands. At 15-second
polling, fourteen days is roughly 80k rows and 3 MB. The expensive part of this
service is talking to the adapter, not storing the answer.

`/api/outages` turns those samples into what the chart cannot show: how often
the link actually dropped, and for how long.

```bash
curl -s 'localhost:8087/api/outages?hours=168' | jq '{uptime_pct, renegotiations}'
```

Two kinds of gap are counted separately, because they mean different things:

- **down** — the adapter was readable and reported no partner. A real outage.
- **blind** — `plctool` failed. A monitoring failure, not a powerline failure.

`uptime_pct` is measured against samples that could actually be read, so being
blind lowers `coverage_pct` instead of pretending the link was down.

**Renegotiations** are counted from `SNID` changes between consecutive samples.
This is the failure mode that hides from everything else: a link that
renegotiates every few minutes reads as "Linked" in every individual sample,
while dropping traffic each time it does. If uptime looks perfect but the link
feels bad, look here first.

## Tonemap

Firmware reports rates as e.g. `152 mbps Alternate`. That word is kept and
surfaced. **Alternate** means the adapters abandoned the clean channel and fell
back, which tends to show up *before* the rate itself collapses — an early
warning of noise on the circuit.

## Home Assistant

Set `PLC_MQTT_HOST` and the entities appear under a single device: link
(connectivity), partner adapters, transmit rate, receive rate, link health,
tonemap, link uptime 24h, renegotiations 24h, firmware.

Availability tracks **whether the adapter can be read**, not whether the broker
is reachable. If `plctool` starts failing, entities go *unavailable* rather
than reporting the link as down. A last will covers the service dying
altogether. The verbatim station table is not published — it is large, changes
every poll, and is only useful on the page.

```yaml
automation:
  - alias: Powerline link lost
    trigger:
      - platform: state
        entity_id: binary_sensor.powerline_link_link
        to: "off"
        for: "00:10:00"
    action:
      - service: notify.mobile_app
        data:
          title: Powerline link down
          message: No partner adapter for 10 minutes.
```

The delay avoids alerting on a single failed poll or a brief renegotiation.
Because `unavailable` is not `off`, this does not fire when the monitor itself
breaks — watch `sensor.powerline_link_link_health` for that.

The second automation worth having catches the failure that never appears as an
outage at all:

```yaml
  - alias: Powerline link unstable
    trigger:
      - platform: numeric_state
        entity_id: sensor.powerline_link_renegotiations_24h
        above: 20
    action:
      - service: notify.mobile_app
        data:
          title: Powerline renegotiating
          message: >
            {{ states('sensor.powerline_link_renegotiations_24h') }}
            renegotiations in 24h. Each one is a brief dropout.
```

## API

| Endpoint | Returns |
| --- | --- |
| `/` | the page |
| `/api/status` | current reading, including the verbatim station table |
| `/api/history?hours=&buckets=` | bucketed rate history |
| `/api/outages?hours=&limit=` | outages, uptime, renegotiations |
| `/healthz` | 200 while the adapter is readable **and** recently polled, else 503 |

`/healthz` fails on staleness as well as on read errors, so a poller that died
silently cannot keep looking healthy. The container's `HEALTHCHECK` uses it.

## Terminal watcher

`plcwatch` ships in the image, so the live view needs nothing installed:

```bash
docker exec -it plc-monitor plcwatch -i eth0 -n 15
```

It polls independently of the service; running both is fine, since both only
read. `--once` prints a single line and exits **0** when linked, **2** when
there is no partner and **1** when the adapter could not be read, which makes
it usable from cron or a Home Assistant `command_line` sensor. Point `--log` at
`/data` if the CSV should survive the container.

## More than two adapters

Nothing assumes a pair. Station blocks are parsed for any count, rates are
reported from the **worst** partner rather than the best, and
`PLC_EXPECTED_STATIONS` drives the `degraded` state when some but not all
adapters are present.

One limitation: history stores a single tx/rx pair per sample — the worst
across partners — so the chart shows the link's worst case rather than a series
per peer. The adapters panel still lists every partner individually with its
own rates and tonemap.

## If the rate columns stay empty

Station field names vary between chipsets and firmware builds. The parser keeps
every `station->` key verbatim and the page shows them all, so if a firmware
reports rates under names other than `AvgPHYDR_TX` / `AVGTX`, they will still
appear in the adapters panel. Open an issue with the key names and they can be
mapped.

## Layout

```
plc.py                  shared plctool runner and parsers, stdlib only
plc_monitor.py          service: polling, history, outages, MQTT, HTTP
index.html              status page, no external assets
plcwatch.py             terminal watcher
requirements.txt        fastapi, uvicorn, paho-mqtt
Dockerfile              builds plctool from source onto a slim Python base
docker-compose.yml      host networking plus NET_RAW
plc-monitor.env.example copy to plc-monitor.env
```

`plc.py` is stdlib-only on purpose, so `plcwatch` works without the service's
dependencies. In the image it is installed to `/app` and reached through
`PYTHONPATH`, so `plcwatch` works from anywhere on `PATH`.

## Building

The image compiles `plctool` from the open-plc-utils sources at build time and
copies the single binary onto a slim Python base. It links against libc only,
so nothing else is needed at runtime. Pin a commit for reproducible builds:

```bash
docker compose build --build-arg OPEN_PLC_UTILS_REF=<ref>
```

## Acknowledgements

**[btop++](https://github.com/aristocratos/btop)** by Aristocratos — Apache
License 2.0.

The status page is a deliberate homage to btop's visual design: framed panels
with the title notched into the top border, panels distinguished by frame
colour, the mirrored dual graph, the block meters, and the colour values of
btop's built-in Default theme.

No btop source code is used here. btop is C++; this page is hand-written HTML,
CSS and SVG. But the look is unmistakably its work, and the palette values were
read from btop's `btop_theme.cpp`, so the credit is owed and gladly given. If
you like how this looks, go and use btop — it is a far better piece of software
than this one.

**[open-plc-utils](https://github.com/qca/open-plc-utils)** by Qualcomm Atheros
— BSD-style three-clause licence. `plctool` is compiled from upstream sources
during the image build and is not vendored in this repository.

## Licence

MIT — see [LICENSE](LICENSE), which also carries the third-party notices for
btop++ and open-plc-utils.

Note that a Docker image built from this repository *contains* `plctool`, and
its BSD licence requires binary redistributions to reproduce Qualcomm's
copyright notice and licence conditions. That applies to you if you publish the
built image, not if you merely build and run it yourself.
