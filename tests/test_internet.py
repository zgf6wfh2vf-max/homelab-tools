from __future__ import annotations

import os
import threading
import time as systime
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from homelab_tools.internet.monitor import (
    LineState,
    Outage,
    SpeedState,
    SpeedTest,
    check_line,
    display,
    duration_text,
    monthly_report,
    parse_target,
    record_test,
    run_streams,
    speed_test,
    step,
)


@pytest.fixture(autouse=True)
def london():
    old = os.environ.get("TZ")
    os.environ["TZ"] = "Europe/London"
    systime.tzset()
    yield
    if old is None:
        del os.environ["TZ"]
    else:
        os.environ["TZ"] = old
    systime.tzset()


T0 = datetime(2026, 10, 12, 14, 0, tzinfo=ZoneInfo("Europe/London")).timestamp()  # not the runner's zone


@pytest.mark.parametrize(
    ("seconds", "text"), [(45, "45 s"), (420, "7 min"), (7500, "2 h 5 min"), (7200, "2 h")]
)
def test_duration_text(seconds: float, text: str) -> None:
    assert duration_text(seconds) == text


def test_targets_and_line_check() -> None:
    assert parse_target("1.1.1.1:443") == ("1.1.1.1", 443) and parse_target("example.net") == (
        "example.net",
        443,
    )
    times = {"a": None, "b": 31.0, "c": 12.5}
    assert check_line(["a", "b", "c"], times.get) == 12.5
    assert check_line(["a"], times.get) is None


def run_checks(state: LineState, results: list[tuple[float | None, bool | None]], every: float = 30):
    alerts = []
    for i, (latency, router) in enumerate(results):
        alerts += step(state, T0 + i * every, latency, router, gap_s=120)
    return alerts


def test_a_single_failed_check_is_not_an_outage() -> None:
    state = LineState()
    assert run_checks(state, [(10.0, True), (None, True), (11.0, True)]) == []
    assert state.outages == []


def test_broadband_outage_is_reported_when_the_line_returns(tmp_path: Path) -> None:
    state = LineState()
    results = [(10.0, True)] + [(None, True)] * 14 + [(12.0, True)]
    alerts = run_checks(state, results)
    assert alerts == [
        (
            "Internet back",
            "The internet was down for 7 min (14:00-14:07). The router stayed reachable, so the fault was on "
            "the broadband line or at the provider.",
            "default",
        )
    ]
    assert state.outages == [Outage(T0 + 30, T0 + 15 * 30, True)]
    state.save(tmp_path / "line.json")
    assert LineState.load(tmp_path / "line.json") == state


def test_router_outage_and_status_while_down() -> None:
    state = LineState()
    run_checks(state, [(10.0, True), (None, True), (None, False), (None, True)])
    assert display(state, SpeedState(), T0 + 90)["status"] == "down since 14:00"
    alerts = step(state, T0 + 120, 9.0, True, 120)
    assert "router stopped answering too" in alerts[0][1]
    assert state.outages[-1].cause == "home network"


def test_a_gap_in_monitoring_forgets_a_failure_run() -> None:
    state = LineState()
    run_checks(state, [(None, None), (None, None)])
    assert step(state, T0 + 3600, 10.0, None, 120) == []  # the monitor was stopped for an hour


def test_run_streams_measures_parallel_throughput() -> None:
    calls, lock = [], threading.Lock()

    def transfer(nbytes: int) -> None:
        systime.sleep(0.01)
        with lock:
            calls.append(nbytes)

    mbps = run_streams(transfer, 1_000_000, 2, 0.2)
    assert calls[:2] == [250_000, 250_000]  # warm-ups
    assert 600 < mbps < 1700  # 2 streams x 1 MB per ~10 ms = ~1600 Mbps, minus scheduling


def test_speed_test_records_ping() -> None:
    test = speed_test(T0, lambda n: None, lambda n: None, lambda t: 9.04, seconds=0.01)
    assert test.ping == 9.0 and test.time == T0


def test_slow_line_alerts_after_three_tests_and_clears(tmp_path: Path) -> None:
    state = SpeedState()
    assert record_test(state, SpeedTest(T0, 60, 20, 9), 100) == []
    assert record_test(state, SpeedTest(T0 + 1, 70, 20, 9), 100) == []
    alerts = record_test(state, SpeedTest(T0 + 2, 80, 20, 9), 100)
    assert alerts[0][0] == "Broadband slow" and "(60, 70, 80 Mbps)" in alerts[0][1]
    assert record_test(state, SpeedTest(T0 + 3, 50, 20, 9), 100) == []
    assert record_test(state, SpeedTest(T0 + 4, 150, 20, 9), 100)[0][0] == "Broadband OK again"
    state.save(tmp_path / "s.json")
    assert SpeedState.load(tmp_path / "s.json") == state
    assert record_test(SpeedState(), SpeedTest(T0, 1, 1, 1), 0) == []  # no minimum set


def test_display() -> None:
    line = LineState(last_check=T0, latency_ms=11.6, outages=[Outage(T0 - 3600, T0 - 3000, True)])
    speeds = SpeedState([SpeedTest(T0 - 7200, 140, 30, 8), SpeedTest(T0 - 60, 150.4, 31.2, 8)])
    assert display(line, speeds, T0) == {
        "status": "online",
        "latency": "12 ms",
        "outages_30d": "1, 10 min in total",
        "last_outage": "12 Oct 13:00, 10 min",
        "download": "150 Mbps",
        "upload": "31 Mbps",
        "download_30d": "145 Mbps",
        "upload_30d": "31 Mbps",
        "tested": "12 Oct 13:59",
    }
    assert display(LineState(), SpeedState(), T0)["status"] == "-"


def test_monthly_report() -> None:
    line = LineState(
        outages=[
            Outage(T0, T0 + 840, True),
            Outage(T0 + DAY_S, T0 + DAY_S + 300, False),
            Outage(datetime(2026, 11, 1, 0, 5).timestamp(), datetime(2026, 11, 1, 0, 9).timestamp(), True),
        ]
    )
    speeds = SpeedState([SpeedTest(T0 + i * 3600, 100.0 + i, 30.0, 9.0) for i in range(12)])
    title, message, detail = monthly_report(line, speeds, date(2026, 10, 1), 105)
    assert title == "Broadband in October"
    assert message == (
        "2 outages, 19 min in total (longest 14 min on 12 Oct 14:00, broadband). Faults on the broadband "
        "line itself: 1 of 2. Download median 106 Mbps (slowest 100), upload median 30 Mbps, from 12 "
        "tests. 5 of 12 tests were under your 105 Mbps minimum."
    )
    assert "  12 Oct 14:00  12 Oct 14:14  14 min  broadband" in detail
    assert detail.count("Mbps") >= 1 and "1 Nov" not in detail
    assert monthly_report(LineState(), SpeedState(), date(2026, 12, 1), 0)[1] == (
        "No outages. No speed tests were recorded."
    )


DAY_S = 86400
