# ipv6-watch

Every five minutes, a check that **no router has started announcing IPv6**, or a DNS server other
than yours, on a network where Pi-hole (or any local DNS server) is meant to answer every lookup.

**Why:** a router that announces IPv6 usually announces its own DNS server too. Phones, iPhones in
particular, then prefer that server to the one DHCP gave them, so they quietly stop using Pi-hole.
There's no ad or threat blocking, and local names (`home.example.com` and so on) resolve to your
public address, which usually opens the router's own page instead. Many ISP routers, including BT
Hubs, turn IPv6 back on by themselves after a restart or a firmware update.

Each run sends an IPv6 router solicitation (asking every router on the network to announce itself)
with `rdisc6` from the `ndisc6` package, and reads the answers. It changes nothing.

| Check | Problem when | Priority |
|---|---|---|
| IPv6 announced | A router offers a default route or an address prefix, and `IPV6_EXPECTED` is `no` | high |
| Other DNS server | A router announces a DNS server that isn't in `IPV6_ALLOWED_DNS` | high |

Each problem is reported at once and only once, with a low-priority "OK again" when it clears. The
alert names the router's link-local address and the DNS server it offers.

**When it fires:** turn IPv6 off in the router (on a BT Hub: Advanced Settings → Broadband → IPv6),
then turn Wi-Fi off and on again on the affected phones so they drop the router's DNS server. If you
do want IPv6, set `IPV6_EXPECTED=yes` and have the router announce your Pi-hole's IPv6 address as
its DNS server (or none), and list that address in `IPV6_ALLOWED_DNS`.

## Installing

```sh
sudo apt install ndisc6
```

`rdisc6` needs a raw network socket. The example unit grants just that (`CAP_NET_RAW`), because
`NoNewPrivileges=yes` stops its setuid bit working.

## Settings

| Setting | Default | Meaning |
|---|---|---|
| `IPV6_IFACE` | `eth0` | Network interface to ask on |
| `IPV6_EXPECTED` | `no` | `yes` if IPv6 is meant to be on; then only DNS servers are checked |
| `IPV6_ALLOWED_DNS` | (none) | Space-separated IPv6 DNS servers that routers may announce |
| `IPV6_STATE_FILE` | `/var/lib/ipv6-watch/state.json` | Problems already reported |
| `NTFY_URL`, `VM_URL`, `STATUS_DIR` | - | Outputs (see [installation](installation.md)) |

## Outputs

**Metrics:** `ipv6_routers` (routers that answered), `ipv6_announcing` (of those, how many offer
IPv6), `ipv6_dns_servers` and `ipv6_problems`.

**Status** (`ipv6.json`): `ipv6` ("off", or "on (1 router)"), `dns` (DNS servers announced, or
"none announced"), `problems`, `checked`.
