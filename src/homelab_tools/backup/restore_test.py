"""``restic-restore-test``: restore a sample of files from the newest restic snapshot and check them.

A backup job that "succeeds" every night can still be useless: a lost password, a damaged pack file,
a path that was never included. ``restic check`` verifies the repository's structure, but only a real
restore shows that you get your files back. Each run (monthly is plenty):

1. finds the newest snapshot (optionally filtered, e.g. ``--host myhost --tag nightly``) and checks
   that it is recent;
2. picks a random sample of files from it within a size budget, always including some SQLite
   databases when there are any;
3. restores only those files to a scratch directory with ``restic restore --verify``;
4. checks each restored file: the size recorded in the snapshot, the SHA-256 of the original on disk
   when the original is unchanged since the snapshot, and ``PRAGMA quick_check`` for databases;
5. reports to ntfy (low priority when it passes, high when it fails) and to ``backup.json``, then
   deletes the scratch copy.

The repository is never written to. restic itself is configured the usual way, through its own
environment variables (``RESTIC_REPOSITORY``, ``RESTIC_PASSWORD_FILE``, cloud credentials).

Settings (environment): ``RESTORE_SNAPSHOT_FILTER`` (restic arguments that select snapshots),
``RESTORE_SAMPLE_FILES`` (20), ``RESTORE_MAX_MB`` (200), ``RESTORE_MAX_AGE_DAYS`` (2),
``RESTORE_DIR`` (``/var/tmp``), ``NTFY_URL``, ``STATUS_DIR``.
"""

from __future__ import annotations

import hashlib
import json
import logging
import random
import re
import shlex
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from homelab_tools.common.config import Outputs, env_int, env_str
from homelab_tools.common.notify import Notifier
from homelab_tools.common.status import write_status

log = logging.getLogger("restic-restore-test")

#: Runs restic with the given arguments and returns its standard output.
Runner = Callable[[Sequence[str]], str]

#: Suffixes treated as databases when choosing the sample (the check itself reads the file header).
DB_SUFFIXES = (".db", ".sqlite", ".sqlite3")
SQLITE_HEADER = b"SQLite format 3\x00"
#: restic ``--include`` takes glob patterns; files whose names contain these are skipped.
GLOB_CHARS = re.compile(r"[*?\[\]\\]")


class RestoreTestError(RuntimeError):
    """The test could not run (restic failed, no snapshot, ...), as opposed to a file failing a check."""


def make_runner(binary: str = "restic", env: Mapping[str, str] | None = None) -> Runner:
    """Return a :data:`Runner` that calls the real restic.

    Args:
        binary: restic executable.
        env: Full environment for restic; ``None`` inherits this process's (the normal case).
    """

    def run(args: Sequence[str]) -> str:
        proc = subprocess.run([binary, *args], capture_output=True, text=True, env=env, check=False)
        if proc.returncode != 0:
            detail = proc.stderr.strip().splitlines()[-1:] or [f"exit code {proc.returncode}"]
            raise RestoreTestError(f"restic {args[0]} failed: {detail[0]}")
        return proc.stdout

    return run


def parse_time(text: str) -> datetime:
    """Parse a restic timestamp, which has nanoseconds that :mod:`datetime` cannot hold.

    >>> parse_time("2026-10-26T04:36:17.812418462Z").isoformat()
    '2026-10-26T04:36:17.812418+00:00'
    """
    text = re.sub(r"(\.\d{6})\d+", r"\1", text.replace("Z", "+00:00"))
    return datetime.fromisoformat(text)


@dataclass(frozen=True)
class Snapshot:
    """A restic snapshot: its id and when it was taken."""

    id: str
    time: datetime


@dataclass(frozen=True)
class Entry:
    """A regular file in a snapshot, as listed by ``restic ls``."""

    path: str
    size: int
    mtime: datetime

    @property
    def is_database(self) -> bool:
        """Whether the name suggests an SQLite database."""
        return self.path.endswith(DB_SUFFIXES)


def latest_snapshot(run: Runner, filters: Sequence[str] = ()) -> Snapshot:
    """The newest snapshot matching ``filters``.

    Raises:
        RestoreTestError: If no snapshot matches.
    """
    snapshots = json.loads(run(["snapshots", "--json", *filters]) or "[]")
    if not snapshots:
        raise RestoreTestError("no snapshots found" + (f" for {' '.join(filters)}" if filters else ""))
    newest = max(snapshots, key=lambda s: parse_time(s["time"]))
    return Snapshot(newest["id"], parse_time(newest["time"]))


def list_files(run: Runner, snapshot: Snapshot) -> list[Entry]:
    """Every regular file in ``snapshot``.

    ``restic ls --json`` prints one JSON object per line: the snapshot first, then one per node.
    """
    files = []
    for line in run(["ls", "--json", snapshot.id]).splitlines():
        node = json.loads(line) if line.strip() else {}
        if node.get("type") == "file" and "path" in node:
            files.append(Entry(node["path"], int(node.get("size", 0)), parse_time(node["mtime"])))
    return files


def choose_sample(entries: Sequence[Entry], count: int, max_bytes: int, rng: random.Random) -> list[Entry]:
    """Pick up to ``count`` random files whose total size stays within ``max_bytes``.

    About a quarter of the picks (at least one) are databases when the snapshot has any, because they
    matter most and are the files most likely to be copied inconsistently. Empty files and names
    containing glob characters (which restic's ``--include`` would misread) are never picked.
    """
    usable = [e for e in entries if 0 < e.size <= max_bytes and not GLOB_CHARS.search(e.path)]
    rng.shuffle(usable)
    databases = [e for e in usable if e.is_database][: max(1, count // 4)]
    others = [e for e in usable if e not in databases]
    sample: list[Entry] = []
    budget = max_bytes
    for entry in databases + others:
        if len(sample) == count:
            break
        if entry.size <= budget:
            sample.append(entry)
            budget -= entry.size
    return sample


def restore(run: Runner, snapshot: Snapshot, sample: Sequence[Entry], target: Path) -> None:
    """Restore exactly the ``sample`` files from ``snapshot`` under ``target``, verifying them."""
    includes = [arg for entry in sample for arg in ("--include", entry.path)]
    run(["restore", snapshot.id, "--target", str(target), "--verify", *includes])


def sha256(path: Path) -> str:
    """Hex SHA-256 of a file, read in 1 MiB blocks."""
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def sqlite_problem(path: Path) -> str | None:
    """``None`` if ``path`` is a healthy SQLite database, else what is wrong with it."""
    try:
        with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as db:
            result = db.execute("PRAGMA quick_check").fetchone()[0]
    except sqlite3.Error as exc:
        return f"database does not open: {exc}"
    return None if result == "ok" else f"database check failed: {result}"


@dataclass
class FileResult:
    """Outcome of the checks on one restored file.

    Attributes:
        entry: The file as listed in the snapshot.
        problem: Why it failed, or ``None`` if every check passed.
        matched_original: The content was compared with the unchanged original on disk.
        database_ok: It is an SQLite database and passed ``PRAGMA quick_check``.
    """

    entry: Entry
    problem: str | None = None
    matched_original: bool = False
    database_ok: bool = False


def check_file(entry: Entry, target: Path) -> FileResult:
    """Run every check that applies to one restored file."""
    restored = target / entry.path.lstrip("/")
    if not restored.is_file():
        return FileResult(entry, "not restored")
    if restored.stat().st_size != entry.size:
        return FileResult(entry, f"size {restored.stat().st_size} B, snapshot says {entry.size} B")
    result = FileResult(entry)
    original = Path(entry.path)
    try:
        stat = original.stat()
        unchanged = stat.st_size == entry.size and abs(stat.st_mtime - entry.mtime.timestamp()) < 1
    except OSError:
        unchanged = False  # deleted or unreadable since the backup: nothing to compare with
    if unchanged:
        if sha256(restored) != sha256(original):
            return FileResult(entry, "content differs from the unchanged original")
        result.matched_original = True
    with open(restored, "rb") as f:
        is_sqlite = f.read(len(SQLITE_HEADER)) == SQLITE_HEADER
    if is_sqlite:
        result.problem = sqlite_problem(restored)
        result.database_ok = result.problem is None
    return result


@dataclass
class Report:
    """Result of one restore test.

    Attributes:
        snapshot: The snapshot tested (``None`` if the test could not start).
        results: One entry per sampled file.
        error: Why the test could not run, if it could not.
        seconds: Wall-clock duration.
        finished: When the test ended.
    """

    snapshot: Snapshot | None = None
    results: list[FileResult] = field(default_factory=list)
    error: str | None = None
    seconds: float = 0.0
    finished: datetime = field(default_factory=lambda: datetime.now(UTC))

    @property
    def problems(self) -> list[FileResult]:
        """Files that failed a check."""
        return [r for r in self.results if r.problem]

    @property
    def passed(self) -> bool:
        """True when the test ran, restored at least one file, and every file passed."""
        return self.error is None and bool(self.results) and not self.problems

    @property
    def restored_bytes(self) -> int:
        """Total size of the sampled files."""
        return sum(r.entry.size for r in self.results)


@dataclass
class RestoreTest:
    """One configured restore test.

    Attributes:
        run: Calls restic.
        filters: restic arguments selecting the snapshots to consider.
        sample_files: Number of files to restore.
        max_bytes: Size budget for the sample.
        max_age_days: The newest snapshot must be younger than this, or the test fails.
        scratch_dir: Parent directory for the temporary restore.
        rng: Random source (seeded in tests).
    """

    run: Runner
    filters: Sequence[str] = ()
    sample_files: int = 20
    max_bytes: int = 200 * 10**6
    max_age_days: float = 2
    scratch_dir: Path = Path("/var/tmp")
    rng: random.Random = field(default_factory=random.Random)

    def execute(self, now: datetime | None = None) -> Report:
        """Run the whole test; never raises for restic or check failures, which go in the report."""
        started = time.monotonic()
        report = Report()
        now = now or datetime.now(UTC)
        try:
            report.snapshot = latest_snapshot(self.run, self.filters)
            age = (now - report.snapshot.time).total_seconds() / 86400
            if age > self.max_age_days:
                raise RestoreTestError(f"the newest snapshot is {age:.0f} days old - is the backup running?")
            sample = choose_sample(
                list_files(self.run, report.snapshot), self.sample_files, self.max_bytes, self.rng
            )
            if not sample:
                raise RestoreTestError("the snapshot contains no files to test")
            target = Path(tempfile.mkdtemp(prefix="restore-test-", dir=self.scratch_dir))
            try:
                restore(self.run, report.snapshot, sample, target)
                report.results = [check_file(entry, target) for entry in sample]
            finally:
                shutil.rmtree(target, ignore_errors=True)
        except RestoreTestError as exc:
            report.error = str(exc)
        report.seconds = time.monotonic() - started
        report.finished = datetime.now(UTC)
        return report


def _size(n: int) -> str:
    """Human-readable size: ``"512 kB"``, ``"34.2 MB"``."""
    return f"{n / 1e6:.1f} MB" if n >= 10**6 else f"{max(1, round(n / 1e3))} kB"


def format_report(report: Report) -> tuple[str, str, str]:
    """Phone notification for a report.

    Returns:
        ``(title, message, priority)``.
    """
    if report.error:
        return "Backup restore test FAILED", report.error, "high"
    snap = f"{report.snapshot.time.astimezone():%d %b %H:%M}" if report.snapshot else "?"
    matched = sum(r.matched_original for r in report.results)
    dbs = sum(r.database_ok for r in report.results)
    summary = (
        f"{len(report.results)} files ({_size(report.restored_bytes)}) restored from the {snap} snapshot; "
        f"{matched} identical to the originals, {dbs} database{'s' if dbs != 1 else ''} checked."
    )
    if report.passed:
        return "Backup restore test passed", summary, "low"
    lines = [f"{p.entry.path}: {p.problem}" for p in report.problems[:5]]
    if len(report.problems) > 5:
        lines.append(f"... and {len(report.problems) - 5} more")
    return "Backup restore test FAILED", "\n".join([summary, *lines]), "high"


def display(report: Report) -> dict[str, str]:
    """Display strings for ``backup.json`` (see ``docs/restic-restore-test.md``)."""
    return {
        "result": "passed" if report.passed else "FAILED",
        "tested": f"{report.finished.astimezone():%d %b %H:%M}",
        "snapshot": f"{report.snapshot.time.astimezone():%d %b %H:%M}" if report.snapshot else "-",
        "files": str(len(report.results)),
        "size": _size(report.restored_bytes) if report.results else "-",
        "matched": str(sum(r.matched_original for r in report.results)),
        "databases": str(sum(r.database_ok for r in report.results)),
        "problem": first_problem(report),
    }


def first_problem(report: Report) -> str:
    """The reason a test failed, for a one-line display; empty when it passed."""
    if report.error:
        return report.error
    if report.problems:
        return f"{Path(report.problems[0].entry.path).name}: {report.problems[0].problem}"
    return "" if report.passed else "nothing was restored"


def main() -> None:
    """Entry point: one test (meant for a monthly systemd timer). Exits 1 if the test failed."""
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    outputs = Outputs.from_env()
    test = RestoreTest(
        run=make_runner(),
        filters=shlex.split(env_str("RESTORE_SNAPSHOT_FILTER", "")),
        sample_files=env_int("RESTORE_SAMPLE_FILES", 20),
        max_bytes=env_int("RESTORE_MAX_MB", 200) * 10**6,
        max_age_days=env_int("RESTORE_MAX_AGE_DAYS", 2),
        scratch_dir=Path(env_str("RESTORE_DIR", "/var/tmp")),
    )
    report = test.execute()
    title, message, priority = format_report(report)
    log.info("%s: %s (%.0fs)", title, message, report.seconds)
    Notifier(outputs.ntfy_url).send(
        title, message, priority=priority, tags="white_check_mark" if report.passed else "warning"
    )
    write_status(outputs.status_dir, "backup", display(report))
    if not report.passed:
        sys.exit(1)


if __name__ == "__main__":
    main()
