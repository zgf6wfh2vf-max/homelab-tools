# Changelog

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
