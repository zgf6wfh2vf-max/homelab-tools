# host-health

Every five minutes, a check that **the server itself is healthy**: heat, power, failed services,
memory, disk space, SD-card wear and pending reboots. Each problem has to last for its delay before
you're told (a moment of heat or a service restarting during an upgrade isn't worth a
notification). Each problem is reported once, with a low-priority "OK again" when it clears.

| Check | Problem when | Delay | Priority |
|---|---|---|---|
| Temperature | CPU at or above `TEMP_WARN_C` (clears 5 °C below, so it doesn't flap) | 10 min | high |
| Under-voltage | The power supply is sagging right now (Raspberry Pi) | at once | high |
| Throttled | The CPU is being slowed to protect it (Raspberry Pi) | 10 min | high |
| Power blip | Under-voltage or throttling happened between two checks | at once | low |
| Failed units | Any systemd unit in the failed state, one alert per unit | 10 min | high |
| Memory | Less than `MEM_WARN_PCT` % available | 15 min | high |
| Disks | Any path in `DISK_PATHS` at or above `DISK_WARN_PCT` % full, or missing | at once | high |
| SD writes | More than `WRITE_WARN_GB` GB written to `WRITE_DEVICE` in 24 h | at once | high |
| Reboot | A newer kernel is installed than the one running (or `/run/reboot-required` exists) | 2 days | low |

The **failed units** check is the broad one: it covers every service and timer on the machine,
including the other tools in this project, so a crashed monitor doesn't go unnoticed.

**SD writes:** SD cards wear out with writes. A Raspberry Pi with chatty logs or databases can
quietly write tens of GB a day. The author's server dropped from 14 to about 8 GB a day after
moving a database and slowing a scanner.

**Reboot:** Raspberry Pi OS and Debian don't create `/run/reboot-required`, so the tool also
compares the running kernel with the newest installed kernel of the same flavour. The Pi 5's
`-2712` and the Pi 4's `-v8` are separate flavours.

## Settings

| Setting | Default | Meaning |
|---|---|---|
| `TEMP_WARN_C` | 80 | CPU temperature alert (a Pi 5 starts throttling at 85 °C) |
| `MEM_WARN_PCT` | 10 | Available-memory alert |
| `DISK_PATHS` | `/` | Space-separated mount points to watch |
| `DISK_WARN_PCT` | 90 | Disk-full alert |
| `WRITE_DEVICE` | `mmcblk0` | Block device for the write check (the SD card on a Pi) |
| `WRITE_WARN_GB` | 20 | Daily writes alert |
| `HEALTH_STATE_FILE` | `/var/lib/host-health/state.json` | Open problems, write samples |
| `NTFY_URL`, `VM_URL`, `STATUS_DIR` | - | Outputs (see [installation](installation.md)) |

It needs no root access. The Raspberry Pi checks call `vcgencmd`, which needs the `video` group
(`SupplementaryGroups=video` in the example unit), and are skipped on other machines.

## Outputs

**Metrics** (label `host`, the machine's hostname): `host_cpu_temp_c`, `host_fan_rpm` (the
first fan the kernel reports, such as a Pi 5 active cooler), `host_throttled` and `host_undervolt`
(happening now; Raspberry Pi only), `host_mem_available_pct`, `host_writes_gb_24h`,
`host_failed_units` and `host_problems`. The [`server.json`](dashboards.md) Grafana dashboard graphs them.

**Status** (`host.json`): `cpu_temp`, `power`, `failed_units`, `memory`, `writes_24h`, `reboot`,
`problems` (how many checks currently have a problem), `checked`.
