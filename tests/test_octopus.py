from __future__ import annotations

import urllib.parse
from datetime import UTC, date, datetime, timedelta

import pytest

from homelab_tools.octopus.api import OctopusClient, RatePeriod, parse_time, product_code
from homelab_tools.octopus.collector import collect
from homelab_tools.octopus.pricing import DayTotals, Ledger, RateTable, pounds, summarise
from homelab_tools.octopus.setup import build_config

T0 = datetime(2026, 1, 10, tzinfo=UTC)  # winter: local time == UTC, keeps day maths simple
GO_PEAK, GO_CHEAP = 26.3768, 5.2314


def go_rates(day: datetime = T0) -> RateTable:
    """Octopus Go: cheap 00:30-05:30, peak otherwise, for the given day."""
    cheap_start, cheap_end = day + timedelta(minutes=30), day + timedelta(hours=5, minutes=30)
    return RateTable(
        [
            RatePeriod(day, cheap_start, GO_PEAK),
            RatePeriod(cheap_start, cheap_end, GO_CHEAP),
            RatePeriod(cheap_end, day + timedelta(days=1), GO_PEAK),
        ]
    )


def half_hours(day: datetime, kwh: float = 1.0, n: int = 48) -> list[tuple[datetime, float]]:
    return [(day + timedelta(minutes=30 * i), kwh) for i in range(n)]


# --- API client --------------------------------------------------------------------------------


def test_product_code_and_time_parsing() -> None:
    assert product_code("E-1R-GO-FIX-12M-26-02-11-E") == "GO-FIX-12M-26-02-11"
    assert product_code("G-1R-OE-FIX-12M-26-04-09-E") == "OE-FIX-12M-26-04-09"
    with pytest.raises(ValueError):
        product_code("nonsense")
    assert parse_time("2026-10-23T00:00:00Z") == datetime(2026, 10, 23, tzinfo=UTC)
    assert parse_time("2026-10-23T01:00:00+01:00") == datetime(2026, 10, 23, tzinfo=UTC)


class FakeAPI:
    """Serves consumption pages and rates like the real API, recording each request."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, bool]] = []

    def __call__(self, url: str, auth: bool) -> dict:
        self.calls.append((url, auth))
        q = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(url).query))
        if "consumption" in url:
            start, end = parse_time(q["period_from"]), parse_time(q["period_to"])
            records = []
            t = start
            while t < end:
                records.append({"interval_start": t.isoformat(), "consumption": 0.5})
                t += timedelta(minutes=30)
            records.append(records[0])  # the API sometimes repeats a slot
            return {"results": records, "next": None}

        def rate(pence: float, method: str) -> dict:
            valid = {"valid_from": "2026-01-01T00:00:00Z", "valid_to": None}
            return valid | {"value_inc_vat": pence, "payment_method": method}

        return {"results": [rate(20.0, "DIRECT_DEBIT"), rate(99.0, "NON_DIRECT_DEBIT")], "next": None}


def test_consumption_is_chunked_and_deduplicated() -> None:
    api = FakeAPI()
    client = OctopusClient("sk_test", api)
    slots = client.consumption("electricity", "123", "M1", T0, now=T0 + timedelta(days=65))
    assert len([c for c in api.calls if "consumption" in c[0]]) == 3  # 30 + 30 + 5 days
    assert all(auth for _, auth in api.calls)
    assert len(slots) == 65 * 48  # one per half-hour despite repeats
    assert slots == sorted(slots)


def test_rates_prefer_direct_debit_and_clip_to_range() -> None:
    client = OctopusClient("sk_test", FakeAPI())
    (period,) = client.rates(
        "electricity", "E-1R-GO-VAR-22-10-14-E", "standard-unit-rates", T0, T0 + timedelta(1)
    )
    assert period == RatePeriod(T0, T0 + timedelta(days=1), 20.0)


def test_pagination_follows_next_links() -> None:
    pages = iter([{"results": [{"x": 1}], "next": "page2"}, {"results": [{"x": 2}], "next": None}])
    client = OctopusClient("k", lambda url, auth: next(pages))
    assert list(client._paged("page1")) == [{"x": 1}, {"x": 2}]


# --- pricing -----------------------------------------------------------------------------------


def test_rate_table_lookup() -> None:
    rates = go_rates()
    assert rates.at(T0) == GO_PEAK
    assert rates.at(T0 + timedelta(hours=2)) == GO_CHEAP
    assert rates.at(T0 + timedelta(days=2)) is None


def test_import_priced_and_split_offpeak() -> None:
    ledger = Ledger(offpeak_below_p=12)
    ledger.add_import(half_hours(T0), go_rates())
    day = ledger.days[date(2026, 1, 10)]
    assert day.import_kwh == 48 and day.import_slots == 48 and day.complete
    assert day.import_offpeak_kwh == 10 and day.import_peak_kwh == 38
    assert day.import_cost_p == pytest.approx(10 * GO_CHEAP + 38 * GO_PEAK)
    names = {p[0] for p in ledger.points}
    assert {"import_kwh", "import_rate_p", "import_cost_p", "import_offpeak_kwh", "import_peak_kwh"} <= names


def test_slots_without_a_rate_count_usage_but_not_cost() -> None:
    ledger = Ledger()
    ledger.add_import(half_hours(T0 + timedelta(days=5), n=2), go_rates())
    day = ledger.days[date(2026, 1, 15)]
    assert day.import_kwh == 2 and day.import_cost_p == 0


def test_export_gas_standing_and_net() -> None:
    ledger = Ledger()
    flat = RateTable([RatePeriod(T0, T0 + timedelta(days=1), 15.0)])
    ledger.add_import(half_hours(T0, kwh=0.5), go_rates())
    ledger.add_export(half_hours(T0, kwh=0.1, n=10), flat)
    gas = half_hours(T0, kwh=0.1)
    ledger.add_gas(gas, RateTable([RatePeriod(T0, T0 + timedelta(days=1), 7.0)]), kwh_per_m3=10.0)
    ledger.add_standing("electricity", half_hours(T0), RateTable([RatePeriod(T0, T0 + timedelta(1), 50.0)]))
    ledger.add_standing("gas", gas, RateTable([RatePeriod(T0, T0 + timedelta(1), 29.0)]))
    day = ledger.days[date(2026, 1, 10)]
    assert day.export_earn_p == pytest.approx(15.0)
    assert day.gas_kwh == pytest.approx(48) and day.gas_cost_p == pytest.approx(336)
    assert day.electricity_p == pytest.approx(day.import_cost_p + 50)
    assert day.gas_p == pytest.approx(336 + 29)
    assert day.net_p == pytest.approx(day.electricity_p + day.gas_p - 15)
    assert ("standing_p", T0.replace(hour=12), 50.0, {"fuel": "electricity"}) in ledger.points


def test_saving_needs_house_data() -> None:
    day = DayTotals(import_cost_p=200, export_earn_p=50)
    assert day.saving_p is None
    day.nosolar_cost_p = 600
    assert day.saving_p == 450  # 600 - 200 + 50 (no slot counts: built directly)


def test_saving_needs_house_data_for_the_whole_day() -> None:
    day = DayTotals(import_cost_p=200, export_earn_p=50, import_slots=48, nosolar_cost_p=300, house_slots=22)
    assert day.saving_p is None  # monitoring started mid-day: half a day's use against a whole bill
    day.house_slots = 48
    assert day.saving_p == 150


def test_house_use_priced_at_import_rate() -> None:
    ledger = Ledger()
    ledger.add_house_use({T0 + timedelta(hours=1): 0.4, T0 + timedelta(hours=9): 0.4}, go_rates())
    assert ledger.days[date(2026, 1, 10)].nosolar_cost_p == pytest.approx(0.4 * GO_CHEAP + 0.4 * GO_PEAK)
    assert ledger.days[date(2026, 1, 10)].house_slots == 2


def test_summary_picks_latest_complete_day_and_month() -> None:
    friday = DayTotals(
        import_kwh=31.9,
        import_cost_p=204.4,
        import_slots=48,
        export_earn_p=9.9,
        gas_kwh=23,
        gas_cost_p=161.7,
        standing_p={"electricity": 50, "gas": 29},
    )
    days = {
        date(2026, 1, 9): DayTotals(import_cost_p=100, import_slots=48, standing_p={"electricity": 50}),
        date(2026, 1, 10): friday,
        date(2026, 1, 11): DayTotals(import_kwh=1, import_cost_p=10, import_slots=2),  # partial: ignored
    }
    s = summarise(days)
    assert s is not None
    assert s["day"] == "Sat 10 Jan"
    assert (s["import_cost"], s["gas_cost"], s["export_earn"]) == ("£2.54", "£1.91", "£0.10")
    assert s["net"] == "£4.35" and s["saved"] == "pending"
    assert s["month_days"] == "3 days" and s["month_net"] == pounds(150 + 435.2 + 10)
    assert summarise({}) is None


# --- setup + collect end to end ----------------------------------------------------------------


def agreement(tariff: str) -> dict:
    return {"tariff_code": tariff, "valid_from": "2026-01-01T00:00:00Z", "valid_to": None}


def meter_point(key: str, value: str, serials: list[str], tariff: str, export: bool | None = None) -> dict:
    point = {key: value, "meters": [{"serial_number": s} for s in serials], "agreements": [agreement(tariff)]}
    if export is not None:
        point["is_export"] = export
    return point


ACCOUNT = {
    "properties": [
        {
            "electricity_meter_points": [
                meter_point("mpan", "111", ["OLD", "M1"], "E-1R-GO-FIX-12M-26-02-11-E", export=False),
                meter_point("mpan", "222", ["M1"], "E-1R-OUTGOING-VAR-24-10-26-E", export=True),
            ],
            "gas_meter_points": [meter_point("mprn", "333", ["G1"], "G-1R-OE-FIX-12M-26-04-09-E")],
        }
    ]
}


def test_build_config_uses_latest_meter_and_agreements() -> None:
    cfg = build_config(ACCOUNT, "A-TEST")
    assert cfg["account"] == "A-TEST"
    imp, exp = cfg["electricity"]
    assert imp["serial"] == "M1" and not imp["export"] and exp["export"]
    assert imp["agreements"][0]["tariff"].startswith("E-1R-GO")
    assert cfg["gas"][0]["mprn"] == "333"


def test_collect_end_to_end_with_fake_api() -> None:
    api = FakeAPI()
    ledger = Ledger()
    since = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=2)
    collect(OctopusClient("k", api), build_config(ACCOUNT, "A-TEST"), since, ledger, gas_factor=10.0)
    totals = list(ledger.days.values())
    assert sum(d.import_kwh for d in totals) > 0 and sum(d.export_earn_p for d in totals) > 0
    assert sum(d.gas_kwh for d in totals) == pytest.approx(10 * sum(d.gas_m3 for d in totals))
    assert all(d.standing_p for d in totals if d.import_slots)
    public = [url for url, auth in api.calls if not auth]
    assert public and all("/products/" in url for url in public)  # rates never send the key


def test_standing_charge_on_first_bst_day_when_rates_start_at_utc_midnight() -> None:
    """Regression: local midnight in summer (23:00 UTC) lies before the loaded rates."""
    import os
    import time

    old_tz = os.environ.get("TZ")
    os.environ["TZ"] = "Europe/London"
    time.tzset()
    try:
        utc_midnight = datetime(2026, 7, 1, tzinfo=UTC)
        rates = RateTable([RatePeriod(utc_midnight, utc_midnight + timedelta(days=1), 50.0)])
        ledger = Ledger()
        ledger.add_standing("electricity", half_hours(utc_midnight, n=46), rates)
        assert ledger.days[date(2026, 7, 1)].standing_p == {"electricity": 50.0}
    finally:
        if old_tz is None:
            os.environ.pop("TZ")
        else:
            os.environ["TZ"] = old_tz
        time.tzset()
