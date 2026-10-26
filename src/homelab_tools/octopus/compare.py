"""``tariff-compare``: what would last month have cost on other Octopus tariffs?

It reprices your **actual half-hourly** import and export, and your gas, with the public prices of
other Octopus tariffs for your region, and compares the result with what you really paid (as
recorded by ``octopus-collector``).

One adjustment makes the comparison fair for homes with a battery or an EV. Energy you imported in
your current tariff's cheap slots (``OFFPEAK_BELOW_P``) is *flexible*: on a different time-of-use
tariff you would charge at that tariff's cheap times instead. So that energy is moved, night by
night, into the candidate's cheapest half-hours between 21:00 and 08:00. Everything else (daytime
use, export) is priced exactly when it happened. Export on a tariff like Flux or Agile Outgoing
could earn more if the battery were set to discharge at its peak; that is not modelled, so those
figures are conservative.

Tariffs are found by their display names in Octopus' public product list, newest version first, so
the defaults keep working when Octopus re-issues a product. A product whose prices are not published
as a single-rate tariff (e.g. Intelligent Octopus Go's four-rate EV tariff) is skipped with a note.

Settings (environment): ``OCTOPUS_CONFIG`` (from ``octopus-setup``: region and current tariffs),
``VM_URL`` (required), ``OFFPEAK_BELOW_P`` (12), ``NTFY_URL``, ``STATUS_DIR``.

Usage: ``tariff-compare`` (previous calendar month), ``--month 2026-10``, ``--dry-run``.
"""

from __future__ import annotations

import argparse
import json
import logging
import urllib.parse
import urllib.request
from collections import defaultdict
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from typing import Any, Protocol

from homelab_tools.common.config import Outputs, env_float, env_str
from homelab_tools.common.notify import Notifier
from homelab_tools.common.status import write_status
from homelab_tools.octopus.api import API, OctopusClient
from homelab_tools.octopus.pricing import RateTable, local_day, pounds

log = logging.getLogger("tariff-compare")

#: Returns the raw samples of a metric between two moments as ``{slot start: value}``.
Series = Callable[[str, datetime, datetime], dict[datetime, float]]
#: Fetches a public Octopus API URL and returns the decoded JSON.
Fetcher = Callable[[str], Any]

SLOT = timedelta(minutes=30)
#: Flexible (battery / EV) charging may move anywhere in this local window.
NIGHT = (time(21, 0), time(8, 0))


@dataclass(frozen=True)
class Option:
    """One electricity tariff combination to compare, by Octopus display names.

    Attributes:
        name: Short label for the report.
        import_product: Display name of the import product.
        export_product: Display name of the export product.
        note: Eligibility caveat shown with the result.
    """

    name: str
    import_product: str
    export_product: str
    note: str = ""


OUTGOING = "Outgoing Octopus"
OPTIONS = (
    Option("Go", "Octopus Go", OUTGOING, "needs an EV"),
    Option("Go 12M Fixed", "Octopus Go 12M Fixed", OUTGOING, "needs an EV"),
    Option(
        "Intelligent Go", "Intelligent Octopus Go 12M Fixed", OUTGOING, "needs a compatible EV or charger"
    ),
    Option("Agile", "Agile Octopus", OUTGOING),
    Option("Agile + Agile Outgoing", "Agile Octopus", "Agile Outgoing Octopus"),
    Option("Flux", "Octopus Flux Import", "Octopus Flux Export", "needs solar and a battery"),
    Option("Flexible", "Flexible Octopus", OUTGOING),
    Option("12M Fixed", "Octopus 12M Fixed", OUTGOING),
)
GAS_OPTIONS = ("Flexible Octopus", "Octopus 12M Fixed")


def public_get(url: str) -> Any:
    """Default fetcher for the public (no key) product endpoints."""
    with urllib.request.urlopen(url, timeout=60) as resp:
        return json.load(resp)


def region_of(config: Mapping[str, Any]) -> str:
    """The region letter (``A``-``P``) from the newest import agreement in ``octopus-setup`` output."""
    meters = [m for m in config["electricity"] if not m.get("export")]
    agreements = [a for m in meters for a in m["agreements"]]
    newest = max(agreements, key=lambda a: a["from"])
    return str(newest["tariff"].rsplit("-", 1)[1])


def deal_ends(config: Mapping[str, Any]) -> dict[str, date]:
    """End dates of the current fixed import and gas agreements (open-ended ones are left out)."""
    ends = {}
    for fuel in ("electricity", "gas"):
        meters = [m for m in config.get(fuel, []) if not m.get("export")]
        agreements = [a for m in meters for a in m["agreements"]]
        if agreements:
            newest = max(agreements, key=lambda a: a["from"])
            if newest.get("to"):
                ends[fuel] = datetime.fromisoformat(newest["to"]).date()
    return ends


@dataclass
class Catalogue:
    """Octopus' public products, for finding tariff codes by display name.

    Attributes:
        products: The product list (``code``, ``display_name``, ``available_from``).
        fetch: Fetcher for product details.
    """

    products: list[dict[str, Any]]
    fetch: Fetcher = public_get

    @classmethod
    def load(cls, fetch: Fetcher = public_get) -> Catalogue:
        """Download the current household product list."""
        products: list[dict[str, Any]] = []
        url: str | None = f"{API}/products/?brand=OCTOPUS_ENERGY&is_business=false&page_size=100"
        while url:
            page = fetch(url)
            products += page["results"]
            url = page.get("next")
        return cls(products, fetch)

    def tariff(self, display_name: str, region: str, fuel: str = "electricity") -> str | None:
        """Direct-debit tariff code of the newest product called ``display_name``, or ``None``.

        ``None`` means the product is not offered, or not as a single-rate tariff that can be priced
        from public data.
        """
        matches = sorted(
            (p for p in self.products if p["display_name"] == display_name),
            key=lambda p: p["available_from"] or "",
            reverse=True,
        )
        key = f"single_register_{fuel}_tariffs"
        for product in matches:
            detail = self.fetch(f"{API}/products/{product['code']}/")
            group = detail.get(key) or {}  # an empty group can come back as []
            tariffs = group.get(f"_{region}", {}) if isinstance(group, dict) else {}
            # fixed products list a price per payment method; variable ones only "varying"
            chosen = tariffs.get("direct_debit_monthly") or tariffs.get("varying") or {}
            code = chosen.get("code")
            if code:
                return str(code)
        return None


@dataclass
class Usage:
    """A period's half-hourly data and what was really paid, from VictoriaMetrics.

    Attributes:
        imports: kWh imported per slot.
        import_rates: Actual import rate per slot (pence), to find the flexible slots.
        exports: kWh exported per slot.
        gas: Gas kWh per slot.
        paid_electric_p: Actual import cost plus electricity standing charges.
        earned_p: Actual export earnings.
        paid_gas_p: Actual gas cost plus gas standing charges.
    """

    imports: dict[datetime, float]
    import_rates: dict[datetime, float]
    exports: dict[datetime, float]
    gas: dict[datetime, float]
    paid_electric_p: float
    earned_p: float
    paid_gas_p: float

    @property
    def days(self) -> list[date]:
        """Local days with import data."""
        return sorted({local_day(t) for t in self.imports})

    @property
    def paid_net_electric_p(self) -> float:
        """Electricity paid minus export earned."""
        return self.paid_electric_p - self.earned_p


def vm_series(vm_url: str) -> Series:
    """A :data:`Series` reading raw samples through VictoriaMetrics' export API."""

    def series(selector: str, start: datetime, end: datetime) -> dict[datetime, float]:
        query = urllib.parse.urlencode(
            {"match[]": selector, "start": start.isoformat(), "end": (end - timedelta(seconds=1)).isoformat()}
        )
        out: dict[datetime, float] = {}
        with urllib.request.urlopen(f"{vm_url}/api/v1/export?{query}", timeout=60) as resp:
            for line in resp:
                row = json.loads(line)
                for ms, value in zip(row["timestamps"], row["values"], strict=True):
                    moment = datetime.fromtimestamp(ms / 1000, UTC)
                    out[moment] = out.get(moment, 0.0) + float(value)
        return out

    return series


def load_usage(series: Series, start: datetime, end: datetime) -> Usage:
    """Read a period's half-hours and actual costs (metrics written by ``octopus-collector``)."""

    def total(selector: str) -> float:
        return sum(series(selector, start, end).values())

    return Usage(
        imports=series("octopus_import_kwh", start, end),
        import_rates=series("octopus_import_rate_p", start, end),
        exports=series("octopus_export_kwh", start, end),
        gas=series("octopus_gas_kwh", start, end),
        paid_electric_p=total("octopus_import_cost_p") + total('octopus_standing_p{fuel="electricity"}'),
        earned_p=total("octopus_export_earn_p"),
        paid_gas_p=total("octopus_gas_cost_p") + total('octopus_standing_p{fuel="gas"}'),
    )


def night_of(moment: datetime) -> date:
    """The night a slot belongs to, named by the evening's date (so 02:00 belongs to the day before)."""
    return (moment.astimezone() - timedelta(hours=12)).date()


def night_slots(night: date) -> list[datetime]:
    """Half-hour slot starts in the flexible window of ``night`` (21:00 to 08:00 the next morning)."""
    start = datetime.combine(night, NIGHT[0]).astimezone()
    end = datetime.combine(night + timedelta(days=1), NIGHT[1]).astimezone()
    out, moment = [], start
    while moment < end:
        out.append(moment.astimezone(UTC))
        moment += SLOT
    return out


def import_cost(usage: Usage, rates: Prices, offpeak_below_p: float) -> float | None:
    """Unit cost (pence) of the period's imports on ``rates``, with flexible energy moved to cheap slots.

    Returns:
        ``None`` if the tariff has no price for some slot (e.g. it did not exist yet).
    """
    flexible: dict[date, list[float]] = defaultdict(list)
    cost = 0.0
    for moment, kwh in usage.imports.items():
        if usage.import_rates.get(moment, offpeak_below_p) < offpeak_below_p:
            flexible[night_of(moment)].append(kwh)
            continue
        rate = rates.at(moment)
        if rate is None:
            return None
        cost += kwh * rate
    for night, energies in flexible.items():
        priced = [r for r in (rates.at(s) for s in night_slots(night)) if r is not None]
        if len(priced) < len(energies):
            return None
        # biggest flexible half-hours go to the cheapest slots
        cost += sum(
            kwh * rate for kwh, rate in zip(sorted(energies, reverse=True), sorted(priced), strict=False)
        )
    return cost


class TimeOfDayPrices:
    """Prices by local time of day, taken from one recent day of a tariff's rates.

    Used for tariffs that did not exist during the compared period (e.g. a fixed tariff launched
    this month): the period's usage is priced as it would be on the tariff today.
    """

    def __init__(self, rates: RateTable, day: date) -> None:
        """Sample ``rates`` at every half-hour of local ``day``."""
        self.by_clock: dict[time, float] = {}
        moment = datetime.combine(day, time()).astimezone()
        while moment.date() == day:  # 46, 48 or 50 half-hours, depending on clock changes
            rate = rates.at(moment)
            if rate is not None:
                self.by_clock.setdefault(moment.time(), rate)
            moment = (moment + SLOT).astimezone()

    def at(self, moment: datetime) -> float | None:
        """The price at ``moment``'s local time of day."""
        return self.by_clock.get(moment.astimezone().time().replace(second=0, microsecond=0))


class Prices(Protocol):
    """Anything that gives the price at a moment: a :class:`RateTable` or :class:`TimeOfDayPrices`."""

    def at(self, moment: datetime) -> float | None:
        """Price in pence at ``moment``, or ``None``."""


def priced_at(slots: Mapping[datetime, float], rates: Prices) -> float | None:
    """Sum of kWh x rate at the time each slot happened; ``None`` if a slot has no price."""
    total = 0.0
    for moment, kwh in slots.items():
        rate = rates.at(moment)
        if rate is None:
            return None
        total += kwh * rate
    return total


def standing(days: Iterable[date], rates: Prices) -> float | None:
    """Standing charges for ``days``, at the rate in force at each local noon."""
    total = 0.0
    for day in days:
        rate = rates.at(datetime.combine(day, time(12)).astimezone())
        if rate is None:
            return None
        total += rate
    return total


@dataclass(frozen=True)
class Result:
    """One option's cost for the period (pence), or why it could not be priced."""

    name: str
    net_p: float | None
    note: str = ""


@dataclass
class Comparison:
    """All results for a period, plus what was actually paid."""

    label: str
    days: int
    paid_net_electric_p: float
    paid_gas_p: float
    electric: list[Result] = field(default_factory=list)
    gas: list[Result] = field(default_factory=list)
    deal_ends: dict[str, date] = field(default_factory=dict)

    @staticmethod
    def ranked(results: Sequence[Result]) -> list[Result]:
        """Priced results, cheapest first."""
        return sorted((r for r in results if r.net_p is not None), key=lambda r: r.net_p or 0.0)


#: Gets rate periods: (fuel, tariff code, "standard-unit-rates" | "standing-charges", start, end).
Rates = Callable[[str, str, str, datetime, datetime], RateTable]


@dataclass
class Pricer:
    """Prices usage on one tariff, falling back to today's prices for tariffs newer than the period.

    Attributes:
        rates: Rate fetcher.
        period: Start and end of the compared period.
        recent: A recent complete day, whose prices stand for "today's prices".
    """

    rates: Rates
    period: tuple[datetime, datetime]
    recent: date

    def lookup(self, fuel: str, code: str, what: str, current: bool) -> Prices:
        """The period's prices, or (``current``) a recent day's prices by time of day."""
        if not current:
            return self.rates(fuel, code, what, *self.period)
        day_start = datetime.combine(self.recent, time()).astimezone()
        day_end = datetime.combine(self.recent + timedelta(days=1), time()).astimezone()  # 23-25 h
        return TimeOfDayPrices(self.rates(fuel, code, what, day_start, day_end), self.recent)

    def part(self, fuel: str, code: str, what: str, price: Callable[[Prices], float | None]) -> Priced:
        """Price one component with the period's prices, or with today's if the period has none."""
        value = price(self.lookup(fuel, code, what, current=False))
        if value is not None:
            return value, False
        return price(self.lookup(fuel, code, what, current=True)), True

    def electricity(self, usage: Usage, imp: str, exp: str, offpeak_below_p: float) -> Priced:
        """Import (with flexible energy moved) + standing charges - export earnings, in pence."""
        parts = [
            self.part(
                "electricity", imp, "standard-unit-rates", lambda r: import_cost(usage, r, offpeak_below_p)
            ),
            self.part("electricity", imp, "standing-charges", lambda r: standing(usage.days, r)),
            self.part("electricity", exp, "standard-unit-rates", lambda r: priced_at(usage.exports, r)),
        ]
        return _combine(parts, signs=(1, 1, -1))

    def gas(self, usage: Usage, code: str) -> Priced:
        """Gas units + standing charges, in pence."""
        days = sorted({local_day(t) for t in usage.gas})
        parts = [
            self.part("gas", code, "standard-unit-rates", lambda r: priced_at(usage.gas, r)),
            self.part("gas", code, "standing-charges", lambda r: standing(days, r)),
        ]
        return _combine(parts, signs=(1, 1))


#: A price in pence (``None`` if unavailable) and whether today's prices had to be used.
Priced = tuple[float | None, bool]


def _combine(parts: Sequence[Priced], signs: Sequence[int]) -> Priced:
    """Sum priced components with the given signs; ``None`` if any is missing."""
    if any(value is None for value, _ in parts):
        return None, False
    total = sum(sign * (value or 0.0) for (value, _), sign in zip(parts, signs, strict=True))
    return total, any(today for _, today in parts)


TODAY_NOTE = "at today's prices"


def compare(
    usage: Usage,
    catalogue: Catalogue,
    pricer: Pricer,
    region: str,
    label: str,
    offpeak_below_p: float = 12,
) -> Comparison:
    """Price ``usage`` on every option in :data:`OPTIONS` and :data:`GAS_OPTIONS`.

    A tariff without prices for the whole period (launched since) is priced at today's prices,
    and says so in its note.
    """
    result = Comparison(label, len(usage.days), usage.paid_net_electric_p, usage.paid_gas_p)
    for option in OPTIONS:
        imp = catalogue.tariff(option.import_product, region)
        exp = catalogue.tariff(option.export_product, region)
        if not (imp and exp):
            result.electric.append(Result(option.name, None, "not priceable from public data"))
            continue
        net, today = pricer.electricity(usage, imp, exp, offpeak_below_p)
        note = ", ".join(filter(None, [option.note, TODAY_NOTE if today else ""]))
        result.electric.append(Result(option.name, net, note if net is not None else "no prices"))
    for name in GAS_OPTIONS:
        code = catalogue.tariff(name, region, "gas")
        if not code:
            result.gas.append(Result(name, None, "not offered"))
            continue
        total, today = pricer.gas(usage, code)
        result.gas.append(
            Result(name, total, (TODAY_NOTE if today else "") if total is not None else "no prices")
        )
    return result


def money(pence: float) -> str:
    """Pounds with the sign in front: ``"£2.54"``, or ``"-£3.98"`` for a net credit."""
    return f"-{pounds(-pence)}" if pence < 0 else pounds(pence)


def _versus(cost: float, paid: float) -> str:
    """``"£1.20 less"`` / ``"£1.20 more"`` / ``"the same"`` compared with what was paid."""
    diff = cost - paid
    if abs(diff) < 0.5:
        return "the same"
    return f"{pounds(abs(diff))} {'less' if diff < 0 else 'more'}"


#: Within this many days of a fixed deal ending, the report title says so.
ENDING_SOON = timedelta(days=45)


def format_report(c: Comparison, today: date, show: int = 4) -> tuple[str, str]:
    """Phone notification: the cheapest options against what was actually paid."""
    electric = c.ranked(c.electric)
    paid = money(c.paid_net_electric_p)
    lines = [f"Electricity minus export, your real use over {c.days} days: you paid {paid}."]
    for r in electric[:show]:
        note = f" ({r.note})" if r.note else ""
        lines.append(
            f"{r.name}: {money(r.net_p or 0.0)}, {_versus(r.net_p or 0.0, c.paid_net_electric_p)}{note}"
        )
    gas = c.ranked(c.gas)
    if gas and c.paid_gas_p:
        best = gas[0]
        note = f", {best.note}" if best.note else ""
        lines.append(
            f"Gas: you paid {money(c.paid_gas_p)}; {best.name} would be {money(best.net_p or 0.0)} "
            f"({_versus(best.net_p or 0.0, c.paid_gas_p)}{note})."
        )
    if c.deal_ends:
        ends = ", ".join(f"{fuel} {day:%-d %b %Y}" for fuel, day in sorted(c.deal_ends.items()))
        lines.append(f"Your fixed deals end: {ends}.")
    ending = sorted((day, fuel) for fuel, day in c.deal_ends.items() if today <= day <= today + ENDING_SOON)
    if ending and electric:
        day, fuel = ending[0]
        title = f"Tariff check {c.label}: your {fuel} fix ends {day:%-d %b}; best now {electric[0].name}"
    elif electric and (electric[0].net_p or 0.0) < c.paid_net_electric_p - 100:
        saving = pounds(c.paid_net_electric_p - (electric[0].net_p or 0.0))
        title = f"Tariff check {c.label}: {electric[0].name} could save {saving}"
    else:
        title = f"Tariff check {c.label}: your tariff is still the best fit"
    return title, "\n".join(lines)


def display(c: Comparison) -> dict[str, str]:
    """Status fields for ``tariffs.json`` (see ``docs/tariff-compare.md``)."""
    electric = c.ranked(c.electric)
    gas = c.ranked(c.gas)
    best = electric[0] if electric else None
    return {
        "period": c.label,
        "paid": money(c.paid_net_electric_p),
        "best": best.name if best else "-",
        "best_cost": money(best.net_p or 0.0) if best else "-",
        "versus": _versus(best.net_p or 0.0, c.paid_net_electric_p) if best else "-",
        "gas_paid": money(c.paid_gas_p),
        "gas_best": f"{gas[0].name} {money(gas[0].net_p or 0.0)}" if gas else "-",
        "fix_ends": f"{c.deal_ends['electricity']:%-d %b %Y}" if "electricity" in c.deal_ends else "-",
    }


def month_period(month: str | None, today: date) -> tuple[datetime, datetime, str]:
    """Start, end and label of ``month`` (``YYYY-MM``) or of the previous calendar month."""
    if month:
        first = date.fromisoformat(f"{month}-01")
    else:
        first = (today.replace(day=1) - timedelta(days=1)).replace(day=1)
    following = (first + timedelta(days=32)).replace(day=1)
    start = datetime.combine(first, time()).astimezone()
    end = datetime.combine(following, time()).astimezone()
    return start, end, f"{first:%b %Y}"


def main() -> None:
    """Entry point: compare one month (meant for a monthly timer, a few days into the month)."""
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    parser = argparse.ArgumentParser(description="Price your actual usage on other Octopus tariffs.")
    parser.add_argument("--month", help="YYYY-MM (default: last month)")
    parser.add_argument("--dry-run", action="store_true", help="print instead of sending")
    args = parser.parse_args()

    outputs = Outputs.from_env()
    config = json.loads(Path(env_str("OCTOPUS_CONFIG", "/etc/octopus/config.json")).read_text())
    today = datetime.now().astimezone().date()
    start, end, label = month_period(args.month, today)
    client = OctopusClient("", fetch=lambda url, auth: public_get(url))

    def rates(fuel: str, tariff: str, what: str, lo: datetime, hi: datetime) -> RateTable:
        return RateTable(client.rates(fuel, tariff, what, lo.astimezone(UTC), hi.astimezone(UTC)))

    usage = load_usage(vm_series(env_str("VM_URL")), start, end)
    if not usage.imports:
        log.warning("no Octopus data for %s", label)
        return
    comparison = compare(
        usage,
        Catalogue.load(),
        Pricer(rates, (start, end), today - timedelta(days=1)),
        region_of(config),
        label,
        env_float("OFFPEAK_BELOW_P", 12),
    )
    comparison.deal_ends = deal_ends(config)
    title, message = format_report(comparison, today)
    for r in comparison.electric + comparison.gas:
        log.info("%-24s %s %s", r.name, money(r.net_p) if r.net_p is not None else "-", r.note)
    if args.dry_run:
        print(f"{title}\n{message}")
        return
    Notifier(outputs.ntfy_url).send(title, message, tags="bulb")
    write_status(outputs.status_dir, "tariffs", display(comparison))


if __name__ == "__main__":
    main()
