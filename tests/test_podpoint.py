from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace as Obj

import pytest

from homelab_tools.podpoint.model import (
    Charge,
    charger_state,
    completion_message,
    display,
    month_sessions,
    new_finished,
)
from homelab_tools.podpoint.monitor import read_state, session_sample, to_charge

NOW = datetime(2026, 1, 25, 15, 0, tzinfo=UTC)


def charge(i: int, days_ago: float, kwh: float = 22.2, cost: float = 122, open_: bool = False) -> Charge:
    ends = NOW - timedelta(days=days_ago)
    return Charge(i, kwh, cost, ends - timedelta(hours=6), None if open_ else ends, 10595)


@pytest.mark.parametrize(
    ("args", "key", "text"),
    [
        (("charging", "CHARGING", "ONLINE"), "charging", "Charging"),
        (
            (
                "charging",
                "SUSPENDED_EVSE",
                "ONLINE",
                "CHARGE_SCHEDULE",
                datetime(2026, 1, 26, 0, 30, tzinfo=UTC),
            ),
            "waiting",
            "Waiting for ",
        ),
        (("charging", "SUSPENDED_EVSE", "ONLINE"), "waiting", "Plugged in, paused"),
        (("charging", "SUSPENDED_EV", "ONLINE"), "plugged", "Plugged in (car not taking charge)"),
        (("available", "", "ONLINE"), "available", "Not plugged in"),
        (("charging", "CHARGING", "OFFLINE"), "offline", "Charger offline"),
        (("out-of-service", "FAULTED", ""), "out-of-service", "Out of service"),
    ],
)
def test_charger_state(args: tuple, key: str, text: str) -> None:
    state = charger_state(*args)
    assert state.key == key and state.text.startswith(text)


def test_pod_status_alone_would_be_misleading() -> None:
    """The pod says 'charging' while the schedule holds it off; the connector state wins."""
    state = charger_state("charging", "SUSPENDED_EVSE", "ONLINE", "CHARGE_SCHEDULE", NOW)
    assert state.key == "waiting"


def test_new_finished_sessions() -> None:
    charges = [charge(5, 0.1, open_=True), charge(4, 1), charge(3, 2), charge(2, 3)]
    assert new_finished(charges, None) == []  # first run: announce nothing
    assert [c.id for c in new_finished(charges, 2)] == [3, 4]  # oldest first, open session excluded


def test_month_sessions_and_display() -> None:
    charges = [charge(9, 1), charge(8, 3, kwh=30, cost=165), charge(7, 40)]  # 7 is last month
    month = month_sessions(charges, NOW)
    assert [c.id for c in month] == [9, 8]
    d = display(charger_state("available", "", "ONLINE"), "ONLINE", -61, charges, month, NOW)
    assert d["state"] == "Not plugged in" and d["online"] == "Online" and d["signal"] == "-61 dBm"
    assert (d["last_kwh"], d["last_cost"], d["last_time"]) == ("22.2 kWh", "£1.22", "2h 56m")
    assert (d["month_n"], d["month_kwh"], d["month_cost"]) == ("2", "52 kWh", "£2.87")
    empty = display(charger_state("available", "", ""), "", None, [], [], NOW)
    assert empty["last_kwh"] == "-" and empty["online"] == "?" and empty["signal"] == "-"


def test_completion_message() -> None:
    msg = completion_message(charge(1, 0.2), NOW)
    assert msg.startswith("22.2 kWh in 2h 56m of charging") and "£1.22" in msg


# --- adapters for podpointclient objects ------------------------------------------------------


def test_to_charge_and_session_sample() -> None:
    obj = Obj(
        id=7,
        kwh_used=22.2,
        energy_cost=122,
        starts_at="2026-01-23T01:34:22+00:00",
        ends_at=datetime(2026, 1, 23, 7, 43, tzinfo=UTC),
        charging_duration=Obj(raw=10595),
    )
    c = to_charge(obj)
    assert c == Charge(7, 22.2, 122.0, datetime(2026, 1, 23, 1, 34, 22, tzinfo=UTC), obj.ends_at, 10595)
    line = session_sample(c).to_line()
    assert line is not None and line.startswith(
        "ev session_kwh=22.2,session_cost_p=122.0,session_charging_s=10595 "
    )


def test_read_state_from_library_objects() -> None:
    pod = Obj(statuses=[Obj(key_name="charging")])
    connectivity = Obj(
        evses=[
            Obj(
                connectivity_state=Obj(connectivity_status="ONLINE", signal_strength=-61),
                connectors=[Obj(charging_state="SUSPENDED_EVSE")],
                energy_offer_status=Obj(reason="CHARGE_SCHEDULE", until="2026-01-26T00:30:00+00:00"),
            )
        ]
    )
    state, online, signal = read_state(pod, connectivity)
    assert state.key == "waiting" and online == "ONLINE" and signal == -61
    state, online, signal = read_state(Obj(statuses=[]), Obj(evses=[]))
    assert state.key == "available" and online == "" and signal is None
