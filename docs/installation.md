# Installation

These steps assume a Debian-based Linux host (e.g. Raspberry Pi OS) with systemd. Every tool is
optional - install only what you need.

## 1. Install the package

```sh
sudo python3 -m venv /opt/homelab-tools
sudo /opt/homelab-tools/bin/pip install "homelab-tools @ git+https://github.com/zgf6wfh2vf-max/homelab-tools"
# add the Pod Point extra if you use podpoint-monitor:
sudo /opt/homelab-tools/bin/pip install "homelab-tools[podpoint] @ git+https://github.com/zgf6wfh2vf-max/homelab-tools"
```

The command-line tools are then in `/opt/homelab-tools/bin/` (the example unit files expect that path).

## 2. Service user and directories

```sh
sudo useradd --system --no-create-home --shell /usr/sbin/nologin homelab
sudo install -d -m 750 -o root -g homelab /etc/homelab-tools        # settings and secrets
sudo install -d -m 775 -o root -g homelab /var/lib/homelab-status   # status JSON for dashboards
```

Each service keeps its own state in `/var/lib/<service>` (created by systemd's `StateDirectory=`).

## 3. Settings

Copy the matching file from [`examples/env/`](../examples/env) to `/etc/homelab-tools/<tool>.env`,
edit it, and make it readable only by the service:

```sh
sudo install -m 640 -o root -g homelab examples/env/solis-monitor.env.example /etc/homelab-tools/solis-monitor.env
sudoedit /etc/homelab-tools/solis-monitor.env
```

Three settings are shared by every tool; each may be left empty to disable that output:

| Setting | Meaning |
|---|---|
| `NTFY_URL` | Full ntfy topic URL, e.g. `https://ntfy.sh/<long-random-topic>`. Anyone who knows the topic can read your alerts and send you fake ones - pick a long random name, or self-host ntfy with access control. |
| `VM_URL` | VictoriaMetrics base URL, e.g. `http://127.0.0.1:8428`. |
| `STATUS_DIR` | Directory for status JSON, e.g. `/var/lib/homelab-status`. |

Secrets (API keys, passwords, tokens) go in **separate files** referenced from the settings, owned
by root, group `homelab`, mode 640 - never in the unit files.

## 4. systemd units

```sh
sudo cp examples/systemd/<tool>.service /etc/systemd/system/          # long-running monitors
sudo cp examples/systemd/<tool>.{service,timer} /etc/systemd/system/  # octopus-collector, pihole-threat-alert
sudo systemctl daemon-reload
sudo systemctl enable --now <tool>            # or <tool>.timer for the timer-driven tools
journalctl -u <tool> -f                       # watch it start
```

The units run as the unprivileged `homelab` user with a read-only view of the system
(`ProtectSystem=strict`), writing only to their state directory and the status directory.

## 5. VictoriaMetrics and Grafana (optional)

A minimal Docker Compose setup:

```yaml
services:
  victoriametrics:
    image: victoriametrics/victoria-metrics:latest
    restart: unless-stopped
    command:
      - -storageDataPath=/storage
      - -retentionPeriod=10y
      - -dedup.minScrapeInterval=1ms   # needed: some tools re-send samples (see victoriametrics.md)
    ports: ["127.0.0.1:8428:8428"]
    volumes: ["./vm-data:/storage"]
  grafana:
    image: grafana/grafana-oss:latest
    restart: unless-stopped
    ports: ["3002:3000"]
    volumes:
      - ./grafana-data:/var/lib/grafana
      - ./provisioning:/etc/grafana/provisioning:ro   # put examples/grafana/datasource.yaml in datasources/
```

See [victoriametrics.md](victoriametrics.md) and [dashboards.md](dashboards.md).

## 6. Status JSON for a dashboard (optional)

[`examples/homepage/`](../examples/homepage) shows Homepage plus a read-only busybox web server
that publishes `/var/lib/homelab-status` to Homepage's `customapi` widgets.

## Updating

```sh
sudo /opt/homelab-tools/bin/pip install --upgrade "homelab-tools @ git+https://github.com/zgf6wfh2vf-max/homelab-tools"
sudo systemctl restart solis-monitor ad5x-monitor tado-monitor podpoint-monitor opencanary-ntfy
```
