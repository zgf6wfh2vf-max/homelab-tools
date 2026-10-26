from __future__ import annotations

import json
import os
import random
import shutil
import sqlite3
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from homelab_tools.backup.restore_test import (
    Entry,
    RestoreTest,
    RestoreTestError,
    Snapshot,
    check_file,
    choose_sample,
    display,
    format_report,
    make_runner,
    parse_time,
)

NOW = datetime(2026, 11, 1, 10, 0, tzinfo=UTC)


def make_db(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path) as db:
        db.execute("create table t (x)")
        db.execute("insert into t values (1)")
    db.close()


def entry_for(path: Path) -> Entry:
    st = path.stat()
    return Entry(str(path), st.st_size, datetime.fromtimestamp(st.st_mtime, UTC))


class FakeRestic:
    """Pretends to be restic for a 'snapshot' of real files under a directory.

    ``restore`` copies the requested originals into the target, except ``missing`` paths, and writes
    ``corrupt`` paths with altered content, as a damaged backup would.
    """

    def __init__(self, files: Sequence[Path], taken: datetime = NOW - timedelta(hours=6)) -> None:
        self.files, self.taken = list(files), taken
        self.corrupt: set[str] = set()
        self.missing: set[str] = set()
        self.calls: list[list[str]] = []

    def __call__(self, args: Sequence[str]) -> str:
        args = list(args)
        self.calls.append(args)
        if args[0] == "snapshots":
            old = self.taken - timedelta(days=1)
            return json.dumps(
                [{"id": "new", "time": self.taken.isoformat()}, {"id": "old", "time": old.isoformat()}]
            )
        if args[0] == "ls":
            lines = [json.dumps({"struct_type": "snapshot", "id": args[2]})]
            for f in self.files:
                e = entry_for(f)
                lines.append(
                    json.dumps({"type": "file", "path": e.path, "size": e.size, "mtime": e.mtime.isoformat()})
                )
            lines.append(json.dumps({"type": "dir", "path": str(self.files[0].parent)}))
            return "\n".join(lines) + "\n"
        if args[0] == "restore":
            target = Path(args[args.index("--target") + 1])
            for path in [args[i + 1] for i, a in enumerate(args) if a == "--include"]:
                if path in self.missing:
                    continue
                dest = target / path.lstrip("/")
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(path, dest)
                if path in self.corrupt:
                    data = bytearray(dest.read_bytes())
                    data[-1] ^= 0xFF
                    dest.write_bytes(bytes(data))
                    os.utime(dest, (0, Path(path).stat().st_mtime))
            return ""
        raise AssertionError(f"unexpected restic call {args}")


@pytest.fixture
def tree(tmp_path: Path) -> list[Path]:
    src = tmp_path / "src"
    (src / "etc").mkdir(parents=True)
    (src / "etc" / "hosts").write_text("127.0.0.1 localhost\n")
    (src / "etc" / "big.bin").write_bytes(b"x" * 5000)
    make_db(src / "app" / "app.db")
    return [src / "etc" / "hosts", src / "etc" / "big.bin", src / "app" / "app.db"]


def make_test(run, tmp_path: Path, **kw) -> RestoreTest:
    scratch = tmp_path / "scratch"
    scratch.mkdir(exist_ok=True)
    return RestoreTest(run=run, scratch_dir=scratch, rng=random.Random(1), **kw)


def test_parse_time_handles_nanoseconds_and_zones() -> None:
    assert parse_time("2026-10-26T04:36:17.812418462Z") == datetime(2026, 10, 26, 4, 36, 17, 812418, UTC)
    assert parse_time("2026-10-24T15:05:34+01:00").utcoffset() == timedelta(hours=1)


def test_choose_sample_prefers_databases_and_respects_budget() -> None:
    t = NOW
    entries = [Entry(f"/data/f{i}.txt", 100, t) for i in range(50)]
    entries += [Entry(f"/data/d{i}.db", 1000, t) for i in range(10)]
    entries += [Entry("/data/empty", 0, t), Entry("/data/odd[1].txt", 10, t), Entry("/data/huge", 10**9, t)]
    sample = choose_sample(entries, count=20, max_bytes=10_000, rng=random.Random(3))
    assert len(sample) == 20
    assert sum(e.is_database for e in sample) == 5  # a quarter of the picks
    assert sum(e.size for e in sample) <= 10_000
    assert not {e.path for e in sample} & {"/data/empty", "/data/odd[1].txt", "/data/huge"}
    tight = choose_sample(entries, count=20, max_bytes=1500, rng=random.Random(3))
    assert sum(e.size for e in tight) <= 1500 and sum(e.is_database for e in tight) == 1


def test_check_file_outcomes(tree: list[Path], tmp_path: Path) -> None:
    hosts, big, db = tree
    target = tmp_path / "restored"
    FakeRestic(tree)(
        ["restore", "new", "--target", str(target), "--include", str(hosts), "--include", str(db)]
    )
    good = check_file(entry_for(hosts), target)
    assert good.problem is None and good.matched_original and not good.database_ok
    assert check_file(entry_for(db), target).database_ok
    assert check_file(entry_for(big), target).problem == "not restored"
    wrong_size = Entry(str(hosts), 999, entry_for(hosts).mtime)
    assert "snapshot says 999 B" in (check_file(wrong_size, target).problem or "")


def test_changed_originals_are_not_compared(tree: list[Path], tmp_path: Path) -> None:
    hosts = tree[0]
    target = tmp_path / "restored"
    FakeRestic(tree)(["restore", "new", "--target", str(target), "--include", str(hosts)])
    snapshot_entry = entry_for(hosts)
    hosts.write_text("127.0.0.1 changed\n")  # edited after the backup
    result = check_file(snapshot_entry, target)
    assert result.problem is None and not result.matched_original


def test_full_run_passes(tree: list[Path], tmp_path: Path) -> None:
    restic = FakeRestic(tree)
    report = make_test(restic, tmp_path, filters=["--host", "myhost"]).execute(NOW)
    assert report.passed and report.snapshot == Snapshot("new", restic.taken)
    assert restic.calls[0] == ["snapshots", "--json", "--host", "myhost"]
    assert "--verify" in restic.calls[2]
    assert not list((tmp_path / "scratch").iterdir())  # scratch copy removed
    title, message, priority = format_report(report)
    assert (title, priority) == ("Backup restore test passed", "low")
    assert message.startswith("3 files (") and " kB) restored from the 01 Nov " in message
    assert message.endswith("3 identical to the originals, 1 database checked.")
    d = display(report)
    assert (d["result"], d["files"], d["matched"], d["databases"], d["problem"]) == (
        "passed",
        "3",
        "3",
        "1",
        "",
    )


def test_damaged_and_missing_files_fail(tree: list[Path], tmp_path: Path) -> None:
    restic = FakeRestic(tree)
    restic.corrupt.add(str(tree[2]))
    restic.missing.add(str(tree[0]))
    report = make_test(restic, tmp_path).execute(NOW)
    assert not report.passed and len(report.problems) == 2
    title, message, priority = format_report(report)
    assert (title, priority) == ("Backup restore test FAILED", "high")
    assert f"{tree[0]}: not restored" in message
    assert f"{tree[2]}: content differs from the unchanged original" in message
    assert display(report)["result"] == "FAILED" and display(report)["problem"].startswith(
        "app.db: content differs"
    )


def test_stale_snapshot_and_restic_errors_fail(tree: list[Path], tmp_path: Path) -> None:
    stale = make_test(FakeRestic(tree, taken=NOW - timedelta(days=5)), tmp_path).execute(NOW)
    assert stale.error == "the newest snapshot is 5 days old - is the backup running?"

    def broken(args: Sequence[str]) -> str:
        raise RestoreTestError("restic snapshots failed: wrong password or no key found")

    report = make_test(broken, tmp_path).execute(NOW)
    assert format_report(report) == (
        "Backup restore test FAILED",
        "restic snapshots failed: wrong password or no key found",
        "high",
    )
    assert display(report)["snapshot"] == "-"
    assert make_test(lambda a: "[]", tmp_path).execute(NOW).error == "no snapshots found"


def test_make_runner_reports_failures() -> None:
    with pytest.raises(RestoreTestError, match="restic snapshots failed: exit code 1"):
        make_runner("false")(["snapshots"])


@pytest.mark.skipif(shutil.which("restic") is None, reason="restic is not installed")
def test_against_a_real_restic_repository(tree: list[Path], tmp_path: Path) -> None:
    env = {"PATH": os.environ["PATH"], "RESTIC_PASSWORD": "test", "RESTIC_CACHE_DIR": str(tmp_path / "cache")}
    env["RESTIC_REPOSITORY"] = str(tmp_path / "repo")
    run = make_runner(env=env)
    run(["init"])
    run(["backup", "--host", "test", str(tmp_path / "src")])
    report = make_test(run, tmp_path, filters=["--host", "test"]).execute()
    assert report.passed, format_report(report)
    assert {r.entry.path for r in report.results} == {str(p) for p in tree}
    assert (
        sum(r.matched_original for r in report.results) == 3
        and sum(r.database_ok for r in report.results) == 1
    )
