# ad5x-monitor

Watches a **FlashForge AD5X** (likely other FlashForge models with the same legacy protocol) over
TCP port 8899. Read-only: it sends query commands only, and releases control after each poll, so
slicers and the touch screen keep working mid-print.

## Designed for a printer that is switched off when idle

| Situation | Result |
|---|---|
| Off while idle | Status "Off" - **no alert** |
| Job starts | "Print started" |
| Job finishes | "Print finished", with duration when the start was seen |
| Job paused (e.g. filament runout) | "Print paused" (high priority) |
| Job stops early | "Print stopped" |
| Printer disappears **mid-print** (3 failed polls in a row) | "Printer went offline mid-print" (high priority) - power cut, tripped plug |

The finish estimate extrapolates from progress: with a known start, `elapsed / progress − elapsed`;
when the monitor joins a print already running, from the rate of progress since joining. Progress is
reported by bytes of the file processed, so the estimate is approximate.

## Settings

| Setting | Default | Meaning |
|---|---|---|
| `AD5X_HOST` | required | Printer IP (reserve it in DHCP) |
| `AD5X_PORT` | 8899 | Legacy TCP port |
| `AD5X_STATE_FILE` | `/var/lib/ad5x-monitor/state.json` | Current job + 30-day history (survives restarts) |

## Outputs

**Metrics** (`printer_*`): `online`, `printing`, and while online `progress`, `nozzle_c`,
`nozzle_target_c`, `bed_c`, `bed_target_c`. Gaps while the printer is off are expected.

**Status** (`printer.json`): `state`, `progress`, `job`, `elapsed`, `remaining`, `eta`, `nozzle`, `bed`,
`light`, `prints_7d`, `hours_7d`, `last_print`, `last_when`.

## Protocol

See [`protocol.py`](../src/homelab_tools/ad5x/protocol.py): `~M601 S1` (take control), `~M119`
(status), `~M27` (progress), `~M105` (temperatures), `~M602` (release control).
