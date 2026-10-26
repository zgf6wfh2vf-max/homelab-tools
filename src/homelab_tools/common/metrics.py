"""Write time-series samples to VictoriaMetrics using the InfluxDB line protocol.

VictoriaMetrics turns ``measurement field=value`` into a metric called ``measurement_field``, so
``solis pv_w=512`` becomes ``solis_pv_w``. Tags become labels: ``tado,zone=Upstairs inside_c=21``
becomes ``tado_inside_c{zone="Upstairs"}``.

Two VictoriaMetrics behaviours matter for tools that write *old* timestamps (e.g. Octopus data that
arrives a day late, or backfilled history):

* Re-sending the same sample creates a duplicate unless the server runs with
  ``-dedup.minScrapeInterval=1ms``; see ``docs/victoriametrics.md``.
* Query results for past time ranges are cached, so after backdated writes
  :meth:`VictoriaMetrics.reset_cache` must be called or graphs show stale values.

:func:`vm_query` and :func:`vm_range` read values back (tools that analyse other tools' metrics).
"""

from __future__ import annotations

import json
import logging
import math
import urllib.parse
import urllib.request
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from datetime import datetime

log = logging.getLogger(__name__)

Opener = Callable[[urllib.request.Request, float], object]
#: Evaluates a MetricsQL expression at a moment (epoch seconds); returns the first value or ``None``.
Query = Callable[[str, float], float | None]
#: Evaluates a MetricsQL expression from ``start`` to ``end`` every ``step`` seconds; returns the
#: first series as ``(epoch seconds, value)`` pairs (empty if there is no data).
RangeQuery = Callable[[str, float, float, int], list[tuple[float, float]]]
FieldValue = float | int | bool


def _urlopen(request: urllib.request.Request, timeout: float) -> object:
    """Default opener: a real HTTP request."""
    return urllib.request.urlopen(request, timeout=timeout)


def _escape(text: str) -> str:
    """Escape a measurement name, tag key or tag value for the line protocol."""
    return text.replace("\\", "\\\\").replace(" ", "\\ ").replace(",", "\\,").replace("=", "\\=")


def _format_value(value: FieldValue) -> str:
    """Render a field value; booleans become 1/0 so they graph like any other number."""
    if isinstance(value, bool):
        return "1" if value else "0"
    return repr(round(float(value), 6)) if isinstance(value, float) else str(value)


@dataclass(frozen=True)
class Sample:
    """One line of line protocol: several fields measured at the same moment.

    Attributes:
        measurement: Metric prefix, e.g. ``solis``.
        fields: Field name to number. ``None`` and NaN values are skipped, so callers can pass
            readings that a device did not report without special-casing them.
        tags: Label name to value.
        time: When the values were measured; ``None`` lets the server use its receive time.
    """

    measurement: str
    fields: Mapping[str, FieldValue | None]
    tags: Mapping[str, str] = field(default_factory=dict)
    time: datetime | None = None

    def to_line(self) -> str | None:
        """Render as a line-protocol line, or ``None`` if there are no usable fields."""
        values = {
            k: v
            for k, v in self.fields.items()
            if v is not None and not (isinstance(v, float) and math.isnan(v))
        }
        if not values:
            return None
        head = _escape(self.measurement) + "".join(
            f",{_escape(k)}={_escape(v)}" for k, v in sorted(self.tags.items())
        )
        body = ",".join(f"{_escape(k)}={_format_value(v)}" for k, v in values.items())
        stamp = f" {int(self.time.timestamp() * 1e9)}" if self.time is not None else ""
        return f"{head} {body}{stamp}"


class VictoriaMetrics:
    """Minimal VictoriaMetrics writer.

    Args:
        base_url: e.g. ``http://127.0.0.1:8428``. Empty disables every write.
        opener: HTTP function, injectable for tests.
        batch_size: Lines per HTTP request when writing many samples.
    """

    def __init__(self, base_url: str, opener: Opener = _urlopen, batch_size: int = 5000) -> None:
        """Store the server URL, HTTP function and batch size."""
        self.base_url = base_url.rstrip("/")
        self._open = opener
        self.batch_size = batch_size

    @property
    def enabled(self) -> bool:
        """Whether a server is configured."""
        return bool(self.base_url)

    def write(self, samples: Iterable[Sample]) -> int:
        """Send samples; returns the number of lines written (0 when disabled or on failure)."""
        if not self.enabled:
            return 0
        lines = [line for line in (s.to_line() for s in samples) if line]
        written = 0
        for start in range(0, len(lines), self.batch_size):
            chunk = lines[start : start + self.batch_size]
            request = urllib.request.Request(
                f"{self.base_url}/write", data="\n".join(chunk).encode(), method="POST"
            )
            try:
                self._open(request, 60)
            except OSError as exc:
                log.warning("metrics write failed: %s", exc)
                return written
            written += len(chunk)
        return written

    def reset_cache(self) -> None:
        """Drop cached query results; call after writing samples with past timestamps."""
        if not self.enabled:
            return
        try:
            self._open(urllib.request.Request(f"{self.base_url}/internal/resetRollupResultCache"), 30)
        except OSError as exc:
            log.warning("cache reset failed: %s", exc)


def _get_json(url: str) -> dict[str, object]:
    """GET a VictoriaMetrics API URL and decode the JSON body."""
    with urllib.request.urlopen(url, timeout=30) as resp:
        body: dict[str, object] = json.load(resp)
        return body


def vm_query(vm_url: str) -> Query:
    """A :data:`Query` backed by VictoriaMetrics' instant-query API."""

    def query(expr: str, at: float) -> float | None:
        params = urllib.parse.urlencode({"query": expr, "time": int(at)})
        result = _get_json(f"{vm_url}/api/v1/query?{params}")["data"]["result"]  # type: ignore[index]
        return float(result[0]["value"][1]) if result else None

    return query


def vm_range(vm_url: str) -> RangeQuery:
    """A :data:`RangeQuery` backed by VictoriaMetrics' range-query API."""

    def query(expr: str, start: float, end: float, step: int) -> list[tuple[float, float]]:
        params = urllib.parse.urlencode({"query": expr, "start": int(start), "end": int(end), "step": step})
        result = _get_json(f"{vm_url}/api/v1/query_range?{params}")["data"]["result"]  # type: ignore[index]
        return [(float(t), float(v)) for t, v in result[0]["values"]] if result else []

    return query
