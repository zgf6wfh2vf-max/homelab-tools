# pihole-threat-alert

Pi-hole blocks malware and phishing domains if they are on your blocklists, but it does not tell
you. A device **asking** for such a domain is a strong sign it is compromised or that someone just
clicked a bad link. This tool sends a high-priority alert naming the device and domains.

## Setup

1. Add a dedicated threat list to Pi-hole (Lists / Adlists), e.g. HaGeZi's Threat Intelligence Feeds
   (adblock format): `https://raw.githubusercontent.com/hagezi/dns-blocklists/main/adblock/tif.txt`,
   then update gravity. Using a *separate* list means ordinary ad blocking never triggers alerts.
2. Give the service user read access to `pihole-FTL.db` and `gravity.db` (e.g. a group with read
   permission, or run it as the user that owns them).
3. Enable `pihole-threat-alert.timer` (every 5 minutes). The first run records the current position
   and alerts only on new lookups. To see what happened *before*, run
   `pihole-threat-alert --report 90`.

## Settings

| Setting | Default | Meaning |
|---|---|---|
| `PIHOLE_DIR` | `/etc/pihole` | Directory with Pi-hole's databases |
| `THREAT_LIST_MATCH` | `%hagezi%tif%` | SQL LIKE pattern for the threat list's URL |
| `THREAT_STATE_FILE` | `/var/lib/pihole-threat-alert/state.json` | Progress and de-duplication |
| `THREAT_REALERT_HOURS` | 24 | Re-alert the same device + domain after this long |

## How matching works

Adblock rules like `||evil.example^` cover the domain and all its subdomains, so a lookup of
`cdn.evil.example` is checked against `||cdn.evil.example^`, `||evil.example^` and the bare name.
Only queries Pi-hole marked as blocked by gravity (status 1, or 9 for a blocked CNAME) alert; the
`--report` mode also lists *allowed* lookups (e.g. from before the list was added).

The device name is the most recent name Pi-hole recorded for the IP, since DHCP addresses change
hands over time.

## Caveats

* Pi-hole writes queries to its database about once a minute, so detection takes a minute or two.
* Devices that bypass Pi-hole (hard-coded DNS, DNS-over-HTTPS, iCloud Private Relay, or a router
  advertising its own IPv6 DNS server) are invisible to it. Pi-hole's
  `dns.specialDomains.iCloudPrivateRelay` setting helps with the Apple case.
