"""``solar-forecast``: today's and tomorrow's expected solar generation from Forecast.Solar.

Forecast.Solar (https://forecast.solar) estimates PV output from your location, the roof's tilt and
direction, the array size and the weather forecast. The public API needs no account and allows
12 requests per hour per IP address; this tool makes one request per roof plane per run.

Run it hourly from a systemd timer. Each run refreshes ``$STATUS_DIR/forecast.json`` (today's and
tomorrow's estimate, tomorrow's peak hour and - if ``ACTUAL_METRIC`` is set - generation so far
today). The hourly *curve* is written to VictoriaMetrics only once per day, from the first run
after midnight: VictoriaMetrics de-duplicates identical timestamps by keeping the largest value, so
rewriting a revised forecast would leave the most optimistic one in the graphs.

Settings (environment):

``SOLAR_LAT``, ``SOLAR_LON`` (required)  Location in decimal degrees (a nearby town is enough).
``SOLAR_PLANES`` (required)  One ``tilt,azimuth,kWp`` triple per roof plane, separated by ``;``.
                             Tilt: 0 = flat, 90 = vertical. Azimuth: 0 = south, -90 = east,
                             90 = west, 180 = north. Example: ``35,0,6`` or ``35,-90,3;35,90,3``.
``ACTUAL_METRIC``            Optional metric with today's generation in kWh (e.g. ``solis_pv_today_kwh``).
``FORECAST_STATE_FILE``      Which day's curve was stored (default /var/lib/solar-forecast/state.json).
``FORECAST_SOLAR_API_KEY``   Optional paid-plan key (higher limits, more planes).

Metrics: ``forecast_solar_w`` (expected power, hourly points for today) and ``forecast_solar_day_kwh``
(expected total, one sample at local midnight per day).
"""

from __future__ import annotations

import json
import logging
import urllib.parse
import urllib.request
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any

from homelab_tools.common.config import ConfigError, Outputs, env_str
from homelab_tools.common.metrics import Sample, VictoriaMetrics
from homelab_tools.common.status import write_status

API = "https://api.forecast.solar"
log = logging.getLogger("solar-forecast")

#: Fetches a URL and returns its decoded JSON body; replaced by a fake in tests.
Fetcher = Callable[[str], dict[str, Any]]


@dataclass(frozen=True)
class Plane:
    """One roof plane: tilt in degrees, azimuth in degrees (0 = south, -90 = east) and size in kWp."""

    tilt: float
    azimuth: float
    kwp: float


def parse_planes(text: str) -> list[Plane]:
    """Parse ``SOLAR_PLANES`` (``"35,0,6"`` or ``"35,-90,3;35,90,3"``).

    Raises:
        ConfigError: For malformed or out-of-range values.
    """
    planes = []
    for part in filter(None, (p.strip() for p in text.split(";"))):
        try:
            tilt, azimuth, kwp = (float(v) for v in part.split(","))
        except ValueError as exc:
            raise ConfigError(f"SOLAR_PLANES entry {part!r} must be tilt,azimuth,kWp") from exc
        if not (0 <= tilt <= 90 and -180 <= azimuth <= 180 and kwp > 0):
            raise ConfigError(f"SOLAR_PLANES entry {part!r} is out of range")
        planes.append(Plane(tilt, azimuth, kwp))
    if not planes:
        raise ConfigError("SOLAR_PLANES is empty")
    return planes


def estimate_url(lat: float, lon: float, plane: Plane, api_key: str = "") -> str:
    """The Forecast.Solar ``estimate`` URL for one plane (ISO timestamps with UTC offsets)."""
    key = f"/{urllib.parse.quote(api_key)}" if api_key else ""
    return f"{API}{key}/estimate/{lat:g}/{lon:g}/{plane.tilt:g}/{plane.azimuth:g}/{plane.kwp:g}?time=iso8601"


def http_json(url: str) -> dict[str, Any]:
    """Default fetcher: GET and decode JSON."""
    with urllib.request.urlopen(url, timeout=30) as resp:
        result: dict[str, Any] = json.load(resp)
        return result


@dataclass(frozen=True)
class Forecast:
    """Combined forecast for all planes.

    Attributes:
        watts: Expected power (W) at each forecast time.
        day_wh: Expected energy per local day (Wh).
    """

    watts: dict[datetime, float]
    day_wh: dict[date, float]

    def peak(self, day: date) -> datetime | None:
        """Time of the highest expected power on ``day`` (local), or ``None`` if not forecast."""
        points = {t: w for t, w in self.watts.items() if t.astimezone().date() == day and w > 0}
        return max(points, key=lambda t: points[t]) if points else None


def combine(responses: Iterable[dict[str, Any]]) -> Forecast:
    """Sum the ``result.watts`` and ``result.watt_hours_day`` of several plane responses."""
    watts: dict[datetime, float] = {}
    day_wh: dict[date, float] = {}
    for response in responses:
        result = response["result"]
        for stamp, value in result["watts"].items():
            moment = datetime.fromisoformat(stamp)
            watts[moment] = watts.get(moment, 0.0) + float(value)
        for day, value in result["watt_hours_day"].items():
            d = date.fromisoformat(day)
            day_wh[d] = day_wh.get(d, 0.0) + float(value)
    return Forecast(watts, day_wh)


def status(forecast: Forecast, today: date, actual_kwh: float | None) -> dict[str, str]:
    """Display strings for a dashboard status file."""
    tomorrow = date.fromordinal(today.toordinal() + 1)
    peak = forecast.peak(tomorrow)

    def kwh(day: date) -> str:
        return f"{forecast.day_wh[day] / 1000:.1f} kWh" if day in forecast.day_wh else "-"

    return {
        "today_kwh": kwh(today),
        "today_actual": f"{actual_kwh:.1f} kWh" if actual_kwh is not None else "-",
        "tomorrow_kwh": kwh(tomorrow),
        "tomorrow_peak": f"{peak.astimezone():%H:%M}" if peak else "-",
    }


def curve_samples(forecast: Forecast, day: date) -> list[Sample]:
    """Metric samples for ``day``: the hourly power curve plus the day's total at local midnight."""
    samples = [
        Sample("forecast_solar", {"w": w}, time=t)
        for t, w in sorted(forecast.watts.items())
        if t.astimezone().date() == day
    ]
    if day in forecast.day_wh:
        midnight = datetime.combine(day, datetime.min.time()).astimezone()
        samples.append(Sample("forecast_solar", {"day_kwh": forecast.day_wh[day] / 1000}, time=midnight))
    return samples


def latest_value(vm_url: str, metric: str, fetch: Fetcher | None = None) -> float | None:
    """Most recent value of ``metric`` from VictoriaMetrics, or ``None`` if unset or unavailable."""
    if not (vm_url and metric):
        return None
    query = urllib.parse.urlencode({"query": f"last_over_time({metric}[2h])"})
    try:
        result = (fetch or http_json)(f"{vm_url}/api/v1/query?{query}")["data"]["result"]
    except (OSError, KeyError, ValueError):
        return None
    return float(result[0]["value"][1]) if result else None


@dataclass
class ForecastJob:
    """One configured forecast: where, which roof planes, and where results go.

    Attributes:
        lat: Latitude in decimal degrees.
        lon: Longitude in decimal degrees.
        planes: Roof planes to forecast and add up.
        metrics: VictoriaMetrics writer (may be disabled).
        status_dir: Directory for ``forecast.json``; empty disables it.
        state_file: Records the day whose curve was last stored.
        actual: Returns today's generation so far in kWh (or ``None``).
        fetch: HTTP JSON fetcher for Forecast.Solar.
        api_key: Optional paid-plan key.
    """

    lat: float
    lon: float
    planes: list[Plane]
    metrics: VictoriaMetrics
    status_dir: str
    state_file: Path
    actual: Callable[[], float | None] = lambda: None
    fetch: Fetcher = http_json
    api_key: str = ""

    def run(self, today: date | None = None) -> Forecast:
        """Fetch, publish the status file, and store today's curve if not yet stored today."""
        forecast = combine(self.fetch(estimate_url(self.lat, self.lon, p, self.api_key)) for p in self.planes)
        today = today or datetime.now().astimezone().date()
        write_status(
            self.status_dir,
            "forecast",
            status(forecast, today, self.actual()) | {"updated": f"{datetime.now():%H:%M}"},
        )
        if self._stored_day() != today.isoformat() and self.metrics.write(curve_samples(forecast, today)):
            self.metrics.reset_cache()
            self.state_file.parent.mkdir(parents=True, exist_ok=True)
            self.state_file.write_text(json.dumps({"stored": today.isoformat()}))
            log.info("stored the forecast curve for %s", today)
        return forecast

    def _stored_day(self) -> str | None:
        """The day whose curve is already in VictoriaMetrics, if any."""
        try:
            day: str | None = json.loads(self.state_file.read_text()).get("stored")
        except (OSError, ValueError):
            return None
        return day


def main() -> None:
    """Entry point: one run (meant for an hourly systemd timer)."""
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    planes = parse_planes(env_str("SOLAR_PLANES"))
    outputs = Outputs.from_env()
    actual_metric = env_str("ACTUAL_METRIC", "")
    job = ForecastJob(
        lat=float(env_str("SOLAR_LAT")),
        lon=float(env_str("SOLAR_LON")),
        planes=planes,
        metrics=VictoriaMetrics(outputs.vm_url),
        status_dir=outputs.status_dir,
        state_file=Path(env_str("FORECAST_STATE_FILE", "/var/lib/solar-forecast/state.json")),
        actual=lambda: latest_value(outputs.vm_url, actual_metric),
        api_key=env_str("FORECAST_SOLAR_API_KEY", ""),
    )
    forecast = job.run()
    days = ", ".join(f"{d}: {wh / 1000:.1f} kWh" for d, wh in sorted(forecast.day_wh.items()))
    log.info("forecast for %d plane(s): %s", len(planes), days)


if __name__ == "__main__":
    main()
