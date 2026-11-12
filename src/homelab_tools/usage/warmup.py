"""How long each heating zone takes to warm up after its target temperature is raised.

A *warm-up* starts when a zone's target rises by at least ``min_step`` °C (a heating schedule
block starting) while the room is at least ``min_gap`` °C below the new target. It ends when the
room gets within ``tolerance`` of the target, or - if it never does - when the target changes
again. Warm-ups still in progress when the data ends are left for the next run.

Slow warm-ups are a useful early sign that the boiler's flow temperature (or weather-compensation
curve) is too low for the weather, or that a radiator valve is stuck: the gas bill may look fine
while rooms stay cold for longer.
"""

from __future__ import annotations

import json
import statistics
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

#: ``(metric, start, end) -> {zone: {moment: value}}``.
ZoneSeries = Callable[[str, datetime, datetime], dict[str, dict[datetime, float]]]


@dataclass(frozen=True)
class Warmup:
    """One warm-up of one zone.

    Attributes:
        zone: tado zone name.
        start: When the target was raised.
        target: The new target (°C).
        start_temp: Room temperature when it was raised (°C).
        reached: When the room got to the target; ``None`` if the target changed first.
        best: Warmest the room got during the warm-up (°C).
        outside: Outside temperature at the start (°C), if known.
    """

    zone: str
    start: datetime
    target: float
    start_temp: float
    reached: datetime | None
    best: float
    outside: float | None

    @property
    def minutes(self) -> float | None:
        """Minutes to reach the target, or ``None`` if it wasn't reached."""
        return None if self.reached is None else (self.reached - self.start).total_seconds() / 60


def vm_zone_series(vm_url: str) -> ZoneSeries:
    """A :data:`ZoneSeries` reading raw samples through VictoriaMetrics' export API."""

    def series(metric: str, start: datetime, end: datetime) -> dict[str, dict[datetime, float]]:
        params = {"match[]": metric, "start": start.isoformat(), "end": end.isoformat()}
        query = urllib.parse.urlencode(params)
        out: dict[str, dict[datetime, float]] = {}
        with urllib.request.urlopen(f"{vm_url}/api/v1/export?{query}", timeout=60) as resp:
            for line in resp:
                row = json.loads(line)
                zone = row["metric"].get("zone", "")
                points = out.setdefault(zone, {})
                for ms, value in zip(row["timestamps"], row["values"], strict=True):
                    points[datetime.fromtimestamp(ms / 1000, UTC)] = float(value)
        return out

    return series


def _before(points: Sequence[tuple[datetime, float]], moment: datetime) -> float | None:
    """The last value at or before ``moment`` (or the first after it, within 10 minutes)."""
    value = None
    for when, v in points:
        if when > moment:
            return v if value is None and when - moment <= timedelta(minutes=10) else value
        value = v
    return value


def find_warmups(
    zone: str,
    inside: Mapping[datetime, float],
    target: Mapping[datetime, float],
    outside: Mapping[datetime, float] | None = None,
    min_step: float = 1.0,
    min_gap: float = 1.0,
    tolerance: float = 0.1,
) -> list[Warmup]:
    """Every complete warm-up in the data, oldest first."""
    temps = sorted(inside.items())
    targets = sorted(target.items())
    weather = sorted((outside or {}).items())
    found = []
    for i in range(1, len(targets)):
        start, goal = targets[i]
        if goal - targets[i - 1][1] < min_step:
            continue
        start_temp = _before(temps, start)
        if start_temp is None or goal - start_temp < min_gap:
            continue
        # The block ends when the target next changes; without a change, at the end of the data.
        ends = next((when for when, t in targets[i + 1 :] if abs(t - goal) > 0.05), None)
        during = [(when, t) for when, t in temps if when >= start and (ends is None or when < ends)]
        reached = next((when for when, t in during if t >= goal - tolerance), None)
        if reached is None and ends is None:
            continue  # still warming up
        best = max([t for when, t in during if reached is None or when <= reached] or [start_temp])
        found.append(Warmup(zone, start, goal, start_temp, reached, best, _before(weather, start)))
    return found


def usual_minutes(history: Sequence[Warmup], before: Warmup, days: int = 14, need: int = 5) -> float | None:
    """Median minutes of the zone's completed warm-ups in the ``days`` before ``before``."""
    since = before.start - timedelta(days=days)
    times = [
        w.minutes
        for w in history
        if w.zone == before.zone and since <= w.start < before.start and w.minutes is not None
    ]
    return statistics.median(times) if len(times) >= need else None


def is_slow(warmup: Warmup, usual: float | None, factor: float, min_extra: float) -> bool:
    """Whether a warm-up took ``factor`` times its usual time and ``min_extra`` minutes more, or failed."""
    if usual is None:
        return False
    if warmup.minutes is None:
        return True
    return warmup.minutes > factor * usual and warmup.minutes - usual >= min_extra


def duration(minutes: float) -> str:
    """``125`` -> ``"2h 05m"``; under an hour, ``"45m"``."""
    whole = round(minutes)
    return f"{whole // 60}h {whole % 60:02d}m" if whole >= 60 else f"{whole}m"
