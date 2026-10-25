# Changelog

## 0.2.0

* New `solar-forecast`: Forecast.Solar estimate for today and tomorrow, status file and a daily
  forecast curve for comparing with actual generation.
* New `weekly-report`: Monday-to-Sunday energy summary (bill, solar, battery, car, off-peak share,
  gas, heating) with week-on-week changes, sent via ntfy.

## 0.1.1

* `podpoint-monitor`: reuse one Pod Point sign-in (also across restarts) instead of creating a new
  session at every hourly token refresh, which made Pod Point send a "new sign-in" email each time.

## 0.1.0 - initial release

* `octopus-collector` / `octopus-setup`: half-hourly Octopus Energy usage priced at the exact tariff
  rate, standing charges, off-peak split, solar-saving estimate, 30-day chunked fetching.
* `solis-monitor`: local Modbus TCP monitor for Solis hybrid inverters with fault alerts.
* `ad5x-monitor`: FlashForge AD5X job tracking with "offline mid-print" detection.
* `tado-monitor` / `tado-auth`: device-flow login with rotating tokens, battery and offline alerts.
* `podpoint-monitor`: real charger state and session tracking (unofficial API).
* `pihole-threat-alert`: alerts on lookups of threat-list domains.
* `opencanary-ntfy`: rate-limited honeypot alerts.
* `sqlite-snapshot-backup`: hard-link snapshots with consistent SQLite copies.
* Examples: systemd units, settings files, Grafana dashboards, Homepage layout, keepalived failover.
