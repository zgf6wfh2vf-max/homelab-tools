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

### Chart: did a heating change help?

Each run also writes the last `GAS_CHART_DAYS` (60) days to VictoriaMetrics as `usage_gas_kwh`,
`usage_gas_expected_kwh` and `usage_gas_outside_c`, timestamped at local midnight. Each day's
expected figure comes from a model fitted only to the days *before* it. A lasting change to the
heating (a lower boiler flow temperature, new insulation, a different schedule) therefore shows as a
gap between the bars and the line, rather than being absorbed by the fit. The
[Heating dashboard](dashboards.md) charts used against expected, the daily difference in kWh, and the
past week overall. Mark the day of a change with a Grafana annotation (Ctrl+click on the chart).

## Heating warm-up

Gas use can look normal while the house takes longer and longer to get warm, for example when the
boiler's flow temperature or weather-compensation curve is too low for colder weather. So each time
a [tado](tado.md) zone's target is raised by at least 1 °C (a heating schedule block starting) while
the room is at least 1 °C below the new target, usage-watch measures how long the room takes to get
within 0.1 °C of it.

```
Slow to warm up: Upstairs
Upstairs took 4h 20m to reach 21.5 °C from 20.0 °C after Fri 06:00 (usually 2h 30m), with 1.5 °C
outside. If this keeps happening as it gets colder, the boiler's flow temperature or weather curve
may be too low, or a radiator valve may be stuck.
```

A warm-up is reported when it takes `WARMUP_FACTOR` (1.5×) the zone's usual time (the median of
its warm-ups over the previous two weeks, once there are at least 5) **and** `WARMUP_MIN_EXTRA_MIN`
(45) minutes longer, or when the room never reaches the target before the target changes again.
Each warm-up is checked once. The first run only learns from history.

Every warm-up of the last 35 days is written to VictoriaMetrics with the zone as a label:
`heating_warmup_min` (minutes; missing when the target wasn't reached), `heating_warmup_reached`
(1/0), `heating_warmup_gap_c` (degrees to climb) and `heating_warmup_outside_c`. The
[Heating dashboard](dashboards.md) charts the minutes against the outside temperature.

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
| `GAS_CHART_DAYS` | 60 | Days of used/expected gas written for the Grafana chart |
| `WARMUP_CHECK` | 1 | `0` turns the heating warm-up check off |
| `WARMUP_FACTOR` | 1.5 | Warm-up threshold as a multiple of usual |
| `WARMUP_MIN_EXTRA_MIN` | 45 | Warm-up threshold in minutes over usual |
| `USAGE_STATE_FILE` | `/var/lib/usage-watch/state.json` | Last gas day and warm-up checked; whether a baseload alert is open |
| `NTFY_URL`, `STATUS_DIR` | - | Outputs (see [installation](installation.md)) |

Usage: `usage-watch` (meant for a daily timer after `octopus-collector` has run), `--dry-run`.

## Outputs

**Status** (`usage.json`): `gas_day`, `gas_used`, `gas_expected`, `gas_outside` (the newest gas day
checked), `base_now`, `base_usual` (baseload in W; until there are 9 complete days of
history, "learning" and the date it starts, e.g. "from Wed 4 Nov"), `warmup_when` (when the latest
warm-up started, e.g. "Thu 06:00"), per zone `warmup_<zone>` (its latest warm-up and usual, e.g.
"2h 56m (usual 2h 44m)", "not reached (usual 2h 44m)" or "1h 50m (usual learning)"; `<zone>` is the
zone name's first word in lower case, as in [tado](tado.md)) and `checked`.
