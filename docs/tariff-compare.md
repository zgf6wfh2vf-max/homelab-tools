# tariff-compare

Once a month: **what would last month have cost on other Octopus tariffs?** It reprices your actual
half-hourly import and export, and your gas, at the public prices of other Octopus tariffs for your
region, then compares the result with what you really paid (as recorded by
[`octopus-collector`](octopus.md)).

```
Tariff check Sep 2026: your tariff is still the best fit
Electricity minus export, your real use over 30 days: you paid -£3.98.
Go: £11.37, £15.35 more (needs an EV)
Go 12M Fixed: £17.01, £20.99 more (needs an EV, at today's prices)
Flux: £34.41, £38.39 more (needs solar and a battery)
Agile: £73.89, £77.87 more
Gas: you paid £18.42; Flexible Octopus would be £18.73 (£0.31 more).
Your fixed deals end: electricity 11 Apr 2027, gas 22 Apr 2027.
```

When a fixed deal ends within 45 days, the title says so and names the best option at the time.

## How it compares fairly

* **Your real half-hours.** Daytime and evening use and export are priced at the moment they
  happened, so a tariff's expensive 16:00-19:00 peak hits exactly the use you had then.
* **Flexible energy moves.** Energy you imported in your current tariff's cheap slots (below
  `OFFPEAK_BELOW_P`) is battery and EV charging. You'd retime that on another tariff, so it's moved,
  night by night, into the candidate tariff's cheapest half-hours between 21:00 and 08:00. The
  largest half-hours go into the cheapest slots.
* **New tariffs at today's prices.** If a tariff (or part of it, such as its standing charge)
  didn't exist for the whole month, its current prices are applied by time of day, and the result
  says "at today's prices". That's also what you'd actually pay if you switched now.
* **Export as it happened.** On Flux or Agile Outgoing, a battery set to discharge at the export
  peak could earn more. That isn't modelled, so those figures are on the cautious side.

## Tariffs compared

Electricity (import with export): Go, Go 12M Fixed, Intelligent Go, Agile (with Outgoing or Agile
Outgoing), Flux (import and export), Flexible and 12M Fixed. Gas: Flexible and 12M Fixed. Tariffs
are looked up by their **display name** in Octopus's public product list, using the newest version
of each, so the defaults keep working when Octopus re-issues a product.

Products that aren't published as a single-rate tariff can't be priced from public data, and are
listed as "not priceable". At the time of writing that includes Intelligent Octopus Go's four-rate
EV tariff.

Eligibility notes are shown but not checked. For example, Go needs an EV, and Flux needs solar and
a battery.

## Settings

| Setting | Default | Meaning |
|---|---|---|
| `OCTOPUS_CONFIG` | `/etc/octopus/config.json` | Output of `octopus-setup`: your region, and when your fixed deals end |
| `VM_URL` | required | VictoriaMetrics holding `octopus-collector`'s metrics |
| `OFFPEAK_BELOW_P` | 12 | Import below this rate (pence) counts as flexible |
| `NTFY_URL`, `STATUS_DIR` | - | Outputs (see [installation](installation.md)) |

Usage: `tariff-compare` (last calendar month), `--month 2026-09`, `--dry-run`. The example timer runs
on the 4th of each month, when Octopus has published the whole previous month.

No API key is needed: prices come from Octopus's public endpoints, and your usage from
VictoriaMetrics.

## Outputs

**Status** (`tariffs.json`): `period`, `paid` (electricity minus export), `best`, `best_cost`,
`versus` (best against paid), `gas_paid`, `gas_best`, `fix_ends` (your electricity fixed deal).
