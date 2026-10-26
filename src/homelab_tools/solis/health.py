"""``solar-health``: a daily check of the solar strings, production against forecast, and battery health.

Run once a day after sunset. Everything comes from metrics already recorded by ``solis-monitor`` (and
``solar-forecast``), so it adds no load on the inverter.

**String balance.** Hybrid inverters with two MPPT inputs report each string's voltage and current.
Each day's energy split between the strings is compared with *your* usual split (the median of the
previous two weeks) - two strings rarely share 50/50 exactly because of shading or orientation.
When the ratio of the two strings' energy moves by more than ``STRING_TOLERANCE`` (20 % by default,
about two failed panels in a string of eight) on two productive days running, it suggests a failed
panel or optimiser, a loose connector or new shading. A string producing almost nothing on a
productive day is reported at once.

**Production against forecast.** Single days vary too much with the weather, so the last 7 days'
ratio of actual to forecast generation is compared with the previous 28 days' ratio. A drop below
``FORECAST_DROP`` of the usual ratio (e.g. 60 %) points to soiling, a fault or a tripped isolator.
It needs three weeks of history before it starts.

**Battery health.** The battery's state of health (SOH, %) is tracked; an alert is sent when it falls
below ``SOH_WARN_PCT`` (typical warranties guarantee 70-80 %) or by ``SOH_DROP_PTS`` points within 30
days, which is much faster than normal ageing. Equivalent full cycles (discharged kWh / capacity)
are counted for the status file.

Each problem is reported once, with a low-priority message when it clears.

Settings (environment): ``VM_URL`` (required), ``BATTERY_KWH`` (for cycles), ``NTFY_URL``,
``STATUS_DIR`` and the thresholds in ``docs/solar-health.md``.
"""

from __future__ import annotations

import json
import logging
import statistics
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from pathlib import Path

from homelab_tools.common.config import Outputs, env_float, env_str
from homelab_tools.common.metrics import Query, RangeQuery, vm_query, vm_range
from homelab_tools.common.notify import Notifier
from homelab_tools.common.status import write_status

log = logging.getLogger("solar-health")

#: Days producing less than this (kWh) are too dull to judge the strings by.
PRODUCTIVE_KWH = 3.0


@dataclass(frozen=True)
class DayStats:
    """One local day's figures (``None`` where the metric had no data).

    Attributes:
        day: The date.
        string1_kwh: Energy from string 1 (DC volts x amps, integrated).
        string2_kwh: Energy from string 2.
        pv_kwh: Inverter's daily generation counter.
        forecast_kwh: Forecast.Solar's estimate for the day.
        soh_pct: Battery state of health at the end of the day.
        battery_out_kwh: Energy discharged from the battery.
    """

    day: date
    string1_kwh: float | None = None
    string2_kwh: float | None = None
    pv_kwh: float | None = None
    forecast_kwh: float | None = None
    soh_pct: float | None = None
    battery_out_kwh: float | None = None

    @property
    def string_ratio(self) -> float | None:
        """String 1's energy divided by string 2's, on a productive day (else ``None``)."""
        if self.string1_kwh is None or not self.string2_kwh:
            return None
        return (
            self.string1_kwh / self.string2_kwh
            if self.string1_kwh + self.string2_kwh >= PRODUCTIVE_KWH
            else None
        )


def local_bounds(day: date) -> tuple[datetime, datetime]:
    """Local midnight to midnight (23-25 hours on clock-change days)."""
    return (
        datetime.combine(day, time()).astimezone(),
        datetime.combine(day + timedelta(days=1), time()).astimezone(),
    )


def collect_day(query: Query, history: RangeQuery, day: date) -> DayStats:
    """Read one day's figures from VictoriaMetrics."""
    start, end = local_bounds(day)
    seconds = int((end - start).total_seconds())
    at_end = end.timestamp() - 1

    def energy(n: int) -> float | None:
        rows = history(f"solis_dc{n}_v * solis_dc{n}_a", start.timestamp(), at_end, 300)
        return sum(w for _, w in rows) * 300 / 3600 / 1000 if rows else None

    noon = datetime.combine(day, time(12)).astimezone().timestamp()
    return DayStats(
        day,
        energy(1),
        energy(2),
        query(f"max_over_time(solis_pv_today_kwh[{seconds}s])", at_end),
        query("last_over_time(forecast_solar_day_kwh[1h])", noon + 60),
        query(f"last_over_time(solis_soh[{seconds}s])", at_end),
        query(f"max_over_time(solis_batt_out_today_kwh[{seconds}s])", at_end),
    )


@dataclass(frozen=True)
class Finding:
    """A check's outcome: ``problem`` is ``None`` when all is well (or there is too little data)."""

    key: str
    title: str
    problem: str | None


def check_strings(days: Sequence[DayStats], tolerance: float, run: int = 2) -> list[Finding]:
    """A dead string today, or a split that moved from the usual one on ``run`` productive days."""
    latest = days[-1]
    dead = None
    if latest.string1_kwh is not None and latest.string2_kwh is not None:
        total = latest.string1_kwh + latest.string2_kwh
        weak = min((latest.string1_kwh, 1), (latest.string2_kwh, 2))
        if total >= PRODUCTIVE_KWH and weak[0] < 0.05 * total:
            amounts = f"{weak[0]:.1f} of {total:.1f} kWh"
            dead = f"string {weak[1]} produced almost nothing ({amounts}) on {latest.day:%-d %b}"
    findings = [Finding("string-dead", "solar string down", dead)]
    productive = [d for d in days if d.string_ratio is not None]
    recent, before = productive[-run:], productive[:-run][-14:]
    moved = None
    if len(recent) == run and len(before) >= 5 and recent[-1].day == latest.day:
        usual = statistics.median(d.string_ratio for d in before if d.string_ratio is not None)
        ratios = [d.string_ratio for d in recent if d.string_ratio is not None]
        if all(abs(r / usual - 1) > tolerance for r in ratios):
            weaker = 1 if ratios[-1] < usual else 2
            change = abs(ratios[-1] / usual - 1) if weaker == 1 else abs(usual / ratios[-1] - 1)
            moved = (
                f"string {weaker} has made about {change:.0%} less than usual compared with the other string "
                f"on the last {run} sunny days - check for shading, a faulty panel or a loose connector"
            )
    findings.append(Finding("string-balance", "solar strings out of balance", moved))
    return findings


def ratio(days: Sequence[DayStats]) -> float | None:
    """Total actual / total forecast over days that have both."""
    pairs = [(d.pv_kwh, d.forecast_kwh) for d in days if d.pv_kwh is not None and d.forecast_kwh]
    forecast = sum(f for _, f in pairs if f)
    return sum(a for a, _ in pairs if a is not None) / forecast if pairs and forecast > 0 else None


def check_forecast(days: Sequence[DayStats], drop: float) -> tuple[Finding, float | None]:
    """The last 7 days' actual/forecast ratio against the previous 28 days'; also returns the recent ratio."""
    recent, before = days[-7:], days[-35:-7]
    now, usual = ratio(recent), ratio(before)
    problem = None
    if now is not None and usual and len([d for d in before if d.forecast_kwh]) >= 14 and now < drop * usual:
        problem = (
            f"solar made {now:.0%} of the forecast over the last week, against a usual {usual:.0%} - "
            "check the panels, isolator switches and inverter"
        )
    return Finding("production", "solar below forecast", problem), now


def check_battery(days: Sequence[DayStats], warn_pct: float, drop_pts: float) -> Finding:
    """State of health below ``warn_pct``, or down ``drop_pts`` within 30 days."""
    soh = [(d.day, d.soh_pct) for d in days if d.soh_pct is not None]
    problem = None
    if soh:
        latest_day, latest = soh[-1]
        month = [v for d, v in soh if latest_day - d <= timedelta(days=30)]
        if latest < warn_pct:
            problem = f"battery health is down to {latest:.0f}%"
        elif month and max(month) - latest >= drop_pts:
            problem = f"battery health fell from {max(month):.0f}% to {latest:.0f}% within 30 days"
    return Finding("battery-soh", "battery health", problem)


@dataclass
class State:
    """Problems already reported (key -> text), kept between runs."""

    open: dict[str, str] = field(default_factory=dict)

    @classmethod
    def load(cls, path: Path) -> State:
        """Read the state file, or start fresh."""
        try:
            return cls(dict(json.loads(path.read_text()).get("open", {})))
        except (OSError, ValueError, AttributeError):
            return cls()

    def save(self, path: Path) -> None:
        """Write the state file."""
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"open": self.open}))


def evaluate(findings: Sequence[Finding], state: State) -> list[tuple[str, str, str]]:
    """New problems as alerts (once), and cleared ones as low-priority messages."""
    alerts = []
    for f in findings:
        if f.problem and f.key not in state.open:
            state.open[f.key] = f.problem
            alerts.append((f"Solar health: {f.title}", f.problem[0].upper() + f.problem[1:] + ".", "default"))
        elif not f.problem and f.key in state.open:
            alerts.append(("Solar health: OK again", f"Cleared: {state.open.pop(f.key)}.", "low"))
    return alerts


def _share(d: DayStats) -> float:
    """String 1's share of the two strings' energy."""
    return (d.string1_kwh or 0.0) / ((d.string1_kwh or 0.0) + (d.string2_kwh or 0.0))


def display(days: Sequence[DayStats], week_ratio: float | None, capacity_kwh: float) -> dict[str, str]:
    """Status fields for ``solar_health.json`` (see ``docs/solar-health.md``)."""
    latest = days[-1]
    shares = [_share(d) for d in days[-15:-1] if d.string_ratio is not None]
    soh = next((d.soh_pct for d in reversed(days) if d.soh_pct is not None), None)
    out = [d.battery_out_kwh for d in days if d.battery_out_kwh is not None]
    first = next((d.day for d in days if d.battery_out_kwh is not None), None)
    share = _share(latest) if latest.string_ratio is not None else None
    return {
        "strings": f"{share:.0%} / {1 - share:.0%}" if share is not None else "-",
        "strings_usual": f"{statistics.median(shares):.0%} / {1 - statistics.median(shares):.0%}"
        if shares
        else "-",
        "week_vs_forecast": f"{week_ratio:.0%}" if week_ratio is not None else "-",
        "battery_health": f"{soh:.0f}%" if soh is not None else "-",
        "cycles": f"{sum(out) / capacity_kwh:.1f} since {first:%-d %b}"
        if out and first and capacity_kwh
        else "-",
    }


def run_checks(
    days: Sequence[DayStats],
    state: State,
    tolerance: float,
    drop: float,
    warn_pct: float,
    drop_pts: float,
    capacity: float,
) -> tuple[list[tuple[str, str, str]], dict[str, str]]:
    """All checks on the collected days; returns alerts and status fields."""
    findings = check_strings(days, tolerance)
    production, week_ratio = check_forecast(days, drop)
    findings += [production, check_battery(days, warn_pct, drop_pts)]
    return evaluate(findings, state), display(days, week_ratio, capacity)


def collect(query: Query, history: RangeQuery, last: date, days: int = 36) -> list[DayStats]:
    """``days`` local days ending with ``last``, oldest first, skipping days with no data at all."""
    out = []
    for n in range(days - 1, -1, -1):
        stats = collect_day(query, history, last - timedelta(days=n))
        if any(v is not None for v in (stats.string1_kwh, stats.pv_kwh, stats.soh_pct)):
            out.append(stats)
    return out


def main() -> None:
    """Entry point: one check including today (meant for a timer after sunset)."""
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    outputs = Outputs.from_env()
    vm_url = env_str("VM_URL")
    state_file = Path(env_str("SOLAR_HEALTH_STATE_FILE", "/var/lib/solar-health/state.json"))
    days = collect(vm_query(vm_url), vm_range(vm_url), datetime.now().astimezone().date())
    if not days:
        log.warning("no solis metrics found")
        return
    state = State.load(state_file)
    alerts, status = run_checks(
        days,
        state,
        tolerance=env_float("STRING_TOLERANCE", 0.2),
        drop=env_float("FORECAST_DROP", 0.6),
        warn_pct=env_float("SOH_WARN_PCT", 80),
        drop_pts=env_float("SOH_DROP_PTS", 3),
        capacity=env_float("BATTERY_KWH", 0),
    )
    log.info("%s", ", ".join(f"{k}={v}" for k, v in status.items()))
    notifier = Notifier(outputs.ntfy_url)
    for title, message, priority in alerts:
        log.info("%s: %s", title, message)
        notifier.send(title, message, priority=priority, tags="sunny")
    state.save(state_file)
    write_status(outputs.status_dir, "solar_health", status | {"checked": f"{datetime.now():%d %b %H:%M}"})


if __name__ == "__main__":
    main()
