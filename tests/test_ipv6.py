from __future__ import annotations

from pathlib import Path

from homelab_tools.ipv6.watch import State, display, metrics, parse, run

HUB_RA = """Soliciting ff02::2 (ff02::2) on eth0...

Hop limit                 :           64 (      0x40)
Stateful address conf.    :           No
Stateful other conf.      :          Yes
Router preference         :       medium
Router lifetime           :         1800 (0x00000708) seconds
Reachable time            :  unspecified (0x00000000)
 Prefix                   : 2001:db8:1::/64
  On-link                 :          Yes
  Autonomous address conf.:          Yes
  Valid time              :        86400 (0x00015180) seconds
 Recursive DNS server     : 2001:db8:1::1
  DNS server lifetime     :         1800 (0x00000708) seconds
 Source link-layer address: 02:00:00:00:00:01
 from fe80::1
"""

PIHOLE_RA = """Soliciting ff02::2 (ff02::2) on eth0...

Router lifetime           :            0 (0x00000000) seconds
 Recursive DNS server     : 2001:db8:1::53
 from fe80::53
"""

NO_ANSWER = "Soliciting ff02::2 (ff02::2) on eth0...\n"


def test_parse_reads_each_router() -> None:
    routers = parse(HUB_RA + PIHOLE_RA)
    assert [(r.source, r.lifetime_s, r.prefixes, r.dns) for r in routers] == [
        ("fe80::1", 1800, ("2001:db8:1::/64",), ("2001:db8:1::1",)),
        ("fe80::53", 0, (), ("2001:db8:1::53",)),
    ]
    assert routers[0].announces_ipv6 and not routers[1].announces_ipv6
    assert parse(NO_ANSWER) == []


def test_alerts_once_when_ipv6_comes_back_then_clears(tmp_path: Path) -> None:
    answers = {"now": NO_ANSWER}
    state = State()
    assert run("eth0", lambda _: answers["now"], state, False, set())[0] == []
    answers["now"] = HUB_RA
    alerts = run("eth0", lambda _: answers["now"], state, False, set())[0]
    assert len(alerts) == 1
    title, message, priority = alerts[0]
    assert (title, priority) == ("IPv6: DNS may bypass Pi-hole", "high")
    assert message.startswith(
        "A router (fe80::1) is announcing IPv6 with its own DNS server 2001:db8:1::1. Phones may stop"
    )
    assert run("eth0", lambda _: answers["now"], state, False, set())[0] == []  # told once
    state.save(tmp_path / "s.json")
    assert State.load(tmp_path / "s.json") == state
    answers["now"] = NO_ANSWER
    assert run("eth0", lambda _: answers["now"], state, False, set())[0] == [
        ("IPv6: OK again", "Cleared: a router (fe80::1) is announcing IPv6 with its own DNS server "
         "2001:db8:1::1.", "low")
    ]  # fmt: skip


def test_expected_ipv6_only_checks_dns_servers() -> None:
    state = State()
    alerts, routers, problems = run("eth0", lambda _: HUB_RA + PIHOLE_RA, state, True, {"2001:db8:1::53"})
    assert list(problems) == ["dns:2001:db8:1::1"]
    assert alerts[0][1].startswith("A router (fe80::1) is announcing DNS server 2001:db8:1::1.")
    assert run("eth0", lambda _: HUB_RA, State(), True, {"2001:db8:1::1"})[2] == {}


def test_metrics_and_display() -> None:
    routers = parse(HUB_RA + PIHOLE_RA)
    _, _, problems = run("eth0", lambda _: HUB_RA + PIHOLE_RA, State(), False, {"2001:db8:1::53"})
    assert [s.to_line() for s in metrics(routers, problems)] == [
        "ipv6 routers=2,announcing=1,dns_servers=2,problems=1"
    ]
    assert display(routers, problems) == {
        "ipv6": "on (1 router)",
        "dns": "2001:db8:1::1, 2001:db8:1::53",
        "problems": "1",
    }
    assert display([], {}) == {"ipv6": "off", "dns": "none announced", "problems": "0"}
