"""Price half-hourly consumption and roll it up into daily totals.

Everything here is pure (no network), so the arithmetic is covered by unit tests.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime

from homelab_tools.octopus.api import RatePeriod

#: Volume correction factor x a typical calorific value (39.5 MJ/m³) / 3.6 MJ per kWh. Octopus bills
#: with the actual calorific value of your gas, which varies slightly; this is close enough for
#: dashboards. Override with ``GAS_KWH_PER_M3`` if you know your figure.
DEFAULT_GAS_KWH_PER_M3 = 1.02264 * 39.5 / 3.6


class RateTable:
    """Look up the price in force at a moment, across one or more tariff agreements."""

    def __init__(self, periods: Iterable[RatePeriod]) -> None:
        """Store the periods, sorted by start time."""
        self.periods: list[RatePeriod] = sorted(periods, key=lambda p: p.start)

    def at(self, moment: datetime) -> float | None:
        """Return the price (pence) whose period contains ``moment``, or ``None`` if none does."""
        for period in self.periods:
            if period.start <= moment < period.end:
                return period.pence
        return None


@dataclass
class DayTotals:
    """Sums for one local calendar day. Money is in pence, energy in kWh."""

    import_kwh: float = 0.0
    import_cost_p: float = 0.0
    import_slots: int = 0
    import_offpeak_kwh: float = 0.0
    import_peak_kwh: float = 0.0
    export_kwh: float = 0.0
    export_earn_p: float = 0.0
    gas_m3: float = 0.0
    gas_kwh: float = 0.0
    gas_cost_p: float = 0.0
    standing_p: dict[str, float] = field(default_factory=dict)  # fuel -> pence
    nosolar_cost_p: float = 0.0
    house_slots: int = 0  # half-hours with house-use data

    @property
    def complete(self) -> bool:
        """Whether all half-hours are in (46-50 on clock-change days).

        Octopus' newest day is usually partial, so summaries skip incomplete days.
        """
        return self.import_slots >= 46

    @property
    def electricity_p(self) -> float:
        """Import cost plus the electricity standing charge."""
        return self.import_cost_p + self.standing_p.get("electricity", 0.0)

    @property
    def gas_p(self) -> float:
        """Gas cost plus the gas standing charge."""
        return self.gas_cost_p + self.standing_p.get("gas", 0.0)

    @property
    def net_p(self) -> float:
        """What the day costs overall: electricity + gas (each incl. standing charge) - export earnings."""
        return self.electricity_p + self.gas_p - self.export_earn_p

    @property
    def saving_p(self) -> float | None:
        """Solar/battery saving, if house-use data was available for the day.

        Defined as: what the house's own use would have cost at the import rates with no solar or
        battery, minus what was actually paid for imports, plus export earnings. Standing charges
        are left out because they are paid either way.

        The saving is only given for days where every imported half-hour also has house-use data:
        a part-covered day (e.g. the first day of inverter monitoring) would compare a whole day's
        bill with part of a day's use.
        """
        if not self.nosolar_cost_p or self.house_slots < self.import_slots:
            return None
        return self.nosolar_cost_p - self.import_cost_p + self.export_earn_p


def local_day(moment: datetime) -> date:
    """The local calendar day a slot belongs to."""
    return moment.astimezone().date()


@dataclass
class Ledger:
    """Accumulates priced slots into :class:`DayTotals` and remembers the metric samples to write.

    Attributes:
        offpeak_below_p: Import slots cheaper than this (pence) count as off-peak, e.g. the
            Octopus Go night window. Set it between your tariff's cheap and normal rates.
        days: Daily totals keyed by local date.
        points: ``(metric field, slot time, value, tags)`` tuples for the time-series database.
    """

    offpeak_below_p: float = 12.0
    days: dict[date, DayTotals] = field(default_factory=lambda: defaultdict(DayTotals))
    points: list[tuple[str, datetime, float, dict[str, str]]] = field(default_factory=list)

    def _point(self, name: str, moment: datetime, value: float, tags: dict[str, str] | None = None) -> None:
        self.points.append((name, moment, value, tags or {}))

    def add_import(self, slots: Sequence[tuple[datetime, float]], rates: RateTable) -> None:
        """Price grid-import slots (kWh) at the import unit rate."""
        for moment, kwh in slots:
            day = self.days[local_day(moment)]
            day.import_kwh += kwh
            day.import_slots += 1
            self._point("import_kwh", moment, kwh)
            rate = rates.at(moment)
            if rate is None:
                continue
            cost = kwh * rate
            day.import_cost_p += cost
            self._point("import_rate_p", moment, rate)
            self._point("import_cost_p", moment, cost)
            if rate < self.offpeak_below_p:
                day.import_offpeak_kwh += kwh
                self._point("import_offpeak_kwh", moment, kwh)
            else:
                day.import_peak_kwh += kwh
                self._point("import_peak_kwh", moment, kwh)

    def add_export(self, slots: Sequence[tuple[datetime, float]], rates: RateTable) -> None:
        """Price export slots (kWh) at the export rate."""
        for moment, kwh in slots:
            day = self.days[local_day(moment)]
            day.export_kwh += kwh
            self._point("export_kwh", moment, kwh)
            rate = rates.at(moment)
            if rate is not None:
                day.export_earn_p += kwh * rate
                self._point("export_rate_p", moment, rate)
                self._point("export_earn_p", moment, kwh * rate)

    def add_gas(self, slots: Sequence[tuple[datetime, float]], rates: RateTable, kwh_per_m3: float) -> None:
        """Convert gas volume (m³) to kWh and price it."""
        for moment, m3 in slots:
            kwh = m3 * kwh_per_m3
            day = self.days[local_day(moment)]
            day.gas_m3 += m3
            day.gas_kwh += kwh
            self._point("gas_m3", moment, m3)
            self._point("gas_kwh", moment, kwh)
            rate = rates.at(moment)
            if rate is not None:
                day.gas_cost_p += kwh * rate
                self._point("gas_cost_p", moment, kwh * rate)

    def add_standing(self, fuel: str, slots: Sequence[tuple[datetime, float]], rates: RateTable) -> None:
        """Add one standing charge per local day that has data.

        The charge is looked up at the day's *first slot with data* rather than at local midnight:
        when the fetch window starts at UTC midnight during British Summer Time, local midnight
        (23:00 UTC the day before) lies outside the loaded rates and the day would get no charge.
        The metric sample is timestamped at local *noon*, for the same reason: a midnight stamp
        falls on the previous UTC day in summer, so daily charts counted it on the wrong day.
        """
        first: dict[date, datetime] = {}
        for moment, _ in slots:
            day = local_day(moment)
            if day not in first or moment < first[day]:
                first[day] = moment
        for day, moment in sorted(first.items()):
            pence = rates.at(moment)
            if pence is not None:
                self.days[day].standing_p[fuel] = pence
                noon = moment.astimezone().replace(hour=12, minute=0, second=0, microsecond=0)
                self._point("standing_p", noon, pence, {"fuel": fuel})

    def add_house_use(self, house_kwh: dict[datetime, float], import_rates: RateTable) -> None:
        """Record what the house's own use would have cost on the import tariff, slot by slot."""
        for moment, kwh in house_kwh.items():
            rate = import_rates.at(moment)
            if rate is not None:
                self.days[local_day(moment)].nosolar_cost_p += kwh * rate
                self.days[local_day(moment)].house_slots += 1
                self._point("nosolar_cost_p", moment, kwh * rate)


def pounds(pence: float) -> str:
    """Format pence as pounds, e.g. ``254.3`` -> ``"£2.54"``."""
    return f"£{pence / 100:.2f}"


def summarise(days: dict[date, DayTotals]) -> dict[str, str] | None:
    """Display strings for the newest complete day and the month it falls in.

    Returns:
        ``None`` until at least one complete day is available.
    """
    complete = sorted(d for d, totals in days.items() if totals.complete and totals.import_cost_p)
    if not complete:
        return None
    latest = complete[-1]
    t = days[latest]
    month = [v for d, v in days.items() if (d.year, d.month) == (latest.year, latest.month)]
    saving = t.saving_p
    return {
        "day": latest.strftime("%a ") + f"{latest.day} " + latest.strftime("%b"),
        "import_kwh": f"{t.import_kwh:.1f} kWh",
        "import_cost": pounds(t.electricity_p),
        "export_kwh": f"{t.export_kwh:.1f} kWh",
        "export_earn": pounds(t.export_earn_p),
        "gas_kwh": f"{t.gas_kwh:.1f} kWh",
        "gas_cost": pounds(t.gas_p),
        "net": pounds(t.net_p),
        "saved": pounds(saving) if saving is not None else "pending",
        "month_net": pounds(sum(v.net_p for v in month)),
        "month_days": f"{len(month)} days",
    }
