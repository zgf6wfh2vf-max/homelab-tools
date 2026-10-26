# powerline-monitor

Every five minutes, a check on **devolo Magic powerline adapters**: the speed of each link over the
mains wiring, whether every adapter answers, and when one restarts or updates its firmware. It only
reads the status file each adapter publishes for its own web page (`/assets/data.cfl`); it never
changes a setting and needs no devolo software or extra Python packages.

| Check | Problem when | Delay | Priority |
|---|---|---|---|
| Offline | An adapter in `PLC_HOSTS` does not answer | 10 min | high |
| Link lost | An adapter that was linked no longer receives from another | 10 min | high |
| Slow link | A link's rate is below `PLC_DROP_PCT` % of its 7-day median | 30 min | high |
| Restart | An adapter's uptime went backwards (power cut, update, unplugged) | at once | low |
| Firmware | An adapter's firmware version changed | at once | low |

Each problem is reported once, with a low-priority "OK again" when it clears. The slow-link check
starts after half a day of history (it says "learning" until then).

**Why links slow down:** powerline speed depends on the wiring between the two sockets and on
electrical noise. New chargers, LED dimmers, some appliances and extension leads with surge
protection are common causes. A sudden drop that lasts usually means something new was plugged in
near one of the adapters.

**Speeds:** each adapter reports the rate at which it *receives* from every other adapter, so each
direction of a link is measured at its far end. The raw figure is in an undocumented unit.
`PLC_RATE_SCALE` converts it to the Mbps that the devolo Home Network app shows. The default
(0.0533) was measured on a pair of Magic 2 LAN 1-1 adapters: the app showed 1315 Mbps for a raw
24657. To calibrate yours, divide the app's figure by `ACTUAL_RX_BPS` in the data file. These are
link rates (what the wiring can carry), not traffic, so they don't rise and fall with use.

**Lost MAPs** is a running count of scheduling messages from the master adapter that an adapter
missed. It's recorded but not alerted on. An adapter whose count climbs much faster than the others
is on a noisier socket.

**Security:** without a device password, the data file can be read by anything on your network and
includes the adapters' internal API tokens. The tool keeps only the fields it uses and never stores
or logs the rest. Setting a password in the adapters' web interface protects the settings. This
tool doesn't log in yet, so it would then need that support adding.

## Settings

| Setting | Default | Meaning |
|---|---|---|
| `PLC_HOSTS` | (required) | Space-separated adapter addresses (reserve them in your DHCP server) |
| `PLC_RATE_SCALE` | 0.0533 | App Mbps per raw rate unit (see above) |
| `PLC_DROP_PCT` | 50 | Slow-link alert, as a percentage of the usual rate |
| `PLC_STATE_FILE` | `/var/lib/powerline-monitor/state.json` | Names, uptimes, firmware, open problems, a week of rates |
| `NTFY_URL`, `VM_URL`, `STATUS_DIR` | - | Outputs (see [installation](installation.md)) |

## Outputs

**Metrics:** `powerline_rate_mbps{from,to}`, and per adapter `powerline_online`,
`powerline_uptime_s`, `powerline_cpu_pct` and `powerline_lost_maps` (label `adapter`, the name set
in the devolo app).

**Status** (`powerline.json`): `links` (the link directions, e.g. "Lounge → Office, Office →
Lounge"), `speed` (current rate of each, in the same order), `usual` (7-day median of each),
`adapters` ("2 of 2 online"), `uptime` (time since the most recent restart), `firmware`,
`lost_maps` (count per adapter), `problems` (how many checks currently have a problem), `checked`.
