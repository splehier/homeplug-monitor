#!/usr/bin/env python3
"""Powerline link monitor.

Polls a local HomePlug AV adapter through plc.py, exposes the result as JSON
and a status page, and publishes it to MQTT with Home Assistant discovery.

Read-only: only VS_NW_INFO and VS_SW_VER are ever requested. Nothing here can
re-pair, rekey or reset an adapter.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import shutil
import sqlite3
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import paho.mqtt.client as mqtt
import uvicorn
from fastapi import FastAPI
from fastapi.responses import HTMLResponse, JSONResponse

import plc

VERSION = "1.0"

log = logging.getLogger("plc-monitor")

# --------------------------------------------------------------------------
# Configuration (environment, with sane defaults)
# --------------------------------------------------------------------------


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except ValueError:
        log.warning("%s is not an integer, using %s", name, default)
        return default


IFACE = os.getenv("PLC_IFACE", "enp3s0")
PLCTOOL = os.getenv("PLC_PLCTOOL", shutil.which("plctool") or "/usr/local/bin/plctool")
TIMEOUT_MS = _env_int("PLC_TIMEOUT_MS", 1000)
POLL_INTERVAL = _env_int("PLC_POLL_INTERVAL", 60)
EXPECTED_STATIONS = _env_int("PLC_EXPECTED_STATIONS", 1)
# Below this PHY rate the link is up but too slow to be trusted.
MARGINAL_MBPS = _env_int("PLC_MARGINAL_MBPS", 50)
# Ceiling for the rate gauge when history has nothing better to offer.
MAX_MBPS = _env_int("PLC_MAX_MBPS", 1300)
# Firmware and MAC do not change between reboots, so they cost one extra
# plctool round trip this often rather than on every poll.
VERSION_EVERY = _env_int("PLC_VERSION_EVERY", 60)

DB_PATH = os.getenv("PLC_DB_PATH", str(Path(__file__).resolve().parent / "plc-history.db"))
RETENTION_DAYS = _env_int("PLC_RETENTION_DAYS", 14)

HTTP_HOST = os.getenv("PLC_HTTP_HOST", "0.0.0.0")
HTTP_PORT = _env_int("PLC_HTTP_PORT", 8087)

MQTT_HOST = os.getenv("PLC_MQTT_HOST", "")
MQTT_PORT = _env_int("PLC_MQTT_PORT", 1883)
MQTT_USER = os.getenv("PLC_MQTT_USER", "")
MQTT_PASS = os.getenv("PLC_MQTT_PASS", "")
MQTT_PREFIX = os.getenv("PLC_MQTT_PREFIX", "plcmon/powerline")
DISCOVERY_PREFIX = os.getenv("PLC_MQTT_DISCOVERY_PREFIX", "homeassistant")
DEVICE_NAME = os.getenv("PLC_DEVICE_NAME", "Powerline link")
DEVICE_ID = os.getenv("PLC_DEVICE_ID", "plc_powerline")
# Cosmetic: the label on the Home Assistant device card. Left empty by
# default because nothing here is tied to any particular vendor.
DEVICE_MANUFACTURER = os.getenv("PLC_DEVICE_MANUFACTURER", "")

HERE = Path(__file__).resolve().parent

# A reading older than this is stale enough that the service is no longer
# monitoring anything, whatever the last sample said.
STALE_AFTER = max(90, POLL_INTERVAL * 3)

# How often the rolling 24h uptime and the gauge scale are recomputed. Both
# scan a day of samples and neither moves fast enough to want it per poll.
ROLLUP_INTERVAL = max(60, POLL_INTERVAL * 5)


# --------------------------------------------------------------------------
# State
# --------------------------------------------------------------------------


@dataclass
class Status:
    ok: bool = False
    error: str | None = None
    version: str = VERSION
    iface: str = IFACE
    local_mac: str | None = None
    firmware: str | None = None
    nid: str | None = None
    snid: str | None = None
    role: str | None = None
    station_count: int = 0
    stations: list[dict[str, Any]] = field(default_factory=list)
    tx_mbps: float | None = None
    rx_mbps: float | None = None
    best_tx_mbps: float | None = None
    best_rx_mbps: float | None = None
    tonemap: str = ""
    linked: bool = False
    health: str = "unknown"  # linked | marginal | degraded | down | unknown
    polled_at: float | None = None
    changed_at: float | None = None
    last_linked_at: float | None = None
    started_at: float = 0.0
    renegotiations: int = 0
    expected_stations: int = EXPECTED_STATIONS
    marginal_mbps: int = MARGINAL_MBPS
    max_mbps: int = MAX_MBPS
    scale_mbps: int = MAX_MBPS
    poll_interval: int = POLL_INTERVAL
    history_ok: bool = True
    db_path: str = DB_PATH

    @property
    def stale(self) -> bool:
        return self.polled_at is None or (time.time() - self.polled_at) > STALE_AFTER


STARTED_AT = time.time()
STATE = Status(started_at=STARTED_AT)
RENEGOTIATIONS = 0
LAST_NET_KEY: tuple | None = None
_LAST_VERSION: dict[str, str | None] = {"mac": None, "firmware": None}
_POLL_COUNT = 0


def _station_dict(station: plc.Station) -> dict[str, Any]:
    return {
        "mac": station.mac,
        "bda": station.bda,
        "tei": station.tei,
        "tx": station.tx,
        "rx": station.rx,
        "tx_map": station.tx_map,
        "rx_map": station.rx_map,
        "raw": station.raw,
    }


def health_of(reading: plc.Reading) -> str:
    """Four ways for a link to be less than fine, kept apart deliberately.

    `unknown` is not `down`: a monitor that cannot read the adapter knows
    nothing about the powerline, and saying "down" there is a false alarm.
    """
    if not reading.ok:
        return "unknown"
    if reading.station_count == 0:
        return "down"
    if reading.station_count < EXPECTED_STATIONS:
        return "degraded"
    worst = reading.worst
    if worst is not None and worst < MARGINAL_MBPS:
        return "marginal"
    return "linked"


def poll_once() -> Status:
    """One full read of the local adapter. Never raises."""
    global RENEGOTIATIONS, LAST_NET_KEY, _POLL_COUNT

    previous = STATE
    now = time.time()

    _POLL_COUNT += 1
    want_version = (
        _POLL_COUNT == 1
        or _LAST_VERSION["firmware"] is None
        or _POLL_COUNT % max(1, VERSION_EVERY) == 0
    )
    reading = plc.read(PLCTOOL, IFACE, TIMEOUT_MS, now, with_version=want_version)
    if reading.ok and want_version and reading.firmware:
        _LAST_VERSION["mac"] = reading.local_mac
        _LAST_VERSION["firmware"] = reading.firmware

    if reading.ok and reading.net_key != (None, None):
        if LAST_NET_KEY is not None and reading.net_key != LAST_NET_KEY:
            RENEGOTIATIONS += 1
            log.info("network renegotiated: snid %s -> %s", LAST_NET_KEY[1], reading.snid)
        LAST_NET_KEY = reading.net_key

    status = Status(
        ok=reading.ok,
        error=reading.error,
        local_mac=reading.local_mac or _LAST_VERSION["mac"],
        firmware=reading.firmware or _LAST_VERSION["firmware"],
        nid=reading.nid,
        snid=reading.snid,
        role=reading.role,
        station_count=reading.station_count,
        stations=[_station_dict(s) for s in reading.stations],
        tx_mbps=reading.tx,
        rx_mbps=reading.rx,
        best_tx_mbps=reading.best_tx,
        best_rx_mbps=reading.best_rx,
        tonemap=reading.tonemap,
        linked=reading.linked,
        health=health_of(reading),
        polled_at=now,
        started_at=STARTED_AT,
        renegotiations=RENEGOTIATIONS,
        history_ok=HISTORY.conn is not None,
    )

    if not reading.ok:
        log.warning("poll failed: %s", reading.error)

    status.last_linked_at = now if status.linked else previous.last_linked_at
    status.changed_at = (
        previous.changed_at
        if previous.changed_at and previous.health == status.health
        else now
    )
    status.scale_mbps = previous.scale_mbps
    return status


# --------------------------------------------------------------------------
# History
# --------------------------------------------------------------------------

BASE_COLUMNS = ("ts", "ok", "stations", "tx", "rx")
ADDED_COLUMNS = {
    "snid": "TEXT",
    "role": "TEXT",
    "tonemap": "TEXT",
}


class History:
    """A small SQLite ring of samples, bucketed or summarised on read.

    One INSERT per poll and a DELETE once an hour is nothing for the NAS. All
    access goes through a lock and is called from a worker thread, so the
    event loop never blocks on disk.
    """

    def __init__(self, path: str, retention_days: int) -> None:
        self.path = path
        self.retention = retention_days * 86400
        self.lock = threading.Lock()
        self.conn: sqlite3.Connection | None = None
        self.last_prune = 0.0

    def open(self) -> None:
        try:
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(self.path, check_same_thread=False)
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS samples (
                    ts       INTEGER PRIMARY KEY,
                    ok       INTEGER NOT NULL,
                    stations INTEGER NOT NULL,
                    tx       REAL,
                    rx       REAL,
                    snid     TEXT,
                    role     TEXT,
                    tonemap  TEXT
                )
                """
            )
            # v1.0 databases predate these columns; add what is missing so an
            # existing history file keeps its data across the upgrade.
            have = {row[1] for row in conn.execute("PRAGMA table_info(samples)")}
            for name, decl in ADDED_COLUMNS.items():
                if name not in have:
                    conn.execute(f"ALTER TABLE samples ADD COLUMN {name} {decl}")
                    log.info("history: added column %s", name)
            conn.commit()
            self.conn = conn
            log.info("history at %s, keeping %d days", self.path, self.retention // 86400)
        except (sqlite3.Error, OSError) as exc:
            log.error(
                "history disabled: cannot use %s (%s). Set PLC_DB_PATH to a "
                "writable location — under Docker the filesystem is read-only "
                "apart from the /data volume.",
                self.path,
                exc,
            )
            self.conn = None

    def record(self, status: Status) -> None:
        if not self.conn or status.polled_at is None:
            return
        with self.lock:
            try:
                self.conn.execute(
                    "INSERT OR REPLACE INTO samples "
                    "(ts, ok, stations, tx, rx, snid, role, tonemap) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        int(status.polled_at),
                        int(status.ok),
                        status.station_count,
                        status.tx_mbps,
                        status.rx_mbps,
                        status.snid,
                        status.role,
                        status.tonemap or None,
                    ),
                )
                self.conn.commit()
                now = time.time()
                if now - self.last_prune > 3600:
                    self.conn.execute(
                        "DELETE FROM samples WHERE ts < ?", (int(now - self.retention),)
                    )
                    self.conn.commit()
                    self.last_prune = now
            except sqlite3.Error as exc:
                log.warning("history write failed: %s", exc)

    def _query(self, sql: str, args: tuple = ()) -> list:
        if not self.conn:
            return []
        with self.lock:
            try:
                return self.conn.execute(sql, args).fetchall()
            except sqlite3.Error as exc:
                log.warning("history read failed: %s", exc)
                return []

    def series(self, hours: float, buckets: int) -> dict[str, Any]:
        """Bucketed history: average, floor and ceiling per bucket.

        Bucketing happens in SQL so a week of data still returns a few hundred
        points rather than tens of thousands.
        """
        empty = {"hours": hours, "points": [], "available": False}
        if not self.conn:
            return empty

        since = int(time.time() - hours * 3600)
        width = max(1, int(hours * 3600 / max(1, buckets)))
        rows = self._query(
            """
            SELECT (ts / ?) * ?          AS bucket,
                   AVG(tx), MIN(tx), MAX(tx),
                   AVG(rx), MIN(rx), MAX(rx),
                   MIN(ok), MIN(stations), COUNT(*)
            FROM samples
            WHERE ts >= ?
            GROUP BY bucket
            ORDER BY bucket
            """,
            (width, width, since),
        )
        points = [
            {
                "t": r[0],
                "tx": None if r[1] is None else round(r[1], 1),
                "tx_lo": r[2],
                "tx_hi": r[3],
                "rx": None if r[4] is None else round(r[4], 1),
                "rx_lo": r[5],
                "rx_hi": r[6],
                "ok": bool(r[7]),
                "linked": bool(r[8]),
                "n": r[9],
            }
            for r in rows
        ]
        return {
            "hours": hours,
            "bucket_seconds": width,
            "points": points,
            "available": True,
        }

    def peak(self, hours: float = 24) -> float | None:
        """Best rate seen recently, for scaling the gauge to reality."""
        since = int(time.time() - hours * 3600)
        rows = self._query(
            "SELECT MAX(tx), MAX(rx) FROM samples WHERE ts >= ?", (since,)
        )
        if not rows:
            return None
        values = [v for v in rows[0] if v is not None]
        return max(values) if values else None

    def summary(self, hours: float, limit: int = 50) -> dict[str, Any]:
        """Outages, uptime and renegotiations over a window.

        Derived by scanning samples rather than from an event log, so it works
        retroactively over history recorded before this code existed.
        """
        empty = {
            "hours": hours,
            "available": False,
            "outages": [],
            "samples": 0,
            "uptime_pct": None,
            "coverage_pct": None,
            "renegotiations": 0,
            "blind_seconds": 0,
            "down_seconds": 0,
        }
        if not self.conn:
            return empty

        now = time.time()
        since = int(now - hours * 3600)
        rows = self._query(
            "SELECT ts, ok, stations, snid FROM samples WHERE ts >= ? ORDER BY ts",
            (since,),
        )
        if not rows:
            return {**empty, "available": True}

        def state_of(ok: int, stations: int) -> str:
            if not ok:
                return "blind"
            return "linked" if stations > 0 else "down"

        runs: list[dict[str, Any]] = []
        total = len(rows)
        ok_count = 0
        linked_count = 0
        renegotiations = 0
        last_snid: str | None = None
        run_state = state_of(rows[0][1], rows[0][2])
        run_start = rows[0][0]

        for ts, ok, stations, snid in rows:
            ok_count += bool(ok)
            linked_count += bool(ok and stations > 0)
            if ok and snid:
                if last_snid is not None and snid != last_snid:
                    renegotiations += 1
                last_snid = snid

            state = state_of(ok, stations)
            if state != run_state:
                runs.append({"state": run_state, "start": run_start, "end": ts})
                run_state = state
                run_start = ts
        runs.append({"state": run_state, "start": run_start, "end": int(now)})

        outages = [
            {
                "kind": r["state"],
                "start": r["start"],
                "end": r["end"],
                "seconds": max(0, r["end"] - r["start"]),
                "ongoing": r is runs[-1] and r["state"] != "linked",
            }
            for r in runs
            if r["state"] != "linked"
        ]
        down_seconds = sum(o["seconds"] for o in outages if o["kind"] == "down")
        blind_seconds = sum(o["seconds"] for o in outages if o["kind"] == "blind")

        return {
            "hours": hours,
            "available": True,
            "samples": total,
            # Uptime is measured against samples we could actually read: being
            # blind is a monitoring failure, not a powerline outage.
            "uptime_pct": round(linked_count / ok_count * 100, 2) if ok_count else None,
            "coverage_pct": round(ok_count / total * 100, 2) if total else None,
            "renegotiations": renegotiations,
            "down_seconds": down_seconds,
            "blind_seconds": blind_seconds,
            "outages": list(reversed(outages))[:limit],
        }

    def seed(self, linked_now: bool) -> dict[str, float | None]:
        """Recover last-linked and last-changed across a restart.

        Without this the page claims "never" after every rebuild, while the
        database on disk holds weeks of evidence to the contrary.
        """
        out: dict[str, float | None] = {"last_linked_at": None, "changed_at": None}
        rows = self._query("SELECT MAX(ts) FROM samples WHERE stations > 0")
        if rows and rows[0][0]:
            out["last_linked_at"] = float(rows[0][0])
        rows = self._query(
            "SELECT MAX(ts) FROM samples WHERE (stations > 0) != ?", (int(linked_now),)
        )
        if rows and rows[0][0]:
            out["changed_at"] = float(rows[0][0])
        return out

    def close(self) -> None:
        with self.lock:
            if self.conn:
                self.conn.close()
                self.conn = None


HISTORY = History(DB_PATH, RETENTION_DAYS)


# --------------------------------------------------------------------------
# MQTT
# --------------------------------------------------------------------------

STATE_TOPIC = f"{MQTT_PREFIX}/state"
AVAIL_TOPIC = f"{MQTT_PREFIX}/availability"

DEVICE_BLOCK = {
    "identifiers": [DEVICE_ID],
    "name": DEVICE_NAME,
    "model": "HomePlug AV adapter",
    "sw_version": VERSION,
}
if DEVICE_MANUFACTURER:
    DEVICE_BLOCK["manufacturer"] = DEVICE_MANUFACTURER

ENTITIES: list[dict[str, Any]] = [
    {
        "component": "binary_sensor",
        "object_id": "link",
        "name": "Link",
        "device_class": "connectivity",
        "value_template": "{{ 'ON' if value_json.linked else 'OFF' }}",
    },
    {
        "component": "sensor",
        "object_id": "stations",
        "name": "Partner adapters",
        "state_class": "measurement",
        "value_template": "{{ value_json.station_count }}",
    },
    {
        "component": "sensor",
        "object_id": "tx_rate",
        "name": "Transmit rate",
        "unit_of_measurement": "Mbit/s",
        "state_class": "measurement",
        "value_template": "{{ value_json.tx_mbps if value_json.tx_mbps is not none else 0 }}",
    },
    {
        "component": "sensor",
        "object_id": "rx_rate",
        "name": "Receive rate",
        "unit_of_measurement": "Mbit/s",
        "state_class": "measurement",
        "value_template": "{{ value_json.rx_mbps if value_json.rx_mbps is not none else 0 }}",
    },
    {
        "component": "sensor",
        "object_id": "health",
        "name": "Link health",
        "value_template": "{{ value_json.health }}",
        "entity_category": "diagnostic",
    },
    {
        "component": "sensor",
        "object_id": "tonemap",
        "name": "Tonemap",
        "value_template": "{{ value_json.tonemap | default('unknown', true) }}",
        "entity_category": "diagnostic",
    },
    {
        "component": "sensor",
        "object_id": "uptime_24h",
        "name": "Link uptime 24h",
        "unit_of_measurement": "%",
        "state_class": "measurement",
        "value_template": "{{ value_json.uptime_24h if value_json.uptime_24h is not none else 0 }}",
    },
    {
        "component": "sensor",
        "object_id": "renegotiations_24h",
        "name": "Renegotiations 24h",
        "state_class": "measurement",
        "value_template": "{{ value_json.renegotiations_24h | default(0) }}",
        "entity_category": "diagnostic",
    },
    {
        "component": "sensor",
        "object_id": "firmware",
        "name": "Firmware",
        "value_template": "{{ value_json.firmware | default('unknown', true) }}",
        "entity_category": "diagnostic",
    },
]

# The state topic carries only what the templates above read. The verbatim
# station table is large, changes every poll and is only useful on the page.
PUBLISHED_FIELDS = (
    "ok",
    "linked",
    "health",
    "station_count",
    "tx_mbps",
    "rx_mbps",
    "tonemap",
    "firmware",
    "local_mac",
    "snid",
    "role",
    "polled_at",
    "changed_at",
    "last_linked_at",
)


class MqttPublisher:
    def __init__(self) -> None:
        self.enabled = bool(MQTT_HOST)
        self.client: mqtt.Client | None = None
        self.available: bool | None = None

    def start(self) -> None:
        if not self.enabled:
            log.info("MQTT disabled (PLC_MQTT_HOST not set)")
            return
        client = mqtt.Client(
            mqtt.CallbackAPIVersion.VERSION2,
            client_id=f"{DEVICE_ID}-{os.getpid()}",
        )
        if MQTT_USER:
            client.username_pw_set(MQTT_USER, MQTT_PASS)
        client.will_set(AVAIL_TOPIC, "offline", retain=True)
        client.on_connect = self._on_connect
        client.connect_async(MQTT_HOST, MQTT_PORT, keepalive=60)
        client.loop_start()
        self.client = client
        log.info("MQTT connecting to %s:%s", MQTT_HOST, MQTT_PORT)

    def _on_connect(self, client, _userdata, _flags, reason_code, _props=None) -> None:
        if reason_code != 0:
            log.error("MQTT connect refused: %s", reason_code)
            return
        log.info("MQTT connected")
        self._publish_discovery(client)
        self.available = None  # force the next publish to state availability

    def _publish_discovery(self, client: mqtt.Client) -> None:
        for entity in ENTITIES:
            component = entity["component"]
            object_id = entity["object_id"]
            payload = {
                k: v for k, v in entity.items() if k not in ("component", "object_id")
            }
            payload.update(
                {
                    "unique_id": f"{DEVICE_ID}_{object_id}",
                    "state_topic": STATE_TOPIC,
                    "availability_topic": AVAIL_TOPIC,
                    "device": DEVICE_BLOCK,
                }
            )
            topic = f"{DISCOVERY_PREFIX}/{component}/{DEVICE_ID}/{object_id}/config"
            client.publish(topic, json.dumps(payload), retain=True)
        log.info("published %d discovery configs", len(ENTITIES))

    def publish(self, status: Status, extra: dict[str, Any] | None = None) -> None:
        if not self.client:
            return
        # Availability tracks whether the adapter can be read, not whether the
        # broker is reachable. A monitor that cannot see the adapter knows
        # nothing, and publishing "link off" there is a false alarm.
        available = bool(status.ok)
        if available != self.available:
            self.client.publish(
                AVAIL_TOPIC, "online" if available else "offline", retain=True
            )
            self.available = available
            if not available:
                log.info("MQTT availability -> offline (adapter unreadable)")

        if not available:
            return
        full = asdict(status)
        payload = {k: full[k] for k in PUBLISHED_FIELDS if k in full}
        payload.update(extra or {})
        self.client.publish(STATE_TOPIC, json.dumps(payload), retain=True)

    def stop(self) -> None:
        if not self.client:
            return
        self.client.publish(AVAIL_TOPIC, "offline", retain=True)
        self.client.loop_stop()
        self.client.disconnect()


MQTT = MqttPublisher()


# --------------------------------------------------------------------------
# Web service
# --------------------------------------------------------------------------


def _nice_ceiling(peak: float) -> int:
    for step in (50, 100, 200, 400, 600, 800, 1000, 1300):
        if step >= peak * 1.15:
            return step
    return MAX_MBPS


async def _refresh_scale(status: Status) -> int:
    """Scale the gauge to rates this link actually reaches.

    An AV1300 pair never negotiates 1300 Mbit/s in a real house, so a fixed
    full scale renders a perfectly healthy link as a sliver of bar.
    """
    peak = await asyncio.to_thread(HISTORY.peak, 24)
    candidates = [p for p in (peak, status.tx_mbps, status.rx_mbps) if p]
    status.scale_mbps = _nice_ceiling(max(candidates)) if candidates else MAX_MBPS
    return status.scale_mbps


async def poller() -> None:
    global STATE
    seeded = False
    next_at = time.monotonic()
    rollup_at = -1e9
    rollup: dict[str, Any] = {}

    while True:
        try:
            status = await asyncio.to_thread(poll_once)

            if not seeded:
                seed = await asyncio.to_thread(HISTORY.seed, status.linked)
                status.last_linked_at = status.last_linked_at or seed["last_linked_at"]
                if seed["changed_at"]:
                    status.changed_at = seed["changed_at"]
                seeded = True

            if status.health != STATE.health:
                log.info(
                    "link %s -> %s (%d station(s))",
                    STATE.health,
                    status.health,
                    status.station_count,
                )

            await asyncio.to_thread(HISTORY.record, status)

            # Rolling 24h figures scan a day of samples, so they are refreshed
            # on their own slow clock rather than once per poll.
            now = time.monotonic()
            if now - rollup_at >= ROLLUP_INTERVAL:
                scale = await _refresh_scale(status)
                rollup = await asyncio.to_thread(HISTORY.summary, 24, 1)
                rollup["scale"] = scale
                rollup_at = now
            status.scale_mbps = rollup.get("scale", status.scale_mbps)

            STATE = status
            MQTT.publish(
                status,
                {
                    "uptime_24h": rollup.get("uptime_pct"),
                    "renegotiations_24h": rollup.get("renegotiations", 0),
                },
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            # A dead poller is the one failure this service must not have: it
            # would keep serving a stale reading as though it were current.
            log.exception("poll cycle failed, continuing")

        # Sleep to a deadline so the cadence does not drift by however long
        # the adapter took to answer.
        next_at += POLL_INTERVAL
        delay = next_at - time.monotonic()
        if delay < 0:
            next_at = time.monotonic()
            delay = 0
        await asyncio.sleep(delay)


@contextlib.asynccontextmanager
async def lifespan(_app: FastAPI):
    HISTORY.open()
    MQTT.start()
    task = asyncio.create_task(poller())
    try:
        yield
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
        MQTT.stop()
        HISTORY.close()


app = FastAPI(title="Powerline monitor", version=VERSION, lifespan=lifespan)


@app.get("/", response_class=HTMLResponse)
async def index() -> str:
    return (HERE / "index.html").read_text(encoding="utf-8")


@app.get("/api/status")
async def api_status() -> JSONResponse:
    payload = asdict(STATE)
    payload["stale"] = STATE.stale
    payload["now"] = time.time()
    return JSONResponse(payload)


@app.get("/api/history")
async def api_history(hours: float = 6, buckets: int = 240) -> JSONResponse:
    hours = min(max(hours, 0.25), RETENTION_DAYS * 24)
    buckets = min(max(buckets, 10), 1000)
    return JSONResponse(await asyncio.to_thread(HISTORY.series, hours, buckets))


@app.get("/api/outages")
async def api_outages(hours: float = 24, limit: int = 50) -> JSONResponse:
    hours = min(max(hours, 0.25), RETENTION_DAYS * 24)
    limit = min(max(limit, 1), 200)
    return JSONResponse(await asyncio.to_thread(HISTORY.summary, hours, limit))


@app.get("/healthz")
async def healthz() -> JSONResponse:
    # Being unable to read the adapter is a fault. So is not having read it
    # recently: a poller that died silently must not look healthy.
    stale = STATE.stale
    ok = STATE.ok and not stale
    return JSONResponse(
        {
            "ok": ok,
            "adapter_ok": STATE.ok,
            "stale": stale,
            "polled_at": STATE.polled_at,
            "error": STATE.error,
            "history_ok": STATE.history_ok,
            "db_path": STATE.db_path,
            "version": VERSION,
        },
        status_code=200 if ok else 503,
    )


def main() -> None:
    logging.basicConfig(
        level=os.getenv("PLC_LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    log.info(
        "v%s interface=%s plctool=%s interval=%ss expect=%d station(s) db=%s",
        VERSION,
        IFACE,
        PLCTOOL,
        POLL_INTERVAL,
        EXPECTED_STATIONS,
        DB_PATH,
    )
    uvicorn.run(app, host=HTTP_HOST, port=HTTP_PORT, access_log=False)


if __name__ == "__main__":
    main()
