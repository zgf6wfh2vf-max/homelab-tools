# restic-restore-test

Proves that a [restic](https://restic.net) backup can actually be restored, by restoring a random
sample of files from the newest snapshot and checking each one. Run it monthly.

A nightly backup that "succeeds" can still be useless: the password was lost, a pack file is
damaged, or an important path was never included. `restic check` verifies the repository's
structure (and `--read-data-subset` re-reads some of the data), but only a real restore shows you get
your files back in a usable state.

```
Backup restore test passed
20 files (41.3 MB) restored from the 01 Nov 04:36 snapshot; 17 identical to the originals, 5 databases checked.
```

## What it checks

1. **The newest snapshot is recent** (`RESTORE_MAX_AGE_DAYS`), so a backup job that silently stopped
   is caught too.
2. **A random sample restores**: up to `RESTORE_SAMPLE_FILES` files within `RESTORE_MAX_MB`, about a
   quarter of them SQLite databases when the snapshot has any. Only those files are downloaded, so
   the cost from a cloud repository is small. `restic restore --verify` re-reads the restored files
   and checks them against the snapshot.
3. **Each restored file** has the size recorded in the snapshot, and:
   * if the original on disk is **unchanged** since the snapshot (same size and modification time),
     the restored copy must be **byte-for-byte identical** (SHA-256);
   * if it is an **SQLite database** (recognised by its header), it must open and pass
     `PRAGMA quick_check`.
4. The scratch copy is deleted. The repository is only read, never written.

A pass sends a low-priority ntfy message; a failure sends a high-priority one listing up to five
problem files, and the service exits with an error.

## Setup

restic reads its usual environment variables, so reuse your backup job's environment file
(`RESTIC_REPOSITORY`, `RESTIC_PASSWORD_FILE`, and e.g. `B2_ACCOUNT_ID` / `B2_ACCOUNT_KEY`):

```sh
sudo cp examples/env/restic-restore-test.env.example /etc/homelab-tools/restic-restore-test.env
sudo cp examples/systemd/restic-restore-test.{service,timer} /etc/systemd/system/
# edit the service's first EnvironmentFile= line to point at your restic settings
sudo systemctl daemon-reload
sudo systemctl start restic-restore-test       # try it once now
sudo systemctl enable --now restic-restore-test.timer
```

The example service runs as **root**, because the backed-up files (and the restic password) are
usually readable only by root, and the byte-for-byte comparison needs to read the originals. It uses
systemd sandboxing to keep the rest of the system read-only.

## Settings

| Setting | Default | Meaning |
|---|---|---|
| `RESTORE_SNAPSHOT_FILTER` | - | restic arguments selecting the snapshots, e.g. `--host myhost --tag nightly` |
| `RESTORE_SAMPLE_FILES` | 20 | Files to restore |
| `RESTORE_MAX_MB` | 200 | Size budget for the sample (larger files are never picked) |
| `RESTORE_MAX_AGE_DAYS` | 2 | Fail if the newest snapshot is older than this |
| `RESTORE_DIR` | `/var/tmp` | Where the temporary restore is made (needs `RESTORE_MAX_MB` free) |
| `NTFY_URL`, `STATUS_DIR` | - | Outputs (see [installation](installation.md)) |

## Outputs

**Status** (`backup.json`): `result` (`passed` / `FAILED`), `tested`, `snapshot`, `files`, `size`,
`matched` (files identical to the originals), `databases` (databases that passed their check),
`problem` (the first reason for a failure).

## Notes

* Files that changed after the snapshot cannot be compared with the original; they are still checked
  for size, by `restore --verify`, and as databases. If very few files match, most of your backup is
  data that changes daily, which is normal for databases.
* Names containing `* ? [ ] \` are never picked, because `restic restore --include` treats them as
  patterns.
* Cloud egress: the sample is downloaded once a month (200 MB at most by default). Backblaze B2 gives
  free egress up to three times the data stored.
