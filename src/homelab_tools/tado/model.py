"""Interpret tado API responses: zone readings, device health and dashboard strings (no I/O).

Field names follow tado's v2 API (``/homes/{id}/zoneStates``, ``/zones``, ``/weather``).
"""

from __future__ import annotations

import math
import time
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

#: Device type codes -> what people call them (shown in alerts).
DEVICE_NAMES = {
    "RU01": "room thermostat",
    "RU02": "room thermostat",
    "VA01": "radiator valve",
    "VA02": "radiator valve",
    "BR02": "bridge",
    "IB01": "bridge",
    "BU01": "wireless receiver",
}


@dataclass(frozen=True)
class ZoneReading:
    """One heating zone at one moment. ``target_c`` is ``None`` while the zone is switched off."""

    name: str
    inside_c: float | None
    humidity_pct: float | None
    target_c: float | None
    heating_pct: float | None
    open_window: bool
    manual: bool  # an overlay (manual change) is active instead of the schedule


def parse_zone_state(name: str, state: Mapping[str, Any]) -> ZoneReading:
    """Build a :class:`ZoneReading` from one entry of ``zoneStates``."""
    sensors = state.get("sensorDataPoints") or {}
    activity = state.get("activityDataPoints") or {}
    setting = state.get("setting") or {}
    powered = setting.get("power") == "ON"
    return ZoneReading(
        name=name,
        inside_c=(sensors.get("insideTemperature") or {}).get("celsius"),
        humidity_pct=(sensors.get("humidity") or {}).get("percentage"),
        target_c=(setting.get("temperature") or {}).get("celsius") if powered else None,
        heating_pct=(activity.get("heatingPower") or {}).get("percentage"),
        open_window=bool(state.get("openWindow") or state.get("openWindowDetected")),
        manual=bool(state.get("overlay")),
    )


def zone_metrics(z: ZoneReading) -> dict[str, float | int | None]:
    """Numbers for ``tado_<field>{zone=...}``."""
    return {
        "inside_c": z.inside_c,
        "humidity_pct": z.humidity_pct,
        "target_c": z.target_c,
        "heating_pct": z.heating_pct,
        "open_window": int(z.open_window),
    }


@dataclass(frozen=True)
class DeviceProblem:
    """A device needing attention. ``key`` identifies it for alert de-duplication."""

    key: str
    title: str
    message: str


def device_problems(zones: Iterable[Mapping[str, Any]]) -> list[DeviceProblem]:
    """List low batteries and offline devices across all zones of a ``/zones`` response."""
    problems = []
    for zone in zones:
        for device in zone.get("devices", []):
            kind = DEVICE_NAMES.get(device.get("deviceType", ""), device.get("deviceType", "device"))
            serial = device.get("serialNo", "?")
            short = device.get("shortSerialNo", serial)
            where = f"{zone.get('name', 'unknown zone')} {kind} ({short})"
            if device.get("batteryState") == "LOW":
                problems.append(
                    DeviceProblem(
                        f"{serial}:battery",
                        f"tado {kind} battery low",
                        f"{where} has a low battery. Replace its batteries (usually AAA).",
                    )
                )
            if not (device.get("connectionState") or {}).get("value", True):
                problems.append(
                    DeviceProblem(f"{serial}:offline", f"tado {kind} offline", f"{where} is offline.")
                )
    return problems


def new_problems(problems: list[DeviceProblem], alerted: set[str]) -> tuple[list[DeviceProblem], set[str]]:
    """Return problems not yet alerted, and the updated alerted-set.

    A problem that has gone away is dropped from the set, so it alerts again if it comes back
    (e.g. the next time those batteries run low).
    """
    current = {p.key for p in problems}
    fresh = [p for p in problems if p.key not in alerted]
    return fresh, current


def dew_point(temp_c: float, humidity_pct: float) -> float:
    """Dew point (°C) from air temperature and relative humidity (Magnus formula)."""
    g = math.log(max(humidity_pct, 1.0) / 100) + 17.62 * temp_c / (243.12 + temp_c)
    return 243.12 * g / (17.62 - g)


@dataclass
class DampZone:
    """Damp tracking for one zone: when humidity went over the limit, and whether that was alerted."""

    since: float | None = None
    alerted: bool = False


def _hours(seconds: float) -> str:
    """``11400`` -> ``"3h 10m"``."""
    minutes = round(seconds / 60)
    return f"{minutes // 60}h {minutes % 60:02d}m" if minutes >= 60 else f"{minutes}m"


def _damp_alert(z: ZoneReading, high: float, seconds: float) -> tuple[str, str]:
    """The damp warning for one zone that has been at ``high`` % or more for ``seconds``."""
    humidity = z.humidity_pct or 0.0
    dew = ""
    if z.inside_c is not None:
        dew = (
            f" at {z.inside_c:.1f} °C; dew point {dew_point(z.inside_c, humidity):.1f} °C, "
            "so windows and outside walls colder than that get wet"
        )
    return (
        f"Damp: {z.name} humidity {humidity:.0f}%",
        f"{z.name} has been at {high:.0f}% humidity or more for {_hours(seconds)} "
        f"(now {humidity:.0f}%{dew}). Air the room for 10 minutes, use an extractor fan, or dry "
        "washing elsewhere: long spells of damp air lead to condensation and mould.",
    )


def check_damp(
    zones: Iterable[ZoneReading],
    state: dict[str, DampZone],
    now: float,
    high: float = 70.0,
    hours: float = 3.0,
    clear: float = 65.0,
) -> list[tuple[str, str]]:
    """Damp alerts as ``(title, message)``, updating ``state``.

    A zone alerts once its humidity has stayed at ``high`` % or more for ``hours``, and sends a
    "back to normal" message when it drops below ``clear`` % (the gap stops it flapping).
    """
    alerts = []
    for z in zones:
        if z.humidity_pct is None:
            continue
        st = state.setdefault(z.name, DampZone())
        if z.humidity_pct >= high:
            st.since = now if st.since is None else st.since
            if not st.alerted and now - st.since >= hours * 3600:
                st.alerted = True
                alerts.append(_damp_alert(z, high, now - st.since))
        elif st.alerted:
            if z.humidity_pct < clear:
                above = _hours(now - st.since) if st.since is not None else "a while"
                alerts.append(
                    (
                        f"{z.name} humidity back to {z.humidity_pct:.0f}%",
                        f"{z.name} is below {clear:.0f}% again, after a damp spell of {above}.",
                    )
                )
                state[z.name] = DampZone()
        else:
            st.since = None
    return alerts


def damp_status(zones: Iterable[ZoneReading], state: Mapping[str, DampZone], now: float | None = None) -> str:
    """``"OK"``, or the damp zones, e.g. ``"Upstairs 72% for 3h 10m"``."""
    now = time.time() if now is None else now
    damp = [
        f"{z.name} {z.humidity_pct:.0f}% for {_hours(now - st.since)}"
        for z in zones
        if z.humidity_pct is not None and (st := state.get(z.name)) is not None and st.since is not None
    ]
    return ", ".join(damp) or "OK"


def zone_key(name: str) -> str:
    """Dashboard key prefix for a zone: first word, lower case (``"Living Room"`` -> ``"living"``)."""
    return name.split()[0].lower() if name.strip() else "zone"


def display(
    zones: list[ZoneReading], outside_c: float, weather: str, devices: int, problems: list[DeviceProblem]
) -> dict[str, str]:
    """Display strings for a dashboard.

    Per-zone keys are ``<zone_key>_temp``, ``_target``, ``_hum``, ``_heat`` and ``_mode``.
    """
    out: dict[str, str] = {}
    for z in zones:
        key = zone_key(z.name)
        out[f"{key}_temp"] = f"{z.inside_c:.1f} °C" if z.inside_c is not None else "-"
        out[f"{key}_target"] = f"{z.target_c:.1f} °C" if z.target_c is not None else "off"
        out[f"{key}_hum"] = f"{z.humidity_pct:.0f}%" if z.humidity_pct is not None else "-"
        if z.open_window:
            out[f"{key}_heat"] = "open window"
        else:
            out[f"{key}_heat"] = f"{z.heating_pct:.0f}%" if z.heating_pct is not None else "-"
        out[f"{key}_mode"] = "manual" if z.manual else "schedule"
    low = sum(1 for p in problems if p.key.endswith(":battery"))
    offline = sum(1 for p in problems if p.key.endswith(":offline"))
    out |= {
        "outside": f"{outside_c:.1f} °C",
        "weather": weather.replace("_", " ").lower(),
        "devices": str(devices),
        "batteries": "all OK" if not low else f"{low} low",
        "offline": str(offline),
    }
    return out
