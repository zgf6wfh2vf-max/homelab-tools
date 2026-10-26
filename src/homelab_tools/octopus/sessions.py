"""``octopus-sessions``: alerts for Octopus Power Down / Power Up sessions and free-electricity events.

Octopus runs demand-flexibility events through its GraphQL API:

* **Saving sessions** (backend API, ``savingSessions``): *Power Down* (``TURN_DOWN``: you are paid
  Octopoints for each kWh you import *below* your usual level for that hour), *Power Up*
  (``TURN_UP``: use more), and *Weekend Happy Hours*. You opt in to each event in the Octopus app or
  from the invitation email.
* **Flexibility campaigns** (``customerFlexibilityCampaignEvents``): e.g. ``free_electricity``
  (national free hours, which ended in 2025) and regional ``power_ups_<network>`` events.

This tool only reads: it sends a notification when a new event is announced (saying whether you
have joined it), and another once Octopus has worked out the result of an event you joined -
including when it paid nothing because of ``MISSING_READINGS``, which is worth raising with Octopus.
It never joins events for you.

Authentication: your account API key (the same one ``octopus-collector`` uses) is exchanged for a
short-lived token with the ``obtainKrakenToken`` mutation.

Settings (environment): ``OCTOPUS_API_KEY_FILE`` / ``OCTOPUS_API_KEY``, ``OCTOPUS_CONFIG``,
``SESSION_TYPES`` (``TURN_DOWN TURN_UP``), ``CAMPAIGNS`` (``free_electricity``),
``OCTOPOINTS_PER_POUND`` (800), ``SESSIONS_STATE_FILE``, ``NTFY_URL``, ``STATUS_DIR``.
"""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.request
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from homelab_tools.common.config import ConfigError, Outputs, env_float, env_str
from homelab_tools.common.notify import Notifier
from homelab_tools.common.status import write_status

log = logging.getLogger("octopus-sessions")

MAIN = "https://api.octopus.energy/v1/graphql/"
BACKEND = "https://api.backend.octopus.energy/v1/graphql/"

#: Posts a GraphQL query: (endpoint, query, variables, token or "") -> decoded response.
Post = Callable[[str, str, Mapping[str, Any], str], dict[str, Any]]

TOKEN_MUTATION = "mutation($key: String!) { obtainKrakenToken(input: {APIKey: $key}) { token } }"
SESSIONS_QUERY = """
query($account: String!) {
  savingSessions {
    events { id code startAt endAt status eventType rewardPerKwhInOctoPoints }
    account(accountNumber: $account) {
      hasJoinedCampaign tokenBalance
      joinedEvents { eventId startAt endAt eventType eventStatus resultsStatus rewardGivenInOctoPoints }
    }
  }
}"""
CAMPAIGN_QUERY = """
query($account: String!, $mpan: String!, $slug: String!) {
  customerFlexibilityCampaignEvents(
    accountNumber: $account, supplyPointIdentifier: $mpan, campaignSlug: $slug, first: 10
  ) { edges { node { code startAt endAt } } }
}"""

NAMES = {"TURN_DOWN": "Power Down", "TURN_UP": "Power Up", "WEEKEND_HAPPY_HOUR": "Weekend Happy Hour"}


class OctopusGraphQLError(RuntimeError):
    """The API returned errors and no usable data."""


def http_post(url: str, query: str, variables: Mapping[str, Any], token: str) -> dict[str, Any]:
    """Default :data:`Post`: a JSON POST, returning the decoded body (including GraphQL errors)."""
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = token
    body = json.dumps({"query": query, "variables": dict(variables)}).encode()
    request = urllib.request.Request(url, data=body, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=30) as resp:
            result: dict[str, Any] = json.load(resp)
    except urllib.error.HTTPError as exc:  # GraphQL validation errors come back as HTTP 400
        result = json.loads(exc.read() or b"{}")
    return result


def data(response: Mapping[str, Any], name: str) -> Any:
    """``response["data"][name]``, raising :class:`OctopusGraphQLError` with the API's message if absent."""
    value = (response.get("data") or {}).get(name)
    if value is None:
        messages = "; ".join(e.get("message", "?") for e in response.get("errors") or []) or "no data"
        raise OctopusGraphQLError(f"{name}: {messages}")
    return value


def parse_time(text: str) -> datetime:
    """Parse an API timestamp as an aware datetime."""
    return datetime.fromisoformat(text.replace("Z", "+00:00"))


@dataclass(frozen=True)
class Event:
    """A flexibility event, from either API.

    Attributes:
        key: Unique id for de-duplication (``session:<id>`` or ``campaign:<code>``).
        kind: Display name, e.g. "Power Down" or "free_electricity".
        start: Start time.
        end: End time.
        reward_points: Octopoints per kWh (saving sessions), or ``None``.
        joined: Whether the account has opted in (``None`` when unknown, e.g. campaigns).
    """

    key: str
    kind: str
    start: datetime
    end: datetime
    reward_points: int | None = None
    joined: bool | None = None


@dataclass(frozen=True)
class Outcome:
    """The result of a joined saving session."""

    key: str
    kind: str
    start: datetime
    status: str
    points: int


@dataclass
class Snapshot:
    """Everything one run learned from Octopus."""

    upcoming: list[Event] = field(default_factory=list)
    outcomes: list[Outcome] = field(default_factory=list)
    points_balance: int | None = None


def parse_sessions(sessions: Mapping[str, Any], types: Iterable[str], now: datetime) -> Snapshot:
    """Upcoming events of the wanted ``types``, and results of joined events."""
    wanted = set(types)
    account = sessions.get("account") or {}
    joined = {int(e["eventId"]) for e in account.get("joinedEvents") or []}
    snapshot = Snapshot(points_balance=account.get("tokenBalance"))
    for e in sessions.get("events") or []:
        end = parse_time(e["endAt"])
        if e["eventType"] in wanted and end > now:
            snapshot.upcoming.append(
                Event(
                    f"session:{e['id']}",
                    NAMES.get(e["eventType"], e["eventType"]),
                    parse_time(e["startAt"]),
                    end,
                    int(e["rewardPerKwhInOctoPoints"]),
                    int(e["id"]) in joined,
                )
            )
    for j in account.get("joinedEvents") or []:
        if j.get("eventStatus") == "DONE" and j.get("resultsStatus"):
            snapshot.outcomes.append(
                Outcome(
                    f"session:{j['eventId']}",
                    NAMES.get(j["eventType"], j["eventType"]),
                    parse_time(j["startAt"]),
                    str(j["resultsStatus"]),
                    int(j.get("rewardGivenInOctoPoints") or 0),
                )
            )
    return snapshot


def parse_campaign(slug: str, events: Mapping[str, Any], now: datetime) -> list[Event]:
    """Future events of one flexibility campaign."""
    out = []
    for edge in events.get("edges") or []:
        node = edge["node"]
        end = parse_time(node["endAt"])
        if end > now:
            out.append(
                Event(f"campaign:{node['code']}", slug.replace("_", " "), parse_time(node["startAt"]), end)
            )
    return out


@dataclass
class OctopusFlex:
    """Reads events from the two GraphQL APIs.

    Attributes:
        api_key: Account API key.
        account: Account number (``A-...``).
        mpan: Import MPAN (for campaign queries).
        post: GraphQL transport.
    """

    api_key: str
    account: str
    mpan: str
    post: Post = http_post

    def token(self) -> str:
        """Exchange the API key for a short-lived token."""
        response = self.post(MAIN, TOKEN_MUTATION, {"key": self.api_key}, "")
        return str(data(response, "obtainKrakenToken")["token"])

    def snapshot(self, types: Iterable[str], campaigns: Iterable[str], now: datetime) -> Snapshot:
        """Upcoming events and recent results, from both APIs."""
        token = self.token()
        sessions = data(
            self.post(BACKEND, SESSIONS_QUERY, {"account": self.account}, token), "savingSessions"
        )
        snapshot = parse_sessions(sessions, types, now)
        for slug in campaigns:
            variables = {"account": self.account, "mpan": self.mpan, "slug": slug}
            try:
                events = data(
                    self.post(MAIN, CAMPAIGN_QUERY, variables, token), "customerFlexibilityCampaignEvents"
                )
            except OctopusGraphQLError as exc:  # e.g. not enrolled, or an unknown campaign slug
                log.warning("campaign %s: %s", slug, exc)
                continue
            snapshot.upcoming += parse_campaign(slug, events, now)
        return snapshot


def when(event: Event, now: datetime) -> str:
    """``"today 18:00-19:00"``, ``"tomorrow 13:00-14:00"`` or ``"Sat 1 Nov 11:00-12:00"`` (local time)."""
    start, end = event.start.astimezone(), event.end.astimezone()
    days = (start.date() - now.astimezone().date()).days
    day = {0: "today", 1: "tomorrow"}.get(days, f"{start:%a %-d %b}")
    return f"{day} {start:%H:%M}-{end:%H:%M}"


def announce(event: Event, now: datetime, points_per_pound: float) -> tuple[str, str, str]:
    """Notification for a newly announced event: (title, message, priority)."""
    title = f"{event.kind} {when(event, now)}"
    parts = []
    if event.reward_points:
        pence = event.reward_points / points_per_pound * 100
        verb = "use above" if event.kind == "Power Up" else "save below"
        reward = f"{event.reward_points} Octopoints (about {pence:.0f}p)"
        parts.append(f"Octopus pays {reward} per kWh you {verb} your usual level.")
    if event.joined:
        parts.append("You've joined it.")
    elif event.joined is False:
        parts.append("Not joined yet: opt in from the Octopus app or the invitation email.")
    if event.kind == "Power Down":
        parts.append("Keep the battery charged for that hour and avoid heavy appliances.")
    return title, " ".join(parts) or "A new event was announced.", "default"


def result_message(outcome: Outcome) -> tuple[str, str, str]:
    """Notification for the result of a joined session: (title, message, priority)."""
    day = f"{outcome.start.astimezone():%a %-d %b}"
    if outcome.status == "MISSING_READINGS":
        message = (
            f"{outcome.kind} on {day}: no reward - Octopus says it is missing meter readings for that hour. "
            "If this keeps happening, ask Octopus to check your smart meter data for saving sessions."
        )
    else:
        status = outcome.status.lower().replace("_", " ")
        message = f"{outcome.kind} on {day}: {outcome.points} Octopoints ({status})."
    return f"{outcome.kind} result", message, "low"


@dataclass
class State:
    """Event keys already announced and results already reported."""

    announced: list[str] = field(default_factory=list)
    reported: list[str] = field(default_factory=list)

    @classmethod
    def load(cls, path: Path) -> State | None:
        """The saved state, or ``None`` on the very first run."""
        try:
            raw = json.loads(path.read_text())
            return cls(list(raw.get("announced", [])), list(raw.get("reported", [])))
        except (OSError, ValueError):
            return None

    def save(self, path: Path) -> None:
        """Write the state (keeping the most recent 500 keys of each kind)."""
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"announced": self.announced[-500:], "reported": self.reported[-500:]}))


def process(
    snapshot: Snapshot, state: State | None, now: datetime, points_per_pound: float = 800
) -> tuple[State, list[tuple[str, str, str]]]:
    """Decide what to send. On the first run, existing results are recorded silently (no backlog flood)."""
    first_run = state is None
    state = state or State()
    alerts = []
    for event in sorted(snapshot.upcoming, key=lambda e: e.start):
        if event.key not in state.announced:
            state.announced.append(event.key)
            alerts.append(announce(event, now, points_per_pound))
    for outcome in sorted(snapshot.outcomes, key=lambda o: o.start):
        if outcome.key not in state.reported:
            state.reported.append(outcome.key)
            if not first_run:
                alerts.append(result_message(outcome))
    return state, alerts


def display(snapshot: Snapshot, now: datetime) -> dict[str, str]:
    """Status fields for ``sessions.json`` (see ``docs/octopus-sessions.md``)."""
    upcoming = sorted(snapshot.upcoming, key=lambda e: e.start)
    nxt = upcoming[0] if upcoming else None
    recent = [o for o in snapshot.outcomes if now - o.start < timedelta(days=90)]
    last = max(snapshot.outcomes, key=lambda o: o.start, default=None)
    return {
        "next": f"{nxt.kind} {when(nxt, now)}" if nxt else "none announced",
        "reward": f"{nxt.reward_points} pts/kWh" if nxt and nxt.reward_points else "-",
        "joined": ("yes" if nxt.joined else "no") if nxt and nxt.joined is not None else "-",
        "last_result": (f"{last.points} pts" if last.status != "MISSING_READINGS" else "missing readings")
        if last
        else "-",
        "missing_90d": f"{sum(o.status == 'MISSING_READINGS' for o in recent)} of {len(recent)}",
        "points": str(snapshot.points_balance) if snapshot.points_balance is not None else "-",
    }


def load_key() -> str:
    """The API key from ``OCTOPUS_API_KEY_FILE`` or ``OCTOPUS_API_KEY``."""
    path = env_str("OCTOPUS_API_KEY_FILE", "")
    key = Path(path).read_text().strip() if path else env_str("OCTOPUS_API_KEY", "")
    if not key:
        raise ConfigError("set OCTOPUS_API_KEY_FILE or OCTOPUS_API_KEY")
    return key


def main() -> None:
    """Entry point: one check (meant for a 30-minute timer)."""
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    outputs = Outputs.from_env()
    config = json.loads(Path(env_str("OCTOPUS_CONFIG", "/etc/octopus/config.json")).read_text())
    mpan = next(str(m["mpan"]) for m in config["electricity"] if not m.get("export"))
    flex = OctopusFlex(load_key(), str(config["account"]), mpan)
    state_file = Path(env_str("SESSIONS_STATE_FILE", "/var/lib/octopus-sessions/state.json"))
    now = datetime.now(UTC)
    snapshot = flex.snapshot(
        env_str("SESSION_TYPES", "TURN_DOWN TURN_UP").split(),
        env_str("CAMPAIGNS", "free_electricity").split(),
        now,
    )
    state, alerts = process(snapshot, State.load(state_file), now, env_float("OCTOPOINTS_PER_POUND", 800))
    notifier = Notifier(outputs.ntfy_url)
    for title, message, priority in alerts:
        log.info("%s: %s", title, message)
        notifier.send(title, message, priority=priority, tags="zap")
    state.save(state_file)
    write_status(
        outputs.status_dir, "sessions", display(snapshot, now) | {"checked": f"{datetime.now():%H:%M}"}
    )


if __name__ == "__main__":
    main()
