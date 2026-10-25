# sqlite-snapshot-backup

A nightly snapshot of a directory - typically `/opt/stacks` with your Docker Compose apps - that
handles **live SQLite databases** correctly.

## Why

Most self-hosted apps (Pi-hole, Uptime Kuma, Grafana, Nginx Proxy Manager, Syncthing...) keep their
state in SQLite and write to it constantly. Copying the file while it is being written can give you
a corrupt backup - and you only find out when you need it.

## What it does

* `rsync --link-dest` snapshot per day: unchanged files are hard links to yesterday's copy, so each
  snapshot looks complete but only changed files take space.
* `*.db` / `*.sqlite` files are copied with SQLite's `VACUUM INTO` (a consistent copy of a live
  database) and verified with `PRAGMA quick_check`.
* `.db` files that are not SQLite (e.g. BoltDB, used by Diun) are recognised by their header and
  copied normally.
* Builds `<date>.partial`, renames it when complete, keeps the newest `SNAPSHOT_KEEP`.
* ntfy alert on failure; optional heartbeat URL on success, so a backup that stops running at all
  is noticed (e.g. an Uptime Kuma push monitor with a 25-hour interval).

## Setup

```sh
sudo install -m 755 scripts/sqlite-snapshot-backup /usr/local/bin/
sudo cp examples/env/sqlite-snapshot-backup.env.example /etc/default/sqlite-snapshot-backup   # edit
sudo cp examples/systemd/sqlite-snapshot-backup.{service,timer} /etc/systemd/system/
sudo systemctl enable --now sqlite-snapshot-backup.timer
sudo systemctl start sqlite-snapshot-backup && journalctl -u sqlite-snapshot-backup -n 5
```

Requires `rsync` and `sqlite3`. Edit `RequiresMountsFor=` in the unit to your backup disk so the
backup never writes into an empty mount point.

## Settings

| Setting | Default | Meaning |
|---|---|---|
| `SNAPSHOT_SOURCE` | required | Directory to back up |
| `SNAPSHOT_EXTRA` | - | Optional second directory, stored as `<snapshot>/_extra` |
| `SNAPSHOT_DEST` | required | Where snapshots go (another disk!) |
| `SNAPSHOT_KEEP` | 14 | Daily snapshots to keep |
| `NTFY_URL` | - | Failure alerts |
| `HEARTBEAT_URL` | - | GET after success (`status`, `msg`, `ping` appended) |

## Off-site

A snapshot on a disk in the same box does not survive fire, theft or a dead disk. Push the newest
snapshot off-site with an encrypting tool such as `restic` (e.g. to Backblaze B2's free 10 GB), and
keep the encryption password somewhere other than that machine.
