from __future__ import annotations

import pytest

from homelab_tools.ad5x.monitor import load_tracker, save_tracker
from homelab_tools.ad5x.protocol import PrinterStatus, parse_status, read_status
from homelab_tools.ad5x.tracker import JobTracker, nice_name

# Replies in the shape the AD5X (firmware 3.1.x) sends them.
M119 = (
    "CMD M119 Received.\r\nEndstop: X-max:0 Y-max:0 Z-min:0\r\nMachineStatus: {status}\r\n"
    "MoveMode: MOVING\r\nStatus: S:1 L:0 J:0 F:0\r\nLED: 1\r\nCurrentFile: {file}\r\nok\r\n"
)
M105 = "CMD M105 Received.\r\nT0:230.2/230.0 T1:0/0 B:50.0/50.0\r\nok\r\n"


def m27(done: int, total: int = 100) -> str:
    return f"CMD M27 Received.\r\nSD printing byte {done}/{total}\r\nok\r\n"


def status(
    state: str = "BUILDING_FROM_SD", pct: int = 50, file: str = "crystal_dragon_silk.3mf"
) -> PrinterStatus:
    return parse_status(M119.format(status=state, file=file), m27(pct), M105)


# --- protocol ----------------------------------------------------------------------------------


def test_parse_status_fields() -> None:
    s = status(pct=11)
    assert s.machine_status == "BUILDING_FROM_SD" and s.state == "printing"
    assert s.current_file == "crystal_dragon_silk.3mf" and s.led_on is True
    assert s.progress_pct == 11.0
    assert (s.nozzle_c, s.nozzle_target_c, s.bed_c, s.bed_target_c) == (230.2, 230.0, 50.0, 50.0)


@pytest.mark.parametrize(
    ("firmware", "state"),
    [("READY", "idle"), ("PAUSED", "paused"), ("BUILDING_COMPLETED", "done"), ("BUILDING", "printing")],
)
def test_state_mapping(firmware: str, state: str) -> None:
    assert status(firmware).state == state


def test_percentage_progress_and_missing_fields() -> None:
    s = parse_status("MachineStatus: READY\r\nok", "CMD M27\r\n42%\r\nok", "")
    assert s.progress_pct == 42 and s.nozzle_c is None and s.led_on is None and s.current_file == ""


class FakePrinter:
    """Answers each command from a script, like the real port."""

    def __init__(self, answers: dict[str, str]) -> None:
        self.answers, self.pending, self.sent = answers, b"", []

    def sendall(self, data: bytes) -> None:
        cmd = data.decode().strip()
        self.sent.append(cmd)
        self.pending += self.answers.get(cmd.split()[0], "ok\r\n").encode()

    def recv(self, size: int) -> bytes:
        chunk, self.pending = self.pending[:size], self.pending[size:]
        return chunk

    def settimeout(self, value: float | None) -> None:
        pass

    def close(self) -> None:
        pass


def test_read_status_takes_and_releases_control() -> None:
    printer = FakePrinter({"~M119": M119.format(status="READY", file=""), "~M27": m27(0), "~M105": M105})
    result = read_status("p", connect=lambda addr, timeout: printer, timeout=0.5)
    assert result is not None and result.state == "idle"
    assert printer.sent == ["~M601 S1", "~M119", "~M27", "~M105", "~M602"]


def test_read_status_off_returns_none() -> None:
    def refuse(addr: tuple[str, int], timeout: float) -> FakePrinter:
        raise ConnectionRefusedError

    assert read_status("p", connect=refuse) is None


# --- tracker -----------------------------------------------------------------------------------


def titles(events) -> list[str]:
    return [e.title for e in events]


def test_full_print_lifecycle_with_estimates() -> None:
    t = JobTracker()
    assert titles(t.step(None, 0)) == []  # off and idle: silent
    assert titles(t.step(status(pct=0), 100)) == ["Print started"]
    t.step(status(pct=25), 100 + 3600)
    assert t.remaining_seconds(100 + 3600) == pytest.approx(3 * 3600)
    d = t.display(status(pct=25), 100 + 3600)
    assert d["state"] == "Printing" and d["elapsed"] == "1h 00m" and d["remaining"] == "≈ 3h 00m"
    events = t.step(status("BUILDING_COMPLETED", 100), 100 + 4 * 3600)
    assert titles(events) == ["Print finished"] and "after 4h 00m" in events[0].message
    assert t.job is None and t.history[-1].result == "done"
    assert t.display(None, 100 + 4 * 3600)["prints_7d"] == "1"


def test_joining_mid_print_estimates_from_observed_rate() -> None:
    t = JobTracker()
    assert titles(t.step(status(pct=40), 0)) == []  # no "started" alert for a job already running
    assert t.remaining_seconds(0) is None
    t.step(status(pct=50), 600)  # 10% in 10 minutes
    assert t.remaining_seconds(600) == pytest.approx(3000)
    assert t.display(status(pct=50), 600)["elapsed"].startswith("since ")


def test_offline_mid_print_is_alerted_but_offline_when_idle_is_not() -> None:
    t = JobTracker()
    t.step(status(pct=0), 0)
    t.step(status(pct=60), 600)
    assert titles(t.step(None, 630)) == [] and titles(t.step(None, 660)) == []  # not yet: could be busy
    events = t.step(None, 690)  # third failed poll in a row
    assert titles(events) == ["Printer went offline mid-print"] and events[0].priority == "high"
    assert t.history[-1].result == "lost"
    assert titles(t.step(None, 720)) == []


def test_single_refused_polls_do_not_lose_the_job() -> None:
    """Regression: the AD5X refuses ~1 poll in 15 mid-print; that must not alert or reset the job."""
    t = JobTracker()
    t.step(status(pct=0), 0)
    for i, pct in enumerate([10, 20, 30, 40], start=1):
        assert titles(t.step(None, i * 120 - 30)) == []  # one refused poll...
        assert titles(t.step(status(pct=pct), i * 120)) == []  # ...then back
    assert t.job is not None and t.job.started == 0 and t.misses == 0
    assert t.history == []


def test_pause_and_cancel() -> None:
    t = JobTracker()
    t.step(status(pct=0), 0)
    t.step(status(pct=30), 60)
    assert titles(t.step(status("PAUSED", 30), 120)) == ["Print paused"]
    assert titles(t.step(status("PAUSED", 30), 180)) == []  # only once
    assert titles(t.step(status("READY", 30), 240)) == ["Print stopped"]
    assert t.history[-1].result == "stopped"


def test_history_expires_and_metrics() -> None:
    t = JobTracker()
    t.step(status(pct=0), 0)
    t.step(status("READY", 100), 60)
    assert len(t.history) == 1
    t.step(None, 31 * 86400)
    assert t.history == []
    assert t.metrics(None) == {"online": 0, "printing": 0}
    assert t.metrics(status("READY", 0))["nozzle_c"] == 230.2


def test_tracker_persistence_round_trip(tmp_path) -> None:
    t = JobTracker()
    t.step(status(pct=0), 0)
    path = tmp_path / "state" / "s.json"
    save_tracker(path, t)
    assert load_tracker(path) == t
    assert (path.stat().st_mode & 0o777) == 0o600
    assert load_tracker(tmp_path / "missing.json") == JobTracker()


def test_nice_name() -> None:
    assert nice_name("/data/crystal_dragon.3mf") == "crystal dragon"
    assert nice_name("") == "-"
