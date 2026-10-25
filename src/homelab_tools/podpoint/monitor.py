"""``podpoint-monitor``: watch a Pod Point charger and publish its state and charging sessions.

Read-only: only the library's *read* calls are used (``async_get_all_pods``,
``async_get_connectivity_status``, ``async_get_charges``); nothing changes schedules or charging.
Pod Point has no public API - this uses the mobile app's API via ``podpointclient``, so it may
break if Pod Point changes it. The status file's ``updated`` time then stops moving; no false alerts
are sent. Install with ``pip install "homelab-tools[podpoint]"``.

Settings (environment):

``PODPOINT_CREDENTIALS_FILE``  File with the app login: email on line 1, password on line 2 (mode 600).
``PODPOINT_STATE_FILE``        Last seen session id (default /var/lib/podpoint-monitor/state.json).
``PODPOINT_INTERVAL``          Seconds between polls, default 180.

Outputs: ``ev_charging/plugged/online`` metrics each poll, ``ev_session_kwh/_cost_p/_charging_s``
once per finished session (timestamped at its end; the whole history is backfilled on first run),
``$STATUS_DIR/ev.json``, and ntfy on "charging started" / "charge complete".
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Any

from homelab_tools.common.config import Outputs, env_int, env_str
from homelab_tools.common.metrics import Sample, VictoriaMetrics
from homelab_tools.common.notify import Notifier
from homelab_tools.common.status import write_status
from homelab_tools.podpoint.model import (
    Charge,
    ChargerState,
    charger_state,
    completion_message,
    display,
    month_sessions,
    new_finished,
)

log = logging.getLogger("podpoint-monitor")


def _dt(value: Any) -> datetime | None:
    """Accept a datetime or an ISO string from the library; ``None`` stays ``None``."""
    if value is None or isinstance(value, datetime):
        return value
    return datetime.fromisoformat(str(value))


def to_charge(obj: Any) -> Charge:
    """Convert a ``podpointclient`` charge object into a :class:`Charge`."""
    seconds = getattr(getattr(obj, "charging_duration", None), "raw", 0) or 0
    starts = _dt(obj.starts_at)
    assert starts is not None
    return Charge(
        int(obj.id),
        float(obj.kwh_used or 0),
        float(obj.energy_cost or 0),
        starts,
        _dt(obj.ends_at),
        int(seconds),
    )


def read_state(pod: Any, connectivity: Any) -> tuple[ChargerState, str, int | None]:
    """Extract (state, connectivity status, Wi-Fi signal in dBm) from library objects."""
    evses = getattr(connectivity, "evses", None) or []
    evse = evses[0] if evses else None
    conn = getattr(evse, "connectivity_state", None)
    connectors = getattr(evse, "connectors", None) or []
    offer = getattr(evse, "energy_offer_status", None)
    status = pod.statuses[0].key_name if getattr(pod, "statuses", None) else "unknown"
    online = getattr(conn, "connectivity_status", "") or ""
    state = charger_state(
        status,
        (connectors[0].charging_state if connectors else "") or "",
        online,
        getattr(offer, "reason", "") or "",
        _dt(getattr(offer, "until", None)),
    )
    return state, online, getattr(conn, "signal_strength", None)


def session_sample(c: Charge) -> Sample:
    """Metric sample for one finished session, timestamped at its end."""
    return Sample(
        "ev",
        {"session_kwh": c.kwh, "session_cost_p": c.cost_p, "session_charging_s": c.charging_seconds},
        time=c.ends,
    )


async def run() -> None:
    """Log in once and poll forever (the library refreshes its token itself)."""
    import aiohttp
    from podpointclient.client import PodPointClient  # imported inside the event loop on purpose:
    # the library creates an aiohttp session at import time, which needs a running loop.

    outputs = Outputs.from_env()
    email, password = Path(env_str("PODPOINT_CREDENTIALS_FILE")).read_text().splitlines()[:2]
    state_file = Path(env_str("PODPOINT_STATE_FILE", "/var/lib/podpoint-monitor/state.json"))
    interval = env_int("PODPOINT_INTERVAL", 180)
    notifier, vm = Notifier(outputs.ntfy_url), VictoriaMetrics(outputs.vm_url)
    try:
        saved: dict[str, Any] = json.loads(state_file.read_text())
    except (OSError, ValueError):
        saved = {}
    last_id: int | None = saved.get("last_charge_id")
    previous_state: str | None = saved.get("state")

    async with aiohttp.ClientSession() as session:
        client = PodPointClient(username=email.strip(), password=password.strip(), session=session)
        if last_id is None:  # first run: backfill all history, announce nothing
            page, history = 1, []
            while batch := await client.async_get_charges(perpage=50, page=page):
                history += [to_charge(c) for c in batch]
                if len(batch) < 50:
                    break
                page += 1
            vm.write(session_sample(c) for c in history if c.ends)
            vm.reset_cache()
            last_id = max((c.id for c in history), default=0)
            log.info("backfilled %d charging sessions", len(history))
        while True:
            try:
                pod = (await client.async_get_all_pods())[0]
                state, online, signal = read_state(pod, await client.async_get_connectivity_status(pod))
                charges = [to_charge(c) for c in await client.async_get_charges(perpage=50, page=1)]
                now = datetime.now().astimezone()
                for c in new_finished(charges, last_id):
                    notifier.send(
                        "Car charge complete", completion_message(c, now), tags="white_check_mark,car"
                    )
                    vm.write([session_sample(c)])
                    vm.reset_cache()
                last_id = max([last_id or 0] + [c.id for c in charges if c.ends])
                if state.key == "charging" and previous_state != "charging":
                    notifier.send(
                        "Car charging started",
                        f"The charger started charging at {now:%H:%M}.",
                        tags="electric_plug,car",
                    )
                previous_state = state.key
                write_status(
                    outputs.status_dir,
                    "ev",
                    display(state, online, signal, charges, month_sessions(charges, now), now)
                    | {"updated": f"{now:%H:%M}"},
                )
                vm.write(
                    [
                        Sample(
                            "ev",
                            {
                                "charging": state.key == "charging",
                                "plugged": state.key not in ("available", "offline"),
                                "online": state.key != "offline",
                            },
                        )
                    ]
                )
                state_file.parent.mkdir(parents=True, exist_ok=True)
                state_file.write_text(json.dumps({"last_charge_id": last_id, "state": previous_state}))
            except Exception as exc:  # noqa: BLE001 - the unofficial API can fail in many ways; retry next round
                log.warning("poll failed: %r", exc)
            await asyncio.sleep(interval)


def main() -> None:
    """Entry point for ``podpoint-monitor``."""
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    asyncio.run(run())


if __name__ == "__main__":
    main()
