"""``host-health``: alerts when the server itself needs attention.

Run every five minutes from a timer. Each check is either fine or has a problem; a problem must
persist for its *delay* before an alert is sent (a short burst of heat or a service restarting
during an upgrade is not worth a notification), each problem is reported once, and a low-priority
"OK again" follows when it clears.

=================  ============================================================  ==========
Check              Problem when                                                   Delay
=================  ============================================================  ==========
``temperature``    CPU at or above ``TEMP_WARN_C`` (clears 5 °C below)            10 min
``undervoltage``   the power supply is sagging right now (Raspberry Pi)           at once
``throttled``      the CPU is being slowed for heat right now (Raspberry Pi)       10 min
``power-blip``     under-voltage or throttling *happened* since the last run      at once
``unit:<name>``    a systemd unit is in the failed state                          10 min
``memory``         less than ``MEM_WARN_PCT`` % of memory available               15 min
``disk:<path>``    a filesystem in ``DISK_PATHS`` is ``DISK_WARN_PCT`` % full      at once
``sd-writes``      more than ``WRITE_WARN_GB`` GB written to ``WRITE_DEVICE`` in   at once
                   24 hours (wears out SD cards)
``reboot``         a newer kernel is installed (or ``/run/reboot-required``)      2 days
=================  ============================================================  ==========

The Raspberry Pi checks use ``vcgencmd`` and are skipped where it is missing; run the service with
``SupplementaryGroups=video`` so an unprivileged user can call it. Everything else reads ``/proc``,
``/sys`` and ``systemctl``, so no root access is needed.

Settings (environment): ``NTFY_URL``, ``STATUS_DIR``, ``HEALTH_STATE_FILE`` and the thresholds in
``docs/host-health.md``; ``VM_URL`` to record temperature, fan speed and the other readings.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import socket
import subprocess
import time
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from homelab_tools.common.config import Outputs, env_float, env_str
from homelab_tools.common.metrics import Sample, VictoriaMetrics
from homelab_tools.common.notify import Notifier
from homelab_tools.common.status import write_status

log = logging.getLogger("host-health")

MINUTE = 60.0
DAY = 86400.0

#: vcgencmd get_throttled bits: happening now, and happened since boot.
UNDERVOLT_NOW, THROTTLED_NOW, SOFT_LIMIT_NOW = 0x1, 0x4, 0x8
UNDERVOLT_SINCE, THROTTLED_SINCE = 0x10000, 0x40000


@dataclass(frozen=True)
class Finding:
    """The outcome of one check.

    Attributes:
        key: Stable identifier, e.g. ``temperature`` or ``unit:nginx.service``.
        problem: What is wrong (the alert text), or ``None`` when fine.
        delay: Seconds the problem must persist before alerting.
        title: Short label used in notification titles.
    """

    key: str
    problem: str | None
    delay: float = 0.0
    title: str = ""


# --- readers: tiny wrappers around the system, replaced in tests -----------------------------------


def read_text(path: str) -> str | None:
    """File contents, or ``None`` if it cannot be read."""
    try:
        return Path(path).read_text()
    except OSError:
        return None


def run(cmd: Sequence[str]) -> str | None:
    """Stdout of a command, or ``None`` if it is missing or fails."""
    if shutil.which(cmd[0]) is None:
        return None
    proc = subprocess.run(list(cmd), capture_output=True, text=True, timeout=30, check=False)
    return proc.stdout if proc.returncode == 0 else None


def read_fan_rpm() -> int | None:
    """Speed of the first fan the kernel reports (a Pi 5 active cooler shows up here), if any."""
    for path in sorted(Path("/sys/class/hwmon").glob("hwmon*/fan1_input")):
        text = read_text(str(path))
        if text and text.strip().isdigit():
            return int(text)
    return None


@dataclass
class System:
    """Where the checks get their facts; every field can be replaced in tests."""

    read: Callable[[str], str | None] = read_text
    command: Callable[[Sequence[str]], str | None] = run
    disk_usage: Callable[[str], tuple[int, int]] = lambda p: (
        shutil.disk_usage(p).used,
        shutil.disk_usage(p).total,
    )
    kernels: Callable[[], list[str]] = lambda: (
        sorted(p.name for p in Path("/lib/modules").iterdir()) if Path("/lib/modules").is_dir() else []
    )
    running_kernel: Callable[[], str] = lambda: os.uname().release
    fan_rpm: Callable[[], int | None] = read_fan_rpm


# --- checks ------------------------------------------------------------------------------------------


def cpu_temperature(system: System) -> float | None:
    """CPU temperature in °C from the first thermal zone."""
    raw = system.read("/sys/class/thermal/thermal_zone0/temp")
    return int(raw) / 1000 if raw and raw.strip().isdigit() else None


def check_temperature(celsius: float | None, warn: float, alerting: bool) -> Finding:
    """Too hot? Once alerting, it must cool 5 °C below the threshold to clear (no flapping)."""
    limit = warn - 5 if alerting else warn
    problem = f"CPU at {celsius:.0f} °C" if celsius is not None and celsius >= limit else None
    return Finding("temperature", problem, 10 * MINUTE, "running hot")


def throttle_bits(system: System) -> int | None:
    """``vcgencmd get_throttled`` as an integer, or ``None`` when not a Raspberry Pi."""
    out = system.command(["vcgencmd", "get_throttled"])
    match = re.search(r"0x[0-9a-fA-F]+", out or "")
    return int(match.group(), 16) if match else None


def check_throttling(bits: int | None, seen_since_boot: int) -> list[Finding]:
    """Current under-voltage / throttling, plus events that happened since the last run.

    Args:
        bits: Current ``get_throttled`` value.
        seen_since_boot: The "since boot" bits already reported, so each event is reported once.
    """
    if bits is None:
        return []
    findings = [
        Finding(
            "undervoltage",
            "the power supply voltage is too low right now" if bits & UNDERVOLT_NOW else None,
            0,
            "under-voltage",
        ),
        Finding(
            "throttled",
            "the CPU is being slowed down to protect it" if bits & (THROTTLED_NOW | SOFT_LIMIT_NOW) else None,
            10 * MINUTE,
            "throttled",
        ),
    ]
    new = bits & ~seen_since_boot & (UNDERVOLT_SINCE | THROTTLED_SINCE)
    events = [
        n for b, n in ((UNDERVOLT_SINCE, "an under-voltage"), (THROTTLED_SINCE, "throttling")) if new & b
    ]
    problem = f"{' and '.join(events)} happened since the last check" if events and not bits & 0xF else None
    findings.append(Finding("power-blip", problem, 0, "power blip"))
    return findings


def check_units(system: System) -> list[Finding]:
    """One finding per failed systemd unit."""
    out = system.command(["systemctl", "list-units", "--failed", "--plain", "--no-legend", "--no-pager"])
    units = [line.split()[0] for line in (out or "").splitlines() if line.strip()]
    return [Finding(f"unit:{u}", f"{u} has failed", 10 * MINUTE, f"{u} failed") for u in units]


def memory_available_pct(system: System) -> float | None:
    """Percentage of memory available, from ``/proc/meminfo``."""
    info = dict(re.findall(r"^(\w+):\s+(\d+)", system.read("/proc/meminfo") or "", re.M))
    total, available = int(info.get("MemTotal", 0)), int(info.get("MemAvailable", 0))
    return 100 * available / total if total else None


def check_memory(system: System, warn_pct: float) -> Finding:
    """Too little memory available?"""
    pct = memory_available_pct(system)
    pct = 100.0 if pct is None else pct
    problem = f"only {pct:.0f}% of memory available" if pct < warn_pct else None
    return Finding("memory", problem, 15 * MINUTE, "low memory")


def check_disks(system: System, paths: Iterable[str], warn_pct: float) -> list[Finding]:
    """One finding per filesystem in ``paths``."""
    findings = []
    for path in paths:
        try:
            used, total = system.disk_usage(path)
        except OSError:
            findings.append(Finding(f"disk:{path}", f"{path} is not available", 0, f"{path} missing"))
            continue
        pct = 100 * used / total if total else 0.0
        problem = f"{path} is {pct:.0f}% full" if pct >= warn_pct else None
        findings.append(Finding(f"disk:{path}", problem, 0, f"{path} filling up"))
    return findings


def sectors_written(system: System, device: str) -> int | None:
    """Sectors (512 bytes) written to ``device`` since boot, from ``/proc/diskstats``."""
    for line in (system.read("/proc/diskstats") or "").splitlines():
        fields = line.split()
        if len(fields) > 9 and fields[2] == device:
            return int(fields[9])
    return None


def writes_last_day(samples: Sequence[tuple[float, int]], now: float, sectors: int) -> float | None:
    """GB written over the last ~24 hours, from the oldest sample at least 23 hours old."""
    old = [(t, s) for t, s in samples if now - t >= 23 * 3600]
    if not old:
        return None
    t0, s0 = max(old)
    return (sectors - s0) * 512 / 1e9 * DAY / (now - t0)


def check_writes(gb: float | None, warn_gb: float, device: str) -> Finding:
    """Too many writes for an SD card?"""
    problem = f"{gb:.0f} GB written to {device} in the last day" if gb is not None and gb > warn_gb else None
    return Finding("sd-writes", problem, 0, "heavy disk writes")


def kernel_version_key(release: str) -> list[int]:
    """Sort key for kernel releases, ignoring the flavour after the last ``-``.

    ``6.12.47+rpt-rpi-2712`` -> ``[6, 12, 47]``; ``6.8.0-45-generic`` -> ``[6, 8, 0, 45]``.
    """
    return [int(n) for n in re.findall(r"\d+", release.rsplit("-", 1)[0].split("+")[0])]


def check_reboot(system: System) -> Finding:
    """Is a reboot pending? ``/run/reboot-required`` exists, or a newer same-flavour kernel is installed."""
    running = system.running_kernel()
    flavour = running.rsplit("-", 1)[-1]
    newest = max(
        (k for k in system.kernels() if k.rsplit("-", 1)[-1] == flavour),
        key=kernel_version_key,
        default=running,
    )
    if system.read("/run/reboot-required") is not None:
        problem: str | None = "updates are waiting for a reboot"
    elif kernel_version_key(newest) > kernel_version_key(running):
        problem = f"kernel {newest} is installed but {running} is running"
    else:
        problem = None
    return Finding("reboot", problem, 2 * DAY, "reboot needed")


# --- alert state ------------------------------------------------------------------------------------


@dataclass
class State:
    """Kept between runs.

    Attributes:
        since: When each current problem was first seen (epoch seconds).
        alerted: Problems that have been reported (key -> text).
        boot_id: Detects reboots, which reset the "since boot" counters.
        throttle_seen: "Since boot" throttling bits already reported.
        write_samples: ``(time, sectors written)`` for the last day.
    """

    since: dict[str, float] = field(default_factory=dict)
    alerted: dict[str, str] = field(default_factory=dict)
    boot_id: str = ""
    throttle_seen: int = 0
    write_samples: list[tuple[float, int]] = field(default_factory=list)

    @classmethod
    def load(cls, path: Path) -> State:
        """Read the state file, or start fresh."""
        try:
            data = json.loads(path.read_text())
            data["write_samples"] = [tuple(s) for s in data.get("write_samples", [])]
            return cls(**data)
        except (OSError, ValueError, TypeError):
            return cls()

    def save(self, path: Path) -> None:
        """Write the state file atomically."""
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.__dict__))
        tmp.replace(path)


Alert = tuple[str, str, str]  # title, message, priority


def evaluate(findings: Iterable[Finding], state: State, now: float, host: str) -> list[Alert]:
    """Turn findings into alerts: after each problem's delay, once, then "OK again" when it clears.

    Problems not seen in this run (e.g. a failed unit that was removed) count as cleared.
    """
    alerts: list[Alert] = []
    current = {f.key: f for f in findings}
    for key, finding in current.items():
        if finding.problem is None:
            continue
        first = state.since.setdefault(key, now)
        if key not in state.alerted and now - first >= finding.delay:
            state.alerted[key] = finding.problem
            priority = "low" if key in ("reboot", "power-blip") else "high"
            alerts.append(
                (
                    f"{host}: {finding.title or key}",
                    finding.problem[0].upper() + finding.problem[1:] + ".",
                    priority,
                )
            )
    for key in list(state.since):
        if key not in current or current[key].problem is None:
            del state.since[key]
            reported = state.alerted.pop(key, None)
            if reported and key != "power-blip":  # a blip is an event, not a state: no "OK again"
                alerts.append((f"{host}: OK again", f"Cleared: {reported}.", "low"))
    return alerts


def display(
    temp: float | None, bits: int | None, findings: Sequence[Finding], gb: float | None, mem: Finding
) -> dict[str, str]:
    """Status fields for ``host.json`` (see ``docs/host-health.md``)."""
    failed = [f.key.split(":", 1)[1] for f in findings if f.key.startswith("unit:")]
    reboot = next((f for f in findings if f.key == "reboot"), None)
    problems = [f for f in findings if f.problem]
    return {
        "cpu_temp": f"{temp:.1f} °C" if temp is not None else "-",
        "power": "-" if bits is None else ("under-voltage" if bits & UNDERVOLT_NOW else "OK"),
        "failed_units": ", ".join(failed) if failed else "none",
        "memory": mem.problem or "OK",
        "writes_24h": f"{gb:.1f} GB" if gb is not None else "-",
        "reboot": "needed" if reboot and reboot.problem else "not needed",
        "problems": str(len(problems)),
    }


@dataclass
class Settings:
    """Thresholds, read by :meth:`from_env`."""

    temp_warn_c: float = 80.0
    mem_warn_pct: float = 10.0
    disk_paths: tuple[str, ...] = ("/",)
    disk_warn_pct: float = 90.0
    write_device: str = "mmcblk0"
    write_warn_gb: float = 20.0

    @classmethod
    def from_env(cls) -> Settings:
        """Read the thresholds from the environment."""
        return cls(
            temp_warn_c=env_float("TEMP_WARN_C", 80.0),
            mem_warn_pct=env_float("MEM_WARN_PCT", 10.0),
            disk_paths=tuple(env_str("DISK_PATHS", "/").split()),
            disk_warn_pct=env_float("DISK_WARN_PCT", 90.0),
            write_device=env_str("WRITE_DEVICE", "mmcblk0"),
            write_warn_gb=env_float("WRITE_WARN_GB", 20.0),
        )


def check_all(
    system: System, settings: Settings, state: State, now: float
) -> tuple[list[Finding], dict[str, str], Sample]:
    """Run every check, updating the reboot-scoped parts of ``state``; returns findings, status, metrics."""
    boot_id = (system.read("/proc/sys/kernel/random/boot_id") or "").strip()
    if boot_id != state.boot_id:
        state.boot_id, state.throttle_seen, state.write_samples = boot_id, 0, []
    temp = cpu_temperature(system)
    bits = throttle_bits(system)
    findings = [check_temperature(temp, settings.temp_warn_c, "temperature" in state.alerted)]
    findings += check_throttling(bits, state.throttle_seen)
    if bits is not None:
        state.throttle_seen |= bits & (UNDERVOLT_SINCE | THROTTLED_SINCE)
    findings += check_units(system)
    memory = check_memory(system, settings.mem_warn_pct)
    findings.append(memory)
    findings += check_disks(system, settings.disk_paths, settings.disk_warn_pct)
    gb = None
    sectors = sectors_written(system, settings.write_device)
    if sectors is not None:
        gb = writes_last_day(state.write_samples, now, sectors)
        if not state.write_samples or now - state.write_samples[-1][0] >= 3600:  # hourly samples
            state.write_samples.append((now, sectors))
        state.write_samples = [s for s in state.write_samples if now - s[0] <= 26 * 3600]
        findings.append(check_writes(gb, settings.write_warn_gb, settings.write_device))
    findings.append(check_reboot(system))
    readings = Sample(
        "host",
        {
            "cpu_temp_c": temp,
            "fan_rpm": system.fan_rpm(),
            "throttled": None if bits is None else bool(bits & (THROTTLED_NOW | SOFT_LIMIT_NOW)),
            "undervolt": None if bits is None else bool(bits & UNDERVOLT_NOW),
            "mem_available_pct": memory_available_pct(system),
            "writes_gb_24h": gb,
            "failed_units": sum(1 for f in findings if f.key.startswith("unit:")),
            "problems": sum(1 for f in findings if f.problem),
        },
    )
    return findings, display(temp, bits, findings, gb, memory), readings


def main() -> None:
    """Entry point: one run (meant for a 5-minute timer)."""
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    outputs = Outputs.from_env()
    state_file = Path(env_str("HEALTH_STATE_FILE", "/var/lib/host-health/state.json"))
    state = State.load(state_file)
    now = time.time()
    findings, status, readings = check_all(System(), Settings.from_env(), state, now)
    host = socket.gethostname()
    notifier = Notifier(outputs.ntfy_url)
    for title, message, priority in evaluate(findings, state, now, host):
        log.info("%s: %s", title, message)
        notifier.send(title, message, priority=priority, tags="warning" if priority == "high" else "")
    VictoriaMetrics(outputs.vm_url).write([Sample(readings.measurement, readings.fields, {"host": host})])
    state.save(state_file)
    write_status(outputs.status_dir, "host", status | {"checked": time.strftime("%H:%M")})


if __name__ == "__main__":
    main()
