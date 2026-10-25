"""``octopus-setup``: write the meter/tariff config the collector needs, from your Octopus account.

Usage::

    OCTOPUS_API_KEY=sk_live_... octopus-setup A-1234ABCD > /etc/octopus/config.json

The account number is on your bills and the dashboard. Re-run it when your tariff changes - the
collector prices each half-hour with the agreement that was in force at the time.
"""

from __future__ import annotations

import json
import sys
from typing import Any

from homelab_tools.common.config import env_str
from homelab_tools.octopus.api import JSON, OctopusClient


def build_config(account: JSON, number: str) -> dict[str, Any]:
    """Extract meter points, current meter serials and agreements from an account record.

    Only the first property (address) on the account is used; accounts with several properties can
    be split by editing the output.
    """

    def agreements(point: JSON) -> list[dict[str, str | None]]:
        return [
            {"tariff": a["tariff_code"], "from": a["valid_from"], "to": a.get("valid_to")}
            for a in point.get("agreements", [])
        ]

    prop = account["properties"][0]
    return {
        "account": number,
        "electricity": [
            {
                "mpan": p["mpan"],
                "serial": p["meters"][-1]["serial_number"],
                "export": bool(p.get("is_export")),
                "agreements": agreements(p),
            }
            for p in prop.get("electricity_meter_points", [])
        ],
        "gas": [
            {"mprn": p["mprn"], "serial": p["meters"][-1]["serial_number"], "agreements": agreements(p)}
            for p in prop.get("gas_meter_points", [])
        ],
    }


def main() -> None:
    """Entry point: print the config JSON for the account given on the command line."""
    if len(sys.argv) != 2:
        sys.exit("usage: OCTOPUS_API_KEY=... octopus-setup <account number, e.g. A-1234ABCD>")
    number = sys.argv[1]
    client = OctopusClient(env_str("OCTOPUS_API_KEY"))
    print(json.dumps(build_config(client.account(number), number), indent=1))


if __name__ == "__main__":
    main()
