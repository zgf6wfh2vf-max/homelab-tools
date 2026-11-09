# octopus-sessions

Alerts for Octopus Energy **demand-flexibility events**: Power Down and Power Up sessions (and, if
you want them, Weekend Happy Hours), plus free-electricity and regional Power-up campaigns. It also
reports the **result** of each event you joined, so you notice when sessions pay nothing.

```
Power Down today 17:00-18:00
Octopus pays 80 Octopoints (about 10p) per kWh you save below your usual level. You've joined it.
Keep the battery charged for that hour and avoid heavy appliances.
```
```
Power Down result
Power Down on Mon 19 Oct: no reward - Octopus says it is missing meter readings for that hour. If
this keeps happening, ask Octopus to check your smart meter data for saving sessions.
```

Read-only: it never joins events for you. You opt in from the Octopus app or the invitation email.

## Where the data comes from

Octopus's GraphQL API, authenticated with your account API key (exchanged for a short-lived token
with `obtainKrakenToken`):

| Source | Endpoint | Events |
|---|---|---|
| `savingSessions` | `api.backend.octopus.energy` | `TURN_DOWN` (Power Down), `TURN_UP` (Power Up), `WEEKEND_HAPPY_HOUR`, plus your joined events and results |
| `customerFlexibilityCampaignEvents` | `api.octopus.energy` | Campaigns by slug: `free_electricity`, `power_ups_ukpn`, `power_ups_nged`, `power_ups_npg`, `power_ups_scotland` |

What the author saw in October 2026 (region E):

* Power Downs ran about twice a week at 17:00-18:00 UTC, paying 64-82 Octopoints per kWh.
* The national `free_electricity` campaign's last event was in October 2025.
* Regional Power-ups (`power_ups_nged`) had no events after September 2025.

Octopus changes these schemes often, so the event types and campaigns are settings.

**Worth knowing with a battery:** Power Down rewards are measured against your *usual* import for
that hour. If a home battery already covers the evening peak, there's little import left to cut, so
the reward will be small whatever you do. And if every result says `MISSING_READINGS`, the reward
was never calculated. The author's account showed this for 16 sessions in a row, despite complete
half-hourly data in the API. The cause was the sign-up itself: Octopus had registered it against
the account's **export** MPAN, which has no import readings to judge a Power Down by. The tool now
checks this on every run and sends one high-priority alert if the sign-up is on the export meter
(or any meter other than your import one). The fix is to ask Octopus to move the sign-up to your
import MPAN. List export meters in the config with `"export": true` so the tool can tell them apart.

## Will the battery last?

With a home battery, a Power Down mostly comes down to whether the battery still has charge when the
session starts. If `BATTERY_KWH` and `VM_URL` are set, then for a Power Down you have joined that
starts within `SESSION_BATTERY_HOURS`, each run simulates the battery from its current charge to the
end of the session. It uses the same model as [battery-check](battery-check.md): typical house use
for each half-hour over the last week, plus the saved solar forecast. If the house would need more
than `SESSION_SHORTFALL_KWH` from the grid during the session, you get one warning per session:

```text
Battery may not last: Power Down today 17:00-18:00
The battery is at 45% and will probably run down to its reserve around 15:30, before it starts.
That leaves about 0.4 kWh to come from the grid during the session, which counts against your
saving. Put off heavy appliances (dryer, oven, washing, car) until after 18:00.
```

The check runs every 30 minutes, so it also catches an afternoon where something heavy (a tumble
dryer, say) drains the battery after an earlier check said it would last. It never changes anything
on the inverter.

## Settings

| Setting | Default | Meaning |
|---|---|---|
| `OCTOPUS_API_KEY_FILE` / `OCTOPUS_API_KEY` | required | Same key as `octopus-collector` |
| `OCTOPUS_CONFIG` | `/etc/octopus/config.json` | Output of `octopus-setup` (account number, import MPAN) |
| `SESSION_TYPES` | `TURN_DOWN TURN_UP` | Saving-session types to announce (add `WEEKEND_HAPPY_HOUR` if wanted) |
| `CAMPAIGNS` | `free_electricity` | Campaign slugs to check, e.g. `free_electricity power_ups_nged` |
| `OCTOPOINTS_PER_POUND` | 800 | For the approximate pence value in messages |
| `SESSIONS_STATE_FILE` | `/var/lib/octopus-sessions/state.json` | Events already announced, results already reported |
| `NTFY_URL`, `STATUS_DIR` | - | Outputs (see [installation](installation.md)) |
| `BATTERY_KWH`, `VM_URL` | unset | Both set: turn on the battery check |
| `SESSION_BATTERY_HOURS` | 8 | Check sessions that start within this many hours |
| `SESSION_SHORTFALL_KWH` | 0.2 | Warn when the session would need at least this much from the grid |
| `BATTERY_RESERVE_PCT`, `BATTERY_EFFICIENCY`, `SOLAR_CURVE_FILE`, `LOAD_METRIC`, `SOC_METRIC`, `PROFILE_DAYS` | as [battery-check](battery-check.md) | The battery model |

A result is reported once Octopus has finished calculating it, usually a day or two after the
session. While it is still "calculating", `last_result` says so and no notification is sent.

On the first run, existing results are recorded without notifying, so you don't get a backlog. The
example timer runs every 30 minutes. Sessions are usually announced the day before or that morning.

## Outputs

**Status** (`sessions.json`): `next` (the next announced event), `reward`, `joined`, `last_result`,
`missing_90d` (results with missing readings, out of all results in 90 days), `meter` (which meter
the sign-up is on: "import", or "EXPORT (wrong)" / "other (wrong)"), `points` (Octopoints
balance), `battery` (for the next joined Power Down within `SESSION_BATTERY_HOURS`: "lasts", or
"short 0.4 kWh" for the expected grid use during it; "-" when there is none or no battery check),
`checked`.
