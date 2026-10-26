# internet-monitor

Keeps a record of your **broadband**: every outage, the latency, and a speed test a few times a
day, with a monthly report you can send to your provider. It uses only the Python standard library
and Cloudflare's public speed-test servers.

| Command | Run by | Does |
|---|---|---|
| `internet-monitor watch` | a service (always running) | checks the line every `INTERNET_CHECK_S` seconds and reports each outage when it ends |
| `internet-monitor speedtest` | a timer, 4 times a day | download and upload test; alerts after three slow tests in a row |
| `internet-monitor report` | a timer, on the 1st | last month's outages and speeds, with a detailed file |

## Outages

Each check opens a TCP connection to each of `INTERNET_TARGETS` (by default Cloudflare, Google and
Quad9 DNS servers on port 443). The line counts as up if any of them answers. Two failed checks in
a row (a minute, by default) start an outage. Nothing can be sent while the internet is down, so
the alert arrives when it's back:

```
Internet back
The internet was down for 7 min (14:02-14:09). The router stayed reachable, so the fault was on
the broadband line or at the provider.
```

If `INTERNET_GATEWAY` (your router) is set, it is checked too. That tells a broadband fault apart
from a router restart, a power cut or a home-network problem. The monthly report keeps the two
apart.

## Speed tests

Each test measures the connection time to `speed.cloudflare.com`, then downloads for about 10
seconds and uploads for about 10 seconds over four parallel connections. Each connection does a
warm-up transfer first, which isn't counted. A test uses roughly as many megabytes as your speed in
Mbps (about 150 MB on a 150 Mbps line), so four tests a day come to a few GB a month.

A test competes with whatever else is using the line, so a single low figure means little. That is
why the alert needs **three slow tests in a row** under `INTERNET_MIN_DOWN_MBPS`. Set that to the
minimum guaranteed speed in your broadband contract (UK providers that follow Ofcom's code must
tell you one), or leave it at 0 to turn the alert off.

## Monthly report

On the 1st of each month, a notification sums up the previous month:

```
Broadband in October
2 outages, 19 min in total (longest 14 min on 12 Oct 14:00, broadband). Download median 142 Mbps
(slowest 98), upload median 31 Mbps, from 124 tests. 3 of 124 tests were under your 100 Mbps minimum.
```

"Slowest" is the 10th percentile: one test in ten was slower. Every outage and test is listed in
`report-YYYY-MM.txt` in the state directory. `internet-monitor report --month 2026-10 --dry-run`
prints any month instead of sending it.

## Settings

| Setting | Default | Meaning |
|---|---|---|
| `INTERNET_TARGETS` | `1.1.1.1:443 8.8.8.8:443 9.9.9.9:443` | Addresses to connect to (any answering = up) |
| `INTERNET_GATEWAY` | - | Your router, e.g. `192.168.1.1:80`, to tell broadband faults from home-network ones |
| `INTERNET_CHECK_S` | 30 | Seconds between checks |
| `INTERNET_MIN_DOWN_MBPS` | 0 | Slow-line alert threshold (0 = off) |
| `INTERNET_STATE_DIR` | `/var/lib/internet-monitor` | Outages, test results (13 months) and reports |
| `NTFY_URL`, `VM_URL`, `STATUS_DIR` | - | Outputs (see [installation](installation.md)) |

## Outputs

**Metrics:** `internet_up`, `internet_latency_ms` and `internet_router_up` from each check;
`internet_outage_s` when an outage ends; `internet_download_mbps`, `internet_upload_mbps` and
`internet_ping_ms` from each speed test.

**Status** (`internet.json`, written by both `watch` and `speedtest`): `status` ("online" or "down
since 14:02"), `latency`, `outages_30d`, `last_outage`, `download` and `upload` (latest test),
`download_30d` and `upload_30d` (30-day medians), `tested` (time of the latest test), `checked`.
