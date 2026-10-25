# opencanary-ntfy

[OpenCanary](https://github.com/thinkst/opencanary) is a honeypot: a fake device with tempting
services (SSH, FTP, a NAS login page, databases). Nothing legitimate ever connects to it, so **any**
connection is worth knowing about. This relay follows OpenCanary's JSON log and turns events into
readable phone alerts.

## Alerting

* The **first** event from a source alerts immediately at urgent priority:
  "192.168.1.99 touched the honeypot (fake NAS): SSH login attempt on port 22 (username=root, password=admin)".
* Everything else from that source in the next `CANARY_WINDOW` seconds (a port scan produces
  hundreds of events) is summarised in **one** "still active" alert at the end of the window.

## Setup

1. Run OpenCanary - [`examples/opencanary/`](../examples/opencanary) gives it its own LAN address
   via macvlan and a NAS disguise, with a `file` log handler.
2. Give the service user read access to the log, set `CANARY_LOG`, enable `opencanary-ntfy.service`.
3. Test **from another machine** (a Docker host cannot reach its own macvlan containers):
   `nc -zv 192.168.1.240 22` or open `http://192.168.1.240/`.

## Settings

| Setting | Default | Meaning |
|---|---|---|
| `CANARY_LOG` | required | OpenCanary's log file |
| `CANARY_NAME` | `honeypot` | How alerts describe it, e.g. `fake NAS nas01 (192.168.1.240)` |
| `CANARY_WINDOW` | 900 | Seconds to batch follow-up events per source |

The relay survives log rotation and truncation (`copytruncate`) and does not replay old events
after a restart.
