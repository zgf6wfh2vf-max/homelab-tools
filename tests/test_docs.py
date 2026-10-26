"""Keep the documentation honest: every status-file field a tool produces must be documented."""

from __future__ import annotations

from datetime import UTC, date, datetime
from pathlib import Path

import pytest

from homelab_tools.ad5x.tracker import JobTracker
from homelab_tools.backup.restore_test import Report
from homelab_tools.backup.restore_test import display as restore_display
from homelab_tools.battery.check import Outlook, outlook_display
from homelab_tools.forecast.solar import Forecast
from homelab_tools.forecast.solar import status as forecast_status
from homelab_tools.host.health import Finding
from homelab_tools.host.health import display as host_display
from homelab_tools.octopus.compare import Comparison
from homelab_tools.octopus.compare import display as tariff_display
from homelab_tools.octopus.pricing import DayTotals, summarise
from homelab_tools.octopus.sessions import Snapshot
from homelab_tools.octopus.sessions import display as sessions_display
from homelab_tools.podpoint.model import charger_state
from homelab_tools.podpoint.model import display as ev_display
from homelab_tools.report.weekly import WeekFigures
from homelab_tools.report.weekly import display as weekly_display
from homelab_tools.solis.registers import REGISTER_BLOCKS, decode, display_fields
from homelab_tools.tado.model import display as tado_display

DOCS = Path(__file__).parent.parent / "docs"
README = Path(__file__).parent.parent / "README.md"


def documented(page: str) -> str:
    return (DOCS / page).read_text()


@pytest.mark.parametrize(
    ("page", "keys"),
    [
        (
            "solis.md",
            display_fields(decode({a: 0 for s, n in REGISTER_BLOCKS for a in range(s, s + n)})).keys(),
        ),
        ("ad5x.md", JobTracker().display(None, 0).keys()),
        (
            "octopus.md",
            (summarise({date(2026, 1, 1): DayTotals(import_cost_p=1, import_slots=48)}) or {}).keys(),
        ),
        (
            "podpoint.md",
            ev_display(charger_state("available", "", ""), "", None, [], [], datetime.now(UTC)).keys(),
        ),
        ("tado.md", tado_display([], 0.0, "SUN", 0, []).keys()),
        ("solar-forecast.md", forecast_status(Forecast({}, {}), date(2026, 1, 1), None).keys()),
        (
            "battery-check.md",
            ["overnight", "checked", *outlook_display(Outlook(datetime.now(UTC), 0, 0, 0, 0, None))],
        ),
        ("octopus-sessions.md", [*sessions_display(Snapshot(), datetime.now(UTC)), "checked"]),
        ("tariff-compare.md", tariff_display(Comparison("x", 0, 0, 0)).keys()),
        ("host-health.md", [*host_display(None, None, [], None, Finding("memory", None)), "checked"]),
        (
            "usage-watch.md",
            ["gas_day", "gas_used", "gas_expected", "gas_outside", "base_now", "base_usual", "checked"],
        ),
        ("restic-restore-test.md", restore_display(Report()).keys()),
        ("weekly-report.md", weekly_display(WeekFigures(date(2026, 1, 5)), None).keys()),
    ],
)
def test_status_fields_are_documented(page: str, keys) -> None:
    text = documented(page)
    missing = [k for k in keys if f"`{k}`" not in text]
    assert not missing, f"{page} does not document status fields {missing}"


def test_every_doc_page_is_linked() -> None:
    linked = README.read_text() + documented("installation.md")
    for page in DOCS.glob("*.md"):
        assert page.name in linked, f"docs/{page.name} is not linked from README or installation.md"
