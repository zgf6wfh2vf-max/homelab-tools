from __future__ import annotations

import os
import time as systime
from datetime import UTC, datetime
from pathlib import Path

import pytest

from homelab_tools.octopus.sessions import (
    BACKEND,
    MAIN,
    OctopusFlex,
    OctopusGraphQLError,
    State,
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
    }
