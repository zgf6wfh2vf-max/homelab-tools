from __future__ import annotations

import time
from datetime import date

import pytest

from homelab_tools.report.weekly import WeekFigures, collect, display, format_report, last_complete_week


@pytest.fixture(autouse=True)
def london():
    """Weeks are local: run these tests in the UK time zone (clock change on 25 Oct 2026)."""
    import os

    old = os.environ.get("TZ")
    os.environ["TZ"] = "Europe/London"
    time.tzset()
    yield
    if old is None:
        del os.environ["TZ"]
    else:
        os.environ["TZ"] = old
    time.tzset()


class FakeVM:
    """Answers queries by metric name; records every query for inspection."""

    def __init__(self, values: dict[str, float | None], octopus_days: int = 7) -> None:
        self.values, self.octopus_days, self.queries = values, octopus_days, []
        self._days_answered = 0

    def __call__(self, expr: str, at: float) -> float | None:
        self.queries.append(expr)
        if expr.startswith("count_over_time(octopus_import_kwh"):
            self._days_answered += 1
            return 48 if self._days_answered <= self.octopus_days else 20
        for name, value in self.values.items():
            if name in expr:
                return value
        return None


FULL = {
    "octopus_import_cost_p": 900.0,
    "octopus_export_earn_p": 220.0,
    "octopus_gas_cost_p": 530.0,
    'octopus_standing_p{fuel="electricity"}': 350.0,
    'octopus_standing_p{fuel="gas"}': 203.0,
    "octopus_import_kwh": 150.0,
    "octopus_import_offpeak_kwh": 138.0,
    "octopus_import_peak_kwh": 12.0,
    "octopus_export_kwh": 15.0,
    "octopus_gas_kwh": 160.0,
    "solis_pv_today_kwh": 5.5,  # per day
    "solis_batt_out_today_kwh": 6.0,
    "solis_batt_in_today_kwh": 7.0,
    "count_over_time(ev_session_kwh": 3,
    "ev_session_kwh": 61.0,
    "ev_session_cost_p": 340.0,
    "tado_heating_pct": 12.4,
    "tado_weather_outside_c": 9.84,
}


def test_last_complete_week() -> None:
    assert last_complete_week(date(2026, 10, 27)) == (date(2026, 10, 19), date(2026, 10, 26))  # Tuesday
    assert last_complete_week(date(2026, 10, 25)) == (date(2026, 10, 12), date(2026, 10, 19))  # Sunday


def test_collect_and_clock_change_week() -> None:
    vm = FakeVM(FULL)
    w = collect(vm, date(2026, 10, 19))
    assert any("[608400s]" in q for q in vm.queries)  # 169 hours: the clocks went back on the 25th
    assert w.solar_kwh == pytest.approx(5.5 * 7) and w.solar_days == 7
    assert w.battery_out_kwh == pytest.approx(42)
    assert w.standing_p == {"electricity": 350.0, "gas": 203.0}
    assert w.electricity_p == 1250 and w.gas_p == 733 and w.net_p == pytest.approx(1250 + 733 - 220)
    assert w.offpeak_share == pytest.approx(0.92)
    assert w.car_sessions == 3 and w.octopus_days == 7
    assert w.label == "19-25 Oct"


def test_format_report_full_week_with_comparison() -> None:
    this = collect(FakeVM(FULL), date(2026, 10, 19))
    prev = collect(
        FakeVM(FULL | {"octopus_import_cost_p": 1110.0, "octopus_gas_kwh": 140.0}), date(2026, 10, 12)
    )
    title, message = format_report(this, prev)
    assert title == "Week 19-25 Oct: £17.63"
    lines = message.splitlines()
    assert lines[0] == (
        "Bill £17.63 (electric £12.50, gas £7.33, export -£2.20) - £2.10 less than the week before."
    )
    assert lines[1] == "Solar 38 kWh, battery supplied 42 kWh."
    assert lines[2] == "Car 61 kWh, £3.40 (3 charges)."
    assert lines[3].startswith("92% of grid import off-peak; gas 160 kWh (20 kWh more than the week before)")
    assert lines[3].endswith("heating demand 12%, outside 9.8°C on average.")


def test_partial_and_empty_weeks() -> None:
    partial = collect(FakeVM(FULL | {"solis_pv_today_kwh": None}, octopus_days=5), date(2026, 10, 19))
    _, message = format_report(partial, None)
    assert "Costs cover 5 of 7 days so far." in message and "Solar" not in message
    assert "than the week before" not in message


def test_part_weeks_are_not_compared() -> None:
    full = collect(FakeVM(FULL), date(2026, 10, 12))
    part = collect(FakeVM(FULL | {"solis_pv_today_kwh": 5.0}, octopus_days=5), date(2026, 10, 19))
    _, message = format_report(part, full)
    assert "than the week before" not in message
    one_day = WeekFigures(date(2026, 10, 19), solar_kwh=17.0, solar_days=1)
    assert "Solar 17 kWh (1 day recorded)." in format_report(one_day, None)[1]
    d = display(part, full)
    assert d["net_change"] == d["gas_change"] == "-"  # cost comparisons need complete weeks
    first_solar_day = collect(FakeVM(FULL), date(2026, 10, 19))
    first_solar_day.solar_days = 1  # inverter monitoring started on the last day of the week
    assert display(full, first_solar_day)["solar_change"] == "-"
    empty = WeekFigures(date(2026, 10, 26))
    assert format_report(empty, None) == ("Week 26 Oct-1 Nov", "No data was recorded this week.")


def test_display_fields_and_changes() -> None:
    this = collect(FakeVM(FULL), date(2026, 10, 19))
    prev = collect(
        FakeVM(FULL | {"octopus_import_cost_p": 1110.0, "ev_session_kwh": 61.2}), date(2026, 10, 12)
    )
    d = display(this, prev)
    assert (d["week"], d["net"], d["electric"], d["gas"], d["export"]) == (
        "19-25 Oct",
        "£17.63",
        "£12.50",
        "£7.33",
        "£2.20",
    )
    assert (d["solar"], d["car"], d["offpeak"], d["gas_kwh"]) == ("38 kWh", "61 kWh", "92%", "160 kWh")
    assert d["net_change"] == "▼ £2.10" and d["car_change"] == "same" and d["solar_change"] == "same"
    assert display(this, None)["net_change"] == "-"
