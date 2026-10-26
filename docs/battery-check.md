# battery-check

Two daily checks for a home battery that charges on a cheap overnight rate (Octopus Go, Intelligent
Go, or any tariff with a cheap window) and runs the house for the rest of the day:

| Mode | When (example timers) | Alerts when |
|---|---|---|
| `morning` | 06:15, after the cheap window | The battery never reached `CHARGED_BELOW_PCT` overnight (high priority) |
| `evening` | 20:00, for tomorrow | Tomorrow the house would buy at least `SHORTFALL_KWH` at the peak rate |

```
Battery will run low Tuesday
2.0 kWh of solar forecast, against about 15 kWh of house use. The battery will probably run out
around 18:30, leaving 4.5 kWh to buy at the peak rate (about £1.20). Heavy appliances are cheapest
after 00:30.
```

Read-only: it never changes the inverter's settings.

## Why it doesn't tell you to charge less on sunny days

It's tempting to leave room in the battery for tomorrow's solar. But if your **export rate is higher
than your cheap import rate** (including about 10 % round-trip losses), that loses money. Every kWh
of solar you store instead of exporting gives up the export rate, to save a kWh you could have bought
overnight for less. A full overnight charge is then the right default.

So the useful questions are whether the charge actually happened (`morning`), and whether a full
battery will be enough tomorrow (`evening`). If your export rate is *below* your cheap rate, the
trade-off reverses, and this tool doesn't model that.

## How the evening outlook works

Tomorrow's **peak period** runs from the end of the cheap window until it opens again (05:30 to
00:30 the next night on Octopus Go). It's simulated in half-hour steps:

* **House use:** for each half-hour of the day, the median of `avg_over_time(LOAD_METRIC[30m])`
  over the last `PROFILE_DAYS` days. The median keeps an occasional heavy day (e.g. daytime car
  charging) from skewing the profile.
* **Solar:** the forecast curve that [`solar-forecast`](solar-forecast.md) saves
  (`FORECAST_CURVE_FILE`), linearly interpolated and integrated over each half-hour.
* **Battery:** it starts at `BATTERY_FULL_PCT`. Surplus solar charges it up to 100 % (anything
  beyond that is exported). A deficit is taken from it down to `BATTERY_RESERVE_PCT`, and the rest
  comes from the grid. `BATTERY_EFFICIENCY` applies both ways.

The **peak rate** in the message is the highest `PEAK_RATE_METRIC` value of the last 7 days (from
[`octopus-collector`](octopus.md)). Without that metric, the cost is left out.

It's an estimate: the solar forecast is often off by 20-30 % either way, and your house use varies
from day to day. Use it as a heads-up, not a bill.

## Settings

| Setting | Default | Meaning |
|---|---|---|
| `BATTERY_KWH` | required | Usable capacity as the inverter reports it, e.g. `10` |
| `BATTERY_FULL_PCT` | 100 | State of charge the overnight charge reaches |
| `BATTERY_RESERVE_PCT` | 20 | The inverter's discharge floor (Solis "overdischarge SOC") |
| `BATTERY_EFFICIENCY` | 0.95 | One-way efficiency (0.95 ≈ 90 % round trip) |
| `CHEAP_HOURS` | `00:30-05:30` | The cheap window, local time |
| `CHARGED_BELOW_PCT` | 90 | `morning`: alert if the overnight peak was below this |
| `SHORTFALL_KWH` | 1.0 | `evening`: alert if tomorrow needs at least this much from the grid at peak rate |
| `PROFILE_DAYS` | 7 | Days of house-use history for the profile |
| `LOAD_METRIC` | `solis_load_w` | House power (W) |
| `SOC_METRIC` | `solis_soc` | Battery state of charge (%) |
| `PEAK_RATE_METRIC` | `octopus_import_rate_p` | Import rate (pence) |
| `SOLAR_CURVE_FILE` | `/var/lib/solar-forecast/curve.json` | Written by `solar-forecast` |
| `VM_URL` | required | VictoriaMetrics |
| `NTFY_URL`, `STATUS_DIR` | - | Outputs (see [installation](installation.md)) |

Usage: `battery-check morning`, `battery-check evening [--day YYYY-MM-DD] [--dry-run]`.

## Outputs

**Status** (`battery.json`; each mode updates its own fields):

* `morning`: `overnight` (highest state of charge in the cheap window) and `checked`;
* `evening`: `outlook_day`, `outlook_solar`, `outlook_use`, `outlook_battery` (`lasts` or
  `runs out HH:MM`) and `outlook_grid` (kWh needed at the peak rate).

If there's no house-use history yet, or no forecast for tomorrow, the evening check logs why and
sends nothing.
