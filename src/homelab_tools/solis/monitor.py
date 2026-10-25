"""``solis-monitor``: poll a Solis hybrid inverter every minute and publish what it reports.

Outputs (each optional, see :class:`~homelab_tools.common.config.Outputs`):

* metrics ``solis_<field>`` in VictoriaMetrics, for graphs;
* ``$STATUS_DIR/solis.json`` with display strings, for a dashboard;
* an ntfy alert when fault codes appear, and another when they clear;
* an Uptime Kuma *push* heartbeat (``KUMA_PUSH_URL``): "up" normally, "down" while a fault is
  active, and silence if the inverter stops answering - Kuma alerts on the missing heartbeat.

Settings (environment):

``SOLIS_HOST`` (required)  IP address of the Solis Wi-Fi/LAN data logger.
``SOLIS_PORT``             Modbus TCP port, default 502.
``SOLIS_UNIT``             Modbus unit id of the inverter, default 1.
``SOLIS_INTERVAL``         Seconds between polls, default 60.
``KUMA_PUSH_URL``          Optional Uptime Kuma push URL (``http://kuma:3001/api/push/<token>``).
"""

from __future__ import annotations

import logging
import time
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field

from homelab_tools.common.config import Outputs, env_int, env_str
from homelab_tools.common.metrics import Sample, VictoriaMetrics
from homelab_tools.common.notify import Notifier
from homelab_tools.common.status import write_status
from homelab_tools.solis import modbus
from homelab_tools.solis.registers import (
    REGISTER_BLOCKS,
    Reading,
    decode,
    display_fields,
    metric_fields,
    summary,
)

log = logging.getLogger("solis-monitor")

#: Reads every register in :data:`REGISTER_BLOCKS`; replaced by a fake in tests.
RegisterReader = Callable[[], Mapping[int, int]]


@dataclass
class FaultTracker:
    """Remembers the last fault codes so each change is notified exactly once."""

    last: tuple[int, ...] = ()

    def update(self, codes: tuple[int, ...]) -> str | None:
        """Return ``"raised"``, ``"cleared"`` or ``None`` (no change) for the new fault codes."""
        if codes == self.last:
            return None
        event = "raised" if codes else "cleared"
        self.last = codes
        return event


@dataclass
class SolisMonitor:
    """One inverter and the places its readings are published to."""

    read_registers: RegisterReader
    notifier: Notifier
    metrics: VictoriaMetrics
    status_dir: str
    kuma_push_url: str = ""
    faults: FaultTracker = field(default_factory=FaultTracker)
    http: Callable[[str], object] = lambda url: urllib.request.urlopen(url, timeout=10)

    def poll_once(self) -> Reading:
        """Read the inverter once and publish the result. Raises ``OSError`` if it is unreachable."""
        reading = decode(self.read_registers())
        write_status(self.status_dir, "solis", display_fields(reading))
        self.metrics.write([Sample("solis", metric_fields(reading))])
        event = self.faults.update(reading.fault_codes)
        if event == "raised":
            self.notifier.send(
                "Inverter fault",
                f"The inverter reports fault code(s) {display_fields(reading)['faults']}. "
                f"{summary(reading)}. "
                "Look the codes up in the inverter manual or the Solis app.",
                priority="high",
                tags="warning,sunny",
            )
        elif event == "cleared":
            self.notifier.send("Inverter fault cleared", "No fault codes reported any more.", tags="sunny")
        self._heartbeat(reading)
        return reading

    def _heartbeat(self, reading: Reading) -> None:
        """Push status to Uptime Kuma, if configured (best effort)."""
        if not self.kuma_push_url:
            return
        query = urllib.parse.urlencode(
            {"status": "down" if reading.has_fault else "up", "msg": summary(reading)}
        )
        try:
            self.http(f"{self.kuma_push_url}?{query}")
        except OSError as exc:
            log.warning("Uptime Kuma push failed: %s", exc)


def modbus_reader(host: str, port: int, unit: int, pause: float = 0.5) -> RegisterReader:
    """Build a reader that fetches every register block from a live inverter.

    Args:
        host: Data logger IP address.
        port: Modbus TCP port.
        unit: Modbus unit id.
        pause: Seconds between block reads; Wi-Fi loggers also upload to the cloud and appreciate a gap.
    """

    def read() -> dict[int, int]:
        regs: dict[int, int] = {}
        for i, (address, count) in enumerate(REGISTER_BLOCKS):
            if i:
                time.sleep(pause)
            regs.update(modbus.read_input_registers(host, address, count, port=port, unit=unit))
        return regs

    return read


def main() -> None:
    """Entry point: poll forever, logging (not crashing on) unreachable-inverter errors."""
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    outputs = Outputs.from_env()
    host = env_str("SOLIS_HOST")
    interval = env_int("SOLIS_INTERVAL", 60)
    monitor = SolisMonitor(
        read_registers=modbus_reader(host, env_int("SOLIS_PORT", 502), env_int("SOLIS_UNIT", 1)),
        notifier=Notifier(outputs.ntfy_url),
        metrics=VictoriaMetrics(outputs.vm_url),
        status_dir=outputs.status_dir,
        kuma_push_url=env_str("KUMA_PUSH_URL", ""),
    )
    log.info("polling Solis inverter at %s every %ss", host, interval)
    while True:
        try:
            monitor.poll_once()
        except (OSError, KeyError) as exc:  # unreachable logger or a truncated response
            log.warning("poll failed: %s", exc)
        time.sleep(interval)


if __name__ == "__main__":
    main()
