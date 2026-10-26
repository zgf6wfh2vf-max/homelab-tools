# octopus-collector

Fetches **Octopus Energy** (UK) smart-meter half-hours - electricity import, export and gas - and
prices each one at the **exact rate in force at that half-hour**, including time-of-use tariffs such
as Octopus Go or Agile, tariff switches mid-month, and daily standing charges.

## Setup

1. Get your API key at <https://octopus.energy/dashboard/new/accounts/personal-details/api-access>.
   It gives read access to your account data. Store it in a file:
   ```sh
   echo 'sk_live_...' | sudo install -m 640 -o root -g homelab /dev/stdin /etc/homelab-tools/octopus-api-key
   ```
2. Generate the meter config (MPANs, meter serials, tariff agreements) from your account number:
   ```sh
   sudo OCTOPUS_API_KEY="$(sudo cat /etc/homelab-tools/octopus-api-key)" \
     /opt/homelab-tools/bin/octopus-setup A-XXXXXXXX | sudo tee /etc/homelab-tools/octopus.json
   ```
   Re-run this when your tariff changes.
3. Backfill history once (Octopus usually holds about two years):
   ```sh
   sudo -u homelab env $(sudo cat /etc/homelab-tools/octopus-collector.env | xargs) \
     /opt/homelab-tools/bin/octopus-collector --days 800
   ```
4. Enable the timer (every 3 hours): `sudo systemctl enable --now octopus-collector.timer`.

## Settings

| Setting | Default | Meaning |
|---|---|---|
| `OCTOPUS_API_KEY_FILE` / `OCTOPUS_API_KEY` | required | API key (file preferred) |
| `OCTOPUS_CONFIG` | `/etc/octopus/config.json` | Output of `octopus-setup` |
| `OCTOPUS_DAYS` | 7 | Days re-fetched per run (the current month is always included) |
| `OFFPEAK_BELOW_P` | 12 | Import rates below this (pence) count as off-peak |
| `GAS_KWH_PER_M3` | 11.22 | m³ → kWh conversion for SMETS2 gas meters |
| `HOUSE_LOAD_METRIC` | - | A house-power metric in W (e.g. `solis_load_w`) for savings estimates |

## How the numbers are made

* **Cost** of a half-hour = kWh × unit rate (pence incl. VAT) of the agreement in force then.
* **Standing charges** are added once per local day; `Electric` / `Gas` on the status include them.
* **Net** = electricity + gas (incl. standing charges) − export earnings.
* **Solar saving** = house use × import rate (what it would have cost with no solar or battery)
  − what was paid for imports + export earnings. It needs `HOUSE_LOAD_METRIC` and shows
  "pending" until Octopus has published a day that house-load data covers completely (so the first
  day of monitoring, which starts part-way through, is skipped).
* **Gas**: SMETS2 meters report m³; kWh uses a typical calorific value (39.5 MJ/m³). Your bill uses
  the actual value, so expect differences of a percent or two.

## Outputs

**Metrics** (`octopus_*`, one sample per half-hour at the slot start): `import_kwh`,
`import_rate_p`, `import_cost_p`, `import_offpeak_kwh`, `import_peak_kwh`, `export_kwh`,
`export_rate_p`, `export_earn_p`, `gas_m3`, `gas_kwh`, `gas_cost_p`, `nosolar_cost_p`, and
`standing_p{fuel="electricity|gas"}` once a day, at local noon.

**Status** (`octopus.json`, newest complete day): `day`, `import_kwh`, `import_cost`, `export_kwh`,
`export_earn`, `gas_kwh`, `gas_cost`, `net`, `saved`, `month_net`, `month_days`, `updated`.

## Things learned the hard way

* **Fetch in 30-day chunks.** Paging through long ranges returned repeated and skipped half-hours.
  Each 30-day request fits in one 1,500-record page.
* **Data lags 1-2 days**, and the newest day is usually partial, so summaries use the newest
  *complete* day.
* **Standing charges on the first day:** when the fetch window starts at UTC midnight in summer,
  local midnight is outside the loaded rates; the collector prices each day's standing charge at its
  first half-hour of data instead.
* Requires VictoriaMetrics de-duplication - see [victoriametrics.md](victoriametrics.md).
