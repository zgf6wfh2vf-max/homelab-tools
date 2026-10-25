from __future__ import annotations

import io
import json

import pytest

from homelab_tools.common.metrics import VictoriaMetrics
from homelab_tools.common.notify import Notifier
from homelab_tools.tado.auth import AccessToken, TokenStore, device_login
from homelab_tools.tado.model import device_problems, display, new_problems, parse_zone_state, zone_key
from homelab_tools.tado.monitor import TadoClient, TadoMonitor

ZONE_STATE = {
    "setting": {"type": "HEATING", "power": "ON", "temperature": {"celsius": 21.5}},
    "overlay": None,
    "openWindow": None,
    "activityDataPoints": {"heatingPower": {"percentage": 37.0}},
    "sensorDataPoints": {"insideTemperature": {"celsius": 22.02}, "humidity": {"percentage": 56.6}},
}

ZONES = [
    {
        "id": 10,
        "name": "Downstairs",
        "devices": [
            {
                "deviceType": "RU01",
                "serialNo": "RU1",
                "shortSerialNo": "RU1",
                "batteryState": "NORMAL",
                "connectionState": {"value": True},
            },
            {
                "deviceType": "VA01",
                "serialNo": "VA1",
                "shortSerialNo": "VA1",
                "batteryState": "LOW",
                "connectionState": {"value": True},
            },
        ],
    },
    {
        "id": 9,
        "name": "Upstairs",
        "devices": [
            {
                "deviceType": "VA02",
                "serialNo": "VA2",
                "shortSerialNo": "VA2",
                "batteryState": "NORMAL",
                "connectionState": {"value": False},
            },
        ],
    },
]


# --- model -------------------------------------------------------------------------------------


def test_parse_zone_state() -> None:
    z = parse_zone_state("Downstairs", ZONE_STATE)
    assert (z.inside_c, z.humidity_pct, z.target_c, z.heating_pct) == (22.02, 56.6, 21.5, 37.0)
    assert not z.open_window and not z.manual
    off = parse_zone_state(
        "Spare", {"setting": {"power": "OFF"}, "openWindowDetected": True, "overlay": {"x": 1}}
    )
    assert off.target_c is None and off.inside_c is None and off.open_window and off.manual


def test_device_problems_and_dedup() -> None:
    problems = device_problems(ZONES)
    assert [p.key for p in problems] == ["VA1:battery", "VA2:offline"]
    assert problems[0].title == "tado radiator valve battery low"
    assert "Downstairs radiator valve (VA1)" in problems[0].message
    fresh, alerted = new_problems(problems, set())
    assert len(fresh) == 2
    fresh, alerted = new_problems(problems, alerted)
    assert fresh == []
    _, alerted = new_problems([], alerted)  # batteries replaced
    assert alerted == set()


def test_display_strings() -> None:
    zones = [
        parse_zone_state("Downstairs", ZONE_STATE),
        parse_zone_state("Living Room", {"openWindow": {"x": 1}}),
    ]
    d = display(zones, 12.23, "PARTLY_CLOUDY", 13, device_problems(ZONES))
    assert (
        d["downstairs_temp"] == "22.0 °C"
        and d["downstairs_heat"] == "37%"
        and d["downstairs_mode"] == "schedule"
    )
    assert d["living_heat"] == "open window" and d["living_target"] == "off"
    assert (d["outside"], d["weather"], d["batteries"], d["offline"]) == (
        "12.2 °C",
        "partly cloudy",
        "1 low",
        "1",
    )
    assert zone_key("") == "zone"


# --- auth --------------------------------------------------------------------------------------


def test_token_store_round_trip_is_private(tmp_path) -> None:
    store = TokenStore(tmp_path / "t" / "token.json")
    store.save("r1")
    assert store.load() == "r1"
    assert (store.path.stat().st_mode & 0o777) == 0o600


def test_access_token_refreshes_and_saves_rotated_token(tmp_path) -> None:
    store = TokenStore(tmp_path / "token.json")
    store.save("r1")
    calls = []

    def post(url, form):
        calls.append(form["refresh_token"])
        return {"access_token": f"a{len(calls)}", "refresh_token": f"r{len(calls) + 1}", "expires_in": 600}

    now = [1000.0]
    token = AccessToken(store, post, clock=lambda: now[0])
    assert token.get() == "a1" and store.load() == "r2"
    assert token.get() == "a1"  # cached while valid
    now[0] += 590  # within a minute of expiry
    assert token.get() == "a2" and calls == ["r1", "r2"]


def test_access_token_reports_revoked_login(tmp_path) -> None:
    store = TokenStore(tmp_path / "token.json")
    store.save("old")
    with pytest.raises(PermissionError, match="tado-auth"):
        AccessToken(store, lambda url, form: {"error": "invalid_grant"}).get()


def test_device_login_waits_for_approval(tmp_path) -> None:
    replies = iter(
        [
            {
                "device_code": "D",
                "user_code": "ABC123",
                "verification_uri_complete": "https://login/x",
                "expires_in": 300,
                "interval": 5,
            },
            {"error": "authorization_pending"},
            {"error": "slow_down"},
            {"refresh_token": "R"},
        ]
    )
    shown: list[str] = []
    store = TokenStore(tmp_path / "token.json")
    assert device_login(
        store, lambda url, form: next(replies), shown.append, sleep=lambda s: None, clock=lambda: 0
    )
    assert store.load() == "R" and "https://login/x" in shown[0] and "ABC123" in shown[1]


def test_device_login_gives_up_on_error(tmp_path) -> None:
    replies = iter(
        [
            {"device_code": "D", "user_code": "U", "verification_uri_complete": "u", "expires_in": 300},
            {"error": "access_denied"},
        ]
    )
    assert not device_login(
        TokenStore(tmp_path / "t"),
        lambda u, f: next(replies),
        lambda s: None,
        sleep=lambda s: None,
        clock=lambda: 0,
    )


# --- monitor -----------------------------------------------------------------------------------


class FakeTado:
    """Serves canned API responses by path."""

    def __init__(self) -> None:
        self.paths: list[str] = []
        self.responses = {
            "/me": {"homes": [{"id": 1234}]},
            "/homes/1234/zones": ZONES,
            "/homes/1234/zoneStates": {"zoneStates": {"10": ZONE_STATE}},
            "/homes/1234/weather": {
                "outsideTemperature": {"celsius": 12.2},
                "weatherState": {"value": "SUN"},
                "solarIntensity": {"percentage": 40},
            },
        }

    def __call__(self, request):
        path = request.full_url.removeprefix("https://my.tado.com/api/v2")
        self.paths.append(path)
        assert request.get_header("Authorization") == "Bearer tok"
        return io.BytesIO(json.dumps(self.responses[path]).encode())


def test_monitor_round(tmp_path, http) -> None:
    api = FakeTado()
    mon = TadoMonitor(
        TadoClient(lambda: "tok", api),
        Notifier("https://ntfy/t", http),
        VictoriaMetrics("http://vm", http),
        str(tmp_path),
        tmp_path / "state.json",
        device_every=2,
    )
    mon.poll_once()
    mon.poll_once()
    assert api.paths.count("/homes/1234/zones") == 1  # devices only every 2nd round
    alerts = [r.get_header("Title") for r in http.requests if r.get_header("Title")]
    assert alerts == ["tado radiator valve battery low", "tado radiator valve offline"]
    lines = "\n".join(http.bodies())
    assert (
        "tado,zone=Downstairs inside_c=22.02" in lines and "tado_weather outside_c=12.2,solar_pct=40" in lines
    )
    status = json.loads((tmp_path / "tado.json").read_text())
    assert status["downstairs_heat"] == "37%" and status["devices"] == "3"
    assert json.loads((tmp_path / "state.json").read_text())["alerted"] == ["VA1:battery", "VA2:offline"]
