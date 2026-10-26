"""Runs scripts/ssh-login-alert with PAM-style environments and a fake curl."""

from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path

import pytest

SCRIPT = Path(__file__).parent.parent / "scripts" / "ssh-login-alert"


@pytest.fixture
def fake_curl(tmp_path: Path) -> Path:
    """A curl stand-in that records its arguments, one line per call."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    log = tmp_path / "curl.log"
    curl = bin_dir / "curl"
    curl.write_text(f'#!/bin/sh\nprintf "%s|" "$@" >> "{log}"\necho >> "{log}"\n')
    curl.chmod(0o755)
    return log


def run(tmp_path: Path, log: Path, **env: str) -> list[str]:
    """Run the script; return the curl calls it made (waiting briefly for the background send)."""
    base = {
        "PATH": f"{tmp_path / 'bin'}:{os.environ['PATH']}",
        "SSH_ALERT_CONFIG": str(tmp_path / "no-config"),
        "NTFY_URL": "https://ntfy.example/topic",
        "PAM_TYPE": "open_session",
        "PAM_USER": "admin",
        "PAM_RHOST": "192.0.2.181",
        "SSH_ALERT_HOST": "server",
    }
    result = subprocess.run(
        [str(SCRIPT)], env=base | env, capture_output=True, text=True, check=False, timeout=10
    )
    assert result.returncode == 0  # never fails the PAM stack
    deadline = time.time() + 1.5
    while time.time() < deadline and not log.exists():
        time.sleep(0.05)
    time.sleep(0.1)
    return log.read_text().splitlines() if log.exists() else []


def test_login_sends_an_alert(tmp_path: Path, fake_curl: Path) -> None:
    calls = run(tmp_path, fake_curl, SSH_ALERT_NAMES="10.0.0.9=laptop 192.0.2.181=my_phone")
    assert len(calls) == 1
    args = calls[0].split("|")
    assert "Title: SSH login on server" in args and args[-2] == "https://ntfy.example/topic"
    message = args[args.index("-d") + 1]
    assert message.startswith("admin logged in from 192.0.2.181 (my phone) at ")


@pytest.mark.parametrize(
    ("allow", "alerts"),
    [
        ("192.0.2.181", False),
        ("192.0.2.", False),
        ("192.0.2.18", True),
        ("10.0.0. 192.0.2.50", True),
    ],
)
def test_allow_list(tmp_path: Path, fake_curl: Path, allow: str, alerts: bool) -> None:
    assert bool(run(tmp_path, fake_curl, SSH_ALERT_ALLOW=allow)) is alerts


def test_session_close_and_missing_topic_are_ignored(tmp_path: Path, fake_curl: Path) -> None:
    assert run(tmp_path, fake_curl, PAM_TYPE="close_session") == []
    assert run(tmp_path, fake_curl, NTFY_URL="") == []


def test_settings_file(tmp_path: Path, fake_curl: Path) -> None:
    config = tmp_path / "settings"
    config.write_text('SSH_ALERT_ALLOW="192.0.2.181"\n')
    assert run(tmp_path, fake_curl, SSH_ALERT_CONFIG=str(config)) == []


def test_returns_immediately_even_if_curl_hangs(tmp_path: Path) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "curl").write_text("#!/bin/sh\nsleep 5\n")
    (bin_dir / "curl").chmod(0o755)
    env = {
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "SSH_ALERT_CONFIG": "/nonexistent",
        "NTFY_URL": "https://ntfy.example/topic",
        "PAM_TYPE": "open_session",
        "PAM_RHOST": "10.1.1.1",
    }
    started = time.time()
    subprocess.run([str(SCRIPT)], env=env, check=True, timeout=10)
    assert time.time() - started < 2  # the login is not held up by the notification
