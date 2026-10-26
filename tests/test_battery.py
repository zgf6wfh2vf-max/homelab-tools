from __future__ import annotations

import json
import os
import time as systime
from datetime import date, datetime, time, timedelta
from pathlib import Path

import pytest

from homelab_tools.battery.check import (
    Battery,
    Outlook,
    Settings,
    evening_message,
    load_profile,
    morning_message,
    outlook_display,
    parse_window,
    peak_period,
    round_to_slot,
    run_evening,
    run_morning,
    simulate,
    slots,
    solar_kwh,
    tomorrow_outlook,
    update_status,
)
from homelab_tools.common.config import ConfigError
from homelab_tools.forecast.solar import Forecast, save_curve

GO = (time(0, 30), time(5, 30))
EXACT = Battery(10, full_pct=100, reserve_pct=20, efficiency=1.0)


@pytest.fixture(autouse=True)
def london():
    """Local-time logic is tested in the UK time zone."""
    old = os.environ.get("TZ")
    os.environ["TZ"] = "Europe/London"
    systime.tzset()
    yield
    if old is None:
        del os.environ["TZ"]
    else:
        os.environ["TZ"] = old
    systime.tzset()


def local(day: date, hh: int, mm: int = 0) -> datetime:
    return datetime.combine(day, time(hh, mm)).astimezone()


def test_parse_window() -> None:
    assert parse_window("00:30-05:30") == GO
    assert parse_window("23:30 - 05:30") == (time(23, 30), time(5, 30))
    for bad in ("00:30", "night", "05:30-05:30"):
        with pytest.raises(ConfigError):
            parse_window(bad)


def test_peak_period_and_slots() -> None:
    day = date(2026, 10, 27)
    start, end = peak_period(day, GO)
    assert (start, end) == (local(day, 5, 30), local(date(2026, 10, 28), 0, 30))
    assert len(slots(start, end)) == 38  # 19 hours
    assert peak_period(day, (time(23, 30), time(5, 30)))[1] == local(day, 23, 30)  # Intelligent Go style


def test_solar_kwh_integrates_the_curve() -> None:
    day = date(2026, 10, 27)
    flat = [(local(day, 9), 2000.0), (local(day, 15), 2000.0)]
    assert solar_kwh(flat, local(day, 10)) == pytest.approx(1.0)  # 2 kW for half an hour
    assert solar_kwh(flat, local(day, 20)) == 0.0  # after the forecast ends
    ramp = [(local(day, 10), 0.0), (local(day, 11), 2000.0)]
    assert solar_kwh(ramp, local(day, 10)) == pytest.approx(0.25)  # average 500 W over the first half


def test_load_profile_is_the_median_per_half_hour() -> None:
    day = date(2026, 10, 20)
    rows = [(local(day + timedelta(days=d), 18, 30).timestamp(), w) for d, w in enumerate([400, 500, 3000])]
    rows.append((local(day, 19).timestamp(), 250))
    assert load_profile(rows) == {time(18, 0): 500, time(18, 30): 250}  # an EV-charging outlier is ignored


def test_simulate_dull_day_runs_out() -> None:
    day = date(2026, 10, 27)
    period = peak_period(day, GO)
    outlook = simulate(EXACT, period, [0.0] * 38, [1.0] * 38)  # 2 kW all day, no sun
    assert outlook.runs_out == local(day, 9, 30)  # 8 kWh usable lasts 8 half-hours
    assert outlook.grid_kwh == pytest.approx(30) and outlook.export_kwh == 0


def test_simulate_sunny_day_lasts_and_exports() -> None:
    period = peak_period(date(2026, 10, 27), GO)
    solar = [0.0] * 8 + [2.0] * 12 + [0.0] * 18
    outlook = simulate(EXACT, period, solar, [0.3] * 38)
    assert outlook.runs_out is None and outlook.grid_kwh == 0
    # 2.4 kWh used before the sun refilled the battery, so 12 x 1.7 - 2.4 is exported
    assert outlook.export_kwh == pytest.approx(12 * 1.7 - 2.4)


def test_simulate_applies_losses_and_partial_slots() -> None:
    period = peak_period(date(2026, 10, 27), GO)
    lossy = simulate(Battery(10, efficiency=0.9), period, [0.0] * 38, [1.0] * 38)
    assert lossy.grid_kwh == pytest.approx(38 - 8 * 0.9)
    half = simulate(EXACT, period, [0.0] * 38, [0.0] * 4 + [16.0] + [0.0] * 33)
    assert half.runs_out == period[0] + timedelta(hours=2, minutes=15)  # half way through slot 5


def test_tomorrow_outlook_needs_history_and_forecast() -> None:
    day = date(2026, 10, 27)
    profile = {s.time(): 600.0 for s in slots(*peak_period(day, GO))}
    curve = {local(day, 8): 0.0, local(day, 12): 3000.0, local(day, 16): 0.0}
    outlook = tomorrow_outlook(day, GO, EXACT, curve, profile)
    assert outlook.load_kwh == pytest.approx(0.3 * 38)
    assert outlook.solar_kwh == pytest.approx(12.0, rel=0.01)  # triangle: 3 kW peak over 8 hours
    with pytest.raises(ValueError, match="no house-load history for 1 half-hours"):
        tomorrow_outlook(day, GO, EXACT, curve, {k: v for k, v in profile.items() if k != time(18, 0)})
    with pytest.raises(ValueError, match="does not cover 2026-10-28"):
        tomorrow_outlook(date(2026, 10, 28), GO, EXACT, curve, profile)


def test_messages() -> None:
    day = date(2026, 10, 27)
    outlook = Outlook(local(day, 5, 30), 2.04, 14.6, 4.53, 0.0, local(day, 18, 41))
    assert round_to_slot(outlook.runs_out) == local(day, 18, 30)
    assert evening_message(outlook, 5.0, 26.38, GO[0]) is None
    title, message = evening_message(outlook, 1.0, 26.38, GO[0])
    assert title == "Battery will run low Tuesday"
    assert message == (
        "2.0 kWh of solar forecast, against about 15 kWh of house use. The battery will probably run out "
        "around 18:30, leaving 4.5 kWh to buy at the peak rate (about £1.20). Heavy appliances are cheapest "
        "after 00:30."
    )
    assert "about £" not in evening_message(outlook, 1.0, None, GO[0])[1]
    assert outlook_display(outlook)["outlook_battery"] == "runs out 18:30"
    assert morning_message(97, 90) is None and morning_message(None, 90) is None
    assert morning_message(31, 90)[0] == "Battery did not charge overnight"


def settings(tmp_path: Path) -> Settings:
    return Settings(EXACT, GO, 90, 1.0, tmp_path / "curve.json", "load_w", "soc", "rate_p", 7)


def test_run_morning_looks_back_to_the_window_opening(tmp_path: Path) -> None:
    asked = []

    def query(expr: str, at: float) -> float | None:
        asked.append(expr)
        return 42.0

    fields, alert = run_morning(settings(tmp_path), query, local(date(2026, 10, 27), 6, 15))
    assert asked == ["max_over_time(soc[20700s])"]  # 00:30 to 06:15
    assert fields["overnight"] == "42%" and alert and "42%" in alert[1]


def test_run_evening_end_to_end(tmp_path: Path) -> None:
    day = date(2026, 10, 27)
    now = local(day - timedelta(days=1), 20)
    save_curve(
        Forecast({local(day, 8): 0.0, local(day, 12): 600.0, local(day, 16): 0.0}, {}),
        tmp_path / "curve.json",
    )
    rows = [(s.timestamp() + 1800, 1000.0) for s in slots(local(day - timedelta(days=1), 0), now)]
    rows += [(s.timestamp() + 1800, 1000.0) for s in slots(now, local(day - timedelta(days=1), 23, 59))]
    fields, alert = run_evening(settings(tmp_path), lambda e, a: 26.38, lambda *a: rows, day, now)
    assert fields["outlook_battery"] == "runs out 16:00" and fields["outlook_solar"] == "2.4 kWh"
    assert alert and alert[0] == "Battery will run low Tuesday"


def test_update_status_merges_both_modes(tmp_path: Path) -> None:
    update_status(str(tmp_path), {"overnight": "98%"})
    update_status(str(tmp_path), {"outlook_battery": "lasts"})
    assert json.loads((tmp_path / "battery.json").read_text()) == {
        "overnight": "98%",
        "outlook_battery": "lasts",
    }
    update_status("", {"x": "y"})  # disabled: no error
