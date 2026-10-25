# tado-monitor and tado-auth

Reads **tado°** heating - every zone's temperature, target, humidity, heating demand and open-window
state, plus tado's outside weather - and alerts when a thermostat or radiator valve has a **low
battery** or goes **offline**. Read-only (GET requests only).

## Login (once)

tado's API uses the OAuth device flow since 2025:

```sh
sudo -u homelab TADO_TOKEN_FILE=/var/lib/tado-monitor/token.json /opt/homelab-tools/bin/tado-auth
```

It prints a URL. **Open it within five minutes**, log in, check the code matches, and approve. The
refresh token is saved (mode 600). tado *rotates* refresh tokens on every use; `tado-monitor` saves
each new one immediately. If the token is ever lost or rejected, the log says so - run `tado-auth`
again.

## API limits

tado allows **100 requests a day** without an Auto-Assist subscription and **20,000** with one. Each
round costs two requests plus one every `TADO_DEVICE_EVERY` rounds:

| Account | Suggested `TADO_INTERVAL` | Requests/day |
|---|---|---|
| Free | 1800 (30 min) | ~100 |
| Auto-Assist | 120 (2 min) | ~1,500 |

On HTTP 429 (rate limited) the monitor pauses for 30 minutes.

## Settings

| Setting | Default | Meaning |
|---|---|---|
| `TADO_TOKEN_FILE` | `/var/lib/tado-monitor/token.json` | Refresh token from `tado-auth` |
| `TADO_STATE_FILE` | `/var/lib/tado-monitor/state.json` | Problems already alerted |
| `TADO_INTERVAL` | 120 | Seconds between rounds |
| `TADO_DEVICE_EVERY` | 15 | Device-health check every N rounds |

## Outputs

**Alerts:** "tado radiator valve battery low", "tado room thermostat offline", ... once per device and
problem; they re-arm when the problem clears (e.g. after new batteries).

**Metrics:** `tado_inside_c`, `tado_target_c`, `tado_humidity_pct`, `tado_heating_pct`,
`tado_open_window` - each with a `zone` label - and `tado_weather_outside_c`, `tado_weather_solar_pct`.

**Status** (`tado.json`): per zone `<zone>_temp`, `<zone>_target`, `<zone>_hum`, `<zone>_heat`,
`<zone>_mode`, where `<zone>` is the zone name's first word in lower case (`Living Room` → `living`);
plus `outside`, `weather`, `devices`, `batteries`, `offline`.

The example `heating` Grafana dashboard puts daily gas use (from `octopus-collector`) next to
average heating demand and outside temperature.
