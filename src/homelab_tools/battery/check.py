"""``battery-check``: did the home battery charge overnight, and will it last through tomorrow?

Written for a battery that charges on a cheap overnight rate (e.g. Octopus Go, 00:30-05:30) and
powers the house for the rest of the day. Two modes, each run by its own timer:

``morning``  After the cheap window: alert if the battery never reached ``CHARGED_BELOW_PCT``
             overnight - the timed charge did not happen (settings lost after a power cut or
             firmware update, a tripped breaker, an inverter fault).
``evening``  Simulates tomorrow half-hour by half-hour, from the end of the cheap window until it
             opens again: the battery starts full, typical house use (median of recent days at each
             time of day) is taken from it, and the forecast solar generation tops it up. If the
             house would need more than ``SHORTFALL_KWH`` from the grid at the peak rate, it sends a
             heads-up with roughly when the battery runs out, so heavy appliances can wait for the
             cheap rate.

Why not suggest charging *less* on sunny days? With an export rate above the cheap import rate (plus
charging losses), a full battery at dawn plus exported surplus solar earns more than leaving room
for solar, so a full overnight charge is the right default; this tool only warns when even that
will not be enough.

Data comes from VictoriaMetrics (``solis-monitor`` metrics by default, ``octopus-collector`` for the
peak rate) and from the forecast curve that ``solar-forecast`` saves. Nothing on the inverter is
changed.

Settings (environment): ``BATTERY_KWH`` (required), ``VM_URL`` (required), ``NTFY_URL``,
``STATUS_DIR`` and the optional settings in ``docs/battery-check.md``.
"""

from __future__ import annotations

import argparse
import json
import logging
import statistics
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from pathlib import Path

from homelab_tools.common.config import ConfigError, Outputs, env_float, env_int, env_str
from homelab_tools.common.metrics import Query, RangeQuery, vm_query, vm_range
from homelab_tools.common.notify import Notifier
from homelab_tools.common.status import write_status
from homelab_tools.forecast.solar import load_curve

log = logging.getLogger("battery-check")

SLOT = timedelta(minutes=30)
#: Power is sampled this often inside a slot when integrating the forecast curve.
SUBSTEP = timedelta(minutes=5)


@dataclass(frozen=True)
class Battery:
    """The battery as the simulation sees it.

    Attributes:
        capacity_kwh: Nominal capacity.
        full_pct: State of charge reached by the overnight charge.
        reserve_pct: The inverter stops discharging at this state of charge.
        efficiency: One-way efficiency, applied both when charging and discharging.
    """

    capacity_kwh: float
    full_pct: float = 100
    reserve_pct: float = 20
    efficiency: float = 0.95

    @property
    def reserve_kwh(self) -> float:
        """Energy left in the battery when it stops discharging."""
        return self.capacity_kwh * self.reserve_pct / 100


def parse_window(text: str) -> tuple[time, time]:
    """Parse ``CHEAP_HOURS`` such as ``"00:30-05:30"`` or ``"23:30-05:30"``.

    Raises:
        ConfigError: If it is not ``HH:MM-HH:MM`` with two different times.
    """
    try:
        start, end = (time.fromisoformat(part.strip()) for part in text.split("-"))
    except ValueError as exc:
        raise ConfigError(f"CHEAP_HOURS must look like 00:30-05:30, got {text!r}") from exc
    if start == end:
        raise ConfigError("CHEAP_HOURS start and end must differ")
    return start, end


def at_local(day: date, clock: time) -> datetime:
    """``day`` at the local wall-clock time ``clock``, as an aware datetime."""
    return datetime.combine(day, clock).astimezone()


def peak_period(day: date, cheap: tuple[time, time]) -> tuple[datetime, datetime]:
    """From the end of ``day``'s cheap window until the cheap window opens again."""
    start = at_local(day, cheap[1])
    cheap_day = day if cheap[0] > cheap[1] else day + timedelta(days=1)
    return start, at_local(cheap_day, cheap[0])


def slots(start: datetime, end: datetime) -> list[datetime]:
    """Half-hour slot starts from ``start`` up to ``end``, in real time (safe across clock changes)."""
    out = []
    moment = start
    while moment < end:
        out.append(moment.astimezone())
        moment = moment + SLOT
    return out


def power_at(points: Sequence[tuple[datetime, float]], moment: datetime) -> float:
    """Forecast power (W) at ``moment``, linearly interpolated; zero outside the forecast."""
    for (t0, w0), (t1, w1) in zip(points, points[1:], strict=False):
        if t0 <= moment <= t1:
            span = (t1 - t0).total_seconds()
            return w0 + (w1 - w0) * ((moment - t0).total_seconds() / span if span else 0)
    return 0.0


def solar_kwh(points: Sequence[tuple[datetime, float]], start: datetime) -> float:
    """Forecast energy (kWh) in the half-hour slot starting at ``start``."""
    steps = int(SLOT / SUBSTEP)
    total_w = sum(power_at(points, start + SUBSTEP * (i + 0.5)) for i in range(steps))
    return total_w / steps * (SLOT / timedelta(hours=1)) / 1000


def load_profile(rows: Sequence[tuple[float, float]]) -> dict[time, float]:
    """Typical house power per local half-hour of the day: the median of each slot over the days given.

    Args:
        rows: ``(end of slot epoch seconds, average W in the slot)``, as returned by a range query of
            ``avg_over_time(metric[30m])`` with a 30-minute step.
    """
    by_slot: dict[time, list[float]] = {}
    for end, watts in rows:
        start = datetime.fromtimestamp(end - SLOT.total_seconds()).astimezone()  # local time, DST-safe
        by_slot.setdefault(start.time().replace(second=0, microsecond=0), []).append(watts)
    return {slot: statistics.median(values) for slot, values in by_slot.items()}


@dataclass(frozen=True)
class Outlook:
    """Result of simulating one peak period.

    Attributes:
        start: Start of the period (end of the cheap window).
        solar_kwh: Forecast generation in the period.
        load_kwh: Expected house use in the period.
        grid_kwh: House use the battery and solar cannot cover (bought at the peak rate).
        export_kwh: Solar that neither the house nor the battery can take.
        runs_out: When the battery first reaches its reserve, if it does.
    """

    start: datetime
    solar_kwh: float
    load_kwh: float
    grid_kwh: float
    export_kwh: float
    runs_out: datetime | None


def simulate(
    battery: Battery, period: tuple[datetime, datetime], solar: Sequence[float], load: Sequence[float]
) -> Outlook:
    """Step the battery through the period's half-hour slots.

    Args:
        battery: Battery parameters; it starts the period at ``full_pct``.
        period: Start and end of the peak period.
        solar: Forecast kWh per slot.
        load: Expected house kWh per slot.
    """
    starts = slots(*period)
    stored = battery.capacity_kwh * battery.full_pct / 100
    grid = export = 0.0
    runs_out = None
    for start, pv, use in zip(starts, solar, load, strict=True):
        net = pv - use
        if net >= 0:
            room = (battery.capacity_kwh - stored) / battery.efficiency
            stored += min(net, room) * battery.efficiency
            export += max(0.0, net - room)
            continue
        available = (stored - battery.reserve_kwh) * battery.efficiency
        taken = min(-net, max(0.0, available))
        stored -= taken / battery.efficiency
        if taken < -net:
            grid += -net - taken
            if runs_out is None:
                runs_out = start + SLOT * (taken / -net)
    return Outlook(period[0], sum(solar), sum(load), grid, export, runs_out)


def tomorrow_outlook(
    day: date,
    cheap: tuple[time, time],
    battery: Battery,
    curve: Mapping[datetime, float],
    profile: Mapping[time, float],
) -> Outlook:
    """Simulate ``day``'s peak period from a forecast curve and a load profile.

    Raises:
        ValueError: If the load profile lacks a slot, or the forecast does not cover ``day``.
    """
    period = peak_period(day, cheap)
    starts = slots(*period)
    missing = [s for s in starts if s.time() not in profile]
    if missing:
        raise ValueError(f"no house-load history for {len(missing)} half-hours yet (e.g. {missing[0]:%H:%M})")
    points = sorted(curve.items())
    if not any(t.astimezone().date() == day for t, _ in points):
        raise ValueError(f"the solar forecast does not cover {day}")
    solar = [solar_kwh(points, s) for s in starts]
    load = [profile[s.time()] * (SLOT / timedelta(hours=1)) / 1000 for s in starts]
    return simulate(battery, period, solar, load)


def round_to_slot(moment: datetime) -> datetime:
    """Round to the nearest half-hour, for a "around 18:30" estimate."""
    local = moment.astimezone()
    base = local.replace(minute=0, second=0, microsecond=0)
    halves = round((local - base) / SLOT)
    return base + SLOT * halves


def evening_message(
    outlook: Outlook, threshold_kwh: float, peak_rate_p: float | None, cheap_start: time
) -> tuple[str, str] | None:
    """Heads-up for a day when the battery will not last; ``None`` when no warning is needed."""
    if outlook.grid_kwh < threshold_kwh:
        return None
    when = f" around {round_to_slot(outlook.runs_out):%H:%M}" if outlook.runs_out else ""
    cost = f" (about £{outlook.grid_kwh * peak_rate_p / 100:.2f})" if peak_rate_p else ""
    title = f"Battery will run low {outlook.start:%A}"
    message = (
        f"{outlook.solar_kwh:.1f} kWh of solar forecast, against about {outlook.load_kwh:.0f} kWh of "
        f"house use. The battery will probably run out{when}, leaving {outlook.grid_kwh:.1f} kWh to buy "
        f"at the peak rate{cost}. Heavy appliances are cheapest after {cheap_start:%H:%M}."
    )
    return title, message


def morning_message(peak_soc: float | None, threshold_pct: float) -> tuple[str, str] | None:
    """Alert when the overnight charge did not happen; ``None`` if it did or there is no data."""
    if peak_soc is None or peak_soc >= threshold_pct:
        return None
    return (
        "Battery did not charge overnight",
        f"It only reached {peak_soc:.0f}% during the cheap-rate window. Check the inverter's timed-charge "
        "settings (they can reset after a power cut or update), the battery breaker, and the inverter app.",
    )


def outlook_display(outlook: Outlook) -> dict[str, str]:
    """Status fields for the evening check (see ``docs/battery-check.md``)."""
    lasts = f"runs out {round_to_slot(outlook.runs_out):%H:%M}" if outlook.runs_out else "lasts"
    return {
        "outlook_day": f"{outlook.start:%a %d %b}",
        "outlook_solar": f"{outlook.solar_kwh:.1f} kWh",
        "outlook_use": f"{outlook.load_kwh:.1f} kWh",
        "outlook_battery": lasts,
        "outlook_grid": f"{outlook.grid_kwh:.1f} kWh",
    }


def update_status(directory: str, fields: Mapping[str, str]) -> None:
    """Merge ``fields`` into ``battery.json``, keeping the other mode's fields."""
    if not directory:
        return
    try:
        current = json.loads((Path(directory) / "battery.json").read_text())
    except (OSError, ValueError):
        current = {}
    write_status(directory, "battery", current | dict(fields))


@dataclass
class Settings:
    """Everything the checks need, read from the environment by :meth:`from_env`."""

    battery: Battery
    cheap: tuple[time, time]
    charged_below_pct: float
    shortfall_kwh: float
    curve_file: Path
    load_metric: str
    soc_metric: str
    rate_metric: str
    profile_days: int

    @classmethod
    def from_env(cls) -> Settings:
        """Read and validate the settings."""
        return cls(
            battery=Battery(
                env_float("BATTERY_KWH"),
                env_float("BATTERY_FULL_PCT", 100),
                env_float("BATTERY_RESERVE_PCT", 20),
                env_float("BATTERY_EFFICIENCY", 0.95),
            ),
            cheap=parse_window(env_str("CHEAP_HOURS", "00:30-05:30")),
            charged_below_pct=env_float("CHARGED_BELOW_PCT", 90),
            shortfall_kwh=env_float("SHORTFALL_KWH", 1.0),
            curve_file=Path(env_str("SOLAR_CURVE_FILE", "/var/lib/solar-forecast/curve.json")),
            load_metric=env_str("LOAD_METRIC", "solis_load_w"),
            soc_metric=env_str("SOC_METRIC", "solis_soc"),
            rate_metric=env_str("PEAK_RATE_METRIC", "octopus_import_rate_p"),
            profile_days=env_int("PROFILE_DAYS", 7),
        )


def run_morning(
    settings: Settings, query: Query, now: datetime
) -> tuple[dict[str, str], tuple[str, str] | None]:
    """Highest state of charge since the cheap window opened; returns status fields and any alert."""
    opened = at_local(now.date(), settings.cheap[0])
    if opened > now:
        opened -= timedelta(days=1)
    seconds = int((now - opened).total_seconds())
    peak = query(f"max_over_time({settings.soc_metric}[{seconds}s])", now.timestamp())
    fields = {"overnight": f"{peak:.0f}%" if peak is not None else "-", "checked": f"{now:%d %b %H:%M}"}
    if peak is None:
        log.warning("no %s data since %s; cannot check the overnight charge", settings.soc_metric, opened)
    return fields, morning_message(peak, settings.charged_below_pct)


def run_evening(
    settings: Settings, query: Query, history: RangeQuery, day: date, now: datetime
) -> tuple[dict[str, str], tuple[str, str] | None]:
    """Simulate ``day``; returns status fields and any heads-up.

    Raises:
        ValueError: If there is not enough data to simulate (no history, no forecast).
        OSError: If the forecast curve file cannot be read.
    """
    fetched, curve = load_curve(settings.curve_file)
    if now - fetched > timedelta(hours=6):
        log.warning("the solar forecast is from %s; is solar-forecast running?", fetched)
    rows = history(
        f"avg_over_time({settings.load_metric}[30m])",
        (now - timedelta(days=settings.profile_days)).timestamp(),
        now.timestamp(),
        int(SLOT.total_seconds()),
    )
    outlook = tomorrow_outlook(day, settings.cheap, settings.battery, curve, load_profile(rows))
    rate = query(f"max_over_time({settings.rate_metric}[7d])", now.timestamp())
    return outlook_display(outlook), evening_message(outlook, settings.shortfall_kwh, rate, settings.cheap[0])


def main() -> None:
    """Entry point: ``battery-check morning`` or ``battery-check evening [--day YYYY-MM-DD]``."""
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    parser = argparse.ArgumentParser(description="Home battery checks.")
    parser.add_argument("mode", choices=["morning", "evening"])
    parser.add_argument(
        "--day", type=date.fromisoformat, help="evening: the day to simulate (default tomorrow)"
    )
    parser.add_argument("--dry-run", action="store_true", help="print instead of notifying")
    args = parser.parse_args()

    settings = Settings.from_env()
    outputs = Outputs.from_env()
    vm_url = env_str("VM_URL")
    now = datetime.now().astimezone()
    if args.mode == "morning":
        fields, alert = run_morning(settings, vm_query(vm_url), now)
    else:
        day = args.day or now.date() + timedelta(days=1)
        try:
            fields, alert = run_evening(settings, vm_query(vm_url), vm_range(vm_url), day, now)
        except (OSError, ValueError) as exc:
            log.warning("cannot forecast %s: %s", day, exc)
            return
    log.info("%s: %s", args.mode, ", ".join(f"{k}={v}" for k, v in fields.items()))
    if args.dry_run:
        print("\n".join(alert) if alert else "no alert")
        return
    if alert:
        priority = "high" if args.mode == "morning" else "default"
        Notifier(outputs.ntfy_url).send(*alert, priority=priority, tags="battery")
        log.info("sent: %s", alert[0])
    update_status(outputs.status_dir, fields)


if __name__ == "__main__":
    main()
