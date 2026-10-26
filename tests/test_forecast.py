from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta

import pytest

from homelab_tools.common.config import ConfigError
from homelab_tools.common.metrics import VictoriaMetrics
from homelab_tools.forecast.solar import (
    ForecastJob,
    Plane,
    combine,
    curve_samples,
    estimate_url,
    latest_value,
    load_curve,
    parse_planes,
    status,
)


def response(day: str, scale: float = 1.0) -> dict:
    """Shaped like a real Forecast.Solar reply (ISO timestamps, Wh per day)."""
    watts = {
        f"{day}T{h:02d}:00:00+00:00": round(scale * max(0, 2500 - 300 * abs(h - 12))) for h in range(7, 18)
    }
    next_day = (date.fromisoformat(day) + timedelta(days=1)).isoformat()
    watts[f"{next_day}T12:00:00+00:00"] = round(scale * 1200)
    return {
        "result": {"watts": watts, "watt_hours_day": {day: 15299 * scale, next_day: 5787 * scale}},
        "message": {"type": "success"},
    }


def test_parse_planes() -> None:
    assert parse_planes("35,0,6") == [Plane(35, 0, 6)]
    assert parse_planes("35,-90,3; 35,90,3") == [Plane(35, -90, 3), Plane(35, 90, 3)]
    for bad in ("", "35,0", "35,0,0", "95,0,6", "a,b,c"):
        with pytest.raises(ConfigError):
            parse_planes(bad)


def test_estimate_url() -> None:
    url = estimate_url(51.5, -0.12, Plane(35, 0, 6))
    assert url == "https://api.forecast.solar/estimate/51.5/-0.12/35/0/6?time=iso8601"
    assert "/KEY/estimate/" in estimate_url(1, 2, Plane(30, -90, 2.5), api_key="KEY")


def test_combine_sums_planes() -> None:
    f = combine([response("2026-10-25"), response("2026-10-25", scale=0.5)])
    assert f.day_wh[date(2026, 10, 25)] == pytest.approx(15299 * 1.5)
    assert f.watts[datetime(2026, 10, 25, 12, tzinfo=UTC)] == pytest.approx(2500 * 1.5)
    assert f.peak(date(2026, 10, 25)) == datetime(2026, 10, 25, 12, tzinfo=UTC)
    assert f.peak(date(2026, 12, 1)) is None


def test_status_and_curve(utc_zone) -> None:
    f = combine([response("2026-10-25")])
    s = status(f, date(2026, 10, 25), actual_kwh=15.62)
    assert s == {
        "today_kwh": "15.3 kWh",
        "today_actual": "15.6 kWh",
        "tomorrow_kwh": "5.8 kWh",
        "tomorrow_peak": "12:00",
    }
    samples = curve_samples(f, date(2026, 10, 25))
    assert sum(1 for s in samples if "w" in s.fields) == 11  # only today's points
    (total,) = [s for s in samples if "day_kwh" in s.fields]
    assert total.fields["day_kwh"] == pytest.approx(15.299)
    assert total.time is not None and total.time.hour == 12
    assert status(f, date(2026, 12, 1), None)["today_kwh"] == "-"


def test_run_stores_the_curve_once_per_day(tmp_path, http) -> None:
    job = ForecastJob(
        51.5,
        -0.12,
        [Plane(35, 0, 6)],
        VictoriaMetrics("http://vm", http),
        str(tmp_path),
        tmp_path / "state.json",
        actual=lambda: 12.0,
        fetch=lambda url: response("2026-10-25"),
    )
    job.run(today=date(2026, 10, 25))
    writes = len(http.requests)
    assert writes == 2  # the curve, then the query-cache reset
    assert json.loads((tmp_path / "state.json").read_text()) == {"stored": "2026-10-25"}
    status_file = json.loads((tmp_path / "forecast.json").read_text())
    assert status_file["tomorrow_kwh"] == "5.8 kWh" and status_file["today_actual"] == "12.0 kWh"
    job.run(today=date(2026, 10, 25))  # later the same day: status only, no second curve
    assert len(http.requests) == writes
    job.run(today=date(2026, 10, 26))  # next day: stored again
    assert len(http.requests) == writes + 2


def test_latest_value() -> None:
    reply = {"data": {"result": [{"value": [1, "15.6"]}]}}
    assert latest_value("http://vm", "solis_pv_today_kwh", lambda url: reply) == 15.6
    assert latest_value("http://vm", "m", lambda url: {"data": {"result": []}}) is None
    assert latest_value("", "m") is None


@pytest.fixture
def utc_zone(monkeypatch):
    """Run a test with local time = UTC, restoring the real zone afterwards."""
    import time

    monkeypatch.setenv("TZ", "UTC")
    time.tzset()
    yield
    monkeypatch.undo()
    time.tzset()


def test_daily_total_lands_on_its_own_utc_day_in_summer() -> None:
    """Regression: a local-midnight stamp in BST is 23:00 UTC the day before."""
    import os
    import time

    old = os.environ.get("TZ")
    os.environ["TZ"] = "Europe/London"
    time.tzset()
    try:
        f = combine([response("2026-10-25")])  # the last day of BST
        (total,) = [s for s in curve_samples(f, date(2026, 10, 25)) if "day_kwh" in s.fields]
        assert total.time is not None and total.time.astimezone(UTC).date() == date(2026, 10, 25)
    finally:
        if old is None:
            del os.environ["TZ"]
        else:
            os.environ["TZ"] = old
        time.tzset()


def test_run_saves_the_full_curve_every_time(tmp_path, http) -> None:
    curve = tmp_path / "curve" / "curve.json"
    job = ForecastJob(
        51.5,
        -0.12,
        [Plane(35, 0, 6)],
        VictoriaMetrics("", http),
        "",
        tmp_path / "state.json",
        fetch=lambda url: response("2026-10-25"),
        curve_file=curve,
    )
    job.run(today=date(2026, 10, 25))
    fetched, watts = load_curve(curve)
    assert fetched.tzinfo is not None and len(watts) == 12
    assert watts[datetime(2026, 10, 26, 12, tzinfo=UTC)] == 1200  # tomorrow's points are kept too
    curve.write_text('{"something": "else"}')
    with pytest.raises(ValueError, match="not a forecast curve"):
        load_curve(curve)
