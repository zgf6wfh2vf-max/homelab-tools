# Pi-hole failover

If your one Pi-hole goes down, every device on the network loses DNS. Two Pi-holes sharing **one
DNS address** fix that: devices only ever know the shared address, and `keepalived` moves it to the
standby within seconds when the primary stops answering.

```
devices --DNS--> 192.168.1.53 (shared)
                  |-- primary Pi-hole   (holds it normally, priority 150)
                  `-- secondary Pi-hole (takes it over when the primary fails, priority 100)
```

Why a shared address instead of handing out two DNS servers? With two, devices split their queries
unpredictably, so query logs and alerts (e.g. [pihole-threat-alert](pihole-threat-alert.md)) only
see part of the traffic. With a shared address everything goes to the primary while it is healthy.

## 1. Second Pi-hole, DNS only

Run Pi-hole on the second machine with DHCP and router advertisements **locked off** through
environment variables, which override `pihole.toml` and cannot be changed by a sync:

```yaml
environment:
  FTLCONF_dhcp_active: "false"
  FTLCONF_dhcp_ipv6: "false"
```

Two DHCP servers on one network cause chaos; this makes that impossible.

## 2. Keep them in sync

[nebula-sync](https://github.com/lovelaze/nebula-sync) copies lists, groups, clients and DNS
settings from the primary. Use **selective** mode and leave DHCP and host-specific settings out:

```yaml
FULL_SYNC: "false"
SYNC_CONFIG_DNS: "true"
SYNC_CONFIG_DHCP: "false"
SYNC_CONFIG_MISC: "false"
SYNC_GRAVITY_AD_LIST: "true"
SYNC_GRAVITY_DOMAIN_LIST: "true"
SYNC_GRAVITY_GROUP: "true"
SYNC_GRAVITY_CLIENT: "true"
RUN_GRAVITY: "true"
CRON: "45 4 * * *"   # daily: every run rebuilds the blocklist database (hundreds of MB written)
```

## 3. keepalived

Install `keepalived` on both machines and use the files in
[`examples/keepalived`](../examples/keepalived). The health check resolves a **local** DNS record,
so an internet outage does not make both look unhealthy. The notify script sends an ntfy alert on
failover.

## 4. Hand out the shared address

In the primary Pi-hole's DHCP settings, set the DNS server option to the shared address, e.g. via
`misc.dnsmasq_lines`: `dhcp-option=option:dns-server,192.168.1.53`. Devices switch as they renew
their leases.

## 5. Test

```sh
docker stop pihole                      # on the primary
ip -br addr show eth0                   # on the secondary: the shared address appears within ~5 s
dig @192.168.1.53 example.com           # still answers
docker start pihole                     # the address returns to the primary ~30 s later
```

## Gotchas

* **IPv6:** if forwarding is enabled on the Pi-hole host (e.g. for Tailscale), Pi-hole's router
  advertisements announce the host as an IPv6 router. Add `ra-param=eth0,60,0` to
  `misc.dnsmasq_lines` (router lifetime 0).
* **Routers advertising their own IPv6 DNS** (common on ISP routers) make some devices bypass
  Pi-hole entirely. If your router cannot stop advertising it, consider disabling IPv6 on it.
* DHCP still runs only on the primary; with 24-hour leases a short outage does not matter.
