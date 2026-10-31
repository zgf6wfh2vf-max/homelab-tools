# Dashboards

## Grafana

[`examples/grafana/`](../examples/grafana) contains provisioning-ready dashboards. They use a
Prometheus-type data source with uid `vm` pointing at VictoriaMetrics
([`datasource.yaml`](../examples/grafana/datasource.yaml)).

| Dashboard | Needs | Shows |
|---|---|---|
| `solar.json` | solis-monitor | Live power, power flows, battery charge, inverter temperature, grid voltage, daily energy |
| `costs.json` | octopus-collector (+ podpoint-monitor for the car row) | Import / export / gas / standing costs, daily cost bars, off-peak share, solar saving, car charging |
| `heating.json` | tado-monitor (+ octopus-collector) | Room vs target vs outside temperature, heating demand, humidity, gas vs heating per day |
| `network.json` | internet-monitor, powerline-monitor | Online/offline timeline for the internet and router, latency, speed tests, powerline link speed, lost MAPs and adapter status |
| `server.json` | host-health with `VM_URL` | CPU temperature (with the fan and throttling points marked), hottest in 7 days, fan speed, throttling, memory, SD-card writes, open problems |
| `embed.json` | the above | Small transparent panels for embedding in other pages |

Daily bars use `max_over_time(<counter>[1d] offset -1d)` (for daily counters that reset at
midnight) or `sum_over_time(<per-slot metric>[1d] offset -1d)`, with a 1-day interval.

To embed panels elsewhere (e.g. Homepage), set `GF_SECURITY_ALLOW_EMBEDDING=true`, allow anonymous
viewing (`GF_AUTH_ANONYMOUS_ENABLED=true`, role `Viewer`) on a trusted network, and use
`/d-solo/<uid>/<slug>?panelId=<id>&theme=dark&hideLogo=true`.

## Homepage

[`examples/homepage/`](../examples/homepage) shows:

* a read-only busybox web server publishing `/var/lib/homelab-status` to Homepage,
* `customapi` widgets reading each tool's status file (field names are listed on each tool's page),
* Grafana panels in `iframe` widgets,
* `custom.css` that makes all cards the same height so rows line up.

Keep values short (the tools already format them as e.g. `"1.25 kW"`, `"£2.54"`, `"Sat 14:05"`):
long values wrap and make cards uneven.
