"""``usage-watch``: alerts when gas or electricity use is out of line with what is normal for the house.

Two checks, run once a day:

**Gas against the weather.** Gas for heating rises as the outside temperature falls, so daily gas
is modelled as ``a + b x max(0, base - mean outside temperature)`` (heating degree-days). The model
is fitted to the last year of complete days from ``octopus-collector``, with daily mean temperatures
from Open-Meteo (free, no account), and the best ``base`` between 12 and 18 °C is chosen. A day is
flagged when it used more than ``GAS_SIGMA`` standard deviations above the model's prediction,
*and* at least ``GAS_MIN_EXCESS_PCT`` % and ``GAS_MIN_EXCESS_KWH`` more: a boiler or thermostat
problem, a heating schedule left on, a hot-water fault. Octopus data arrives a day or two late, so
each new complete day is checked once.

**Electricity always-on load.** Each day's *baseload* is the average of its six quietest half-hours
of house power (``solis_load_w`` by default): fridges, routers, standby. When the baseload is
``BASELOAD_FACTOR`` times its usual level (the median of the previous two weeks) and at least
``BASELOAD_MIN_W`` higher, on two days running, something has probably been left on - a heater, a
dehumidifier, a failing appliance. One alert, then a "back to normal" message when it recovers.

Settings (environment): ``VM_URL`` (required), ``WEATHER_LAT`` / ``WEATHER_LON`` (required for the
gas check), ``NTFY_URL``, ``STATUS_DIR`` and the thresholds in ``docs/usage-watch.md``.
"""

from __future__ import annotations

import argparse
import json
import logging
import statistics
import urllib.parse
import urllib.request
from collections import defaultdict
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from homelab_tools.common.config import Outputs, env_float, env_str
from homelab_tools.common.metrics import RangeQuery, vm_range
from homelab_tools.common.notify import Notifier
from homelab_tools.common.status import write_status
from homelab_tools.octopus.compare import Series, vm_series
from homelab_tools.octopus.pricing import local_day

log = logging.getLogger("usage-watch")

#: Fetches a URL and returns its decoded JSON.
Fetcher = Callable[[str], Any]
ARCHIVE = "https://archive-api.open-meteo.com/v1/archive"
FORECAST = "https://api.open-meteo.com/v1/forecast"
#: A day needs this many half-hours of data to count (46 on the short clock-change day).
COMPLETE_SLOTS = 46


def http_json(url: str) -> Any:
    """Default fetcher: GET and decode JSON."""
    with urllib.request.urlopen(url, timeout=60) as resp:
        return json.load(resp)


def daily_temperatures(
    lat: float, lon: float, start: date, end: date, fetch: Fetcher = http_json
) -> dict[date, float]:
    """Daily mean outside temperature (°C) from ``start`` to ``end`` inclusive.

    The archive lags by a few days, so the most recent days come from the forecast API's
    ``past_days``; archive values win where both exist.
    """
    params = {
        "latitude": lat,
        "longitude": lon,
        "daily": "temperature_2m_mean",
        "timezone": "auto",
    }
    temps: dict[date, float] = {}
    recent = urllib.parse.urlencode(params | {"past_days": 14, "forecast_days": 1})
    archive = urllib.parse.urlencode(params | {"start_date": start.isoformat(), "end_date": end.isoformat()})
    for url in (f"{FORECAST}?{recent}", f"{ARCHIVE}?{archive}"):
        try:
            daily = fetch(url)["daily"]
        except (OSError, KeyError, ValueError) as exc:
            log.warning("weather lookup failed: %s", exc)
            continue
        for day, value in zip(daily["time"], daily["temperature_2m_mean"], strict=True):
            if value is not None and start <= date.fromisoformat(day) <= end:
                temps[date.fromisoformat(day)] = float(value)
    return temps


def daily_totals(slots: Mapping[datetime, float]) -> dict[date, float]:
    """Sum half-hourly values into complete local days (incomplete days are left out)."""
    totals: dict[date, float] = defaultdict(float)
    counts: dict[date, int] = defaultdict(int)
    for moment, value in slots.items():
        totals[local_day(moment)] += value
        counts[local_day(moment)] += 1
    return {day: total for day, total in totals.items() if counts[day] >= COMPLETE_SLOTS}


@dataclass(frozen=True)
class GasModel:
    """Daily gas = ``a + b x max(0, base - temperature)``, with the scatter of the fit.

    Attributes:
        base: Balance temperature (°C) above which no heating is needed.
        a: kWh per day that doesn't depend on the weather (hot water, cooking).
        b: kWh per degree-day.
        sd: Standard deviation of the residuals (kWh).
        days: Number of days fitted.
    """

    base: float
    a: float
    b: float
    sd: float
    days: int

    def predict(self, temperature: float) -> float:
        """Expected kWh for a day with this mean outside temperature."""
        return self.a + self.b * max(0.0, self.base - temperature)

    @classmethod
    def fit(
        cls, gas: Mapping[date, float], temps: Mapping[date, float], bases: Sequence[float] = range(12, 19)
    ) -> GasModel | None:
        """Least-squares fit for each candidate ``base``; the one with the smallest scatter wins.

        Returns:
            ``None`` with fewer than 60 days of data, or no temperature variation.
        """
        days = [d for d in gas if d in temps]
        if len(days) < 60:
            return None
        best: GasModel | None = None
        for base in bases:
            xs = [max(0.0, base - temps[d]) for d in days]
            ys = [gas[d] for d in days]
            mx, my = statistics.fmean(xs), statistics.fmean(ys)
            var = sum((x - mx) ** 2 for x in xs)
            if var == 0:
                continue
            b = sum((x - mx) * (y - my) for x, y in zip(xs, ys, strict=True)) / var
            a = my - b * mx
            sd = statistics.pstdev([y - (a + b * x) for x, y in zip(xs, ys, strict=True)])
            if best is None or sd < best.sd:
                best = cls(float(base), a, b, sd, len(days))
        return best


@dataclass(frozen=True)
class GasCheck:
    """One day's gas against the model."""

    day: date
    used: float
    expected: float
    temperature: float
    unusual: bool


def check_gas(
    model: GasModel,
    day: date,
    used: float,
    temperature: float,
    sigma: float,
    min_pct: float,
    min_kwh: float = 5.0,
) -> GasCheck:
    """Compare a day's gas with the model's prediction; every threshold must be exceeded."""
    expected = model.predict(temperature)
    excess = used - expected
    unusual = excess > sigma * model.sd and excess > expected * min_pct / 100 and excess >= min_kwh
    return GasCheck(day, used, expected, temperature, unusual)


def baseloads(rows: Sequence[tuple[float, float]], quiet_slots: int = 6) -> dict[date, float]:
    """Each local day's baseload: the mean of its ``quiet_slots`` lowest half-hour averages (W).

    Args:
        rows: ``(end of slot epoch seconds, average W)`` from ``avg_over_time(metric[30m])`` with a
            30-minute step. Days with fewer than 40 half-hours of data are left out.
        quiet_slots: How many of the quietest half-hours to average.
    """
    by_day: dict[date, list[float]] = defaultdict(list)
    for end, watts in rows:
        by_day[datetime.fromtimestamp(end - 1800).astimezone().date()].append(watts)
    return {
        day: statistics.fmean(sorted(values)[:quiet_slots])
        for day, values in by_day.items()
        if len(values) >= 40
    }


@dataclass(frozen=True)
class BaseloadCheck:
    """Recent baseload against its usual level."""

    latest: float
    usual: float
    high: bool


def check_baseload(
    loads: Mapping[date, float], factor: float, min_w: float, run: int = 2, history: int = 14
) -> BaseloadCheck | None:
    """Whether the last ``run`` complete days all had a high baseload.

    Returns:
        ``None`` until there are at least 7 days of history before the last ``run`` days.
    """
    days = sorted(loads)
    recent, before = days[-run:], days[:-run][-history:]
    if len(recent) < run or len(before) < 7:
        return None
    usual = statistics.median(loads[d] for d in before)
    limit = max(usual * factor, usual + min_w)
    return BaseloadCheck(loads[days[-1]], usual, all(loads[d] > limit for d in recent))


@dataclass
class State:
    """What has already been checked and reported, kept between runs."""

    gas_checked: str = ""
    baseload_high: bool = False

    @classmethod
    def load(cls, path: Path) -> State:
        """Read the state file, or start fresh."""
        try:
            data = json.loads(path.read_text())
            return cls(str(data.get("gas_checked", "")), bool(data.get("baseload_high", False)))
        except (OSError, ValueError):
            return cls()

    def save(self, path: Path) -> None:
        """Write the state file."""
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"gas_checked": self.gas_checked, "baseload_high": self.baseload_high}))


Alert = tuple[str, str, str]  # title, message, priority


def gas_alert(c: GasCheck) -> Alert:
    """Notification for an unusual gas day."""
    return (
        f"Unusual gas use {c.day:%a %-d %b}",
        f"{c.used:.0f} kWh used; about {c.expected:.0f} kWh is normal at {c.temperature:.1f} °C outside "
        f"({c.used - c.expected:.0f} kWh more). Worth checking the heating schedule, thermostat and boiler.",
        "default",
    )


def baseload_alert(c: BaseloadCheck, recovered: bool) -> Alert:
    """Notification for a high always-on electricity load, or its return to normal."""
    if recovered:
        return ("Always-on electricity back to normal", f"{c.latest:.0f} W (usually {c.usual:.0f} W).", "low")
    extra_kwh = (c.latest - c.usual) * 24 / 1000
    return (
        "Something may have been left on",
        f"The house's always-on electricity has been about {c.latest:.0f} W for two days, against a usual "
        f"{c.usual:.0f} W - roughly {extra_kwh:.1f} kWh a day extra. Look for a heater, dehumidifier or "
        "appliance that stays on.",
        "default",
    )


@dataclass
class Watch:
    """One run of both checks.

    Attributes:
        series: Raw-sample reader (gas half-hours).
        history: Range-query function (house power).
        temperatures: ``(start, end) -> {day: °C}``.
        state: Previous progress.
        gas_metric: Half-hourly gas kWh.
        load_metric: House power in W.
        sigma: Gas threshold in standard deviations.
        min_pct: Gas threshold as a percentage of the prediction.
        min_kwh: Gas threshold in kWh above the prediction.
        factor: Baseload threshold as a multiple of usual.
        min_w: Baseload threshold as watts above usual.
    """

    series: Series
    history: RangeQuery
    temperatures: Callable[[date, date], dict[date, float]] | None
    state: State
    gas_metric: str = "octopus_gas_kwh"
    load_metric: str = "solis_load_w"
    sigma: float = 3.0
    min_pct: float = 25.0
    min_kwh: float = 5.0
    factor: float = 1.5
    min_w: float = 100.0

    def run(self, now: datetime) -> tuple[list[Alert], dict[str, str]]:
        """Check new gas days and the electricity baseload; returns alerts and status fields."""
        alerts: list[Alert] = []
        status: dict[str, str] = {}
        if self.temperatures:
            alerts += self._gas(now, status)
        alerts += self._baseload(now, status)
        status["checked"] = f"{now:%d %b %H:%M}"
        return alerts, status

    def _gas(self, now: datetime, status: dict[str, str]) -> list[Alert]:
        gas = daily_totals(self.series(self.gas_metric, now - timedelta(days=400), now))
        if not gas or self.temperatures is None:
            return []
        temps = self.temperatures(min(gas), max(gas))
        new = [d for d in sorted(gas) if d.isoformat() > self.state.gas_checked and d in temps]
        if not self.state.gas_checked:
            new = new[-1:]  # first run: start from the newest day, don't replay a year of history
        alerts = []
        for day in new:
            model = GasModel.fit({d: v for d, v in gas.items() if d != day}, temps)
            if model is None:
                log.info("gas: not enough history to model yet")
                break
            check = check_gas(model, day, gas[day], temps[day], self.sigma, self.min_pct, self.min_kwh)
            log.info(
                "gas %s: %.1f kWh, expected %.1f at %.1f °C (base %.0f °C, sd %.1f)%s",
                day, check.used, check.expected, check.temperature, model.base, model.sd,
                " UNUSUAL" if check.unusual else "",
            )  # fmt: skip
            if check.unusual:
                alerts.append(gas_alert(check))
            self.state.gas_checked = day.isoformat()
            status.update(
                gas_day=f"{day:%a %-d %b}",
                gas_used=f"{check.used:.0f} kWh",
                gas_expected=f"{check.expected:.0f} kWh",
                gas_outside=f"{check.temperature:.1f} °C",
            )
        return alerts

    def _baseload(self, now: datetime, status: dict[str, str]) -> list[Alert]:
        midnight = datetime.combine(now.date(), datetime.min.time()).astimezone()
        rows = self.history(
            f"avg_over_time({self.load_metric}[30m])",
            (midnight - timedelta(days=21)).timestamp(),
            midnight.timestamp(),
            1800,
        )
        check = check_baseload(baseloads(rows), self.factor, self.min_w)
        if check is None:
            log.info("baseload: not enough history yet")
            return []
        log.info("baseload %.0f W, usual %.0f W%s", check.latest, check.usual, " HIGH" if check.high else "")
        status.update(base_now=f"{check.latest:.0f} W", base_usual=f"{check.usual:.0f} W")
        if check.high == self.state.baseload_high:
            return []
        self.state.baseload_high = check.high
        return [baseload_alert(check, recovered=not check.high)]


def main() -> None:
    """Entry point: one daily run."""
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    parser = argparse.ArgumentParser(description="Unusual gas and electricity use alerts.")
    parser.add_argument("--dry-run", action="store_true", help="print alerts instead of sending; keep state")
    args = parser.parse_args()

    outputs = Outputs.from_env()
    vm_url = env_str("VM_URL")
    state_file = Path(env_str("USAGE_STATE_FILE", "/var/lib/usage-watch/state.json"))
    lat, lon = env_str("WEATHER_LAT", ""), env_str("WEATHER_LON", "")
    temperatures = (lambda s, e: daily_temperatures(float(lat), float(lon), s, e)) if lat and lon else None
    watch = Watch(
        series=vm_series(vm_url),
        history=vm_range(vm_url),
        temperatures=temperatures,
        state=State.load(state_file),
        gas_metric=env_str("GAS_METRIC", "octopus_gas_kwh"),
        load_metric=env_str("LOAD_METRIC", "solis_load_w"),
        sigma=env_float("GAS_SIGMA", 3.0),
        min_pct=env_float("GAS_MIN_EXCESS_PCT", 25.0),
        min_kwh=env_float("GAS_MIN_EXCESS_KWH", 5.0),
        factor=env_float("BASELOAD_FACTOR", 1.5),
        min_w=env_float("BASELOAD_MIN_W", 100.0),
    )
    alerts, status = watch.run(datetime.now().astimezone())
    if args.dry_run:
        print("\n".join(f"{t}: {m}" for t, m, _ in alerts) or "no alerts")
        return
    notifier = Notifier(outputs.ntfy_url)
    for title, message, priority in alerts:
        notifier.send(title, message, priority=priority, tags="chart_with_upwards_trend")
    watch.state.save(state_file)
    if status:
        write_status(outputs.status_dir, "usage", status)


if __name__ == "__main__":
    main()
