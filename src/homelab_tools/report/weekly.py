"""``weekly-report``: a Monday-to-Sunday energy summary sent to your phone.

It reads only from VictoriaMetrics, using the metrics recorded by the other tools; every section is
optional and simply left out when its metrics are missing:

* money - ``octopus-collector`` (import, export, gas, standing charges, off-peak share);
* solar and battery - ``solis-monitor`` (daily counters ``solis_pv_today_kwh`` etc.);
* car - ``podpoint-monitor`` (``ev_session_*``);
* heating - ``tado-monitor`` (average heating demand and outside temperature, the coldest day,
  humidity and damp hours) and ``usage-watch`` (gas against what the weather predicted, and how
  quickly rooms warmed up compared with the fortnight before);
* systems - ``internet-monitor`` (outages, speeds), ``powerline-monitor`` (link speed),
  ``solar-health`` (battery state of health), plus the current ``host-health`` and restore-test
  results read from ``$STATUS_DIR``.

Octopus data arrives 1-2 days late, so run it on **Tuesday morning** for the week that ended on
Sunday; the report says if some days' costs were still missing.

Settings (environment): ``VM_URL`` (required), ``NTFY_URL``, ``STATUS_DIR`` (writes ``weekly.json``).

Usage: ``weekly-report`` (last complete week), ``--week-ending YYYY-MM-DD`` (the week ending on that
Sunday), ``--dry-run`` (print instead of sending).
"""

from __future__ import annotations

import argparse
import json
import logging
import statistics
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path

from homelab_tools.common.config import Outputs, env_str
from homelab_tools.common.metrics import Query, QueryBy, vm_query, vm_query_by
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
    gas_model_kwh: float | None = None
    gas_expected_kwh: float | None = None
    gas_model_days: int = 0
    coldest_day: date | None = None
    coldest_c: float | None = None
    warmup_ratio: float | None = None
    warmup_zone_ratio: dict[str, float] = field(default_factory=dict)
    warmups_missed: int = 0
    humidity_max: float | None = None
    damp_hours: float | None = None
    internet_checks: int = 0
    outages: int = 0
    outage_s: float = 0.0
    download_mbps: float | None = None
    upload_mbps: float | None = None
    powerline_min_mbps: float | None = None
    powerline_max_mbps: float | None = None
    battery_soh_pct: float | None = None
    server_problems: int | None = None
    restore_test: str | None = None

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
    def gas_vs_weather(self) -> float | None:
        """Gas used as a fraction of what the weather predicted (``1.1`` = 10% more), if known."""
        if not self.gas_model_kwh or not self.gas_expected_kwh:
            return None
        return self.gas_model_kwh / self.gas_expected_kwh

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


def collect(query: Query, start: date, by_zone: QueryBy | None = None) -> WeekFigures:
    """Gather the week starting on ``start`` (a Monday) from the time-series database.

    ``by_zone`` (a query keyed by the ``zone`` label) adds each heating zone's warm-up change.
    """
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
    w.internet_checks = int(query(f"sum(count_over_time(internet_up[{window}]))", at) or 0)
    w.outages = int(query(f"sum(count_over_time(internet_outage_s[{window}]))", at) or 0)
    w.outage_s = query(f"sum(sum_over_time(internet_outage_s[{window}]))", at) or 0.0
    w.download_mbps = query(f"quantile_over_time(0.5, internet_download_mbps[{window}])", at)
    w.upload_mbps = query(f"quantile_over_time(0.5, internet_upload_mbps[{window}])", at)
    w.powerline_min_mbps = query(f"min(avg_over_time(powerline_rate_mbps[{window}]))", at)
    w.powerline_max_mbps = query(f"max(avg_over_time(powerline_rate_mbps[{window}]))", at)
    w.battery_soh_pct = query(f"last_over_time(solis_soh[{window}])", at)
    collect_heating(query, w, days, window, at, by_zone)
    return w


def collect_heating(
    query: Query, w: WeekFigures, days: list[date], window: str, at: float, by_zone: QueryBy | None = None
) -> None:
    """Add the heating figures: gas against the weather, coldest day, warm-ups and humidity."""
    # usage-watch's daily gas samples sit at the start of their day (local midnight), so look back
    # from just before the week's end: that takes in Monday 00:00 but not the next Monday's.
    before_end = at - 1
    w.gas_model_days = int(query(f"sum(count_over_time(usage_gas_kwh[{window}]))", before_end) or 0)
    if w.gas_model_days:
        w.gas_model_kwh = query(f"sum(sum_over_time(usage_gas_kwh[{window}]))", before_end)
        w.gas_expected_kwh = query(f"sum(sum_over_time(usage_gas_expected_kwh[{window}]))", before_end)
    for day in days:
        span, end = day_window(day)
        mean = query(f"avg(avg_over_time(tado_weather_outside_c[{span}]))", end)
        if mean is not None and (w.coldest_c is None or mean < w.coldest_c):
            w.coldest_day, w.coldest_c = day, mean
    # each zone's median last-degree time this week over its median in the two weeks before
    ratio = (
        f"quantile_over_time(0.5, heating_warmup_last_min[{window}]) "
        f"/ quantile_over_time(0.5, heating_warmup_last_min[14d] offset {window})"
    )
    if by_zone is not None:
        w.warmup_zone_ratio = dict(sorted(by_zone(ratio, at).items()))
    if w.warmup_zone_ratio:
        w.warmup_ratio = statistics.fmean(w.warmup_zone_ratio.values())
    else:
        w.warmup_ratio = query(f"avg({ratio})", at)
    # heating_warmup_reached is 1/0 per warm-up: the count minus the sum is the ones not reached,
    # less those cut short (target lowered early, e.g. Away mode)
    reached = f"heating_warmup_reached[{window}]"
    missed = f"sum(count_over_time({reached}) - sum_over_time({reached}))"
    cut = query(f"sum(sum_over_time(heating_warmup_cut_short[{window}]))", at) or 0
    w.warmups_missed = max(0, round((query(missed, at) or 0) - cut))
    w.humidity_max = query(f"max(max_over_time(tado_humidity_pct[{window}]))", at)
    if w.humidity_max is not None:
        w.damp_hours = (
            query(f"sum(sum_over_time((tado_humidity_pct >= bool 70)[{window}:10m]))", at) or 0
        ) / 6


def read_status_files(w: WeekFigures, status_dir: str) -> None:
    """Add the current server-health and restore-test results from the tools' status files."""
    if not status_dir:
        return

    def load(name: str) -> dict[str, str]:
        try:
            data: dict[str, str] = json.loads((Path(status_dir) / f"{name}.json").read_text())
            return data
        except (OSError, ValueError):
            return {}

    problems = load("host").get("problems")
    if problems is not None and problems.isdigit():
        w.server_problems = int(problems)
    backup = load("backup")
    if backup.get("result"):
        w.restore_test = backup["result"] + (f" ({backup['tested']})" if backup.get("tested") else "")


def _duration(seconds: float) -> str:
    minutes = round(seconds / 60)
    if minutes < 60:
        return f"{minutes} min"
    hours, minutes = divmod(minutes, 60)
    return f"{hours} h {minutes} min" if minutes else f"{hours} h"


def systems_line(w: WeekFigures) -> str | None:
    """``"Systems: internet down twice (9 min); ..."``, or ``None`` with nothing to say."""
    parts = []
    if w.outages:
        times = {1: "once", 2: "twice"}.get(w.outages, f"{w.outages} times")
        parts.append(f"internet down {times} ({_duration(w.outage_s)} in total)")
    elif w.internet_checks:
        parts.append("no internet outages")
    if w.download_mbps is not None:
        up = f", up {w.upload_mbps:.0f}" if w.upload_mbps is not None else ""
        parts.append(f"speed tests {w.download_mbps:.0f} Mbps down{up}")
    if w.powerline_min_mbps is not None and w.powerline_max_mbps is not None:
        low, high = w.powerline_min_mbps, w.powerline_max_mbps
        parts.append(
            f"powerline {low:.0f}-{high:.0f} Mbps" if high - low >= 1 else f"powerline {low:.0f} Mbps"
        )
    if w.battery_soh_pct is not None:
        parts.append(f"battery health {w.battery_soh_pct:.0f}%")
    if w.server_problems is not None:
        n = w.server_problems
        parts.append("server OK" if n == 0 else f"server has {n} open problem{'s' if n != 1 else ''}")
    if w.restore_test:
        parts.append(f"last restore test {w.restore_test}")
    return "Systems: " + "; ".join(parts) + "." if parts else None


def _percent(ratio: float, more: str, less: str, same: str, than: str) -> str:
    """``1.13`` -> ``"13% <more> <than>"``; within 3% of 1, ``"<same>"``."""
    if abs(ratio - 1) < 0.03:
        return same
    return f"{abs(ratio - 1) * 100:.0f}% {more if ratio > 1 else less} {than}"


def _signed_pct(ratio: float) -> str:
    """``1.13`` -> ``"+13%"``, ``0.95`` -> ``"-5%"``; under half a percent either way, ``"0%"``."""
    pct = round((ratio - 1) * 100)
    return f"{pct:+d}%" if pct else "0%"


def _warmup_change(w: WeekFigures) -> str:
    """``"Downstairs +40%, Upstairs -3%"`` with several zones, else e.g. ``"-5%"``; ``"-"`` if unknown."""
    if len(w.warmup_zone_ratio) > 1:
        return ", ".join(f"{zone} {_signed_pct(r)}" for zone, r in w.warmup_zone_ratio.items())
    return _signed_pct(w.warmup_ratio) if w.warmup_ratio is not None else "-"


def heating_line(w: WeekFigures) -> str | None:
    """``"Heating: gas 13% below what the weather predicted; ..."``, or ``None`` with nothing to say."""
    parts = []
    if (ratio := w.gas_vs_weather) is not None:
        days = f" ({w.gas_model_days} days)" if w.gas_model_days < 7 else ""
        predicted = "what the weather predicted"
        parts.append("gas " + _percent(ratio, "above", "below", f"about {predicted}", predicted) + days)
    before = "the fortnight before"
    if len(w.warmup_zone_ratio) > 1:
        rooms = ", ".join(
            f"{zone} " + _percent(r, "slower", "quicker", "about the same", "").rstrip()
            for zone, r in w.warmup_zone_ratio.items()
        )
        parts.append(f"last degree against {before}: {rooms}")
    elif w.warmup_ratio is not None:
        speed = _percent(w.warmup_ratio, "slower", "quicker", f"about as quick as {before}", f"than {before}")
        parts.append(f"rooms' last degree {speed}")
    if w.warmups_missed:
        plural = "s" if w.warmups_missed != 1 else ""
        parts.append(f"{w.warmups_missed} warm-up{plural} never reached the target")
    if w.coldest_day is not None and w.coldest_c is not None:
        parts.append(f"coldest day {w.coldest_day:%a}, {w.coldest_c:.1f}°C on average")
    if w.humidity_max is not None:
        damp = (
            f"damp (70%+) for {w.damp_hours:.0f} h in total"
            if w.damp_hours and w.damp_hours >= 0.5
            else "no damp spells"
        )
        parts.append(f"humidity peaked at {w.humidity_max:.0f}%, {damp}")
    return "Heating: " + "; ".join(parts) + "." if parts else None


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
    heating = heating_line(w)
    if heating:
        lines.append(heating)
    systems = systems_line(w)
    if systems:
        lines.append(systems)
    title = f"Week {w.label}" + (f": {_pounds(w.net_p)}" if w.net_p is not None else "")
    return title, "\n".join(lines) if lines else "No data was recorded this week."


def display(w: WeekFigures, prev: WeekFigures | None) -> dict[str, str]:
    """Display strings for a dashboard (``weekly.json``)."""

    def money(v: float | None) -> str:
        return _pounds(v) if v is not None else "-"

    def kwh(v: float | None) -> str:
        return f"{v:.0f} kWh" if v is not None else "-"

    def delta(now: float | None, before: float | None, pence: bool = False, why: str = "-") -> str:
        if now is None:
            return "-"
        if before is None:
            return why  # why there is nothing to compare with
        diff = now - before
        text = _pounds(abs(diff)) if pence else f"{abs(diff):.0f} kWh"
        return f"{'▲' if diff > 0 else '▼'} {text}" if abs(diff) >= (1 if pence else 0.5) else "same"

    p = prev
    # money and gas come from Octopus: only compare when both weeks have every day's data
    octopus_comparable = p is not None and w.octopus_days == 7 and p.octopus_days == 7
    # the same for solar: a week that started mid-way (e.g. new monitoring) is not comparable
    solar_comparable = p is not None and w.solar_days == 7 and p.solar_days == 7
    why = "first week" if p is None else "part week"
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
        "net_change": delta(w.net_p, p.net_p if p and octopus_comparable else None, True, why),
        "solar_change": delta(w.solar_kwh, p.solar_kwh if p and solar_comparable else None, why=why),
        "car_change": delta(w.car_kwh, p.car_kwh if p else None, why="first week" if p is None else "-"),
        "gas_change": delta(w.gas_kwh, p.gas_kwh if p and octopus_comparable else None, why=why),
        "outages": (f"{w.outages}, {_duration(w.outage_s)}" if w.outages else "none")
        if w.internet_checks or w.outages
        else "-",
        "broadband": f"{w.download_mbps:.0f} / {w.upload_mbps or 0:.0f} Mbps"
        if w.download_mbps is not None
        else "-",
        "powerline": f"{w.powerline_min_mbps:.0f}-{w.powerline_max_mbps:.0f} Mbps"
        if w.powerline_min_mbps is not None and w.powerline_max_mbps is not None
        else "-",
        "gas_vs_weather": _signed_pct(w.gas_vs_weather) if w.gas_vs_weather is not None else "-",
        "warmup_change": _warmup_change(w),
        "coldest": f"{w.coldest_day:%a} {w.coldest_c:.1f} °C"
        if w.coldest_day is not None and w.coldest_c is not None
        else "-",
        "damp": "-"
        if w.humidity_max is None
        else f"{w.damp_hours:.0f} h"
        if w.damp_hours and w.damp_hours >= 0.5
        else "none",
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
    by_zone = vm_query_by(env_str("VM_URL"), "zone")
    if args.week_ending:
        start = args.week_ending - timedelta(days=6)
    else:
        start, _ = last_complete_week(datetime.now().astimezone().date())
    this_week = collect(query, start, by_zone)
    read_status_files(this_week, outputs.status_dir)
    prev_week = collect(query, start - timedelta(days=7), by_zone)
    title, message = format_report(this_week, prev_week)
    if args.dry_run:
        print(f"{title}\n{message}")
        return
    Notifier(outputs.ntfy_url).send(title, message, tags="bar_chart")
    write_status(outputs.status_dir, "weekly", display(this_week, prev_week))
    log.info("%s | %s", title, message.replace("\n", " | "))


if __name__ == "__main__":
    main()
