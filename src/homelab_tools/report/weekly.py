"""``weekly-report``: a Monday-to-Sunday energy summary sent to your phone.

It reads only from VictoriaMetrics, using the metrics recorded by the other tools; every section is
optional and simply left out when its metrics are missing:

* money - ``octopus-collector`` (import, export, gas, standing charges, off-peak share);
* solar and battery - ``solis-monitor`` (daily counters ``solis_pv_today_kwh`` etc.);
* car - ``podpoint-monitor`` (``ev_session_*``);
* heating - ``tado-monitor`` (average heating demand and outside temperature).

Octopus data arrives 1-2 days late, so run it on **Tuesday morning** for the week that ended on
Sunday; the report says if some days' costs were still missing.

Settings (environment): ``VM_URL`` (required), ``NTFY_URL``, ``STATUS_DIR`` (writes ``weekly.json``).

Usage: ``weekly-report`` (last complete week), ``--week-ending YYYY-MM-DD`` (the week ending on that
Sunday), ``--dry-run`` (print instead of sending).
"""

from __future__ import annotations

import argparse
import logging
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

from homelab_tools.common.config import Outputs, env_str
from homelab_tools.common.metrics import Query, vm_query
from homelab_tools.common.notify import Notifier
from homelab_tools.common.status import write_status

log = logging.getLogger("weekly-report")


def local_midnight(day: date) -> datetime:
    """Aware datetime for local midnight at the start of ``day``."""
    return datetime.combine(day, datetime.min.time()).astimezone()


def last_complete_week(today: date) -> tuple[date, date]:
    """(Monday, next Monday) of the most recent Monday-to-Sunday week that has fully ended."""
    this_monday = today - timedelta(days=today.weekday())
    return this_monday - timedelta(days=7), this_monday


@dataclass
class WeekFigures:
    """Totals for one week. Money in pence, energy in kWh; ``None`` = no data recorded.

    ``octopus_days`` and ``solar_days`` say how many of the seven days had data, so a partial week
    can be flagged rather than silently under-reported.
    """

    start: date
    import_cost_p: float | None = None
    export_earn_p: float | None = None
    gas_cost_p: float | None = None
    standing_p: dict[str, float] = field(default_factory=dict)
    import_kwh: float | None = None
    offpeak_kwh: float | None = None
    peak_kwh: float | None = None
    export_kwh: float | None = None
    gas_kwh: float | None = None
    octopus_days: int = 0
    solar_kwh: float | None = None
    solar_days: int = 0
    battery_in_kwh: float | None = None
    battery_out_kwh: float | None = None
    car_kwh: float | None = None
    car_cost_p: float | None = None
    car_sessions: int = 0
    heating_pct: float | None = None
    outside_c: float | None = None

    @property
    def electricity_p(self) -> float | None:
        """Import cost plus the electricity standing charge."""
        if self.import_cost_p is None:
            return None
        return self.import_cost_p + self.standing_p.get("electricity", 0.0)

    @property
    def gas_p(self) -> float | None:
        """Gas cost plus the gas standing charge."""
        if self.gas_cost_p is None:
            return None
        return self.gas_cost_p + self.standing_p.get("gas", 0.0)

    @property
    def net_p(self) -> float | None:
        """Electricity + gas (each incl. standing charge) - export earnings."""
        if self.electricity_p is None:
            return None
        return self.electricity_p + (self.gas_p or 0.0) - (self.export_earn_p or 0.0)

    @property
    def offpeak_share(self) -> float | None:
        """Share of grid import bought at the off-peak rate (0-1)."""
        if not self.offpeak_kwh and not self.peak_kwh:
            return None
        return (self.offpeak_kwh or 0.0) / ((self.offpeak_kwh or 0.0) + (self.peak_kwh or 0.0))

    @property
    def label(self) -> str:
        """``"20-26 Oct"`` (or ``"27 Oct-2 Nov"`` across a month end)."""
        end = self.start + timedelta(days=6)
        first = f"{self.start.day}" if self.start.month == end.month else f"{self.start.day} {self.start:%b}"
        return f"{first}-{end.day} {end:%b}"


def day_window(day: date) -> tuple[str, float]:
    """MetricsQL range covering one local day (23-25 h around clock changes) and its end (epoch)."""
    start, end = local_midnight(day), local_midnight(day + timedelta(days=1))
    return f"{int((end - start).total_seconds())}s", end.timestamp()


def collect(query: Query, start: date) -> WeekFigures:
    """Gather the week starting on ``start`` (a Monday) from the time-series database."""
    days = [start + timedelta(days=i) for i in range(7)]
    t_start, t_end = local_midnight(start), local_midnight(start + timedelta(days=7))
    window = f"{int((t_end - t_start).total_seconds())}s"  # 167 h or 169 h in clock-change weeks
    at = t_end.timestamp()

    def total(metric: str) -> float | None:
        return query(f"sum(sum_over_time({metric}[{window}]))", at)

    def daily_max(metric: str) -> tuple[float | None, int]:
        """Sum of per-day maxima of a counter that resets at local midnight, and the days with data."""
        values = []
        for day in days:
            span, end = day_window(day)
            value = query(f"max_over_time({metric}[{span}])", end)
            if value is not None:
                values.append(value)
        return (sum(values) if values else None), len(values)

    def complete_octopus_day(day: date) -> bool:
        span, end = day_window(day)
        return (query(f"count_over_time(octopus_import_kwh[{span}])", end) or 0) >= 46

    w = WeekFigures(start)
    w.import_cost_p = total("octopus_import_cost_p")
    w.export_earn_p = total("octopus_export_earn_p")
    w.gas_cost_p = total("octopus_gas_cost_p")
    for fuel in ("electricity", "gas"):
        pence = total(f'octopus_standing_p{{fuel="{fuel}"}}')
        if pence is not None:
            w.standing_p[fuel] = pence
    w.import_kwh = total("octopus_import_kwh")
    w.offpeak_kwh = total("octopus_import_offpeak_kwh")
    w.peak_kwh = total("octopus_import_peak_kwh")
    w.export_kwh = total("octopus_export_kwh")
    w.gas_kwh = total("octopus_gas_kwh")
    w.octopus_days = sum(1 for day in days if complete_octopus_day(day))
    w.solar_kwh, w.solar_days = daily_max("solis_pv_today_kwh")
    w.battery_in_kwh, _ = daily_max("solis_batt_in_today_kwh")
    w.battery_out_kwh, _ = daily_max("solis_batt_out_today_kwh")
    w.car_kwh = total("ev_session_kwh")
    w.car_cost_p = total("ev_session_cost_p")
    w.car_sessions = int(query(f"sum(count_over_time(ev_session_kwh[{window}]))", at) or 0)
    w.heating_pct = query(f"avg(avg_over_time(tado_heating_pct[{window}]))", at)
    w.outside_c = query(f"avg(avg_over_time(tado_weather_outside_c[{window}]))", at)
    return w


def _pounds(pence: float) -> str:
    """``1420.4`` -> ``"£14.20"``; negative values get a minus sign before the pound sign."""
    return f"-£{-pence / 100:.2f}" if pence < 0 else f"£{pence / 100:.2f}"


def _change(now: float | None, before: float | None, unit: str, money: bool = False) -> str:
    """``"£2.10 less than the week before"``-style comparison, or ``""`` if either side is missing."""
    if now is None or before is None:
        return ""
    diff = now - before
    amount = _pounds(abs(diff)) if money else f"{abs(diff):.0f} {unit}"
    if (money and abs(diff) < 1) or (not money and abs(diff) < 0.5):
        return "the same as the week before"
    return f"{amount} {'more' if diff > 0 else 'less'} than the week before"


def format_report(w: WeekFigures, prev: WeekFigures | None) -> tuple[str, str]:
    """Return (title, message) for the notification."""
    lines = []
    if w.net_p is not None:
        parts = [f"electric {_pounds(w.electricity_p or 0)}"]
        if w.gas_p is not None:
            parts.append(f"gas {_pounds(w.gas_p)}")
        if w.export_earn_p:
            parts.append(f"export -{_pounds(w.export_earn_p)}")
        # comparing a part-week with a full week would mislead: only compare complete weeks
        comparable = prev is not None and w.octopus_days == 7 and prev.octopus_days == 7
        change = _change(w.net_p, prev.net_p if comparable and prev else None, "", money=True)
        line = f"Bill {_pounds(w.net_p)} ({', '.join(parts)})" + (f" - {change}" if change else "") + "."
        if w.octopus_days < 7:
            line += f" Costs cover {w.octopus_days} of 7 days so far."
        lines.append(line)
    if w.solar_kwh is not None:
        line = f"Solar {w.solar_kwh:.0f} kWh"
        if w.solar_days < 7:
            line += f" ({w.solar_days} day{'s' if w.solar_days != 1 else ''} recorded)"
        if w.battery_out_kwh is not None:
            line += f", battery supplied {w.battery_out_kwh:.0f} kWh"
        lines.append(line + ".")
    if w.car_sessions:
        cost = f", {_pounds(w.car_cost_p)}" if w.car_cost_p is not None else ""
        plural = "s" if w.car_sessions != 1 else ""
        lines.append(f"Car {w.car_kwh or 0:.0f} kWh{cost} ({w.car_sessions} charge{plural}).")
    usage = []
    if w.offpeak_share is not None:
        usage.append(f"{w.offpeak_share * 100:.0f}% of grid import off-peak")
    if w.gas_kwh:
        gas = f"gas {w.gas_kwh:.0f} kWh"
        comparable = prev is not None and w.octopus_days == 7 and prev.octopus_days == 7
        change = _change(w.gas_kwh, prev.gas_kwh if comparable and prev else None, "kWh")
        usage.append(gas + (f" ({change})" if change else ""))
    if w.heating_pct is not None:
        outside = f", outside {w.outside_c:.1f}°C on average" if w.outside_c is not None else ""
        usage.append(f"heating demand {w.heating_pct:.0f}%{outside}")
    if usage:
        lines.append(usage[0][0].upper() + "; ".join(usage)[1:] + ".")
    title = f"Week {w.label}" + (f": {_pounds(w.net_p)}" if w.net_p is not None else "")
    return title, "\n".join(lines) if lines else "No data was recorded this week."


def display(w: WeekFigures, prev: WeekFigures | None) -> dict[str, str]:
    """Display strings for a dashboard (``weekly.json``)."""

    def money(v: float | None) -> str:
        return _pounds(v) if v is not None else "-"

    def kwh(v: float | None) -> str:
        return f"{v:.0f} kWh" if v is not None else "-"

    def delta(now: float | None, before: float | None, pence: bool = False) -> str:
        if now is None or before is None:
            return "-"
        diff = now - before
        text = _pounds(abs(diff)) if pence else f"{abs(diff):.0f} kWh"
        return f"{'▲' if diff > 0 else '▼'} {text}" if abs(diff) >= (1 if pence else 0.5) else "same"

    p = prev
    # money and gas come from Octopus: only compare when both weeks have every day's data
    octopus_comparable = p is not None and w.octopus_days == 7 and p.octopus_days == 7
    # the same for solar: a week that started mid-way (e.g. new monitoring) is not comparable
    solar_comparable = p is not None and w.solar_days == 7 and p.solar_days == 7
    return {
        "week": w.label,
        "net": money(w.net_p),
        "electric": money(w.electricity_p),
        "gas": money(w.gas_p),
        "export": money(w.export_earn_p),
        "solar": kwh(w.solar_kwh),
        "car": kwh(w.car_kwh) if w.car_sessions else "0 kWh",
        "offpeak": f"{w.offpeak_share * 100:.0f}%" if w.offpeak_share is not None else "-",
        "gas_kwh": kwh(w.gas_kwh),
        "net_change": delta(w.net_p, p.net_p if p and octopus_comparable else None, pence=True),
        "solar_change": delta(w.solar_kwh, p.solar_kwh if p and solar_comparable else None),
        "car_change": delta(w.car_kwh, p.car_kwh if p else None),
        "gas_change": delta(w.gas_kwh, p.gas_kwh if p and octopus_comparable else None),
    }


def main() -> None:
    """Entry point: summarise the last complete week (or ``--week-ending``) and send it."""
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    parser = argparse.ArgumentParser(description="Weekly energy summary.")
    parser.add_argument("--week-ending", type=date.fromisoformat, help="the Sunday the week ends on")
    parser.add_argument("--dry-run", action="store_true", help="print instead of sending")
    args = parser.parse_args()

    outputs = Outputs.from_env()
    query = vm_query(env_str("VM_URL"))
    if args.week_ending:
        start = args.week_ending - timedelta(days=6)
    else:
        start, _ = last_complete_week(datetime.now().astimezone().date())
    this_week = collect(query, start)
    prev_week = collect(query, start - timedelta(days=7))
    title, message = format_report(this_week, prev_week)
    if args.dry_run:
        print(f"{title}\n{message}")
        return
    Notifier(outputs.ntfy_url).send(title, message, tags="bar_chart")
    write_status(outputs.status_dir, "weekly", display(this_week, prev_week))
    log.info("%s | %s", title, message.replace("\n", " | "))


if __name__ == "__main__":
    main()
