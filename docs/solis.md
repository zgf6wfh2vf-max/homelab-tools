# solis-monitor

Reads a **Solis hybrid inverter** (solar + battery) every minute over **local Modbus TCP** through its
Wi-Fi/LAN data logger. No cloud account, and read-only: the client cannot write registers.

## Requirements

* A Solis single-phase hybrid (RHI / S5 / S6 hybrid family) whose data logger answers Modbus TCP on
  port 502 - recent S3-WIFI-ST loggers do. Find yours: look for a device talking to
  `*.soliscloud.com` / Alibaba Cloud IoT, then check `nc -zv <ip> 502`.
* Older Solarman-based sticks (port 8899, "Solarman V5" protocol) are **not** supported.
* Give the logger a fixed address (DHCP reservation).

## Settings

| Setting | Default | Meaning |
|---|---|---|
| `SOLIS_HOST` | required | Data logger IP address |
| `SOLIS_PORT` | 502 | Modbus TCP port |
| `SOLIS_UNIT` | 1 | Modbus unit (slave) id |
| `SOLIS_INTERVAL` | 60 | Seconds between polls |
| `KUMA_PUSH_URL` | - | Optional Uptime Kuma push monitor URL |

Plus the shared `NTFY_URL`, `VM_URL`, `STATUS_DIR` ([installation](installation.md#3-settings)).

## Outputs

**Alerts:** "Inverter fault" when fault codes appear (high priority) and "Inverter fault cleared".

**Uptime Kuma:** a push every poll - `up` normally, `down` while a fault is active. Create a *Push*
monitor with a heartbeat interval of ~180 s: if the logger drops off Wi-Fi, the pushes stop and Kuma
alerts.

**Metrics** (`solis_*`): `pv_w`, `load_w`, `soc`, `soh`, `batt_w` (+ charging / − discharging),
`grid_w` (+ export / − import), `pv_today_kwh`, `pv_total_kwh`, `load_today_kwh`,
`batt_in_today_kwh`, `batt_out_today_kwh`, `grid_in_today_kwh`, `grid_out_today_kwh`,
`inv_temp_c`, `grid_v`, `dc1_v`, `dc1_a`, `dc2_v`, `dc2_a`.

**Status** (`solis.json`): `pv_w`, `pv_today`, `pv_month`, `pv_total`, `batt_soc`, `batt_soh`,
`batt_now`, `batt_in_today`, `batt_out_today`, `grid_dir`, `grid_power`, `grid_in_today`,
`grid_out_today`, `grid_v`, `load_w`, `load_today`, `inv_temp`, `faults`.

## Register map

The decoded registers and their scaling are documented in
[`registers.py`](../src/homelab_tools/solis/registers.py). They were verified against a live
inverter; the meter-power sign was checked by comparing inverter AC output minus house load with
the meter reading. If your model differs, adjust `decode()` - the unit tests show the expected shape.

## Troubleshooting

* `poll failed: timed out` - the logger is offline or another client holds its single Modbus
  connection. Poll no more often than every 30 s; loggers also upload to the cloud.
* All values zero at night - normal for PV registers; battery and grid values keep updating.
