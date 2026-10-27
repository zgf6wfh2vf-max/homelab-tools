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
was never calculated. The author's account showed this for 14 sessions in a row, despite complete
half-hourly data in the API, so it's worth raising with Octopus.

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

A result is reported once Octopus has finished calculating it, usually a day or two after the
session. While it is still "calculating", `last_result` says so and no notification is sent.

On the first run, existing results are recorded without notifying, so you don't get a backlog. The
example timer runs every 30 minutes. Sessions are usually announced the day before or that morning.

## Outputs

**Status** (`sessions.json`): `next` (the next announced event), `reward`, `joined`, `last_result`,
`missing_90d` (results with missing readings, out of all results in 90 days), `points` (Octopoints
balance), `checked`.
