"""Turn a stream of printer polls into job events, estimates and history - no I/O, fully testable.

A 3D printer that is *switched off when not in use* breaks the usual "unreachable = alert" rule, so
the tracker distinguishes:

* off while idle - normal, silent;
* off while a job was running - "went offline mid-print" (power cut, tripped plug), alerted;
* job finished, paused, stopped/cancelled.

The finish-time estimate extrapolates from progress: with a known start time it is
``elapsed / progress - elapsed``; when the monitor joins a print already under way it uses the rate
of progress observed since joining.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import PurePath
from typing import Any

from homelab_tools.ad5x.protocol import PrinterStatus
from homelab_tools.common.timefmt import duration, when

HISTORY_DAYS = 30
#: Consecutive failed polls before a running job counts as lost. The printer accepts one control
#: connection at a time, so a single poll can be refused while the touch screen, a slicer or the
#: printer's own cloud client is talking to it - about 1 poll in 15 during a print.
OFFLINE_AFTER = 3
LABELS = {"off": "Off", "idle": "Idle", "printing": "Printing", "paused": "Paused", "done": "Finished"}


@dataclass
class Job:
    """The job being tracked.

    Attributes:
        file: File name reported by the printer.
        seen: Epoch seconds when the tracker first saw the job.
        seen_pct: Progress at that moment.
        started: Epoch seconds the job started, or ``None`` if the tracker joined mid-print.
        pct: Last progress seen.
    """

    file: str
    seen: float
    seen_pct: float
    started: float | None
    pct: float = 0.0


@dataclass(frozen=True)
class Event:
    """Something worth a notification."""

    title: str
    message: str
    priority: str = "default"
    tags: str = "printer"


@dataclass(frozen=True)
class HistoryEntry:
    """A finished (or lost) job."""

    file: str
    end: float
    seconds: float | None
    result: str  # "done", "stopped" or "lost"


def nice_name(path: str) -> str:
    """``/data/crystal_dragon.3mf`` -> ``"crystal dragon"``."""
    name = re.sub(r"\.(gcode|gx|3mf)$", "", PurePath(path or "").name, flags=re.IGNORECASE)
    return name.replace("_", " ") or "-"


def _ago(epoch: float, now: float) -> str:
    """:func:`~homelab_tools.common.timefmt.when` for epoch seconds."""
    return when(datetime.fromtimestamp(epoch).astimezone(), datetime.fromtimestamp(now).astimezone())


def _temperature(now: float | None, target: float | None) -> str:
    """``"230 / 230 °C"``, ``"25 °C"`` with no target, or ``"-"``."""
    if now is None:
        return "-"
    return f"{now:.0f} / {target:.0f} °C" if target else f"{now:.0f} °C"


@dataclass
class JobTracker:
    """State machine fed with one :class:`PrinterStatus` (or ``None`` = unreachable) per poll."""

    state: str = "off"
    job: Job | None = None
    history: list[HistoryEntry] = field(default_factory=list)
    misses: int = 0  # consecutive unreachable polls while a job was running

    # --- persistence ---------------------------------------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        """Serialise for a JSON state file, so a restart does not forget a running job."""
        return {
            "state": self.state,
            "job": asdict(self.job) if self.job else None,
            "history": [asdict(h) for h in self.history],
            "misses": self.misses,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> JobTracker:
        """Inverse of :meth:`to_dict`; tolerates an empty or partial dict."""
        job = data.get("job")
        return cls(
            state=data.get("state", "off"),
            job=Job(**job) if job else None,
            history=[HistoryEntry(**h) for h in data.get("history", [])],
            misses=int(data.get("misses", 0)),
        )

    # --- transitions -----------------------------------------------------------------------------

    def step(self, status: PrinterStatus | None, now: float) -> list[Event]:
        """Advance with one poll result and return the notifications it causes."""
        events: list[Event] = []
        if status is None and self.job is not None and self.state in ("printing", "paused"):
            self.misses += 1
            if self.misses < OFFLINE_AFTER:
                return events  # probably just a refused connection: keep the job, decide next poll
        else:
            self.misses = 0
        state = "off" if status is None else status.state
        previous = self.state

        if status is not None and state in ("printing", "paused") and self.job is None:
            fresh = status.progress_pct < 2
            self.job = Job(status.current_file, now, status.progress_pct, now if fresh else None)
            if fresh:
                events.append(
                    Event(
                        "Print started",
                        f"{nice_name(status.current_file)} has started.",
                        tags="arrow_forward,printer",
                    )
                )

        if self.job and state == "paused" and previous != "paused":
            events.append(
                Event(
                    "Print paused",
                    f"{nice_name(self.job.file)} paused at {self.job.pct:.0f}% - "
                    "filament runout or a pause on the printer?",
                    "high",
                    "warning,printer",
                )
            )

        ended = state in ("done", "idle") or (state == "off" and previous in ("printing", "paused"))
        if self.job and ended:
            events.append(self._finish(state, now))
        self.history = [h for h in self.history if now - h.end < HISTORY_DAYS * 86400]

        if self.job and status is not None:
            self.job.pct = status.progress_pct
        self.state = state
        return events

    def _finish(self, state: str, now: float) -> Event:
        """Close the current job, record it in history and describe how it ended."""
        assert self.job is not None
        job, self.job = self.job, None
        took = now - (job.started or job.seen)
        name = nice_name(job.file)
        if state == "off":
            result, event = (
                "lost",
                Event(
                    "Printer went offline mid-print",
                    f"{name} was at {job.pct:.0f}% when the printer stopped responding - "
                    "power cut, tripped plug, or switched off?",
                    "high",
                    "warning,printer",
                ),
            )
        elif state == "done" or job.pct >= 99:
            after = f" after {duration(took)}" if job.started else ""
            result, event = (
                "done",
                Event("Print finished", f"{name} is done{after}.", tags="white_check_mark,printer"),
            )
        else:
            result, event = (
                "stopped",
                Event("Print stopped", f"{name} stopped at {job.pct:.0f}% (cancelled?)."),
            )
        self.history.append(HistoryEntry(job.file, now, took if job.started else None, result))
        return event

    # --- estimates and display -----------------------------------------------------------------

    def remaining_seconds(self, now: float) -> float | None:
        """Estimated seconds until the job finishes, or ``None`` while there is too little to go on."""
        job = self.job
        if job is None:
            return None
        if job.started is not None:
            if job.pct < 3:
                return None
            elapsed = now - job.started
            return elapsed / (job.pct / 100) - elapsed
        gained = job.pct - job.seen_pct
        if gained < 2:
            return None
        return (100 - job.pct) / (gained / (now - job.seen))

    def display(self, status: PrinterStatus | None, now: float) -> dict[str, str]:
        """Display strings for a dashboard status file."""
        job = self.job
        remaining = self.remaining_seconds(now)
        week = [h for h in self.history if now - h.end < 7 * 86400 and h.result == "done"]
        last = self.history[-1] if self.history else None
        if job is None:
            elapsed = "-"
        elif job.started is not None:
            elapsed = duration(now - job.started)
        else:
            elapsed = "since " + when(
                datetime.fromtimestamp(job.seen).astimezone(), datetime.fromtimestamp(now)
            )
        return {
            "state": LABELS.get(self.state, self.state),
            "progress": f"{job.pct:.0f}%" if job else "-",
            "job": nice_name(job.file) if job else "-",
            "elapsed": elapsed,
            "remaining": f"≈ {duration(remaining)}"
            if remaining is not None
            else ("estimating" if job else "-"),
            "eta": f"≈ {datetime.fromtimestamp(now + remaining):%H:%M}" if remaining is not None else "-",
            "nozzle": _temperature(status.nozzle_c, status.nozzle_target_c) if status else "-",
            "bed": _temperature(status.bed_c, status.bed_target_c) if status else "-",
            "light": "-" if status is None or status.led_on is None else ("on" if status.led_on else "off"),
            "prints_7d": str(len(week)),
            "hours_7d": f"{sum(h.seconds or 0 for h in week) / 3600:.1f} h",
            "last_print": nice_name(last.file) if last else "none yet",
            "last_when": f"{_ago(last.end, now)} · {last.result}" if last else "-",
        }

    def metrics(self, status: PrinterStatus | None) -> dict[str, float | int | None]:
        """Numbers for the time-series database (``printer_<key>``); temperatures only while online."""
        fields: dict[str, float | int | None] = {
            "online": 0 if status is None else 1,
            "printing": 1 if self.state in ("printing", "paused") else 0,
        }
        if status is not None:
            fields |= {
                "progress": status.progress_pct if self.job else 0,
                "nozzle_c": status.nozzle_c,
                "nozzle_target_c": status.nozzle_target_c,
                "bed_c": status.bed_c,
                "bed_target_c": status.bed_target_c,
            }
        return fields
