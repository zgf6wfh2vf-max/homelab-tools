# solar-forecast

Today's and tomorrow's expected solar generation from [Forecast.Solar](https://forecast.solar),
for a dashboard and for comparing forecast with actual generation in Grafana. Handy on a
time-of-use tariff: a dull day tomorrow is a reason to charge the battery from the grid overnight.

The free API needs no account and allows 12 requests an hour per IP address; this tool makes one
request per roof plane per run, so an hourly timer is comfortable.

## Settings

| Setting | Default | Meaning |
|---|---|---|
| `SOLAR_LAT`, `SOLAR_LON` | required | Location in decimal degrees - a nearby town is fine |
| `SOLAR_PLANES` | required | `tilt,azimuth,kWp` per roof plane, `;`-separated. Tilt 0 = flat, 90 = vertical. Azimuth 0 = south, -90 = east, 90 = west, 180 = north. E.g. `35,0,6` or `35,-90,3;35,90,3` |
| `ACTUAL_METRIC` | - | Today's generation in kWh, e.g. `solis_pv_today_kwh`, shown next to the forecast |
| `FORECAST_STATE_FILE` | `/var/lib/solar-forecast/state.json` | Which day's curve has been stored |
| `FORECAST_SOLAR_API_KEY` | - | Optional paid-plan key |

Not sure of your tilt or kWp? Start with a typical pitched roof (35°) and compare the forecast with
actual generation on a few clear days; adjust kWp if it is consistently high or low.

## Outputs

**Status** (`forecast.json`, refreshed every run): `today_kwh`, `today_actual`, `tomorrow_kwh`,
`tomorrow_peak`, `updated`.

**Metrics:** `forecast_solar_w` (expected power through the day) and `forecast_solar_day_kwh`
(the day's expected total, at local midnight).

## Why the curve is stored once a day

VictoriaMetrics de-duplicates samples with identical timestamps by keeping the **largest** value. If
the hourly runs rewrote a revised forecast, graphs would show the most optimistic version rather
than the latest. So the curve is stored once, by the first run of the day, and the hourly runs only
refresh the status file. The stored curve is "this morning's forecast" - the fair one to compare
with what the panels actually produced.
