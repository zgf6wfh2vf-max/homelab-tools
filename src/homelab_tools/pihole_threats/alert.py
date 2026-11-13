"""``pihole-threat-alert``: notify when a LAN device looks up a known malware/phishing domain.

Pi-hole normally *blocks* these lookups; the point of the alert is that a device asking for a
malware, phishing or command-and-control domain is a strong hint it is compromised or that someone
clicked something they should not have. Each alert says whether Pi-hole blocked the lookup; one it
*allowed* (the domain is allowlisted, or was added to the list after the lookup was answered from
cache) is sent at urgent priority, since the device may have reached the site.

Use a dedicated threat list (e.g. HaGeZi's "Threat Intelligence Feeds", adblock format) as one of
your Pi-hole adlists and point ``THREAT_LIST_MATCH`` at it, so ordinary ad blocking does not cause
alerts.

Run every few minutes from a systemd timer. Pi-hole v6 writes queries to its database roughly once
a minute, so a lookup can take a couple of minutes to be noticed.

Settings (environment):

``PIHOLE_DIR``          Directory with ``pihole-FTL.db`` and ``gravity.db`` (default /etc/pihole).
``THREAT_LIST_MATCH``   SQL LIKE pattern for the threat adlist's URL (default ``%hagezi%tif%``).
``THREAT_STATE_FILE``   Progress and de-duplication state (default /var/lib/pihole-threat-alert/state.json).
``THREAT_REALERT_HOURS`` Re-alert the same device+domain after this many hours, default 24.

``pihole-threat-alert --report N`` lists every lookup of listed domains in the last N days, and
whether it was blocked, without alerting - useful when you first add the list, to see what happened
before.
"""

from __future__ import annotations

import argparse
import json
import logging
import sqlite3
import sys
import time
from collections.abc import Iterator
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path

from homelab_tools.common.config import Outputs, env_int, env_str
from homelab_tools.common.notify import Notifier

log = logging.getLogger("pihole-threat-alert")

#: Pi-hole FTL query status codes meaning the lookup was blocked: gravity, regex and denylist (also
#: via a CNAME), blocked upstream, database busy, and special domains.
BLOCKED_STATUSES = (1, 4, 5, 6, 7, 8, 9, 10, 11, 15, 16, 18)


def candidate_keys(domain: str) -> list[str]:
    """Adblock-style keys that would match ``domain``: ``||a.b.c^``, ``||b.c^``, plus the bare domain.

    A rule ``||example.com^`` covers ``example.com`` and every subdomain, so each parent suffix is a
    candidate. Top-level domains on their own (``||com^``) are not considered.
    """
    name = domain.lower().rstrip(".")
    parts = name.split(".")
    return [f"||{'.'.join(parts[i:])}^" for i in range(len(parts) - 1)] + [name]


@dataclass(frozen=True)
class Hit:
    """One lookup of a listed domain."""

    query_id: int
    timestamp: float
    domain: str
    client_ip: str
    client_name: str
    blocked: bool = True


class ThreatDatabase:
    """Read-only access to Pi-hole's query log and gravity (blocklist) databases."""

    def __init__(self, pihole_dir: str, list_match: str) -> None:
        """Open both databases read-only and find the threat list's id(s)."""
        directory = Path(pihole_dir)
        self.db = sqlite3.connect(f"file:{directory / 'pihole-FTL.db'}?mode=ro", uri=True, timeout=30)
        self.db.execute("ATTACH DATABASE ? AS g", (f"file:{directory / 'gravity.db'}?mode=ro",))
        self.list_ids = [
            row[0]
            for row in self.db.execute(
                "SELECT id FROM g.adlist WHERE address LIKE ? AND enabled = 1", (list_match,)
            )
        ]
        self._cache: dict[str, bool] = {}

    def close(self) -> None:
        """Close the database connection."""
        self.db.close()

    def is_threat(self, domain: str) -> bool:
        """Whether ``domain`` (or a parent domain) is on the threat list; cached per run."""
        if domain not in self._cache:
            keys = candidate_keys(domain)
            sql = (
                f"SELECT 1 FROM g.gravity WHERE adlist_id IN ({','.join('?' * len(self.list_ids))}) "
                f"AND domain IN ({','.join('?' * len(keys))}) LIMIT 1"
            )
            self._cache[domain] = self.db.execute(sql, [*self.list_ids, *keys]).fetchone() is not None
        return self._cache[domain]

    def max_query_id(self) -> int:
        """Newest query id in the log."""
        return int(self.db.execute("SELECT COALESCE(MAX(id), 0) FROM queries").fetchone()[0])

    def hits(self, after_id: int = 0, since: float = 0.0, blocked_only: bool = False) -> Iterator[Hit]:
        """Lookups of listed domains with id > ``after_id`` and timestamp >= ``since``.

        The client name is the most recent non-empty name Pi-hole recorded for the IP (an address can
        change hands through DHCP over time).
        """
        status = f"AND q.status IN ({','.join(map(str, BLOCKED_STATUSES))}) " if blocked_only else ""
        rows = self.db.execute(
            "SELECT q.id, q.timestamp, q.domain, q.client, q.status, "
            "COALESCE((SELECT c.name FROM client_by_id c WHERE c.ip = q.client AND c.name <> '' "
            "ORDER BY c.id DESC LIMIT 1), q.client) "
            f"FROM queries q WHERE q.id > ? AND q.timestamp >= ? {status}ORDER BY q.id",
            (after_id, since),
        )
        for qid, ts, domain, ip, status, name in rows:
            if self.is_threat(domain):
                yield Hit(qid, ts, domain, ip, name, status in BLOCKED_STATUSES)


@dataclass
class AlertState:
    """Where the last run stopped, and which device+domain pairs were alerted when."""

    last_id: int
    seen: dict[str, float]

    @classmethod
    def load(cls, path: Path) -> AlertState | None:
        """Read the state file; ``None`` if this is the first run."""
        try:
            data = json.loads(path.read_text())
        except (OSError, ValueError):
            return None
        return cls(int(data["last_id"]), {k: float(v) for k, v in data.get("seen", {}).items()})

    def save(self, path: Path) -> None:
        """Write the state file."""
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"last_id": self.last_id, "seen": self.seen}))


def group_new_hits(
    hits: list[Hit], state: AlertState, now: float, realert_seconds: float
) -> dict[tuple[str, str], dict[str, bool]]:
    """Decide what to alert about, updating ``state.seen``.

    Each device+domain pair alerts at most once per ``realert_seconds`` - separately for blocked
    and allowed lookups, so an allowed one still alerts after a blocked one. Domains are grouped per
    device so one compromised device produces one notification, not dozens.

    Returns:
        ``{(client name, client ip): {domain: blocked}}`` for the alerts to send; a domain counts as
        blocked only if every new lookup of it was.
    """
    state.seen = {k: t for k, t in state.seen.items() if now - t < realert_seconds}
    grouped: dict[tuple[str, str], dict[str, bool]] = {}
    for hit in hits:
        key = f"{hit.client_ip}|{hit.domain}" + ("" if hit.blocked else "|allowed")
        if key in state.seen:
            continue
        state.seen[key] = now
        domains = grouped.setdefault((hit.client_name, hit.client_ip), {})
        domains[hit.domain] = domains.get(hit.domain, True) and hit.blocked
    return grouped


def _listed(domains: list[str]) -> str:
    """At most five domains, comma-separated."""
    return ", ".join(domains[:5]) + (f" (+{len(domains) - 5} more)" if len(domains) > 5 else "")


def alert_text(name: str, ip: str, domains: dict[str, bool]) -> str:
    """Notification body for one device: which lookups Pi-hole blocked and which it allowed."""
    allowed = [d for d, blocked in domains.items() if not blocked]
    blocked = [d for d, was_blocked in domains.items() if was_blocked]
    if not allowed:
        return (
            f"{name} ({ip}) tried to reach known malware/phishing domain(s): {_listed(blocked)}. "
            "Pi-hole blocked it. Check that device."
        )
    also = f" (blocked: {_listed(blocked)})" if blocked else ""
    return (
        f"{name} ({ip}) looked up known malware/phishing domain(s) and Pi-hole did NOT block: "
        f"{_listed(allowed)}{also}. The device may have reached the site - check it now, and check "
        "Pi-hole's allowlist and that the threat list is up to date."
    )


def alert_title(name: str, domains: dict[str, bool]) -> tuple[str, str]:
    """Notification title and priority: urgent when any lookup was allowed."""
    if all(domains.values()):
        return f"Threat DNS lookup: {name}", "high"
    return f"Threat DNS lookup NOT blocked: {name}", "urgent"


def report(db: ThreatDatabase, days: int) -> list[str]:
    """Lines describing every lookup (blocked or not) of listed domains in the last ``days`` days."""
    found: dict[tuple[str, str, str], list[Hit]] = {}
    for hit in db.hits(since=time.time() - days * 86400):
        found.setdefault((hit.client_name, hit.client_ip, hit.domain), []).append(hit)
    lines = []
    for (name, ip, domain), hits in sorted(found.items(), key=lambda kv: -len(kv[1])):
        allowed = sum(not h.blocked for h in hits)
        last = time.strftime("%F %H:%M", time.localtime(max(h.timestamp for h in hits)))
        what = f"{allowed} allowed" if allowed else "blocked"
        lines.append(f"{len(hits):6}  {last}  {what:<10}  {name} ({ip})  {domain}")
    return lines + [f"{len(found)} distinct device/domain pairs in the last {days} days"]


def main() -> None:
    """Entry point: one check (for a systemd timer), or ``--report N``."""
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    parser = argparse.ArgumentParser(description="Alert on DNS lookups of threat-list domains.")
    parser.add_argument(
        "--report", type=int, metavar="DAYS", help="list lookups in the last DAYS days; no alerts"
    )
    args = parser.parse_args()

    pihole_dir = env_str("PIHOLE_DIR", "/etc/pihole")
    with closing(ThreatDatabase(pihole_dir, env_str("THREAT_LIST_MATCH", "%hagezi%tif%"))) as db:
        check(db, args.report)


def check(db: ThreatDatabase, report_days: int | None) -> None:
    """Alert on new threat-list lookups (or print a report of the last ``report_days``)."""
    if not db.list_ids:
        sys.exit("threat list not found among the enabled Pi-hole adlists (check THREAT_LIST_MATCH)")
    if report_days:
        print("\n".join(report(db, report_days)))
        return

    state_file = Path(env_str("THREAT_STATE_FILE", "/var/lib/pihole-threat-alert/state.json"))
    state = AlertState.load(state_file)
    if state is None:  # first run: start from now; use --report for history
        AlertState(db.max_query_id(), {}).save(state_file)
        log.info("initialised; watching new queries from now on")
        return
    notifier = Notifier(Outputs.from_env().ntfy_url)
    newest = db.max_query_id()
    alerts = group_new_hits(
        list(db.hits(after_id=state.last_id)), state, time.time(), env_int("THREAT_REALERT_HOURS", 24) * 3600
    )
    for (name, ip), domains in alerts.items():
        log.info(
            "ALERT: %s (%s): %s", name, ip,
            ", ".join(d + ("" if blocked else " (allowed)") for d, blocked in domains.items()),
        )  # fmt: skip
        title, priority = alert_title(name, domains)
        notifier.send(title, alert_text(name, ip, domains), priority=priority, tags="rotating_light")
    state.last_id = max(state.last_id, newest)
    state.save(state_file)


if __name__ == "__main__":
    main()
