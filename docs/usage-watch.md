# usage-watch

A daily check for **gas or electricity use that is out of line** with what's normal for your house.

```
Unusual gas use Thu 12 Nov
58 kWh used; about 31 kWh is normal at 6.5 °C outside (27 kWh more). Worth checking the heating
schedule, thermostat and boiler.
```

## Gas against the weather

Heating gas rises as it gets colder, so each day's gas is modelled as

    gas = a + b × max(0, base − mean outside temperature)

Here `a` is the weather-independent part (hot water, cooking) and `b` is the kWh per degree-day.
The model is fitted to the last year of complete days from [`octopus-collector`](octopus.md), and
the best `base` between 12 and 18 °C is chosen. Daily mean temperatures come from
[Open-Meteo](https://open-meteo.com), which is free and needs no account. On the author's house,
temperature explains 86 % of daily gas use, with a typical scatter of ±7 kWh.

A day is flagged only when **all three** thresholds are passed:

* more than `GAS_SIGMA` (3) standard deviations above the prediction. Over a year of the author's
  data this would never have fired by chance;
* at least `GAS_MIN_EXCESS_PCT` (25 %) above it;
* at least `GAS_MIN_EXCESS_KWH` (5 kWh) above it, so a steady summer hot-water day doesn't alert
  over a kWh or two.

Octopus data arrives a day or two late, so each new complete day is checked once. The first run
checks only the newest day, rather than replaying a year of history.

## Electricity always-on load

Each day's **baseload** is the average of its six quietest half-hours of house power: fridges,
routers, standby. It's an alert when the baseload is `BASELOAD_FACTOR` (1.5×) its usual level (the
median of the previous two weeks) **and** `BASELOAD_MIN_W` (100 W) higher, **on two days running**.
That usually means a heater, dehumidifier or failing appliance is staying on. You get one alert,
then a low-priority "back to normal" when it recovers. It needs 7 days of history before it starts.

## Settings

| Setting | Default | Meaning |
|---|---|---|
| `VM_URL` | required | VictoriaMetrics |
| `WEATHER_LAT`, `WEATHER_LON` | - | Location for temperatures; without them the gas check is skipped |
| `GAS_METRIC` | `octopus_gas_kwh` | Half-hourly gas kWh |
| `LOAD_METRIC` | `solis_load_w` | House power (W) |
| `GAS_SIGMA` | 3 | Gas threshold in standard deviations |
| `GAS_MIN_EXCESS_PCT` | 25 | Gas threshold as % above the prediction |
| `GAS_MIN_EXCESS_KWH` | 5 | Gas threshold in kWh above the prediction |
| `BASELOAD_FACTOR` | 1.5 | Baseload threshold as a multiple of usual |
| `BASELOAD_MIN_W` | 100 | Baseload threshold in W above usual |
| `USAGE_STATE_FILE` | `/var/lib/usage-watch/state.json` | Last gas day checked; whether a baseload alert is open |
| `NTFY_URL`, `STATUS_DIR` | - | Outputs (see [installation](installation.md)) |

Usage: `usage-watch` (meant for a daily timer after `octopus-collector` has run), `--dry-run`.

## Outputs

**Status** (`usage.json`): `gas_day`, `gas_used`, `gas_expected`, `gas_outside` (the newest gas day
checked), `base_now`, `base_usual` (baseload in W) and `checked`.
