from __future__ import annotations

from pathlib import Path

import pytest

from homelab_tools.host.health import (
    DAY,
    MINUTE,
    Finding,
    Settings,
    State,
    System,
    check_all,
    check_reboot,
    check_throttling,
    evaluate,
    kernel_version_key,
    writes_last_day,
)

MEMINFO = "MemTotal:        8000000 kB\nMemFree:  100 kB\nMemAvailable:    {avail} kB\n"
DISKSTATS = " 179       0 mmcblk0 1 2 3 4 5 6 {written} 0 0 0 0\n   8       0 sda 1 2 3 4 5 6 7 999 0 0 0\n"


def fake_system(
    temp: int = 55000,
    throttled: str = "throttled=0x0",
    failed: str = "",
    avail: int = 6000000,
    written: int = 1000,
    used_pct: float = 40,
    kernels: tuple[str, ...] = ("6.18.50+rpt-rpi-2712", "6.18.50+rpt-rpi-v8"),
    running: str = "6.18.50+rpt-rpi-2712",
    boot_id: str = "boot-1",
    reboot_file: bool = False,
) -> System:
    files = {
        "/sys/class/thermal/thermal_zone0/temp": f"{temp}\n",
        "/proc/meminfo": MEMINFO.format(avail=avail),
        "/proc/diskstats": DISKSTATS.format(written=written),
        "/proc/sys/kernel/random/boot_id": boot_id + "\n",
    }
    if reboot_file:
        files["/run/reboot-required"] = "*** System restart required ***\n"
    commands = {"vcgencmd": throttled, "systemctl": failed}
    return System(
        read=files.get,
        command=lambda cmd: commands.get(cmd[0]),
        disk_usage=lambda path: (int(used_pct), 100),
        kernels=lambda: list(kernels),
        running_kernel=lambda: running,
    )


def run_checks(system: System, state: State, now: float, **settings) -> tuple[list, dict]:
    findings, status = check_all(system, Settings(**settings), state, now)
    return evaluate(findings, state, now, "ajax"), status


def test_all_well() -> None:
    alerts, status = run_checks(fake_system(), State(), 1000.0)
    assert alerts == []
    assert status == {
        "cpu_temp": "55.0 °C",
        "power": "OK",
        "failed_units": "none",
        "memory": "OK",
        "writes_24h": "-",
        "reboot": "not needed",
        "problems": "0",
    }


def test_heat_waits_ten_minutes_alerts_once_and_clears_with_hysteresis() -> None:
    state = State()
    hot = fake_system(temp=82000)
    assert run_checks(hot, state, 0)[0] == []
    assert run_checks(hot, state, 5 * MINUTE)[0] == []
    alerts = run_checks(hot, state, 10 * MINUTE)[0]
    assert alerts == [("ajax: running hot", "CPU at 82 °C.", "high")]
    assert run_checks(hot, state, 15 * MINUTE)[0] == []  # told once
    assert run_checks(fake_system(temp=77000), state, 20 * MINUTE)[0] == []  # below 80 but not below 75
    assert run_checks(fake_system(temp=74000), state, 25 * MINUTE)[0] == [
        ("ajax: OK again", "Cleared: CPU at 82 °C.", "low")
    ]


def test_a_short_spike_never_alerts() -> None:
    state = State()
    run_checks(fake_system(temp=85000), state, 0)
    assert run_checks(fake_system(temp=60000), state, 5 * MINUTE)[0] == []
    assert state.since == {} and state.alerted == {}


def test_throttling_now_and_since_last_check() -> None:
    findings = {f.key: f.problem for f in check_throttling(0x50005, seen_since_boot=0)}
    assert findings["undervoltage"] and findings["throttled"]
    assert findings["power-blip"] is None  # happening now: the current-state alerts cover it
    blip = {f.key: f.problem for f in check_throttling(0x10000, seen_since_boot=0)}
    assert blip["power-blip"] == "an under-voltage happened since the last check"
    assert {f.key: f.problem for f in check_throttling(0x10000, 0x10000)}[
        "power-blip"
    ] is None  # already told
    assert check_throttling(None, 0) == []  # not a Raspberry Pi


def test_power_blip_is_reported_once_per_boot() -> None:
    state = State()
    blip = fake_system(throttled="throttled=0x10000")
    assert run_checks(blip, state, 0)[0] == [
        ("ajax: power blip", "An under-voltage happened since the last check.", "low")
    ]
    assert run_checks(blip, state, 5 * MINUTE)[0] == []  # no "OK again" for an event
    assert run_checks(fake_system(throttled="throttled=0x10000", boot_id="boot-2"), state, DAY)[0] != []


def test_failed_units_each_get_their_own_alert_and_recovery() -> None:
    state = State()
    failed = fake_system(
        failed="nginx.service loaded failed failed nginx\nfoo.timer loaded failed failed foo\n"
    )
    run_checks(failed, state, 0)
    alerts, status = run_checks(failed, state, 10 * MINUTE)
    assert [a[0] for a in alerts] == ["ajax: nginx.service failed", "ajax: foo.timer failed"]
    assert status["failed_units"] == "nginx.service, foo.timer"
    alerts = run_checks(fake_system(failed="foo.timer loaded failed failed foo\n"), state, 15 * MINUTE)[0]
    assert alerts == [("ajax: OK again", "Cleared: nginx.service has failed.", "low")]


def test_memory_disk_and_writes() -> None:
    state = State()
    low = fake_system(avail=400000, used_pct=95)
    alerts = run_checks(low, state, 0, disk_paths=("/", "/mnt/storage"))[0]
    assert [a[1] for a in alerts] == ["/ is 95% full.", "/mnt/storage is 95% full."]  # disks: at once
    assert [a[1] for a in run_checks(low, state, 15 * MINUTE, disk_paths=("/",))[0]] == [
        "Only 5% of memory available.",
        "Cleared: /mnt/storage is 95% full.",  # no longer checked
    ]
    # 30 GB written in a day (sectors of 512 bytes)
    busy = State()
    run_checks(fake_system(written=0), busy, 0)
    alerts = run_checks(fake_system(written=int(30e9 / 512)), busy, DAY)[0]
    assert alerts == [("ajax: heavy disk writes", "30 GB written to mmcblk0 in the last day.", "high")]


def test_writes_last_day_needs_a_day_of_samples() -> None:
    assert writes_last_day([(0, 0)], 3600, 10**9) is None
    assert writes_last_day([(0, 0), (12 * 3600, 5)], DAY, int(10e9 / 512)) == pytest.approx(10)


def test_reboot_detection() -> None:
    assert kernel_version_key("6.12.47+rpt-rpi-2712") == [6, 12, 47]
    assert kernel_version_key("6.8.0-45-generic") == [6, 8, 0, 45]
    newer = fake_system(kernels=("6.18.50+rpt-rpi-2712", "6.18.62+rpt-rpi-2712", "6.19.1+rpt-rpi-v8"))
    assert (
        check_reboot(newer).problem
        == "kernel 6.18.62+rpt-rpi-2712 is installed but 6.18.50+rpt-rpi-2712 is running"
    )
    assert check_reboot(fake_system()).problem is None  # a newer v8 kernel alone doesn't count
    assert check_reboot(fake_system(reboot_file=True)).problem == "updates are waiting for a reboot"
    state = State()
    assert run_checks(newer, state, 0)[0] == []
    assert run_checks(newer, state, 2 * DAY)[0][0][0] == "ajax: reboot needed"  # after two days, low priority


def test_state_round_trip(tmp_path: Path) -> None:
    state = State()
    run_checks(fake_system(written=5), state, 0)
    evaluate([Finding("memory", "low", 0, "low memory")], state, 0, "ajax")
    state.save(tmp_path / "s.json")
    assert State.load(tmp_path / "s.json") == state
    assert State.load(tmp_path / "missing.json") == State()
