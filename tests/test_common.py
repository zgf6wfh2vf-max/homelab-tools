from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta, timezone

import pytest

from homelab_tools.common.config import ConfigError, Outputs, env_float, env_int, env_str
from homelab_tools.common.metrics import Sample, VictoriaMetrics
from homelab_tools.common.notify import Notifier
from homelab_tools.common.status import write_status
from homelab_tools.common.timefmt import duration, minutes_ago, when

# --- config ------------------------------------------------------------------------------------


def test_env_str_required_and_default() -> None:
    assert env_str("A", env={"A": "x"}) == "x"
    assert env_str("A", "d", env={}) == "d"
    assert env_str("A", "d", env={"A": ""}) == "d"  # empty counts as unset
    with pytest.raises(ConfigError, match="A is not set"):
        env_str("A", env={})


def test_env_numbers_validate() -> None:
    assert env_int("N", env={"N": "42"}) == 42
    assert env_int("N", 7, env={}) == 7
    assert env_float("F", env={"F": "1.5"}) == 1.5
    with pytest.raises(ConfigError, match="whole number"):
        env_int("N", env={"N": "4.2"})
    with pytest.raises(ConfigError, match="must be a number"):
        env_float("F", env={"F": "abc"})


def test_outputs_from_env_strips_trailing_slash() -> None:
    out = Outputs.from_env({"NTFY_URL": "https://ntfy.sh/t", "VM_URL": "http://vm:8428/"})
    assert out == Outputs("https://ntfy.sh/t", "http://vm:8428", "")


# --- notify ------------------------------------------------------------------------------------


def test_notifier_sends_headers_and_body(http) -> None:
    assert Notifier("https://ntfy.example/topic", http).send("Hi", "body", priority="high", tags="warning")
    (req,) = http.requests
    assert req.full_url == "https://ntfy.example/topic"
    assert req.get_header("Title") == "Hi" and req.get_header("Priority") == "high"
    assert req.get_header("Tags") == "warning" and req.data == b"body"


def test_notifier_disabled_and_failure_are_harmless(http) -> None:
    assert not Notifier("", http).send("t", "m")
    assert http.requests == []
    http.fail = True
    assert not Notifier("https://x/t", http).send("t", "m")


# --- metrics -----------------------------------------------------------------------------------


def test_sample_line_protocol_escapes_and_skips_missing_values() -> None:
    t = datetime(2026, 1, 1, tzinfo=UTC)
    fields = {"inside_c": 21.25, "humidity": None, "open": True}
    line = Sample("tado", fields, {"zone": "Living Room"}, t).to_line()
    assert line == f"tado,zone=Living\\ Room inside_c=21.25,open=1 {int(t.timestamp() * 1e9)}"
    assert Sample("x", {"a": None, "b": float("nan")}).to_line() is None


def test_vm_write_batches_and_reports_count(http) -> None:
    vm = VictoriaMetrics("http://vm:8428/", http, batch_size=2)
    assert vm.write(Sample("m", {"v": i}) for i in range(5)) == 5
    assert len(http.requests) == 3
    assert http.requests[0].full_url == "http://vm:8428/write"
    assert http.bodies()[0] == "m v=0\nm v=1"


def test_vm_disabled_or_failing(http) -> None:
    assert VictoriaMetrics("", http).write([Sample("m", {"v": 1})]) == 0
    http.fail = True
    vm = VictoriaMetrics("http://vm", http)
    assert vm.write([Sample("m", {"v": 1})]) == 0
    vm.reset_cache()  # must not raise


# --- status ------------------------------------------------------------------------------------


def test_write_status_atomic_and_optional(tmp_path) -> None:
    path = write_status(str(tmp_path), "solis", {"pv_w": "1 kW", "temp": "21 °C"})
    assert path is not None and json.loads(path.read_text())["temp"] == "21 °C"
    assert not list(tmp_path.glob("*.tmp"))
    assert write_status("", "x", {}) is None


# --- time formatting ---------------------------------------------------------------------------


def test_duration_and_minutes_ago() -> None:
    assert duration(3 * 3600 + 5 * 60 + 59) == "3h 05m"
    assert duration(None) == duration(-5) == "0h 00m"
    assert minutes_ago(7 * 60) == "7 min ago"
    assert minutes_ago(3 * 3600) == "3 h ago"


def test_when_relative_to_now() -> None:
    tz = timezone(timedelta(0))
    now = datetime(2026, 10, 25, 15, 0, tzinfo=tz)
    assert when(now - timedelta(hours=2), now).endswith(":00")
    assert when(now - timedelta(days=2), now)[:3] in {"Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"}
    assert when(now - timedelta(days=30), now).endswith("Sep")
