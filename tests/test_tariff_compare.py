from __future__ import annotations

import io
import json
import os
import time as systime
from datetime import UTC, date, datetime, time, timedelta

import pytest

from homelab_tools.octopus import compare as cmp
from homelab_tools.octopus.api import RatePeriod
from homelab_tools.octopus.compare import (
    Catalogue,
    Comparison,
    Pricer,
    Result,
    TimeOfDayPrices,
    Usage,
    compare,
    deal_ends,
    display,
    format_report,
    import_cost,
    money,
    month_period,
    night_of,
    region_of,
    vm_series,
)
from homelab_tools.octopus.pricing import RateTable


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


def local(day: date, hh: int, mm: int = 0) -> datetime:
    return datetime.combine(day, time(hh, mm)).astimezone()


DAY = date(2026, 9, 10)
CONFIG = {
    "electricity": [
        {
            "export": True,
            "agreements": [{"tariff": "E-1R-OUTGOING-VAR-24-10-26-E", "from": "2024-11-15", "to": None}],
        },
        {
            "export": False,
            "agreements": [
                {"tariff": "E-1R-GO-VAR-22-10-14-E", "from": "2024-08-14", "to": "2025-04-11"},
                {
                    "tariff": "E-1R-GO-FIX-12M-26-02-11-E",
                    "from": "2026-04-11",
                    "to": "2027-04-11T00:00:00+01:00",
                },
            ],
        },
    ],
    "gas": [
        {"agreements": [{"tariff": "G-1R-OE-FIX-12M-26-04-09-E", "from": "2026-04-22", "to": "2027-04-22"}]}
    ],
}


def test_region_and_deal_ends() -> None:
    assert region_of(CONFIG) == "E"
    assert deal_ends(CONFIG) == {"electricity": date(2027, 4, 11), "gas": date(2027, 4, 22)}


def detail(code: str, fuel: str = "electricity", key: str = "direct_debit_monthly") -> dict:
    prefix = "E" if fuel == "electricity" else "G"
    return {f"single_register_{fuel}_tariffs": {"_E": {key: {"code": f"{prefix}-1R-{code}-E"}}}}


def test_catalogue_picks_the_newest_priceable_product() -> None:
    products = [
        {
            "code": "GO-FIX-12M-26-04-18",
            "display_name": "Octopus Go 12M Fixed",
            "available_from": "2026-04-18",
        },
        {
            "code": "GO-FIX-12M-26-10-14",
            "display_name": "Octopus Go 12M Fixed",
            "available_from": "2026-10-14",
        },
        {"code": "VAR-22-11-01", "display_name": "Flexible Octopus", "available_from": "2022-11-01"},
        {
            "code": "IOG-SMB-FIX",
            "display_name": "Intelligent Octopus Go 12M Fixed",
            "available_from": "2026-10-20",
        },
    ]
    details = {
        "GO-FIX-12M-26-10-14": detail("GO-FIX-12M-26-10-14"),
        "GO-FIX-12M-26-04-18": detail("GO-FIX-12M-26-04-18"),
        "VAR-22-11-01": detail("VAR-22-11-01", "gas", key="varying"),
        "IOG-SMB-FIX": {
            "four_rate_ev_electricity_tariffs": {"_E": {}},
            "single_register_electricity_tariffs": [],
        },
    }
    cat = Catalogue(products, fetch=lambda url: details[url.rstrip("/").rsplit("/", 1)[1]])
    assert cat.tariff("Octopus Go 12M Fixed", "E") == "E-1R-GO-FIX-12M-26-10-14-E"
    assert cat.tariff("Flexible Octopus", "E", "gas") == "G-1R-VAR-22-11-01-E"  # variable: "varying"
    assert cat.tariff("Intelligent Octopus Go 12M Fixed", "E") is None  # four-rate EV tariff only
    assert cat.tariff("No such tariff", "E") is None


def go_rates(
    cheap: float = 5.0, peak: float = 30.0, start: date = DAY - timedelta(days=1), days: int = 3
) -> RateTable:
    """Go-style prices: cheap 00:30-05:30 local, peak otherwise."""
    periods = []
    for d in range(days):
        day = start + timedelta(days=d)
        a, b, c = local(day, 0), local(day, 0, 30), local(day, 5, 30)
        periods += [
            RatePeriod(a, b, peak),
            RatePeriod(b, c, cheap),
            RatePeriod(c, local(day + timedelta(days=1), 0), peak),
        ]
    return RateTable(periods)


def usage(**kw) -> Usage:
    """One day: 4 kWh charged at 01:00-02:30 (cheap) and 1 kWh at 18:00 (peak); 2 kWh exported at noon."""
    imports = {
        local(DAY, 1): 1.0,
        local(DAY, 1, 30): 1.0,
        local(DAY, 2): 1.0,
        local(DAY, 2, 30): 1.0,
        local(DAY, 18): 1.0,
    }
    defaults = dict(
        imports={t.astimezone(UTC): k for t, k in imports.items()},
        import_rates={t.astimezone(UTC): (5.0 if t.hour < 5 else 30.0) for t in imports},
        exports={local(DAY, 12).astimezone(UTC): 2.0},
        gas={local(DAY, 7).astimezone(UTC): 10.0},
        paid_electric_p=4 * 5.0 + 30.0 + 50,
        earned_p=2 * 15.0,
        paid_gas_p=10 * 7.0 + 30,
    )
    return Usage(**(defaults | kw))


def test_night_of() -> None:
    assert night_of(local(DAY, 2)) == DAY - timedelta(days=1)
    assert night_of(local(DAY, 22)) == DAY


def test_flexible_energy_moves_to_the_cheapest_night_slots() -> None:
    # an Agile-like night: 03:00 and 03:30 cost 2p, everything else 20p
    periods = [RatePeriod(local(DAY - timedelta(days=1), 12), local(DAY, 3), 20.0)]
    periods += [
        RatePeriod(local(DAY, 3), local(DAY, 4), 2.0),
        RatePeriod(local(DAY, 4), local(DAY, 23), 20.0),
    ]
    cost = import_cost(usage(), RateTable(periods), offpeak_below_p=12)
    assert cost == pytest.approx(
        2 * 2.0 + 2 * 20.0 + 1 * 20.0
    )  # 2 slots at 2p, 2 more at 20p; peak use as-is
    assert import_cost(usage(), go_rates(), 12) == pytest.approx(4 * 5.0 + 30.0)  # same as paid on Go
    assert import_cost(usage(), RateTable([]), 12) is None  # no prices


def test_time_of_day_prices_cover_a_clock_change_day() -> None:
    day = date(2026, 10, 25)  # 25 hours long
    tod = TimeOfDayPrices(go_rates(start=day, days=1), day)
    assert tod.at(local(date(2026, 9, 1), 23, 30)) == 30.0
    assert tod.at(local(date(2026, 9, 1), 3)) == 5.0


def fake_rates(prices: dict[str, RateTable]):
    def rates(fuel, code, what, lo, hi):
        return prices.get(f"{code}/{what}", RateTable([]))

    return rates


def standing_table(pence: float, start: date, days: int) -> RateTable:
    return RateTable([RatePeriod(local(start, 0), local(start + timedelta(days=days), 0), pence)])


def test_compare_ranks_options_and_falls_back_to_todays_prices() -> None:
    products = [
        {"code": "GO", "display_name": "Octopus Go", "available_from": "2022"},
        {"code": "OUT", "display_name": "Outgoing Octopus", "available_from": "2024"},
        {"code": "NEW", "display_name": "Octopus 12M Fixed", "available_from": "2026-10-19"},
        {"code": "FLEX", "display_name": "Flexible Octopus", "available_from": "2022"},
    ]
    details = {
        "GO": detail("GO"),
        "OUT": detail("OUT", key="varying"),
        "NEW": detail("NEW") | detail("NEW", "gas"),
        "FLEX": detail("FLEX", key="varying") | detail("FLEX", "gas", key="varying"),
    }
    cat = Catalogue(products, fetch=lambda url: details[url.rstrip("/").rsplit("/", 1)[1]])
    recent = date(2026, 10, 25)
    flat = RateTable([RatePeriod(local(DAY - timedelta(days=2), 0), local(DAY + timedelta(days=2), 0), 25.0)])
    prices = {
        "E-1R-GO-E/standard-unit-rates": go_rates(cheap=8.0),
        "E-1R-GO-E/standing-charges": standing_table(50, DAY - timedelta(days=1), 3),
        "E-1R-OUT-E/standard-unit-rates": flat,
        "E-1R-FLEX-E/standard-unit-rates": flat,
        "E-1R-FLEX-E/standing-charges": standing_table(50, DAY - timedelta(days=1), 3),
        # launched after the period: only today's prices exist
        "E-1R-NEW-E/standard-unit-rates": standing_table(20.0, recent, 1),
        "E-1R-NEW-E/standing-charges": standing_table(45, recent, 1),
        "G-1R-NEW-E/standard-unit-rates": standing_table(6.0, recent, 1),
        "G-1R-NEW-E/standing-charges": standing_table(30, recent, 1),
    }
    pricer = Pricer(fake_rates(prices), (local(DAY, 0), local(DAY + timedelta(days=1), 0)), recent)
    c = compare(usage(), cat, pricer, "E", "Sep 2026")
    by_name = {r.name: r for r in c.electric}
    assert by_name["Go"].net_p == pytest.approx(4 * 8.0 + 30 + 50 - 2 * 25)
    assert by_name["Flexible"].net_p == pytest.approx(5 * 25 + 50 - 2 * 25)
    assert by_name["12M Fixed"].net_p == pytest.approx(5 * 20 + 45 - 2 * 25)
    assert by_name["12M Fixed"].note == "at today's prices"
    assert by_name["Agile"].note == "not priceable from public data"  # not in this catalogue
    gas = {r.name: r for r in c.gas}
    assert gas["Octopus 12M Fixed"].net_p == pytest.approx(10 * 6.0 + 30)
    assert gas["Flexible Octopus"].net_p is None and gas["Flexible Octopus"].note == "no prices"
    assert [r.name for r in Comparison.ranked(c.electric)][:2] == ["Go", "12M Fixed"]


def comparison(**kw) -> Comparison:
    c = Comparison("Sep 2026", 30, -398.0, 1842.0)
    c.electric = [
        Result("Go", 1137.0, "needs an EV"),
        Result("Agile", 7389.0),
        Result("Intelligent Go", None, "x"),
    ]
    c.gas = [Result("Flexible Octopus", 1873.0)]
    for k, v in kw.items():
        setattr(c, k, v)
    return c


def test_report_when_the_current_tariff_wins() -> None:
    c = comparison(deal_ends={"electricity": date(2027, 4, 11), "gas": date(2027, 4, 22)})
    title, message = format_report(c, today=date(2026, 10, 3))
    assert title == "Tariff check Sep 2026: your tariff is still the best fit"
    assert message.splitlines() == [
        "Electricity minus export, your real use over 30 days: you paid -£3.98.",
        "Go: £11.37, £15.35 more (needs an EV)",
        "Agile: £73.89, £77.87 more",
        "Gas: you paid £18.42; Flexible Octopus would be £18.73 (£0.31 more).",
        "Your fixed deals end: electricity 11 Apr 2027, gas 22 Apr 2027.",
    ]


def test_report_titles_for_savings_and_ending_deals() -> None:
    cheaper = comparison(electric=[Result("Flux", -1000.0)])
    assert format_report(cheaper, date(2026, 10, 3))[0] == "Tariff check Sep 2026: Flux could save £6.02"
    ending = comparison(deal_ends={"electricity": date(2026, 11, 1)})
    assert format_report(ending, date(2026, 10, 3))[0] == (
        "Tariff check Sep 2026: your electricity fix ends 1 Nov; best now Go"
    )
    d = display(comparison(deal_ends={"electricity": date(2027, 4, 11)}))
    assert (d["paid"], d["best"], d["best_cost"], d["versus"], d["fix_ends"]) == (
        "-£3.98",
        "Go",
        "£11.37",
        "£15.35 more",
        "11 Apr 2027",
    )
    assert money(-398) == "-£3.98" and money(1137) == "£11.37"


def test_month_period() -> None:
    start, end, label = month_period(None, date(2026, 11, 3))
    assert (start, end, label) == (local(date(2026, 10, 1), 0), local(date(2026, 11, 1), 0), "Oct 2026")
    assert month_period("2026-03", date(2026, 11, 3))[2] == "Mar 2026"


def test_vm_series_sums_raw_samples(monkeypatch) -> None:
    rows = [
        {"metric": {"fuel": "gas"}, "values": [1.5, 2.0], "timestamps": [1789000000000, 1789001800000]},
        {"metric": {"fuel": "electricity"}, "values": [0.5], "timestamps": [1789000000000]},
    ]
    body = "\n".join(json.dumps(r) for r in rows).encode()
    seen = []

    def fake_urlopen(url, timeout):
        seen.append(url)
        return io.BytesIO(body)

    monkeypatch.setattr(cmp.urllib.request, "urlopen", fake_urlopen)
    out = vm_series("http://vm")("octopus_standing_p", local(DAY, 0), local(DAY, 1))
    assert out == {datetime.fromtimestamp(1789000000, UTC): 2.0, datetime.fromtimestamp(1789001800, UTC): 2.0}
    assert seen[0].startswith("http://vm/api/v1/export?match%5B%5D=octopus_standing_p")
