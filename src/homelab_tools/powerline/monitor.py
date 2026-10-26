"""``powerline-monitor``: link speed and availability of devolo Magic powerline adapters.

Run every five minutes from a timer. Each adapter publishes its state at ``/assets/data.cfl`` (key=value
lines, sometimes gzipped) for its own web page; that file is read and nothing is ever changed.

Each adapter reports the rate at which it *receives* from every other adapter, so each direction of
a link is measured by the adapter at its far end. The raw figure is in an undocumented unit;
``PLC_RATE_SCALE`` turns it into the Mbps the devolo Home Network app shows.

======================  =============================================================  ========
Check                   Problem when                                                    Delay
======================  =============================================================  ========
``offline:<address>``   the adapter does not answer                                     10 min
``link:<from>><to>``    an adapter that was linked no longer receives from the other    10 min
``slow:<from>><to>``    the rate is below ``PLC_DROP_PCT`` % of its 7-day median        30 min
======================  =============================================================  ========

Restarts (uptime went backwards) and firmware changes are reported once, at low priority.

The data file also contains the adapters' internal API tokens; only the fields listed in
:data:`FIELDS` are kept, and nothing else is logged or stored.

Settings (environment): ``PLC_HOSTS``, ``PLC_RATE_SCALE``, ``PLC_DROP_PCT``, ``PLC_STATE_FILE``,
``NTFY_URL``, ``VM_URL``, ``STATUS_DIR``.
"""

from __future__ import annotations

import gzip
import json
import logging
import re
import statistics
import time
import urllib.request
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from homelab_tools.common.config import Outputs, env_float, env_str
from homelab_tools.common.metrics import Sample, VictoriaMetrics
from homelab_tools.common.notify import Notifier
from homelab_tools.common.status import write_status

log = logging.getLogger("powerline-monitor")

MINUTE = 60.0
DAY = 86400.0
#: Mbps in the devolo app per raw rate unit, measured on a Magic 2 LAN pair (1315 app / 24657 raw).
DEFAULT_SCALE = 0.0533
HISTORY_DAYS = 7
HISTORY_EVERY = 30 * MINUTE  # one stored sample per link per half hour
MIN_HISTORY = 24  # half a day of samples before "slower than usual" is judged

#: The only fields read from the data file.
FIELDS = (
    "NODE.GENERAL.DEVICE_NAME",
    "NODE.GENERAL.NODE_TYPE",
    "SYSTEM.PRODUCTION.DEVICE_NAME",
    "SYSTEM.GENERAL.FW_VERSION",
    "SYSTEM.GENERAL.UPTIME",
    "SYSTEM.STATS.CPU_USAGE",
    "MASTERSELECTION.DOMAIN.LOST_MAPS",
    "DIDMNG.GENERAL.DIDS",
    "DIDMNG.GENERAL.MACS",
    "DIDMNG.GENERAL.ACTUAL_RX_BPS",
    "DEVOLO.REMOTE.DEVICE_NAMES",
)

#: Fetches an adapter's data file (raw bytes); raises ``OSError`` when it does not answer.
Fetch = Callable[[str], bytes]


def http_fetch(host: str) -> bytes:
    """Default fetcher: GET ``http://<host>/assets/data.cfl``."""
    with urllib.request.urlopen(f"http://{host}/assets/data.cfl", timeout=10) as resp:
        body: bytes = resp.read()
        return body


def parse(raw: bytes) -> dict[str, str]:
    """The :data:`FIELDS` present in a data file (gzipped or plain)."""
    if raw[:2] == b"\x1f\x8b":
        raw = gzip.decompress(raw)
    values = {}
    for line in raw.decode("utf-8", "replace").splitlines():
        key, sep, value = line.partition("=")
        if sep and key.strip() in FIELDS:
            values[key.strip()] = value.strip()
    return values


def parse_uptime(text: str) -> int | None:
    """Seconds from devolo's ``5 days, 21h 10m 43s``; ``None`` if unrecognised."""
    units = {"day": 86400, "days": 86400, "h": 3600, "m": 60, "s": 1}
    parts = re.findall(r"(\d+)\s*(days?|h|m|s)\b", text)
    return sum(int(n) * units[u] for n, u in parts) if parts else None


def _number(text: str | None) -> float | None:
    try:
        return float(text) if text is not None else None
    except ValueError:
        return None


@dataclass(frozen=True)
class Adapter:
    """What one adapter reports.

    Attributes:
        host: Address it was read from.
        name: Its name in the devolo app (falls back to the address).
        model: e.g. ``Magic 2 LAN 1-1``.
        role: ``DOMAIN_MASTER`` (coordinates the powerline network) or ``END_POINT``.
        firmware: Version string.
        uptime_s: Seconds since it started.
        cpu_pct: CPU use.
        lost_maps: Running count of missed scheduling messages from the master (a sign of noise).
        receive_mbps: Receive rate from each other adapter, by name.
    """

    host: str
    name: str
    model: str = ""
    role: str = ""
    firmware: str = ""
    uptime_s: int | None = None
    cpu_pct: float | None = None
    lost_maps: float | None = None
    receive_mbps: dict[str, float] = field(default_factory=dict)


def read_adapter(host: str, values: dict[str, str], scale: float) -> Adapter:
    """Build an :class:`Adapter` from parsed data-file fields."""
    name = values.get("NODE.GENERAL.DEVICE_NAME") or host
    ids = values.get("DIDMNG.GENERAL.DIDS", "").split(",")
    macs = values.get("DIDMNG.GENERAL.MACS", "").split(",")
    rates = values.get("DIDMNG.GENERAL.ACTUAL_RX_BPS", "").split(",")
    names = values.get("DEVOLO.REMOTE.DEVICE_NAMES", "").split(",")
    receive = {}
    for i, rate in enumerate(rates):
        value = _number(rate)
        if not value:
            continue  # 0 for itself and for empty slots
        device = int(ids[i]) if i < len(ids) and ids[i].isdigit() else -1
        peer = names[device] if 0 <= device < len(names) and names[device] else ""
        peer = peer or (macs[i] if i < len(macs) else f"device {device}")
        receive[peer] = round(value * scale, 1)
    return Adapter(
        host=host,
        name=name,
        model=values.get("SYSTEM.PRODUCTION.DEVICE_NAME", ""),
        role=values.get("NODE.GENERAL.NODE_TYPE", ""),
        firmware=values.get("SYSTEM.GENERAL.FW_VERSION", ""),
        uptime_s=parse_uptime(values.get("SYSTEM.GENERAL.UPTIME", "")),
        cpu_pct=_number(values.get("SYSTEM.STATS.CPU_USAGE")),
        lost_maps=_number(values.get("MASTERSELECTION.DOMAIN.LOST_MAPS")),
        receive_mbps=receive,
    )


def poll(hosts: Iterable[str], fetch: Fetch, scale: float) -> dict[str, Adapter | None]:
    """Read every adapter; ``None`` for one that does not answer."""
    found: dict[str, Adapter | None] = {}
    for host in hosts:
        try:
            found[host] = read_adapter(host, parse(fetch(host)), scale)
        except (OSError, ValueError, EOFError) as exc:
            log.info("%s: no answer (%s)", host, exc)
            found[host] = None
    return found


@dataclass
class State:
    """Kept between runs.

    Attributes:
        since: When each current problem was first seen (epoch seconds).
        alerted: Problems that have been reported (key -> text).
        names: Last known name of each address (for alerts about an adapter that is offline).
        uptime: Last uptime of each address, to spot restarts.
        firmware: Last firmware of each address, to spot updates.
        history: Half-hourly ``(time, Mbps)`` samples for each ``from>to`` link, last 7 days.
    """

    since: dict[str, float] = field(default_factory=dict)
    alerted: dict[str, str] = field(default_factory=dict)
    names: dict[str, str] = field(default_factory=dict)
    uptime: dict[str, int] = field(default_factory=dict)
    firmware: dict[str, str] = field(default_factory=dict)
    history: dict[str, list[tuple[float, float]]] = field(default_factory=dict)

    @classmethod
    def load(cls, path: Path) -> State:
        """Read the state file, or start fresh."""
        try:
            data = json.loads(path.read_text())
            data["history"] = {k: [tuple(s) for s in v] for k, v in data.get("history", {}).items()}
            return cls(**data)
        except (OSError, ValueError, TypeError):
            return cls()

    def save(self, path: Path) -> None:
        """Write the state file atomically."""
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.__dict__))
        tmp.replace(path)

    def usual(self, link: str) -> float | None:
        """Median rate of a link over the stored history, once there is enough of it."""
        samples = self.history.get(link, [])
        return statistics.median(v for _, v in samples) if len(samples) >= MIN_HISTORY else None


def link_key(sender: str, receiver: str) -> str:
    """State key for one direction of a link."""
    return f"{sender}>{receiver}"


def current_links(adapters: dict[str, Adapter | None]) -> dict[str, float]:
    """Rate of every link direction measured this run, by ``from>to`` key."""
    return {
        link_key(peer, a.name): mbps
        for a in adapters.values()
        if a is not None
        for peer, mbps in a.receive_mbps.items()
    }


@dataclass(frozen=True)
class Finding:
    """One check: ``problem`` is the alert text, or ``None`` when fine."""

    key: str
    problem: str | None
    delay: float


Alert = tuple[str, str, str]  # title, message, priority


def check(
    adapters: dict[str, Adapter | None], state: State, drop_pct: float
) -> tuple[list[Finding], list[Alert]]:
    """Findings for the delayed checks, plus one-off events (restarts and firmware changes).

    Updates the names, uptimes and firmware kept in ``state``.
    """
    findings: list[Finding] = []
    events: list[Alert] = []
    for host, adapter in adapters.items():
        name = adapter.name if adapter else state.names.get(host, host)
        findings.append(
            Finding(
                f"offline:{host}",
                None if adapter else f"the {name} powerline adapter ({host}) is not answering",
                10 * MINUTE,
            )
        )
        if adapter is None:
            continue
        state.names[host] = adapter.name
        before = state.uptime.get(host)
        if adapter.uptime_s is not None:
            if before is not None and adapter.uptime_s < before:
                events.append(
                    (
                        f"Powerline: {name} restarted",
                        f"The {name} adapter restarted {_ago(adapter.uptime_s)} ago - a power cut, "
                        "a firmware update or it was unplugged.",
                        "low",
                    )
                )
            state.uptime[host] = adapter.uptime_s
        old_fw = state.firmware.get(host)
        if adapter.firmware:
            if old_fw and old_fw != adapter.firmware:
                events.append(
                    (
                        f"Powerline: {name} updated",
                        f"The {name} adapter's firmware changed from {old_fw} to {adapter.firmware}.",
                        "low",
                    )
                )
            state.firmware[host] = adapter.firmware

    links = current_links(adapters)
    online = {a.name for a in adapters.values() if a is not None}
    for link in sorted(set(state.history) | set(links)):
        sender, _, receiver = link.partition(">")
        rate = links.get(link)
        if receiver not in online or (sender not in online and rate is None):
            # An offline adapter's own check covers this; keep any reported link problem open meanwhile.
            findings += [Finding(k, state.alerted.get(k), 0) for k in (f"link:{link}", f"slow:{link}")]
            continue
        findings.append(
            Finding(
                f"link:{link}",
                None if rate else f"{receiver} no longer has a powerline link from {sender}",
                10 * MINUTE,
            )
        )
        usual = state.usual(link)
        slow = rate is not None and usual is not None and rate < usual * drop_pct / 100
        findings.append(
            Finding(
                f"slow:{link}",
                f"the powerline link {sender} to {receiver} is down to {rate:.0f} Mbps "
                f"(usually {usual:.0f}) - often new electrical noise near a socket, or an adapter "
                "moved onto an extension lead"
                if slow and rate is not None and usual is not None
                else None,
                30 * MINUTE,
            )
        )
    return findings, events


def _ago(seconds: float) -> str:
    if seconds < 3600:
        return f"{max(1, round(seconds / 60))} min"
    if seconds < 2 * DAY:
        return f"{round(seconds / 3600)} h"
    return f"{round(seconds / DAY)} days"


def evaluate(findings: Iterable[Finding], state: State, now: float) -> list[Alert]:
    """Alert after each problem's delay, once; "OK again" when it clears."""
    alerts: list[Alert] = []
    current = {f.key: f for f in findings}
    for key, finding in current.items():
        if finding.problem is None:
            continue
        first = state.since.setdefault(key, now)
        if key not in state.alerted and now - first >= finding.delay:
            state.alerted[key] = finding.problem
            alerts.append(
                ("Powerline problem", finding.problem[0].upper() + finding.problem[1:] + ".", "high")
            )
    for key in list(state.since):
        if key not in current or current[key].problem is None:
            del state.since[key]
            reported = state.alerted.pop(key, None)
            if reported:
                alerts.append(("Powerline: OK again", f"Cleared: {reported}.", "low"))
    return alerts


def record_history(state: State, links: dict[str, float], now: float) -> None:
    """Keep one sample per link per half hour, for the last week."""
    for link, mbps in links.items():
        samples = state.history.setdefault(link, [])
        if not samples or now - samples[-1][0] >= HISTORY_EVERY:
            samples.append((now, mbps))
    for link in list(state.history):
        state.history[link] = [s for s in state.history[link] if now - s[0] <= HISTORY_DAYS * DAY]
        if not state.history[link]:
            del state.history[link]


def metrics(adapters: dict[str, Adapter | None], state: State) -> list[Sample]:
    """``powerline_rate_mbps{from,to}`` and per-adapter ``powerline_online``, uptime, CPU, lost maps."""
    samples = [
        Sample(
            "powerline", {"rate_mbps": mbps}, {"from": link.partition(">")[0], "to": link.partition(">")[2]}
        )
        for link, mbps in current_links(adapters).items()
    ]
    for host, a in adapters.items():
        fields: dict[str, float | int | bool | None] = {"online": a is not None}
        if a is not None:
            fields |= {"uptime_s": a.uptime_s, "cpu_pct": a.cpu_pct, "lost_maps": a.lost_maps}
        samples.append(Sample("powerline", fields, {"adapter": state.names.get(host, host)}))
    return samples


def display(adapters: dict[str, Adapter | None], state: State, findings: Sequence[Finding]) -> dict[str, str]:
    """Status fields for ``powerline.json`` (see ``docs/powerline-monitor.md``)."""
    links = current_links(adapters)
    order = sorted(set(links) | set(state.history))
    online = [a for a in adapters.values() if a is not None]
    usual = [state.usual(k) for k in order]
    firmware = {a.firmware.split()[-1].split("_")[0] for a in online if a.firmware}
    uptimes = [a.uptime_s for a in online if a.uptime_s is not None]
    noise = [f"{a.name} {a.lost_maps:.0f}" for a in online if a.lost_maps is not None]
    return {
        "links": ", ".join(k.replace(">", " → ") for k in order) or "-",
        "speed": " / ".join(f"{links[k]:.0f}" if k in links else "-" for k in order) + " Mbps"
        if order
        else "-",
        "usual": " / ".join(f"{u:.0f}" if u is not None else "-" for u in usual) + " Mbps"
        if any(u is not None for u in usual)
        else "learning",
        "adapters": f"{len(online)} of {len(adapters)} online",
        "uptime": _ago(min(uptimes)) if uptimes else "-",
        "firmware": ", ".join(sorted(firmware)) or "-",
        "lost_maps": ", ".join(noise) or "-",
        "problems": str(sum(1 for f in findings if f.problem)),
    }


def run(
    hosts: Sequence[str], fetch: Fetch, state: State, now: float, scale: float, drop_pct: float
) -> tuple[list[Alert], dict[str, Adapter | None], list[Finding]]:
    """One pass: poll, check, alert, and record history (after judging, so a drop isn't its own norm)."""
    adapters = poll(hosts, fetch, scale)
    findings, events = check(adapters, state, drop_pct)
    alerts = events + evaluate(findings, state, now)
    record_history(state, current_links(adapters), now)
    return alerts, adapters, findings


def main() -> None:
    """Entry point: one run (meant for a 5-minute timer)."""
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    outputs = Outputs.from_env()
    hosts = env_str("PLC_HOSTS").split()
    state_file = Path(env_str("PLC_STATE_FILE", "/var/lib/powerline-monitor/state.json"))
    state = State.load(state_file)
    alerts, adapters, findings = run(
        hosts,
        http_fetch,
        state,
        time.time(),
        env_float("PLC_RATE_SCALE", DEFAULT_SCALE),
        env_float("PLC_DROP_PCT", 50.0),
    )
    notifier = Notifier(outputs.ntfy_url)
    for title, message, priority in alerts:
        log.info("%s: %s", title, message)
        notifier.send(title, message, priority=priority, tags="electric_plug" if priority == "high" else "")
    VictoriaMetrics(outputs.vm_url).write(metrics(adapters, state))
    state.save(state_file)
    write_status(
        outputs.status_dir,
        "powerline",
        display(adapters, state, findings) | {"checked": time.strftime("%H:%M")},
    )


if __name__ == "__main__":
    main()
