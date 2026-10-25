"""Client for the Octopus Energy REST API (https://developer.octopus.energy/rest/).

Authentication is HTTP Basic with the account's API key as the user name and an empty password.
Product and tariff rate endpoints are public; consumption and account endpoints need the key.

Lesson learned: paging through a *long* consumption range (``next`` links over many pages) returned
repeated and skipped half-hours. :meth:`OctopusClient.consumption` therefore asks for 30-day chunks,
each of which fits in a single 1,500-record page (30 x 48 = 1,440), and de-duplicates by slot.
"""

from __future__ import annotations

import base64
import json
import time
import urllib.parse
import urllib.request
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

API = "https://api.octopus.energy/v1"
JSON = dict[str, Any]

#: Fetches a URL and returns the decoded JSON body; ``auth`` says whether to send the API key.
Fetcher = Callable[[str, bool], JSON]


def parse_time(text: str) -> datetime:
    """Parse an API timestamp (ISO 8601, possibly ending in ``Z``) as an aware UTC datetime."""
    return datetime.fromisoformat(text.replace("Z", "+00:00")).astimezone(UTC)


@dataclass(frozen=True)
class RatePeriod:
    """A price that applies from ``start`` (inclusive) to ``end`` (exclusive), in pence incl. VAT."""

    start: datetime
    end: datetime
    pence: float


class OctopusClient:
    """Thin wrapper over the endpoints the collector needs.

    Args:
        api_key: The account's API key (``sk_live_...``).
        fetch: HTTP function, injectable for tests. Defaults to a real request with retries.
    """

    def __init__(self, api_key: str, fetch: Fetcher | None = None) -> None:
        """Store the key and the HTTP function."""
        self._auth = "Basic " + base64.b64encode(f"{api_key}:".encode()).decode()
        self._fetch = fetch or self._http_get

    def _http_get(self, url: str, auth: bool) -> JSON:
        """GET ``url`` with up to three attempts (the API occasionally times out)."""
        headers = {"Authorization": self._auth} if auth else {}
        for attempt in range(3):
            try:
                with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=60) as resp:
                    result: JSON = json.load(resp)
                    return result
            except OSError:
                if attempt == 2:
                    raise
                time.sleep(5)
        raise AssertionError("unreachable")

    def _paged(self, url: str, auth: bool = True) -> Iterator[JSON]:
        """Yield every result across ``next`` links."""
        next_url: str | None = url
        while next_url:
            page = self._fetch(next_url, auth)
            yield from page["results"]
            next_url = page.get("next")

    def account(self, number: str) -> JSON:
        """Return the account record: properties, meter points, meters and tariff agreements."""
        return self._fetch(f"{API}/accounts/{number}/", True)

    def consumption(
        self, kind: str, point: str, serial: str, since: datetime, now: datetime | None = None
    ) -> list[tuple[datetime, float]]:
        """Half-hourly readings from ``since`` until now, oldest first, one value per slot.

        Args:
            kind: ``"electricity"`` or ``"gas"``.
            point: MPAN (electricity) or MPRN (gas).
            serial: Meter serial number.
            since: Start of the range (aware datetime).
            now: End of the range; defaults to the current time.

        Returns:
            ``(slot start in UTC, consumption)`` pairs. Electricity is in kWh; SMETS2 gas meters
            report m³ (SMETS1 gas meters report kWh).
        """
        end_all = now or datetime.now(UTC)
        slots: dict[datetime, float] = {}
        start = since
        while start < end_all:
            end = min(start + timedelta(days=30), end_all)
            params = {
                "period_from": start.isoformat(),
                "period_to": end.isoformat(),
                "page_size": 1500,
                "order_by": "period",
            }
            query = urllib.parse.urlencode(params)
            url = f"{API}/{kind}-meter-points/{point}/meters/{serial}/consumption/?{query}"
            for record in self._paged(url):
                slots[parse_time(record["interval_start"])] = float(record["consumption"])
            start = end
        return sorted(slots.items())

    def rates(self, kind: str, tariff: str, what: str, start: datetime, end: datetime) -> list[RatePeriod]:
        """Unit rates or standing charges of one tariff between ``start`` and ``end``.

        Args:
            kind: ``"electricity"`` or ``"gas"``.
            tariff: Tariff code, e.g. ``E-1R-GO-FIX-12M-26-02-11-E``.
            what: ``"standard-unit-rates"`` or ``"standing-charges"``.
            start: Range start (aware).
            end: Range end (aware); open-ended rates are clipped to it.

        Returns:
            Periods in pence including VAT. Where a tariff lists separate direct-debit and
            non-direct-debit prices, only the direct-debit one is kept.
        """
        query = urllib.parse.urlencode(
            {"period_from": start.isoformat(), "period_to": end.isoformat(), "page_size": 1500}
        )
        url = f"{API}/products/{product_code(tariff)}/{kind}-tariffs/{tariff}/{what}/?{query}"
        periods = []
        for r in self._paged(url, auth=False):
            if r.get("payment_method") not in (None, "DIRECT_DEBIT"):
                continue
            lo = max(parse_time(r["valid_from"]), start)
            hi = min(parse_time(r["valid_to"]), end) if r.get("valid_to") else end
            if lo < hi:
                periods.append(RatePeriod(lo, hi, float(r["value_inc_vat"])))
        return periods


def product_code(tariff: str) -> str:
    """Extract the product code from a tariff code.

    Tariff codes look like ``E-1R-<PRODUCT>-<REGION>`` (electricity, single register) or
    ``G-1R-<PRODUCT>-<REGION>`` (gas), e.g. ``E-1R-GO-FIX-12M-26-02-11-E`` -> ``GO-FIX-12M-26-02-11``.
    """
    parts = tariff.split("-")
    if len(parts) < 4 or parts[0] not in ("E", "G"):
        raise ValueError(f"unrecognised tariff code {tariff!r}")
    return "-".join(parts[2:-1])
