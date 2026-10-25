# podpoint-monitor

Watches a **Pod Point** home EV charger (tested with a Solo 3) through Pod Point's mobile-app API,
using the [`podpointclient`](https://pypi.org/project/podpointclient/) library. **Read-only**: only
the library's read calls are used.

> Pod Point has no public API. This may stop working if Pod Point changes its app API. When it
> fails, the status file's `updated` time stops moving; no false alerts are sent.

Pod Point chargers have no local API (the local web page is only Wi-Fi provisioning), so this needs
your Pod Point app email and password.

## The real charger state

The pod's own status reads **Charging** as soon as a car is plugged in, even while the charger is
holding off for its schedule. The monitor reads the connector's charging state instead:

| Shown | Meaning |
|---|---|
| Charging | Energy flowing |
| Waiting for 00:30 | Plugged in; the schedule (e.g. a cheap night tariff) starts at 00:30 |
| Plugged in, paused | Charger withholding energy for another reason (smart mode) |
| Plugged in (car not taking charge) | Car full, or the car's own timer |
| Not plugged in | Available |
| Charger offline | Lost its connection to Pod Point |

## Setup

```sh
printf '%s\n%s\n' 'you@example.com' 'app-password' \
  | sudo install -m 640 -o root -g homelab /dev/stdin /etc/homelab-tools/podpoint-credentials
```

On the first run the whole charging history is written to VictoriaMetrics (nothing is announced).

## Sign-in emails

Left alone, `podpointclient` creates a new Pod Point session - a fresh sign-in with your password -
every time it renews its one-hour access token, and Pod Point emails you about each one (~24 a
day). The monitor prevents that: it keeps the first session and the refresh token in
`PODPOINT_LOGIN_FILE` (mode 600) and reuses them, including across restarts. You get one sign-in
email when the file is first created, and another only if Pod Point rejects the saved sign-in.

## Settings

| Setting | Default | Meaning |
|---|---|---|
| `PODPOINT_CREDENTIALS_FILE` | required | Email on line 1, password on line 2 |
| `PODPOINT_STATE_FILE` | `/var/lib/podpoint-monitor/state.json` | Last session id seen |
| `PODPOINT_INTERVAL` | 180 | Seconds between polls |
| `PODPOINT_LOGIN_FILE` | `login.json` next to the state file | Saved sign-in (refresh token, user id) |

## Outputs

**Alerts:** "Car charging started" and "Car charge complete" (kWh, charging time, plug-in/out times,
cost).

**Metrics:** `ev_charging`, `ev_plugged`, `ev_online` every poll; `ev_session_kwh`,
`ev_session_cost_p`, `ev_session_charging_s` once per finished session, timestamped at its end.

**Status** (`ev.json`): `state`, `online`, `signal`, `last_kwh`, `last_cost`, `last_when`, `last_time`,
`month_kwh`, `month_cost`, `month_n`, `updated`.

**Cost** is Pod Point's own estimate at the electricity rate set in the app - set your off-peak rate
there for realistic figures. Live charging power is not available from this API.
