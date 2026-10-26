"""``internet-monitor``: broadband outages, latency and speed, with a monthly report.

Three commands, all standard library only:

``internet-monitor watch``
    Runs continuously. Every ``INTERNET_CHECK_S`` seconds it opens a TCP connection to each of
    ``INTERNET_TARGETS``; the line is up if any of them answers. Two failed checks in a row start
    an outage. When the line comes back, one alert says how long it was down and whether the router
    (``INTERNET_GATEWAY``) answered meanwhile, which tells a broadband fault from a home-network
    one. (Nothing can be sent while the internet is down, so the alert comes afterwards.)

``internet-monitor speedtest``
    A download and upload test against Cloudflare's speed-test servers (``speed.cloudflare.com``),
    run from a timer a few times a day. Each test transfers roughly 10 s of data in each direction.
    Three tests in a row under ``INTERNET_MIN_DOWN_MBPS`` raise an alert.

``internet-monitor report [--month YYYY-MM]``
    A summary of a month (default: the last one): outages and their total length, the median and
    slowest download and upload speeds, and how many tests fell below the minimum. The full list
    of outages and tests is written next to the state files as ``report-YYYY-MM.txt``, ready to
    send to your provider.

Settings (environment): see ``docs/internet-monitor.md``.
"""

from __future__ import annotations

import argparse
import json
import logging
import socket
import statistics
import time
import urllib.request
from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path

from homelab_tools.common.config import Outputs, env_float, env_int, env_str
from homelab_tools.common.metrics import Sample, VictoriaMetrics
from homelab_tools.common.notify import Notifier
from homelab_tools.common.status import write_status

log = logging.getLogger("internet-monitor")

DAY = 86400.0
FAILS_FOR_OUTAGE = 2
KEEP_DAYS = 400
SPEED_HOST = "speed.cloudflare.com"
USER_AGENT = "homelab-tools internet-monitor"
SLOW_TESTS = 3  # consecutive tests under the minimum before alerting

Alert = tuple[str, str, str]  # title, message, priority


def _clock(ts: float) -> str:
    return datetime.fromtimestamp(ts).strftime("%H:%M")


def _when(ts: float) -> str:
    return datetime.fromtimestamp(ts).strftime("%-d %b %H:%M")


def duration_text(seconds: float) -> str:
    """``"45 s"``, ``"7 min"``, ``"2 h 5 min"``."""
    if seconds < 60:
        return f"{seconds:.0f} s"
    minutes = round(seconds / 60)
    if minutes < 60:
        return f"{minutes} min"
    hours, minutes = divmod(minutes, 60)
    return f"{hours} h {minutes} min" if minutes else f"{hours} h"


# --- probing -------------------------------------------------------------------------------------


def parse_target(target: str, default_port: int = 443) -> tuple[str, int]:
    """``"1.1.1.1:443"`` -> ``("1.1.1.1", 443)``."""
    host, sep, port = target.rpartition(":")
    return (host, int(port)) if sep and port.isdigit() else (target, default_port)


def tcp_ms(target: str, timeout: float = 3.0) -> float | None:
    """Milliseconds to open a TCP connection, or ``None`` if it fails."""
    host, port = parse_target(target)
    started = time.monotonic()
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return (time.monotonic() - started) * 1000
    except OSError:
        return None


Probe = Callable[[str], float | None]


def check_line(targets: Sequence[str], probe: Probe = tcp_ms) -> float | None:
    """Fastest answer among ``targets`` (ms), or ``None`` if none answered."""
    times = [ms for ms in (probe(t) for t in targets) if ms is not None]
    return min(times) if times else None


# --- outages -------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Outage:
    """A period with no internet. ``router_ok`` is False if the router stopped answering too."""

    start: float
    end: float
    router_ok: bool | None

    @property
    def seconds(self) -> float:
        """Length of the outage."""
        return self.end - self.start

    @property
    def cause(self) -> str:
        """Where the fault probably was."""
        if self.router_ok is None:
            return "unknown"
        return "broadband" if self.router_ok else "home network"


@dataclass
class LineState:
    """Kept by ``watch``.

    Attributes:
        last_check: When the line was last checked.
        latency_ms: Its latency then (``None`` = no answer).
        first_fail: Time of the first failure in the current run of failures.
        fails: Consecutive failed checks.
        router_down: Whether the router failed to answer during the current outage.
        outages: Finished outages, oldest first.
    """

    last_check: float = 0.0
    latency_ms: float | None = None
    first_fail: float | None = None
    fails: int = 0
    router_down: bool | None = None
    outages: list[Outage] = field(default_factory=list)

    @property
    def down_since(self) -> float | None:
        """Start of the current outage, if the line is down."""
        return self.first_fail if self.fails >= FAILS_FOR_OUTAGE else None

    @classmethod
    def load(cls, path: Path) -> LineState:
        """Read the state file, or start fresh."""
        try:
            data = json.loads(path.read_text())
            data["outages"] = [Outage(*o) for o in data.get("outages", [])]
            return cls(**data)
        except (OSError, ValueError, TypeError):
            return cls()

    def save(self, path: Path) -> None:
        """Write the state file atomically."""
        data = self.__dict__ | {"outages": [[o.start, o.end, o.router_ok] for o in self.outages]}
        _save(path, data)


def _save(path: Path, data: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data))
    tmp.replace(path)


def step(
    state: LineState, now: float, latency_ms: float | None, router_ok: bool | None, gap_s: float
) -> list[Alert]:
    """Record one check; returns an alert when an outage ends.

    Args:
        state: Updated in place.
        now: Time of the check.
        latency_ms: Result of :func:`check_line`.
        router_ok: Whether the router answered (``None`` if no router is configured).
        gap_s: Longest believable gap between checks. After a longer one (the monitor was stopped)
            a failure run in progress is forgotten, as nobody knows what happened meanwhile.
    """
    if state.last_check and now - state.last_check > gap_s:
        state.first_fail, state.fails, state.router_down = None, 0, None
    state.last_check, state.latency_ms = now, latency_ms
    alerts: list[Alert] = []
    if latency_ms is None:
        if state.fails == 0:
            state.first_fail = now
        state.fails += 1
        if router_ok is not None:
            state.router_down = bool(state.router_down) or not router_ok
        return alerts
    start = state.down_since
    if start is not None:
        router = None if state.router_down is None else not state.router_down
        outage = Outage(start, now, router)
        state.outages.append(outage)
        alerts.append(outage_alert(outage))
    state.first_fail, state.fails, state.router_down = None, 0, None
    state.outages = [o for o in state.outages if now - o.end <= KEEP_DAYS * DAY]
    return alerts


def outage_alert(outage: Outage) -> Alert:
    """The "internet is back" notification."""
    span = f"{duration_text(outage.seconds)} ({_clock(outage.start)}-{_clock(outage.end)})"
    reason = {
        "broadband": "The router stayed reachable, so the fault was on the broadband line or at the "
        "provider.",
        "home network": "The router stopped answering too: a router restart, power cut or home-network "
        "fault rather than the broadband line.",
        "unknown": "",
    }[outage.cause]
    return ("Internet back", f"The internet was down for {span}. {reason}".strip(), "default")


# --- speed tests ---------------------------------------------------------------------------------

#: Moves ``nbytes`` one way (download or upload) and returns when done.
Transfer = Callable[[int], None]


def run_streams(
    transfer: Transfer,
    chunk: int,
    streams: int,
    seconds: float,
    clock: Callable[[], float] = time.monotonic,
) -> float:
    """Mbps achieved by ``streams`` parallel workers repeating ``transfer(chunk)`` for ``seconds``.

    Each worker does one untimed warm-up transfer first (TCP slow start would otherwise drag the
    figure down), then transfers until the time is up.
    """

    def worker(deadline: float) -> int:
        moved = 0
        while clock() < deadline:
            transfer(chunk)
            moved += chunk
        return moved

    with ThreadPoolExecutor(streams) as pool:
        list(pool.map(lambda _: transfer(chunk // 4), range(streams)))
        started = clock()
        moved = sum(pool.map(worker, [started + seconds] * streams))
        elapsed = clock() - started
    return moved * 8 / elapsed / 1e6 if elapsed > 0 else 0.0


def cloudflare_download(nbytes: int) -> None:
    """Download ``nbytes`` from Cloudflare's speed-test server, discarding them."""
    request = urllib.request.Request(
        f"https://{SPEED_HOST}/__down?bytes={nbytes}", headers={"User-Agent": USER_AGENT}
    )
    with urllib.request.urlopen(request, timeout=60) as resp:
        while resp.read(1 << 16):
            pass


def cloudflare_upload(nbytes: int) -> None:
    """Upload ``nbytes`` of zeros to Cloudflare's speed-test server."""
    request = urllib.request.Request(
        f"https://{SPEED_HOST}/__up",
        data=bytes(nbytes),
        headers={"User-Agent": USER_AGENT, "Content-Type": "application/octet-stream"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=60) as resp:
        resp.read()


@dataclass(frozen=True)
class SpeedTest:
    """One speed test (Mbps and ms)."""

    time: float
    download: float
    upload: float
    ping: float | None


def speed_test(
    now: float,
    download: Transfer = cloudflare_download,
    upload: Transfer = cloudflare_upload,
    probe: Probe = tcp_ms,
    seconds: float = 10.0,
) -> SpeedTest:
    """Measure ping (best of five connections), then download and upload."""
    pings = [ms for ms in (probe(f"{SPEED_HOST}:443") for _ in range(5)) if ms is not None]
    down = run_streams(download, 25_000_000, 4, seconds)
    up = run_streams(upload, 5_000_000, 4, seconds)
    return SpeedTest(now, round(down, 1), round(up, 1), round(min(pings), 1) if pings else None)


@dataclass
class SpeedState:
    """Kept by ``speedtest``: past results and whether a slow-line alert is open."""

    tests: list[SpeedTest] = field(default_factory=list)
    slow_alerted: bool = False

    @classmethod
    def load(cls, path: Path) -> SpeedState:
        """Read the state file, or start fresh."""
        try:
            data = json.loads(path.read_text())
            return cls([SpeedTest(*t) for t in data.get("tests", [])], data.get("slow_alerted", False))
        except (OSError, ValueError, TypeError):
            return cls()

    def save(self, path: Path) -> None:
        """Write the state file atomically."""
        tests = [[t.time, t.download, t.upload, t.ping] for t in self.tests]
        _save(path, {"tests": tests, "slow_alerted": self.slow_alerted})


def record_test(state: SpeedState, test: SpeedTest, min_down: float) -> list[Alert]:
    """Store a result; alert once after :data:`SLOW_TESTS` slow tests in a row, and when it recovers."""
    state.tests = [t for t in state.tests if test.time - t.time <= KEEP_DAYS * DAY] + [test]
    if not min_down:
        return []
    recent = state.tests[-SLOW_TESTS:]
    if len(recent) == SLOW_TESTS and all(t.download < min_down for t in recent) and not state.slow_alerted:
        state.slow_alerted = True
        speeds = ", ".join(f"{t.download:.0f}" for t in recent)
        return [
            (
                "Broadband slow",
                f"The last {SLOW_TESTS} speed tests were all under your {min_down:.0f} Mbps minimum "
                f"({speeds} Mbps). If it continues, report it to your provider - the monthly report "
                "lists every test.",
                "high",
            )
        ]
    if state.slow_alerted and test.download >= min_down:
        state.slow_alerted = False
        return [("Broadband OK again", f"Download is back to {test.download:.0f} Mbps.", "low")]
    return []


# --- status and report ---------------------------------------------------------------------------


def _median(values: Sequence[float]) -> float | None:
    return statistics.median(values) if values else None


def display(line: LineState, speeds: SpeedState, now: float) -> dict[str, str]:
    """Status fields for ``internet.json`` (see ``docs/internet-monitor.md``)."""
    month = [o for o in line.outages if now - o.end <= 30 * DAY]
    last = line.outages[-1] if line.outages else None
    tests = [t for t in speeds.tests if now - t.time <= 30 * DAY]
    latest = speeds.tests[-1] if speeds.tests else None
    down = line.down_since

    def mbps(v: float | None) -> str:
        return f"{v:.0f} Mbps" if v is not None else "-"

    if not line.last_check:
        status = "-"
    elif down is not None:
        status = f"down since {_clock(down)}"
    else:
        status = "online"
    return {
        "status": status,
        "latency": f"{line.latency_ms:.0f} ms" if line.latency_ms is not None else "-",
        "outages_30d": f"{len(month)}, {duration_text(sum(o.seconds for o in month))} in total"
        if month
        else "none",
        "last_outage": f"{_when(last.start)}, {duration_text(last.seconds)}" if last else "none",
        "download": mbps(latest.download if latest else None),
        "upload": mbps(latest.upload if latest else None),
        "download_30d": mbps(_median([t.download for t in tests])),
        "upload_30d": mbps(_median([t.upload for t in tests])),
        "tested": _when(latest.time) if latest else "-",
    }


def month_bounds(month: date) -> tuple[float, float]:
    """Epoch seconds of local midnight at the start of ``month`` and of the next month."""
    start = datetime(month.year, month.month, 1).astimezone()
    nxt = datetime(month.year + month.month // 12, month.month % 12 + 1, 1).astimezone()
    return start.timestamp(), nxt.timestamp()


def monthly_report(line: LineState, speeds: SpeedState, month: date, min_down: float) -> tuple[str, str, str]:
    """(title, message, full text) for one month."""
    start, end = month_bounds(month)
    outages = [o for o in line.outages if start <= o.start < end]
    tests = [t for t in speeds.tests if start <= t.time < end]
    name = f"{month:%B}"
    lines = []
    if outages:
        longest = max(outages, key=lambda o: o.seconds)
        plural = "s" if len(outages) != 1 else ""
        lines.append(
            f"{len(outages)} outage{plural}, {duration_text(sum(o.seconds for o in outages))} in total "
            f"(longest {duration_text(longest.seconds)} on {_when(longest.start)}, {longest.cause})."
        )
        broadband = [o for o in outages if o.cause == "broadband"]
        if broadband and len(broadband) != len(outages):
            lines.append(f"Faults on the broadband line itself: {len(broadband)} of {len(outages)}.")
    else:
        lines.append("No outages.")
    if tests:
        downs = sorted(t.download for t in tests)
        ups = sorted(t.upload for t in tests)
        slowest = downs[max(0, len(downs) // 10 - 1)] if len(downs) >= 10 else downs[0]
        lines.append(
            f"Download median {_median(downs):.0f} Mbps (slowest {slowest:.0f}), upload median "
            f"{_median(ups):.0f} Mbps, from {len(tests)} tests."
        )
        if min_down:
            under = sum(1 for d in downs if d < min_down)
            lines.append(f"{under} of {len(tests)} tests were under your {min_down:.0f} Mbps minimum.")
    else:
        lines.append("No speed tests were recorded.")
    detail = [f"Broadband report for {month:%B %Y}", "", *lines, "", "Outages (start, end, length, cause):"]
    detail += [
        f"  {_when(o.start)}  {_when(o.end)}  {duration_text(o.seconds)}  {o.cause}" for o in outages
    ] or ["  none"]
    detail += ["", "Speed tests (time, download Mbps, upload Mbps, ping ms):"]
    detail += [
        f"  {_when(t.time)}  {t.download:.1f}  {t.upload:.1f}  {t.ping if t.ping is not None else '-'}"
        for t in tests
    ] or ["  none"]
    return f"Broadband in {name}", " ".join(lines), "\n".join(detail) + "\n"


# --- commands ------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Settings:
    """Settings shared by the commands."""

    state_dir: Path
    targets: tuple[str, ...]
    gateway: str
    check_s: int
    min_down: float

    @classmethod
    def from_env(cls) -> Settings:
        """Read from the environment."""
        return cls(
            state_dir=Path(env_str("INTERNET_STATE_DIR", "/var/lib/internet-monitor")),
            targets=tuple(env_str("INTERNET_TARGETS", "1.1.1.1:443 8.8.8.8:443 9.9.9.9:443").split()),
            gateway=env_str("INTERNET_GATEWAY", ""),
            check_s=env_int("INTERNET_CHECK_S", 30),
            min_down=env_float("INTERNET_MIN_DOWN_MBPS", 0.0),
        )

    @property
    def line_file(self) -> Path:
        """``watch``'s state."""
        return self.state_dir / "line.json"

    @property
    def speed_file(self) -> Path:
        """``speedtest``'s state."""
        return self.state_dir / "speeds.json"


def publish_status(outputs: Outputs, settings: Settings, now: float) -> None:
    """Write ``internet.json`` from both state files (each command owns one)."""
    status = display(LineState.load(settings.line_file), SpeedState.load(settings.speed_file), now)
    write_status(outputs.status_dir, "internet", status | {"checked": _clock(now)})


def send(notifier: Notifier, alerts: Sequence[Alert]) -> None:
    """Log and send alerts."""
    for title, message, priority in alerts:
        log.info("%s: %s", title, message)
        notifier.send(title, message, priority=priority, tags="globe_with_meridians")


def watch(outputs: Outputs, settings: Settings) -> None:
    """Check the line forever."""
    notifier, vm = Notifier(outputs.ntfy_url), VictoriaMetrics(outputs.vm_url)
    state = LineState.load(settings.line_file)
    log.info("checking %s every %d s", " ".join(settings.targets), settings.check_s)
    while True:
        now = time.time()
        latency = check_line(settings.targets)
        router = tcp_ms(settings.gateway, 2.0) is not None if settings.gateway else None
        alerts = step(state, now, latency, router, gap_s=settings.check_s * 4)
        state.save(settings.line_file)
        send(notifier, alerts)
        vm.write(
            [Sample("internet", {"up": latency is not None, "latency_ms": latency, "router_up": router})]
        )
        if alerts:  # an outage just ended
            vm.write([Sample("internet", {"outage_s": state.outages[-1].seconds})])
        publish_status(outputs, settings, now)
        time.sleep(max(1.0, settings.check_s - (time.time() - now)))


def speedtest(outputs: Outputs, settings: Settings) -> None:
    """Run one speed test."""
    now = time.time()
    state = SpeedState.load(settings.speed_file)
    try:
        test = speed_test(now)
    except OSError as exc:
        log.warning("speed test failed: %s", exc)
        return
    log.info("download %.1f Mbps, upload %.1f Mbps, ping %s ms", test.download, test.upload, test.ping)
    alerts = record_test(state, test, settings.min_down)
    state.save(settings.speed_file)
    send(Notifier(outputs.ntfy_url), alerts)
    VictoriaMetrics(outputs.vm_url).write(
        [
            Sample(
                "internet", {"download_mbps": test.download, "upload_mbps": test.upload, "ping_ms": test.ping}
            )
        ]
    )
    publish_status(outputs, settings, now)


def report(outputs: Outputs, settings: Settings, month: date | None, dry_run: bool) -> None:
    """Send the monthly summary and write the detailed report file."""
    if month is None:
        first = datetime.now().astimezone().date().replace(day=1)
        month = (first - timedelta(days=1)).replace(day=1)
    title, message, detail = monthly_report(
        LineState.load(settings.line_file), SpeedState.load(settings.speed_file), month, settings.min_down
    )
    if dry_run:
        print(f"{title}\n{message}\n\n{detail}")
        return
    path = settings.state_dir / f"report-{month:%Y-%m}.txt"
    path.write_text(detail)
    Notifier(outputs.ntfy_url).send(title, f"{message} Full list: {path}", tags="bar_chart")
    log.info("%s: %s", title, message)


def main() -> None:
    """Entry point."""
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    parser = argparse.ArgumentParser(description="Broadband outages, latency and speed.")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("watch", help="check the line continuously")
    sub.add_parser("speedtest", help="run one speed test")
    rep = sub.add_parser("report", help="monthly summary")
    rep.add_argument("--month", type=lambda s: date.fromisoformat(f"{s}-01"), help="YYYY-MM")
    rep.add_argument("--dry-run", action="store_true", help="print instead of sending")
    args = parser.parse_args()
    outputs, settings = Outputs.from_env(), Settings.from_env()
    if args.command == "watch":
        watch(outputs, settings)
    elif args.command == "speedtest":
        speedtest(outputs, settings)
    else:
        report(outputs, settings, args.month, args.dry_run)


if __name__ == "__main__":
    main()
