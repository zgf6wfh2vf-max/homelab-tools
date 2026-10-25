# weekly-report

A Monday-to-Sunday summary sent to your phone, built from the metrics the other tools record:

```
Week 19-25 Oct: £17.63
Bill £17.63 (electric £12.50, gas £7.33, export -£2.20) - £2.10 less than the week before.
Solar 38 kWh, battery supplied 42 kWh.
Car 61 kWh, £3.40 (3 charges).
92% of grid import off-peak; gas 160 kWh (20 kWh more than the week before); heating demand 12%, outside 9.8°C on average.
```

Each line appears only when its data exists:

| Line | Needs |
|---|---|
| Bill | `octopus-collector` (electric and gas include standing charges) |
| Solar, battery | `solis-monitor` |
| Car | `podpoint-monitor` |
| Off-peak, gas, heating | `octopus-collector`, `tado-monitor` |

## When to run it

Octopus publishes smart-meter data one to two days late, so the example timer runs on **Tuesday at
08:00** for the week that ended on Sunday. If some days are still missing, the report says
"Costs cover N of 7 days so far". Weeks follow local time, including the 167- or 169-hour weeks
when the clocks change.

## Settings and usage

`VM_URL` (required), `NTFY_URL`, `STATUS_DIR` (writes `weekly.json`).

```sh
weekly-report                          # last complete week, sent to ntfy
weekly-report --dry-run                # print it instead
weekly-report --week-ending 2026-10-25 # a specific week (ending on that Sunday)
```

**Status** (`weekly.json`): `week`, `net`, `electric`, `gas`, `export`, `solar`, `car`, `offpeak`,
`gas_kwh`, and changes against the previous week: `net_change`, `solar_change`, `car_change`,
`gas_change` (e.g. `▼ £2.10`).
