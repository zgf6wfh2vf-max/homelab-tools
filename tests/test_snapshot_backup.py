"""Runs scripts/sqlite-snapshot-backup for real against temporary directories."""

from __future__ import annotations

import os
import shutil
import sqlite3
import subprocess
from datetime import date
from pathlib import Path

import pytest

SCRIPT = Path(__file__).parent.parent / "scripts" / "sqlite-snapshot-backup"
pytestmark = pytest.mark.skipif(
    not (shutil.which("rsync") and shutil.which("sqlite3")), reason="needs rsync and sqlite3"
)


def run(src: Path, dest: Path, **extra: str) -> subprocess.CompletedProcess[str]:
    env = os.environ | {"SNAPSHOT_SOURCE": str(src), "SNAPSHOT_DEST": str(dest)} | extra
    return subprocess.run([str(SCRIPT)], env=env, capture_output=True, text=True, check=False)


@pytest.fixture
def stacks(tmp_path: Path) -> Path:
    src = tmp_path / "stacks"
    (src / "app" / "data").mkdir(parents=True)
    (src / "app" / "compose.yaml").write_text("services: {}\n")
    db = sqlite3.connect(src / "app" / "data" / "app.db")
    db.execute("PRAGMA journal_mode=WAL")  # live database with a WAL file, like a running app
    db.execute("CREATE TABLE t (x)")
    db.executemany("INSERT INTO t VALUES (?)", [(i,) for i in range(100)])
    db.commit()
    (src / "app" / "data" / "bolt.db").write_bytes(b"\x00\x00\x00\x00BoltDB-ish binary")
    return src


def test_snapshot_contents(tmp_path: Path, stacks: Path) -> None:
    dest = tmp_path / "backups"
    result = run(stacks, dest)
    assert result.returncode == 0, result.stderr
    snap = dest / date.today().isoformat()
    assert (snap / "app" / "compose.yaml").read_text() == "services: {}\n"
    copy = sqlite3.connect(snap / "app" / "data" / "app.db")
    assert copy.execute("SELECT count(*) FROM t").fetchone() == (100,)
    assert copy.execute("PRAGMA integrity_check").fetchone() == ("ok",)
    assert not (snap / "app" / "data" / "app.db-wal").exists()
    assert (snap / "app" / "data" / "bolt.db").read_bytes().endswith(b"BoltDB-ish binary")
    assert not list(dest.glob("*.partial"))


def test_unchanged_files_are_hard_links_to_previous_snapshot(tmp_path: Path, stacks: Path) -> None:
    dest = tmp_path / "backups"
    previous = dest / "2000-01-01"
    previous.mkdir(parents=True)
    shutil.copytree(stacks, previous, dirs_exist_ok=True)
    assert run(stacks, dest).returncode == 0
    today = dest / date.today().isoformat()
    assert (today / "app" / "compose.yaml").stat().st_ino == (previous / "app" / "compose.yaml").stat().st_ino


def test_keeps_only_newest_snapshots(tmp_path: Path, stacks: Path) -> None:
    dest = tmp_path / "backups"
    for day in ("2000-01-01", "2000-01-02", "2000-01-03"):
        (dest / day).mkdir(parents=True)
    assert run(stacks, dest, SNAPSHOT_KEEP="2").returncode == 0
    assert sorted(p.name for p in dest.iterdir()) == ["2000-01-03", date.today().isoformat()]


def test_extra_directory(tmp_path: Path, stacks: Path) -> None:
    extra = tmp_path / "appdata"
    extra.mkdir()
    sqlite3.connect(extra / "x.sqlite").execute("CREATE TABLE y (z)")
    dest = tmp_path / "backups"
    assert run(stacks, dest, SNAPSHOT_EXTRA=str(extra)).returncode == 0
    assert (dest / date.today().isoformat() / "_extra" / "x.sqlite").exists()


def test_missing_source_fails_clearly(tmp_path: Path) -> None:
    result = run(tmp_path / "nope", tmp_path / "backups")
    assert result.returncode != 0 and "does not exist" in result.stderr
