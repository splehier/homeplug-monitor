#!/usr/bin/env python3
"""plcwatch - live view of a HomePlug AV link.

Polls plctool and draws rolling sparklines for the TX and RX PHY rates,
flagging renegotiations and dropouts as they happen. Stdlib only.

    sudo ./plcwatch.py -i enp3s0
    sudo ./plcwatch.py -i enp3s0 --once          # one line, for scripts
    sudo ./plcwatch.py -i enp3s0 --log ~/plc.csv # also append CSV

Keys: q quit, +/- poll interval, c clear history.

Parsing lives in plc.py, shared with the service, so both agree about what a
given firmware's output means.
"""

from __future__ import annotations

import argparse
import curses
import sys
import time
from collections import deque

import plc

SPARK = "▁▂▃▄▅▆▇█"

# --once exit codes, so this is usable from cron or a HA command_line sensor.
EXIT_LINKED = 0
EXIT_ERROR = 1
EXIT_NO_PARTNER = 2


def spark(values: list[float | None], width: int, ceiling: float) -> str:
    """Sparkline. None means "no rate reported", which is not the same as 0."""
    out = []
    for v in values[-width:]:
        if v is None:
            out.append(" ")
        elif v <= 0:
            out.append("·")
        else:
            idx = min(len(SPARK) - 1, int(v / max(ceiling, 1) * (len(SPARK) - 1)))
            out.append(SPARK[idx])
    return "".join(out)


class Watcher:
    def __init__(self, args: argparse.Namespace) -> None:
        self.args = args
        self.interval = args.interval
        self.tx: deque[float | None] = deque(maxlen=600)
        self.rx: deque[float | None] = deque(maxlen=600)
        self.events: deque[str] = deque(maxlen=12)
        self.last: plc.Reading | None = None
        self.last_key: tuple | None = None
        self.renegotiations = 0
        self.dropouts = 0
        self.started = time.time()
        self.logfile = open(args.log, "a", buffering=1) if args.log else None
        if self.logfile and self.logfile.tell() == 0:
            self.logfile.write(
                "timestamp,ok,stations,tx_mbps,rx_mbps,tonemap,snid,role\n"
            )

    def note(self, text: str) -> None:
        self.events.appendleft(f"{time.strftime('%H:%M:%S')}  {text}")

    def poll(self) -> None:
        r = plc.read(self.args.plctool, self.args.iface, self.args.timeout, time.time())
        previous = self.last

        if r.ok:
            self.tx.append(r.tx)
            self.rx.append(r.rx)
            if previous is not None and previous.ok:
                if r.net_key != self.last_key and self.last_key is not None:
                    self.renegotiations += 1
                    self.note(f"network renegotiated (snid {previous.snid} -> {r.snid})")
                if previous.station_count and not r.station_count:
                    self.dropouts += 1
                    self.note("partner adapter lost")
                elif not previous.station_count and r.station_count:
                    self.note("partner adapter joined")
                if (previous.rx or 0) > 0 and (r.rx or 0) == 0:
                    self.note("receive rate fell to zero")
                if previous.role != r.role:
                    self.note(f"role changed to {r.role}")
                if previous.tonemap != r.tonemap and r.tonemap:
                    self.note(f"tonemap now {r.tonemap}")
            self.last_key = r.net_key
        elif previous is None or previous.ok:
            self.note(f"read failed: {r.error}")

        self.last = r
        if self.logfile:
            self.logfile.write(
                f"{r.at:.0f},{int(r.ok)},{r.station_count},"
                f"{'' if r.tx is None else r.tx},{'' if r.rx is None else r.rx},"
                f"{r.tonemap},{r.snid or ''},{r.role or ''}\n"
            )

    # -- drawing ---------------------------------------------------------

    def colour(self, value: float | None) -> int:
        if value is None or value <= 0:
            return curses.color_pair(3)
        if value < self.args.marginal:
            return curses.color_pair(2)
        return curses.color_pair(1)

    def draw(self, scr: "curses._CursesWindow") -> None:
        scr.erase()
        height, width = scr.getmaxyx()
        r = self.last
        row = 0

        def put(text: str, attr: int = 0, indent: int = 0) -> None:
            nonlocal row
            if row < height - 1:
                scr.addnstr(row, indent, text, max(0, width - 1 - indent), attr)
            row += 1

        title = f" powerline  {self.args.iface} "
        put(title + "─" * max(0, width - len(title) - 1), curses.A_BOLD)

        if not r:
            put("  reading…")
            scr.refresh()
            return

        if not r.ok:
            put("")
            put(f"  cannot read adapter: {r.error}", curses.color_pair(3) | curses.A_BOLD)
            put("")
        else:
            worst = r.worst
            if not r.station_count:
                state = "no partner"
            elif worst is not None and worst < self.args.marginal:
                state = "weak link"
            else:
                state = "linked"
            put("")
            put(f"  {state}", self.colour(worst) | curses.A_BOLD)
            put(
                f"  {r.local_mac or '?'}  role {r.role or '?'}  snid {r.snid or '?'}"
                f"  partners {r.station_count}",
                curses.A_DIM,
            )
            for s in r.stations:
                put(
                    f"  peer {s.mac or '?'}  behind it {s.bda or '?'}"
                    f"  tei {s.tei or '?'}",
                    curses.A_DIM,
                )
            put("")

            graph_width = max(10, width - 26)
            for label, series, value, tonemap in (
                ("tx", list(self.tx), r.tx, r.tonemap),
                ("rx", list(self.rx), r.rx, r.tonemap),
            ):
                bar = spark(series, graph_width, self.args.ceiling)
                head = f"  {label} {0 if value is None else round(value):>4} Mbit/s "
                scr.addnstr(row, 0, head, width - 1, self.colour(value) | curses.A_BOLD)
                scr.addnstr(row, len(head), bar, max(0, width - len(head) - 12))
                if tonemap:
                    tail = f" {tonemap}"
                    if len(head) + len(bar) + len(tail) < width - 1:
                        attr = curses.color_pair(2) if tonemap == "alternate" else curses.A_DIM
                        scr.addnstr(row, len(head) + len(bar), tail, 12, attr)
                row += 1

            seen = [v for v in self.rx if v is not None]
            sent = [v for v in self.tx if v is not None]
            if seen and sent:
                put(
                    f"  rx min {round(min(seen))} max {round(max(seen))}"
                    f"   tx min {round(min(sent))} max {round(max(sent))}",
                    curses.A_DIM,
                )
            put("")

        mins = (time.time() - self.started) / 60
        put(
            f"  watching {mins:.0f} min   renegotiations {self.renegotiations}"
            f"   dropouts {self.dropouts}",
            curses.A_DIM,
        )
        put("")

        if self.events:
            put("  events", curses.A_BOLD)
            for line in list(self.events)[: max(0, height - row - 2)]:
                put("  " + line, curses.A_DIM)

        footer = f" q quit   +/- interval ({self.interval}s)   c clear "
        if height > 1:
            scr.addnstr(height - 1, 0, footer.ljust(width - 1), width - 1, curses.A_REVERSE)
        scr.refresh()

    def loop(self, scr: "curses._CursesWindow") -> None:
        curses.curs_set(0)
        curses.use_default_colors()
        curses.init_pair(1, curses.COLOR_GREEN, -1)
        curses.init_pair(2, curses.COLOR_YELLOW, -1)
        curses.init_pair(3, curses.COLOR_RED, -1)
        scr.nodelay(True)

        next_poll = 0.0
        last_draw = 0.0
        while True:
            now = time.time()
            if now >= next_poll:
                self.poll()
                next_poll = now + self.interval
                self.draw(scr)
                last_draw = now
            elif now - last_draw >= 1:
                self.draw(scr)  # keep the elapsed counter moving between polls
                last_draw = now

            ch = scr.getch()
            if ch in (ord("q"), 27):
                return
            if ch in (ord("+"), ord("=")):
                self.interval = min(300, self.interval + 5)
                self.draw(scr)
            elif ch == ord("-"):
                self.interval = max(2, self.interval - 5)
                self.draw(scr)
            elif ch == ord("c"):
                self.tx.clear()
                self.rx.clear()
                self.events.clear()
                self.renegotiations = self.dropouts = 0
                self.started = time.time()
                self.draw(scr)
            elif ch == curses.KEY_RESIZE:
                self.draw(scr)
            time.sleep(0.1)


def main() -> int:
    p = argparse.ArgumentParser(description="live view of a HomePlug AV link")
    p.add_argument("-i", "--iface", default="enp3s0", help="interface (default enp3s0)")
    p.add_argument("-n", "--interval", type=int, default=15, help="seconds between polls")
    p.add_argument("--timeout", type=int, default=1000, help="plctool read timeout in ms")
    p.add_argument("--plctool", default="plctool", help="path to plctool")
    p.add_argument("--marginal", type=float, default=50, help="Mbit/s below which a link is weak")
    p.add_argument("--ceiling", type=float, default=200, help="sparkline full scale in Mbit/s")
    p.add_argument("--log", help="append samples to this CSV file")
    p.add_argument("--once", action="store_true", help="print one line and exit")
    args = p.parse_args()

    if args.once:
        r = plc.read(args.plctool, args.iface, args.timeout, time.time(), with_version=False)
        if not r.ok:
            print(f"error: {r.error}", file=sys.stderr)
            return EXIT_ERROR
        print(
            f"stations={r.station_count} "
            f"tx={0 if r.tx is None else round(r.tx)} "
            f"rx={0 if r.rx is None else round(r.rx)} "
            f"tonemap={r.tonemap or '-'} snid={r.snid} role={r.role}"
        )
        return EXIT_LINKED if r.station_count else EXIT_NO_PARTNER

    watcher = Watcher(args)
    try:
        curses.wrapper(watcher.loop)
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
