"""``ad5x-monitor``: watch a FlashForge AD5X and publish job progress, temperatures and events.

Polls every 60 s (30 s while printing). See :mod:`homelab_tools.ad5x.tracker` for the job logic.

Settings (environment):

``AD5X_HOST`` (required)  Printer IP address (reserve it in your DHCP server).
``AD5X_PORT``             Legacy TCP port, default 8899.
``AD5X_STATE_FILE``       Where the current job and history are kept between restarts
                          (default ``/var/lib/ad5x-monitor/state.json``).

Outputs: ntfy events, ``printer_*`` metrics, ``$STATUS_DIR/printer.json``.
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path

from homelab_tools.ad5x.protocol import read_status
from homelab_tools.ad5x.tracker import JobTracker
from homelab_tools.common.config import Outputs, env_int, env_str
from homelab_tools.common.metrics import Sample, VictoriaMetrics
from homelab_tools.common.notify import Notifier
from homelab_tools.common.status import write_status

log = logging.getLogger("ad5x-monitor")


def load_tracker(path: Path) -> JobTracker:
    """Load the tracker from ``path``; start fresh if it is missing or unreadable."""
    try:
        return JobTracker.from_dict(json.loads(path.read_text()))
    except (OSError, ValueError, TypeError):
        return JobTracker()


def save_tracker(path: Path, tracker: JobTracker) -> None:
    """Write the tracker atomically (private: it can contain file names)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(tracker.to_dict()))
    tmp.chmod(0o600)
    tmp.replace(path)


def main() -> None:
    """Entry point: poll forever."""
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    outputs = Outputs.from_env()
    host, port = env_str("AD5X_HOST"), env_int("AD5X_PORT", 8899)
    state_file = Path(env_str("AD5X_STATE_FILE", "/var/lib/ad5x-monitor/state.json"))
    notifier, vm = Notifier(outputs.ntfy_url), VictoriaMetrics(outputs.vm_url)
    tracker = load_tracker(state_file)
    log.info("watching AD5X at %s:%s", host, port)
    while True:
        now = time.time()
        status = read_status(host, port)
        for event in tracker.step(status, now):
            notifier.send(event.title, event.message, priority=event.priority, tags=event.tags)
        write_status(outputs.status_dir, "printer", tracker.display(status, now))
        vm.write([Sample("printer", tracker.metrics(status))])
        save_tracker(state_file, tracker)
        time.sleep(30 if tracker.state in ("printing", "paused") else 60)


if __name__ == "__main__":
    main()
