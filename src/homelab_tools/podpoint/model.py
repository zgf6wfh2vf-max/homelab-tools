"""Charger state and charge-session logic for Pod Point, on plain data (no network, no library).

Why the "real" state needs care: the pod's own status says ``Charging`` as soon as a car is plugged
in - even while the charger is holding off for its schedule (e.g. until a cheap night tariff). The
connectivity endpoint is more precise: each connector reports a ``charging_state`` and the EVSE an
``energy_offer_status`` explaining why no energy is offered:

=================  ===========================================================
charging_state     meaning
=================  ===========================================================
CHARGING           energy is flowing
SUSPENDED_EVSE     the charger is withholding energy (schedule, smart mode...)
SUSPENDED_EV       the car is not accepting energy (full, or its own timer)
(empty) / IDLE     nothing plugged in
=================  ===========================================================
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime

from homelab_tools.common.timefmt import duration, when


@dataclass(frozen=True)
class ChargerState:
    """What the charger is doing now.

    Attributes:
        key: Stable machine-readable state: ``charging``, ``waiting``, ``plugged``, ``available``,
            ``offline`` or the pod's own status.
        text: Human description, e.g. ``"Waiting for 00:30"``.
    """

    key: str
    text: str


def charger_state(
    pod_status: str,
    charging_state: str,
    connectivity: str,
    offer_reason: str = "",
    offer_until: datetime | None = None,
) -> ChargerState:
    """Work out the real charger state from the API's fields.

    Args:
        pod_status: ``key_name`` of the pod's first status (``available``, ``charging``, ...).
        charging_state: The connector's ``charging_state`` (see module docstring).
        connectivity: The EVSE's ``connectivity_status`` (``ONLINE`` / ``OFFLINE``); empty if unknown.
        offer_reason: ``energy_offer_status.reason`` (``CHARGE_SCHEDULE`` when waiting for a schedule).
        offer_until: ``energy_offer_status.until`` - when the schedule next allows charging.
    """
    if connectivity and connectivity != "ONLINE":
        return ChargerState("offline", "Charger offline")
    if charging_state == "CHARGING":
        return ChargerState("charging", "Charging")
    if charging_state == "SUSPENDED_EVSE":
        if offer_reason == "CHARGE_SCHEDULE" and offer_until is not None:
            return ChargerState("waiting", f"Waiting for {offer_until.astimezone():%H:%M}")
        return ChargerState("waiting", "Plugged in, paused")
    if charging_state == "SUSPENDED_EV":
        return ChargerState("plugged", "Plugged in (car not taking charge)")
    status = (pod_status or "unknown").lower()
    if status == "available" or charging_state in ("", "IDLE", "AVAILABLE"):
        return ChargerState("available", "Not plugged in")
    return ChargerState(status, status.replace("-", " ").capitalize())


@dataclass(frozen=True)
class Charge:
    """One charging session as reported by Pod Point.

    Attributes:
        id: Increasing session id.
        kwh: Energy delivered.
        cost_p: Pod Point's cost estimate in pence, using the electricity rate set in the app.
        starts: Plug-in time.
        ends: Unplug/finish time, or ``None`` while the session is still open.
        charging_seconds: Time energy was actually flowing (excludes waiting).
    """

    id: int
    kwh: float
    cost_p: float
    starts: datetime
    ends: datetime | None
    charging_seconds: int


def new_finished(charges: Iterable[Charge], last_id: int | None) -> list[Charge]:
    """Finished sessions newer than ``last_id``, oldest first.

    ``last_id`` is ``None`` on the very first run: nothing counts as new then, so historic sessions
    are not announced.
    """
    if last_id is None:
        return []
    return sorted((c for c in charges if c.ends and c.id > last_id), key=lambda c: c.id)


def completion_message(c: Charge, now: datetime | None = None) -> str:
    """Notification text for a finished session."""
    assert c.ends is not None
    return (
        f"{c.kwh:.1f} kWh in {duration(c.charging_seconds)} of charging "
        f"(plugged in {when(c.starts, now)} - {when(c.ends, now)}), "
        f"about £{c.cost_p / 100:.2f} at your app's rate."
    )


def month_sessions(charges: Iterable[Charge], now: datetime) -> list[Charge]:
    """Finished sessions that ended in the same local calendar month as ``now``."""
    local_now = now.astimezone()
    return [
        c
        for c in charges
        if c.ends
        and (c.ends.astimezone().year, c.ends.astimezone().month) == (local_now.year, local_now.month)
    ]


def display(
    state: ChargerState,
    connectivity: str,
    signal_dbm: int | None,
    charges: list[Charge],
    month: list[Charge],
    now: datetime,
) -> dict[str, str]:
    """Display strings for a dashboard. ``charges`` is newest first."""
    finished = [c for c in charges if c.ends]
    last = finished[0] if finished else None
    return {
        "state": state.text,
        "online": (connectivity or "?").capitalize(),
        "signal": f"{signal_dbm} dBm" if signal_dbm else "-",
        "last_kwh": f"{last.kwh:.1f} kWh" if last else "-",
        "last_cost": f"£{last.cost_p / 100:.2f}" if last else "-",
        "last_when": when(last.ends, now) if last and last.ends else "-",
        "last_time": duration(last.charging_seconds) if last else "-",
        "month_kwh": f"{sum(c.kwh for c in month):.0f} kWh",
        "month_cost": f"£{sum(c.cost_p for c in month) / 100:.2f}",
        "month_n": str(len(month)),
    }
