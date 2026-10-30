"""``ipv6-watch``: alert when a router starts announcing IPv6, or a DNS server other than Pi-hole.

Run every five minutes from a timer. Each run asks the routers on the network to announce themselves
(an IPv6 router solicitation, sent by ``rdisc6`` from the ``ndisc6`` package) and reads the answers.

Why it matters: a router that announces IPv6 can also announce its own DNS server, and phones
(iPhones especially) then prefer it over the one DHCP gives them. Those devices stop using Pi-hole:
no blocking, and local names resolve to their public address instead. Many ISP routers turn IPv6
back on by themselves after a restart or firmware update.

=========================  ============================================================
Check                      Problem when
=========================  ============================================================
``ipv6:<router>``          a router announces IPv6 and ``IPV6_EXPECTED`` is off
``dns:<server>``           a router announces a DNS server not in ``IPV6_ALLOWED_DNS``
=========================  ============================================================

Each problem is reported at once, once, with "OK again" when it clears.

Settings (environment): ``IPV6_IFACE``, ``IPV6_EXPECTED``, ``IPV6_ALLOWED_DNS``, ``IPV6_STATE_FILE``,
``NTFY_URL``, ``VM_URL``, ``STATUS_DIR``.
"""

from __future__ import annotations

import json
import logging
import re
import subprocess
import time
from collections.abc import Callable, Collection, Iterable
from dataclasses import dataclass, field
from pathlib import Path

from homelab_tools.common.config import ConfigError, Outputs, env_str
from homelab_tools.common.metrics import Sample, VictoriaMetrics
from homelab_tools.common.notify import Notifier
from homelab_tools.common.status import write_status

log = logging.getLogger("ipv6-watch")

#: Asks the routers on an interface to announce themselves and returns ``rdisc6``'s output.
Solicit = Callable[[str], str]

FIX = (
    "Phones may stop using Pi-hole (no blocking, and local names go to the router instead). "
    "Turn IPv6 off in the router (on a BT Hub: Advanced Settings > Broadband > IPv6), then "
    "reconnect affected devices to Wi-Fi."
)


def rdisc6(iface: str) -> str:
    """Default solicitation: ``rdisc6`` with three tries a second apart; ``""`` when nothing answers."""
    if not re.fullmatch(r"[\w.@-]{1,15}", iface):
        raise ConfigError(f"IPV6_IFACE is not an interface name: {iface!r}")
    result = subprocess.run(
        ["rdisc6", "-r", "3", "-w", "1000", iface], capture_output=True, text=True, timeout=30, check=False
    )
    if "Soliciting" not in result.stdout + result.stderr:
        raise OSError(f"rdisc6 failed: {(result.stderr or result.stdout).strip()}")
    return result.stdout


@dataclass(frozen=True)
class Router:
    """One router advertisement.

    Attributes:
        source: The router's link-local address.
        lifetime_s: How long devices may use it as their default route (0: not a default router).
        prefixes: Address prefixes it offers.
        dns: DNS servers it announces (RDNSS).
    """

    source: str
    lifetime_s: int = 0
    prefixes: tuple[str, ...] = ()
    dns: tuple[str, ...] = ()

    @property
    def announces_ipv6(self) -> bool:
        """Whether devices will get IPv6 from it (a default route or an address prefix)."""
        return self.lifetime_s > 0 or bool(self.prefixes)


def parse(output: str) -> list[Router]:
    """Routers in ``rdisc6`` output; each answer ends with a ``from <address>`` line."""
    routers: list[Router] = []
    lifetime, prefixes, dns = 0, [], []
    for line in output.splitlines():
        key, _, value = (part.strip() for part in line.partition(":"))
        if key == "Router lifetime":
            lifetime = int(value.split()[0]) if value.split()[0].isdigit() else 0
        elif key == "Prefix":
            prefixes.append(value)
        elif key == "Recursive DNS server":
            dns.append(value)
        elif line.strip().startswith("from "):
            routers.append(Router(line.split()[1], lifetime, tuple(prefixes), tuple(dns)))
            lifetime, prefixes, dns = 0, [], []
    return routers


def findings(routers: Iterable[Router], expected: bool, allowed_dns: Collection[str]) -> dict[str, str]:
    """Current problems, by key."""
    problems: dict[str, str] = {}
    for router in routers:
        if router.announces_ipv6 and not expected:
            extra = f" with its own DNS server {', '.join(router.dns)}" if router.dns else ""
            problems[f"ipv6:{router.source}"] = f"a router ({router.source}) is announcing IPv6{extra}"
        for server in router.dns:
            if server not in allowed_dns and not (router.announces_ipv6 and not expected):
                problems[f"dns:{server}"] = f"a router ({router.source}) is announcing DNS server {server}"
    return problems


@dataclass
class State:
    """Problems already reported (key -> text), kept between runs."""

    alerted: dict[str, str] = field(default_factory=dict)

    @classmethod
    def load(cls, path: Path) -> State:
        """Read the state file, or start fresh."""
        try:
            return cls(**json.loads(path.read_text()))
        except (OSError, ValueError, TypeError):
            return cls()

    def save(self, path: Path) -> None:
        """Write the state file atomically."""
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.__dict__))
        tmp.replace(path)


Alert = tuple[str, str, str]  # title, message, priority


def evaluate(problems: dict[str, str], state: State) -> list[Alert]:
    """Alert on each new problem once; "OK again" when one clears."""
    alerts: list[Alert] = []
    for key, text in problems.items():
        if key not in state.alerted:
            state.alerted[key] = text
            alerts.append(("IPv6: DNS may bypass Pi-hole", f"{text[0].upper()}{text[1:]}. {FIX}", "high"))
    for key in [k for k in state.alerted if k not in problems]:
        alerts.append(("IPv6: OK again", f"Cleared: {state.alerted.pop(key)}.", "low"))
    return alerts


def metrics(routers: list[Router], problems: dict[str, str]) -> list[Sample]:
    """``ipv6_routers``, ``ipv6_announcing``, ``ipv6_dns_servers`` and ``ipv6_problems``."""
    return [
        Sample(
            "ipv6",
            {
                "routers": len(routers),
                "announcing": sum(r.announces_ipv6 for r in routers),
                "dns_servers": len({s for r in routers for s in r.dns}),
                "problems": len(problems),
            },
        )
    ]


def display(routers: list[Router], problems: dict[str, str]) -> dict[str, str]:
    """Status fields for ``ipv6.json`` (see ``docs/ipv6-watch.md``)."""
    announcing = [r for r in routers if r.announces_ipv6]
    servers = sorted({s for r in routers for s in r.dns})
    plural = "s" if len(announcing) != 1 else ""
    return {
        "ipv6": f"on ({len(announcing)} router{plural})" if announcing else "off",
        "dns": ", ".join(servers) or "none announced",
        "problems": str(len(problems)),
    }


def run(
    iface: str, solicit: Solicit, state: State, expected: bool, allowed_dns: Collection[str]
) -> tuple[list[Alert], list[Router], dict[str, str]]:
    """One pass: solicit, check and alert."""
    routers = parse(solicit(iface))
    problems = findings(routers, expected, allowed_dns)
    return evaluate(problems, state), routers, problems


def main() -> None:
    """Entry point: one run (meant for a 5-minute timer)."""
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    outputs = Outputs.from_env()
    state_file = Path(env_str("IPV6_STATE_FILE", "/var/lib/ipv6-watch/state.json"))
    state = State.load(state_file)
    alerts, routers, problems = run(
        env_str("IPV6_IFACE", "eth0"),
        rdisc6,
        state,
        env_str("IPV6_EXPECTED", "no").lower() in ("yes", "true", "1", "on"),
        set(env_str("IPV6_ALLOWED_DNS", "").split()),
    )
    notifier = Notifier(outputs.ntfy_url)
    for title, message, priority in alerts:
        log.info("%s: %s", title, message)
        notifier.send(title, message, priority=priority, tags="warning" if priority == "high" else "")
    VictoriaMetrics(outputs.vm_url).write(metrics(routers, problems))
    state.save(state_file)
    write_status(outputs.status_dir, "ipv6", display(routers, problems) | {"checked": time.strftime("%H:%M")})


if __name__ == "__main__":
    main()
