"""``octopus-collector``: fetch Octopus half-hourly usage, price it, and publish it.

Run it every few hours from a systemd timer. Smart-meter data reaches Octopus 1-2 days late, so each
run re-fetches the last ``OCTOPUS_DAYS`` days plus the whole current month (re-sent samples carry the
same timestamps, which VictoriaMetrics de-duplicates). Use ``--days N`` once to backfill history.

Settings (environment):

``OCTOPUS_API_KEY`` or ``OCTOPUS_API_KEY_FILE``  The API key, or a file holding it (preferred).
``OCTOPUS_CONFIG``         Path to the JSON written by ``octopus-setup`` (default /etc/octopus/config.json).
``OCTOPUS_DAYS``           Days to re-fetch on each run, default 7.
``OFFPEAK_BELOW_P``        Import rates below this many pence count as off-peak, default 12.
``GAS_KWH_PER_M3``         Gas conversion factor, default ~11.22 (SMETS2 meters report m³).
``HOUSE_LOAD_METRIC``      Optional metric with the house's power in watts (e.g. ``solis_load_w``),
                           used to estimate solar/battery savings; empty to disable.

Metrics (pence include VAT; one sample per half-hour, timestamped at the slot start):
``octopus_import_kwh``, ``_import_rate_p``, ``_import_cost_p``, ``_import_offpeak_kwh``,
``_import_peak_kwh``, ``_export_kwh``, ``_export_rate_p``, ``_export_earn_p``, ``_gas_m3``,
``_gas_kwh``, ``_gas_cost_p``, ``_nosolar_cost_p``, and ``octopus_standing_p{fuel=...}`` once a day
(timestamped at local noon, so it always lands on the right day in daily charts).
"""

from __future__ import annotations

import argparse
import json
import logging
import urllib.parse
import urllib.request
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from homelab_tools.common.config import Outputs, env_float, env_int, env_str
from homelab_tools.common.metrics import Sample, VictoriaMetrics
from homelab_tools.common.status import write_status
from homelab_tools.octopus.api import OctopusClient, RatePeriod, parse_time
from homelab_tools.octopus.pricing import DEFAULT_GAS_KWH_PER_M3, Ledger, RateTable, summarise

log = logging.getLogger("octopus-collector")


def agreement_rates(
    client: OctopusClient, kind: str, agreements: list[dict[str, Any]], what: str, since: datetime
) -> RateTable:
    """Collect rates for every agreement that overlaps ``since``..now into one table.

    Each agreement's rates are clipped to the agreement's own dates, so a tariff switch mid-month
    prices each half-hour at the tariff that applied to it.
    """
    until = datetime.now(UTC) + timedelta(days=2)
    periods: list[RatePeriod] = []
    for ag in agreements:
        start = parse_time(ag["from"])
        end = parse_time(ag["to"]) if ag.get("to") else until
        if end <= since:
            continue
        for p in client.rates(kind, ag["tariff"], what, max(start, since), end):
            periods.append(RatePeriod(max(p.start, start), min(p.end, end), p.pence))
    return RateTable(periods)


def house_use(vm_url: str, metric: str, slots: list[datetime]) -> dict[datetime, float]:
    """Average a power metric (W) over each half-hour slot and convert it to kWh.

    Only slots with at least 20 samples (of a possible 30 at one per minute) are returned, so gaps
    in the source data do not produce misleading savings.
    """
    if not (vm_url and metric and slots):
        return {}

    def query(expr: str) -> dict[int, float]:
        params = urllib.parse.urlencode(
            {
                "query": expr,
                "start": int(min(slots).timestamp()) + 1800,
                "end": int(max(slots).timestamp()) + 1800,
                "step": "1800",
            }
        )
        with urllib.request.urlopen(f"{vm_url}/api/v1/query_range?{params}", timeout=30) as resp:
            result = json.load(resp)["data"]["result"]
        return {int(t): float(v) for t, v in result[0]["values"]} if result else {}

    try:
        avg, count = query(f"avg_over_time({metric}[30m])"), query(f"count_over_time({metric}[30m])")
    except (OSError, KeyError, ValueError) as exc:
        log.warning("house-use query failed: %s", exc)
        return {}
    return {
        datetime.fromtimestamp(t - 1800, UTC): watts * 0.5 / 1000
        for t, watts in avg.items()
        if count.get(t, 0) >= 20
    }


def load_key() -> str:
    """Read the API key from ``OCTOPUS_API_KEY_FILE`` (preferred) or ``OCTOPUS_API_KEY``."""
    key_file = env_str("OCTOPUS_API_KEY_FILE", "")
    return Path(key_file).read_text().strip() if key_file else env_str("OCTOPUS_API_KEY")


def collect(
    client: OctopusClient,
    config: dict[str, Any],
    since: datetime,
    ledger: Ledger,
    gas_factor: float,
    vm_url: str = "",
    house_metric: str = "",
) -> None:
    """Fetch and price everything in ``config`` from ``since`` onwards into ``ledger``."""
    import_rates: RateTable | None = None
    import_slots: list[datetime] = []
    for point in config.get("electricity", []):
        rates = agreement_rates(client, "electricity", point["agreements"], "standard-unit-rates", since)
        slots = client.consumption("electricity", point["mpan"], point["serial"], since)
        if point["export"]:
            ledger.add_export(slots, rates)
        else:
            ledger.add_import(slots, rates)
            standing = agreement_rates(client, "electricity", point["agreements"], "standing-charges", since)
            ledger.add_standing("electricity", slots, standing)
            import_rates, import_slots = rates, [m for m, _ in slots]
    for point in config.get("gas", []):
        rates = agreement_rates(client, "gas", point["agreements"], "standard-unit-rates", since)
        slots = client.consumption("gas", point["mprn"], point["serial"], since)
        ledger.add_gas(slots, rates, gas_factor)
        ledger.add_standing(
            "gas", slots, agreement_rates(client, "gas", point["agreements"], "standing-charges", since)
        )
    if import_rates and house_metric:
        recent = datetime.now(UTC) - timedelta(days=10)  # older history rarely has house-load data
        ledger.add_house_use(
            house_use(vm_url, house_metric, [m for m in import_slots if m >= recent]), import_rates
        )


def fetch_start(days: int, now: datetime | None = None) -> datetime:
    """Start of the fetch window: ``days`` ago at UTC midnight, or the 1st of this month if earlier.

    Always covering the whole current month keeps the month-to-date total complete.
    """
    now = now or datetime.now(UTC)
    days_ago = (now - timedelta(days=days)).replace(hour=0, minute=0, second=0, microsecond=0)
    month_start = now.astimezone().replace(day=1, hour=0, minute=0, second=0, microsecond=0).astimezone(UTC)
    return min(days_ago, month_start)


def main() -> None:
    """Entry point: one collection run (meant for a systemd timer)."""
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--days", type=int, default=env_int("OCTOPUS_DAYS", 7), help="days to (re)fetch")
    args = parser.parse_args()

    outputs = Outputs.from_env()
    config = json.loads(Path(env_str("OCTOPUS_CONFIG", "/etc/octopus/config.json")).read_text())
    since = fetch_start(args.days)
    ledger = Ledger(offpeak_below_p=env_float("OFFPEAK_BELOW_P", 12.0))
    collect(
        OctopusClient(load_key()),
        config,
        since,
        ledger,
        gas_factor=env_float("GAS_KWH_PER_M3", DEFAULT_GAS_KWH_PER_M3),
        vm_url=outputs.vm_url,
        house_metric=env_str("HOUSE_LOAD_METRIC", ""),
    )

    vm = VictoriaMetrics(outputs.vm_url)
    written = vm.write(
        Sample("octopus", {name: value}, tags, moment) for name, moment, value, tags in ledger.points
    )
    vm.reset_cache()  # we wrote past timestamps; cached query results would be stale
    summary = summarise(ledger.days)
    if summary:
        write_status(outputs.status_dir, "octopus", summary | {"updated": datetime.now().strftime("%H:%M")})
    log.info("wrote %d samples covering %d days since %s", written, len(ledger.days), since.date())


if __name__ == "__main__":
    main()
