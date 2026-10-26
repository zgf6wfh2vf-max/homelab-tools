from __future__ import annotations

import os
import time as systime
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path

import pytest

from homelab_tools.usage.watch import (
    GasModel,
    State,
    Watch,
    baseloads,
    check_baseload,
    check_gas,
    daily_temperatures,
    daily_totals,
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


START = date(2026, 1, 1)


def history(days: int = 120, base: float = 14.0, a: float = 4.0, b: float = 4.0) -> tuple[dict, dict]:
    """Synthetic year: temperatures swing 0-20 °C; gas follows the model plus +-2 kWh of scatter."""
    temps, gas = {}, {}
    for i in range(days):
        day = START + timedelta(days=i)
        temps[day] = 10 + 10 * ((i % 20) - 10) / 10
        gas[day] = a + b * max(0.0, base - temps[day]) + (2 if i % 2 else -2)
    return gas, temps


def test_gas_model_finds_the_balance_temperature() -> None:
    gas, temps = history()
    model = GasModel.fit(gas, temps)
    assert model and model.base == 14 and model.b == pytest.approx(4, abs=0.1)
    assert model.sd == pytest.approx(2, abs=0.2) and model.days == 120
    assert GasModel.fit(dict(list(gas.items())[:30]), temps) is None  # too little history


def test_check_gas_needs_both_thresholds() -> None:
    model = GasModel(14, 4, 4, 2, 100)  # 10 °C -> 20 kWh expected
    assert not check_gas(model, START, 25, 10, sigma=3, min_pct=25).unusual  # +5: inside 3 sd
    assert check_gas(model, START, 27, 10, sigma=3, min_pct=25).unusual  # +7: > 6 and > 5
    summer = GasModel(14, 4, 4, 0.2, 100)  # hot water only: 4 kWh a day, very steady
    assert not check_gas(summer, START, 5.8, 20, sigma=3, min_pct=25).unusual  # +1.8 kWh: too small to matter
    assert check_gas(summer, START, 9.5, 20, sigma=3, min_pct=25).unusual


def slots_for(day: date, per_slot: float, count: int = 48) -> dict[datetime, float]:
    start = datetime.combine(day, time()).astimezone()
    return {(start + timedelta(minutes=30 * i)).astimezone(UTC): per_slot for i in range(count)}


def test_daily_totals_drop_incomplete_days() -> None:
    slots = slots_for(START, 0.5) | slots_for(START + timedelta(days=1), 0.5, count=20)
    assert daily_totals(slots) == {START: pytest.approx(24.0)}


def test_daily_temperatures_merge_recent_and_archive() -> None:
    urls = []

    def fetch(url: str):
        urls.append(url)
        if "archive" in url:
            return {"daily": {"time": ["2026-01-01", "2026-01-02"], "temperature_2m_mean": [3.0, None]}}
        return {"daily": {"time": ["2026-01-02", "2026-01-03"], "temperature_2m_mean": [4.0, 5.0]}}

    temps = daily_temperatures(51.5, -0.12, START, date(2026, 1, 2), fetch)
    assert temps == {START: 3.0, date(2026, 1, 2): 4.0}  # day 3 is outside the range
    assert any("past_days=14" in u for u in urls)


def rows_for(day: date, quiet_w: float, busy_w: float = 900) -> list[tuple[float, float]]:
    """48 half-hours: six quiet ones at ``quiet_w``, the rest busy."""
    start = datetime.combine(day, time()).astimezone()
    return [
        ((start + timedelta(minutes=30 * (i + 1))).timestamp(), quiet_w if i in range(4, 10) else busy_w)
        for i in range(48)
    ]


def test_baseload_is_the_quietest_three_hours() -> None:
    assert baseloads(rows_for(START, 150)) == {START: 150}
    assert baseloads(rows_for(START, 150)[:30]) == {}  # too few half-hours


def test_check_baseload() -> None:
    loads = {START + timedelta(days=i): 150.0 for i in range(10)}
    assert check_baseload(loads, 1.5, 100) is not None and not check_baseload(loads, 1.5, 100).high
    one_day = loads | {START + timedelta(days=10): 400.0}
    assert not check_baseload(one_day, 1.5, 100).high  # one high day could be a one-off
    two_days = one_day | {START + timedelta(days=11): 420.0}
    result = check_baseload(two_days, 1.5, 100)
    assert result.high and result.usual == 150 and result.latest == 420
    small = loads | {START + timedelta(days=10): 240.0, START + timedelta(days=11): 240.0}
    assert not check_baseload(small, 1.5, 100).high  # > 1.5x but not 100 W more
    assert check_baseload(dict(list(loads.items())[:5]), 1.5, 100) is None


def make_watch(gas_days: dict[date, float], temps: dict, load_rows, state: State) -> Watch:
    series = {}
    for day, kwh in gas_days.items():
        series |= slots_for(day, kwh / 48)
    return Watch(
        series=lambda metric, start, end: series,
        history=lambda expr, start, end, step: load_rows,
        temperatures=lambda s, e: temps,
        state=state,
    )


def test_watch_first_run_checks_only_the_newest_day_then_continues(tmp_path: Path) -> None:
    gas, temps = history()
    last = max(gas)
    gas[last] += 30  # a boiler left on
    rows = [r for i in range(12) for r in rows_for(last - timedelta(days=11 - i), 150)]
    state = State()
    alerts, status = make_watch(gas, temps, rows, state).run(
        datetime.combine(last + timedelta(days=2), time(10)).astimezone()
    )
    assert [a[0] for a in alerts] == [f"Unusual gas use {last:%a %-d %b}"]
    assert state.gas_checked == last.isoformat() and status["base_now"] == "150 W"
    state.save(tmp_path / "state.json")
    assert State.load(tmp_path / "state.json") == state
    again, _ = make_watch(gas, temps, rows, state).run(
        datetime.combine(last + timedelta(days=2), time(11)).astimezone()
    )
    assert again == []  # already checked


def test_watch_baseload_alerts_once_and_on_recovery() -> None:
    day0 = START + timedelta(days=100)
    normal = [r for i in range(10) for r in rows_for(day0 + timedelta(days=i), 150)]
    high = normal + rows_for(day0 + timedelta(days=10), 500) + rows_for(day0 + timedelta(days=11), 500)
    state = State(gas_checked="2099-01-01")
    now = datetime.combine(day0 + timedelta(days=12), time(10)).astimezone()
    alerts, _ = make_watch({}, {}, high, state).run(now)
    assert [a[0] for a in alerts] == ["Something may have been left on"] and "8.4 kWh a day extra" in alerts[
        0
    ][1]
    assert make_watch({}, {}, high, state).run(now)[0] == []  # told once
    recovered = high + rows_for(day0 + timedelta(days=12), 150) + rows_for(day0 + timedelta(days=13), 150)
    later = now + timedelta(days=2)
    alerts, _ = make_watch({}, {}, recovered, state).run(later)
    assert [(a[0], a[2]) for a in alerts] == [("Always-on electricity back to normal", "low")]
