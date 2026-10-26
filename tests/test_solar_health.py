from __future__ import annotations

import os
import time as systime
from datetime import date, timedelta
from pathlib import Path

import pytest

from homelab_tools.solis.health import (
    DayStats,
    State,
    check_battery,
    check_forecast,
    check_strings,
    collect,
    collect_day,
    run_checks,
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


START = date(2026, 6, 1)


def days_of(
    n: int = 20, s1: float = 9.0, s2: float = 11.0, pv=None, forecast: float = 20.0, soh: float = 99.0
):
    return [
        DayStats(START + timedelta(days=i), s1, s2, pv if pv is not None else s1 + s2, forecast, soh, 6.0)
        for i in range(n)
    ]


def keys(findings) -> dict:
    return {f.key: f.problem for f in findings}


def test_healthy_strings_with_an_uneven_usual_split() -> None:
    assert keys(check_strings(days_of(), 0.2)) == {"string-dead": None, "string-balance": None}


def test_a_dead_string_is_reported_at_once() -> None:
    days = days_of(3)
    days[-1] = DayStats(days[-1].day, 0.1, 10.0)
    assert (
        keys(check_strings(days, 0.2))["string-dead"]
        == "string 1 produced almost nothing (0.1 of 10.1 kWh) on 3 Jun"
    )


def test_a_weakened_string_needs_two_productive_days() -> None:
    days = days_of(12)
    days[-1] = DayStats(days[-1].day, 6.0, 11.0)  # string 1 down ~33% relative to string 2
    assert keys(check_strings(days, 0.2))["string-balance"] is None  # one day could be passing shade
    days[-2] = DayStats(days[-2].day, 6.3, 11.0)
    problem = keys(check_strings(days, 0.2))["string-balance"]
    assert problem and problem.startswith("string 1 has made about 33% less than usual")
    days[-1] = DayStats(days[-1].day, 0.5, 1.0)  # a dull day isn't judged
    assert keys(check_strings(days, 0.2))["string-balance"] is None


def test_too_little_history_for_balance() -> None:
    days = days_of(4)
    days[-1] = DayStats(days[-1].day, 4.0, 11.0)
    days[-2] = DayStats(days[-2].day, 4.0, 11.0)
    assert keys(check_strings(days, 0.2))["string-balance"] is None


def test_production_against_forecast() -> None:
    days = days_of(35, pv=18.0)  # usually 90% of forecast
    finding, week = check_forecast(days, 0.6)
    assert finding.problem is None and week == pytest.approx(0.9)
    poor = days[:-7] + [DayStats(d.day, 4, 4, 8.0, 20.0) for d in days[-7:]]  # 40%
    finding, week = check_forecast(poor, 0.6)
    assert week == pytest.approx(0.4)
    assert finding.problem == (
        "solar made 40% of the forecast over the last week, against a usual 90% - "
        "check the panels, isolator switches and inverter"
    )
    assert check_forecast(poor[-12:], 0.6)[0].problem is None  # needs 14+ days of history


def test_battery_health() -> None:
    assert check_battery(days_of(), 80, 3).problem is None
    ageing = days_of(40)
    ageing[-1] = DayStats(ageing[-1].day, soh_pct=95.0)
    assert check_battery(ageing, 80, 3).problem == "battery health fell from 99% to 95% within 30 days"
    assert check_battery(days_of(soh=78), 80, 3).problem == "battery health is down to 78%"
    assert check_battery([DayStats(START)], 80, 3).problem is None


def test_run_checks_alerts_once_and_clears(tmp_path: Path) -> None:
    state = State()
    days = days_of(3)
    days[-1] = DayStats(days[-1].day, 0.0, 12.0, 12.0, 20.0, 99.0, 6.0)
    alerts, status = run_checks(days, state, 0.2, 0.6, 80, 3, 10)
    assert [a[0] for a in alerts] == ["Solar health: solar string down"]
    assert status["strings"] == "0% / 100%" and status["cycles"] == "1.8 since 1 Jun"
    assert run_checks(days, state, 0.2, 0.6, 80, 3, 10)[0] == []  # told once
    state.save(tmp_path / "s.json")
    assert State.load(tmp_path / "s.json") == state
    alerts, status = run_checks(days_of(3), state, 0.2, 0.6, 80, 3, 10)
    assert alerts == [
        (
            "Solar health: OK again",
            "Cleared: string 1 produced almost nothing (0.0 of 12.0 kWh) on 3 Jun.",
            "low",
        )
    ]
    assert status["strings"] == "45% / 55%" and status["strings_usual"] == "45% / 55%"
    assert status["battery_health"] == "99%" and status["week_vs_forecast"] == "100%"


def test_collect_integrates_string_power() -> None:
    def query(expr: str, at: float) -> float | None:
        return {"solis_pv_today": 19.5, "forecast": 18.0, "solis_soh": 99.0, "batt_out": 7.1}.get(
            next((k for k in ("solis_pv_today", "forecast", "solis_soh", "batt_out") if k in expr), "")
        )

    def history(expr: str, start: float, end: float, step: int) -> list[tuple[float, float]]:
        assert step == 300 and end - start == 24 * 3600 - 1
        watts = 1200.0 if "dc1" in expr else 1800.0
        return [(start + i * 300, watts) for i in range(12 * 5)]  # 5 hours

    stats = collect_day(query, history, START)
    assert stats.string1_kwh == pytest.approx(6.0) and stats.string2_kwh == pytest.approx(9.0)
    assert (stats.pv_kwh, stats.forecast_kwh, stats.soh_pct, stats.battery_out_kwh) == (19.5, 18.0, 99.0, 7.1)
    assert len(collect(lambda e, a: None, lambda *a: [], START, days=3)) == 0  # no data: no days
