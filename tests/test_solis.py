from __future__ import annotations

import struct

import pytest

from homelab_tools.common.metrics import VictoriaMetrics
from homelab_tools.common.notify import Notifier
from homelab_tools.solis import modbus
from homelab_tools.solis.monitor import FaultTracker, SolisMonitor
from homelab_tools.solis.registers import REGISTER_BLOCKS, decode, display_fields, metric_fields, summary

# Register values read from a real Solis hybrid on a sunny October afternoon (other registers 0).
LIVE = {
    33029: 0, 33030: 17120, 33031: 0, 33032: 342, 33035: 118,
    33049: 2225, 33050: 12, 33051: 2227, 33052: 13, 33057: 0, 33058: 563,
    33093: 338, 33128: 2485, 33130: 0, 33131: 414, 33133: 534, 33135: 1,
    33139: 89, 33140: 99, 33147: 491, 33149: 0, 33150: 469,
    33163: 96, 33167: 16, 33171: 116, 33175: 86, 33179: 69,
}  # fmt: skip


def registers(**overrides: int) -> dict[int, int]:
    regs = {a: 0 for start, n in REGISTER_BLOCKS for a in range(start, start + n)}
    regs.update(LIVE)
    regs.update({int(k[1:]): v for k, v in overrides.items()})
    return regs


# --- decoding ----------------------------------------------------------------------------------


def test_decode_live_snapshot() -> None:
    r = decode(registers())
    assert (r.pv_w, r.pv_today_kwh, r.pv_month_kwh, r.pv_total_kwh) == (563, 11.8, 342, 17120)
    assert r.battery_w == -469  # register 33135 = 1 means discharging
    assert r.grid_w == 414  # positive = exporting
    assert (r.soc_pct, r.load_w, r.grid_v, r.inverter_temp_c) == (89, 491, 248.5, 33.8)
    assert (r.dc1_v, r.dc2_a) == (222.5, 1.3)
    assert r.fault_codes == () and not r.has_fault


def test_negative_meter_power_means_import() -> None:
    minus_1200 = (1 << 32) - 1200
    r = decode(registers(r33130=minus_1200 >> 16, r33131=minus_1200 & 0xFFFF))
    assert r.grid_w == -1200
    assert display_fields(r)["grid_dir"] == "importing"
    assert display_fields(r)["grid_power"] == "1.20 kW"


def test_charging_battery_is_positive_and_faults_are_collected() -> None:
    r = decode(registers(r33135=0, r33117=0x1015))
    assert r.battery_w == 469
    assert r.fault_codes == (0x1015,) and r.has_fault
    assert display_fields(r)["faults"] == "0x1015"
    assert summary(r).endswith("FAULT 0x1015")


def test_display_and_metric_fields() -> None:
    r = decode(registers())
    d = display_fields(r)
    assert d["batt_now"] == "out 469 W" and d["pv_total"] == "17.1 MWh" and d["grid_dir"] == "exporting"
    m = metric_fields(r)
    assert m["batt_w"] == -469 and m["grid_w"] == 414 and m["pv_today_kwh"] == 11.8


def test_small_flows_count_as_idle() -> None:
    d = display_fields(decode(registers(r33150=5, r33131=12)))
    assert d["batt_now"] == "idle" and d["grid_dir"] == "idle"


# --- Modbus framing ----------------------------------------------------------------------------


class FakeSocket:
    """Returns a canned response, delivered in small pieces to exercise reassembly."""

    def __init__(self, response: bytes) -> None:
        self.response, self.sent, self.closed = response, b"", False

    def sendall(self, data: bytes) -> None:
        self.sent += data

    def recv(self, size: int) -> bytes:
        chunk, self.response = self.response[: min(size, 3)], self.response[min(size, 3) :]
        return chunk

    def close(self) -> None:
        self.closed = True


def test_build_request_and_limits() -> None:
    assert modbus.build_request(1, 33022, 74) == struct.pack(">HHHBBHH", 1, 0, 6, 1, 4, 33022, 74)
    with pytest.raises(ValueError):
        modbus.build_request(1, 0, 126)


def test_read_input_registers_round_trip() -> None:
    values = [10, 20, 65535]
    response = struct.pack(">HHHBBB", 1, 0, 3 + 6, 1, 4, 6) + struct.pack(">3H", *values)
    sock = FakeSocket(response)
    regs = modbus.read_input_registers("h", 100, 3, connect=lambda addr, timeout: sock)
    assert regs == {100: 10, 101: 20, 102: 65535}
    assert sock.sent == modbus.build_request(1, 100, 3) and sock.closed


def test_exception_and_truncated_responses() -> None:
    exc = FakeSocket(struct.pack(">HHHBBB", 1, 0, 3, 1, 0x84, 2))
    with pytest.raises(modbus.ModbusError, match="exception 2"):
        modbus.read_input_registers("h", 1, 1, connect=lambda a, t: exc)
    short = FakeSocket(struct.pack(">HHHBBB", 1, 0, 5, 1, 4, 4) + b"\x00")
    with pytest.raises(modbus.ModbusError, match="closed"):
        modbus.read_input_registers("h", 1, 2, connect=lambda a, t: short)


# --- monitor -----------------------------------------------------------------------------------


def test_fault_tracker_reports_each_change_once() -> None:
    t = FaultTracker()
    assert t.update(()) is None
    assert t.update((1,)) == "raised"
    assert t.update((1,)) is None
    assert t.update(()) == "cleared"


def test_poll_once_publishes_everywhere(tmp_path, http) -> None:
    pushed: list[str] = []
    regs = registers()
    mon = SolisMonitor(
        read_registers=lambda: regs,
        notifier=Notifier("https://ntfy/t", http),
        metrics=VictoriaMetrics("http://vm", http),
        status_dir=str(tmp_path),
        kuma_push_url="http://kuma/api/push/abc",
        http=pushed.append,
    )
    mon.poll_once()
    assert (tmp_path / "solis.json").exists()
    assert any(b.startswith("solis ") and "pv_w=563" in b for b in http.bodies())
    assert pushed[0].startswith("http://kuma/api/push/abc?status=up")
    regs[33116] = 7
    mon.poll_once()
    titles = [r.get_header("Title") for r in http.requests if r.get_header("Title")]
    assert titles == ["Inverter fault"]
    assert "status=down" in pushed[1]
