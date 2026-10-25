"""``tado-monitor``: poll tado zones and weather, publish them, and alert on device problems.

Read-only: only GET requests are made, so it cannot change the heating.

tado limits API calls per day: 100 for accounts without an Auto-Assist subscription, 20,000 with
one. Each round costs two calls (zone states + weather), plus one every ``TADO_DEVICE_EVERY`` rounds
for device health. With the defaults (every 120 s, devices every 15th round) that is about 1,500
calls a day - set ``TADO_INTERVAL=1800`` or more on a free account.

Settings (environment):

``TADO_TOKEN_FILE``     Refresh token written by ``tado-auth`` (default /var/lib/tado-monitor/token.json).
``TADO_STATE_FILE``     Which problems were already alerted (default /var/lib/tado-monitor/state.json).
``TADO_INTERVAL``       Seconds between rounds, default 120.
``TADO_DEVICE_EVERY``   Check device health every N rounds, default 15.

Outputs: ``tado_*{zone=...}`` and ``tado_weather_*`` metrics, ``$STATUS_DIR/tado.json``, ntfy alerts.
"""

from __future__ import annotations

import json
import logging
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from pathlib import Path
from typing import Any

from homelab_tools.common.config import Outputs, env_int, env_str
from homelab_tools.common.metrics import Sample, VictoriaMetrics
from homelab_tools.common.notify import Notifier
from homelab_tools.common.status import write_status
from homelab_tools.tado.auth import DEFAULT_TOKEN_FILE, AccessToken, TokenStore
from homelab_tools.tado.model import (
    DeviceProblem,
    device_problems,
    display,
    new_problems,
    parse_zone_state,
    zone_metrics,
)

API = "https://my.tado.com/api/v2"
log = logging.getLogger("tado-monitor")


class TadoClient:
    """GET-only tado API client."""

    def __init__(
        self, token: Callable[[], str], opener: Callable[[urllib.request.Request], Any] | None = None
    ) -> None:
        """Use ``token()`` for a bearer token on each call."""
        self._token = token
        self._open = opener or (lambda req: urllib.request.urlopen(req, timeout=30))

    def get(self, path: str) -> Any:
        """GET ``API + path`` and return the decoded JSON."""
        request = urllib.request.Request(API + path, headers={"Authorization": f"Bearer {self._token()}"})
        with self._open(request) as resp:
            return json.load(resp)


class TadoMonitor:
    """One tado home: polls it and publishes readings."""

    def __init__(
        self,
        client: TadoClient,
        notifier: Notifier,
        metrics: VictoriaMetrics,
        status_dir: str,
        state_file: Path,
        device_every: int = 15,
    ) -> None:
        """Find the home and load which problems were already alerted."""
        self.client, self.notifier, self.metrics = client, notifier, metrics
        self.status_dir, self.state_file, self.device_every = status_dir, state_file, device_every
        self.home = client.get("/me")["homes"][0]["id"]
        self.zone_names: dict[str, str] = {}
        self.devices = 0
        self.problems: list[DeviceProblem] = []
        try:
            self.alerted: set[str] = set(json.loads(state_file.read_text()).get("alerted", []))
        except (OSError, ValueError):
            self.alerted = set()
        self.rounds = 0

    def check_devices(self) -> None:
        """Refresh zone names and device health; alert once per new problem."""
        zones = self.client.get(f"/homes/{self.home}/zones")
        self.zone_names = {str(z["id"]): z["name"] for z in zones}
        self.devices = sum(len(z.get("devices", [])) for z in zones)
        self.problems = device_problems(zones)
        fresh, self.alerted = new_problems(self.problems, self.alerted)
        for p in fresh:
            self.notifier.send(p.title, p.message, tags="thermometer,warning")
        self.state_file.parent.mkdir(parents=True, exist_ok=True)
        self.state_file.write_text(json.dumps({"alerted": sorted(self.alerted)}))

    def poll_once(self) -> None:
        """One round: device health (every Nth round), zone states and weather."""
        if self.rounds % self.device_every == 0:
            self.check_devices()
        self.rounds += 1
        states = self.client.get(f"/homes/{self.home}/zoneStates")["zoneStates"]
        weather = self.client.get(f"/homes/{self.home}/weather")
        zones = [parse_zone_state(self.zone_names.get(zid, zid), s) for zid, s in states.items()]
        outside = weather["outsideTemperature"]["celsius"]
        solar = (weather.get("solarIntensity") or {}).get("percentage")
        self.metrics.write(
            [Sample("tado", zone_metrics(z), {"zone": z.name}) for z in zones]
            + [Sample("tado_weather", {"outside_c": outside, "solar_pct": solar})]
        )
        write_status(
            self.status_dir,
            "tado",
            display(zones, outside, weather["weatherState"]["value"], self.devices, self.problems),
        )


def main() -> None:
    """Entry point: poll forever; back off for 30 minutes if tado rate-limits us."""
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    outputs = Outputs.from_env()
    token = AccessToken(TokenStore(env_str("TADO_TOKEN_FILE", DEFAULT_TOKEN_FILE)))
    monitor = TadoMonitor(
        TadoClient(token.get),
        Notifier(outputs.ntfy_url),
        VictoriaMetrics(outputs.vm_url),
        outputs.status_dir,
        Path(env_str("TADO_STATE_FILE", "/var/lib/tado-monitor/state.json")),
        env_int("TADO_DEVICE_EVERY", 15),
    )
    interval = env_int("TADO_INTERVAL", 120)
    log.info("watching tado home %s every %ss", monitor.home, interval)
    while True:
        try:
            monitor.poll_once()
        except urllib.error.HTTPError as exc:
            log.warning("tado API error %s %s", exc.code, exc.reason)
            if exc.code == 429:
                time.sleep(1800)
        except (OSError, KeyError, ValueError) as exc:
            log.warning("poll failed: %r", exc)
        time.sleep(interval)


if __name__ == "__main__":
    main()
