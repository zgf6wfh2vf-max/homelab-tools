from __future__ import annotations

import json
import sqlite3

import pytest

from homelab_tools.opencanary_ntfy.relay import Batcher, describe, follow, parse_event
from homelab_tools.pihole_threats.alert import (
    AlertState,
    Hit,
    ThreatDatabase,
    alert_text,
    candidate_keys,
    group_new_hits,
    report,
)

# --- Pi-hole threat alerts -----------------------------------------------------------------------


@pytest.fixture
def pihole(tmp_path) -> str:
    """Minimal pihole-FTL.db + gravity.db with the columns the tool reads."""
    gravity = sqlite3.connect(tmp_path / "gravity.db")
    gravity.executescript(
        """
        CREATE TABLE adlist (id INTEGER, address TEXT, enabled INTEGER);
        CREATE TABLE gravity (domain TEXT, adlist_id INTEGER);
        INSERT INTO adlist VALUES (1, 'https://ads.example/list.txt', 1),
                                  (3, 'https://example.org/hagezi/adblock/tif.txt', 1);
        INSERT INTO gravity VALUES ('||evil.example^', 3), ('||ads.example^', 1), ('phish.test', 3);
        """
    )
    gravity.commit()
    ftl = sqlite3.connect(tmp_path / "pihole-FTL.db")
    ftl.executescript(
        """
        CREATE TABLE queries (id INTEGER, timestamp REAL, domain TEXT, client TEXT, status INTEGER);
        CREATE TABLE client_by_id (id INTEGER, ip TEXT, name TEXT);
        INSERT INTO client_by_id VALUES (1, '192.168.1.100', 'old-name'), (2, '192.168.1.100', 'laptop'),
                                        (3, '192.168.1.9', '');
        INSERT INTO queries VALUES
            (1, 1000, 'cdn.evil.example', '192.168.1.100', 1),   -- blocked threat (listed parent)
            (2, 1001, 'ads.example', '192.168.1.100', 1),        -- blocked, but only an ad list
            (3, 1002, 'phish.test', '192.168.1.9', 2),          -- threat, allowed (before the list)
            (4, 1003, 'cdn.evil.example', '192.168.1.100', 9),   -- blocked via CNAME
            (5, 1004, 'example.com', '192.168.1.100', 2);
        """
    )
    ftl.commit()
    return str(tmp_path)


def test_candidate_keys_cover_parent_domains() -> None:
    assert candidate_keys("a.Evil.example.") == ["||a.evil.example^", "||evil.example^", "a.evil.example"]


def test_threat_database_finds_only_threat_list_hits(pihole) -> None:
    db = ThreatDatabase(pihole, "%hagezi%tif%")
    assert db.list_ids == [3]
    hits = list(db.hits())
    assert [(h.query_id, h.domain, h.client_name) for h in hits] == [
        (1, "cdn.evil.example", "laptop"),  # most recent name for the IP
        (4, "cdn.evil.example", "laptop"),
    ]
    assert [h.query_id for h in db.hits(after_id=1)] == [4]
    allowed = list(db.hits(blocked_only=False))
    assert any(h.domain == "phish.test" and h.client_name == "192.168.1.9" for h in allowed)
    assert db.max_query_id() == 5


def test_grouping_dedup_and_realert() -> None:
    state = AlertState(0, {})
    hits = [
        Hit(1, 0, "a.bad", "1.1.1.1", "pc"),
        Hit(2, 0, "b.bad", "1.1.1.1", "pc"),
        Hit(3, 0, "a.bad", "1.1.1.1", "pc"),
    ]
    assert group_new_hits(hits, state, now=100, realert_seconds=3600) == {
        ("pc", "1.1.1.1"): ["a.bad", "b.bad"]
    }
    assert group_new_hits(hits, state, now=200, realert_seconds=3600) == {}
    assert group_new_hits(hits[:1], state, now=100 + 3601, realert_seconds=3600) == {
        ("pc", "1.1.1.1"): ["a.bad"]
    }


def test_alert_state_round_trip(tmp_path) -> None:
    path = tmp_path / "s" / "state.json"
    assert AlertState.load(path) is None
    AlertState(42, {"ip|d": 1.5}).save(path)
    assert AlertState.load(path) == AlertState(42, {"ip|d": 1.5})


def test_alert_text_truncates_long_lists() -> None:
    text = alert_text("pc", "1.1.1.1", [f"d{i}" for i in range(7)])
    assert "d4" in text and "d5" not in text and "(+2 more)" in text


def test_report_includes_allowed_lookups(pihole, monkeypatch) -> None:
    monkeypatch.setattr("time.time", lambda: 2000)
    lines = report(ThreatDatabase(pihole, "%hagezi%tif%"), days=1)
    assert lines[-1] == "2 distinct device/domain pairs in the last 1 days"
    assert any("phish.test" in line for line in lines)


# --- OpenCanary relay --------------------------------------------------------------------------

SSH = {
    "src_host": "192.168.1.99",
    "dst_port": 22,
    "logtype": 4002,
    "logdata": {"USERNAME": "root", "PASSWORD": "admin"},
}


def test_describe_and_parse() -> None:
    assert describe(SSH) == "SSH login attempt on port 22 (username=root, password=admin)"
    assert describe({"logtype": 99999, "dst_port": 1}) == "event 99999 on port 1"
    assert parse_event(json.dumps(SSH)) == SSH
    assert parse_event('{"logtype": 1001, "src_host": ""}') is None  # canary's own status message
    assert parse_event("not json") is None


def test_batcher_first_alert_then_summary() -> None:
    b = Batcher("fake NAS", window=900)
    first = b.event(SSH, now=0)
    assert first is not None and first.priority == "urgent" and "fake NAS" in first.message
    for port in (21, 23, 80):
        assert b.event(SSH | {"dst_port": port, "logtype": 3000}, now=10) is None
    assert b.flush(now=899) == []
    (summary,) = b.flush(now=900)
    assert summary.title == "Honeypot: 192.168.1.99 still active" and "3 more events" in summary.message
    assert b.event(SSH, now=1000) is not None  # a new window starts with a fresh first alert


def test_batcher_drops_quiet_sources() -> None:
    b = Batcher("x", window=60)
    b.event(SSH, now=0)
    assert b.flush(now=61) == [] and b.pending == {}


def test_follow_handles_append_truncate_and_rotation(tmp_path) -> None:
    log = tmp_path / "opencanary.log"
    log.write_text("old line\n")
    lines = follow(str(log), sleep=lambda s: None)
    assert next(lines) is None  # starts at the end: old events are not replayed
    with log.open("a") as f:
        f.write("one\n")
    assert next(lines) == "one\n"
    log.write_text("")  # truncated (copytruncate)
    assert next(lines) is None or True
    log.write_text("after truncate\n")
    seen = [next(lines) for _ in range(4)]
    assert "after truncate\n" in seen
    log.rename(tmp_path / "opencanary.log.1")  # rotated
    log.write_text("")
    next(lines)
    with log.open("a") as f:
        f.write("new file\n")
    seen = [next(lines) for _ in range(4)]
    assert "new file\n" in seen
