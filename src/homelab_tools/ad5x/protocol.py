"""Talk to a FlashForge printer over its legacy G-code TCP port (8899), read-only.

The printer accepts G-code-like commands prefixed with ``~`` and answers with text ending in ``ok``.
Only query commands are sent:

* ``~M601 S1`` take control of the session (required before other commands),
* ``~M119`` machine status (``MachineStatus``, ``MoveMode``, ``CurrentFile``, ``LED``),
* ``~M27`` print progress (``SD printing byte <done>/<total>``),
* ``~M105`` temperatures (``T0:<now>/<target> B:<now>/<target>``),
* ``~M602`` release control, so slicers and the touch screen keep working.

None of these move the machine or touch the running job, so polling is safe mid-print.
"""

from __future__ import annotations

import re
import socket
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol

PORT = 8899
QUERIES = ("~M119", "~M27", "~M105")


class _Socket(Protocol):
    """The subset of :class:`socket.socket` used here."""

    def sendall(self, data: bytes, /) -> None: ...  # noqa: D102
    def recv(self, size: int, /) -> bytes: ...  # noqa: D102
    def settimeout(self, value: float | None, /) -> None: ...  # noqa: D102
    def close(self) -> None: ...  # noqa: D102


Connector = Callable[[tuple[str, int], float], _Socket]


def _connect(address: tuple[str, int], timeout: float) -> _Socket:
    """Default connector: a real TCP connection."""
    return socket.create_connection(address, timeout=timeout)


@dataclass(frozen=True)
class PrinterStatus:
    """What the printer reported in one poll. Temperatures are in °C; ``None`` when not reported."""

    machine_status: str
    current_file: str
    led_on: bool | None
    progress_pct: float
    nozzle_c: float | None
    nozzle_target_c: float | None
    bed_c: float | None
    bed_target_c: float | None

    @property
    def state(self) -> str:
        """Collapse firmware status strings into ``printing``, ``paused``, ``done`` or ``idle``."""
        s = self.machine_status.upper()
        if "PAUSE" in s:
            return "paused"
        if "COMPLET" in s:
            return "done"
        if "BUILD" in s or "PRINT" in s:
            return "printing"
        return "idle"


def _find(pattern: str, text: str) -> str | None:
    """Return the first capture group of ``pattern`` in ``text``, stripped, or ``None``."""
    match = re.search(pattern, text, re.IGNORECASE)
    return match.group(1).strip() if match else None


def _number(pattern: str, text: str) -> float | None:
    """Like :func:`_find` but converted to float (``None`` if absent or malformed)."""
    value = _find(pattern, text)
    try:
        return float(value) if value is not None else None
    except ValueError:
        return None


def parse_status(state: str, progress: str, temps: str) -> PrinterStatus:
    """Parse the replies to ``M119``, ``M27`` and ``M105`` into a :class:`PrinterStatus`.

    Progress is reported as ``byte done/total`` on most firmware and as a bare percentage on some;
    both are handled.
    """
    done, total = _number(r"byte\s+(\d+)/", progress), _number(r"byte\s+\d+/(\d+)", progress)
    if done is not None and total:
        pct = round(done / total * 100, 1)
    else:
        pct = _number(r"(\d+(?:\.\d+)?)\s*%", progress) or 0.0
    led = _find(r"LED:\s*(\d+)", state)
    return PrinterStatus(
        machine_status=(_find(r"MachineStatus:\s*(\S+)", state) or "UNKNOWN").upper(),
        current_file=_find(r"CurrentFile:\s*(.+)", state) or "",
        led_on=None if led is None else led != "0",
        progress_pct=pct,
        nozzle_c=_number(r"T0:([\d.]+)", temps),
        nozzle_target_c=_number(r"T0:[\d.]+\s*/([\d.]+)", temps),
        bed_c=_number(r"B:([\d.]+)", temps),
        bed_target_c=_number(r"B:[\d.]+\s*/([\d.]+)", temps),
    )


def _query(sock: _Socket, command: str, timeout: float) -> str:
    """Send one command and collect its reply until a line ends with ``ok`` or ``timeout`` passes."""
    sock.sendall((command + "\r\n").encode())
    chunks: list[str] = []
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            data = sock.recv(4096)
        except TimeoutError:
            break
        if not data:
            break
        chunks.append(data.decode(errors="replace"))
        if re.search(r"\bok\b", chunks[-1]):
            break
    return "".join(chunks)


def read_status(
    host: str, port: int = PORT, timeout: float = 5.0, connect: Connector = _connect
) -> PrinterStatus | None:
    """Query the printer; returns ``None`` if it is switched off or unreachable (a normal state)."""
    try:
        sock = connect((host, port), timeout)
    except OSError:
        return None
    sock.settimeout(timeout)
    try:
        _query(sock, "~M601 S1", timeout)
        replies = [_query(sock, q, timeout) for q in QUERIES]
        _query(sock, "~M602", timeout)
    except OSError:
        return None
    finally:
        sock.close()
    return parse_status(*replies)
