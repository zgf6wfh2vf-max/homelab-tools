# solar-health

A daily check, run after sunset, of the **solar strings**, **generation against the forecast** and
**battery health**. It uses only metrics already recorded by [`solis-monitor`](solis.md) and
[`solar-forecast`](solar-forecast.md).

| Check | Alert when | Needs |
|---|---|---|
| Dead string | A string makes under 5 % of the two strings' energy on a productive day (≥ 3 kWh) | 1 day |
| String balance | The ratio of string 1 to string 2 energy moves more than `STRING_TOLERANCE` from its usual value on **two productive days running** | 1 week |
| Generation | The last 7 days' actual/forecast ratio falls below `FORECAST_DROP` × the previous 28 days' ratio | 3 weeks |
| Battery health | State of health below `SOH_WARN_PCT`, or down `SOH_DROP_PTS` points within 30 days | 1 day |

Each problem is reported once, with a low-priority "OK again" when it clears.

## Why these comparisons

* **Strings against each other.** Two strings on the same roof see the same weather, so their
  *ratio* stays steady from day to day even when the total doesn't. It's rarely exactly 1. The
  author's strings split about 44/56 because one is shaded late in the afternoon. So each day is
  compared with the usual ratio of the last two weeks, not with 50/50. A 20 % change is about two
  dead panels in a string of eight. Requiring two days running ignores a one-off, like a bird on a
  panel or a passing shadow from scaffolding.
* **A week against the forecast.** Single-day forecasts are often 20-30 % out, but over a week the
  ratio of actual to forecast is steady. A sustained drop points to soiling, a fault or a tripped
  isolator.
* **Battery health** normally falls a point or two a year. Warranties typically guarantee 70-80 %
  after 10 years. A fast drop is worth reporting while the battery is under warranty.

## Settings

| Setting | Default | Meaning |
|---|---|---|
| `VM_URL` | required | VictoriaMetrics |
| `BATTERY_KWH` | 0 | Capacity, for counting cycles (0 leaves cycles out) |
| `STRING_TOLERANCE` | 0.2 | Allowed relative change in the string ratio |
| `FORECAST_DROP` | 0.6 | Alert when the week's ratio is below this × usual |
| `SOH_WARN_PCT` | 80 | Battery health alert level |
| `SOH_DROP_PTS` | 3 | Battery health drop within 30 days that alerts |
| `SOLAR_HEALTH_STATE_FILE` | `/var/lib/solar-health/state.json` | Problems already reported |
| `NTFY_URL`, `STATUS_DIR` | - | Outputs (see [installation](installation.md)) |

The example dashboard ([`examples/grafana/solar.json`](../examples/grafana/solar.json)) has matching
panels: power per string, battery health over a year, and battery cycles over a year.

## Outputs

**Status** (`solar_health.json`): `strings` (today's split), `strings_usual`, `week_vs_forecast`,
`battery_health`, `cycles` (equivalent full cycles since monitoring began), `checked`.
