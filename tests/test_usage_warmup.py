from __future__ import annotations

from datetime import UTC, datetime, timedelta

from homelab_tools.usage.warmup import Warmup, duration, find_warmups, is_slow, usual_minutes

T0 = datetime(2026, 11, 12, 4, 0, tzinfo=UTC)


def morning(day: int, minutes: int | None, start_temp: float = 20.0, goal: float = 21.5) -> tuple[dict, dict]:
    """A night at a 14 °C target, raised to ``goal`` at 06:00 UTC.

    The room reaches the target after ``minutes`` (or only gets halfway, if ``None``) and the target
    drops back at 22:00.
    """
    base = T0 + timedelta(days=day)
    target, inside = {}, {}
    for step in range(0, 20 * 60, 2):
        moment = base + timedelta(minutes=step)
        since = step - 120  # minutes since 06:00
        target[moment] = goal if 0 <= since < 16 * 60 else 14.0
        if since < 0:
            inside[moment] = start_temp
        elif minutes is None:
            inside[moment] = min(start_temp + (goal - start_temp) / 2, start_temp + since / 100)
        else:
            # within 0.2 °C until exactly ``minutes``, so the 0.1 °C tolerance doesn't cut it short
            linear = start_temp + (goal - start_temp) * since / minutes
            inside[moment] = goal if since >= minutes else min(goal - 0.2, linear)
    return inside, target


def test_find_warmups_measures_time_to_target() -> None:
    inside, target = morning(0, 150)
    outside = {T0: 3.0}
    [w] = find_warmups("Upstairs", inside, target, outside)
    assert w.start == T0 + timedelta(hours=2) and w.minutes == 150 and w.outside == 3.0
    assert w.start_temp == 20.0 and w.target == 21.5


def test_find_warmups_reports_a_target_never_reached_and_skips_unfinished_ones() -> None:
    inside, target = morning(0, None)
    [w] = find_warmups("Upstairs", inside, target)
    assert w.minutes is None and w.best == 20.75
    unfinished = {k: v for k, v in target.items() if k < T0 + timedelta(hours=4)}
    assert find_warmups("Upstairs", inside, unfinished) == []  # still heating at the end of the data


def test_find_warmups_ignores_small_gaps_and_small_steps() -> None:
    inside, target = morning(0, 30, start_temp=21.0)  # only 0.5 °C to go
    assert find_warmups("Upstairs", inside, target) == []
    inside, target = morning(0, 60, start_temp=19.0, goal=14.5)  # target only up 0.5 °C
    assert find_warmups("Upstairs", inside, target) == []


def test_usual_and_slow() -> None:
    history = []
    for day in range(7):
        inside, target = morning(day, 150)
        history += find_warmups("Upstairs", inside, target)
    inside, target = morning(7, 240)
    [late] = find_warmups("Upstairs", inside, target)
    usual = usual_minutes(history + [late], late)
    assert usual == 150
    assert is_slow(late, usual, 1.5, 45)  # 240 > 225 and 90 more
    assert not is_slow(late, usual, 1.7, 45)
    assert usual_minutes(history[:3] + [late], late) is None  # not enough history
    failed = Warmup("Upstairs", late.start, 21.5, 20.0, None, 20.6, None)
    assert is_slow(failed, usual, 1.5, 45) and not is_slow(failed, None, 1.5, 45)


def test_duration() -> None:
    assert duration(45) == "45m" and duration(125) == "2h 05m" and duration(59.6) == "1h 00m"
