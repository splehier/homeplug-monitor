#!/usr/bin/env python3
"""Shared plctool access and parsing.

Both plc_monitor.py (the service) and plcwatch.py (the terminal view) read the
adapter through this module, so there is one parser to keep in step with
firmware quirks rather than two that quietly drift apart.

Stdlib only, on purpose: plcwatch has to keep working without the service's
dependencies installed.

Read-only. Only VS_NW_INFO and VS_SW_VER are ever requested; nothing here can
re-pair, rekey or reset an adapter.
"""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass, field

# plctool echoes its own request to this reserved address before the adapter
# answers from its real MAC. Used to tell request lines from response lines.
LOCAL_REQUEST_MAC = "00:B0:52:00:00:01"

MAC_LINE = re.compile(r"^(\S+)\s+([0-9A-Fa-f]{2}(?::[0-9A-Fa-f]{2}){5})\s+(.*)$")
KV_LINE = re.compile(r"^\s*(network|station)->(\w+)\s*=\s*(.+?)\s*$")
SOURCE_LINE = re.compile(r"^\s*source address\s*=\s*(\S+)", re.IGNORECASE)
NETWORK_COUNT = re.compile(r"Found\s+(\d+)\s+Network", re.IGNORECASE)
NUMBER = re.compile(r"\d+(?:\.\d+)?")


class PlcError(RuntimeError):
    """plctool could not be run, or answered with a recognised failure."""


def run(binary: str, iface: str, timeout_ms: int, *flags: str) -> str:
    """Run plctool against the local adapter and return its stdout."""
    cmd = [binary, "-i", iface, "-t", str(timeout_ms), *flags, "local"]
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=max(10.0, timeout_ms / 1000 * 4),
        )
    except FileNotFoundError as exc:
        raise PlcError(f"plctool not found at {binary}") from exc
    except subprocess.TimeoutExpired as exc:
        raise PlcError("plctool did not return in time") from exc
    except OSError as exc:
        raise PlcError(f"could not run plctool: {exc}") from exc

    blob = f"{proc.stdout}\n{proc.stderr}".strip()
    if "No such device" in blob:
        raise PlcError(f"interface {iface} does not exist on this host")
    if "Operation not permitted" in blob:
        raise PlcError("plctool needs CAP_NET_RAW — check the capability grant")
    if proc.returncode != 0 and not proc.stdout.strip():
        raise PlcError(blob.splitlines()[-1] if blob else "plctool failed")
    return proc.stdout


def parse_version(text: str) -> dict[str, str]:
    """Pull MAC and firmware string out of `plctool -r` output."""
    for line in text.splitlines():
        m = MAC_LINE.match(line)
        if not m:
            continue
        _iface, mac, rest = m.groups()
        if mac.upper() == LOCAL_REQUEST_MAC:
            continue  # this is the echoed request, not the answer
        return {"mac": mac.upper(), "firmware": rest.strip()}
    return {}


def parse_nwinfo(text: str) -> dict:
    """Parse `plctool -m` (VS_NW_INFO) into a network and its stations.

    Field names differ between chipsets and firmware builds, so every
    `network->` / `station->` key is kept verbatim rather than mapped onto a
    fixed schema. A repeated key means the next station block has started.
    """
    network: dict[str, str] = {}
    stations: list[dict[str, str]] = []
    current: dict[str, str] = {}
    source: str | None = None
    local_mac: str | None = None
    networks_found: int | None = None

    for line in text.splitlines():
        if m := MAC_LINE.match(line):
            mac = m.group(2).upper()
            if mac != LOCAL_REQUEST_MAC and local_mac is None:
                local_mac = mac
        if (m := SOURCE_LINE.match(line)) and source is None:
            source = m.group(1).upper()
            continue
        if (m := NETWORK_COUNT.search(line)) and networks_found is None:
            networks_found = int(m.group(1))
            continue
        if not (m := KV_LINE.match(line)):
            continue
        scope, key, value = m.groups()
        key = key.upper()
        if scope == "network":
            network[key] = value
        else:
            if key in current:
                stations.append(current)
                current = {}
            current[key] = value

    if current:
        stations.append(current)

    return {
        "source": source,
        "local_mac": local_mac or source,
        "networks_found": networks_found,
        "network": network,
        "stations": stations,
    }


def first_number(values: dict[str, str], *keys: str) -> float | None:
    for key in keys:
        raw = values.get(key)
        if raw is None:
            continue
        if m := NUMBER.search(raw):
            return float(m.group(0))
    return None


def rate_of(station: dict[str, str], direction: str) -> tuple[float | None, str]:
    """PHY rate and tonemap for one direction, in Mbit/s.

    Field names vary by firmware: QCA7550 reports AvgPHYDR_TX / AvgPHYDR_RX
    with values like "152 mbps Alternate", older builds use AVGTX / AVGRX.
    Match any rate-ish key ending in the direction rather than a fixed list.

    The tonemap word matters as much as the number: "Alternate" means the
    adapters gave up on the clean channel and fell back, which shows up before
    the rate itself collapses.
    """
    for key, value in station.items():
        up = key.upper()
        if not up.endswith(direction):
            continue
        if not ("AVG" in up or "PHY" in up or "RATE" in up or up == direction):
            continue
        if not (m := NUMBER.search(value)):
            continue
        low = value.lower()
        tonemap = "alternate" if "alternate" in low else ("primary" if "primary" in low else "")
        return float(m.group(0)), tonemap
    return None, ""


@dataclass
class Station:
    """One partner adapter on the powerline network."""

    raw: dict[str, str] = field(default_factory=dict)
    mac: str | None = None
    bda: str | None = None
    tei: str | None = None
    tx: float | None = None
    rx: float | None = None
    tx_map: str = ""
    rx_map: str = ""

    @classmethod
    def from_raw(cls, raw: dict[str, str]) -> "Station":
        tx, tx_map = rate_of(raw, "TX")
        rx, rx_map = rate_of(raw, "RX")
        return cls(
            raw=raw,
            mac=raw.get("MAC"),
            bda=raw.get("BDA"),
            tei=raw.get("TEI"),
            tx=tx,
            rx=rx,
            tx_map=tx_map,
            rx_map=rx_map,
        )

    @property
    def worst(self) -> float | None:
        """A link is only as good as its weaker direction."""
        return min([r for r in (self.tx, self.rx) if r is not None], default=None)


@dataclass
class Reading:
    """One complete look at the local adapter. Construction never raises."""

    at: float
    ok: bool = False
    error: str | None = None
    iface: str = ""
    local_mac: str | None = None
    firmware: str | None = None
    nid: str | None = None
    snid: str | None = None
    role: str | None = None
    station_count: int = 0
    stations: list[Station] = field(default_factory=list)
    network: dict[str, str] = field(default_factory=dict)

    @property
    def linked(self) -> bool:
        return self.ok and self.station_count > 0

    @property
    def tx(self) -> float | None:
        """Worst transmit rate across partners — one bad partner is a problem."""
        return _worst(s.tx for s in self.stations)

    @property
    def rx(self) -> float | None:
        return _worst(s.rx for s in self.stations)

    @property
    def best_tx(self) -> float | None:
        return _best(s.tx for s in self.stations)

    @property
    def best_rx(self) -> float | None:
        return _best(s.rx for s in self.stations)

    @property
    def worst(self) -> float | None:
        return _worst([self.tx, self.rx])

    @property
    def tonemap(self) -> str:
        """Worst tonemap in use: one partner on Alternate colours the whole link."""
        maps = {m for s in self.stations for m in (s.tx_map, s.rx_map) if m}
        if "alternate" in maps:
            return "alternate"
        return "primary" if "primary" in maps else ""

    @property
    def net_key(self) -> tuple:
        """Identity of the powerline network, for spotting renegotiations."""
        return (self.nid, self.snid)


def _worst(values) -> float | None:
    vals = [v for v in values if v is not None]
    return min(vals) if vals else None


def _best(values) -> float | None:
    vals = [v for v in values if v is not None]
    return max(vals) if vals else None


def read(
    binary: str,
    iface: str,
    timeout_ms: int,
    at: float,
    with_version: bool = False,
) -> Reading:
    """One full read of the local adapter. Never raises.

    `with_version` adds a second plctool round trip for MAC and firmware.
    Those do not change between reboots, so the caller should ask rarely.
    """
    reading = Reading(at=at, iface=iface)
    try:
        info = parse_nwinfo(run(binary, iface, timeout_ms, "-m"))
        net = info["network"]
        reading.network = net
        reading.local_mac = info["local_mac"]
        reading.nid = net.get("NID")
        reading.snid = net.get("SNID")
        reading.role = net.get("ROLE")
        reading.stations = [Station.from_raw(r) for r in info["stations"]]

        declared = first_number(net, "STATIONS")
        # A declared count with no digits in it should not erase stations we
        # actually parsed, so fall back rather than trusting a blank.
        reading.station_count = (
            int(declared) if declared is not None else len(reading.stations)
        )
        reading.ok = True

        if with_version:
            version = parse_version(run(binary, iface, timeout_ms, "-r"))
            reading.local_mac = version.get("mac") or reading.local_mac
            reading.firmware = version.get("firmware")
    except PlcError as exc:
        reading.ok = False
        reading.error = str(exc)
    return reading
