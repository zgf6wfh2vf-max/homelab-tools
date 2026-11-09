from __future__ import annotations

import os
import time as systime
from datetime import UTC, datetime, time, timedelta
from pathlib import Path

import pytest

from homelab_tools.battery.check import Battery, outlook_between
from homelab_tools.octopus.sessions import (
    BACKEND,
    MAIN,
    Event,
    OctopusFlex,
    OctopusGraphQLError,
    SessionBattery,
    Snapshot,
    State,
    battery_alerts,
    display,
    process,
)


@pytest.fixture(autouse=True)
def london():
    old = os.environ.get("TZ")
    os.environ["TZ"] = "Europe/London"
    systime.tzset()
    yield
    if old is None:
        del os.environ["TZ"]
    else:
        os.environ["TZ"] = old
    systime.tzset()


NOW = datetime(2026, 10, 27, 9, 0, tzinfo=UTC)  # Tuesday


def event(event_id: int, start_hour: str, status: str, kind: str, points: int) -> dict:
    """One saving-sessions event, one hour long, as the backend API returns it."""
    end_hour = f"{start_hour[:-2]}{int(start_hour[-2:]) + 1:02d}"
    return {
        "id": event_id,
        "code": f"EVENT_{event_id}",
        "startAt": f"{start_hour}:00:00+00:00",
        "endAt": f"{end_hour}:00:00+00:00",
        "status": status,
        "eventType": kind,
        "rewardPerKwhInOctoPoints": points,
    }


# Shaped like the real backend response (savingSessions)
SESSIONS = {
    "events": [
        event(7517, "2026-10-19T17", "DONE", "TURN_DOWN", 82),
        event(7600, "2026-10-27T17", "UPCOMING", "TURN_DOWN", 80),
        event(7601, "2026-10-28T13", "UPCOMING", "TURN_UP", 40),
        event(7602, "2026-10-31T09", "UPCOMING", "WEEKEND_HAPPY_HOUR", 0),
    ],
    "account": {
        "hasJoinedCampaign": True,
        "tokenBalance": 7,
        "signedUpMeterPoint": {"mpan": "1234567890123"},
        "joinedEvents": [
            {"eventId": 7517, "startAt": "2026-10-19T17:00:00+00:00", "endAt": "2026-10-19T18:00:00+00:00",
             "eventType": "TURN_DOWN", "eventStatus": "DONE", "resultsStatus": "MISSING_READINGS",
             "rewardGivenInOctoPoints": 0},
            {"eventId": 7600, "startAt": "2026-10-27T17:00:00+00:00", "endAt": "2026-10-27T18:00:00+00:00",
             "eventType": "TURN_DOWN", "eventStatus": "UPCOMING", "resultsStatus": None,
             "rewardGivenInOctoPoints": 0},
        ],
    },
}  # fmt: skip
CAMPAIGN = {
    "edges": [
        {
            "node": {
                "code": "FREE_15",
                "startAt": "2025-10-25T11:00:00+00:00",
                "endAt": "2025-10-25T14:00:00+00:00",
            }
        },
        {
            "node": {
                "code": "FREE_16",
                "startAt": "2026-11-01T12:00:00+00:00",
                "endAt": "2026-11-01T13:00:00+00:00",
            }
        },
    ]
}


class FakeAPI:
    """Answers the three GraphQL operations like the real endpoints."""

    def __init__(self, sessions=SESSIONS, campaign_error: bool = False) -> None:
        self.sessions, self.campaign_error, self.calls = sessions, campaign_error, []

    def __call__(self, url, query, variables, token):
        self.calls.append((url, query.split("(")[0].split("{")[0].strip(), dict(variables), token))
        if "obtainKrakenToken" in query:
            return {"data": {"obtainKrakenToken": {"token": "jwt"}}}
        if "savingSessions" in query:
            return {"data": {"savingSessions": self.sessions}}
        if self.campaign_error:
            return {
                "data": {"customerFlexibilityCampaignEvents": None},
                "errors": [{"message": "Campaign not found."}],
            }
        return {"data": {"customerFlexibilityCampaignEvents": CAMPAIGN}}


def flex(api: FakeAPI) -> OctopusFlex:
    return OctopusFlex("sk_live_x", "A-TEST0001", "1234567890123", post=api)


def test_snapshot_reads_both_apis_with_a_token() -> None:
    api = FakeAPI()
    snap = flex(api).snapshot(["TURN_DOWN", "TURN_UP"], ["free_electricity"], NOW)
    assert [c[0] for c in api.calls] == [MAIN, BACKEND, MAIN]
    assert api.calls[0][2] == {"key": "sk_live_x"} and api.calls[1][3] == "jwt"
    assert api.calls[2][2] == {"account": "A-TEST0001", "mpan": "1234567890123", "slug": "free_electricity"}
    kinds = [(e.kind, e.reward_points, e.joined) for e in snap.upcoming]
    assert kinds == [("Power Down", 80, True), ("Power Up", 40, False), ("free electricity", None, None)]
    assert [(o.kind, o.status, o.points) for o in snap.outcomes] == [("Power Down", "MISSING_READINGS", 0)]
    assert snap.points_balance == 7


def test_unknown_campaigns_are_skipped_and_api_errors_raise() -> None:
    snap = flex(FakeAPI(campaign_error=True)).snapshot(["TURN_DOWN"], ["power_ups_xyz"], NOW)
    assert [e.kind for e in snap.upcoming] == ["Power Down"]

    def broken(url, query, variables, token):
        return {"data": None, "errors": [{"message": "Invalid API key."}]}

    with pytest.raises(OctopusGraphQLError, match="obtainKrakenToken: Invalid API key."):
        OctopusFlex("bad", "A-1", "1", post=broken).snapshot([], [], NOW)


def test_first_run_announces_upcoming_but_not_old_results() -> None:
    snap = flex(FakeAPI()).snapshot(["TURN_DOWN", "TURN_UP"], [], NOW)
    state, alerts = process(snap, None, NOW)
    assert [a[0] for a in alerts] == ["Power Down today 17:00-18:00", "Power Up tomorrow 13:00-14:00"]
    assert alerts[0][1] == (
        "Octopus pays 80 Octopoints (about 10p) per kWh you save below your usual level. You've joined it. "
        "Keep the battery charged for that hour and avoid heavy appliances."
    )
    assert "Not joined yet" in alerts[1][1] and "use above" in alerts[1][1]
    assert state.reported == ["session:7517"]  # recorded silently
    assert process(snap, state, NOW)[1] == []  # nothing new


def test_new_results_are_reported_including_missing_readings(tmp_path: Path) -> None:
    snap = flex(FakeAPI()).snapshot(["TURN_DOWN"], [], NOW)
    state = State(announced=["session:7600"], reported=[])
    _, alerts = process(snap, state, NOW)
    assert alerts == [
        (
            "Power Down result",
            "Power Down on Mon 19 Oct: no reward - Octopus says it is missing meter readings for that hour. "
            "If this keeps happening, ask Octopus to check your smart meter data for saving sessions.",
            "low",
        )
    ]
    state.save(tmp_path / "s.json")
    assert State.load(tmp_path / "s.json") == state and State.load(tmp_path / "none.json") is None


def test_display() -> None:
    snap = flex(FakeAPI()).snapshot(["TURN_DOWN", "TURN_UP"], [], NOW)
    assert display(snap, NOW) == {
        "next": "Power Down today 17:00-18:00",
        "reward": "80 pts/kWh",
        "joined": "yes",
        "last_result": "missing readings",
        "missing_90d": "1 of 1",
        "points": "7",
        "meter": "import",
        "battery": "-",
    }


def test_results_still_being_calculated_wait_for_the_final_figure() -> None:
    calculating = {
        "eventId": 7600, "startAt": "2026-10-26T18:00:00+00:00", "endAt": "2026-10-26T19:00:00+00:00",
        "eventType": "TURN_DOWN", "eventStatus": "DONE", "resultsStatus": "CALCULATING",
        "rewardGivenInOctoPoints": 0,
    }  # fmt: skip
    sessions = SESSIONS | {"account": SESSIONS["account"] | {"joinedEvents": [calculating]}}
    later = datetime(2026, 10, 26, 19, 30, tzinfo=UTC)
    snap = flex(FakeAPI(sessions)).snapshot(["TURN_DOWN"], [], later)
    state = State(announced=[], reported=[])
    state, alerts = process(snap, state, later)
    assert not [a for a in alerts if a[0].endswith("result")] and "session:7600" not in state.reported
    assert display(snap, later)["last_result"] == "calculating"
    final = calculating | {"resultsStatus": "COMPLETED", "rewardGivenInOctoPoints": 240}
    sessions = SESSIONS | {"account": SESSIONS["account"] | {"joinedEvents": [final]}}
    snap = flex(FakeAPI(sessions)).snapshot(["TURN_DOWN"], [], later)
    _, alerts = process(snap, state, later)
    assert ("Power Down result", "Power Down on Mon 26 Oct: 240 Octopoints (completed).", "low") in alerts


def test_a_sign_up_on_the_export_meter_is_reported_once_then_cleared() -> None:
    on_export = SESSIONS | {
        "account": SESSIONS["account"] | {"signedUpMeterPoint": {"mpan": "9900000000001"}}
    }
    api = FakeAPI(on_export)
    snap = OctopusFlex(
        "k", "A-TEST0001", "1234567890123", post=api, export_mpans=("9900000000001",)
    ).snapshot(["TURN_DOWN"], [], NOW)
    assert snap.signed_up_meter == "export" and display(snap, NOW)["meter"] == "EXPORT (wrong)"
    state, alerts = process(snap, None, NOW)  # even on the first run
    assert alerts[0][0] == "Octopus sessions: wrong meter" and alerts[0][2] == "high"
    assert "EXPORT meter" in alerts[0][1]
    assert process(snap, state, NOW)[1] == []  # told once
    fixed = flex(FakeAPI()).snapshot(["TURN_DOWN"], [], NOW)
    assert process(fixed, state, NOW)[1] == [
        ("Octopus sessions: meter OK", "Your saving-sessions sign-up is now on your import meter.", "low")
    ]
    assert state.meter_alerted == ""


def test_not_signed_up_means_no_meter_verdict() -> None:
    account = {k: v for k, v in SESSIONS["account"].items() if k != "signedUpMeterPoint"}
    snap = flex(FakeAPI(SESSIONS | {"account": account})).snapshot(["TURN_DOWN"], [], NOW)
    assert snap.signed_up_meter is None and display(snap, NOW)["meter"] == "-"
    assert process(snap, State(), NOW)[1][0][0] != "Octopus sessions: wrong meter"


# Battery check: a 10 kWh battery with a 20% reserve, no losses, no sun, and a house using 1 kW all day.
BATT = Battery(10, full_pct=100, reserve_pct=20, efficiency=1.0)
FLAT_1KW = {time(h, m): 1000.0 for h in range(24) for m in (0, 30)}
SESSION = Event(
    "session:9",
    "Power Down",
    datetime(2026, 10, 27, 17, 0, tzinfo=UTC),
    datetime(2026, 10, 27, 18, 0, tzinfo=UTC),
    149,
    True,
)


def outlook_for(soc_pct: float, now: datetime, event: Event = SESSION) -> SessionBattery:
    """Simulate like the real check, but with a fixed load and no solar."""
    before = outlook_between((now, event.start), BATT, {}, FLAT_1KW, BATT.capacity_kwh * soc_pct / 100)
    during = outlook_between((event.start, event.end), BATT, {}, FLAT_1KW, before.end_kwh)
    return SessionBattery(event, soc_pct, before, during)


def test_battery_warning_when_it_runs_out_before_the_session() -> None:
    now = datetime(2026, 10, 27, 13, 0, tzinfo=UTC)
    snap, state = Snapshot(upcoming=[SESSION]), State()
    # 50% = 3 kWh above the reserve: lasts until 16:00, so the whole session (1 kWh) is from the grid.
    alerts, outlook = battery_alerts(snap, state, now, lambda e, n: outlook_for(50, n), 8, 0.2)
    assert outlook is not None and outlook.during.grid_kwh == pytest.approx(1.0)
    assert alerts == [
        (
            "Battery may not last: Power Down today 17:00-18:00",
            "The battery is at 50% and will probably run down to its reserve around 16:00, before it "
            "starts. That leaves about 1.0 kWh to come from the grid during the session, which counts "
            "against your saving. Put off heavy appliances (dryer, oven, washing, car) until after 18:00.",
            "default",
        )
    ]
    assert display(snap, now, outlook)["battery"] == "short 1.0 kWh"
    # Told once per session.
    assert battery_alerts(snap, state, now, lambda e, n: outlook_for(50, n), 8, 0.2)[0] == []
    # Already at the reserve: say so rather than giving a time.
    flat = battery_alerts(Snapshot(upcoming=[SESSION]), State(), now, lambda e, n: outlook_for(19, n), 8, 0.2)
    assert flat[0][0][1].startswith("The battery is at 19% and is already down to its reserve. That leaves")


def test_no_battery_warning_when_it_lasts_or_the_session_is_not_relevant() -> None:
    now = datetime(2026, 10, 27, 13, 0, tzinfo=UTC)
    # 80% = 6 kWh above the reserve: 4 kWh to 17:00, 1 kWh for the session, 1 kWh to spare.
    alerts, outlook = battery_alerts(
        Snapshot(upcoming=[SESSION]), State(), now, lambda e, n: outlook_for(80, n), 8, 0.2
    )
    assert alerts == [] and outlook is not None and display(Snapshot(), now, outlook)["battery"] == "lasts"

    def never(e: Event, n: datetime) -> SessionBattery:
        raise AssertionError("should not be checked")

    not_joined = Event(SESSION.key, "Power Down", SESSION.start, SESSION.end, 149, False)
    power_up = Event(SESSION.key, "Power Up", SESSION.start, SESSION.end, 149, True)
    for upcoming, at in (
        ([not_joined], now),
        ([power_up], now),
        ([SESSION], SESSION.start - timedelta(hours=9)),  # too far ahead
        ([SESSION], SESSION.start + timedelta(minutes=10)),  # already started
    ):
        assert battery_alerts(Snapshot(upcoming=upcoming), State(), at, never, 8, 0.2) == ([], None)
    # No data: nothing to say.
    assert battery_alerts(Snapshot(upcoming=[SESSION]), State(), now, lambda e, n: None, 8, 0.2) == ([], None)


def test_battery_warned_survives_a_save(tmp_path: Path) -> None:
    state = State(battery_warned=["session:9"])
    state.save(tmp_path / "s.json")
    assert State.load(tmp_path / "s.json") == state
