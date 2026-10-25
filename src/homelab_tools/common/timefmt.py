"""Human-friendly time formatting shared by the tools' status output and notifications."""

from __future__ import annotations

from datetime import datetime


def duration(seconds: float | None) -> str:
    """Format a duration as ``"2h 05m"``; ``None`` or negative values give ``"0h 00m"``."""
    total = max(0, int(seconds or 0))
    return f"{total // 3600}h {total % 3600 // 60:02d}m"


def when(moment: datetime, now: datetime | None = None) -> str:
    """Describe a past moment briefly, relative to ``now``.

    * same day: ``"14:05"``
    * within the last week: ``"Sat 14:05"``
    * older: ``"12 Oct"``

    Both datetimes are converted to the local timezone before comparing calendar days.
    """
    local = moment.astimezone()
    today = (now or datetime.now()).astimezone().date()
    days = (today - local.date()).days
    if days == 0:
        return local.strftime("%H:%M")
    if 0 < days < 7:
        return local.strftime("%a %H:%M")
    return f"{local.day} {local.strftime('%b')}"


def minutes_ago(seconds: float) -> str:
    """Format an age: ``"7 min ago"`` up to two hours, then ``"3 h ago"``."""
    minutes = max(0, int(seconds // 60))
    return f"{minutes} min ago" if minutes < 120 else f"{minutes // 60} h ago"
