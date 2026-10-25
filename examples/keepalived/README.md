# Pi-hole failover with keepalived

Two Pi-holes, one DNS address. See [docs/pihole-failover.md](../../docs/pihole-failover.md) for the
full walkthrough (keeping the secondary DNS-only, syncing settings with nebula-sync, testing).

| File | Install to |
|---|---|
| `keepalived-primary.conf` / `keepalived-secondary.conf` | `/etc/keepalived/keepalived.conf` on each host (mode 600) |
| `check-pihole-dns` | `/usr/local/bin/` on both |
| `keepalived-notify-ntfy` | `/usr/local/bin/` on both |
