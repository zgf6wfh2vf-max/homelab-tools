"""``opencanary-ntfy``: follow OpenCanary's JSON log and send phone alerts.

Nothing legitimate talks to a honeypot, so the *first* event from a source alerts immediately (urgent
priority). A port scan then produces hundreds more events; those are batched into a single "still
active" summary per source every ``CANARY_WINDOW`` seconds, so a scan costs two notifications rather
than hundreds.

Settings (environment):

``CANARY_LOG``     OpenCanary's log file (the ``file`` handler in opencanary.conf); required.
``CANARY_NAME``    How to describe the honeypot in alerts, e.g. ``fake NAS nas01 (192.168.1.240)``.
``CANARY_WINDOW``  Seconds to batch follow-up events per source, default 900.
"""

from __future__ import annotations

import json
import logging
import os
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, TextIO

from homelab_tools.common.config import Outputs, env_int, env_str
from homelab_tools.common.notify import Notifier

log = logging.getLogger("opencanary-ntfy")

#: OpenCanary log types -> readable names. Types below 2000 are the canary's own start/status messages.
LOG_TYPES = {
    2000: "FTP login",
    3000: "HTTP request",
    3001: "HTTP login attempt",
    4000: "SSH connection",
    4001: "SSH client version",
    4002: "SSH login attempt",
    5001: "port scan",
    6001: "Telnet login attempt",
    8001: "MySQL login attempt",
    12001: "VNC login attempt",
    14001: "RDP connection",
    16001: "Git clone",
    17001: "Redis command",
    18001: "TCP banner",
}

#: ``logdata`` keys worth showing, in order.
DETAIL_KEYS = ("USERNAME", "PASSWORD", "PATH", "USERAGENT", "CMD", "REPO", "REMOTEVERSION")


def describe(event: dict[str, Any]) -> str:
    """One-line description, e.g. ``"SSH login attempt on port 22 (username=root, password=admin)"``."""
    kind = LOG_TYPES.get(event.get("logtype", 0), f"event {event.get('logtype')}")
    data = event.get("logdata") or {}
    details = [f"{k.lower()}={str(data[k])[:60]}" for k in DETAIL_KEYS if data.get(k)]
    return f"{kind} on port {event.get('dst_port')}" + (f" ({', '.join(details)})" if details else "")


def parse_event(line: str) -> dict[str, Any] | None:
    """Parse a log line; ``None`` for malformed lines and the canary's own (logtype < 2000) messages."""
    try:
        event = json.loads(line)
    except ValueError:
        return None
    if not isinstance(event, dict) or not event.get("src_host") or event.get("logtype", 0) < 2000:
        return None
    return event


@dataclass(frozen=True)
class Alert:
    """A notification to send."""

    title: str
    message: str
    priority: str


@dataclass
class Batcher:
    """First event per source alerts at once; later ones are summarised per window."""

    honeypot: str
    window: float = 900
    pending: dict[str, tuple[float, list[dict[str, Any]]]] = field(default_factory=dict)

    def event(self, event: dict[str, Any], now: float) -> Alert | None:
        """Register an event; returns an immediate alert for the first event from its source."""
        src = event["src_host"]
        if src in self.pending:
            self.pending[src][1].append(event)
            return None
        self.pending[src] = (now, [])
        return Alert(
            f"Honeypot tripped by {src}",
            f"{src} touched the honeypot ({self.honeypot}): {describe(event)}. "
            "Nothing legitimate connects to it - find out what that device is.",
            "urgent",
        )

    def flush(self, now: float) -> list[Alert]:
        """Summaries for sources whose window has ended (sources with no follow-ups are dropped quietly)."""
        alerts = []
        for src, (first, events) in list(self.pending.items()):
            if now - first < self.window:
                continue
            del self.pending[src]
            if events:
                ports = sorted({e.get("dst_port") for e in events}, key=str)
                kinds = "; ".join(sorted({describe(e) for e in events})[:6])
                alerts.append(
                    Alert(
                        f"Honeypot: {src} still active",
                        f"{len(events)} more events in {int(self.window // 60)} min, ports {ports}: {kinds}",
                        "high",
                    )
                )
        return alerts


def follow(path: str, sleep: Callable[[float], None] = time.sleep) -> Iterator[str | None]:
    """Yield new lines appended to ``path``, forever.

    Yields ``None`` whenever there is nothing new (callers use these ticks for timed work). Survives
    log rotation (the file is replaced) and truncation (``copytruncate``), and waits for the file if
    it does not exist yet. Starts at the end of the file: old events are not replayed.
    """
    handle: TextIO | None = None
    inode = -1
    while True:
        if handle is None:
            try:
                handle = open(path)  # noqa: SIM115 - kept open across iterations on purpose
                inode = os.fstat(handle.fileno()).st_ino
                handle.seek(0, os.SEEK_END)
            except OSError:
                yield None
                sleep(5)
                continue
        line = handle.readline()
        if line:
            yield line
            continue
        try:
            stat = Path(path).stat()
            reopen = stat.st_ino != inode or stat.st_size < handle.tell()
        except OSError:
            reopen = True
        if reopen:
            handle.close()
            handle = None
            continue
        yield None
        sleep(1)


def main() -> None:
    """Entry point: follow the log forever."""
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    path = env_str("CANARY_LOG")
    notifier = Notifier(Outputs.from_env().ntfy_url)
    batcher = Batcher(env_str("CANARY_NAME", "honeypot"), env_int("CANARY_WINDOW", 900))
    log.info("watching %s", path)
    tags = "rotating_light,honey_pot"
    for line in follow(path):
        now = time.time()
        alerts = batcher.flush(now)
        event = parse_event(line) if line else None
        if event is not None:
            log.info("event: %s %s", event["src_host"], describe(event))
            first = batcher.event(event, now)
            if first:
                alerts.append(first)
        for alert in alerts:
            notifier.send(alert.title, alert.message, priority=alert.priority, tags=tags)


if __name__ == "__main__":
    main()
