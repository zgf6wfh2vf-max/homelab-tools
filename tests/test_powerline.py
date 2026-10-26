from __future__ import annotations

import gzip
from pathlib import Path

import pytest

from homelab_tools.powerline.monitor import (
    State,
    display,
    metrics,
    parse,
    parse_uptime,
    poll,
    run,
)

MASTER, OFFICE = "192.0.2.10", "192.0.2.20"


def data_file(name: str, me: int, rx: tuple[int, int], uptime: str = "5 days, 21h 10m 43s", **extra) -> bytes:
    """A data.cfl shaped like a Magic 2 LAN's, including fields that must never be kept."""
    fields = {
        "NODE.GENERAL.DEVICE_NAME": name,
        "NODE.GENERAL.NODE_TYPE": "DOMAIN_MASTER" if me == 1 else "END_POINT",
        "SYSTEM.PRODUCTION.DEVICE_NAME": "Magic 2 LAN 1-1",
        "SYSTEM.GENERAL.FW_VERSION": "magic-2-lan 7.12.9.142_2023-05-02",
        "SYSTEM.GENERAL.UPTIME": uptime,
        "SYSTEM.STATS.CPU_USAGE": "11",
        "MASTERSELECTION.DOMAIN.LOST_MAPS": "992",
        "DIDMNG.GENERAL.DIDS": "0,1,2",
        "DIDMNG.GENERAL.MACS": "00:00:00:00:00:00,02:00:00:00:00:01,02:00:00:00:00:02",
        "DIDMNG.GENERAL.ACTUAL_RX_BPS": f"0,{rx[0]},{rx[1]}",
        "DEVOLO.REMOTE.DEVICE_NAMES": ",Lounge,Office,,,",
        "DEVOLO.DEVICEAPI.TOKEN": "secret-token",
    } | extra
    return "\n".join(f"{k}={v}" for k, v in fields.items()).encode()


class Network:
    """Two adapters; either can be unplugged, and link rates can be changed."""

    def __init__(self) -> None:
        self.down: set[str] = set()
        self.to_office, self.to_lounge = 24657, 21467  # raw units
        self.uptime = {MASTER: "5 days, 21h 10m 45s", OFFICE: "5 days, 21h 10m 43s"}
        self.firmware = "magic-2-lan 7.12.9.142_2023-05-02"

    def __call__(self, host: str) -> bytes:
        if host in self.down:
            raise OSError("timed out")
        fw = {"SYSTEM.GENERAL.FW_VERSION": self.firmware}
        if host == MASTER:
            return gzip.compress(data_file("Lounge", 1, (0, self.to_lounge), self.uptime[host], **fw))
        return data_file("Office", 2, (self.to_office, 0), self.uptime[host], **fw)


def test_parse_keeps_only_known_fields_and_handles_gzip() -> None:
    values = parse(gzip.compress(data_file("Office", 2, (24657, 0))))
    assert values["NODE.GENERAL.DEVICE_NAME"] == "Office"
    assert not any("TOKEN" in k for k in values)


@pytest.mark.parametrize(
    ("text", "seconds"),
    [("5 days, 21h 10m 43s", 508243), ("1 day, 0h 0m 5s", 86405), ("3m 2s", 182), ("soon", None)],
)
def test_uptime(text: str, seconds: int | None) -> None:
    assert parse_uptime(text) == seconds


def test_poll_reads_rates_by_peer_name() -> None:
    adapters = poll([MASTER, OFFICE], Network(), 0.0533)
    office = adapters[OFFICE]
    assert office is not None and office.receive_mbps == {"Lounge": 1314.2}
    assert office.uptime_s == 508243 and office.role == "END_POINT"
    assert adapters[MASTER] is not None and adapters[MASTER].receive_mbps == {"Office": 1144.2}


def test_learns_then_alerts_on_a_slow_link_after_half_an_hour(tmp_path: Path) -> None:
    net, state, t = Network(), State(), 1_000_000.0
    for i in range(24):  # half a day of half-hourly samples
        assert run([MASTER, OFFICE], net, state, t + i * 1800, 0.0533, 50)[0] == []
    t += 24 * 1800
    assert display(*run([MASTER, OFFICE], net, state, t, 0.0533, 50)[1:2], state, [])["usual"] == (
        "1314 / 1144 Mbps"
    )
    net.to_office = 9000  # ~480 Mbps, under half the usual
    assert run([MASTER, OFFICE], net, state, t + 300, 0.0533, 50)[0] == []
    alerts = run([MASTER, OFFICE], net, state, t + 300 + 1800, 0.0533, 50)[0]
    assert alerts == [
        (
            "Powerline problem",
            "The powerline link Lounge to Office is down to 480 Mbps (usually 1314) - often new electrical "
            "noise near a socket, or an adapter moved onto an extension lead.",
            "high",
        )
    ]
    assert run([MASTER, OFFICE], net, state, t + 2400, 0.0533, 50)[0] == []  # told once
    net.to_office = 24657
    assert run([MASTER, OFFICE], net, state, t + 2700, 0.0533, 50)[0][0][0] == "Powerline: OK again"
    state.save(tmp_path / "s.json")
    assert State.load(tmp_path / "s.json") == state


def test_offline_adapter_and_lost_link() -> None:
    net, state, t = Network(), State(), 1_000_000.0
    run([MASTER, OFFICE], net, state, t, 0.0533, 50)
    net.down.add(OFFICE)
    assert run([MASTER, OFFICE], net, state, t + 300, 0.0533, 50)[0] == []
    alerts = run([MASTER, OFFICE], net, state, t + 900, 0.0533, 50)[0]
    assert [a[1] for a in alerts] == [f"The Office powerline adapter ({OFFICE}) is not answering."]
    net.down.clear()
    net.to_lounge = 0  # Office answers on Ethernet but the master hears nothing over the mains
    alerts = run([MASTER, OFFICE], net, state, t + 1200, 0.0533, 50)[0]
    assert alerts[0] == ("Powerline: OK again", f"Cleared: the Office powerline adapter ({OFFICE}) "
                         "is not answering.", "low")  # fmt: skip
    alerts = run([MASTER, OFFICE], net, state, t + 1900, 0.0533, 50)[0]
    assert [a[1] for a in alerts] == ["Lounge no longer has a powerline link from Office."]


def test_restart_and_firmware_update_are_reported_once() -> None:
    net, state, t = Network(), State(), 1_000_000.0
    run([MASTER, OFFICE], net, state, t, 0.0533, 50)
    net.uptime[OFFICE] = "0h 4m 10s"
    net.firmware = "magic-2-lan 7.14.1.20_2026-09-01"
    alerts = run([MASTER, OFFICE], net, state, t + 300, 0.0533, 50)[0]
    assert ("Powerline: Office restarted", "The Office adapter restarted 4 min ago - a power cut, a firmware "
            "update or it was unplugged.", "low") in alerts  # fmt: skip
    assert ("Powerline: Lounge updated", "The Lounge adapter's firmware changed from magic-2-lan "
            "7.12.9.142_2023-05-02 to magic-2-lan 7.14.1.20_2026-09-01.", "low") in alerts  # fmt: skip
    assert len(alerts) == 3
    assert run([MASTER, OFFICE], net, state, t + 600, 0.0533, 50)[0] == []


def test_metrics_and_display() -> None:
    net, state = Network(), State()
    net.down.add(OFFICE)
    run([MASTER, OFFICE], Network(), state, 0, 0.0533, 50)
    _, adapters, findings = run([MASTER, OFFICE], net, state, 300, 0.0533, 50)
    lines = sorted(line for s in metrics(adapters, state) if (line := s.to_line()))
    assert lines == [
        "powerline,adapter=Lounge online=1,uptime_s=508245,cpu_pct=11.0,lost_maps=992.0",
        "powerline,adapter=Office online=0",
        "powerline,from=Office,to=Lounge rate_mbps=1144.2",
    ]
    assert display(adapters, state, findings) == {
        "links": "Lounge → Office, Office → Lounge",
        "speed": "- / 1144 Mbps",
        "usual": "learning",
        "adapters": "1 of 2 online",
        "uptime": "6 days",
        "firmware": "7.12.9.142",
        "lost_maps": "Lounge 992",
        "problems": "1",
    }
